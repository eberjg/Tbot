# coinbase_ws_feeder.py
from coinbase_ws_feed import CoinbaseWebSocket
from real_time_tape import RealTimeTape
from tape_snapshot import tape_snapshot

def start_coinbase_tape_feeds(symbols):
    print("[INFO] Starting Coinbase WebSocket feeds for real-time tape...")
    for sym in symbols:
        ws_symbol = sym.replace("/", "-")
        tape_key = f"Coinbase_{sym}"

        # Create tape if not already present
        if tape_key not in tape_snapshot:
            tape_snapshot[tape_key] = RealTimeTape(window_sec=5)

        ws = CoinbaseWebSocket(symbol=ws_symbol)
        ws.start()