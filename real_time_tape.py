# === real_time_tape.py ===
# Collects and aggregates real-time trades from Coinbase WebSocket feed
# Outputs momentum, volume imbalance, and rolling trade pressure

import time
import threading
from collections import deque, defaultdict

class RealTimeTape:
    def __init__(self, window_sec=5):
        self.trades = deque()
        self.window_sec = window_sec
        self.lock = threading.Lock()
        self.last_metrics = {}

    def add_trade(self, side, price, size):
        with self.lock:
            now = time.time()
            self.trades.append({"side": side, "price": float(price), "size": float(size), "time": now})
            self._clean_old_trades(now)

    def _clean_old_trades(self, now):
        while self.trades and (now - self.trades[0]["time"]) > self.window_sec:
            self.trades.popleft()

    def get_metrics(self):
        with self.lock:
            now = time.time()
            self._clean_old_trades(now)
            buys = [t for t in self.trades if t["side"] == "buy"]
            sells = [t for t in self.trades if t["side"] == "sell"]

            buy_volume = sum(t["size"] for t in buys)
            sell_volume = sum(t["size"] for t in sells)
            total_volume = buy_volume + sell_volume

            price_change = 0
            if len(self.trades) >= 2:
                price_change = self.trades[-1]["price"] - self.trades[0]["price"]

            imbalance = 0
            if total_volume > 0:
                imbalance = (buy_volume - sell_volume) / total_volume

            self.last_metrics = {
                "buy_volume": round(buy_volume, 4),
                "sell_volume": round(sell_volume, 4),
                "imbalance": round(imbalance, 4),
                "price_change": round(price_change, 2),
                "trade_count": len(self.trades)
            }

            return self.last_metrics

    def print_debug(self):
        metrics = self.get_metrics()
        print("\n[Real-Time Tape Metrics — {}s]".format(self.window_sec))
        for k, v in metrics.items():
            print(f"{k}: {v}")