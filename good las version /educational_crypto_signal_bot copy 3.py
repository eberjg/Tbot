# === EDUCATIONAL CRYPTO SIGNAL BOT — Full Auto-Managed — Spot + Futures Support ===
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

SCALP_RISK_MULTIPLIER = 0.5   # ✅ Scalp trades risk half size
SCALP_TP_MULTIPLIER = 0.75    # ✅ Scalp TP set closer (0.75x target)
CHECK_INTERVAL_SECONDS = 900  # ✅ Bot scan interval (seconds)

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

def calculate_position_size(balance, risk_percent=RISK_PER_TRADE_PERCENT):
    return round(balance * risk_percent / 100, 2)

def check_daily_loss():
    total_pnl = sum(daily_pnl.values())
    if total_pnl <= MAX_DAILY_LOSS:
        send_telegram(f"🚫 Max Daily Loss Hit: ${total_pnl}. Bot will pause trading for today.")
        return False
    return True

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
    tf_list = ['1m', '5m', '15m', '1h']
    weights = {'1m': 1, '5m': 2, '15m': 3, '1h': 4}

    weighted_score = 0
    recent_drop = False

    for tf in tf_list:
        s = analyze(exchange, symbol, tf)
        weighted_score += s['score'] * weights[tf]
        if tf == '5m':
            entry = s['price']
            atr = s['atr']
            stop_loss = round(entry - atr, 2)
            target = round(entry + atr * 1.5, 2)
            macd_5m = s['macd']
            ema21_5m = s['ema21']
            price_5m = s['price']
        if s['price_drop']:
            recent_drop = True

    # === Trend Filter Logic ===
    if price_5m < ema21_5m and macd_5m < 0:
        bias = "SHORT"
        stop_loss = round(entry + atr, 2)   # SL above entry for short
        target = round(entry - atr * 1.5, 2)
    elif price_5m > ema21_5m and macd_5m > 0:
        bias = "LONG"
        stop_loss = round(entry - atr, 2)
        target = round(entry + atr * 1.5, 2)
    else:
        bias = "AVOID"

    action = (
        "🚀 LONG Opportunity" if bias == "LONG" else
        "🔻 SHORT Opportunity" if bias == "SHORT" else
        "🚫 NO TRADE — Mixed trend."
    )

    msg = (
        f"Signal for {symbol} ({exchange_name})\n"
        f"-----------------------------\n"
        f"Bias: {bias}\n"
        f"Entry: ${entry}, SL: ${stop_loss}, TP: ${target}\n"
        f"{action}"
    )

    return entry, stop_loss, target, msg, bias

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

    # ✅ If price hits target — let the placed TP Limit Order handle it (we still record it here)
    if price >= target:
        send_telegram(f"✅ SPOT TARGET HIT — {symbol} at ${price} (Entry: ${entry})\nPnL: ${round(pnl, 2)}")
        log_trade(symbol, entry, stop, target, price, round(pnl, 2), mode, "target")
        del open_trades[symbol]
        daily_pnl[symbol] = daily_pnl.get(symbol, 0) + pnl
        return

    # ✅ If price hits stop — manually sell at market
    if price <= stop:
        send_telegram(f"🛑 SPOT STOP LOSS HIT — {symbol} at ${price} (Entry: ${entry})\nPnL: ${round(pnl, 2)}")
        try:
            exchange.create_order(
                symbol=symbol,
                type='market',
                side='sell',
                amount=None,
                params={"cost": float(amount_usd)}
            )
            send_telegram(f"✅ SPOT SL SELL EXECUTED — {symbol} at ${price}")
        except Exception as e:
            send_telegram(f"❌ SPOT SL SELL ERROR: {e}")

        log_trade(symbol, entry, stop, target, price, round(pnl, 2), mode, "stopped")
        del open_trades[symbol]
        daily_pnl[symbol] = daily_pnl.get(symbol, 0) + pnl

def check_futures_exit(exchange, symbol, price, send_telegram_func):
    trade = open_trades.get(symbol)
    if not trade or trade.get('mode') != 'futures':
        return

    entry = trade['entry']
    stop = trade['stop']
    target = trade['target']
    amount = int(trade['amount'])  # Assuming you recorded contracts amount here
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

def daily_summary():
    summary = "\n📊 Daily PnL Summary:\n"
    for s, p in daily_pnl.items():
        summary += f"{s}: ${round(p, 2)}\n"
    send_telegram(summary)
    daily_pnl.clear()
    threading.Timer(86400, daily_summary).start()

def check_all_exits():
    for sym in open_trades.copy():
        price = get_price(exchanges["Coinbase"], resolve_symbol("Coinbase", sym))
        check_trade_exit(sym, price)  # Paper tracking (PnL only)
        check_spot_exit(exchanges["Coinbase"], resolve_symbol("Coinbase", sym), price)  # Spot monitoring
        check_futures_exit(exchanges["Coinbase"], resolve_symbol("Coinbase", sym), price, send_telegram)  # ✅ Futures TP/SL monitor


CHECK_INTERVAL_SECONDS = 900  # ✅ Adjustable check interval

def bot_loop():
    while True:
        try:
            if not check_daily_loss():
                print("[INFO] Max daily loss hit, pausing bot until next check.")
                time.sleep(600)
                continue

            check_all_exits()

            for sym in ['XRP/USD']:
                for name, exchange in exchanges.items():
                    resolved = resolve_symbol(name, sym)
                    price = get_price(exchange, resolved)

                    entry, stop, target, msg, bias = build_signal(name, resolved)
                    send_telegram(msg)

                    if AUTO_TRADE_ENABLED and check_daily_loss():
                        cooldown_ok = sym not in last_trade_time or (
                            datetime.now(timezone.utc) - last_trade_time[sym]
                        ) >= timedelta(minutes=TRADE_COOLDOWN_MINUTES)

                        if not cooldown_ok or bias not in ["LONG", "SHORT", "SCALP"]:
                            continue

                        account_balance = exchange.fetch_balance()['total'].get('USDC', 0)
                        position_size = calculate_position_size(account_balance)

                        if position_size <= 0:
                            send_telegram(f"🚫 Skipped Trade: Position size too small (${position_size}).")
                            continue

                        # ✅ Apply SCALP Risk Adjustments
                        if bias == "SCALP":
                            position_size = round(position_size * SCALP_RISK_MULTIPLIER, 2)
                            target = round(entry + (target - entry) * SCALP_TP_MULTIPLIER, 2)

                        if name == "Coinbase" and LIVE_FUTURES_TRADE_ENABLED:
                            if bias in ["LONG", "SCALP"]:
                                fut_size = futures_trade(exchange, resolved, position_size, send_telegram, stop=stop, target=target)
                                if fut_size:
                                    record_trade(sym, entry, stop, target, amount=position_size, mode="futures")

                            elif bias == "SHORT":
                                short_size = futures_short_trade(exchange, resolved, position_size, send_telegram, stop=stop, target=target)
                                if short_size:
                                    record_trade(sym, entry, stop, target, amount=position_size, mode="short")

                        if name == "Coinbase" and LIVE_SPOT_TRADE_ENABLED and bias in ["LONG", "SCALP"]:
                            spot_size = spot_trade(exchange, resolved, position_size, send_telegram, target_price=target)
                            if spot_size:
                                record_trade(sym, entry, stop, target, amount=position_size, mode="spot")

            time.sleep(CHECK_INTERVAL_SECONDS)

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