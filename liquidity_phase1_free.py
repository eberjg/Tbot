# Phase 1 Liquidity Collector (free, no ccxt.pro)
# Binance Futures (BTCUSDT, XRPUSDT) + Coinbase Spot (BTC-USD, XRP-USD)
# Streams order book and trades, prints best bid/ask, spread, and cum depth ±10 bps.

import asyncio
import math
import time
from collections import deque
from typing import Dict, List, Tuple

import orjson as json
import websockets
import aiohttp
import os  # ← add this
# -----------------------
# -----------------------
# Config
# -----------------------
BINANCE_F_SYMBOLS = ["btcusdt", "ethusdt", "xrpusdt"]   # futures, USDT‑margined
COINBASE_SPOT     = ["BTC-USD", "ETH-USD", "XRP-USD"]   # spot, Advanced Trade WS

PRINT_EVERY_SEC = 1.0
LIQ_SNAPSHOT_PATH = os.getenv("LIQ_SNAPSHOT", "liquidity_snapshot.json")
# -----------------------
# -----------------------
# Simple order book model
# -----------------------
class L2Book:
    def __init__(self, symbol: str, price_tick: float = 0.01):
        self.symbol = symbol
        self.bids: Dict[float, float] = {}  # price -> size
        self.asks: Dict[float, float] = {}
        self.price_tick = price_tick
        self.last_update_ts = 0

    def _clean(self, side: Dict[float, float], price: float, size: float):
        if size <= 0:
            side.pop(price, None)
        else:
            side[price] = size

    def update_binance_levels(self, bids: List[List[str]], asks: List[List[str]]):
        # bids/asks: [[price_str, qty_str], ...]
        for p_str, q_str in bids:
            p = float(p_str); q = float(q_str)
            self._clean(self.bids, p, q)
        for p_str, q_str in asks:
            p = float(p_str); q = float(q_str)
            self._clean(self.asks, p, q)
        self.last_update_ts = time.time()

    def snapshot_coinbase(self, bids: List[List[str]], asks: List[List[str]]):
        self.bids = {float(p): float(q) for p, q in bids}
        self.asks = {float(p): float(q) for p, q in asks}
        self.last_update_ts = time.time()

    def update_coinbase(self, changes: List[List[str]]):
        # changes: [["buy"/"sell", price, size], ...]
        for side, p_str, q_str in changes:
            p = float(p_str); q = float(q_str)
            if side == "buy":
                self._clean(self.bids, p, float(q))
            else:
                self._clean(self.asks, p, float(q))
        self.last_update_ts = time.time()

    def best_bid(self) -> Tuple[float, float]:
        if not self.bids: return (0.0, 0.0)
        p = max(self.bids.keys()); return (p, self.bids[p])

    def best_ask(self) -> Tuple[float, float]:
        if not self.asks: return (0.0, 0.0)
        p = min(self.asks.keys()); return (p, self.asks[p])

    def spread(self) -> float:
        b, _ = self.best_bid(); a, _ = self.best_ask()
        if b == 0 or a == 0: return 0.0
        return a - b

    def cum_depth_bps(self, side: str, mid: float, bps: float = 10.0) -> float:
        """Cumulative size within ±bps of mid on a given side.
           side='bid' sums bids from (mid*(1- bps/10000))..mid;
           side='ask' sums asks from mid..(mid*(1+ bps/10000))."""
        if mid <= 0: return 0.0
        if side == "bid":
            lo = mid * (1.0 - bps / 10000.0)
            return sum(q for p, q in self.bids.items() if lo <= p <= mid)
        else:
            hi = mid * (1.0 + bps / 10000.0)
            return sum(q for p, q in self.asks.items() if mid <= p <= hi)

# -----------------------
# Binance Futures WS
# -----------------------
async def binance_futures_loop(books: Dict[str, L2Book]):
    # stream depth@100ms and aggTrade
    # docs: wss://fstream.binance.com/stream?streams=btcusdt@depth@100ms
    streams = []
    for s in BINANCE_F_SYMBOLS:
        streams += [f"{s}@depth@100ms", f"{s}@aggTrade"]
    url = "wss://fstream.binance.com/stream?streams=" + "/".join(streams)

    async for ws in websockets.connect(url, ping_interval=20, ping_timeout=20):
        try:
            async for raw in ws:
                msg = json.loads(raw)
                data = msg.get("data", {})
                stream = msg.get("stream", "")

                if stream.endswith("@depth@100ms"):
                    sym = stream.split("@")[0].upper()
                    book = books.setdefault(f"BINANCEFUT:{sym}", L2Book(symbol=f"BINANCEFUT:{sym}"))
                    bids = data.get("b", [])
                    asks = data.get("a", [])
                    book.update_binance_levels(bids, asks)

                elif stream.endswith("@aggTrade"):
                    # you could consume trades here if needed
                    pass
        except Exception:
            await asyncio.sleep(1.5)
            continue  # reconnect

# -----------------------
# Coinbase Spot WS (Advanced Trade)
# -----------------------
async def coinbase_spot_loop(books: Dict[str, L2Book]):
    # docs: wss://advanced-trade-ws.coinbase.com
    url = "wss://advanced-trade-ws.coinbase.com"
    prods = COINBASE_SPOT  # e.g., ["BTC-USD","XRP-USD"]

    sub = {
        "type": "subscribe",
        "product_ids": prods,
        "channel": "level2",  # we’ll subscribe to level2 (includes snapshots + updates)
    }
    # We’ll open two connections: level2 (order book) and ticker (optional)
    async for ws in websockets.connect(url, ping_interval=20, ping_timeout=20):
        try:
            await ws.send(json.dumps(sub))
            # Optional second subscription for ticker:
            await ws.send(json.dumps({
                "type": "subscribe", "product_ids": prods, "channel": "ticker"
            }))

            async for raw in ws:
                msg = json.loads(raw)
                typ = msg.get("type")
                ch  = msg.get("channel")
                pid = msg.get("product_id")

                if typ == "snapshot" and ch == "level2":
                    book = books.setdefault(f"COINBASE:{pid}", L2Book(symbol=f"COINBASE:{pid}"))
                    book.snapshot_coinbase(msg.get("bids", []), msg.get("asks", []))

                elif typ == "l2update" and ch == "level2":
                    book = books.setdefault(f"COINBASE:{pid}", L2Book(symbol=f"COINBASE:{pid}"))
                    book.update_coinbase(msg.get("changes", []))

                elif typ == "ticker" and ch == "ticker":
                    # best bid/ask are also present here sometimes; we rely on level2
                    pass

        except Exception:
            await asyncio.sleep(1.5)
            continue  # reconnect

# -----------------------
# Printer task
# -----------------------
# ========= Printer task (writes liquidity_snapshot.json) =========
def _mid(book: L2Book) -> float:
    b, _ = book.best_bid(); a, _ = book.best_ask()
    return 0.0 if (b == 0 or a == 0) else 0.5 * (a + b)

def _bps(from_px: float, to_px: float) -> float:
    if from_px <= 0: return 0.0
    return (to_px / from_px - 1.0) * 10000.0

async def printer(books: Dict[str, L2Book]):
    path = os.getenv("LIQ_SNAPSHOT", "liquidity_snapshot.json")
    print(f"[printer] writing snapshot to: {os.path.abspath(path)}", flush=True)
    while True:
        try:
            lines = []
            snapshot = {"ts": int(time.time()), "symbols": {}}

            for name, book in sorted(books.items()):
                bb, bbq = book.best_bid(); ba, baq = book.best_ask()
                if bb == 0 or ba == 0:
                    continue

                mid   = _mid(book)
                spr   = book.spread()
                bid10 = book.cum_depth_bps("bid", mid, 10.0)
                ask10 = book.cum_depth_bps("ask", mid, 10.0)
                total = bid10 + ask10
                imb   = 0.0 if total <= 0 else (bid10 - ask10) / total   # -1..+1

                # -------- nearest "walls" inside ±0.30% --------
                win_pct = 0.30
                lo = mid * (1 - win_pct/100.0)
                hi = mid * (1 + win_pct/100.0)

                # largest bid level in window, and largest ask level in window
                nb_price, nb_size = 0.0, 0.0
                for p, q in book.bids.items():
                    if lo <= p <= mid and q > nb_size:
                        nb_price, nb_size = p, q

                na_price, na_size = 0.0, 0.0
                for p, q in book.asks.items():
                    if mid <= p <= hi and q > na_size:
                        na_price, na_size = p, q

                # distances to those walls (in bps from mid)
                dist_bid_bps = abs(_bps(mid, nb_price)) if nb_price else 0.0
                dist_ask_bps = abs(_bps(mid, na_price)) if na_price else 0.0

                # a very simple "magnet" guess:
                #   if ask wall is closer &/or size-dominant and OB is ask-heavy -> magnet_up == ask wall
                #   if bid wall is closer &/or size-dominant and OB is bid-heavy -> magnet_down == bid wall
                ask_weight = (na_size or 0.0) * (1.0 + max(-imb, 0.0)) / max(dist_ask_bps or 1e-9, 1e-9)
                bid_weight = (nb_size or 0.0) * (1.0 + max(+imb, 0.0)) / max(dist_bid_bps or 1e-9, 1e-9)
                magnet_side = "ASK" if ask_weight > bid_weight else "BID"
                magnet_price = na_price if magnet_side == "ASK" else nb_price
                # crude confidence 0..1
                raw_conf = abs(ask_weight - bid_weight) / max(ask_weight + bid_weight, 1e-9)
                confidence = max(0.0, min(1.0, raw_conf))

                venue = "BINANCEFUT" if name.startswith("BINANCEFUT:") else ("COINBASE" if name.startswith("COINBASE:") else "")
                snapshot["symbols"][name] = {
                    "venue": venue,
                    "mid": float(mid),
                    "spread": float(spr),
                    "imbalance": float(imb),
                    "cum_bid10": float(bid10),
                    "cum_ask10": float(ask10),

                    "nearest_bid_wall_price": float(nb_price),
                    "nearest_bid_wall_size":  float(nb_size),
                    "nearest_bid_wall_dist_bps": float(dist_bid_bps),

                    "nearest_ask_wall_price": float(na_price),
                    "nearest_ask_wall_size":  float(na_size),
                    "nearest_ask_wall_dist_bps": float(dist_ask_bps),

                    "magnet_side": magnet_side,       # "ASK" or "BID"
                    "magnet_price": float(magnet_price) if magnet_price else 0.0,
                    "magnet_confidence": float(confidence),  # 0..1
                }

                # nice console line
                lines.append(
                    f"{name:<18} bbid={bb:.6f}({bbq:.4f})  bask={ba:.6f}({baq:.4f})  "
                    f"spr={spr:.6f}  mid={mid:.6f}  "
                    f"±10bps bid={bid10:.0f} ask={ask10:.0f}  imb={imb:+.2f}  "
                    f"walls: bid {nb_price:.2f}({nb_size:.0f})/{dist_bid_bps:.1f}bps  "
                    f"ask {na_price:.2f}({na_size:.0f})/{dist_ask_bps:.1f}bps  "
                    f"magnet→{magnet_side}@{magnet_price:.2f}({confidence:.2f})"
                )

            if lines:
                print("—" * 140)
                print("\n".join(lines))

            # atomic write
            try:
                directory = os.path.dirname(path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                tmp = f"{path}.tmp"
                payload = json.dumps(snapshot)  # orjson -> bytes
                with open(tmp, "wb") as f:
                    f.write(payload); f.flush(); os.fsync(f.fileno())
                os.replace(tmp, path)
            except Exception as e:
                print(f"[printer] write failed: {e}", flush=True)

            await asyncio.sleep(PRINT_EVERY_SEC)
        except Exception as e:
            print(f"[printer] loop error: {e}", flush=True)
            await asyncio.sleep(1.0)

# -----------------------
# Main
# -----------------------
async def main():
    books: Dict[str, L2Book] = {}
    await asyncio.gather(
        binance_futures_loop(books),
        coinbase_spot_loop(books),
        printer(books),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
