import os
import time
import base64
import requests
import hashlib
from dotenv import load_dotenv
from ecdsa import SigningKey

load_dotenv()

COINBASE_API_KEY = os.getenv("COINBASE_API_KEY")
PEM_PATH = os.getenv("COINBASE_API_SECRET_PATH")

def build_headers(method, path, body=""):
    timestamp = str(int(time.time()))
    message = f"{timestamp}{method.upper()}{path}{body}"

    with open(PEM_PATH, "r") as f:
        private_key = SigningKey.from_pem(f.read())

    message_hash = hashlib.sha256(message.encode()).digest()
    signature = private_key.sign_digest(message_hash)
    signature_b64 = base64.b64encode(signature).decode()

    print("\n[DEBUG] ECDSA Signature:")
    print("Timestamp:", timestamp)
    print("Message to sign:", message)
    print("Signature (base64):", signature_b64)

    return {
        "CB-ACCESS-KEY": COINBASE_API_KEY,
        "CB-ACCESS-SIGN": signature_b64,
        "CB-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json"
    }

# Test GET request
path = "/api/v3/brokerage/products/XRP-PERPUSD"
url = f"https://api.coinbase.com{path}"

headers = build_headers("GET", path)
response = requests.get(url, headers=headers)

print(f"Status: {response.status_code}")
print("Response:", response.text)
