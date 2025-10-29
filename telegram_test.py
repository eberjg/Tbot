import requests

TOKEN = "7426906968:AAGhrtj3DL4Bbstt6ThYFjMD0t1_5YqhAl4"
CHAT_ID = "5855104096"
url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
payload = {
    "chat_id": CHAT_ID,
    "text": "✅ Test message from Tbot",
    "parse_mode": "MarkdownV2"
}
r = requests.post(url, data=payload)
print(r.status_code, r.text)