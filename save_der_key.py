import base64

# Your values
key_id = "49a57ced-9688-4bd7-8825-b93073239a6d"
b64_key = "etAGdhXXCYjvQJ3xbCH0H1GG3ndb0zEgCa5vIijAFAHp0B7tq56tQonhPHz5dRYmPCnVj/01hvSepo2jgjj/RQ=="

# Decode base64
raw_der = base64.b64decode(b64_key)

# Save to .der file
with open("coinbase_private_key.der", "wb") as f:
    f.write(raw_der)

print("✅ DER key saved to coinbase_private_key.der")
