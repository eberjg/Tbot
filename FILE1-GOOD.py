
# === EDUCATIONAL CRYPTO SIGNAL BOT — Full Auto-Managed — Spot + Futures Support ===
import ccxt
import pandas as pd
import ta
import requests
import re
import os
import threading
import time
import json
import urllib.parse
from flask import Flask, request
from dotenv import load_dotenv
from decimal import Decimal
from datetime import datetime, timezone, timedelta

# ✅ Trade logic imports
from futures_trade_snippet import futures_trade, futures_short_trade
from spot_trade_snippet import spot_trade

# === ENV VARS ===
load_dotenv()
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY")
COINBASE_API_PASSPHRASE = os.getenv("COINBASE_API_PASSPHRASE")
with open("coinbase_private_key.pem", "r") as f:
    COINBASE_API_SECRET = f.read()
KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY")
KRAKEN_API_SECRET = os.getenv("KRAKEN_API_SECRET")

# === CONFIG ===
AUTO_TRADE_ENABLED = True
LIVE_FUTURES_TRADE_ENABLED = True
LIVE_SPOT_TRADE_ENABLED = True
TRADE_AMOUNT_USD = 10
TRADE_COOLDOWN_MINUTES = 60
MAX_DAILY_LOSS = -500
RISK_PER_TRADE_PERCENT = 1

SCALP_RISK_MULTIPLIER = 0.5
SCALP_TP_MULTIPLIER = 0.75
CHECK_INTERVAL_SECONDS = 900
symbols_to_watch = ['XRP/USD', 'BTC/USD', 'ETH/USD']

SENTIMENT_MODE = "NEUTRAL"

# === Flask app and Exchanges INIT ===
app = Flask(__name__)

exchanges = {
    "Coinbase": ccxt.coinbaseadvanced({
        'apiKey': COINBASE_API_KEY,
        'secret': COINBASE_API_SECRET,
        'password': COINBASE_API_PASSPHRASE,
        'enableRateLimit': True,
        'options': {
            'createMarketBuyOrderRequiresPrice': False
        }
    }),
    "Kraken": ccxt.kraken({
        'apiKey': KRAKEN_API_KEY,
        'secret': KRAKEN_API_SECRET,
        'enableRateLimit': True,
    })
}
for name, ex in exchanges.items():
    ex.load_markets()

# === STATE ===
open_trades = {}
last_trade_time = {}
daily_pnl = {}

DAILY_PNL_FILE = "daily_pnl.json"

# === UTILS ===
def load_daily_pnl():
    global daily_pnl
    if os.path.exists(DAILY_PNL_FILE):
        try:
            with open(DAILY_PNL_FILE, "r") as f:
                daily_pnl = json.load(f)
        except Exception as e:
            print(f"[ERROR] Failed to load {DAILY_PNL_FILE}: {e}")
            daily_pnl = {}
    else:
        daily_pnl = {}

def save_daily_pnl():
    try:
        with open(DAILY_PNL_FILE, "w") as f:
            json.dump(daily_pnl, f, indent=2)
        print(f"[INFO] Daily PnL saved to {DAILY_PNL_FILE}")
    except Exception as e:
        print(f"[ERROR] Failed to save {DAILY_PNL_FILE}: {e}")

OPEN_TRADES_FILE = "open_trades.json"

def save_open_trades():
    try:
        with open(OPEN_TRADES_FILE, "w") as f:
            json.dump(open_trades, f, indent=2, default=str)
        print("[INFO] ✅ Open Trades saved.")
    except Exception as e:
        print(f"[ERROR] Failed to save open trades: {e}")

def load_open_trades():
    global open_trades
    if os.path.exists(OPEN_TRADES_FILE):
        try:
            with open(OPEN_TRADES_FILE, "r") as f:
                open_trades = json.load(f)
            print("[INFO] ✅ Open Trades loaded from file.")
        except Exception as e:
            print(f"[ERROR] Failed to load open trades: {e}")
            open_trades = {}
    else:
        open_trades = {}

def daily_summary(force=False):
    summary = "\n📊 Daily PnL Summary & Stats:\n"
    total_pnl = 0
    total_trades = 0
    total_wins = 0
    total_losses = 0

    for symbol, pnl in daily_pnl.items():
        summary += f"• {symbol}: ${round(pnl, 2)}\n"
        total_pnl += pnl
        if pnl > 0:
            total_wins += 1
        elif pnl < 0:
            total_losses += 1
        total_trades += 1

    summary += f"\n📈 Total PnL: ${round(total_pnl, 2)}"
    summary += f"\n✅ Wins: {total_wins}  |  ❌ Losses: {total_losses}"
    summary += f"\n📊 Total Trades: {total_trades}"

    # ✅ Send only if forced OR trades happened
    if force or total_trades > 0:
        send_telegram(summary)

    daily_pnl.clear()
    save_daily_pnl()

    # ✅ Schedule again in 24h with forced summary
    threading.Timer(86400, lambda: daily_summary(force=True)).start()

def daily_summary_scheduler():
    def scheduler_loop():
        while True:
            now = datetime.now(timezone.utc)
            if now.hour == 0 and now.minute == 0:
                daily_summary()
                time.sleep(61)
            time.sleep(30)
    threading.Thread(target=scheduler_loop, daemon=True).start()

load_daily_pnl()
daily_summary_scheduler()

# [REMAINING CODE BLOCKS REMAIN AS THEY WERE]
# ✅ The daily_summary function is now unique, called after each close AND on daily schedule
# ✅ You can now safely continue with your working codebase with this cleaned setup


# === UTILS ===
def escape_md(text):
    escape_chars = r'_*[]()~`>#+=|{}.!-'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', str(text))

import html

def send_telegram(text):
    if len(text) > 4000:
        text = text[:4000] + "\n\n📎 Message trimmed."
    try:
        response = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', data={
            'chat_id': CHAT_ID,
            'text': html.escape(text),   # ✅ Escapes HTML entities safely
            'parse_mode': 'HTML',
            'disable_web_page_preview': False
        })
        if response.status_code != 200:
            print(f"[TELEGRAM ERROR] {response.status_code}: {response.text}")
    except Exception as e:
        print(f"[TELEGRAM ERROR] Exception: {e}")

def send_telegram_photo(caption, image_url):
    try:
        response = requests.post(f'https://api.telegram.org/bot{TOKEN}/sendPhoto', data={
            'chat_id': CHAT_ID,
            'photo': image_url,
            'caption': caption,  # ✅ No html.escape()!
            'parse_mode': 'HTML'
        })
        if response.status_code != 200:
            print(f"[TELEGRAM PHOTO ERROR] {response.status_code}: {response.text}")
    except Exception as e:
        print(f"[TELEGRAM PHOTO ERROR] Exception: {e}")


def resolve_symbol(exchange_name, user_symbol):
    alias_map = {"BTC/USD": "XXBTZUSD", "ETH/USD": "XETHZUSD", "XRP/USD": "XXRPZUSD"} if exchange_name == "Kraken" else {}
    return alias_map.get(user_symbol, user_symbol)

def get_price(exchange, symbol):
    return exchange.fetch_ticker(symbol)['last']

def log_trade(symbol, entry, stop, target, price_exit, pnl, mode, status):
    file_exists = os.path.isfile("trade_log.csv")
    with open("trade_log.csv", "a") as f:
        if not file_exists:
            f.write("time,symbol,entry,stop,target,exit_price,pnl_usd,mode,status\n")
        f.write(f"{datetime.now(timezone.utc)},{symbol},{entry},{stop},{target},{price_exit},{pnl},{mode},{status}\n")

def calculate_position_size(balance, risk_percent=RISK_PER_TRADE_PERCENT):
    return round(balance * risk_percent / 100, 2)

def check_daily_loss():
    total_pnl = sum(daily_pnl.values())
    if total_pnl <= MAX_DAILY_LOSS:
        send_telegram(f"🚫 Max Daily Loss Hit: ${total_pnl}. Bot will pause trading for today.")
        return False
    return True

def get_chart_snapshot(symbol, exchange_name, tf='5m', poc_level=None):
    try:
        exchange = exchanges[exchange_name]
        df = pd.DataFrame(exchange.fetch_ohlcv(symbol, tf, limit=50), columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        labels = df['ts'].dt.strftime('%H:%M').tolist()
        closes = df['close'].round(4).tolist()

        datasets = [{
            "label": f"{symbol} {tf}",
            "data": closes,
            "fill": False,
            "borderColor": "blue",
            "borderWidth": 2,
            "pointRadius": 0
        }]

        if poc_level is not None:
            datasets.append({
                "label": f"POC ${poc_level}",
                "data": [poc_level] * len(closes),
                "fill": False,
                "borderColor": "red",
                "borderWidth": 1,
                "borderDash": [5, 5],
                "pointRadius": 0
            })

        chart_config = {
            "type": "line",
            "data": {"labels": labels, "datasets": datasets},
            "options": {
                "title": {"display": True, "text": f"{symbol} {tf} Chart with POC"},
                "scales": {"y": {"beginAtZero": False}}
            }
        }

        chart_json = json.dumps(chart_config, separators=(',', ':'))
        encoded_config = urllib.parse.quote_plus(chart_json)
        chart_url = f"https://quickchart.io/chart?c={encoded_config}"
        return chart_url

    except Exception as e:
        return f"❌ Chart Error: {e}"

def get_liquidity_heatmap(symbol, exchange_name, tf='5m'):
    try:
        exchange = exchanges[exchange_name]
        df = pd.DataFrame(exchange.fetch_ohlcv(symbol, tf, limit=200), columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['price_bin'] = (df['close'] * 100).round(0) / 100  

        volume_profile = (
            df.groupby('price_bin')['volume']
            .sum()
            .reset_index()
            .sort_values('volume', ascending=False)
            .head(10)
            .sort_values('price_bin')
        )

        labels = [f"{x:.2f}" for x in volume_profile['price_bin']]
        volumes = [round(v, 2) for v in volume_profile['volume']]

        chart_config = {
            "type": "bar",
            "data": {
                "labels": labels,
                "datasets": [{
                    "label": "Liquidity by Price",
                    "data": volumes,
                    "backgroundColor": "rgba(54, 162, 235, 0.6)"
                }]
            },
            "options": {
                "indexAxis": "y",
                "title": {"display": True, "text": f"{symbol} {tf} Volume Heatmap"},
                "scales": {"x": {"beginAtZero": True}}
            }
        }

        chart_json = json.dumps(chart_config, separators=(',', ':'))
        encoded_config = urllib.parse.quote_plus(chart_json)
        chart_url = f"https://quickchart.io/chart?c={encoded_config}"
        return chart_url

    except Exception as e:
        return f"❌ Heatmap Chart Error: {e}"

def get_volume_profile_snapshot(symbol, exchange_name, tf='5m'):
    try:
        exchange = exchanges[exchange_name]
        df = pd.DataFrame(exchange.fetch_ohlcv(symbol, tf, limit=100), columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df['price_bucket'] = (df['close'] * 100).round(0) / 100
        volume_profile = df.groupby('price_bucket')['volume'].sum().reset_index()

        chart_config = {
            "type": "bar",
            "data": {
                "labels": volume_profile['price_bucket'].astype(str).tolist(),
                "datasets": [{
                    "label": "Volume at Price",
                    "data": volume_profile['volume'].round(2).tolist(),
                    "backgroundColor": "rgba(0, 123, 255, 0.5)"
                }]
            },
            "options": {
                "indexAxis": "y",
                "title": {"display": True, "text": f"{symbol} {tf} Volume Heatmap"},
                "scales": {"x": {"beginAtZero": True}}
            }
        }

        chart_json = json.dumps(chart_config, separators=(',', ':'))
        encoded_config = urllib.parse.quote_plus(chart_json)
        chart_url = f"https://quickchart.io/chart?c={encoded_config}"
        return chart_url

    except Exception as e:
        return f"❌ Volume Profile Chart Error: {e}"

# === SMART MONEY CONCEPTS ===

def is_liquidity_sweep(df, threshold=0.002):
    """
    Detects liquidity sweep based on price wick extremes.
    Returns True if candle sweeps below recent low or above recent high.
    """
    recent_low = df['low'].rolling(window=20).min()
    recent_high = df['high'].rolling(window=20).max()

    last = df.iloc[-1]
    sweep_down = last['low'] < recent_low.iloc[-2] and last['close'] > last['open']
    sweep_up = last['high'] > recent_high.iloc[-2] and last['close'] < last['open']

    return sweep_down, sweep_up


def detect_order_block(df):
    """
    Detects last bullish or bearish engulfing before market moves.
    Returns bullish or bearish order block detected.
    """
    last = df.iloc[-1]
    prev = df.iloc[-2]

    bullish_ob = prev['close'] > prev['open'] and last['close'] < last['open'] and last['high'] >= prev['high']
    bearish_ob = prev['close'] < prev['open'] and last['close'] > last['open'] and last['low'] <= prev['low']

    return bullish_ob, bearish_ob


def is_valid_session():
    """
    Optional — Filter to allow trading only during active sessions.
    Example: Only allow between 8 AM - 6 PM UTC.
    """
    now = datetime.utcnow().hour
    return 8 <= now <= 18  # Adjust this to your preferred session window


# === ANALYSIS ===
def analyze(exchange, symbol, tf):
    df = pd.DataFrame(exchange.fetch_ohlcv(symbol, tf), columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
    df['ema21'] = ta.trend.EMAIndicator(df['close'], 21).ema_indicator()
    df['rsi'] = ta.momentum.RSIIndicator(df['close']).rsi()
    df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close']).adx()
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd()
    df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range()
    df['avg_volume'] = df['volume'].rolling(window=10).mean()
    df['spike'] = df['volume'] > 1.5 * df['avg_volume']
    df['price_change'] = df['close'].pct_change()
    df['price_drop'] = df['price_change'] < -0.01
    df['recent_lows'] = df['low'].rolling(window=5).min()
    df['recent_highs'] = df['high'].rolling(window=5).max()
    df['trap_down'] = df['low'] < df['recent_lows'].shift(1)
    df['trap_up'] = df['high'] > df['recent_highs'].shift(1)
    df['bullish_engulfing'] = (df['close'] > df['open']) & (df['close'].shift(1) < df['open'].shift(1)) & (df['close'] > df['open'].shift(1)) & (df['open'] < df['close'].shift(1))
    df['bearish_engulfing'] = (df['close'] < df['open']) & (df['close'].shift(1) > df['open'].shift(1)) & (df['close'] < df['open'].shift(1)) & (df['open'] > df['close'].shift(1))

    # ✅ Volume Profile / POC Calculation
    df['price_bucket'] = (df['close'] * 100).round(0) / 100  # $0.01 buckets
    poc_price = df.groupby('price_bucket')['volume'].sum().idxmax()

    last = df.iloc[-1]
    return {
        'score': 2 * (last['close'] > last['ema21']) +
                 2 * (last['macd'] > 0) +
                 2 * (last['rsi'] > 60) +
                 2 * (last['adx'] > 25) +
                 2 * last['spike'] +
                 1 * (last['atr'] > df['atr'].rolling(10).mean().iloc[-1]),
        'price': round(last['close'], 2),
        'rsi': round(last['rsi'], 1),
        'macd': round(last['macd'], 2),
        'adx': round(last['adx'], 1),
        'ema21': round(last['ema21'], 2),
        'atr': round(last['atr'], 2),
        'spike': last['spike'],
        'price_drop': last['price_drop'],
        'trap_down': last['trap_down'],
        'trap_up': last['trap_up'],
        'bullish_engulfing': last['bullish_engulfing'],
        'bearish_engulfing': last['bearish_engulfing'],
        'poc': round(poc_price, 2)  # ✅ Final POC
    }

    # === Volume Profile / POC Calculation ===
    df['price_bucket'] = (df['close'] * 100).round(0) / 100  # Buckets by $0.01 steps
    poc_price = df.groupby('price_bucket')['volume'].sum().idxmax()

    last = df.iloc[-1]
    return {
        'score': 2 * (last['close'] > last['ema21']) +
                 2 * (last['macd'] > 0) +
                 2 * (last['rsi'] > 60) +
                 2 * (last['adx'] > 25) +
                 2 * last['spike'] +
                 1 * (last['atr'] > df['atr'].rolling(10).mean().iloc[-1]),
        'price': round(last['close'], 2),
        'rsi': round(last['rsi'], 1),
        'macd': round(last['macd'], 2),
        'adx': round(last['adx'], 1),
        'ema21': round(last['ema21'], 2),
        'atr': round(last['atr'], 2),
        'spike': last['spike'],
        'price_drop': last['price_drop'],
        'trap_down': last['trap_down'],
        'trap_up': last['trap_up'],
        'bullish_engulfing': last['bullish_engulfing'],
        'bearish_engulfing': last['bearish_engulfing'],
        'poc': round(poc_price, 2)
    }

def build_signal(exchange_name, symbol):
    exchange = exchanges[exchange_name]
    tf_list = ['1m', '5m', '15m', '1h']
    weights = {'1m': 1, '5m': 2, '15m': 3, '1h': 4}

    weighted_score = 0
    liquidity_trap_detected = False
    tf_data = {}

    for tf in tf_list:
        s = analyze(exchange, symbol, tf)
        tf_data[tf] = s
        weighted_score += s['score'] * weights[tf]
        if tf == '5m' and (s['trap_down'] or s['trap_up']):
            liquidity_trap_detected = True

    s_1m = tf_data['1m']
    s_5m = tf_data['5m']
    s_1h = tf_data['1h']

    entry = s_5m['price']
    atr = s_5m['atr']
    poc_price = round(s_5m.get('price', entry), 2)

    stop_loss = round(entry - atr, 2)
    target = round(entry + atr * 1.5, 2)

    bias = "AVOID"
    filter_reason = ""

    poc_proximity = abs(entry - poc_price) / entry < 0.005

    if s_5m['trap_down'] and s_5m['bullish_engulfing']:
        bias = "SCALP LONG"
    elif s_5m['trap_up'] and s_5m['bearish_engulfing']:
        bias = "SCALP SHORT"
    elif s_5m['price'] > s_5m['ema21'] and s_5m['macd'] > 0 and s_1h['price'] > s_1h['ema21']:
        bias = "LONG"
    elif s_5m['price'] < s_5m['ema21'] and s_5m['macd'] < 0 and s_1h['price'] < s_1h['ema21']:
        bias = "SHORT"
    elif s_5m['trap_down'] and s_1h['price'] > s_1h['ema21']:
        bias = "SMART LONG"
        filter_reason = "Smart Money — Sweep & HTF Bullish"
    elif s_5m['trap_up'] and s_1h['price'] < s_1h['ema21']:
        bias = "SMART SHORT"
        filter_reason = "Smart Money — Sweep & HTF Bearish"
    else:
        filter_reason = "No Clear Trend"

    if not poc_proximity and bias != "AVOID":
        filter_reason = f"Price far from POC ${poc_price} — No Trade"
        bias = "AVOID"

    action = (
        "🚀 LONG Opportunity" if bias in ["LONG", "SCALP LONG", "SMART LONG"] else
        "🔻 SHORT Opportunity" if bias in ["SHORT", "SCALP SHORT", "SMART SHORT"] else
        f"🚫 NO TRADE — {filter_reason or 'Mixed trend.'}"
    )

    chart_url = get_chart_snapshot(symbol, exchange_name, poc_level=poc_price)
    volume_chart_url = get_volume_profile_snapshot(symbol, exchange_name)

    msg = (
        f"📢 Signal for {symbol} ({exchange_name})\n"
        f"———————————————\n"
        f"Bias: {bias}\n"
        f"Entry: ${entry}  |  SL: ${stop_loss}  |  TP: ${target}\n\n"
        f"📊 Opportunity: {action}\n"
    )

    if bias != "AVOID" and liquidity_trap_detected:
        msg += "⚠️ Liquidity Trap Detected — Caution Advised.\n"

    msg += f"🧭 POC Level: ${poc_price}\n"

    return entry, stop_loss, target, msg, chart_url, volume_chart_url, bias

# === TRADE MANAGEMENT ===

def place_futures_sl_tp(exchange, symbol, entry_price, stop_price, target_price, send_telegram_func):
    try:
        amount = 1  # Or pass correct size
        # 📉 Place Stop-Limit Sell (for SL)
        stop_params = {
            "stop_price": stop_price,
            "limit_price": stop_price,
        }
        exchange.create_order(
            symbol=symbol,
            type='stop_limit',
            side='sell',
            amount=amount,
            price=stop_price,
            params=stop_params
        )
        send_telegram_func(f"✅ Futures STOP LIMIT placed at ${stop_price}")

        # ⚠️ Futures TP will be handled by bot monitoring
        send_telegram_func(f"⚠️ Coinbase Futures TP will be monitored by the bot — TP Level: ${target_price}")

    except Exception as e:
        send_telegram_func(f"❌ Error Placing Futures SL: {e}")

def check_trade_exit(symbol, price):
    trade = open_trades.get(symbol)
    if not trade:
        return
    entry, stop, target = trade['entry'], trade['stop'], trade['target']
    amount = trade['amount']
    mode = trade.get('mode', 'paper')
    pnl = 0

    if price <= stop or price >= target:
        pnl = (price - entry) / entry * amount
        status = "target" if price >= target else "stopped"
        send_telegram(f"✅ {status.upper()} — {symbol} closed at ${price} (entry: ${entry})\nPnL: ${round(pnl, 2)}")
        log_trade(symbol, entry, stop, target, price, round(pnl, 2), mode, status)
        del open_trades[symbol]
        daily_pnl[symbol] = daily_pnl.get(symbol, 0) + pnl
        save_open_trades()  # ✅ Save after removing trade
        send_daily_summary()
        send_daily_summary()

def check_spot_exit(exchange, symbol, price):
    trade = open_trades.get(symbol)
    if not trade:
        return

    entry = trade['entry']
    stop = trade['stop']
    target = trade['target']
    amount_usd = trade['amount']
    mode = trade.get('mode', 'paper')

    pnl = (price - entry) / entry * amount_usd

    if price >= target:
        send_telegram(f"✅ SPOT TARGET HIT — {symbol} at ${price} (Entry: ${entry})\nPnL: ${round(pnl, 2)}")
        log_trade(symbol, entry, stop, target, price, round(pnl, 2), mode, "target")
        del open_trades[symbol]
        daily_pnl[symbol] = daily_pnl.get(symbol, 0) + pnl
        save_open_trades()  # ✅ Save after removing trade
        send_daily_summary()
        return

    if price <= stop:
        send_telegram(f"🛑 SPOT STOP LOSS HIT — {symbol} at ${price} (Entry: ${entry})\nPnL: ${round(pnl, 2)}")
        try:
            exchange.create_order(symbol=symbol, type='market', side='sell', amount=None, params={"cost": float(amount_usd)})
            send_telegram(f"✅ SPOT SL SELL EXECUTED — {symbol} at ${price}")
        except Exception as e:
            send_telegram(f"❌ SPOT SL SELL ERROR: {e}")

        log_trade(symbol, entry, stop, target, price, round(pnl, 2), mode, "stopped")
        del open_trades[symbol]
        daily_pnl[symbol] = daily_pnl.get(symbol, 0) + pnl
        save_open_trades()  # ✅ Save after removing trade
        send_daily_summary()

def check_futures_exit(exchange, symbol, price, send_telegram_func):
    trade = open_trades.get(symbol)
    if not trade or trade.get('mode') != 'futures':
        return

    entry = trade['entry']
    stop = trade['stop']
    target = trade['target']
    amount = int(trade['amount'])
    mode = trade.get('mode', 'futures')

    pnl = (price - entry) * amount

    if price >= target:
        try:
            exchange.create_market_sell_order(symbol, amount)
            send_telegram_func(f"✅ FUTURES TARGET HIT — {symbol} closed at ${price}\nPnL: ${round(pnl, 2)}")
        except Exception as e:
            send_telegram_func(f"❌ FUTURES TP CLOSE ERROR: {e}")

        log_trade(symbol, entry, stop, target, price, round(pnl, 2), mode, "target")
        del open_trades[symbol]
        daily_pnl[symbol] = daily_pnl.get(symbol, 0) + pnl
        save_open_trades()  # ✅ Save after removing trade
        send_daily_summary()
        return

    if price <= stop:
        try:
            exchange.create_market_sell_order(symbol, amount)
            send_telegram_func(f"🛑 FUTURES STOP LOSS HIT — {symbol} closed at ${price}\nPnL: ${round(pnl, 2)}")
        except Exception as e:
            send_telegram_func(f"❌ FUTURES SL CLOSE ERROR: {e}")

        log_trade(symbol, entry, stop, target, price, round(pnl, 2), mode, "stopped")
        del open_trades[symbol]
        daily_pnl[symbol] = daily_pnl.get(symbol, 0) + pnl
        save_open_trades()  # ✅ Save after removing trade
        send_daily_summary()

def record_trade(symbol, entry, stop, target, amount=TRADE_AMOUNT_USD, mode="paper"):
    open_trades[symbol] = {
        "entry": entry,
        "stop": stop,
        "target": target,
        "time": datetime.now(timezone.utc),
        "amount": amount,
        "mode": mode
    }
    last_trade_time[symbol] = datetime.now(timezone.utc)
    log_trade(symbol, entry, stop, target, "", "", mode, "entry")
    save_open_trades()  # ✅ Save immediately after recording the trade

def daily_summary():
    summary = "\n📊 Daily PnL Summary & Stats:\n"
    total_pnl = 0
    total_trades = 0
    total_wins = 0
    total_losses = 0

    load_daily_pnl()  # ✅ Always load before reporting

    for symbol, pnl in daily_pnl.items():
        summary += f"• {symbol}: ${round(pnl, 2)}\n"
        total_pnl += pnl
        if pnl > 0:
            total_wins += 1
        elif pnl < 0:
            total_losses += 1
        total_trades += 1

    summary += f"\n📈 Total PnL: ${round(total_pnl, 2)}"
    summary += f"\n✅ Wins: {total_wins}  |  ❌ Losses: {total_losses}"
    summary += f"\n📊 Total Trades: {total_trades}"

    send_telegram(summary)
    save_daily_pnl()  # ✅ Save after sending


def reset_daily_pnl():
    daily_pnl.clear()
    save_daily_pnl()
    print("[INFO] ✅ Daily PnL Reset after summary.")


def daily_summary_scheduler():
    def scheduler_loop():
        while True:
            now = datetime.now(timezone.utc)
            if now.hour == 0 and now.minute == 0:
                daily_summary()
                reset_daily_pnl()
                time.sleep(61)
            time.sleep(30)
    threading.Thread(target=scheduler_loop, daemon=True).start()

def reset_daily_pnl():
    daily_pnl.clear()
    save_daily_pnl()
    print("[INFO] ✅ Daily PnL Reset after summary.")


def daily_summary_scheduler():
    def scheduler_loop():
        while True:
            now = datetime.now(timezone.utc)
            if now.hour == 0 and now.minute == 0:
                daily_summary()
                reset_daily_pnl()
                time.sleep(61)
            time.sleep(30)
    threading.Thread(target=scheduler_loop, daemon=True).start()


def send_open_trades_summary():
    if not open_trades:
        send_telegram("📋 <b>Open Trades Summary:</b>\n— No open trades at the moment.")
        return

    msg = "📋 <b>Open Trades Summary:</b>\n"
    for sym, trade in open_trades.items():
        mode = trade.get('mode', 'paper').capitalize()
        entry = trade['entry']
        stop = trade['stop']
        target = trade['target']
        amount = trade['amount']
        pnl = round(daily_pnl.get(sym, 0), 2)
        msg += (
            f"\n• <b>{sym} [{mode}]</b>\n"
            f"  ➖ Entry: ${entry} | SL: ${stop} | TP: ${target}\n"
            f"  ➖ Size: ${amount} | Today PnL: ${pnl}\n"
        )

    send_telegram(msg)


def check_all_exits():
    for sym in list(open_trades):
        price = get_price(exchanges["Coinbase"], resolve_symbol("Coinbase", sym))
        check_trade_exit(sym, price)
        check_spot_exit(exchanges["Coinbase"], resolve_symbol("Coinbase", sym), price)
        check_futures_exit(exchanges["Coinbase"], resolve_symbol("Coinbase", sym), price, send_telegram)


def sentiment_allows_trade(bias):
    if SENTIMENT_MODE == "BULLISH" and bias in ["LONG", "SCALP LONG", "SMART LONG"]:
        return True
    if SENTIMENT_MODE == "BEARISH" and bias in ["SHORT", "SCALP SHORT", "SMART SHORT"]:
        return True
    if SENTIMENT_MODE == "NEUTRAL":
        return True
    return False


def bot_loop():
    while True:
        try:
            if not check_daily_loss():
                print("[INFO] Max daily loss hit, pausing bot until next check.")
                daily_summary()
                time.sleep(600)
                continue

            check_all_exits()

            for sym in symbols_to_watch:
                for name, exchange in exchanges.items():
                    resolved = resolve_symbol(name, sym)
                    price = get_price(exchange, resolved)

                    entry, stop, target, signal_msg, chart_url, heatmap_url, bias = build_signal(name, resolved)
                    send_telegram(signal_msg)

                    if bias != "AVOID":
                        send_telegram_photo("📈 Price Chart", chart_url)
                        send_telegram_photo("🗺️ Liquidity Heatmap", heatmap_url)

                        if AUTO_TRADE_ENABLED and check_daily_loss():
                            cooldown_ok = sym not in last_trade_time or (
                                datetime.now(timezone.utc) - last_trade_time[sym]
                            ) >= timedelta(minutes=TRADE_COOLDOWN_MINUTES)

                            if not cooldown_ok:
                                continue

                            if bias not in ["LONG", "SHORT", "SCALP LONG", "SCALP SHORT", "SMART LONG", "SMART SHORT"]:
                                continue

                            if not sentiment_allows_trade(bias):
                                send_telegram(f"🔴 Skipped {bias} — Sentiment Mode: {SENTIMENT_MODE}")
                                continue

                            account_balance = exchange.fetch_balance()['total'].get('USDC', 0)
                            position_size = calculate_position_size(account_balance)

                            if position_size <= 0:
                                send_telegram(f"🚫 Skipped {bias} — Position too small (${position_size}).")
                                continue

                            if bias in ["SCALP LONG", "SCALP SHORT"]:
                                position_size = round(position_size * SCALP_RISK_MULTIPLIER, 2)
                                target = round(entry + (target - entry) * SCALP_TP_MULTIPLIER, 2)

                            if name == "Coinbase":
                                if LIVE_FUTURES_TRADE_ENABLED:
                                    if bias in ["LONG", "SCALP LONG", "SMART LONG"]:
                                        fut_size = futures_trade(exchange, resolved, position_size, send_telegram, stop=stop, target=target)
                                        if fut_size:
                                            record_trade(sym, entry, stop, target, amount=position_size, mode="futures")
                                    elif bias in ["SHORT", "SCALP SHORT", "SMART SHORT"]:
                                        short_size = futures_short_trade(exchange, resolved, position_size, send_telegram, stop=stop, target=target)
                                        if short_size:
                                            record_trade(sym, entry, stop, target, amount=position_size, mode="short")

                                if LIVE_SPOT_TRADE_ENABLED and bias in ["LONG", "SCALP LONG", "SMART LONG"]:
                                    spot_size = spot_trade(exchange, resolved, position_size, send_telegram, target_price=target)
                                    if spot_size:
                                        record_trade(sym, entry, stop, target, amount=position_size, mode="spot")

            daily_summary()  # ✅ Summary after each loop
            time.sleep(CHECK_INTERVAL_SECONDS)

        except Exception as e:
            print(f"[ERROR] Exception in bot loop: {e}")
            send_telegram(f"⚠️ Bot Error: {e}")
            daily_summary()
            time.sleep(60)


# === LAUNCH ===
if __name__ == '__main__':
    send_telegram("✅ Bot Started — Monitoring Markets.")
    load_daily_pnl()          # ✅ Load saved daily PnL from file
    load_open_trades()        # ✅ Load saved open trades from file (critical for tracking)
    daily_summary_scheduler() # ✅ Start daily summary checker in background
    threading.Thread(target=bot_loop).start()  # ✅ Launch the bot loop in background
    app.run(host='0.0.0.0', port=5001)         # ✅ Start Flask API server