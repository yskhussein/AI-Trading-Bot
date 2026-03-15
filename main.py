"""
main.py — AI Paper Trading Bot + Web Dashboard (single file, no import issues)
Deploy on Railway: just push this file + requirements.txt + railway.toml
"""
import os
import sys
import asyncio
import logging
import json
import requests
import feedparser
import threading
from datetime import datetime
from flask import Flask, jsonify, render_template_string, request
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
COINS          = ["bitcoin", "ethereum", "solana", "dogecoin", "shiba-inu"]
COIN_SYMBOLS   = {"bitcoin":"BTC","ethereum":"ETH","solana":"SOL","dogecoin":"DOGE","shiba-inu":"SHIB"}
RISK_PER_TRADE = 0.10
MIN_CONFIDENCE = 60
SCAN_INTERVAL  = 900
DAILY_LOSS_CAP = 0.20
PAPER_BALANCE  = 10000.0

# ── SHARED STATE ──────────────────────────────────────────────────────────────
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
    "portfolio_history": [],
    "ai_log": [],
    "news_feed": [],
    "prices": {},
    "risk_per_trade": RISK_PER_TRADE,
}

groq_client = None  # initialized lazily in get_groq()
telegram_bot = Bot(token=TELEGRAM_TOKEN)
flask_app    = Flask(__name__)

def get_groq():
    """Return Groq client, bypassing Railway proxy env vars."""
    import httpx
    return Groq(api_key=GROQ_API_KEY, http_client=httpx.Client())


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

def get_prices() -> dict:
    ids = ",".join(COINS)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency":"usd","ids":ids,"order":"market_cap_desc","price_change_percentage":"1h,24h,7d"},
            timeout=15,
        )
        result = {}
        data = r.json()
        if not isinstance(data, list):
            logger.error(f"CoinGecko unexpected response: {str(data)[:200]}")
            return state.get("prices", {})
        for coin in data:
            if not isinstance(coin, dict):
                continue
            result[coin["id"]] = {
                "price":      coin["current_price"],
                "change_1h":  coin.get("price_change_percentage_1h_in_currency", 0) or 0,
                "change_24h": coin.get("price_change_percentage_24h", 0) or 0,
                "change_7d":  coin.get("price_change_percentage_7d_in_currency", 0) or 0,
                "volume_24h": coin.get("total_volume", 0) or 0,
                "high_24h":   coin.get("high_24h", 0) or 0,
                "low_24h":    coin.get("low_24h", 0) or 0,
                "symbol":     coin["symbol"].upper(),
                "name":       coin["name"],
                "image":      coin.get("image",""),
            }
        state["prices"] = result
        return result
    except Exception as e:
        logger.error(f"CoinGecko error: {e}")
        return state.get("prices", {})


def get_portfolio_value(prices_data: dict) -> float:
    total = state["cash_balance"]
    for coin_id, trade in state["open_trades"].items():
        price = prices_data.get(coin_id, {}).get("price", trade["entry_price"])
        total += price * trade["qty"]
    return total


def calculate_momentum(coin_id: str, prices_data: dict) -> dict:
    pd = prices_data.get(coin_id, {})
    c1h  = pd.get("change_1h", 0)
    c24h = pd.get("change_24h", 0)
    if c1h > 1 and c24h > 2:
        trend = "bullish"
    elif c1h < -1 and c24h < -2:
        trend = "bearish"
    else:
        trend = "neutral"
    return {
        "trend":        trend,
        "momentum_pct": round(c1h, 3),
        "volume_spike": pd.get("volume_24h", 0) > 1_000_000_000,
        "change_1h":    c1h,
        "change_24h":   c24h,
    }


# ══════════════════════════════════════════════════════════════════════════════
# NEWS GATHERING
# ══════════════════════════════════════════════════════════════════════════════

def fetch_cryptopanic(symbol: str) -> list:
    if not CRYPTOPANIC_KEY:
        return []
    try:
        r = requests.get(
            "https://cryptopanic.com/api/v1/posts/",
            params={"auth_token":CRYPTOPANIC_KEY,"currencies":symbol,"public":"true"},
            timeout=10,
        )
        return [{"source":"CryptoPanic","title":i["title"],"url":i.get("url","#"),"time":i.get("published_at","")} for i in r.json().get("results",[])[:4]]
    except Exception as e:
        logger.warning(f"CryptoPanic: {e}")
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
                items.append({"source":source,"title":entry.title,"url":entry.get("link","#"),"time":entry.get("published","")})
        except Exception as e:
            logger.warning(f"RSS: {e}")
    return items


def gather_all_news(coin_id: str) -> str:
    symbol    = COIN_SYMBOLS.get(coin_id, coin_id.upper())
    coin_name = coin_id.replace("-"," ")
    items     = fetch_cryptopanic(symbol) + fetch_rss_news(coin_name, symbol)
    for item in items:
        item["coin"] = symbol
        if item not in state["news_feed"]:
            state["news_feed"].insert(0, item)
    state["news_feed"] = state["news_feed"][:30]
    return "\n".join([f"[{i['source']}] {i['title']}" for i in items]) or "No recent news found."


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
  "market_summary": "<1 sentence overview>"
}
Rules: BUY only if confidence>=60 and no open trade. SELL only if confidence>=60 and open trade exists. HOLD otherwise."""

    user_msg = f"""Analyze {symbol}:
PRICE: ${price_data.get('price','N/A')}
1h: {price_data.get('change_1h',0):.2f}% | 24h: {price_data.get('change_24h',0):.2f}% | 7d: {price_data.get('change_7d',0):.2f}%
Volume: ${price_data.get('volume_24h',0):,.0f}
High/Low 24h: ${price_data.get('high_24h',0)} / ${price_data.get('low_24h',0)}
Momentum: {momentum.get('trend')} | {momentum.get('momentum_pct')}% | Vol spike: {momentum.get('volume_spike')}
Open trade: {'YES - holding ' + symbol if has_open else 'NO position'}
NEWS:
{news}
JSON only:"""

    try:
        resp = get_groq().chat.completions.create(
            model="llama3-70b-8192",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_msg}],
            temperature=0.2, max_tokens=600,
        )
        raw      = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        decision = json.loads(raw)
        state["ai_log"].insert(0, {
            "time":           datetime.utcnow().isoformat(),
            "coin":           symbol,
            "action":         decision.get("action","HOLD"),
            "confidence":     decision.get("confidence",0),
            "sentiment":      decision.get("sentiment","neutral"),
            "reasoning":      decision.get("reasoning",""),
            "key_news":       decision.get("key_news",""),
            "risk_level":     decision.get("risk_level","medium"),
            "market_summary": decision.get("market_summary",""),
        })
        state["ai_log"] = state["ai_log"][:50]
        return decision
    except Exception as e:
        logger.error(f"AI error {coin_id}: {e}")
        fallback = {"action":"HOLD","confidence":0,"reasoning":str(e),"sentiment":"neutral",
                    "key_news":"","risk_level":"high","suggested_hold_duration":"N/A",
                    "price_target":None,"stop_suggestion":None,"market_summary":"Error"}
        state["ai_log"].insert(0, {**fallback,"time":datetime.utcnow().isoformat(),"coin":symbol})
        return fallback


# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADE EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def paper_buy(coin_id: str, price: float) -> dict:
    usdt_amount = state["cash_balance"] * state["risk_per_trade"]
    qty         = usdt_amount / price
    state["cash_balance"] -= usdt_amount
    trade = {"qty":qty,"entry_price":price,"entry_time":datetime.utcnow().isoformat(),
             "usdt_spent":usdt_amount,"coin":COIN_SYMBOLS.get(coin_id,coin_id.upper())}
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
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

async def notify(msg: str):
    try:
        await telegram_bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Telegram: {e}")


def format_trade_alert(coin_id, action, decision, price_data, trade=None, pnl=None):
    symbol  = COIN_SYMBOLS.get(coin_id, coin_id.upper())
    price   = price_data.get("price", 0)
    c1h     = price_data.get("change_1h", 0)
    c24h    = price_data.get("change_24h", 0)
    a_emoji = "🟢" if action=="BUY" else "🔴" if action=="SELL" else "⚪"
    s_emoji = "📈" if decision.get("sentiment")=="bullish" else "📉" if decision.get("sentiment")=="bearish" else "➡️"
    msg  = f"📝 <b>PAPER</b> | {a_emoji} <b>{action} {symbol}</b> {s_emoji}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"💰 <b>Price:</b> ${price:,.6f}\n"
    msg += f"⏱ <b>1h:</b> {c1h:+.2f}% | <b>24h:</b> {c24h:+.2f}%\n"
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
        held  = datetime.utcnow() - datetime.fromisoformat(trade["entry_time"])
        hours = held.total_seconds() / 3600
        p_emoji = "✅" if pnl >= 0 else "❌"
        msg += f"━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"{p_emoji} <b>PnL:</b> {'+' if pnl>=0 else ''}{pnl:.4f} USDT | Held {hours:.1f}h\n"
        msg += f"💵 <b>Cash:</b> ${state['cash_balance']:,.2f}\n"
    msg += f"\n🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC"
    return msg


# ══════════════════════════════════════════════════════════════════════════════
# MAIN SCAN LOOP
# ══════════════════════════════════════════════════════════════════════════════

async def scan_and_trade():
    if state["paused"]:
        logger.info("Bot paused.")
        return

    prices_data   = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    logger.info(f"Scan | Portfolio: ${portfolio_val:,.2f}")

    if state["daily_start_balance"] is None:
        state["daily_start_balance"] = portfolio_val

    if state["daily_start_balance"] and state["daily_start_balance"] > 0:
        daily_loss = (state["daily_start_balance"] - portfolio_val) / state["daily_start_balance"]
        if daily_loss >= DAILY_LOSS_CAP:
            state["paused"] = True
            await notify(f"🚨 <b>DAILY LOSS LIMIT HIT</b>\nLost {daily_loss*100:.1f}% — bot paused.\nPortfolio: ${portfolio_val:,.2f}\nSend /resume to restart.")
            return

    state["last_scan"] = datetime.utcnow().isoformat()
    state["portfolio_history"].append({"time":datetime.utcnow().strftime("%H:%M"),"value":round(portfolio_val,2)})
    state["portfolio_history"] = state["portfolio_history"][-96:]

    for coin_id in COINS:
        try:
            price_data = prices_data.get(coin_id, {})
            if not price_data:
                continue
            momentum   = calculate_momentum(coin_id, prices_data)
            news       = gather_all_news(coin_id)
            open_trade = state["open_trades"].get(coin_id)
            decision   = ai_analyze(coin_id, news, price_data, momentum, open_trade)
            action     = decision.get("action","HOLD")
            confidence = decision.get("confidence",0)
            price      = price_data["price"]
            logger.info(f"{coin_id}: {action} @ {confidence}%")

            if action == "BUY" and confidence >= MIN_CONFIDENCE and not open_trade:
                if state["cash_balance"] >= price * 0.01:
                    trade = paper_buy(coin_id, price)
                    state["trades_today"] += 1
                    await notify(format_trade_alert(coin_id,"BUY",decision,price_data,trade))

            elif action == "SELL" and confidence >= MIN_CONFIDENCE and open_trade:
                pnl = paper_sell(coin_id, price)
                state["daily_pnl"]  += pnl
                state["total_pnl"]  += pnl
                state["wins" if pnl >= 0 else "losses"] += 1
                await notify(format_trade_alert(coin_id,"SELL",decision,price_data,open_trade,pnl))

            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Error {coin_id}: {e}")


async def send_daily_report():
    prices_data   = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    total         = state["wins"] + state["losses"]
    win_rate      = (state["wins"] / total * 100) if total > 0 else 0
    await notify(
        f"📊 <b>DAILY REPORT</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Portfolio: ${portfolio_val:,.2f}\n💵 Cash: ${state['cash_balance']:,.2f}\n"
        f"{'📈' if state['daily_pnl']>=0 else '📉'} Today PnL: {'+' if state['daily_pnl']>=0 else ''}{state['daily_pnl']:.2f} USDT\n"
        f"📊 Total PnL: {'+' if state['total_pnl']>=0 else ''}{state['total_pnl']:.2f} USDT\n"
        f"🔄 Trades: {state['trades_today']} | ✅ {state['wins']}W / ❌ {state['losses']}L | 🎯 {win_rate:.1f}%\n"
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
        f"📅 <b>WEEKLY REPORT</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 Portfolio: ${portfolio_val:,.2f}\n"
        f"📊 Total PnL: {'+' if state['total_pnl']>=0 else ''}{state['total_pnl']:.2f}\n"
        f"📈 ROI: {roi:+.2f}% (started $10,000)\n"
        f"🔄 Total trades: {total} | ✅ {state['wins']}W / ❌ {state['losses']}L | 🎯 {win_rate:.1f}%\n"
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
        cur = prices_data.get(cid,{}).get("price", t["entry_price"])
        unr = (cur - t["entry_price"]) * t["qty"]
        open_lines += f"\n  • {t['coin']}: {'+' if unr>=0 else ''}{unr:.2f} USDT"
    await update.message.reply_text(
        f"🤖 <b>STATUS</b> (📝 PAPER)\n{'🟢 RUNNING' if not state['paused'] else '🔴 PAUSED'}\n"
        f"💼 Portfolio: ${portfolio_val:,.2f}\n💵 Cash: ${state['cash_balance']:,.2f}\n"
        f"📊 PnL: {'+' if state['total_pnl']>=0 else ''}{state['total_pnl']:.2f}\n"
        f"📂 Open: {len(state['open_trades'])}{open_lines or ' — None'}\n"
        f"🕐 Last scan: {(state['last_scan'] or 'Not yet')[:16]}",
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
        f"💼 Portfolio: ${portfolio_val:,.2f}\n💵 Cash: ${state['cash_balance']:,.2f}\n"
        f"📈 ROI: {roi:+.2f}%\n📊 PnL: {'+' if state['total_pnl']>=0 else ''}{state['total_pnl']:.2f}",
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
        price = prices_data.get(coin_id,{}).get("price", state["open_trades"][coin_id]["entry_price"])
        pnl   = paper_sell(coin_id, price)
        state["total_pnl"] += pnl
        await update.message.reply_text(f"✅ Closed {COIN_SYMBOLS.get(coin_id,coin_id)} | PnL: {'+' if pnl>=0 else ''}{pnl:.2f}")


# ══════════════════════════════════════════════════════════════════════════════
# BOT SCHEDULER
# ══════════════════════════════════════════════════════════════════════════════

async def scheduler(app_tg: Application):
    last_daily  = datetime.utcnow().date()
    last_weekly = datetime.utcnow().isocalendar()[1]
    await notify(
        f"🚀 <b>AI Paper Trading Bot Online!</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 Mode: PAPER TRADING\n💼 Balance: $10,000\n"
        f"📊 Coins: BTC ETH SOL DOGE SHIB\n⚡ Scans: Every 15 mins | 24/7\n"
        f"💰 Risk/trade: 10% | Min confidence: 60%\n"
        f"🌐 Dashboard running on your Railway URL\n━━━━━━━━━━━━━━━━━━━━\n"
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


def run_telegram_bot():
    """Run telegram bot + trading scheduler in its own thread."""
    import asyncio

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        app_tg = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .build()
        )
        app_tg.add_handler(CommandHandler("status",    cmd_status))
        app_tg.add_handler(CommandHandler("pause",     cmd_pause))
        app_tg.add_handler(CommandHandler("resume",    cmd_resume))
        app_tg.add_handler(CommandHandler("balance",   cmd_balance))
        app_tg.add_handler(CommandHandler("trades",    cmd_trades))
        app_tg.add_handler(CommandHandler("forcesell", cmd_forcesell))

        await app_tg.initialize()
        await app_tg.start()
        await app_tg.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram bot polling started")

        # Run the trading scheduler (runs forever)
        await scheduler(app_tg)

    loop.run_until_complete(run())


# ══════════════════════════════════════════════════════════════════════════════
# FLASK WEB DASHBOARD
# ══════════════════════════════════════════════════════════════════════════════

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>AI Trading Bot — Command Center</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
  :root {
    --bg0:#060d1a; --bg1:#0a1628; --bg2:#0f1f38; --bg3:#162844;
    --border:#1e3a5f; --accent:#00a8ff; --accent2:#0066cc;
    --green:#00e676; --red:#ff1744; --yellow:#ffd600;
    --text:#c8d8f0; --text-dim:#5a7a9a; --text-bright:#e8f4ff;
    --mono:'IBM Plex Mono',monospace; --sans:'IBM Plex Sans',sans-serif;
  }
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:var(--bg0);color:var(--text);font-family:var(--sans);min-height:100vh}
  body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:1000;
    background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.03) 2px,rgba(0,0,0,0.03) 4px)}
  .header{background:var(--bg1);border-bottom:1px solid var(--border);padding:0 24px;
    display:flex;align-items:center;justify-content:space-between;height:56px;position:sticky;top:0;z-index:100}
  .logo{font-family:var(--mono);font-size:1.1rem;font-weight:600;color:var(--accent);letter-spacing:2px}
  .logo span{color:var(--text-dim);font-weight:300}
  .mode-badge{background:rgba(0,168,255,0.1);border:1px solid var(--accent2);color:var(--accent);
    font-family:var(--mono);font-size:0.7rem;padding:3px 10px;border-radius:2px;letter-spacing:1px}
  .header-right{display:flex;align-items:center;gap:20px}
  .status-dot{width:8px;height:8px;border-radius:50%;background:var(--green);
    box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
  .status-dot.paused{background:var(--red);box-shadow:0 0 8px var(--red)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
  .last-update{font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)}
  .clock{font-family:var(--mono);font-size:0.85rem;color:var(--accent)}
  .nav{background:var(--bg1);border-bottom:1px solid var(--border);display:flex;padding:0 24px}
  .nav-tab{padding:12px 20px;font-size:0.8rem;font-weight:500;color:var(--text-dim);cursor:pointer;
    border-bottom:2px solid transparent;letter-spacing:0.5px;transition:all 0.2s;text-transform:uppercase}
  .nav-tab:hover{color:var(--text)}
  .nav-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
  .page{display:none;padding:20px 24px}
  .page.active{display:block}
  .grid{display:grid;gap:16px}
  .grid-4{grid-template-columns:repeat(4,1fr)}
  .grid-2{grid-template-columns:repeat(2,1fr)}
  .grid-2-1{grid-template-columns:2fr 1fr}
  @media(max-width:1100px){.grid-4{grid-template-columns:repeat(2,1fr)}}
  @media(max-width:700px){.grid-4,.grid-2,.grid-2-1{grid-template-columns:1fr}}
  .card{background:var(--bg1);border:1px solid var(--border);border-radius:4px;padding:16px;position:relative;overflow:hidden}
  .card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;
    background:linear-gradient(90deg,var(--accent2),var(--accent),transparent)}
  .card-title{font-family:var(--mono);font-size:0.68rem;font-weight:500;color:var(--text-dim);
    letter-spacing:1.5px;text-transform:uppercase;margin-bottom:10px;display:flex;align-items:center;gap:8px}
  .card-title .dot{width:6px;height:6px;border-radius:50%;background:var(--accent)}
  .kpi-value{font-family:var(--mono);font-size:1.8rem;font-weight:600;color:var(--text-bright);line-height:1}
  .kpi-sub{font-family:var(--mono);font-size:0.72rem;color:var(--text-dim);margin-top:6px}
  .kpi-change{font-family:var(--mono);font-size:0.85rem;margin-top:4px}
  .up{color:var(--green)} .down{color:var(--red)} .neutral{color:var(--text-dim)}
  .ticker-row{display:flex;align-items:center;justify-content:space-between;
    padding:9px 0;border-bottom:1px solid var(--border)}
  .ticker-row:last-child{border-bottom:none}
  .ticker-symbol{font-family:var(--mono);font-weight:600;font-size:0.9rem;color:var(--text-bright);width:56px}
  .ticker-name{color:var(--text-dim);font-size:0.78rem;flex:1}
  .ticker-price{font-family:var(--mono);font-size:0.88rem;color:var(--text-bright);min-width:90px;text-align:right}
  .ticker-changes{display:flex;gap:10px;min-width:140px;justify-content:flex-end}
  .ticker-change{font-family:var(--mono);font-size:0.73rem;min-width:58px;text-align:right}
  .chart-wrap{position:relative;height:220px}
  .data-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:0.78rem}
  .data-table th{text-align:left;padding:8px 12px;color:var(--text-dim);font-size:0.68rem;
    letter-spacing:1px;text-transform:uppercase;border-bottom:1px solid var(--border);background:var(--bg2)}
  .data-table td{padding:10px 12px;border-bottom:1px solid rgba(30,58,95,0.5);color:var(--text)}
  .data-table tr:hover td{background:rgba(0,168,255,0.04)}
  .data-table .empty{text-align:center;color:var(--text-dim);padding:30px;font-size:0.8rem}
  .ai-entry{padding:12px;border-left:3px solid var(--border);margin-bottom:10px;
    background:var(--bg2);border-radius:0 4px 4px 0}
  .ai-entry.buy{border-left-color:var(--green)}
  .ai-entry.sell{border-left-color:var(--red)}
  .ai-entry.hold{border-left-color:var(--text-dim)}
  .ai-entry-header{display:flex;align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap}
  .ai-action{font-weight:700;font-size:0.8rem;font-family:var(--mono);padding:2px 8px;border-radius:2px}
  .ai-action.buy{background:rgba(0,230,118,0.15);color:var(--green)}
  .ai-action.sell{background:rgba(255,23,68,0.15);color:var(--red)}
  .ai-action.hold{background:rgba(90,122,154,0.2);color:var(--text-dim)}
  .ai-coin{font-family:var(--mono);font-weight:600;font-size:0.85rem;color:var(--accent)}
  .ai-conf{font-family:var(--mono);font-size:0.75rem;color:var(--text-dim)}
  .ai-time{font-family:var(--mono);font-size:0.7rem;color:var(--text-dim);margin-left:auto}
  .ai-reasoning{font-size:0.8rem;color:var(--text);line-height:1.5;margin-bottom:4px}
  .ai-news{font-size:0.75rem;color:var(--text-dim);font-style:italic}
  .sentiment-badge{font-size:0.68rem;font-family:var(--mono);padding:2px 7px;border-radius:2px;letter-spacing:0.5px}
  .sentiment-badge.bullish{background:rgba(0,230,118,0.1);color:var(--green)}
  .sentiment-badge.bearish{background:rgba(255,23,68,0.1);color:var(--red)}
  .sentiment-badge.neutral{background:rgba(90,122,154,0.15);color:var(--text-dim)}
  .news-item{padding:12px 0;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:flex-start}
  .news-item:last-child{border-bottom:none}
  .news-coin-tag{font-family:var(--mono);font-size:0.68rem;font-weight:600;padding:2px 8px;border-radius:2px;
    background:rgba(0,168,255,0.1);color:var(--accent);min-width:44px;text-align:center;flex-shrink:0;margin-top:2px}
  .news-title{font-size:0.82rem;color:var(--text);line-height:1.4}
  .news-title a{color:var(--text);text-decoration:none}
  .news-title a:hover{color:var(--accent)}
  .news-meta{font-family:var(--mono);font-size:0.7rem;color:var(--text-dim);margin-top:4px}
  .btn{padding:12px 20px;border:1px solid var(--border);background:var(--bg2);color:var(--text);
    font-family:var(--mono);font-size:0.8rem;letter-spacing:1px;cursor:pointer;border-radius:3px;
    transition:all 0.2s;text-transform:uppercase;display:flex;align-items:center;justify-content:center;gap:8px;width:100%}
  .btn:hover{border-color:var(--accent);color:var(--accent);background:rgba(0,168,255,0.06)}
  .btn.danger:hover{border-color:var(--red);color:var(--red);background:rgba(255,23,68,0.06)}
  .btn.success:hover{border-color:var(--green);color:var(--green);background:rgba(0,230,118,0.06)}
  .risk-slider{width:100%;-webkit-appearance:none;height:4px;background:var(--border);border-radius:2px;outline:none;margin-top:12px}
  .risk-slider::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;
    background:var(--accent);cursor:pointer;box-shadow:0 0 6px var(--accent)}
  .risk-value{font-family:var(--mono);font-size:1.4rem;color:var(--accent);margin-top:8px}
  .conf-bar-wrap{display:flex;align-items:center;gap:8px}
  .conf-bar{flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
  .conf-bar-fill{height:100%;border-radius:2px;transition:width 0.5s}
  .conf-label{font-family:var(--mono);font-size:0.72rem;color:var(--text-dim);min-width:36px;text-align:right}
  .scroll-panel{max-height:420px;overflow-y:auto}
  .scroll-panel::-webkit-scrollbar{width:4px}
  .scroll-panel::-webkit-scrollbar-track{background:var(--bg0)}
  .scroll-panel::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
  .toast{position:fixed;bottom:24px;right:24px;background:var(--bg2);border:1px solid var(--accent);
    color:var(--accent);font-family:var(--mono);font-size:0.8rem;padding:12px 20px;border-radius:4px;
    z-index:9999;transform:translateY(100px);opacity:0;transition:all 0.3s}
  .toast.show{transform:translateY(0);opacity:1}
  .controls-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
  .pnl-pos{color:var(--green)} .pnl-neg{color:var(--red)}
</style>
</head>
<body>
<div class="header">
  <div style="display:flex;align-items:center;gap:16px">
    <div class="logo">AI<span>/</span>TRADE <span style="font-size:0.7rem;color:var(--text-dim)">v1.0</span></div>
    <div class="mode-badge">PAPER MODE</div>
  </div>
  <div class="header-right">
    <div class="last-update" id="lastUpdate">Loading...</div>
    <div id="statusDot" class="status-dot"></div>
    <div class="clock" id="clock">--:--:--</div>
  </div>
</div>
<div class="nav">
  <div class="nav-tab active" onclick="switchTab('dashboard',this)">Dashboard</div>
  <div class="nav-tab" onclick="switchTab('trades',this)">Trades</div>
  <div class="nav-tab" onclick="switchTab('ai-log',this)">AI Brain</div>
  <div class="nav-tab" onclick="switchTab('news',this)">News Feed</div>
  <div class="nav-tab" onclick="switchTab('settings',this)">Settings</div>
</div>

<!-- DASHBOARD -->
<div id="page-dashboard" class="page active">
  <div class="grid grid-4" style="margin-bottom:16px">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Portfolio Value</div>
      <div class="kpi-value" id="portfolioVal">$--</div>
      <div class="kpi-change" id="portfolioRoi">--</div>
      <div class="kpi-sub">Started at $10,000.00</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Total PnL</div>
      <div class="kpi-value" id="totalPnl">$--</div>
      <div class="kpi-change" id="dailyPnl">Today: --</div>
      <div class="kpi-sub">All realized trades</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Win Rate</div>
      <div class="kpi-value" id="winRate">--%</div>
      <div class="kpi-change" id="winsLosses">-- W / -- L</div>
      <div class="kpi-sub">Total: <span id="totalTrades">0</span> trades</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Open Positions</div>
      <div class="kpi-value" id="openCount">--</div>
      <div class="kpi-change" id="cashBalance">Cash: $--</div>
      <div class="kpi-sub">Last scan: <span id="lastScan">--</span></div>
    </div>
  </div>
  <div class="grid grid-2-1" style="margin-bottom:16px">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Portfolio Performance</div>
      <div class="chart-wrap"><canvas id="portfolioChart"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Live Prices</div>
      <div id="tickerList"></div>
    </div>
  </div>
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Open Trades</div>
      <div class="scroll-panel">
        <table class="data-table">
          <thead><tr><th>Coin</th><th>Entry</th><th>Current</th><th>Qty</th><th>Unrealised PnL</th><th>Since</th></tr></thead>
          <tbody id="openTradesTbody"><tr><td colspan="6" class="empty">No open trades</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Latest AI Decisions</div>
      <div class="scroll-panel" id="miniAiLog"></div>
    </div>
  </div>
</div>

<!-- TRADES -->
<div id="page-trades" class="page">
  <div class="card">
    <div class="card-title"><span class="dot"></span>Trade History</div>
    <div class="scroll-panel" style="max-height:600px">
      <table class="data-table">
        <thead><tr><th>Coin</th><th>Entry</th><th>Exit</th><th>Qty</th><th>Invested</th><th>PnL</th><th>PnL%</th><th>Opened</th><th>Closed</th></tr></thead>
        <tbody id="tradeHistoryTbody"><tr><td colspan="9" class="empty">No completed trades yet</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<!-- AI BRAIN -->
<div id="page-ai-log" class="page">
  <div class="grid grid-2-1">
    <div class="card">
      <div class="card-title"><span class="dot"></span>AI Decision Log</div>
      <div class="scroll-panel" id="fullAiLog" style="max-height:600px"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Signal Distribution</div>
      <div class="chart-wrap" style="height:200px"><canvas id="signalChart"></canvas></div>
      <div style="margin-top:16px">
        <div class="card-title"><span class="dot"></span>Confidence Stats</div>
        <div id="confStats" style="padding-top:8px"></div>
      </div>
    </div>
  </div>
</div>

<!-- NEWS -->
<div id="page-news" class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>News Feed</div>
      <div class="scroll-panel" id="newsPanel" style="max-height:600px"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>News by Coin</div>
      <div id="newsByCoins"></div>
    </div>
  </div>
</div>

<!-- SETTINGS -->
<div id="page-settings" class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bot Controls</div>
      <div class="controls-grid">
        <button class="btn success" onclick="botControl('resume')">▶ Resume</button>
        <button class="btn danger"  onclick="botControl('pause')">⏸ Pause</button>
        <button class="btn danger"  onclick="botControl('forcesell')">⚡ Force Sell All</button>
        <button class="btn"         onclick="fetchAll()">↻ Refresh</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Risk Per Trade</div>
      <div class="risk-value" id="riskDisplay">10%</div>
      <input type="range" class="risk-slider" id="riskSlider" min="1" max="25" value="10" oninput="document.getElementById('riskDisplay').textContent=this.value+'%'"/>
      <div class="kpi-sub" style="margin-top:8px">% of cash balance risked per trade</div>
      <button class="btn" style="margin-top:12px" onclick="saveRisk()">💾 Save Risk</button>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bot Stats</div>
      <div id="botStats" style="font-family:var(--mono);font-size:0.8rem;line-height:2.2"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Tracked Coins</div>
      <div style="display:flex;flex-wrap:wrap;gap:8px;padding-top:4px">
        <div style="font-family:var(--mono);font-size:0.75rem;padding:6px 14px;border:1px solid var(--border);border-radius:3px;color:var(--accent)">BTC</div>
        <div style="font-family:var(--mono);font-size:0.75rem;padding:6px 14px;border:1px solid var(--border);border-radius:3px;color:var(--accent)">ETH</div>
        <div style="font-family:var(--mono);font-size:0.75rem;padding:6px 14px;border:1px solid var(--border);border-radius:3px;color:var(--accent)">SOL</div>
        <div style="font-family:var(--mono);font-size:0.75rem;padding:6px 14px;border:1px solid var(--border);border-radius:3px;color:var(--accent)">DOGE</div>
        <div style="font-family:var(--mono);font-size:0.75rem;padding:6px 14px;border:1px solid var(--border);border-radius:3px;color:var(--accent)">SHIB</div>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let portfolioChartObj=null, signalChartObj=null;

function updateClock(){document.getElementById('clock').textContent=new Date().toUTCString().slice(17,25)+' UTC'}
setInterval(updateClock,1000); updateClock();

function switchTab(name,el){
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  document.getElementById('page-'+name).classList.add('active');
}

function showToast(msg){
  const t=document.getElementById('toast'); t.textContent=msg; t.classList.add('show');
  setTimeout(()=>t.classList.remove('show'),3000);
}

function fmt(n,d=2){return n==null?'--':Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d})}
function pnlClass(n){return n>0?'pnl-pos':n<0?'pnl-neg':'neutral'}
function sign(n){return n>0?'+':''}

async function fetchAll(){
  try{
    const r=await fetch('/api/state');
    const d=await r.json();
    renderAll(d);
    document.getElementById('lastUpdate').textContent='Updated '+new Date().toLocaleTimeString();
  }catch(e){document.getElementById('lastUpdate').textContent='Update failed'}
}

function renderAll(d){
  // KPIs
  const pv=d.portfolio_value||0, roi=((pv-10000)/10000)*100;
  document.getElementById('portfolioVal').textContent='$'+fmt(pv);
  const re=document.getElementById('portfolioRoi');
  re.textContent=(roi>=0?'+':'')+fmt(roi)+'% ROI'; re.className='kpi-change '+(roi>=0?'up':'down');
  const pnl=d.total_pnl||0;
  const pe=document.getElementById('totalPnl');
  pe.textContent=(pnl>=0?'+$':'-$')+fmt(Math.abs(pnl)); pe.className='kpi-value '+(pnl>=0?'up':'down');
  const dp=d.daily_pnl||0;
  const de=document.getElementById('dailyPnl');
  de.textContent='Today: '+(dp>=0?'+$':'-$')+fmt(Math.abs(dp)); de.className='kpi-change '+(dp>=0?'up':'down');
  const wins=d.wins||0,losses=d.losses||0,wr=(wins+losses>0?(wins/(wins+losses)*100):0);
  document.getElementById('winRate').textContent=fmt(wr,1)+'%';
  document.getElementById('winsLosses').textContent=wins+' W / '+losses+' L';
  document.getElementById('totalTrades').textContent=wins+losses;
  document.getElementById('openCount').textContent=Object.keys(d.open_trades||{}).length;
  document.getElementById('cashBalance').textContent='Cash: $'+fmt(d.cash_balance||0);
  document.getElementById('lastScan').textContent=d.last_scan?(d.last_scan.slice(11,16)+' UTC'):'Not yet';
  document.getElementById('statusDot').className='status-dot'+(d.paused?' paused':'');

  // Ticker
  const prices=d.prices||{};
  const thtml=Object.entries(prices).map(([id,p])=>{
    const c1=p.change_1h||0,c24=p.change_24h||0;
    const isOpen=d.open_trades&&d.open_trades[id];
    const dot=isOpen?'<span style="color:var(--green);font-size:0.6rem">● </span>':'';
    return `<div class="ticker-row">
      <div class="ticker-symbol">${dot}${p.symbol}</div>
      <div class="ticker-name">${p.name}</div>
      <div class="ticker-price">$${p.price>1?fmt(p.price):p.price.toFixed(8)}</div>
      <div class="ticker-changes">
        <span class="ticker-change ${c1>=0?'up':'down'}">${sign(c1)}${fmt(c1,2)}% <small style="color:var(--text-dim)">1H</small></span>
        <span class="ticker-change ${c24>=0?'up':'down'}">${sign(c24)}${fmt(c24,2)}% <small style="color:var(--text-dim)">24H</small></span>
      </div>
    </div>`;
  }).join('');
  document.getElementById('tickerList').innerHTML=thtml||'<div style="color:var(--text-dim);padding:20px;text-align:center">Loading prices...</div>';

  // Portfolio chart
  const hist=d.portfolio_history||[];
  if(!portfolioChartObj){
    const ctx=document.getElementById('portfolioChart').getContext('2d');
    portfolioChartObj=new Chart(ctx,{type:'line',data:{labels:hist.map(h=>h.time),datasets:[
      {label:'Portfolio',data:hist.map(h=>h.value),borderColor:'#00a8ff',backgroundColor:'rgba(0,168,255,0.08)',borderWidth:2,pointRadius:0,fill:true,tension:0.3},
      {label:'Start',data:hist.map(()=>10000),borderColor:'rgba(90,122,154,0.4)',borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false}
    ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',maxTicksLimit:8,font:{family:'IBM Plex Mono',size:10}}},
              y:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',font:{family:'IBM Plex Mono',size:10},callback:v=>'$'+v.toLocaleString()}}}}});
  }else{
    portfolioChartObj.data.labels=hist.map(h=>h.time);
    portfolioChartObj.data.datasets[0].data=hist.map(h=>h.value);
    portfolioChartObj.data.datasets[1].data=hist.map(()=>10000);
    portfolioChartObj.update('none');
  }

  // Open trades
  const ot=d.open_trades||{};
  const otEntries=Object.entries(ot);
  document.getElementById('openTradesTbody').innerHTML=otEntries.length?otEntries.map(([id,t])=>{
    const cur=prices[id]?prices[id].price:t.entry_price;
    const unr=(cur-t.entry_price)*t.qty;
    return `<tr><td><b style="color:var(--accent)">${t.coin}</b></td>
      <td>$${t.entry_price>1?fmt(t.entry_price,4):t.entry_price.toFixed(8)}</td>
      <td>$${cur>1?fmt(cur,4):cur.toFixed(8)}</td>
      <td>${t.qty.toFixed(6)}</td>
      <td class="${pnlClass(unr)}">${sign(unr)}$${fmt(Math.abs(unr),4)}</td>
      <td style="color:var(--text-dim)">${(t.entry_time||'').slice(11,16)} UTC</td></tr>`;
  }).join(''):'<tr><td colspan="6" class="empty">No open trades</td></tr>';

  // Mini AI log
  const log=(d.ai_log||[]).slice(0,8);
  document.getElementById('miniAiLog').innerHTML=log.map(e=>{
    const ac=e.action.toLowerCase(),sc=e.sentiment||'neutral';
    return `<div class="ai-entry ${ac}" style="margin-bottom:8px;padding:10px">
      <div class="ai-entry-header">
        <span class="ai-action ${ac}">${e.action}</span>
        <span class="ai-coin">${e.coin}</span>
        <span class="ai-conf">${e.confidence}%</span>
        <span class="sentiment-badge ${sc}">${sc}</span>
        <span class="ai-time">${(e.time||'').slice(11,16)}</span>
      </div>
      <div class="ai-news" style="font-size:0.72rem">${e.key_news||''}</div>
    </div>`;
  }).join('')||'<div style="color:var(--text-dim);padding:20px;text-align:center;font-size:0.8rem">Waiting for first scan...</div>';

  // Full AI log
  document.getElementById('fullAiLog').innerHTML=(d.ai_log||[]).map(e=>{
    const ac=e.action.toLowerCase(),sc=e.sentiment||'neutral',conf=e.confidence||0;
    const bc=conf>=80?'var(--green)':conf>=60?'var(--accent)':'var(--yellow)';
    return `<div class="ai-entry ${ac}">
      <div class="ai-entry-header">
        <span class="ai-action ${ac}">${e.action}</span>
        <span class="ai-coin">${e.coin}</span>
        <span class="sentiment-badge ${sc}">${sc}</span>
        <span style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)">risk: ${e.risk_level||'--'}</span>
        <span class="ai-time">${(e.time||'').slice(0,16).replace('T',' ')} UTC</span>
      </div>
      <div class="conf-bar-wrap" style="margin-bottom:8px">
        <div class="conf-bar"><div class="conf-bar-fill" style="width:${conf}%;background:${bc}"></div></div>
        <span class="conf-label">${conf}%</span>
      </div>
      ${e.market_summary?`<div style="font-size:0.75rem;color:var(--accent);margin-bottom:6px;font-style:italic">${e.market_summary}</div>`:''}
      <div class="ai-reasoning">${e.reasoning||''}</div>
      ${e.key_news?`<div class="ai-news">📰 ${e.key_news}</div>`:''}
    </div>`;
  }).join('')||'<div style="color:var(--text-dim);padding:40px;text-align:center">No AI decisions yet. Bot scans every 15 mins.</div>';

  // Trade history
  const th=d.trade_history||[];
  document.getElementById('tradeHistoryTbody').innerHTML=th.length?th.map(t=>`<tr>
    <td><b style="color:var(--accent)">${t.symbol}</b></td>
    <td>$${t.entry_price>1?fmt(t.entry_price,4):t.entry_price.toFixed(8)}</td>
    <td>$${t.exit_price>1?fmt(t.exit_price,4):t.exit_price.toFixed(8)}</td>
    <td>${Number(t.qty).toFixed(6)}</td>
    <td>$${fmt(t.usdt_spent,2)}</td>
    <td class="${pnlClass(t.pnl)}">${sign(t.pnl)}$${fmt(Math.abs(t.pnl),4)}</td>
    <td class="${pnlClass(t.pnl_pct)}">${sign(t.pnl_pct)}${fmt(t.pnl_pct,2)}%</td>
    <td style="color:var(--text-dim)">${(t.entry_time||'').slice(0,16).replace('T',' ')}</td>
    <td style="color:var(--text-dim)">${(t.exit_time||'').slice(0,16).replace('T',' ')}</td>
  </tr>`).join(''):'<tr><td colspan="9" class="empty">No completed trades yet</td></tr>';

  // News
  const news=d.news_feed||[];
  document.getElementById('newsPanel').innerHTML=news.map(n=>`
    <div class="news-item">
      <div class="news-coin-tag">${n.coin||'--'}</div>
      <div>
        <div class="news-title"><a href="${n.url||'#'}" target="_blank">${n.title}</a></div>
        <div class="news-meta">${n.source}${n.time?' · '+n.time.slice(0,10):''}</div>
      </div>
    </div>`).join('')||'<div style="color:var(--text-dim);padding:30px;text-align:center">News appears after first scan</div>';

  const coins=['BTC','ETH','SOL','DOGE','SHIB'];
  document.getElementById('newsByCoins').innerHTML=coins.map(c=>{
    const items=news.filter(n=>n.coin===c).slice(0,3);
    if(!items.length) return '';
    return `<div style="margin-bottom:16px">
      <div style="font-family:var(--mono);font-size:0.75rem;color:var(--accent);margin-bottom:8px;letter-spacing:1px">${c}</div>
      ${items.map(n=>`<div style="font-size:0.78rem;margin-bottom:6px;padding-left:8px;border-left:2px solid var(--border)">
        <a href="${n.url||'#'}" target="_blank" style="color:var(--text);text-decoration:none">${n.title}</a>
        <div style="font-size:0.68rem;color:var(--text-dim);margin-top:2px">${n.source}</div>
      </div>`).join('')}
    </div>`;
  }).join('')||'<div style="color:var(--text-dim);padding:30px;text-align:center">Loading...</div>';

  // Signal chart
  const alog=d.ai_log||[];
  const buys=alog.filter(e=>e.action==='BUY').length,sells=alog.filter(e=>e.action==='SELL').length,holds=alog.filter(e=>e.action==='HOLD').length;
  if(!signalChartObj){
    const ctx=document.getElementById('signalChart').getContext('2d');
    signalChartObj=new Chart(ctx,{type:'doughnut',data:{labels:['BUY','SELL','HOLD'],datasets:[{data:[buys,sells,holds],
      backgroundColor:['rgba(0,230,118,0.7)','rgba(255,23,68,0.7)','rgba(90,122,154,0.4)'],borderWidth:0}]},
      options:{responsive:true,maintainAspectRatio:false,cutout:'70%',plugins:{legend:{labels:{color:'#5a7a9a',font:{family:'IBM Plex Mono',size:11}}}}}});
  }else{signalChartObj.data.datasets[0].data=[buys,sells,holds];signalChartObj.update()}
  const avg=arr=>arr.length?(arr.reduce((a,b)=>a+b,0)/arr.length).toFixed(1):'--';
  document.getElementById('confStats').innerHTML=`<div style="font-family:var(--mono);font-size:0.8rem;line-height:2.2">
    <span style="color:var(--green)">BUY</span> avg confidence: <b style="color:var(--text-bright)">${avg(alog.filter(e=>e.action==='BUY').map(e=>e.confidence))}%</b><br>
    <span style="color:var(--red)">SELL</span> avg confidence: <b style="color:var(--text-bright)">${avg(alog.filter(e=>e.action==='SELL').map(e=>e.confidence))}%</b><br>
    Total decisions: <b style="color:var(--accent)">${alog.length}</b></div>`;

  // Settings
  document.getElementById('botStats').innerHTML=`
    Running since: <b style="color:var(--accent)">${(d.start_time||'').slice(0,10)}</b><br>
    Last scan: <b style="color:var(--accent)">${(d.last_scan||'Not yet').slice(0,16).replace('T',' ')}</b><br>
    Status: <b style="${d.paused?'color:var(--red)':'color:var(--green)'}">${d.paused?'PAUSED':'RUNNING'}</b><br>
    Open trades: <b style="color:var(--accent)">${Object.keys(d.open_trades||{}).length}</b><br>
    History size: <b style="color:var(--accent)">${(d.trade_history||[]).length} trades</b>`;
  const risk=Math.round((d.risk_per_trade||0.1)*100);
  document.getElementById('riskSlider').value=risk;
  document.getElementById('riskDisplay').textContent=risk+'%';
}

async function botControl(action){
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
    const d=await r.json();
    showToast(d.message||'Done');
    fetchAll();
  }catch(e){showToast('Error: '+e.message)}
}

async function saveRisk(){
  const v=document.getElementById('riskSlider').value;
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'set_risk',value:parseInt(v)})});
    const d=await r.json(); showToast(d.message||'Saved'); fetchAll();
  }catch(e){showToast('Error')}
}

fetchAll();
setInterval(fetchAll,30000);
</script>
</body>
</html>"""


@flask_app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@flask_app.route("/api/state")
def api_state():
    prices_data   = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    total         = state["wins"] + state["losses"]
    return jsonify({
        **state,
        "portfolio_value": round(portfolio_val, 2),
        "prices":          prices_data,
        "total_trades":    total,
        "win_rate":        round(state["wins"] / total * 100, 1) if total > 0 else 0,
        "roi_pct":         round(((portfolio_val - PAPER_BALANCE) / PAPER_BALANCE) * 100, 2),
    })


@flask_app.route("/api/control", methods=["POST"])
def api_control():
    data   = request.get_json()
    action = data.get("action")
    if action == "pause":
        state["paused"] = True
        return jsonify({"message": "Bot paused"})
    elif action == "resume":
        state["paused"] = False
        return jsonify({"message": "Bot resumed"})
    elif action == "forcesell":
        prices_data = get_prices()
        closed = 0
        for coin_id in list(state["open_trades"].keys()):
            price = prices_data.get(coin_id,{}).get("price", state["open_trades"][coin_id]["entry_price"])
            pnl   = paper_sell(coin_id, price)
            state["total_pnl"] += pnl
            closed += 1
        return jsonify({"message": f"Closed {closed} trade(s)"})
    elif action == "set_risk":
        val = max(1, min(25, data.get("value", 10)))
        state["risk_per_trade"] = val / 100
        return jsonify({"message": f"Risk set to {val}%"})
    return jsonify({"message": "Unknown action"}), 400


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Start Telegram bot in background thread
    bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()
    logger.info("Telegram bot thread started")

    # Start Flask web dashboard (main thread)
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Dashboard starting on port {port}")
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
