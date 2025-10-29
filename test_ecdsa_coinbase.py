import os
from dotenv import load_dotenv
from coinbase_futures import get_futures_price, get_usd_balance, futures_market_buy

load_dotenv()

symbol = "XRP-USD"

print("🔍 Testing Coinbase Advanced ECDSA Integration...")
print("Fetching price...")

price = get_futures_price(symbol)
if not price:
    print("❌ Failed to get price.")
else:
    print(f"✅ Current {symbol} price: {price}")

print("Fetching balance...")
try:
    balance = get_usd_balance()
    print(f"✅ USD Balance: ${balance:.2f}")
except Exception as e:
    print("❌ Balance fetch error:", str(e))

if price and balance > 10:
    print("Placing test market BUY order (small size)...")
    try:
        response = futures_market_buy(symbol, 10)  # Try $10 test buy
        print("✅ Trade executed:", response)
    except Exception as e:
        print("❌ Trade failed:", str(e))
else:
    print("⚠️ Not executing trade due to missing price or low balance.")