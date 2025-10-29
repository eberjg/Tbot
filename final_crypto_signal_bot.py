# === ADVANCED MULTI-TIMEFRAME SIGNAL BOT ===
import ccxt
import pandas as pd
import ta
import requests
import re
import os
import threading
import time
from datetime import datetime
from flask import Flask, request

# === FLASK APP ===
app = Flask(__name__)

# === TELEGRAM ===
TOKEN = '7426906968:AAGhrtj3DL4Bbstt6ThYFjMD0t1_5YqhAl4'
CHAT_ID = '5855104096'

# === ESCAPE MARKDOWN ===
def escape_md(text):
    escape_chars = r'_*[]()~`>#+=|{}.!-'
    return re.sub(r'([{}])'.format(re.escape(escape_chars)), r'\\\1', str(text))

# === TELEGRAM ALERT ===
def send_telegram(text, token, chat_id):
    text = text[:4000] + "\n\n📎 Message trimmed." if len(text) > 4000 else text
    text = text.encode('utf-16', 'surrogatepass').decode('utf-16', 'ignore')
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    payload = {'chat_id': chat_id, 'text': text, 'parse_mode': 'MarkdownV2'}
    response = requests.post(url, data=payload)
    print("Telegram Response:", response.status_code, response.text)

# === ANALYSIS ===
def analyze(symbol, tf):
    df = pd.DataFrame(exchange.fetch_ohlcv(symbol, tf), columns=['ts','open','high','low','close','volume'])
    df['rsi'] = ta.momentum.RSIIndicator(df['close']).rsi()
    macd = ta.trend.MACD(df['close'])
    df['macd'] = macd.macd()
    df['ema21'] = ta.trend.EMAIndicator(df['close'], 21).ema_indicator()
    df['adx'] = ta.trend.ADXIndicator(df['high'], df['low'], df['close']).adx()
    
    last = df.iloc[-1]
    score = sum([
        last['close'] > last['ema21'],
        last['rsi'] > 50,
        last['macd'] > 0,
        last['adx'] > 20
    ])
    return {
        'symbol': symbol,
        'tf': tf,
        'score': score,
        'price': round(last['close'], 2),
        'rsi': round(last['rsi'], 1),
        'macd': round(last['macd'], 2),
        'adx': round(last['adx'], 1),
        'ema21': round(last['ema21'], 2),
        'alignment': '✅' if score == 4 else '⚠️',
        'side': 'LONG' if score >= 3 else 'WAIT'
    }

# === SIGNAL BUILDER ===
def build_signal(symbol):
    tf_list = ['15m', '1h', '6h']
    total_score = 0
    details = ""
    
    for tf in tf_list:
        s = analyze(symbol, tf)
        total_score += s['score']
        details += f"\n📊 *{escape_md(symbol)} {tf}* — {s['alignment']}\n• Price: ${s['price']} | RSI: {s['rsi']} | MACD: {s['macd']} | ADX: {s['adx']} | EMA21: ${s['ema21']} | Score: {s['score']}/4\n"
    
    action = '🚀 *STRONG LONG*' if total_score == 12 else '🔍 *CAUTIOUS*' if total_score >= 9 else '⚠️ *WAIT*'
    msg = f"*Multi-Timeframe Signal ({symbol})*\n━━━━━━━━━━━━━━━━━━━━\nTotal Score: {total_score}/12 → {action}\n{details}"
    return escape_md(msg)

# === FLASK ENDPOINT (OPTIONAL) ===
@app.route('/', methods=['POST'])
def handle():
    data = request.json
    if 'message' in data and 'text' in data['message']:
        chat_id = data['message']['chat']['id']
        if data['message']['text'] == '/signal':
            result = build_signal('BTC/USD')
            send_telegram(result, TOKEN, chat_id)
    return {'ok': True}

# === MAIN BOT LOOP ===
def bot_loop():
    while True:
        try:
            for sym in ['BTC/USD', 'XRP/USD']:
                msg = build_signal(sym)
                send_telegram(msg, TOKEN, CHAT_ID)
            print("✅ Cycle completed. Waiting 15 minutes...\n")
            time.sleep(900)
        except Exception as e:
            print(f"[ERROR] {e}")
            time.sleep(60)

# === MAIN ===
if __name__ == '__main__':
    exchange = ccxt.coinbase()
    threading.Thread(target=bot_loop).start()
    app.run(host='0.0.0.0', port=5001)