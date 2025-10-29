import requests

TELEGRAM_TOKEN = "7426906968:AAGhrtj3DL4Bbstt6ThYFjMD0t1_5YqhAl4"

def get_chat_id():
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    response = requests.get(url)
    data = response.json()
    print(data)  # Look for "chat":{"id":...} in the output

get_chat_id()