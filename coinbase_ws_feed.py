# === EDUCATIONAL CRYPTO SIGNAL BOT — Sniper Futures-Only Fast Breakdown (v6: AI News Filter + Full Telegram Control) ===

import ccxt, pandas as pd, ta, requests, os, threading, time, json, datetime, openai
from flask import Flask, request
from dotenv import load_dotenv

# === ENV Variables ===
load_dotenv()
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# === Coinbase Futures Keys ===
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY")
COINBASE_API_PASSPHRASE = os.getenv("COINBASE_API_PASSPHRASE")
with open("coinbase_private_key.pem", "r") as f:
    COINBASE_API_SECRET = f.read()

# === Config ===
symbols_to_watch = ["XRP/USD", "BTC/USD"]
AUTO_TRADE_ENABLED = True
FAST_BREAKDOWN_ENABLED = True
FAST_BREAKDOWN_CHECK_INTERVAL = 2
TRADE_AMOUNT_RISK_PERCENT = 1
MIN_CONTRACTS = 1
MAX_DAILY_LOSS = -500
PARTIAL_EXIT_AT = 0.75
TRAILING_ACTIVATE = 1.0
RISK_THROTTLE_LOSSES = 2
SPIKE_CONFIRM_CANDLES = 2
# === Test Mode Config ===
TEST_MODE = True               # Set to False after testing
TEST_SYMBOL = "XRP/USD"        # Which pair to test with

NEWS_FILTER_ENABLED = True
NEWS_API_URL = "https://cryptopanic.com/api/v1/posts/?auth_token=demo&filter=important"
NEWS_COOLDOWN = 180

# === State ===
last_sent_time, open_trades, daily_pnl = {}, {}, {}
tape_snapshot = {}
total_realized_pnl = 0.0
loss_streak = 0
risk_scale = 1.0
last_mode = None
last_news_check = 0
pause_trades_until = 0
manual_paused = False

# === Flask App ===
app = Flask(__name__)

# === Exchange Initialization ===
exchange = ccxt.coinbase({
    'apiKey': COINBASE_API_KEY,
    'secret': COINBASE_API_SECRET,
    'enableRateLimit': True,
    'options': {'createMarketBuyOrderRequiresPrice': False}
})
exchange.load_markets()

# === Telegram ===
def send_telegram(text):
    try:
        requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
                      data={'chat_id': CHAT_ID, 'text': text, 'parse_mode': 'HTML'})
    except:
        pass

def send_telegram_throttled(key, text, cooldown=60):
    now = time.time()
    if key in last_sent_time and (now - last_sent_time[key]) < cooldown:
        return
    send_telegram(text)
    last_sent_time[key] = now

# === AI News Filter ===
def classify_news_ai(title: str):
    try:
        openai.api_key = OPENAI_API_KEY
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": "You are a trading risk filter AI."},
                {"role": "user", "content": f"Is this crypto news headline critical and likely to cause volatility? '{title}'. Answer ONLY yes or no."}
            ],
            max_tokens=5
        )
        return "yes" in response['choices'][0]['message']['content'].lower()
    except:
        return False

def check_market_news():
    global pause_trades_until
    try:
        resp = requests.get(NEWS_API_URL, timeout=5)
        data = resp.json()
        headlines = [p['title'] for p in data.get('results', [])]
        for title in headlines:
            if classify_news_ai(title):
                send_telegram(f"📰 AI News Alert: {title}\n⏸ Pausing trades for {NEWS_COOLDOWN//60} min")
                pause_trades_until = time.time() + NEWS_COOLDOWN
                break
    except:
        pass

def news_allows_trade():
    return (not NEWS_FILTER_ENABLED or time.time() > pause_trades_until)

# === Utils ===
def get_price(symbol):
    try:
        return exchange.fetch_ticker(symbol)['last']
    except:
        return None

def calculate_contract_size(balance, price):
    risk_amount = balance * TRADE_AMOUNT_RISK_PERCENT / 100 * risk_scale
    return max(MIN_CONTRACTS, int(risk_amount / price))

def check_daily_loss():
    total_pnl = sum(data.get("pnl", 0) for data in daily_pnl.values())
    if total_pnl <= MAX_DAILY_LOSS:
        send_telegram("🚫 Max Daily Loss Hit — Bot Paused for Today.")
        return False
    return True

def update_daily_pnl(symbol, pnl):
    global loss_streak, risk_scale
    daily_pnl[symbol] = {
        "pnl": daily_pnl.get(symbol, {}).get("pnl", 0) + pnl,
        "trades": daily_pnl.get(symbol, {}).get("trades", 0) + 1
    }
    loss_streak = loss_streak + 1 if pnl < 0 else 0
    risk_scale = 0.5 if loss_streak >= RISK_THROTTLE_LOSSES else 1.0

def in_active_session():
    hour = datetime.datetime.utcnow().hour
    return 6 <= hour <= 20

def market_trending(df):
    adx = ta.trend.ADXIndicator(df['high'], df['low'], df['close'], 14).adx().iloc[-1]
    return adx > 20

# === Tape Metrics ===
class RealTimeTape:
    def __init__(self, window_sec=5):
        self.window_sec, self.trades = window_sec, []
    def add_trade(self, side, size, price):
        now = time.time()
        self.trades.append({"time": now, "side": side, "size": size, "price": price})
        self.trades = [t for t in self.trades if now - t["time"] <= self.window_sec]
    def get_metrics(self):
        buys = sum(t["size"] for t in self.trades if t["side"] == "buy")
        sells = sum(t["size"] for t in self.trades if t["side"] == "sell")
        imbalance = (buys - sells) / max(buys + sells, 1e-8)
        return {"imbalance": imbalance}

# === Fast Breakdown Loop ===
def fast_breakdown_loop():
    global open_trades, last_mode, last_news_check
    while True:
        try:
            if manual_paused:
                time.sleep(FAST_BREAKDOWN_CHECK_INTERVAL)
                continue

            session_active = in_active_session()
            session_text = "🌍 Peak Hours (London/NY)" if session_active else "🌙 Off-Hours"

            # Telegram status alert
            global last_mode
            if last_mode != session_active:
                last_mode = session_active
                send_telegram("🟢 Peak-Hours mode enabled" if session_active else "🟡 Off-Hours (stricter filters)")

            # News check
            if time.time() - last_news_check > 60:
                check_market_news()
                last_news_check = time.time()

            for sym in symbols_to_watch:
                df = pd.DataFrame(exchange.fetch_ohlcv(sym, '1m', limit=30),
                                  columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
                last = df.iloc[-1]
                avg_vol = df['volume'].mean()
                ema21 = ta.trend.EMAIndicator(df['close'], 21).ema_indicator().iloc[-1]
                atr = ta.volatility.AverageTrueRange(df['high'], df['low'], df['close']).average_true_range().iloc[-1]

                vol_spike = last['volume'] >= 2 * avg_vol
                big_move = (last['high'] - last['low']) / max(last['low'], 1e-8) >= (0.005 if session_active else 0.008)
                metrics = tape_snapshot.get(sym, RealTimeTape()).get_metrics()
                imbalance = metrics['imbalance']

                if vol_spike and big_move and market_trending(df) and news_allows_trade():
                    direction = "LONG" if last['close'] > ema21 and imbalance > 0 else "SHORT" if last['close'] < ema21 and imbalance < 0 else None
                    if direction:
                        entry = round(last['close'], 4)
                        stop = round(entry - atr if direction == "LONG" else entry + atr, 4)
                        target = round(entry + 1.5 * atr if direction == "LONG" else entry - 1.5 * atr, 4)

                        send_telegram_throttled(f"fast_{sym}", f"⚡ {direction} {sym} Entry={entry} SL={stop} TP={target}")

                        if AUTO_TRADE_ENABLED and check_daily_loss():
                            try:
                                balance = exchange.fetch_balance()['total'].get('USDC', 0)
                                contracts = calculate_contract_size(balance, entry)
                                if contracts < MIN_CONTRACTS:
                                    continue
                                if direction == "LONG":
                                    exchange.create_market_buy_order(sym, contracts)
                                else:
                                    exchange.create_market_sell_order(sym, contracts)
                                send_telegram(f"✅ Futures TRADE: {direction} {sym} at {entry}")
                                open_trades[sym] = {"entry": entry, "stop": stop, "target": target,
                                                    "contracts": contracts, "bias": direction,
                                                    "atr": atr, "partial_exit": False}
                            except Exception as e:
                                send_telegram(f"❌ Futures Trade Error ({sym}): {e}")

            # Exit management
            for sym, trade in list(open_trades.items()):
                price = get_price(sym)
                if not price:
                    continue

                # Partial exit
                if not trade['partial_exit']:
                    partial_level = trade['entry'] + (trade['target'] - trade['entry']) * PARTIAL_EXIT_AT
                    if (trade['bias'] == "LONG" and price >= partial_level) or (trade['bias'] == "SHORT" and price <= partial_level):
                        exit_contracts = max(1, trade['contracts'] // 2)
                        if trade['bias'] == "LONG":
                            exchange.create_market_sell_order(sym, exit_contracts)
                        else:
                            exchange.create_market_buy_order(sym, exit_contracts)
                        trade['contracts'] -= exit_contracts
                        trade['partial_exit'] = True
                        send_telegram(f"🔹 Partial Exit {sym} at {price}")

                # Final exit
                if trade['bias'] == "LONG" and (price >= trade['target'] or price <= trade['stop']):
                    exchange.create_market_sell_order(sym, trade['contracts'])
                    send_telegram(f"✅ Exit LONG {sym} at {price}")
                    update_daily_pnl(sym, price - trade['entry'])
                    del open_trades[sym]
                elif trade['bias'] == "SHORT" and (price <= trade['target'] or price >= trade['stop']):
                    exchange.create_market_buy_order(sym, trade['contracts'])
                    send_telegram(f"✅ Exit SHORT {sym} at {price}")
                    update_daily_pnl(sym, trade['entry'] - price)
                    del open_trades[sym]

            time.sleep(FAST_BREAKDOWN_CHECK_INTERVAL)

        except Exception as e:
            send_telegram(f"[Fast Error] {e}")
            time.sleep(3)

# === Telegram Commands ===
@app.route(f"/{TOKEN}", methods=["POST"])
def telegram_webhook():
    global manual_paused
    data = request.json
    if "message" in data and "text" in data["message"]:
        chat_id = data["message"]["chat"]["id"]
        text = data["message"]["text"].strip().lower()

        if text == "/status":
            status_msg = f"📊 Bot Status:\n"
            status_msg += f"🕒 Session: {'Peak-Hours' if in_active_session() else 'Off-Hours'}\n"
            status_msg += f"⏸ Paused: {'YES' if manual_paused or time.time() < pause_trades_until else 'NO'}\n"
            status_msg += f"💰 Daily PnL: {sum(d['pnl'] for d in daily_pnl.values()):.2f}\n"
            status_msg += f"📈 Open Trades: {len(open_trades)}\n"
            for sym, t in open_trades.items():
                status_msg += f"- {sym} {t['bias']} at {t['entry']} TP={t['target']} SL={t['stop']}\n"
            requests.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage', data={'chat_id': chat_id, 'text': status_msg})

        elif text == "/pause":
            manual_paused = True
            send_telegram("⏸ Bot manually paused. No new trades will be taken.")

        elif text == "/resume":
            manual_paused = False
            send_telegram("▶️ Bot manually resumed.")

        elif text == "/closeall":
            for sym, trade in list(open_trades.items()):
                try:
                    if trade['bias'] == "LONG":
                        exchange.create_market_sell_order(sym, trade['contracts'])
                    else:
                        exchange.create_market_buy_order(sym, trade['contracts'])
                    send_telegram(f"❌ Force closed {sym} at market price.")
                    del open_trades[sym]
                except Exception as e:
                    send_telegram(f"⚠️ Close error {sym}: {e}")

    return "ok"

# === Start Bot ===
# === Start Bot ===
if __name__ == '__main__':
    send_telegram("✅ Sniper Bot Started (v6: AI News Filter + Full Telegram Control)")

    # === Test Mode: force small trade ===
    if TEST_MODE:
        send_telegram("🔍 TEST MODE is ON: attempting test trade...")
        try:
            balance = exchange.fetch_balance()['total'].get('USDC', 0)
            price = get_price(TEST_SYMBOL)

            if not price:
                raise ValueError(f"Could not fetch price for {TEST_SYMBOL}")

            contracts = max(1, calculate_contract_size(balance, price))
            send_telegram(f"🛠 Preparing TEST trade: {contracts} contracts at {price}")

            exchange.create_market_buy_order(TEST_SYMBOL, contracts)
            send_telegram(f"🚀 TEST TRADE triggered: BUY {TEST_SYMBOL} {contracts} contracts at {price}")

            open_trades[TEST_SYMBOL] = {
                "entry": price,
                "stop": price * 0.99,
                "target": price * 1.01,
                "contracts": contracts,
                "bias": "LONG",
                "atr": 0.01,
                "partial_exit": False
            }
        except Exception as e:
            send_telegram(f"❌ TEST TRADE Error: {str(e)}")

    # === Always start the bot ===
    threading.Thread(target=fast_breakdown_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5001, use_reloader=False)