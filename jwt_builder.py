import os, time, json, requests, jwt, re
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend

# === Load env vars ===
load_dotenv(dotenv_path=".env")

COINBASE_API_KEY_ID = os.getenv("COINBASE_API_KEY_ID")
COINBASE_API_SECRET_PATH = os.getenv("COINBASE_API_SECRET_PATH")

def _validate_env_or_die():
    pattern = r"^organizations/[0-9a-fA-F-]{36}/apiKeys/[0-9a-fA-F-]{36}$"
    if not COINBASE_API_KEY_ID or not re.match(pattern, COINBASE_API_KEY_ID):
        raise EnvironmentError(
            "❌ COINBASE_API_KEY_ID must be the FULL resource path like:\n"
            "   organizations/<org-uuid>/apiKeys/<key-uuid>\n"
            f"   Current: {COINBASE_API_KEY_ID!r}"
        )
    if not COINBASE_API_SECRET_PATH:
        raise EnvironmentError("❌ COINBASE_API_SECRET_PATH missing from .env")
    if not os.path.exists(COINBASE_API_SECRET_PATH):
        raise FileNotFoundError(f"❌ Private key file not found: {COINBASE_API_SECRET_PATH}")

_validate_env_or_die()

# === Load ES256 private key ===
try:
    with open(COINBASE_API_SECRET_PATH, "rb") as f:
        COINBASE_API_SECRET = serialization.load_pem_private_key(
            f.read(), password=None, backend=default_backend()
        )
except Exception as e:
    raise ValueError(f"❌ Failed to parse private key from PEM file: {e}")