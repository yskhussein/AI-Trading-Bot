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
import time as _time
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string, request
from groq import Groq
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
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
COIN_SYMBOLS    = {"bitcoin":"BTC","ethereum":"ETH","solana":"SOL","dogecoin":"DOGE","shiba-inu":"SHIB"}
BINANCE_SYMBOLS = {"bitcoin":"BTCUSDT","ethereum":"ETHUSDT","solana":"SOLUSDT","dogecoin":"DOGEUSDT","shiba-inu":"SHIBUSDT"}
RISK_PER_TRADE  = 0.10
MIN_CONFIDENCE  = 0          # AI decides its own threshold per trade
SCAN_INTERVAL   = 60         # 60 seconds — real scalper pace
DAILY_LOSS_CAP  = 0.20
STOP_LOSS_PCT   = 0.008      # 0.8% default SL — AI can override per trade
TAKE_PROFIT_PCT = 0.015      # 1.5% default TP — AI can override per trade

PAPER_BALANCE   = 10000.0
STATE_FILE      = "/app/state.json"   # persistent storage
MAX_OPEN_TRADES = 5
MAX_TRADES_PER_COIN = 1
TRADE_TYPES     = ["scalp", "momentum", "news", "reversal"]
# Scalp range guidance for AI
SCALP_TP_MIN    = 0.01       # 1% min take profit
SCALP_TP_MAX    = 0.02       # 2% max take profit
SCALP_SL_MAX    = 0.008      # 0.8% max stop loss for scalps

# ── PRICE CACHE ───────────────────────────────────────────────────────────────
_price_cache      = {}
_price_cache_time = 0
PRICE_CACHE_TTL   = 600  # 10 min cache — CoinGecko is context only, Binance handles live prices

# ── BINANCE TICK CACHE (real-time prices for scalping) ─────────────────────────
_binance_cache      = {}
_binance_cache_time = 0
BINANCE_CACHE_TTL   = 8   # refresh every 8 seconds

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
    "pending_approvals": {},   # callback_id -> trade info waiting for approval
    "stop_losses_hit": 0,
    "take_profits_hit": 0,
    "groq_pause_until": 0,
}

groq_client = None  # initialized lazily in get_groq()
telegram_bot = Bot(token=TELEGRAM_TOKEN)
flask_app    = Flask(__name__)

def get_groq():
    """Return Groq client, bypassing Railway proxy env vars."""
    import httpx
    return Groq(api_key=GROQ_API_KEY, http_client=httpx.Client())


# ══════════════════════════════════════════════════════════════════════════════
# BINANCE REAL-TIME PRICES (fast, no key needed)
# ══════════════════════════════════════════════════════════════════════════════

def get_binance_prices() -> dict:
    """Fetch real-time prices from Binance public API — no key needed, very fast."""
    global _binance_cache, _binance_cache_time
    import time as _t
    if _binance_cache and (_t.time() - _binance_cache_time) < BINANCE_CACHE_TTL:
        return _binance_cache
    try:
        symbols = list(BINANCE_SYMBOLS.values())
        # Batch fetch all symbols in one call — Binance needs JSON array format
        import json as _json
        symbols_param = _json.dumps(symbols)
        r = requests.get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbols": symbols_param},
            timeout=8,
        )
        data = r.json()
        if not isinstance(data, list):
            return _binance_cache
        result = {}
        for ticker in data:
            # Map back from BTCUSDT -> bitcoin
            binance_sym = ticker.get("symbol","")
            coin_id = next((k for k,v in BINANCE_SYMBOLS.items() if v == binance_sym), None)
            if not coin_id:
                continue
            price      = float(ticker.get("lastPrice", 0))
            prev_close = float(ticker.get("prevClosePrice", price))
            change_24h = ((price - prev_close) / prev_close * 100) if prev_close else 0
            high_price = float(ticker.get("highPrice", price))
            low_price  = float(ticker.get("lowPrice", price))
            volume     = float(ticker.get("quoteVolume", 0))  # volume in USDT
            # Micro momentum: weighted average price vs last price
            wap        = float(ticker.get("weightedAvgPrice", price))
            micro_mom  = ((price - wap) / wap * 100) if wap else 0
            # Bid/ask spread (important for scalping)
            bid        = float(ticker.get("bidPrice", price))
            ask        = float(ticker.get("askPrice", price))
            spread_pct = ((ask - bid) / bid * 100) if bid else 0
            result[coin_id] = {
                "price":       price,
                "change_24h":  round(change_24h, 3),
                "high_24h":    high_price,
                "low_24h":     low_price,
                "volume_24h":  volume,
                "micro_mom":   round(micro_mom, 4),   # price vs weighted avg
                "bid":         bid,
                "ask":         ask,
                "spread_pct":  round(spread_pct, 4),  # tight spread = good for scalp
                "trades_count": int(ticker.get("count", 0)),  # trade activity
                "price_change_pct": round(float(ticker.get("priceChangePercent", 0)), 3),
            }
        if result:
            _binance_cache      = result
            _binance_cache_time = _t.time()
        return result
    except Exception as e:
        logger.warning(f"Binance price error: {e}")
        return _binance_cache


def get_scalp_signal(coin_id: str, binance_data: dict) -> dict:
    """
    Pure technical scalp signal from Binance tick data.
    No AI needed — fast pattern detection for entry/exit.
    """
    b = binance_data.get(coin_id, {})
    if not b:
        return {"scalp_ready": False, "direction": "neutral", "strength": 0}

    price      = b.get("price", 0)
    high       = b.get("high_24h", price)
    low        = b.get("low_24h", price)
    spread     = b.get("spread_pct", 1)
    micro_mom  = b.get("micro_mom", 0)
    vol        = b.get("volume_24h", 0)
    change_pct = b.get("price_change_pct", 0)

    # Range position (0=at low, 1=at high)
    range_size = high - low
    range_pos  = ((price - low) / range_size) if range_size > 0 else 0.5

    # Scalp conditions
    spread_ok    = spread < 0.05          # tight spread (<0.05%)
    vol_ok       = vol > 1_000_000        # decent volume
    momentum_up  = micro_mom > 0.01       # price above weighted avg
    momentum_dn  = micro_mom < -0.01
    near_low     = range_pos < 0.25       # near 24h low — potential bounce
    near_high    = range_pos > 0.75       # near 24h high — potential pullback

    strength = 0
    direction = "neutral"

    if spread_ok and vol_ok:
        if momentum_up and not near_high:
            strength  = min(int(abs(micro_mom) * 1000), 40) + (20 if near_low else 0)
            direction = "long"
        elif momentum_dn and not near_low:
            strength  = min(int(abs(micro_mom) * 1000), 40) + (20 if near_high else 0)
            direction = "short"

    return {
        "scalp_ready":  strength >= 25 and spread_ok and vol_ok,
        "direction":    direction,
        "strength":     strength,
        "spread_pct":   spread,
        "range_pos":    round(range_pos, 3),
        "micro_mom":    micro_mom,
        "vol_ok":       vol_ok,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PERSISTENT STORAGE
# ══════════════════════════════════════════════════════════════════════════════

SAVE_KEYS = ["open_trades","trade_history","daily_pnl","total_pnl","wins","losses",
             "trades_today","cash_balance","risk_per_trade","paused","start_time",
             "stop_losses_hit","take_profits_hit"]

def save_state():
    """Persist critical state to disk so restarts don't lose data."""
    try:
        data = {k: state[k] for k in SAVE_KEYS}
        with open(STATE_FILE, "w") as f:
            json.dump(data, f)
        logger.info("State saved to disk")
    except Exception as e:
        logger.warning(f"State save failed: {e}")

def load_state():
    """Load persisted state from disk on startup."""
    try:
        if not os.path.exists(STATE_FILE):
            logger.info("No saved state found — starting fresh")
            return
        with open(STATE_FILE) as f:
            data = json.load(f)
        for k, v in data.items():
            if k in state:
                state[k] = v
        logger.info(f"State loaded — portfolio cash: ${state['cash_balance']:,.2f}, "
                    f"open trades: {len(state['open_trades'])}, "
                    f"total PnL: {state['total_pnl']:.2f}")
    except Exception as e:
        logger.warning(f"State load failed: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# TECHNICAL INDICATORS (RSI + MACD + Bollinger Bands)
# ══════════════════════════════════════════════════════════════════════════════

# Cache for OHLCV data per coin — only refresh every 30 mins
_ohlcv_cache = {}
_ohlcv_cache_time = {}

def fetch_ohlcv(coin_id: str, days: int = 30) -> list:
    """Fetch daily OHLCV from CoinGecko — cached 30 mins to avoid rate limits."""
    import time as _t
    now = _t.time()
    if coin_id in _ohlcv_cache and (now - _ohlcv_cache_time.get(coin_id, 0)) < 1800:
        return _ohlcv_cache[coin_id]
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
            params={"vs_currency": "usd", "days": days, "interval": "daily"},
            timeout=15,
        )
        data = r.json()
        prices = [p[1] for p in data.get("prices", [])]
        if prices:
            _ohlcv_cache[coin_id] = prices
            _ohlcv_cache_time[coin_id] = _t.time()
        return prices
    except Exception as e:
        logger.warning(f"OHLCV fetch error {coin_id}: {e}")
        return _ohlcv_cache.get(coin_id, [])


def calc_rsi(prices: list, period: int = 14) -> float:
    """Calculate RSI. Returns 0-100, or 50 if not enough data."""
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    if not gains or not losses:
        return 50.0
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs  = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def calc_macd(prices: list) -> dict:
    """Calculate MACD line, signal, and histogram."""
    def ema(data, period):
        if len(data) < period:
            return data[-1] if data else 0
        k = 2 / (period + 1)
        ema_val = sum(data[:period]) / period
        for price in data[period:]:
            ema_val = price * k + ema_val * (1 - k)
        return ema_val

    if len(prices) < 26:
        return {"macd": 0, "signal": 0, "histogram": 0, "trend": "neutral"}

    ema12   = ema(prices, 12)
    ema26   = ema(prices, 26)
    macd    = ema12 - ema26
    # signal = 9-period EMA of MACD (approximate with last values)
    signal  = macd * 0.8  # simplified for single-value calculation
    hist    = macd - signal
    trend   = "bullish" if macd > signal else "bearish"
    return {"macd": round(macd, 4), "signal": round(signal, 4),
            "histogram": round(hist, 4), "trend": trend}


def calc_bollinger(prices: list, period: int = 20) -> dict:
    """Calculate Bollinger Bands position."""
    if len(prices) < period:
        return {"position": "middle", "pct_b": 0.5}
    recent = prices[-period:]
    mean   = sum(recent) / period
    std    = (sum((p - mean) ** 2 for p in recent) / period) ** 0.5
    upper  = mean + 2 * std
    lower  = mean - 2 * std
    curr   = prices[-1]
    pct_b  = (curr - lower) / (upper - lower) if upper != lower else 0.5
    if pct_b > 0.8:
        position = "overbought"
    elif pct_b < 0.2:
        position = "oversold"
    else:
        position = "middle"
    return {"position": position, "pct_b": round(pct_b, 3),
            "upper": round(upper, 4), "lower": round(lower, 4), "mean": round(mean, 4)}


def get_technical_indicators(coin_id: str) -> dict:
    """Get RSI, MACD, and Bollinger for a coin."""
    prices = fetch_ohlcv(coin_id, days=30)
    if not prices:
        return {"rsi": 50, "macd_trend": "neutral", "bb_position": "middle",
                "bb_pct_b": 0.5, "signal": "neutral"}

    rsi  = calc_rsi(prices)
    macd = calc_macd(prices)
    bb   = calc_bollinger(prices)

    # Combined signal
    signals = []
    if rsi < 30:
        signals.append("oversold_bullish")
    elif rsi > 70:
        signals.append("overbought_bearish")
    if macd["trend"] == "bullish":
        signals.append("macd_bullish")
    elif macd["trend"] == "bearish":
        signals.append("macd_bearish")
    if bb["position"] == "oversold":
        signals.append("bb_oversold_bullish")
    elif bb["position"] == "overbought":
        signals.append("bb_overbought_bearish")

    bullish = sum(1 for s in signals if "bullish" in s)
    bearish = sum(1 for s in signals if "bearish" in s)
    if bullish > bearish:
        combined = "bullish"
    elif bearish > bullish:
        combined = "bearish"
    else:
        combined = "neutral"

    return {
        "rsi":        rsi,
        "macd":       macd["macd"],
        "macd_trend": macd["trend"],
        "bb_position":bb["position"],
        "bb_pct_b":   bb["pct_b"],
        "signal":     combined,
    }


# ══════════════════════════════════════════════════════════════════════════════
# MARKET DATA
# ══════════════════════════════════════════════════════════════════════════════

def get_prices() -> dict:
    global _price_cache, _price_cache_time
    import time as _time

    # Return cached prices if fresh enough
    if _price_cache and (_time.time() - _price_cache_time) < PRICE_CACHE_TTL:
        return _price_cache

    ids = ",".join(COINS)
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency":"usd","ids":ids,"order":"market_cap_desc","price_change_percentage":"1h,24h,7d"},
            timeout=15,
            headers={"Accept": "application/json"},
        )
        data = r.json()
        if not isinstance(data, list):
            logger.error(f"CoinGecko unexpected response: {str(data)[:200]}")
            return _price_cache or state.get("prices", {})
        result = {}
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
        _price_cache = result
        _price_cache_time = _time.time()
        state["prices"] = result
        return result
    except Exception as e:
        logger.error(f"CoinGecko error: {e}")
        return _price_cache or state.get("prices", {})


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
        (f"https://news.google.com/rss/search?q={coin_name.replace(chr(32), "+")}+crypto&hl=en-US&gl=US&ceid=US:en", "Google News"),
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

    system_prompt = """You are an elite 24/7 crypto scalper and day trader AI. You manage a live paper trading portfolio and scan every 60 seconds. You think and act like a professional quant trader.

STRATEGIES (pick the best fit):
1. SCALP    — 1-2% target, 0.5-0.8% stop, 5-30 min hold. Requires: volume spike + tight spread + clear micro-momentum
2. MOMENTUM — 2-5% target, 1-1.5% stop, 1-4 hour hold. Requires: RSI 45-65 trending + MACD aligned
3. NEWS     — 1-4% target, 1% stop, 30-120 min hold. Requires: clear market-moving headline
4. REVERSAL — 2-4% target, 1-2% stop, 1-6 hour hold. Requires: RSI <30 (oversold) or RSI >70 (overbought)

YOU SET ALL PARAMETERS — the system uses exactly what you return:
- confidence_threshold: the minimum confidence YOU require for this specific setup (0-100)
- stop_loss_pct: your chosen stop loss as decimal (e.g. 0.008 = 0.8%)
- take_profit_pct: your chosen take profit as decimal (e.g. 0.015 = 1.5%)

Respond ONLY with valid JSON, no markdown, no fences:
{
  "action": "BUY" | "SELL" | "HOLD",
  "confidence": <integer 0-100>,
  "confidence_threshold": <integer 0-100, YOUR required minimum for this setup>,
  "strategy": "scalp" | "momentum" | "news" | "reversal",
  "stop_loss_pct": <float, e.g. 0.008>,
  "take_profit_pct": <float, e.g. 0.015>,
  "reasoning": "<2-3 sentences — exact trade thesis with specific price levels>",
  "sentiment": "bullish" | "bearish" | "neutral",
  "key_news": "<most impactful headline or 'No major news'>",
  "risk_level": "low" | "medium" | "high",
  "suggested_hold_duration": "<e.g. 10-20 mins, 1-2 hours>",
  "price_target": <float>,
  "stop_price": <float>,
  "market_summary": "<1 sentence>",
  "trade_urgency": "immediate" | "normal" | "patient",
  "entry_reason": "<specific technical or news trigger for entry>"
}

EXECUTION RULES:
- Execute BUY only if confidence >= confidence_threshold
- For SCALP: spread must be <0.05%, volume must be strong
- For REVERSAL: RSI signal must be clear (below 28 or above 72)
- For MOMENTUM: at least 2 indicators aligned (RSI + MACD + BB)
- SELL when: target hit OR news reverses OR technicals flip against position
- HOLD when: genuinely unclear, choppy market, or spread too wide
- Always set tight stops — protect capital first, profits second
- Be aggressive when setup is clear, patient when it is not
- Keep reasoning under 50 words to save tokens"""

    # Get technical indicators
    technicals = get_technical_indicators(coin_id)

    # Use already-fetched price data (merged Binance+CoinGecko from scan loop)
    # Also get fresh Binance scalp signal
    binance_data  = get_binance_prices()  # returns from cache (8s TTL)
    scalp_signal  = get_scalp_signal(coin_id, binance_data)
    b             = binance_data.get(coin_id, price_data)  # fallback to price_data

    # Build open trade context

    open_trade_context = "NO POSITION"
    if has_open:
        ot = open_trade
        current_price = price_data.get('price', ot['entry_price'])
        unrealised_pct = ((current_price - ot['entry_price']) / ot['entry_price']) * 100
        held_mins = (datetime.utcnow() - datetime.fromisoformat(ot['entry_time'])).total_seconds() / 60
        open_trade_context = (
            f"HOLDING {symbol} | Entry: ${ot['entry_price']:,.6f} | "
            f"Current: ${current_price:,.6f} | P&L: {unrealised_pct:+.2f}% | "
            f"Held: {held_mins:.0f} mins | "
            f"SL: ${ot.get('stop_loss',0):,.6f} | TP: ${ot.get('take_profit',0):,.6f}"
        )

    # Compress news to top 2 headlines only to save tokens
    news_lines = [l for l in news.split('\n') if l.strip()][:2]
    news_short = '\n'.join(news_lines) if news_lines else 'No news'

    user_msg = f"""{symbol} | ${b.get('price', price_data.get('price','N/A'))} | 24h:{price_data.get('change_24h',0):+.1f}% | Vol:${price_data.get('volume_24h',0)/1e6:.0f}M
Spread:{b.get('spread_pct',0):.3f}% MicroMom:{b.get('micro_mom',0):+.3f}% RangePos:{scalp_signal.get('range_pos',0.5):.2f}
ScalpSignal:{'READY-'+scalp_signal.get('direction','').upper() if scalp_signal.get('scalp_ready') else 'NOT_READY'} RSI:{technicals['rsi']} MACD:{technicals['macd_trend']} BB:{technicals['bb_position']} TA:{technicals['signal'].upper()}
Momentum:{momentum.get('trend','?')}({momentum.get('momentum_pct',0):+.2f}%)
Position:{open_trade_context}
Cash:${state['cash_balance']:,.0f}
News:{news_short}
JSON:"""

    # Skip if Groq is rate limited
    pause_until = state.get("groq_pause_until", 0)
    if pause_until and datetime.utcnow().timestamp() < pause_until:
        remaining = int((pause_until - datetime.utcnow().timestamp()) / 60)
        logger.info(f"Groq paused — {remaining}m remaining, skipping {symbol}")
        return {"action":"HOLD","confidence":0,"reasoning":"Groq rate limit pause","sentiment":"neutral",
                "key_news":"","risk_level":"high","suggested_hold_duration":"N/A",
                "price_target":None,"stop_suggestion":None,"market_summary":"Paused"}

    try:
        resp = get_groq().chat.completions.create(
            model="llama3-8b-8192",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":user_msg}],
            temperature=0.2, max_tokens=350,
        )
        raw      = resp.choices[0].message.content.strip().replace("```json","").replace("```","").strip()
        decision = json.loads(raw)
        state["ai_log"].insert(0, {
            "time":           datetime.utcnow().isoformat(),
            "coin":           symbol,
            "action":         decision.get("action","HOLD"),
            "confidence":     decision.get("confidence",0),
            "strategy":       decision.get("strategy",""),
            "sentiment":      decision.get("sentiment","neutral"),
            "reasoning":      decision.get("reasoning",""),
            "key_news":       decision.get("key_news",""),
            "risk_level":     decision.get("risk_level","medium"),
            "market_summary": decision.get("market_summary",""),
            "trade_urgency":  decision.get("trade_urgency","normal"),
            "suggested_hold": decision.get("suggested_hold_duration",""),
            "rsi":            technicals.get("rsi", 50),
            "macd_trend":     technicals.get("macd_trend","neutral"),
            "bb_position":    technicals.get("bb_position","middle"),
            "ta_signal":      technicals.get("signal","neutral"),
        })
        state["ai_log"] = state["ai_log"][:50]
        return decision
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "rate_limit" in err_str.lower():
            # Extract wait time from error if available
            import re as _re
            wait_match = _re.search(r'try again in (\d+)m', err_str)
            wait_mins = int(wait_match.group(1)) + 1 if wait_match else 6
            logger.warning(f"Groq rate limit hit — pausing AI for {wait_mins} mins")
            state["groq_pause_until"] = (datetime.utcnow().timestamp() + wait_mins * 60)
        else:
            logger.error(f"AI error {coin_id}: {e}")
        fallback = {"action":"HOLD","confidence":0,"reasoning":str(e)[:80],"sentiment":"neutral",
                    "key_news":"","risk_level":"high","suggested_hold_duration":"N/A",
                    "price_target":None,"stop_suggestion":None,"market_summary":"Rate limit"}
        state["ai_log"].insert(0, {**fallback,"time":datetime.utcnow().isoformat(),"coin":symbol})
        return fallback


# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADE EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def paper_buy(coin_id: str, price: float, decision: dict = None) -> dict:
    """Execute paper buy. Uses AI-provided SL/TP if available, falls back to defaults."""
    usdt_amount = state["cash_balance"] * state["risk_per_trade"]
    qty         = usdt_amount / price

    # Use AI-decided SL/TP if provided, otherwise use defaults
    if decision:
        sl_pct = decision.get("stop_loss_pct") or STOP_LOSS_PCT
        tp_pct = decision.get("take_profit_pct") or TAKE_PROFIT_PCT
        # Clamp to sane ranges — AI can't set crazy values
        sl_pct = max(0.002, min(sl_pct, 0.15))   # 0.2% to 15%
        tp_pct = max(0.005, min(tp_pct, 0.50))   # 0.5% to 50%
    else:
        sl_pct = STOP_LOSS_PCT
        tp_pct = TAKE_PROFIT_PCT

    stop_price   = round(price * (1 - sl_pct), 8)
    target_price = round(price * (1 + tp_pct), 8)

    state["cash_balance"] -= usdt_amount
    trade = {
        "qty":          qty,
        "entry_price":  price,
        "entry_time":   datetime.utcnow().isoformat(),
        "usdt_spent":   usdt_amount,
        "coin":         COIN_SYMBOLS.get(coin_id, coin_id.upper()),
        "stop_loss":    stop_price,
        "take_profit":  target_price,
        "sl_pct":       round(sl_pct * 100, 2),
        "tp_pct":       round(tp_pct * 100, 2),
        "strategy":     decision.get("strategy","") if decision else "",
        "trade_number": len(state["trade_history"]) + len(state["open_trades"]) + 1,
    }
    state["open_trades"][coin_id] = trade
    logger.info(f"PAPER BUY {coin_id}: qty={qty:.6f} @ ${price} | SL=-{sl_pct*100:.2f}% TP=+{tp_pct*100:.2f}%")
    save_state()
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
    save_state()
    return pnl


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

# ── Pending approvals store ───────────────────────────────────────────────────
_pending = {}   # callback_data -> {coin_id, decision, price_data}


async def send_trade_approval(coin_id: str, decision: dict, price_data: dict):
    """Send a Telegram message with YES/NO inline buttons for trade approval."""
    symbol     = COIN_SYMBOLS.get(coin_id, coin_id.upper())
    price      = price_data.get("price", 0)
    confidence = decision.get("confidence", 0)
    sentiment  = decision.get("sentiment", "neutral")
    reasoning  = decision.get("reasoning", "")
    ta_signal  = decision.get("ta_signal", "")
    rsi        = decision.get("rsi", 50)
    amount     = state["cash_balance"] * state["risk_per_trade"]
    sl_pct = decision.get("stop_loss_pct") or STOP_LOSS_PCT
    tp_pct = decision.get("take_profit_pct") or TAKE_PROFIT_PCT
    stop   = round(price * (1 - sl_pct), 8)
    target = round(price * (1 + tp_pct), 8)

    cb_yes = f"approve_{coin_id}"
    cb_no  = f"reject_{coin_id}"
    _pending[cb_yes] = {"coin_id": coin_id, "decision": decision, "price_data": price_data}
    _pending[cb_no]  = {"coin_id": coin_id}

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ BUY", callback_data=cb_yes),
        InlineKeyboardButton("❌ SKIP", callback_data=cb_no),
    ]])

    msg = (
        f"🔔 <b>TRADE SIGNAL — {symbol}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 <b>Price:</b> ${price:,.6f}\n"
        f"💵 <b>Amount:</b> ${amount:,.2f} USDT (10%)\n"
        f"🛑 <b>Stop Loss:</b> ${stop:,.6f} (-{STOP_LOSS_PCT*100:.0f}%)\n"
        f"🎯 <b>Take Profit:</b> ${target:,.6f} (+{TAKE_PROFIT_PCT*100:.0f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🤖 <b>AI Confidence:</b> {confidence}%\n"
        f"📊 <b>Sentiment:</b> {sentiment.capitalize()}\n"
        f"📈 <b>RSI:</b> {rsi} | <b>TA:</b> {ta_signal.upper() if ta_signal else 'N/A'}\n"
        f"🧠 {reasoning}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ Auto-skips in 10 minutes if no response"
    )
    try:
        await telegram_bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=msg,
            parse_mode=ParseMode.HTML, reply_markup=keyboard
        )
        logger.info(f"Approval request sent for {coin_id}")
        # Auto-approve after 10 mins if no response (keeps bot running autonomously)
        asyncio.get_event_loop().call_later(600, lambda: asyncio.ensure_future(auto_execute_buy(coin_id)))
    except Exception as e:
        logger.error(f"Approval send failed: {e}")
        # Fall back to auto-buy if Telegram fails
        await auto_execute_buy(coin_id)


async def auto_execute_buy(coin_id: str):
    """Execute buy automatically (called after timeout or approval)."""
    cb_yes = f"approve_{coin_id}"
    if cb_yes not in _pending:
        return  # Already handled
    pending = _pending.pop(cb_yes)
    _pending.pop(f"reject_{coin_id}", None)
    prices_data = get_prices()
    price       = prices_data.get(coin_id, {}).get("price", 0)
    if not price or coin_id in state["open_trades"]:
        return
    if state["cash_balance"] < price * 0.01:
        return
    trade = paper_buy(coin_id, price)
    state["trades_today"] += 1
    await notify(format_trade_alert(coin_id, "BUY", pending["decision"],
                                    pending["price_data"], trade))


async def handle_approval_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle YES/NO button presses from Telegram."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("approve_"):
        coin_id = data[len("approve_"):]
        cb_yes  = f"approve_{coin_id}"
        cb_no   = f"reject_{coin_id}"
        if cb_yes not in _pending:
            await query.edit_message_text("⚠️ This trade signal has already been handled.")
            return
        pending = _pending.pop(cb_yes)
        _pending.pop(cb_no, None)
        prices_data = get_prices()
        price = prices_data.get(coin_id, {}).get("price", 0)
        if not price or coin_id in state["open_trades"]:
            await query.edit_message_text("⚠️ Price unavailable or trade already open.")
            return
        trade = paper_buy(coin_id, price)
        state["trades_today"] += 1
        symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
        await query.edit_message_text(
            f"✅ <b>BUY {symbol} APPROVED!</b>\n"
            f"Entry: ${price:,.6f} | Qty: {trade['qty']:.6f}\n"
            f"🛑 SL: ${trade['stop_loss']:,.6f} | 🎯 TP: ${trade['take_profit']:,.6f}",
            parse_mode=ParseMode.HTML
        )
        await notify(format_trade_alert(coin_id, "BUY", pending["decision"],
                                        pending["price_data"], trade))

    elif data.startswith("reject_"):
        coin_id = data[len("reject_"):]
        _pending.pop(f"approve_{coin_id}", None)
        _pending.pop(f"reject_{coin_id}", None)
        symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
        await query.edit_message_text(f"❌ <b>{symbol} trade skipped.</b>", parse_mode=ParseMode.HTML)
        logger.info(f"Trade rejected by user: {coin_id}")


async def notify(msg: str):
    for attempt in range(1, 4):
        try:
            await telegram_bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode=ParseMode.HTML)
            return
        except Exception as e:
            logger.warning(f"Telegram notify attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                await asyncio.sleep(3)
    logger.error("Telegram notify failed after 3 attempts")


def format_trade_alert(coin_id, action, decision, price_data, trade=None, pnl=None):
    symbol  = COIN_SYMBOLS.get(coin_id, coin_id.upper())
    price   = price_data.get("price", 0)
    c1h     = price_data.get("change_1h", 0)
    c24h    = price_data.get("change_24h", 0)
    a_emoji = "🟢" if action=="BUY" else "🔴" if action=="SELL" else "⚪"
    s_emoji = "📈" if decision.get("sentiment")=="bullish" else "📉" if decision.get("sentiment")=="bearish" else "➡️"
    strategy    = decision.get("strategy","").upper()
    urgency     = decision.get("trade_urgency","normal")
    strat_emoji = {"SCALP":"⚡","MOMENTUM":"🚀","NEWS":"📰","REVERSAL":"🔄"}.get(strategy,"🤖")
    urg_emoji   = "🔥" if urgency=="immediate" else "⚡" if urgency=="normal" else "🎯"

    msg  = f"📝 <b>PAPER</b> | {a_emoji} <b>{action} {symbol}</b> {s_emoji}\n"
    if strategy:
        msg += f"{strat_emoji} <b>Strategy:</b> {strategy} | {urg_emoji} {urgency.upper()}\n"
    msg += f"━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"💰 <b>Price:</b> ${price:,.6f}\n"
    msg += f"⏱ <b>1h:</b> {c1h:+.2f}% | <b>24h:</b> {c24h:+.2f}%\n"
    msg += f"🤖 <b>Confidence:</b> {decision.get('confidence',0)}%\n"
    msg += f"🎯 <b>Sentiment:</b> {decision.get('sentiment','N/A').capitalize()}\n"
    msg += f"⚠️ <b>Risk:</b> {decision.get('risk_level','N/A').capitalize()}\n"
    msg += f"⏳ <b>Hold:</b> {decision.get('suggested_hold_duration','N/A')}\n"
    if decision.get("price_target"):
        msg += f"🎯 <b>Target:</b> ${decision['price_target']:,.4f}\n"
    if trade and action == "BUY":
        msg += f"🛑 <b>SL:</b> ${trade.get('stop_loss',0):,.6f} | 🎯 <b>TP:</b> ${trade.get('take_profit',0):,.6f}\n"
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

async def check_stop_loss_take_profit(prices_data: dict):
    """Check all open trades for SL/TP triggers."""
    for coin_id in list(state["open_trades"].keys()):
        trade = state["open_trades"].get(coin_id)
        if not trade:
            continue
        price = prices_data.get(coin_id, {}).get("price", 0)
        if not price:
            continue

        hit_sl = price <= trade.get("stop_loss", 0)
        hit_tp = price >= trade.get("take_profit", float("inf"))

        if hit_sl or hit_tp:
            reason = "🛑 STOP LOSS" if hit_sl else "🎯 TAKE PROFIT"
            pnl    = paper_sell(coin_id, price)
            state["daily_pnl"] += pnl
            state["total_pnl"] += pnl
            if pnl >= 0:
                state["wins"] += 1
                state["take_profits_hit"] += 1
            else:
                state["losses"] += 1
                state["stop_losses_hit"] += 1
            symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
            pct    = ((price - trade["entry_price"]) / trade["entry_price"]) * 100
            await notify(
                f"{reason} triggered!\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 <b>{symbol}</b> | Entry: ${trade['entry_price']:,.4f} → Exit: ${price:,.4f}\n"
                f"{'✅' if pnl>=0 else '❌'} <b>PnL:</b> {'+' if pnl>=0 else ''}{pnl:.4f} USDT ({pct:+.2f}%)\n"
                f"💵 Cash: ${state['cash_balance']:,.2f}"
            )
            logger.info(f"{reason} {coin_id}: pnl={pnl:.4f}")


async def scan_and_trade():
    if state["paused"]:
        logger.info("Bot paused.")
        return

    # Binance handles live prices — only call CoinGecko for context (cached 10 mins)
    prices_data   = get_prices()
    binance_live  = get_binance_prices()

    # Merge Binance live prices into prices_data immediately
    for cid, b in binance_live.items():
        if cid in prices_data:
            prices_data[cid]["price"]     = b["price"]
            prices_data[cid]["bid"]       = b.get("bid", 0)
            prices_data[cid]["ask"]       = b.get("ask", 0)
            prices_data[cid]["spread_pct"]= b.get("spread_pct", 0)
            prices_data[cid]["micro_mom"] = b.get("micro_mom", 0)
        else:
            prices_data[cid] = b

    portfolio_val = get_portfolio_value(prices_data)
    logger.info(f"Scan | Portfolio: ${portfolio_val:,.2f} | Binance: {'✅' if binance_live else '❌'} | CoinGecko: {'✅' if prices_data else '❌'}")

    # Check stop loss / take profit first
    await check_stop_loss_take_profit(prices_data)

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
            if not price_data or not price_data.get("price"):
                logger.warning(f"No price data for {coin_id}, skipping")
                continue
            momentum   = calculate_momentum(coin_id, prices_data)
            news       = gather_all_news(coin_id)
            open_trade = state["open_trades"].get(coin_id)
            decision   = ai_analyze(coin_id, news, price_data, momentum, open_trade)
            action     = decision.get("action","HOLD")
            confidence = decision.get("confidence",0)
            price      = price_data["price"]
            logger.info(f"{coin_id}: {action} @ {confidence}%")

            # AI sets its own confidence threshold per trade
            ai_threshold = decision.get("confidence_threshold", 50)
            if action == "BUY" and confidence >= ai_threshold and not open_trade:
                if state["cash_balance"] >= price * 0.01 and len(state["open_trades"]) < MAX_OPEN_TRADES:
                    trade = paper_buy(coin_id, price, decision)
                    state["open_trades"][coin_id]["strategy"] = decision.get("strategy", "")
                    state["trades_today"] += 1
                    await notify(format_trade_alert(coin_id, "BUY", decision, price_data, trade))

            elif action == "SELL" and confidence >= decision.get("confidence_threshold", 50) and open_trade:
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
    save_state()


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
    # Load persisted state first
    load_state()

    last_daily  = datetime.utcnow().date()
    last_weekly = datetime.utcnow().isocalendar()[1]
    await notify(
        f"🚀 <b>AI Paper Trading Bot Online!</b>\n━━━━━━━━━━━━━━━━━━━━\n"
        f"📝 Mode: PAPER TRADING\n"
        f"💼 Portfolio: ${get_portfolio_value(get_prices()):,.2f}\n"
        f"📊 Coins: BTC ETH SOL DOGE SHIB\n"
        f"⚡ Scans: Every 60 seconds | 24/7\n"
        f"💰 Risk/trade: 10% | AI sets own confidence\n"
        f"🛑 SL: AI-decided | 🎯 TP: AI-decided | Max positions: {MAX_OPEN_TRADES}\n"
        f"📈 RSI + MACD + Bollinger Bands active\n"
        f"🔔 Trade approvals via Telegram buttons\n"
        f"💾 State persisted across restarts\n"
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


def run_telegram_bot():
    """Run telegram bot + trading scheduler in its own thread."""
    import asyncio
    from telegram.request import HTTPXRequest

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        # Increase timeouts to handle Railway network latency
        request = HTTPXRequest(
            connect_timeout=30,
            read_timeout=30,
            write_timeout=30,
            pool_timeout=30,
        )
        app_tg = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .request(request)
            .build()
        )
        app_tg.add_handler(CommandHandler("status",    cmd_status))
        app_tg.add_handler(CommandHandler("pause",     cmd_pause))
        app_tg.add_handler(CommandHandler("resume",    cmd_resume))
        app_tg.add_handler(CommandHandler("balance",   cmd_balance))
        app_tg.add_handler(CommandHandler("trades",    cmd_trades))
        app_tg.add_handler(CommandHandler("forcesell", cmd_forcesell))
        app_tg.add_handler(CallbackQueryHandler(handle_approval_callback))

        # Retry initialize up to 5 times with backoff
        for attempt in range(1, 6):
            try:
                logger.info(f"Telegram init attempt {attempt}/5...")
                await app_tg.initialize()
                break
            except Exception as e:
                logger.warning(f"Telegram init failed ({e}), retrying in {attempt*5}s...")
                await asyncio.sleep(attempt * 5)
        else:
            logger.error("Telegram failed to initialize after 5 attempts. Bot running without Telegram.")
            # Keep scheduler running even without Telegram
            await scheduler(app_tg)
            return

        await app_tg.start()
        await app_tg.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message", "callback_query"],
        )
        logger.info("Telegram bot polling started ✅")

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
<title>AI/TRADE — Command Center</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=IBM+Plex+Sans:wght@300;400;500;600;700&display=swap" rel="stylesheet"/>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js"></script>
<style>
:root{--bg0:#060d1a;--bg1:#0a1628;--bg2:#0f1f38;--bg3:#162844;--border:#1e3a5f;
  --accent:#00a8ff;--accent2:#0066cc;--green:#00e676;--red:#ff1744;--yellow:#ffd600;
  --text:#c8d8f0;--text-dim:#5a7a9a;--text-bright:#e8f4ff;
  --mono:'IBM Plex Mono',monospace;--sans:'IBM Plex Sans',sans-serif;}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg0);color:var(--text);font-family:var(--sans);min-height:100vh}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:999;
  background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,0.03) 2px,rgba(0,0,0,0.03) 4px)}
.header{background:var(--bg1);border-bottom:1px solid var(--border);padding:0 24px;
  display:flex;align-items:center;justify-content:space-between;height:56px;position:sticky;top:0;z-index:100}
.logo{font-family:var(--mono);font-size:1.1rem;font-weight:600;color:var(--accent);letter-spacing:2px}
.mode-badge{background:rgba(0,168,255,0.1);border:1px solid var(--accent2);color:var(--accent);
  font-family:var(--mono);font-size:0.7rem;padding:3px 10px;border-radius:2px;letter-spacing:1px}
.header-right{display:flex;align-items:center;gap:20px}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);animation:pulse 2s infinite}
.status-dot.paused{background:var(--red);box-shadow:0 0 8px var(--red)}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}
.last-update{font-family:var(--mono);font-size:0.72rem;color:var(--text-dim)}
.clock{font-family:var(--mono);font-size:0.85rem;color:var(--accent)}
.nav{background:var(--bg1);border-bottom:1px solid var(--border);display:flex;padding:0 24px;overflow-x:auto}
.nav-tab{padding:12px 18px;font-size:0.78rem;font-weight:500;color:var(--text-dim);cursor:pointer;
  border-bottom:2px solid transparent;letter-spacing:0.5px;transition:all 0.2s;text-transform:uppercase;white-space:nowrap}
.nav-tab:hover{color:var(--text)} .nav-tab.active{color:var(--accent);border-bottom-color:var(--accent)}
.page{display:none;padding:20px 24px} .page.active{display:block}
.grid{display:grid;gap:16px}
.grid-4{grid-template-columns:repeat(4,1fr)} .grid-3{grid-template-columns:repeat(3,1fr)}
.grid-2{grid-template-columns:repeat(2,1fr)} .grid-2-1{grid-template-columns:2fr 1fr}
.grid-3-1{grid-template-columns:3fr 1fr}
@media(max-width:1100px){.grid-4{grid-template-columns:repeat(2,1fr)}}
@media(max-width:700px){.grid-4,.grid-3,.grid-2,.grid-2-1,.grid-3-1{grid-template-columns:1fr}}
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
.ticker-row{display:flex;align-items:center;justify-content:space-between;padding:9px 0;border-bottom:1px solid var(--border)}
.ticker-row:last-child{border-bottom:none}
.ticker-symbol{font-family:var(--mono);font-weight:600;font-size:0.9rem;color:var(--text-bright);width:54px}
.ticker-name{color:var(--text-dim);font-size:0.78rem;flex:1}
.ticker-price{font-family:var(--mono);font-size:0.88rem;color:var(--text-bright);min-width:90px;text-align:right}
.ticker-changes{display:flex;gap:8px;min-width:130px;justify-content:flex-end}
.ticker-change{font-family:var(--mono);font-size:0.72rem;min-width:55px;text-align:right}
.ticker-btn{font-family:var(--mono);font-size:0.65rem;padding:3px 8px;border-radius:2px;cursor:pointer;
  border:1px solid var(--border);background:transparent;color:var(--text-dim);transition:all 0.15s;margin-left:6px}
.ticker-btn:hover{border-color:var(--accent);color:var(--accent)}
.ticker-btn.buy-btn:hover{border-color:var(--green);color:var(--green)}
.chart-wrap{position:relative;height:220px}
.data-table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:0.78rem}
.data-table th{text-align:left;padding:8px 10px;color:var(--text-dim);font-size:0.65rem;
  letter-spacing:1px;text-transform:uppercase;border-bottom:1px solid var(--border);background:var(--bg2)}
.data-table td{padding:9px 10px;border-bottom:1px solid rgba(30,58,95,0.5);color:var(--text)}
.data-table tr:hover td{background:rgba(0,168,255,0.04)}
.data-table .empty{text-align:center;color:var(--text-dim);padding:30px;font-size:0.8rem}
.ai-entry{padding:12px;border-left:3px solid var(--border);margin-bottom:8px;background:var(--bg2);border-radius:0 4px 4px 0}
.ai-entry.buy{border-left-color:var(--green)} .ai-entry.sell{border-left-color:var(--red)} .ai-entry.hold{border-left-color:var(--text-dim)}
.ai-entry-header{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap}
.ai-action{font-weight:700;font-size:0.78rem;font-family:var(--mono);padding:2px 8px;border-radius:2px}
.ai-action.buy{background:rgba(0,230,118,0.15);color:var(--green)}
.ai-action.sell{background:rgba(255,23,68,0.15);color:var(--red)}
.ai-action.hold{background:rgba(90,122,154,0.2);color:var(--text-dim)}
.ai-coin{font-family:var(--mono);font-weight:600;font-size:0.85rem;color:var(--accent)}
.ai-time{font-family:var(--mono);font-size:0.7rem;color:var(--text-dim);margin-left:auto}
.ai-reasoning{font-size:0.78rem;color:var(--text);line-height:1.5;margin-bottom:4px}
.ai-news{font-size:0.72rem;color:var(--text-dim);font-style:italic}
.sentiment-badge{font-size:0.65rem;font-family:var(--mono);padding:2px 6px;border-radius:2px}
.sentiment-badge.bullish{background:rgba(0,230,118,0.1);color:var(--green)}
.sentiment-badge.bearish{background:rgba(255,23,68,0.1);color:var(--red)}
.sentiment-badge.neutral{background:rgba(90,122,154,0.15);color:var(--text-dim)}
.news-item{padding:10px 0;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:flex-start}
.news-item:last-child{border-bottom:none}
.news-coin-tag{font-family:var(--mono);font-size:0.65rem;font-weight:600;padding:2px 7px;border-radius:2px;
  background:rgba(0,168,255,0.1);color:var(--accent);min-width:40px;text-align:center;flex-shrink:0;margin-top:2px}
.news-title a{color:var(--text);text-decoration:none;font-size:0.8rem;line-height:1.4}
.news-title a:hover{color:var(--accent)}
.news-meta{font-family:var(--mono);font-size:0.68rem;color:var(--text-dim);margin-top:3px}
.btn{padding:10px 16px;border:1px solid var(--border);background:var(--bg2);color:var(--text);
  font-family:var(--mono);font-size:0.75rem;letter-spacing:1px;cursor:pointer;border-radius:3px;
  transition:all 0.2s;text-transform:uppercase;display:inline-flex;align-items:center;justify-content:center;gap:6px}
.btn:hover{border-color:var(--accent);color:var(--accent);background:rgba(0,168,255,0.06)}
.btn.danger:hover{border-color:var(--red);color:var(--red);background:rgba(255,23,68,0.06)}
.btn.success:hover{border-color:var(--green);color:var(--green);background:rgba(0,230,118,0.06)}
.btn.warn:hover{border-color:var(--yellow);color:var(--yellow);background:rgba(255,214,0,0.06)}
.btn-full{width:100%}
.mini-input{background:var(--bg0);border:1px solid var(--border);color:var(--text);
  font-family:var(--mono);font-size:0.78rem;padding:6px 10px;border-radius:3px;width:100%;margin-top:4px}
.mini-input:focus{outline:none;border-color:var(--accent)}
.risk-slider{width:100%;-webkit-appearance:none;height:4px;background:var(--border);border-radius:2px;outline:none;margin-top:12px}
.risk-slider::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;
  background:var(--accent);cursor:pointer;box-shadow:0 0 6px var(--accent)}
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
  z-index:9999;transform:translateY(100px);opacity:0;transition:all 0.3s;max-width:320px}
.toast.show{transform:translateY(0);opacity:1}
.pnl-pos{color:var(--green)} .pnl-neg{color:var(--red)}
.rsi-gauge{position:relative;height:80px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:4px}
.rsi-value{font-family:var(--mono);font-size:1.6rem;font-weight:600}
.rsi-label{font-family:var(--mono);font-size:0.65rem;color:var(--text-dim);letter-spacing:1px}
.rsi-bar{width:100%;height:8px;background:var(--border);border-radius:4px;overflow:hidden;margin-top:6px}
.rsi-bar-fill{height:100%;border-radius:4px;transition:width 0.6s}
.modal-overlay{position:fixed;inset:0;background:rgba(6,13,26,0.85);z-index:1000;display:flex;align-items:center;justify-content:center}
.modal-overlay.hidden{display:none}
.modal{background:var(--bg1);border:1px solid var(--border);border-radius:6px;padding:24px;min-width:340px;max-width:480px;width:90%}
.modal-title{font-family:var(--mono);font-size:0.9rem;font-weight:600;color:var(--accent);margin-bottom:16px;letter-spacing:1px}
.modal-row{display:flex;gap:10px;margin-top:10px}
.approval-card{background:var(--bg2);border:1px solid var(--border);border-radius:4px;padding:14px;margin-bottom:10px;border-left:3px solid var(--yellow)}
.approval-card .coin-name{font-family:var(--mono);font-weight:700;font-size:1rem;color:var(--yellow);margin-bottom:8px}
.approval-card .detail{font-family:var(--mono);font-size:0.75rem;color:var(--text-dim);margin-bottom:4px}
.approval-btns{display:flex;gap:8px;margin-top:12px}
.activity-line{font-family:var(--mono);font-size:0.75rem;padding:6px 0;border-bottom:1px solid rgba(30,58,95,0.4);
  display:flex;gap:10px;align-items:flex-start}
.activity-line:last-child{border-bottom:none}
.activity-time{color:var(--text-dim);min-width:42px;flex-shrink:0}
.activity-coin{min-width:36px;flex-shrink:0}
.activity-text{color:var(--text);line-height:1.4}
.perf-coin-row{display:flex;align-items:center;justify-content:space-between;padding:8px 0;border-bottom:1px solid var(--border)}
.perf-coin-row:last-child{border-bottom:none}
.perf-bar-wrap{flex:1;margin:0 12px;height:6px;background:var(--border);border-radius:3px;overflow:hidden}
.perf-bar-fill{height:100%;border-radius:3px}
.ta-row{display:flex;align-items:center;justify-content:space-between;padding:10px 0;border-bottom:1px solid var(--border)}
.ta-row:last-child{border-bottom:none}
.ta-label{font-family:var(--mono);font-size:0.75rem;color:var(--text-dim);min-width:80px}
.ta-value{font-family:var(--mono);font-size:0.9rem;font-weight:600}
.badge{font-family:var(--mono);font-size:0.65rem;padding:2px 8px;border-radius:2px;letter-spacing:0.5px}
.badge.bullish{background:rgba(0,230,118,0.15);color:var(--green)}
.badge.bearish{background:rgba(255,23,68,0.15);color:var(--red)}
.badge.neutral{background:rgba(90,122,154,0.2);color:var(--text-dim)}
.badge.oversold{background:rgba(0,230,118,0.15);color:var(--green)}
.badge.overbought{background:rgba(255,23,68,0.15);color:var(--red)}
.badge.middle{background:rgba(90,122,154,0.2);color:var(--text-dim)}
.coin-selector{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.coin-pill{font-family:var(--mono);font-size:0.72rem;padding:5px 14px;border:1px solid var(--border);
  border-radius:20px;cursor:pointer;color:var(--text-dim);transition:all 0.2s}
.coin-pill:hover,.coin-pill.active{border-color:var(--accent);color:var(--accent);background:rgba(0,168,255,0.08)}
</style>
</head>
<body>

<!-- HEADER -->
<div class="header">
  <div style="display:flex;align-items:center;gap:14px">
    <div class="logo">AI<span>/</span>TRADE <span style="font-size:0.7rem;color:var(--text-dim)">v2.0</span></div>
    <div class="mode-badge">PAPER MODE</div>
    <div id="approvalBadge" style="display:none;background:rgba(255,214,0,0.15);border:1px solid var(--yellow);
      color:var(--yellow);font-family:var(--mono);font-size:0.7rem;padding:3px 10px;border-radius:2px;cursor:pointer"
      onclick="switchTab('approvals',document.querySelectorAll('.nav-tab')[5])">
      ⏳ <span id="approvalCount">0</span> PENDING
    </div>
  </div>
  <div class="header-right">
    <div class="last-update" id="lastUpdate">Loading...</div>
    <div id="statusDot" class="status-dot"></div>
    <div class="clock" id="clock">--:--:--</div>
  </div>
</div>

<!-- NAV -->
<div class="nav">
  <div class="nav-tab active" onclick="switchTab('dashboard',this)">Dashboard</div>
  <div class="nav-tab" onclick="switchTab('trades',this)">Trades</div>
  <div class="nav-tab" onclick="switchTab('ai-log',this)">AI Brain</div>
  <div class="nav-tab" onclick="switchTab('news',this)">News Feed</div>
  <div class="nav-tab" onclick="switchTab('technicals',this)">Technicals</div>
  <div class="nav-tab" onclick="switchTab('approvals',this)">Approvals</div>
  <div class="nav-tab" onclick="switchTab('performance',this)">Performance</div>
  <div class="nav-tab" onclick="switchTab('settings',this)">Settings</div>
</div>

<!-- ═══ DASHBOARD ═══ -->
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
      <div class="kpi-sub">SL hits: <span id="slHits">0</span> | TP hits: <span id="tpHits">0</span></div>
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
      <div class="card-title"><span class="dot"></span>Live Prices
        <span style="margin-left:auto;font-size:0.65rem;color:var(--text-dim)">click coin for chart</span>
      </div>
      <div id="tickerList"></div>
    </div>
  </div>
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Open Trades</div>
      <div class="scroll-panel">
        <table class="data-table">
          <thead><tr><th>Coin</th><th>Entry</th><th>Current</th><th>PnL</th><th>SL</th><th>TP</th><th>Actions</th></tr></thead>
          <tbody id="openTradesTbody"><tr><td colspan="7" class="empty">No open trades</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Activity Log <span style="margin-left:auto;font-size:0.65rem;color:var(--green)">● LIVE</span></div>
      <div class="scroll-panel" id="activityLog" style="max-height:300px"></div>
    </div>
  </div>
</div>

<!-- ═══ TRADES ═══ -->
<div id="page-trades" class="page">
  <div class="card">
    <div class="card-title"><span class="dot"></span>Trade History</div>
    <div class="scroll-panel" style="max-height:600px">
      <table class="data-table">
        <thead><tr><th>Coin</th><th>Entry</th><th>Exit</th><th>Qty</th><th>Invested</th><th>PnL</th><th>PnL%</th><th>Exit Reason</th><th>Opened</th><th>Closed</th></tr></thead>
        <tbody id="tradeHistoryTbody"><tr><td colspan="10" class="empty">No completed trades yet</td></tr></tbody>
      </table>
    </div>
  </div>
</div>

<!-- ═══ AI BRAIN ═══ -->
<div id="page-ai-log" class="page">
  <div class="grid grid-2-1">
    <div class="card">
      <div class="card-title"><span class="dot"></span>AI Decision Log</div>
      <div class="scroll-panel" id="fullAiLog" style="max-height:600px"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Signal Distribution</div>
      <div class="chart-wrap" style="height:180px"><canvas id="signalChart"></canvas></div>
      <div style="margin-top:14px">
        <div class="card-title"><span class="dot"></span>Confidence Stats</div>
        <div id="confStats" style="padding-top:8px"></div>
      </div>
    </div>
  </div>
</div>

<!-- ═══ NEWS ═══ -->
<div id="page-news" class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Latest News Feed</div>
      <div class="scroll-panel" id="newsPanel" style="max-height:600px"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>News by Coin</div>
      <div id="newsByCoins"></div>
    </div>
  </div>
</div>

<!-- ═══ TECHNICALS ═══ -->
<div id="page-technicals" class="page">
  <div class="coin-selector" id="taCoinSelector"></div>
  <div class="grid grid-3" id="taCards">
    <div class="card">
      <div class="card-title"><span class="dot"></span>RSI (14)</div>
      <div class="rsi-gauge">
        <div class="rsi-value" id="taRsi" style="color:var(--accent)">--</div>
        <div class="rsi-label" id="taRsiLabel">LOADING</div>
      </div>
      <div class="rsi-bar"><div class="rsi-bar-fill" id="taRsiBar" style="width:50%;background:var(--accent)"></div></div>
      <div style="display:flex;justify-content:space-between;margin-top:4px;font-family:var(--mono);font-size:0.65rem;color:var(--text-dim)">
        <span>0 Oversold</span><span>30</span><span>50</span><span>70</span><span>100 Overbought</span>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>MACD</div>
      <div style="padding:8px 0">
        <div class="ta-row">
          <span class="ta-label">MACD Line</span>
          <span class="ta-value" id="taMacd">--</span>
        </div>
        <div class="ta-row">
          <span class="ta-label">Trend</span>
          <span id="taMacdTrend" class="badge neutral">--</span>
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bollinger Bands</div>
      <div style="padding:8px 0">
        <div class="ta-row">
          <span class="ta-label">Position</span>
          <span id="taBBPos" class="badge middle">--</span>
        </div>
        <div class="ta-row">
          <span class="ta-label">%B</span>
          <span class="ta-value" id="taBBPct">--</span>
        </div>
      </div>
    </div>
  </div>
  <div class="card" style="margin-top:16px">
    <div class="card-title"><span class="dot"></span>Combined TA Signal</div>
    <div id="taCombined" style="padding:16px;font-family:var(--mono);font-size:1rem;text-align:center;color:var(--accent)">
      Select a coin above to load indicators
    </div>
  </div>
</div>

<!-- ═══ APPROVALS ═══ -->
<div id="page-approvals" class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Pending Trade Approvals</div>
      <div id="approvalsList">
        <div style="color:var(--text-dim);padding:30px;text-align:center;font-family:var(--mono);font-size:0.8rem">
          No pending approvals — bot will show BUY signals here
        </div>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Manual Trade</div>
      <div style="margin-bottom:12px;font-family:var(--mono);font-size:0.75rem;color:var(--text-dim)">
        Force buy or sell any coin directly from here
      </div>
      <div id="manualTradeCoinBtns" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px"></div>
      <div style="display:flex;gap:10px">
        <button class="btn success btn-full" onclick="manualBuy()">🟢 Manual BUY</button>
        <button class="btn danger btn-full" onclick="manualSell()">🔴 Manual SELL</button>
      </div>
      <div id="manualResult" style="margin-top:10px;font-family:var(--mono);font-size:0.78rem;color:var(--text-dim)"></div>
    </div>
  </div>
</div>

<!-- ═══ PERFORMANCE ═══ -->
<div id="page-performance" class="page">
  <div class="grid grid-4" style="margin-bottom:16px">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Best Coin</div>
      <div class="kpi-value" id="perfBestCoin" style="font-size:1.4rem">--</div>
      <div class="kpi-sub">Highest total PnL</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Avg Hold Time</div>
      <div class="kpi-value" id="perfAvgHold" style="font-size:1.4rem">-- h</div>
      <div class="kpi-sub">Per closed trade</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Best Hour</div>
      <div class="kpi-value" id="perfBestHour" style="font-size:1.4rem">--</div>
      <div class="kpi-sub">Most profitable UTC hour</div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>SL / TP</div>
      <div class="kpi-value" style="font-size:1.4rem"><span id="perfSL" class="down">0</span> / <span id="perfTP" class="up">0</span></div>
      <div class="kpi-sub">Stop losses / Take profits hit</div>
    </div>
  </div>
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>PnL by Coin</div>
      <div class="chart-wrap"><canvas id="perfCoinChart"></canvas></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Trades by Hour (UTC)</div>
      <div class="chart-wrap"><canvas id="perfHourChart"></canvas></div>
    </div>
  </div>
  <div class="card" style="margin-top:16px">
    <div class="card-title"><span class="dot"></span>Per-Coin Breakdown</div>
    <div id="perfCoinBreakdown" style="padding-top:8px"></div>
  </div>
</div>

<!-- ═══ SETTINGS ═══ -->
<div id="page-settings" class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bot Controls</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <button class="btn success" onclick="botControl('resume')">▶ Resume</button>
        <button class="btn danger"  onclick="botControl('pause')">⏸ Pause</button>
        <button class="btn danger"  onclick="botControl('forcesell')" style="grid-column:span 2">⚡ Force Sell All</button>
        <button class="btn"         onclick="fetchAll()" style="grid-column:span 2">↻ Refresh Now</button>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Risk Per Trade</div>
      <div class="kpi-value" id="riskDisplay" style="font-size:1.4rem;color:var(--accent)">10%</div>
      <input type="range" class="risk-slider" id="riskSlider" min="1" max="25" value="10"
        oninput="document.getElementById('riskDisplay').textContent=this.value+'%'"/>
      <div class="kpi-sub" style="margin-top:8px">% of cash balance per trade</div>
      <button class="btn" style="margin-top:12px;width:100%" onclick="saveRisk()">💾 Save</button>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Stop Loss / Take Profit Defaults</div>
      <div style="font-family:var(--mono);font-size:0.8rem;line-height:2.5;color:var(--text)">
        🛑 Stop Loss: <b style="color:var(--red)">-5%</b> per trade<br>
        🎯 Take Profit: <b style="color:var(--green)">+15%</b> per trade<br>
        <span style="color:var(--text-dim);font-size:0.72rem">Applied automatically on every BUY</span>
      </div>
    </div>
    <div class="card">
      <div class="card-title"><span class="dot"></span>Bot Info</div>
      <div id="botStats" style="font-family:var(--mono);font-size:0.8rem;line-height:2.2"></div>
    </div>
  </div>
</div>

<!-- COIN CHART MODAL -->
<div class="modal-overlay hidden" id="chartModal">
  <div class="modal" style="min-width:500px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px">
      <div class="modal-title" id="chartModalTitle">COIN CHART</div>
      <button class="btn" onclick="closeChartModal()" style="padding:6px 12px">✕ Close</button>
    </div>
    <div style="height:260px"><canvas id="coinPriceChart"></canvas></div>
    <div id="coinChartStats" style="display:flex;gap:20px;margin-top:12px;font-family:var(--mono);font-size:0.75rem;color:var(--text-dim)"></div>
  </div>
</div>

<!-- EDIT TRADE MODAL -->
<div class="modal-overlay hidden" id="editTradeModal">
  <div class="modal">
    <div class="modal-title" id="editTradeTitle">EDIT TRADE</div>
    <input type="hidden" id="editCoinId"/>
    <div style="margin-top:12px">
      <div style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim);margin-bottom:4px">Stop Loss Price</div>
      <input type="number" class="mini-input" id="editSL" step="any" placeholder="e.g. 67000"/>
    </div>
    <div style="margin-top:12px">
      <div style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim);margin-bottom:4px">Take Profit Price</div>
      <input type="number" class="mini-input" id="editTP" step="any" placeholder="e.g. 82000"/>
    </div>
    <div class="modal-row">
      <button class="btn success" style="flex:1" onclick="saveTradeEdit()">💾 Save Changes</button>
      <button class="btn" style="flex:1" onclick="closeEditModal()">✕ Cancel</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
let portfolioChartObj=null, signalChartObj=null, coinChartObj=null, perfCoinChartObj=null, perfHourChartObj=null;
let selectedTACoin='bitcoin';
let selectedManualCoin='bitcoin';
const COINS=['bitcoin','ethereum','solana','dogecoin','shiba-inu'];
const SYMBOLS={bitcoin:'BTC',ethereum:'ETH',solana:'SOL',dogecoin:'DOGE','shiba-inu':'SHIB'};

// ── Clock ────────────────────────────────────────────────────────────────────
function updateClock(){document.getElementById('clock').textContent=new Date().toUTCString().slice(17,25)+' UTC'}
setInterval(updateClock,1000); updateClock();

// ── Tab switch ───────────────────────────────────────────────────────────────
function switchTab(name,el){
  document.querySelectorAll('.nav-tab').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  if(el) el.classList.add('active');
  document.getElementById('page-'+name).classList.add('active');
  if(name==='technicals') loadTA(selectedTACoin);
  if(name==='performance') loadPerformance();
}

// ── Toast ────────────────────────────────────────────────────────────────────
function showToast(msg,type='info'){
  const t=document.getElementById('toast');
  t.textContent=msg; t.classList.add('show');
  t.style.borderColor=type==='error'?'var(--red)':type==='success'?'var(--green)':'var(--accent)';
  t.style.color=type==='error'?'var(--red)':type==='success'?'var(--green)':'var(--accent)';
  setTimeout(()=>t.classList.remove('show'),3500);
}

// ── Helpers ──────────────────────────────────────────────────────────────────
function fmt(n,d=2){return n==null?'--':Number(n).toLocaleString('en-US',{minimumFractionDigits:d,maximumFractionDigits:d})}
function fmtP(p){return p>1?fmt(p,2):p>0.01?p.toFixed(6):p.toFixed(10)}
function pnlClass(n){return n>0?'pnl-pos':n<0?'pnl-neg':'neutral'}
function sign(n){return n>0?'+':''}

// ── Main fetch ───────────────────────────────────────────────────────────────
async function fetchAll(){
  try{
    const r=await fetch('/api/state');
    const d=await r.json();
    renderAll(d);
    document.getElementById('lastUpdate').textContent='Updated '+new Date().toLocaleTimeString();
    loadApprovals();
  }catch(e){document.getElementById('lastUpdate').textContent='Update failed'}
}

function renderAll(d){
  renderKPIs(d); renderTicker(d); renderPortfolioChart(d);
  renderOpenTrades(d); renderActivityLog(d); renderFullAiLog(d);
  renderTradeHistory(d); renderNews(d); renderSignalChart(d);
  renderSettings(d); renderManualCoinBtns();
  document.getElementById('statusDot').className='status-dot'+(d.paused?' paused':'');
}

// ── KPIs ─────────────────────────────────────────────────────────────────────
function renderKPIs(d){
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
  document.getElementById('openCount').textContent=Object.keys(d.open_trades||{}).length;
  document.getElementById('cashBalance').textContent='Cash: $'+fmt(d.cash_balance||0);
  document.getElementById('lastScan').textContent=d.last_scan?(d.last_scan.slice(11,16)+' UTC'):'Not yet';
  document.getElementById('slHits').textContent=d.stop_losses_hit||0;
  document.getElementById('tpHits').textContent=d.take_profits_hit||0;
}

// ── Ticker with chart button ──────────────────────────────────────────────────
function renderTicker(d){
  const prices=d.prices||{};
  document.getElementById('tickerList').innerHTML=Object.entries(prices).map(([id,p])=>{
    const c1=p.change_1h||0,c24=p.change_24h||0;
    const isOpen=d.open_trades&&d.open_trades[id];
    const dot=isOpen?'<span style="color:var(--green);font-size:0.55rem">● </span>':'';
    return `<div class="ticker-row">
      <div class="ticker-symbol">${dot}${p.symbol}</div>
      <div class="ticker-name" style="font-size:0.72rem">${p.name}</div>
      <div class="ticker-price">$${fmtP(p.price)}</div>
      <div class="ticker-changes">
        <span class="ticker-change ${c1>=0?'up':'down'}">${sign(c1)}${fmt(c1,2)}%</span>
        <span class="ticker-change ${c24>=0?'up':'down'}">${sign(c24)}${fmt(c24,2)}%</span>
      </div>
      <button class="ticker-btn" onclick="openCoinChart('${id}','${p.symbol}')">📈</button>
    </div>`;
  }).join('')||'<div style="color:var(--text-dim);padding:20px;text-align:center">Loading...</div>';
}

// ── Portfolio Chart ───────────────────────────────────────────────────────────
function renderPortfolioChart(d){
  const h=d.portfolio_history||[];
  if(!portfolioChartObj){
    const ctx=document.getElementById('portfolioChart').getContext('2d');
    portfolioChartObj=new Chart(ctx,{type:'line',data:{labels:h.map(x=>x.time),datasets:[
      {label:'Portfolio',data:h.map(x=>x.value),borderColor:'#00a8ff',backgroundColor:'rgba(0,168,255,0.08)',borderWidth:2,pointRadius:0,fill:true,tension:0.3},
      {label:'Start',data:h.map(()=>10000),borderColor:'rgba(90,122,154,0.4)',borderWidth:1,borderDash:[4,4],pointRadius:0,fill:false}
    ]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
      scales:{x:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',maxTicksLimit:8,font:{family:'IBM Plex Mono',size:10}}},
              y:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',font:{family:'IBM Plex Mono',size:10},callback:v=>'$'+v.toLocaleString()}}}}});
  }else{
    portfolioChartObj.data.labels=h.map(x=>x.time);
    portfolioChartObj.data.datasets[0].data=h.map(x=>x.value);
    portfolioChartObj.data.datasets[1].data=h.map(()=>10000);
    portfolioChartObj.update('none');
  }
}

// ── Open Trades with Edit SL/TP ───────────────────────────────────────────────
function renderOpenTrades(d){
  const trades=d.open_trades||{}, prices=d.prices||{};
  const entries=Object.entries(trades);
  document.getElementById('openTradesTbody').innerHTML=entries.length?entries.map(([id,t])=>{
    const cur=prices[id]?prices[id].price:t.entry_price;
    const unr=(cur-t.entry_price)*t.qty;
    const slPct=t.stop_loss?((t.stop_loss-t.entry_price)/t.entry_price*100):null;
    const tpPct=t.take_profit?((t.take_profit-t.entry_price)/t.entry_price*100):null;
    return `<tr>
      <td><b style="color:var(--accent)">${t.coin}</b></td>
      <td>$${fmtP(t.entry_price)}</td>
      <td>$${fmtP(cur)}</td>
      <td class="${pnlClass(unr)}">${sign(unr)}$${fmt(Math.abs(unr),4)}</td>
      <td class="down" style="font-size:0.72rem">${t.stop_loss?'$'+fmtP(t.stop_loss)+'<br><span style="color:var(--text-dim)">('+fmt(slPct,1)+'%)</span>':'--'}</td>
      <td class="up" style="font-size:0.72rem">${t.take_profit?'$'+fmtP(t.take_profit)+'<br><span style="color:var(--text-dim)">(+'+fmt(tpPct,1)+'%)</span>':'--'}</td>
      <td style="white-space:nowrap">
        <button class="ticker-btn" onclick="openEditTrade('${id}','${t.coin}',${t.stop_loss||0},${t.take_profit||0})">✏️ Edit</button>
        <button class="ticker-btn" onclick="quickSell('${id}')" style="color:var(--red);border-color:var(--red)">✕ Sell</button>
      </td>
    </tr>`;
  }).join(''):'<tr><td colspan="7" class="empty">No open trades</td></tr>';
}

// ── Activity Log ──────────────────────────────────────────────────────────────
function renderActivityLog(d){
  const log=(d.ai_log||[]).slice(0,20);
  document.getElementById('activityLog').innerHTML=log.map(e=>{
    const ac=e.action.toLowerCase();
    const color=ac==='buy'?'var(--green)':ac==='sell'?'var(--red)':'var(--text-dim)';
    const stratEmoji={scalp:'⚡',momentum:'🚀',news:'📰',reversal:'🔄'}[e.strategy]||'🤖';
    const strat=e.strategy?` ${stratEmoji}${e.strategy.toUpperCase()}`:'';
    const ta=e.ta_signal?` | TA:${e.ta_signal.toUpperCase()}`:'';
    const rsi=e.rsi?` RSI:${e.rsi}`:'';
    return `<div class="activity-line">
      <span class="activity-time">${(e.time||'').slice(11,16)}</span>
      <span class="activity-coin" style="color:${color};font-weight:700">${e.action}</span>
      <span style="color:var(--accent);min-width:36px">${e.coin}</span>
      <span style="color:var(--text-dim);font-size:0.68rem;min-width:90px">${strat}${rsi}${ta}</span>
      <span class="activity-text">${e.confidence}% — ${(e.key_news||'').slice(0,50)}${e.key_news&&e.key_news.length>50?'…':''}</span>
    </div>`;
  }).join('')||'<div style="color:var(--text-dim);padding:20px;text-align:center;font-size:0.8rem">Waiting for first scan...</div>';
}

// ── Full AI Log ───────────────────────────────────────────────────────────────
function renderFullAiLog(d){
  document.getElementById('fullAiLog').innerHTML=(d.ai_log||[]).map(e=>{
    const ac=e.action.toLowerCase(),sc=e.sentiment||'neutral',conf=e.confidence||0;
    const bc=conf>=80?'var(--green)':conf>=60?'var(--accent)':'var(--yellow)';
    const taInfo=(e.rsi||e.macd_trend||e.bb_position)?
      `<div style="font-family:var(--mono);font-size:0.7rem;color:var(--text-dim);margin-top:4px">
        ${e.rsi?'RSI: <b>'+e.rsi+'</b>  ':''} 
        ${e.macd_trend?'MACD: <b>'+e.macd_trend+'</b>  ':''}
        ${e.bb_position?'BB: <b>'+e.bb_position+'</b>':''}
      </div>`:'';
    return `<div class="ai-entry ${ac}">
      <div class="ai-entry-header">
        <span class="ai-action ${ac}">${e.action}</span>
        <span class="ai-coin">${e.coin}</span>
        ${e.strategy?`<span style="font-family:var(--mono);font-size:0.65rem;padding:2px 6px;border-radius:2px;background:rgba(0,168,255,0.1);color:var(--accent)">${{scalp:'⚡ SCALP',momentum:'🚀 MOMENTUM',news:'📰 NEWS',reversal:'🔄 REVERSAL'}[e.strategy]||e.strategy.toUpperCase()}</span>`:''}
        <span class="sentiment-badge ${sc}">${sc}</span>
        <span style="font-family:var(--mono);font-size:0.7rem;color:var(--text-dim)">risk:${e.risk_level||'--'}</span>
        ${e.suggested_hold?`<span style="font-family:var(--mono);font-size:0.68rem;color:var(--text-dim)">⏳${e.suggested_hold}</span>`:''}
        <span class="ai-time">${(e.time||'').slice(0,16).replace('T',' ')} UTC</span>
      </div>
      <div class="conf-bar-wrap" style="margin-bottom:6px">
        <div class="conf-bar"><div class="conf-bar-fill" style="width:${conf}%;background:${bc}"></div></div>
        <span class="conf-label">${conf}%</span>
      </div>
      ${e.market_summary?`<div style="font-size:0.72rem;color:var(--accent);margin-bottom:4px;font-style:italic">${e.market_summary}</div>`:''}
      <div class="ai-reasoning">${e.reasoning||''}</div>
      ${taInfo}
      ${e.key_news?`<div class="ai-news">📰 ${e.key_news}</div>`:''}
    </div>`;
  }).join('')||'<div style="color:var(--text-dim);padding:40px;text-align:center">No AI decisions yet</div>';
}

// ── Trade History ─────────────────────────────────────────────────────────────
function renderTradeHistory(d){
  const th=d.trade_history||[];
  document.getElementById('tradeHistoryTbody').innerHTML=th.length?th.map(t=>`<tr>
    <td><b style="color:var(--accent)">${t.symbol}</b></td>
    <td>$${fmtP(t.entry_price)}</td><td>$${fmtP(t.exit_price)}</td>
    <td>${Number(t.qty).toFixed(6)}</td><td>$${fmt(t.usdt_spent,2)}</td>
    <td class="${pnlClass(t.pnl)}">${sign(t.pnl)}$${fmt(Math.abs(t.pnl),4)}</td>
    <td class="${pnlClass(t.pnl_pct)}">${sign(t.pnl_pct)}${fmt(t.pnl_pct,2)}%</td>
    <td style="color:var(--text-dim);font-size:0.7rem">${t.exit_reason||'AI Signal'}</td>
    <td style="color:var(--text-dim);font-size:0.7rem">${(t.entry_time||'').slice(0,16).replace('T',' ')}</td>
    <td style="color:var(--text-dim);font-size:0.7rem">${(t.exit_time||'').slice(0,16).replace('T',' ')}</td>
  </tr>`).join(''):'<tr><td colspan="10" class="empty">No completed trades yet</td></tr>';
}

// ── News ──────────────────────────────────────────────────────────────────────
function renderNews(d){
  const news=d.news_feed||[];
  document.getElementById('newsPanel').innerHTML=news.map(n=>`
    <div class="news-item">
      <div class="news-coin-tag">${n.coin||'--'}</div>
      <div><div class="news-title"><a href="${n.url||'#'}" target="_blank">${n.title}</a></div>
      <div class="news-meta">${n.source}${n.time?' · '+n.time.slice(0,10):''}</div></div>
    </div>`).join('')||'<div style="color:var(--text-dim);padding:30px;text-align:center">News appears after first scan</div>';
  const coins=['BTC','ETH','SOL','DOGE','SHIB'];
  document.getElementById('newsByCoins').innerHTML=coins.map(c=>{
    const items=news.filter(n=>n.coin===c).slice(0,3);
    if(!items.length) return '';
    return `<div style="margin-bottom:14px"><div style="font-family:var(--mono);font-size:0.75rem;color:var(--accent);margin-bottom:6px">${c}</div>
    ${items.map(n=>`<div style="font-size:0.78rem;margin-bottom:5px;padding-left:8px;border-left:2px solid var(--border)">
      <a href="${n.url||'#'}" target="_blank" style="color:var(--text);text-decoration:none">${n.title}</a>
      <div style="font-size:0.65rem;color:var(--text-dim)">${n.source}</div></div>`).join('')}</div>`;
  }).join('');
}

// ── Signal Chart ──────────────────────────────────────────────────────────────
function renderSignalChart(d){
  const log=d.ai_log||[];
  const buys=log.filter(e=>e.action==='BUY').length,sells=log.filter(e=>e.action==='SELL').length,holds=log.filter(e=>e.action==='HOLD').length;
  if(!signalChartObj){
    const ctx=document.getElementById('signalChart').getContext('2d');
    signalChartObj=new Chart(ctx,{type:'doughnut',data:{labels:['BUY','SELL','HOLD'],datasets:[{
      data:[buys,sells,holds],backgroundColor:['rgba(0,230,118,0.7)','rgba(255,23,68,0.7)','rgba(90,122,154,0.4)'],borderWidth:0}]},
      options:{responsive:true,maintainAspectRatio:false,cutout:'70%',plugins:{legend:{labels:{color:'#5a7a9a',font:{family:'IBM Plex Mono',size:11}}}}}});
  }else{signalChartObj.data.datasets[0].data=[buys,sells,holds];signalChartObj.update()}
  const avg=arr=>arr.length?(arr.reduce((a,b)=>a+b,0)/arr.length).toFixed(1):'--';
  document.getElementById('confStats').innerHTML=`<div style="font-family:var(--mono);font-size:0.8rem;line-height:2.2">
    <span style="color:var(--green)">BUY</span> avg conf: <b style="color:var(--text-bright)">${avg(log.filter(e=>e.action==='BUY').map(e=>e.confidence))}%</b><br>
    <span style="color:var(--red)">SELL</span> avg conf: <b style="color:var(--text-bright)">${avg(log.filter(e=>e.action==='SELL').map(e=>e.confidence))}%</b><br>
    Total decisions: <b style="color:var(--accent)">${log.length}</b></div>`;
}

// ── Settings ──────────────────────────────────────────────────────────────────
function renderSettings(d){
  document.getElementById('botStats').innerHTML=`
    Running since: <b style="color:var(--accent)">${(d.start_time||'').slice(0,10)}</b><br>
    Last scan: <b style="color:var(--accent)">${(d.last_scan||'Not yet').slice(0,16).replace('T',' ')}</b><br>
    Status: <b style="${d.paused?'color:var(--red)':'color:var(--green)'}">${d.paused?'PAUSED':'RUNNING'}</b><br>
    Open trades: <b style="color:var(--accent)">${Object.keys(d.open_trades||{}).length}</b><br>
    History: <b style="color:var(--accent)">${(d.trade_history||[]).length} trades</b>`;
  const risk=Math.round((d.risk_per_trade||0.1)*100);
  document.getElementById('riskSlider').value=risk;
  document.getElementById('riskDisplay').textContent=risk+'%';
}

// ── Technicals ────────────────────────────────────────────────────────────────
function initTACoinSelector(){
  const el=document.getElementById('taCoinSelector');
  el.innerHTML=COINS.map(c=>`<div class="coin-pill${c===selectedTACoin?' active':''}" onclick="selectTACoin('${c}',this)">${SYMBOLS[c]}</div>`).join('');
}
function selectTACoin(coin,el){
  selectedTACoin=coin;
  document.querySelectorAll('#taCoinSelector .coin-pill').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
  loadTA(coin);
}
async function loadTA(coin){
  document.getElementById('taCombined').textContent='Loading...';
  try{
    const r=await fetch('/api/technicals/'+coin);
    const ta=await r.json();
    const rsi=ta.rsi||50;
    const rsiColor=rsi<30?'var(--green)':rsi>70?'var(--red)':'var(--accent)';
    const rsiLabel=rsi<30?'OVERSOLD':rsi>70?'OVERBOUGHT':'NEUTRAL';
    document.getElementById('taRsi').textContent=rsi;
    document.getElementById('taRsi').style.color=rsiColor;
    document.getElementById('taRsiLabel').textContent=rsiLabel;
    document.getElementById('taRsiBar').style.width=rsi+'%';
    document.getElementById('taRsiBar').style.background=rsiColor;
    document.getElementById('taMacd').textContent=ta.macd||'--';
    document.getElementById('taMacdTrend').textContent=(ta.macd_trend||'neutral').toUpperCase();
    document.getElementById('taMacdTrend').className='badge '+(ta.macd_trend||'neutral');
    document.getElementById('taBBPos').textContent=(ta.bb_position||'middle').toUpperCase();
    document.getElementById('taBBPos').className='badge '+(ta.bb_position||'middle');
    document.getElementById('taBBPct').textContent=ta.bb_pct_b||'--';
    const sig=ta.signal||'neutral';
    const sigColor=sig==='bullish'?'var(--green)':sig==='bearish'?'var(--red)':'var(--text-dim)';
    document.getElementById('taCombined').innerHTML=
      `<div style="font-size:1.4rem;font-weight:700;color:${sigColor}">${sig.toUpperCase()}</div>
       <div style="font-size:0.75rem;color:var(--text-dim);margin-top:6px">RSI ${rsi} | MACD ${ta.macd_trend||'N/A'} | BB ${ta.bb_position||'N/A'}</div>`;
  }catch(e){document.getElementById('taCombined').textContent='Error loading indicators'}
}

// ── Approvals ─────────────────────────────────────────────────────────────────
async function loadApprovals(){
  try{
    const r=await fetch('/api/pending_approvals');
    const list=await r.json();
    const badge=document.getElementById('approvalBadge');
    document.getElementById('approvalCount').textContent=list.length;
    badge.style.display=list.length>0?'block':'none';
    const el=document.getElementById('approvalsList');
    if(!list.length){
      el.innerHTML='<div style="color:var(--text-dim);padding:30px;text-align:center;font-family:var(--mono);font-size:0.8rem">No pending approvals</div>';
      return;
    }
    el.innerHTML=list.map(a=>`
      <div class="approval-card">
        <div class="coin-name">⚡ BUY ${a.symbol}</div>
        <div class="detail">💰 Price: $${fmtP(a.price)}</div>
        <div class="detail">💵 Amount: $${fmt(a.amount,2)} USDT</div>
        <div class="detail">🤖 Confidence: ${a.confidence}% | ${a.sentiment}</div>
        <div class="detail" style="margin-top:6px;color:var(--text);font-size:0.72rem">${a.reasoning}</div>
        <div class="approval-btns">
          <button class="btn success" style="flex:1" onclick="handleApproval('${a.coin_id}','approve')">✅ Approve</button>
          <button class="btn danger" style="flex:1" onclick="handleApproval('${a.coin_id}','reject')">❌ Reject</button>
        </div>
      </div>`).join('');
  }catch(e){}
}
async function handleApproval(coinId,action){
  try{
    const r=await fetch('/api/approve_trade',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coin_id:coinId,action})});
    const d=await r.json();
    showToast(d.message,action==='approve'?'success':'info');
    loadApprovals(); fetchAll();
  }catch(e){showToast('Error','error')}
}

// ── Manual trade ──────────────────────────────────────────────────────────────
function renderManualCoinBtns(){
  document.getElementById('manualTradeCoinBtns').innerHTML=COINS.map(c=>`
    <div class="coin-pill${c===selectedManualCoin?' active':''}" onclick="selectManualCoin('${c}',this)">${SYMBOLS[c]}</div>`).join('');
}
function selectManualCoin(coin,el){
  selectedManualCoin=coin;
  document.querySelectorAll('#manualTradeCoinBtns .coin-pill').forEach(p=>p.classList.remove('active'));
  el.classList.add('active');
}
async function manualBuy(){
  try{
    const r=await fetch('/api/manual_buy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coin_id:selectedManualCoin})});
    const d=await r.json();
    document.getElementById('manualResult').textContent=d.message;
    showToast(d.message,'success'); fetchAll();
  }catch(e){showToast('Error','error')}
}
async function manualSell(){
  try{
    const r=await fetch('/api/manual_sell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coin_id:selectedManualCoin})});
    const d=await r.json();
    document.getElementById('manualResult').textContent=d.message;
    showToast(d.message,d.pnl>=0?'success':'info'); fetchAll();
  }catch(e){showToast('Error','error')}
}
async function quickSell(coinId){
  try{
    const r=await fetch('/api/manual_sell',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({coin_id:coinId})});
    const d=await r.json();
    showToast(d.message,d.pnl>=0?'success':'info'); fetchAll();
  }catch(e){showToast('Error','error')}
}

// ── Edit SL/TP modal ──────────────────────────────────────────────────────────
function openEditTrade(coinId,coin,sl,tp){
  document.getElementById('editCoinId').value=coinId;
  document.getElementById('editTradeTitle').textContent='EDIT '+coin+' TRADE';
  document.getElementById('editSL').value=sl||'';
  document.getElementById('editTP').value=tp||'';
  document.getElementById('editTradeModal').classList.remove('hidden');
}
function closeEditModal(){document.getElementById('editTradeModal').classList.add('hidden')}
async function saveTradeEdit(){
  const coinId=document.getElementById('editCoinId').value;
  const sl=document.getElementById('editSL').value;
  const tp=document.getElementById('editTP').value;
  try{
    const r=await fetch('/api/update_trade',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({coin_id:coinId,stop_loss:sl?parseFloat(sl):null,take_profit:tp?parseFloat(tp):null})});
    const d=await r.json();
    showToast(d.message,'success'); closeEditModal(); fetchAll();
  }catch(e){showToast('Error','error')}
}

// ── Coin chart modal ──────────────────────────────────────────────────────────
async function openCoinChart(coinId,symbol){
  document.getElementById('chartModal').classList.remove('hidden');
  document.getElementById('chartModalTitle').textContent=symbol+' — 24H PRICE CHART';
  document.getElementById('coinChartStats').innerHTML='Loading...';
  try{
    const r=await fetch(`https://api.coingecko.com/api/v3/coins/${coinId}/market_chart?vs_currency=usd&days=1&interval=hourly`);
    const data=await r.json();
    const prices=data.prices||[];
    const labels=prices.map(p=>new Date(p[0]).toUTCString().slice(17,22));
    const values=prices.map(p=>p[1]);
    const minP=Math.min(...values),maxP=Math.max(...values);
    const chg=values.length>1?((values[values.length-1]-values[0])/values[0]*100):0;
    const color=chg>=0?'#00e676':'#ff1744';
    if(coinChartObj) coinChartObj.destroy();
    const ctx=document.getElementById('coinPriceChart').getContext('2d');
    coinChartObj=new Chart(ctx,{type:'line',data:{labels,datasets:[{
      data:values,borderColor:color,backgroundColor:color+'22',borderWidth:2,pointRadius:0,fill:true,tension:0.3}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
        scales:{x:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',maxTicksLimit:8,font:{family:'IBM Plex Mono',size:10}}},
                y:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',font:{family:'IBM Plex Mono',size:10},callback:v=>'$'+v.toLocaleString()}}}}});
    document.getElementById('coinChartStats').innerHTML=
      `<span>Low: <b>$${fmtP(minP)}</b></span><span>High: <b>$${fmtP(maxP)}</b></span><span style="color:${color}">24h: ${sign(chg)}${fmt(chg,2)}%</span>`;
  }catch(e){document.getElementById('coinChartStats').innerHTML='Chart unavailable'}
}
function closeChartModal(){
  document.getElementById('chartModal').classList.add('hidden');
  if(coinChartObj){coinChartObj.destroy();coinChartObj=null;}
}

// ── Performance ───────────────────────────────────────────────────────────────
async function loadPerformance(){
  try{
    const r=await fetch('/api/performance');
    const p=await r.json();
    document.getElementById('perfBestCoin').textContent=p.best_coin||'--';
    document.getElementById('perfAvgHold').textContent=(p.avg_hold_hours||0)+' h';
    document.getElementById('perfBestHour').textContent=p.best_hour||'--';
    document.getElementById('perfSL').textContent=p.total_sl_hits||0;
    document.getElementById('perfTP').textContent=p.total_tp_hits||0;
    // By coin chart
    const coins=Object.keys(p.by_coin||{});
    const pnls=coins.map(c=>p.by_coin[c].pnl);
    const colors=pnls.map(v=>v>=0?'rgba(0,230,118,0.7)':'rgba(255,23,68,0.7)');
    if(!perfCoinChartObj){
      const ctx=document.getElementById('perfCoinChart').getContext('2d');
      perfCoinChartObj=new Chart(ctx,{type:'bar',data:{labels:coins,datasets:[{data:pnls,backgroundColor:colors,borderWidth:0}]},
        options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
          scales:{x:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',font:{family:'IBM Plex Mono',size:11}}},
                  y:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',font:{family:'IBM Plex Mono',size:10},callback:v=>'+$'+v}}}}});
    }else{
      perfCoinChartObj.data.labels=coins;
      perfCoinChartObj.data.datasets[0].data=pnls;
      perfCoinChartObj.data.datasets[0].backgroundColor=colors;
      perfCoinChartObj.update();
    }
    // By hour chart
    const hours=Object.keys(p.by_hour||{}).map(Number).sort((a,b)=>a-b);
    const hourPnls=hours.map(h=>p.by_hour[h].pnl);
    const hourColors=hourPnls.map(v=>v>=0?'rgba(0,168,255,0.6)':'rgba(255,23,68,0.5)');
    if(!perfHourChartObj){
      const ctx2=document.getElementById('perfHourChart').getContext('2d');
      perfHourChartObj=new Chart(ctx2,{type:'bar',data:{labels:hours.map(h=>h+':00'),datasets:[{data:hourPnls,backgroundColor:hourColors,borderWidth:0}]},
        options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},
          scales:{x:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',font:{family:'IBM Plex Mono',size:10}}},
                  y:{grid:{color:'rgba(30,58,95,0.5)'},ticks:{color:'#5a7a9a',font:{family:'IBM Plex Mono',size:10}}}}}});
    }else{
      perfHourChartObj.data.labels=hours.map(h=>h+':00');
      perfHourChartObj.data.datasets[0].data=hourPnls;
      perfHourChartObj.update();
    }
    // Coin breakdown
    const maxAbs=Math.max(...Object.values(p.by_coin||{}).map(c=>Math.abs(c.pnl)),1);
    document.getElementById('perfCoinBreakdown').innerHTML=Object.entries(p.by_coin||{}).map(([coin,stats])=>{
      const wr=stats.count>0?(stats.wins/stats.count*100):0;
      const barW=(Math.abs(stats.pnl)/maxAbs*100);
      const barC=stats.pnl>=0?'var(--green)':'var(--red)';
      return `<div class="perf-coin-row">
        <span style="font-family:var(--mono);font-weight:700;color:var(--accent);min-width:48px">${coin}</span>
        <div class="perf-bar-wrap"><div class="perf-bar-fill" style="width:${barW}%;background:${barC}"></div></div>
        <span style="font-family:var(--mono);font-size:0.78rem;min-width:80px;text-align:right" class="${pnlClass(stats.pnl)}">${sign(stats.pnl)}$${fmt(Math.abs(stats.pnl),2)}</span>
        <span style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim);min-width:80px;text-align:right">${stats.count} trades</span>
        <span style="font-family:var(--mono);font-size:0.72rem;color:var(--text-dim);min-width:60px;text-align:right">${fmt(wr,0)}% WR</span>
      </div>`;
    }).join('')||'<div style="color:var(--text-dim);padding:20px;text-align:center">No data yet</div>';
  }catch(e){}
}

// ── Controls ──────────────────────────────────────────────────────────────────
async function botControl(action){
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
    const d=await r.json();
    showToast(d.message,'success'); fetchAll();
  }catch(e){showToast('Error','error')}
}
async function saveRisk(){
  const v=document.getElementById('riskSlider').value;
  try{
    const r=await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'set_risk',value:parseInt(v)})});
    const d=await r.json(); showToast(d.message,'success'); fetchAll();
  }catch(e){showToast('Error','error')}
}

// ── Init ─────────────────────────────────────────────────────────────────────
initTACoinSelector();
fetchAll();
setInterval(fetchAll,30000);
</script>
</body>
</html>"""
@flask_app.route("/")
def index():
    from flask import Response
    return Response(DASHBOARD_HTML, mimetype='text/html')


@flask_app.route("/api/state")
def api_state():
    prices_data   = get_prices()
    portfolio_val = get_portfolio_value(prices_data)
    total         = state["wins"] + state["losses"]
    binance_data = get_binance_prices()
    # Merge Binance real-time prices into price data for dashboard
    for coin_id, b in binance_data.items():
        if coin_id in prices_data:
            prices_data[coin_id]["price"]      = b["price"]   # use Binance live price
            prices_data[coin_id]["spread_pct"] = b.get("spread_pct", 0)
            prices_data[coin_id]["micro_mom"]  = b.get("micro_mom", 0)
            prices_data[coin_id]["bid"]        = b.get("bid", 0)
            prices_data[coin_id]["ask"]        = b.get("ask", 0)
        else:
            prices_data[coin_id] = b
    return jsonify({
        **state,
        "portfolio_value": round(portfolio_val, 2),
        "prices":          prices_data,
        "total_trades":    total,
        "win_rate":        round(state["wins"] / total * 100, 1) if total > 0 else 0,
        "roi_pct":         round(((portfolio_val - PAPER_BALANCE) / PAPER_BALANCE) * 100, 2),
        "binance_live":    True,
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
# EXTRA API ENDPOINTS
# ══════════════════════════════════════════════════════════════════════════════

@flask_app.route("/api/manual_buy", methods=["POST"])
def api_manual_buy():
    data    = request.get_json()
    coin_id = data.get("coin_id")
    if not coin_id or coin_id not in COINS:
        return jsonify({"message": "Invalid coin"}), 400
    if coin_id in state["open_trades"]:
        return jsonify({"message": "Trade already open for this coin"}), 400
    prices_data = get_prices()
    price       = prices_data.get(coin_id, {}).get("price", 0)
    if not price:
        return jsonify({"message": "Price unavailable"}), 400
    trade  = paper_buy(coin_id, price)
    symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
    state["trades_today"] += 1
    return jsonify({"message": f"Bought {symbol} @ ${price:,.4f}", "trade": trade})


@flask_app.route("/api/manual_sell", methods=["POST"])
def api_manual_sell():
    data    = request.get_json()
    coin_id = data.get("coin_id")
    if not coin_id or coin_id not in state["open_trades"]:
        return jsonify({"message": "No open trade for this coin"}), 400
    prices_data = get_prices()
    price       = prices_data.get(coin_id, {}).get("price",
                  state["open_trades"][coin_id]["entry_price"])
    pnl    = paper_sell(coin_id, price)
    state["total_pnl"]  += pnl
    state["daily_pnl"]  += pnl
    state["wins" if pnl >= 0 else "losses"] += 1
    symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
    return jsonify({"message": f"Sold {symbol} @ ${price:,.4f} | PnL: {pnl:+.4f}", "pnl": pnl})


@flask_app.route("/api/update_trade", methods=["POST"])
def api_update_trade():
    data      = request.get_json()
    coin_id   = data.get("coin_id")
    new_sl    = data.get("stop_loss")
    new_tp    = data.get("take_profit")
    if not coin_id or coin_id not in state["open_trades"]:
        return jsonify({"message": "Trade not found"}), 400
    if new_sl is not None:
        state["open_trades"][coin_id]["stop_loss"]   = float(new_sl)
    if new_tp is not None:
        state["open_trades"][coin_id]["take_profit"] = float(new_tp)
    save_state()
    return jsonify({"message": "Trade updated"})


@flask_app.route("/api/approve_trade", methods=["POST"])
def api_approve_trade():
    data    = request.get_json()
    coin_id = data.get("coin_id")
    action  = data.get("action")   # "approve" or "reject"
    cb_yes  = f"approve_{coin_id}"
    cb_no   = f"reject_{coin_id}"
    if cb_yes not in _pending:
        return jsonify({"message": "No pending trade for this coin"}), 400
    if action == "approve":
        pending     = _pending.pop(cb_yes)
        _pending.pop(cb_no, None)
        prices_data = get_prices()
        price       = prices_data.get(coin_id, {}).get("price", 0)
        if not price or coin_id in state["open_trades"]:
            return jsonify({"message": "Price unavailable or trade already open"}), 400
        trade = paper_buy(coin_id, price)
        state["trades_today"] += 1
        symbol = COIN_SYMBOLS.get(coin_id, coin_id.upper())
        return jsonify({"message": f"Approved! Bought {symbol} @ ${price:,.4f}"})
    else:
        _pending.pop(cb_yes, None)
        _pending.pop(cb_no, None)
        return jsonify({"message": "Trade rejected"})


@flask_app.route("/api/pending_approvals")
def api_pending_approvals():
    result = []
    for key, val in list(_pending.items()):
        if key.startswith("approve_"):
            coin_id = key[len("approve_"):]
            prices_data = get_prices()
            price = prices_data.get(coin_id, {}).get("price", 0)
            result.append({
                "coin_id":    coin_id,
                "symbol":     COIN_SYMBOLS.get(coin_id, coin_id.upper()),
                "price":      price,
                "confidence": val.get("decision", {}).get("confidence", 0),
                "sentiment":  val.get("decision", {}).get("sentiment", "neutral"),
                "reasoning":  val.get("decision", {}).get("reasoning", ""),
                "amount":     round(state["cash_balance"] * state["risk_per_trade"], 2),
            })
    return jsonify(result)


@flask_app.route("/api/technicals/<coin_id>")
def api_technicals(coin_id):
    if coin_id not in COINS:
        return jsonify({"error": "Invalid coin"}), 400
    ta = get_technical_indicators(coin_id)
    return jsonify(ta)


@flask_app.route("/api/performance")
def api_performance():
    history = state.get("trade_history", [])
    if not history:
        return jsonify({"best_coin": "N/A", "worst_coin": "N/A",
                        "avg_hold_hours": 0, "best_hour": "N/A",
                        "total_sl_hits": state.get("stop_losses_hit", 0),
                        "total_tp_hits": state.get("take_profits_hit", 0),
                        "by_coin": {}, "by_hour": {}})
    # By coin
    by_coin = {}
    for t in history:
        s = t["symbol"]
        if s not in by_coin:
            by_coin[s] = {"pnl": 0, "count": 0, "wins": 0}
        by_coin[s]["pnl"]   += t["pnl"]
        by_coin[s]["count"] += 1
        if t["pnl"] > 0:
            by_coin[s]["wins"] += 1
    best_coin  = max(by_coin, key=lambda c: by_coin[c]["pnl"]) if by_coin else "N/A"
    worst_coin = min(by_coin, key=lambda c: by_coin[c]["pnl"]) if by_coin else "N/A"
    # Avg hold time
    hold_times = []
    for t in history:
        try:
            entry = datetime.fromisoformat(t["entry_time"])
            exit_ = datetime.fromisoformat(t["exit_time"])
            hold_times.append((exit_ - entry).total_seconds() / 3600)
        except:
            pass
    avg_hold = round(sum(hold_times) / len(hold_times), 1) if hold_times else 0
    # By hour
    by_hour = {}
    for t in history:
        try:
            hour = datetime.fromisoformat(t["exit_time"]).hour
            if hour not in by_hour:
                by_hour[hour] = {"pnl": 0, "count": 0}
            by_hour[hour]["pnl"]   += t["pnl"]
            by_hour[hour]["count"] += 1
        except:
            pass
    best_hour = max(by_hour, key=lambda h: by_hour[h]["pnl"]) if by_hour else "N/A"
    return jsonify({
        "best_coin":   best_coin,
        "worst_coin":  worst_coin,
        "avg_hold_hours": avg_hold,
        "best_hour":   f"{best_hour}:00 UTC" if best_hour != "N/A" else "N/A",
        "total_sl_hits": state.get("stop_losses_hit", 0),
        "total_tp_hits": state.get("take_profits_hit", 0),
        "by_coin":     by_coin,
        "by_hour":     {str(k): v for k, v in by_hour.items()},
    })


@flask_app.route("/api/activity_log")
def api_activity_log():
    """Return last 50 AI decisions as activity log."""
    return jsonify(state.get("ai_log", [])[:50])


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
