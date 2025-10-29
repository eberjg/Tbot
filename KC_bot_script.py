
# === FINAL CRYPTO SIGNAL BOT with Dual Exchange Support (Coinbase & Kraken) ===
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
from dotenv import load_dotenv
import feedparser

# === LOAD ENVIRONMENT VARIABLES ===
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Coinbase credentials
COINBASE_API_KEY = os.getenv("COINBASE_API_KEY")
COINBASE_API_SECRET = os.getenv("COINBASE_API_SECRET")
COINBASE_API_PASSPHRASE = os.getenv("COINBASE_API_PASSPHRASE")

# Kraken credentials
KRAKEN_API_KEY = os.getenv("KRAKEN_API_KEY")
KRAKEN_API_SECRET = os.getenv("KRAKEN_API_SECRET")

# === FLASK APP ===
app = Flask(__name__)

# === EXCHANGE SETUP ===
exchanges = {
    'coinbase': ccxt.coinbase({
        'apiKey': COINBASE_API_KEY,
        'secret': COINBASE_API_SECRET,
        'password': COINBASE_API_PASSPHRASE,
        'enableRateLimit': True,
    }),
    'kraken': ccxt.kraken({
        'apiKey': KRAKEN_API_KEY,
        'secret': KRAKEN_API_SECRET,
        'enableRateLimit': True,
    })
}

# [Remaining core logic will use `exchange` as parameter]
