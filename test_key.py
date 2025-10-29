# test_key.py
from cryptography.hazmat.primitives import serialization

with open("coinbase_private_key.pem", "rb") as f:
    key_data = f.read()
    key = serialization.load_pem_private_key(key_data, password=None)
    print("✅ Key is valid!")