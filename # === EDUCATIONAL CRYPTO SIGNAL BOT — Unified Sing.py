# === EDUCATIONAL CRYPTO SIGNAL BOT — Unified Single File with Fast Breakdown Mode (Updated) ===

# === Imports ===
import ccxt, pandas as pd, ta, requests, re, os, threading, time, json, urllib.parse
from flask import Flask, request, render_template_string
from dotenv import load_dotenv
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from dateutil import parser

# === ENV Variables ===
load_dotenv()
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY")
COINBASE_API_PASSPHRASE = os.getenv("COINBASE_API_PASSPHRASE")
with open("coinbase_private_key.pem", "r") as f:
    COINBASE_API_SECRET = f.read()

# === Config ===
symbols_to_watch = ["XRP/USD", "BTC/USD"]
AUTO_TRADE_ENABLED = True
FAST_BREAKDOWN_ENABLED = True
FAST_BREAKDOWN_CHECK_INTERVAL = 2    # Every 2 seconds
CHECK_INTERVAL_SECONDS = 900         # Regular bot loop (15 min)

TRADE_AMOUNT_USD = 10
RISK_PER_TRADE_PERCENT = 1
MIN_POSITION_SIZE = 10
MAX_DAILY_LOSS = -500

# === State ===
tape_snapshot, last_sent_time, open_trades, daily_pnl = {}, {}, {}, {}
liquidity_snapshot = {}
total_realized_pnl = 0.0

# === Flask App ===
app = Flask(__name__)

# === Exchange Initialization ===
exchanges = {
    "Coinbase": ccxt.coinbaseadvanced({
        'apiKey': COINBASE_API_KEY,
        'secret': COINBASE_API_SECRET,
        'password': COINBASE_API_PASSPHRASE,
        'enableRateLimit': True
    })
}
for name, ex in exchanges.items():
    ex.load_markets()

# === TELEGRAM UTILS ===
def send_telegram(text):
    try:
        requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
            data={'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML'})
    except: pass

def send_telegram_throttled(key, text, cooldown=60):
    now = time.time()
    if key in last_sent_time and (now - last_sent_time[key]) < cooldown: return
    send_telegram(text); last_sent_time[key] = now

# === BASIC UTILS (NEW) ===
def get_price(exchange, symbol):
    try:
        return exchange.fetch_ticker(symbol)['last']
    except Exception as e:
        send_telegram(f"⚠️ Price Fetch Error ({symbol}): {e}")
        return None

def calculate_position_size(balance, risk_percent=RISK_PER_TRADE_PERCENT):
    return round(balance * risk_percent / 100, 2)

def check_daily_loss():
    total_pnl = sum(data.get("pnl", 0) for data in daily_pnl.values())
    if total_pnl <= MAX_DAILY_LOSS:
        send_telegram("🚫 Max Daily Loss Hit — Bot Paused for Today.")
        return False
    return True

def update_daily_pnl(symbol, pnl):
    daily_pnl[symbol] = {
        "pnl": daily_pnl.get(symbol, {}).get("pnl", 0) + pnl,
        "trades": daily_pnl.get(symbol, {}).get("trades", 0) + 1
    }

# === Tape (real-time trades) ===
class RealTimeTape:
    def __init__(self, window_sec=5): self.window_sec, self.trades = window_sec, []
    def add_trade(self, side, size, price):
        now = time.time()
        self.trades.append({"time": now, "side": side, "size": size, "price": price})
        self.trades = [t for t in self.trades if now - t["time"] <= self.window_sec]
    def get_metrics(self):
        buys = sum(t["size"] for t in self.trades if t["side"] == "buy")
        sells = sum(t["size"] for t in self.trades if t["side"] == "sell")
        imbalance = (buys - sells) / max(buys + sells, 1e-8)
        price_change = (self.trades[-1]["price"] - self.trades[0]["price"]) if self.trades else 0
        return {"buy_volume": buys, "sell_volume": sells, "imbalance": imbalance, "price_change": price_change}

# === ANALYSIS ===
def analyze(exchange, symbol, tf='1m'):
    df = pd.DataFrame(exchange.fetch_ohlcv(symbol, tf, limit=30),
                      columns=['ts','open','high','low','close','volume'])
    df['ema21'] = ta.trend.EMAIndicator(df['close'], 21).ema_indicator()
    df['atr'] = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range()
    last = df.iloc[-1]
    return last, df

# === Regular Signals (main bot loop) ===
def build_signal(exchange_name, symbol):
    exchange = exchanges[exchange_name]
    last, df = analyze(exchange, symbol, '5m')
    entry = round(last['close'],4)
    atr = round(last['atr'],4)
    bias = "LONG" if last['close'] > last['ema21'] else "SHORT" if last['close'] < last['ema21'] else "AVOID"
    stop = round(entry - atr,4) if bias == "LONG" else round(entry + atr,4)
    target = round(entry + atr*1.5,4) if bias == "LONG" else round(entry - atr*1.5,4)
    return entry, stop, target, bias

def bot_loop():
    while True:
        try:
            for sym in symbols_to_watch:
                entry, stop, target, bias = build_signal("Coinbase", sym)
                send_telegram(f"Signal {sym}: {bias} Entry={entry} SL={stop} TP={target}")

                # === Auto-trade if enabled and valid bias ===
                if AUTO_TRADE_ENABLED and bias != "AVOID" and check_daily_loss():
                    exchange = exchanges["Coinbase"]
                    balance = exchange.fetch_balance()['total'].get('USDC', 0)
                    size_usd = max(calculate_position_size(balance), MIN_POSITION_SIZE)

                    try:
                        if bias == "LONG":
                            exchange.create_market_buy_order(sym, None, params={"cost": float(size_usd)})
                        elif bias == "SHORT":
                            exchange.create_market_sell_order(sym, None, params={"cost": float(size_usd)})

                        send_telegram(f"✅ TRADE EXECUTED: {bias} {sym} at ${entry}")
                        open_trades[sym] = {"entry": entry, "stop": stop, "target": target, "amount": size_usd, "bias": bias, "mode": "regular"}
                    except Exception as e:
                        send_telegram(f"❌ Trade Error ({sym}): {e}")

            time.sleep(CHECK_INTERVAL_SECONDS)
        except Exception as e:
            send_telegram(f"[Bot Error] {e}")
            time.sleep(60)

# === FAST BREAKDOWN MODE ===
def fast_breakdown_loop():
    while True:
        try:
            for sym in symbols_to_watch:
                exchange = exchanges["Coinbase"]
                df = pd.DataFrame(exchange.fetch_ohlcv(sym, '1m', limit=20),
                                  columns=['ts','open','high','low','close','volume'])
                last = df.iloc[-1]

                # === Conditions ===
                avg_vol = df['volume'].mean()
                vol_spike = last['volume'] >= 2 * avg_vol
                price_move = (last['high'] - last['low']) / max(last['low'], 1e-8)
                big_move = price_move >= 0.005  # ≥ 0.5% candle

                metrics = tape_snapshot.get(sym, RealTimeTape()).get_metrics()
                imbalance = metrics['imbalance']

                # === Trade Signal ===
                if vol_spike and big_move:
                    direction = "SHORT" if imbalance < -0.3 or last['close'] < last['open'] else "LONG"
                    entry = round(last['close'], 4)
                    atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range().iloc[-1]
                    atr = round(atr, 4)
                    stop = round(entry + atr, 4) if direction == "SHORT" else round(entry - atr, 4)
                    target = round(entry - atr * 1.5, 4) if direction == "SHORT" else round(entry + atr * 1.5, 4)

                    send_telegram_throttled(f"fast_{sym}", f"⚡ FAST {direction} {sym} Entry={entry} SL={stop} TP={target}")

                    if AUTO_TRADE_ENABLED and check_daily_loss():
                        balance = exchange.fetch_balance()['total'].get('USDC', 0)
                        size_usd = max(calculate_position_size(balance), MIN_POSITION_SIZE)

                        try:
                            if direction == "LONG":
                                exchange.create_market_buy_order(sym, None, params={"cost": float(size_usd)})
                            else:
                                exchange.create_market_sell_order(sym, None, params={"cost": float(size_usd)})

                            send_telegram(f"✅ FAST TRADE EXECUTED: {direction} {sym} at ${entry}")
                            open_trades[sym] = {"entry": entry, "stop": stop, "target": target, "amount": size_usd, "bias": direction, "mode": "fast"}
                        except Exception as e:
                            send_telegram(f"❌ FAST TRADE ERROR ({sym}): {e}")

            # === Check exits ===
            for sym, trade in list(open_trades.items()):
                price = get_price(exchanges["Coinbase"], sym)
                if price is None: continue
                if trade['bias'] == "LONG":
                    if price >= trade['target']:
                        send_telegram(f"✅ TP HIT {sym}: {price}")
                        exchanges["Coinbase"].create_market_sell_order(sym, None, params={"cost": float(trade['amount'])})
                        update_daily_pnl(sym, price - trade['entry'])
                        del open_trades[sym]
                    elif price <= trade['stop']:
                        send_telegram(f"🛑 SL HIT {sym}: {price}")
                        exchanges["Coinbase"].create_market_sell_order(sym, None, params={"cost": float(trade['amount'])})
                        update_daily_pnl(sym, price - trade['entry'])
                        del open_trades[sym]
                else:
                    if price <= trade['target']:
                        send_telegram(f"✅ TP HIT {sym}: {price}")
                        exchanges["Coinbase"].create_market_buy_order(sym, None, params={"cost": float(trade['amount'])})
                        update_daily_pnl(sym, trade['entry'] - price)
                        del open_trades[sym]
                    elif price >= trade['stop']:
                        send_telegram(f"🛑 SL HIT {sym}: {price}")
                        exchanges["Coinbase"].create_market_buy_order(sym, None, params={"cost": float(trade['amount'])})
                        update_daily_pnl(sym, trade['entry'] - price)
                        del open_trades[sym]

            time.sleep(FAST_BREAKDOWN_CHECK_INTERVAL)

        except Exception as e:
            send_telegram(f"[Fast Error] {e}")
            time.sleep(3)

# === FLASK ROUTES ===
@app.route('/')
def dashboard():
    return {"open_trades": open_trades, "daily_pnl": daily_pnl}

# === START ===
if __name__ == '__main__':
    send_telegram("✅ Unified Bot Started with Fast Breakdown Mode")
    threading.Thread(target=bot_loop, daemon=True).start()
    if FAST_BREAKDOWN_ENABLED:
        threading.Thread(target=fast_breakdown_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5001, use_reloader=False)