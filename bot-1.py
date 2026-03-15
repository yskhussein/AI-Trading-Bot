import os
import asyncio
import logging
import json
import requests
import feedparser
import threading
from datetime import datetime, timedelta
from groq import Groq
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── ENV VARS ──────────────────────────────────────────────────────────────────
GROQ_API_KEY     = os.environ["GROQ_API_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
ADMIN_CHAT_ID    = os.environ["ADMIN_CHAT_ID"]
CRYPTOPANIC_KEY  = os.environ.get("CRYPTOPANIC_KEY", "")
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_SECRET    = os.environ.get("REDDIT_SECRET", "")

# ── TRADING CONFIG ────────────────────────────────────────────────────────────
COINS           = ["bitcoin", "ethereum", "solana", "dogecoin", "shiba-inu"]
COIN_SYMBOLS    = {"bitcoin": "BTC", "ethereum": "ETH", "solana": "SOL", "dogecoin": "DOGE", "shiba-inu": "SHIB"}
RISK_PER_TRADE  = 0.10
MIN_CONFIDENCE  = 60
SCAN_INTERVAL   = 900
DAILY_LOSS_CAP  = 0.20
PAPER_BALANCE   = 10000.0

# ── SHARED STATE (used by both bot + web server) ──────────────────────────────
state = {
    "paused": False,
    "open_trades": {},
    "trade_history": [],
    "daily_start_balance": None,
    "daily_pnl": 0.0,
    "total_pnl": 0.0,
    "start_time": datetime.utcnow().isoformat(),
    "trades_today": 0,
    "wins": 0,
    "losses": 0,
    "last_scan": None,
    "cash_balance": PAPER_BALANCE,
    "portfolio_history": [],   # [{time, value}]
    "ai_log": [],              # last 50 AI decisions
    "news_feed": [],           # last 20 news items
    "prices": {},              # {coin_id: price_data}
    "risk_per_trade": RISK_PER_TRADE,
}

groq_client  = Groq(api_key=GROQ_API_KEY)
telegram_bot = Bot(token=TELEGRAM_TOKEN)


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA — CoinGecko (free, no key needed)
# ══════════════════════════════════════════════════════════════════════════════

def get_prices() -> dict:
    ids = ",".join(COINS)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={
                "vs_currency": "usd",
                "ids": ids,
                "order": "market_cap_desc",
                "price_change_percentage": "1h,24h,7d",
            },
            timeout=15,
        )
        result = {}
        for coin in r.json():
            result[coin["id"]] = {
                "price":        coin["current_price"],
                "change_1h":    coin.get("price_change_percentage_1h_in_currency", 0) or 0,
                "change_24h":   coin.get("price_change_percentage_24h", 0) or 0,
                "change_7d":    coin.get("price_change_percentage_7d_in_currency", 0) or 0,
                "volume_24h":   coin.get("total_volume", 0) or 0,
                "market_cap":   coin.get("market_cap", 0) or 0,
                "high_24h":     coin.get("high_24h", 0) or 0,
                "low_24h":      coin.get("low_24h", 0) or 0,
                "symbol":       coin["symbol"].upper(),
                "name":         coin["name"],
                "image":        coin.get("image", ""),
            }
        state["prices"] = result
        return result
    except Exception as e:
        logger.error(f"CoinGecko error: {e}")
        return state.get("prices", {})


def get_sparkline(coin_id: str) -> list:
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": "1", "interval": "hourly"},
            timeout=15,
        )
        prices = r.json().get("prices", [])
        return [p[1] for p in prices[-24:]]
    except:
        return []


def calculate_momentum(coin_id: str, prices_data: dict) -> dict:
    pd = prices_data.get(coin_id, {})
    change_1h  = pd.get("change_1h", 0)
    change_24h = pd.get("change_24h", 0)
    volume     = pd.get("volume_24h", 0)

    if change_1h > 1 and change_24h > 2:
        trend = "bullish"
    elif change_1h < -1 and change_24h < -2:
        trend = "bearish"
    else:
        trend = "neutral"

    return {
        "trend":        trend,
        "momentum_pct": round(change_1h, 3),
        "volume_spike": volume > 1_000_000_000,
        "change_1h":    change_1h,
        "change_24h":   change_24h,
    }


def get_portfolio_value(prices_data: dict) -> float:
    total = state["cash_balance"]
    for coin_id, trade in state["open_trades"].items():
        price = prices_data.get(coin_id, {}).get("price", trade["entry_price"])
        total += price * trade["qty"]
    return total


# ══════════════════════════════════════════════════════════════════════════════
# NEWS GATHERING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_cryptopanic(symbol: str) -> list:
    if not CRYPTOPANIC_KEY:
        return []
    try:
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={"auth_token": CRYPTOPANIC_KEY, "currencies": symbol, "public": "true"},
            timeout=10,
        )
        items = r.json().get("results", [])
        return [{"source": "CryptoPanic", "title": i["title"], "url": i.get("url","#"), "time": i.get("published_at","")} for i in items[:4]]
    except Exception as e:
        logger.warning(f"CryptoPanic error: {e}")
        return []


def fetch_rss_news(coin_name: str, symbol: str) -> list:
    feeds = [
        (f"https://cointelegraph.com/rss/tag/{symbol.lower()}", "CoinTelegraph"),
        (f"https://news.google.com/rss/search?q={coin_name}+crypto&hl=en-US&gl=US&ceid=US:en", "Google News"),
    ]
    items = []
    for url, source in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:3]:
                items.append({
                    "source": source,
                    "title":  entry.title,
                    "url":    entry.get("link", "#"),
                    "time":   entry.get("published", ""),
                })
        except Exception as e:
            logger.warning(f"RSS error: {e}")
    return items


def gather_all_news(coin_id: str) -> tuple:
    symbol    = COIN_SYMBOLS.get(coin_id, coin_id.upper())
    coin_name = coin_id.replace("-", " ")
    items     = fetch_cryptopanic(symbol) + fetch_rss_news(coin_name, symbol)

    # Update global news feed
    for item in items:
        item["coin"] = symbol
        if item not in state["news_feed"]:
            state["news_feed"].insert(0, item)
    state["news_feed"] = state["news_feed"][:30]

    headlines = "\n".join([f"[{i['source']}] {i['title']}" for i in items])
    return headlines or "No recent news found.", items


# ══════════════════════════════════════════════════════════════════════════════
# AI DECISION ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def ai_analyze(coin_id: str, news: str, price_data: dict, momentum: dict, open_trade) -> dict:
    symbol   = COIN_SYMBOLS.get(coin_id, coin_id.upper())
    has_open = open_trade is not None

    system_prompt = """You are an expert crypto trading AI for a paper trading simulation.
Analyze news sentiment, price data, and momentum. Respond ONLY with valid JSON, no markdown, no fences:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": <integer 0-100>,
  "reasoning": "<2-3 sentences>",
  "sentiment": "bullish" | "bearish" | "neutral",
  "key_news": "<most impactful headline>",
  "risk_level": "low" | "medium" | "high",
  "suggested_hold_duration": "<e.g. 2-6 hours>",
  "price_target": <float or null>,
  "stop_suggestion": <float or null>,
  "market_summary": "<1 sentence market overview>"
}
Rules: BUY only if confidence>=60 and no open trade. SELL only if confidence>=60 and open trade exists. HOLD otherwise."""

    user_msg = f"""Analyze {symbol}:

PRICE: ${price_data.get('price','N/A')}
1h Change: {price_data.get('change_1h',0):.2f}%
24h Change: {price_data.get('change_24h',0):.2f}%
7d Change: {price_data.get('change_7d',0):.2f}%
Volume 24h: ${price_data.get('volume_24h',0):,.0f}
High 24h: ${price_data.get('high_24h','N/A')}
Low 24h: ${price_data.get('low_24h','N/A')}

MOMENTUM: {momentum.get('trend')} | {momentum.get('momentum_pct')}% | Volume spike: {momentum.get('volume_spike')}
OPEN TRADE: {'YES - holding ' + symbol if has_open else 'NO position'}

NEWS:
{news}

JSON only:"""

    try:
        resp = groq_client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_msg}],
            temperature=0.2,
            max_tokens=600,
        )
        raw = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        decision = json.loads(raw)

        # Log AI decision
        log_entry = {
            "time":       datetime.utcnow().isoformat(),
            "coin":       symbol,
            "action":     decision.get("action","HOLD"),
            "confidence": decision.get("confidence",0),
            "sentiment":  decision.get("sentiment","neutral"),
            "reasoning":  decision.get("reasoning",""),
            "key_news":   decision.get("key_news",""),
            "risk_level": decision.get("risk_level","medium"),
            "market_summary": decision.get("market_summary",""),
        }
        state["ai_log"].insert(0, log_entry)
        state["ai_log"] = state["ai_log"][:50]

        return decision
    except Exception as e:
        logger.error(f"AI error {coin_id}: {e}")
        fallback = {"action":"HOLD","confidence":0,"reasoning":str(e),"sentiment":"neutral",
                    "key_news":"","risk_level":"high","suggested_hold_duration":"N/A",
                    "price_target":None,"stop_suggestion":None,"market_summary":"Error"}
        state["ai_log"].insert(0, {**fallback, "time": datetime.utcnow().isoformat(), "coin": symbol})
        return fallback


# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADE EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def paper_buy(coin_id: str, price: float) -> dict:
    usdt_amount = state["cash_balance"] * state["risk_per_trade"]
    qty         = usdt_amount / price
    state["cash_balance"] -= usdt_amount
    trade = {
        "qty":         qty,
        "entry_price": price,
        "entry_time":  datetime.utcnow().isoformat(),
        "usdt_spent":  usdt_amount,
        "coin":        COIN_SYMBOLS.get(coin_id, coin_id.upper()),
    }
    state["open_trades"][coin_id] = trade
    logger.info(f"PAPER BUY {coin_id}: qty={qty:.6f} @ ${price}")
    return trade


def paper_sell(coin_id: str, price: float) -> float:
    trade = state["open_trades"].get(coin_id)
    if not trade:
        return 0
    proceeds = price * trade["qty"]
    pnl      = proceeds - trade["usdt_spent"]
    state["cash_balance"] += proceeds

    state["trade_history"].insert(0, {
        "symbol":      trade["coin"],
        "coin_id":     coin_id,
        "entry_price": trade["entry_price"],
        "exit_price":  price,
        "qty":         trade["qty"],
        "pnl":         round(pnl, 4),
        "pnl_pct":     round((pnl / trade["usdt_spent"]) * 100, 2),
        "entry_time":  trade["entry_time"],
        "exit_time":   datetime.utcnow().isoformat(),
        "usdt_spent":  trade["usdt_spent"],
    })
    state["trade_history"] = state["trade_history"][:100]
    del state["open_trades"][coin_id]
    logger.info(f"PAPER SELL {coin_id}: pnl={pnl:.4f}")
    return pnl


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════════

async def notify(msg: str):
    try:
        await telegram_bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Telegram error: {e}")


def format_trade_alert(coin_id: str, action: str, decision: dict, price_data: dict, trade=None, pnl=None) -> str:
    symbol  = COIN_SYMBOLS.get(coin_id, coin_id.upper())
    price   = price_data.get("price", 0)
    c1h     = price_data.get("change_1h", 0)
    c24h    = price_data.get("change_24h", 0)
    vol     = price_data.get("volume_24h", 0)
    a_emoji = "🟢" if action == "BUY" else "🔴" if action == "SELL" else "⚪"
    s_emoji = "📈" if decision.get("sentiment") == "bullish" else "📉" if decision.get("sentiment") == "bearish" else "➡️"

    msg  = f"📝 <b>PAPER TRADE</b> | {a_emoji} <b>{action} {symbol}</b> {s_emoji}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"💰 <b>Price:</b> ${price:,.6f}\n"
    msg += f"⏱ <b>1h:</b> {c1h:+.2f}% | <b>24h:</b> {c24h:+.2f}%\n"
    msg += f"📦 <b>Volume:</b> ${vol:,.0f}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"🤖 <b>Confidence:</b> {decision.get('confidence',0)}%\n"
    msg += f"🎯 <b>Sentiment:</b> {decision.get('sentiment','N/A').capitalize()}\n"
    msg += f"⚠️ <b>Risk:</b> {decision.get('risk_level','N/A').capitalize()}\n"
    msg += f"⏳ <b>Hold:</b> {decision.get('suggested_hold_duration','N/A')}\n"
    if decision.get("price_target"):
        msg += f"🎯 <b>Target:</b> ${decision['price_target']:,.4f}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📰 {decision.get('key_news','N/A')}\n"
    msg += f"🧠 {decision.get('reasoning','N/A')}\n"
    if action == "SELL" and trade and pnl is not None:
        held    = datetime.utcnow() - datetime.fromisoformat(trade["entry_time"])
        hours   = held.total_seconds() / 3600
        p_emoji = "✅" if pnl >= 0 else "❌"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"{p_emoji} <b>PnL:</b> {'+' if pnl>=0 else ''}{pnl:.4f} USDT | Held {hours:.1f}h\n"
        msg += f"💵 <b>Cash Balance:</b> ${state['cash_balance']:,.2f}\n"
    msg += f"\n🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
    return msg


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def scan_and_trade():
    if state["paused"]:
        logger.info("Bot paused.")
        return

    prices_data = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    logger.info(f"Scan | Portfolio: ${portfolio_val:,.2f}")

    if state["daily_start_balance"] is None:
        state["daily_start_balance"] = portfolio_val

    # Daily loss check
    if state["daily_start_balance"] and state["daily_start_balance"] > 0:
        daily_loss = (state["daily_start_balance"] - portfolio_val) / state["daily_start_balance"]
        if daily_loss >= DAILY_LOSS_CAP:
            state["paused"] = True
            await notify(
                f"🚨 <b>DAILY LOSS LIMIT HIT</b>\n"
                f"Lost {daily_loss*100:.1f}% — bot paused.\n"
                f"Portfolio: ${portfolio_val:,.2f}\nSend /resume to restart."
            )
            return

    state["last_scan"] = datetime.utcnow().isoformat()
    state["portfolio_history"].append({
        "time":  datetime.utcnow().strftime("%H:%M"),
        "value": round(portfolio_val, 2),
    })
    state["portfolio_history"] = state["portfolio_history"][-96:]  # keep 24h of 15-min snapshots

    for coin_id in COINS:
        try:
            price_data = prices_data.get(coin_id, {})
            if not price_data:
                continue
            momentum   = calculate_momentum(coin_id, prices_data)
            news, _    = gather_all_news(coin_id)
            open_trade = state["open_trades"].get(coin_id)
            decision   = ai_analyze(coin_id, news, price_data, momentum, open_trade)

            action     = decision.get("action", "HOLD")
            confidence = decision.get("confidence", 0)
            price      = price_data["price"]

            logger.info(f"{coin_id}: {action} @ {confidence}%")

            if action == "BUY" and confidence >= MIN_CONFIDENCE and not open_trade:
                if state["cash_balance"] >= price * 0.01:
                    trade = paper_buy(coin_id, price)
                    state["trades_today"] += 1
                    await notify(format_trade_alert(coin_id, "BUY", decision, price_data, trade))

            elif action == "SELL" and confidence >= MIN_CONFIDENCE and open_trade:
                pnl = paper_sell(coin_id, price)
                state["daily_pnl"]  += pnl
                state["total_pnl"]  += pnl
                state["wins" if pnl >= 0 else "losses"] += 1
                await notify(format_trade_alert(coin_id, "SELL", decision, price_data, open_trade, pnl))

            await asyncio.sleep(2)

        except Exception as e:
            logger.error(f"Error {coin_id}: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# REPORTS
# ══════════════════════════════════════════════════════════════════════════════

async def send_daily_report():
    prices_data   = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    total         = state["wins"] + state["losses"]
    win_rate      = (state["wins"] / total * 100) if total > 0 else 0
    await notify(
        f"📊 <b>DAILY REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Portfolio:</b> ${portfolio_val:,.2f}\n"
        f"💵 <b>Cash:</b> ${state['cash_balance']:,.2f}\n"
        f"{'📈' if state['daily_pnl']>=0 else '📉'} <b>Today's PnL:</b> {'+' if state['daily_pnl']>=0 else ''}{state['daily_pnl']:.2f} USDT\n"
        f"📊 <b>Total PnL:</b> {'+' if state['total_pnl']>=0 else ''}{state['total_pnl']:.2f} USDT\n"
        f"🔄 <b>Trades Today:</b> {state['trades_today']}\n"
        f"✅ {state['wins']} wins | ❌ {state['losses']} losses | 🎯 {win_rate:.1f}% win rate\n"
        f"📂 <b>Open:</b> {len(state['open_trades'])}\n"
        f"🕐 {datetime.utcnow().strftime('%Y-%m-%d')} UTC"
    )
    state["daily_pnl"] = 0.0
    state["trades_today"] = 0
    state["daily_start_balance"] = None


async def send_weekly_report():
    prices_data   = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    total         = state["wins"] + state["losses"]
    win_rate      = (state["wins"] / total * 100) if total > 0 else 0
    roi           = ((portfolio_val - PAPER_BALANCE) / PAPER_BALANCE) * 100
    await notify(
        f"📅 <b>WEEKLY REPORT</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 <b>Portfolio:</b> ${portfolio_val:,.2f}\n"
        f"📊 <b>Total PnL:</b> {'+' if state['total_pnl']>=0 else ''}{state['total_pnl']:.2f} USDT\n"
        f"📈 <b>ROI:</b> {roi:+.2f}% (started with $10,000)\n"
        f"🔄 <b>Total Trades:</b> {total}\n"
        f"✅ {state['wins']} wins | ❌ {state['losses']} losses | 🎯 {win_rate:.1f}%\n"
        f"🤖 Running since: {state['start_time'][:10]}"
    )


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM COMMANDS
# ══════════════════════════════════════════════════════════════════════════════

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prices_data   = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    open_lines    = ""
    for cid, t in state["open_trades"].items():
        cur = prices_data.get(cid, {}).get("price", t["entry_price"])
        unr = (cur - t["entry_price"]) * t["qty"]
        open_lines += f"\n  • {t['coin']}: ${t['entry_price']:,.4f}→${cur:,.4f} | {'+' if unr>=0 else ''}{unr:.2f}"
    await update.message.reply_text(
        f"🤖 <b>BOT STATUS</b> (📝 PAPER MODE)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{'🟢 RUNNING' if not state['paused'] else '🔴 PAUSED'}\n"
        f"💼 <b>Portfolio:</b> ${portfolio_val:,.2f}\n"
        f"💵 <b>Cash:</b> ${state['cash_balance']:,.2f}\n"
        f"📊 <b>Total PnL:</b> {'+' if state['total_pnl']>=0 else ''}{state['total_pnl']:.2f}\n"
        f"📂 <b>Open:</b> {len(state['open_trades'])}{open_lines or ' — None'}\n"
        f"🕐 Last scan: {state['last_scan'] or 'Not yet'}",
        parse_mode=ParseMode.HTML,
    )

async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = True
    await update.message.reply_text("⏸ Bot <b>paused</b>.", parse_mode=ParseMode.HTML)

async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["paused"] = False
    await update.message.reply_text("▶️ Bot <b>resumed</b>.", parse_mode=ParseMode.HTML)

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    prices_data   = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    roi           = ((portfolio_val - PAPER_BALANCE) / PAPER_BALANCE) * 100
    await update.message.reply_text(
        f"💼 <b>Portfolio:</b> ${portfolio_val:,.2f}\n"
        f"💵 <b>Cash:</b> ${state['cash_balance']:,.2f}\n"
        f"📈 <b>ROI:</b> {roi:+.2f}%\n"
        f"📊 <b>PnL:</b> {'+' if state['total_pnl']>=0 else ''}{state['total_pnl']:.2f} USDT",
        parse_mode=ParseMode.HTML,
    )

async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not state["trade_history"]:
        await update.message.reply_text("📭 No trades yet.")
        return
    lines = ["📋 <b>LAST 10 TRADES</b>\n━━━━━━━━━━━━━━━━━━━━"]
    for t in state["trade_history"][:10]:
        e = "✅" if t["pnl"] >= 0 else "❌"
        lines.append(f"{e} {t['symbol']} | {'+' if t['pnl']>=0 else ''}{t['pnl']:.2f} USDT ({t['pnl_pct']:+.1f}%) | {t['exit_time'][:10]}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_forcesell(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not state["open_trades"]:
        await update.message.reply_text("📭 No open trades.")
        return
    prices_data = get_prices()
    for coin_id in list(state["open_trades"].keys()):
        price = prices_data.get(coin_id, {}).get("price", state["open_trades"][coin_id]["entry_price"])
        pnl   = paper_sell(coin_id, price)
        state["total_pnl"] += pnl
        await update.message.reply_text(f"✅ Closed {COIN_SYMBOLS.get(coin_id,coin_id)} | PnL: {'+' if pnl>=0 else ''}{pnl:.2f} USDT")


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

async def scheduler(app: Application):
    last_daily  = datetime.utcnow().date()
    last_weekly = datetime.utcnow().isocalendar()[1]

    await notify(
        f"🚀 <b>AI Paper Trading Bot Online!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 Mode: PAPER TRADING\n"
        f"💼 Starting Balance: $10,000\n"
        f"📊 Coins: BTC ETH SOL DOGE SHIB\n"
        f"⚡ Scans: Every 15 mins | 24/7\n"
        f"💰 Risk/trade: 10% | Min confidence: 60%\n"
        f"📰 News: CryptoPanic + CoinTelegraph + Google News\n"
        f"🌐 Dashboard: check your Railway URL\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"/status /pause /resume /balance /trades /forcesell"
    )

    while True:
        await scan_and_trade()
        now = datetime.utcnow()
        if now.date() > last_daily:
            await send_daily_report()
            last_daily = now.date()
        week = now.isocalendar()[1]
        if week != last_weekly and now.weekday() == 0:
            await send_weekly_report()
            last_weekly = week
        await asyncio.sleep(SCAN_INTERVAL)


def run_bot():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("pause",     cmd_pause))
    app.add_handler(CommandHandler("resume",    cmd_resume))
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("trades",    cmd_trades))
    app.add_handler(CommandHandler("forcesell", cmd_forcesell))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(scheduler(app))
    app.run_polling()


if __name__ == "__main__":
    # When running standalone (not via main.py)
    run_bot()
