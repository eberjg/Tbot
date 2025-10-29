# tape_snapshot.py
from real_time_tape import RealTimeTape

# Global shared store across modules
tape_snapshot = {
    "Coinbase_BTC/USD": RealTimeTape(window_sec=5)
}