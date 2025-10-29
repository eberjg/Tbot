import os
import time
import base64
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from dotenv import load_dotenv

# Load env
load_dotenv()

# Load vars
API_KEY = os.getenv("COINBASE_API_KEY")
PEM_PATH = os.getenv("COINBASE_API_SECRET_PATH")
PASSPHRASE = os.getenv("COINBASE_API_PASSPHRASE", "dummy")

# Load PEM
with open(PEM_PATH, "rb") as f:
    private_key = load_pem_private_key(f.read(), password=None)

# Coinbase API
method = "GET"
path = "/api/v3/brokerage/products/XRP-PERPUSD"
timestamp = str(int(time.time()))
body = ""

message = timestamp + method + path + body

# Sign the message
signature = private_key.sign(
    message.encode("utf-8"),
    ec.ECDSA(hashes.SHA256())
)
signature_b64 = base64.b64encode(signature).decode()

# Headers
headers = {
    "CB-ACCESS-KEY": API_KEY,
    "CB-ACCESS-SIGN": signature_b64,
    "CB-ACCESS-TIMESTAMP": timestamp,
    "CB-ACCESS-PASSPHRASE": PASSPHRASE,
    "Content-Type": "application/json",
}

# Make the request
url = "https://api.coinbase.com" + path
response = requests.get(url, headers=headers)

print("Status:", response.status_code)
print("Response:", response.text)