# === EDUCATIONAL CRYPTO SIGNAL BOT — Spot + Futures Support ===

import ccxt
import pandas as pd
import ta
import requests
import re
import os
import threading
import time
from flask import Flask, request
from dotenv import load_dotenv
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from futures_trade_snippet import futures_trade
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
TRADE_AMOUNT_USD = 100
TRADE_COOLDOWN_MINUTES = 60

# === STATE ===
open_trades = {}
last_trade_time = {}
daily_pnl = {}

# === EXCHANGES ===
app = Flask(__name__)
exchanges = {
    "Coinbase": ccxt.coinbaseadvanced({
        'apiKey': COINBASE_API_KEY,
        'secret': COINBASE_API_SECRET,
        'password': COINBASE_API_PASSPHRASE,
        'enableRateLimit': True,
    }),
    "Kraken": ccxt.kraken({
        'apiKey': KRAKEN_API_KEY,
        'secret': KRAKEN_API_SECRET,
        'enableRateLimit': True,
    })
}
for name, ex in exchanges.items():
    ex.load_markets()

# === UTILS ===
def escape_md(text):
    escape_chars = r'_*[]()~`>#+=|{}.!-'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', str(text))

def send_telegram(text):
    text = escape_md(text)
    text = text[:4000] + "\n\n📎 Message trimmed." if len(text) > 4000 else text
    text = text.encode('utf-16', 'surrogatepass').decode('utf-16', 'ignore')
    requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', data={'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'MarkdownV2'})

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

# === ANALYSIS ===
def analyze(exchange, symbol, tf):
    df = pd.DataFrame(exchange.fetch_ohlcv(symbol, tf), columns=['ts','open','high','low','close','volume'])
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
        'price_drop': last['price_drop']
    }

def build_signal(exchange_name, symbol):
    exchange = exchanges[exchange_name]
    tf_list = ['1m', '5m', '15m', '1h', '1d']
    weights = {'1m': 1, '5m': 2, '15m': 3, '1h': 4, '1d': 5}
    weighted_score = 0
    volume_spikes, recent_drop = [], False
    details = ""
    for tf in tf_list:
        s = analyze(exchange, symbol, tf)
        weighted_score += s['score'] * weights[tf]
        if tf == '5m':
            entry = s['price']
            stop_loss = round(entry - s['atr'], 2)
            target = round(entry + s['atr'] * 1.5, 2)
        if s['spike']:
            volume_spikes.append(tf)
        if s['price_drop']:
            recent_drop = True
        details += f"\n{symbol} {tf} — ✅\nPrice: ${s['price']} | RSI: {s['rsi']} | MACD: {s['macd']} | ADX: {s['adx']}\nEMA21: ${s['ema21']} | ATR: {s['atr']} | Spike: {'Yes' if s['spike'] else 'No'} | Drop: {'Yes' if s['price_drop'] else 'No'}"
    if recent_drop and weighted_score > 25:
        bias = "SCALP"
        action = "🔥 SCALP — Bounce Setup"
    elif weighted_score >= 30:
        bias = "STRONG LONG"
        action = "ENTRY READY — Strong confluence."
    elif weighted_score >= 24:
        bias = "LONG"
        action = "CAUTIOUS ENTRY — Some confluence."
    else:
        bias = "AVOID"
        action = "NO TRADE — Low confidence."
    return (f"Signal for {symbol} ({exchange_name})\n-----------------------------\nScore: {weighted_score} → Bias: {bias}\nEntry: ${entry}, SL: ${stop_loss}, TP: ${target}\n{action}\n{details}")

# === TRADE MANAGEMENT ===
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

def should_trade(symbol):
    now = datetime.now(timezone.utc)
    last_time = last_trade_time.get(symbol)
    return not last_time or now - last_time >= timedelta(minutes=TRADE_COOLDOWN_MINUTES)

def record_trade(symbol, entry, stop, target, mode="paper"):
    open_trades[symbol] = {"entry": entry, "stop": stop, "target": target, "time": datetime.now(timezone.utc), "amount": TRADE_AMOUNT_USD, "mode": mode}
    last_trade_time[symbol] = datetime.now(timezone.utc)
    log_trade(symbol, entry, stop, target, "", "", mode, "entry")

# === DAILY SUMMARY ===
def daily_summary():
    summary = "\n📊 Daily PnL Summary:\n"
    for s, p in daily_pnl.items():
        summary += f"{s}: ${round(p, 2)}\n"
    send_telegram(summary)
    daily_pnl.clear()
    threading.Timer(86400, daily_summary).start()

def bot_loop():
    while True:
        try:
            for sym in ['BTC/USD', 'XRP/USD']:
                for name, exchange in exchanges.items():
                    resolved = resolve_symbol(name, sym)
                    price = get_price(exchange, resolved)
                    print(f"\n[DEBUG] Checking {sym} on {name} — Current Price: {price}")

                    check_trade_exit(sym, price)
                    msg = build_signal(name, resolved)
                    send_telegram(msg)

                    entry_ready = (
                        ("ENTRY READY" in msg and "STRONG LONG" in msg) or
                        ("SCALP" in msg)
                    )
                    cooldown_ok = should_trade(sym)
                    print(f"[DEBUG] entry_ready={entry_ready}, cooldown_ok={cooldown_ok}, AUTO_TRADE_ENABLED={AUTO_TRADE_ENABLED}")

                    if AUTO_TRADE_ENABLED and entry_ready and cooldown_ok:
                        match = re.search(r"Entry: \$(\d+\.?\d*)", msg)
                        sl_match = re.search(r"SL: \$(\d+\.?\d*)", msg)
                        tp_match = re.search(r"TP: \$(\d+\.?\d*)", msg)

                        if match and sl_match and tp_match:
                            entry = float(match.group(1))
                            stop = float(sl_match.group(1))
                            target = float(tp_match.group(1))

                            print(f"[DEBUG] Trade Conditions Met: entry={entry}, stop={stop}, target={target}")
                            record_trade(sym, entry, stop, target)

                            if name == "Coinbase":
                                if LIVE_FUTURES_TRADE_ENABLED:
                                    futures_trade(
                                        exchange,
                                        resolved,
                                        TRADE_AMOUNT_USD,
                                        send_telegram,
                                        stop=stop,
                                        target=target
                                    )
                                if LIVE_SPOT_TRADE_ENABLED:
                                    spot_trade(
                                        exchange,
                                        resolved,
                                        TRADE_AMOUNT_USD,
                                        send_telegram
                                    )
                        else:
                            print("[DEBUG] Trade entry info not fully parsed — skipping.")
                    else:
                        print("[DEBUG] Conditions not met for trade. Skipping.")
            time.sleep(900)
        except Exception as e:
            print(f"[ERROR] Exception in bot loop: {e}")
            send_telegram(f"⚠️ Bot error: {e}")
            time.sleep(60)

# === LAUNCH ===
if __name__ == '__main__':
    send_telegram("✅ Bot started.")
    daily_summary()
    threading.Thread(target=bot_loop).start()
    app.run(host='0.0.0.0', port=5001)