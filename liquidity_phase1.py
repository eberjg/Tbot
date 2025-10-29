# Phase 1 Liquidity Collector (free, no ccxt.pro)
# Binance Futures (BTCUSDT, XRPUSDT) + Coinbase Spot (BTC-USD, XRP-USD)
# Streams order book and prints best bid/ask, spread, cum depth ±10 bps.
# Also writes liquidity_snapshot.json every PRINT_EVERY_SEC seconds.

import asyncio
import os
import time
from typing import Dict, List, Tuple, Optional

import orjson as json
import websockets
import aiohttp  # reserved for future HTTP snapshots if needed

# ========= Telegram (optional) =========
TG_TOKEN = os.getenv("TOKEN", "")
TG_CHAT  = os.getenv("CHAT_ID", "")

def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        print("[ALERT]", msg, flush=True)
        return
    try:
        import requests
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id": TG_CHAT, "text": msg},
            timeout=4
        )
    except Exception:
        pass

# ========= Small JSON helpers (work with orjson + websockets str frames) =========
def jloads(raw):
    # websockets gives str; orjson.loads needs bytes
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return json.loads(raw)
    return json.loads(raw.encode("utf-8"))

def jdumps(obj) -> str:
    # orjson.dumps -> bytes; convert to str for ws.send
    b = json.dumps(obj)
    return b.decode("utf-8") if isinstance(b, (bytes, bytearray)) else b

# ========= Signal thresholds (tweak to taste) =========
IMBALANCE_GO_LONG   = 0.20     # ≥ +20% imbalance to the bid side
IMBALANCE_GO_SHORT  = -0.20    # ≤ -20% imbalance to the ask side
MIN_DEPTH_XRP       = 500_000  # XRP cumulative depth within ±10 bps (base units)
MIN_DEPTH_BTC       = 80       # BTC cumulative depth within ±10 bps (base units)
COOLOFF_SEC         = 30       # anti‑spam per symbol

_last_alert_ts: Dict[str, float] = {}

# ========= Config =========
BINANCE_F_SYMBOLS = ["btcusdt", "xrpusdt"]   # futures, USDT‑margined
COINBASE_SPOT     = ["BTC-USD", "XRP-USD"]   # Advanced Trade WS products
PRINT_EVERY_SEC   = 1.0
# Where to write the snapshot (change via env if you want an absolute path)
LIQ_SNAPSHOT_PATH = os.getenv("LIQ_SNAPSHOT", "liquidity_snapshot.json")



# ========= Simple order book model =========
class L2Book:
    def __init__(self, symbol: str, price_tick: float = 0.01):
        self.symbol = symbol
        self.bids: Dict[float, float] = {}  # price -> size
        self.asks: Dict[float, float] = {}
        self.price_tick = price_tick
        self.last_update_ts = 0.0

    def _apply(self, side: Dict[float, float], price: float, size: float):
        if size <= 0:
            side.pop(price, None)
        else:
            side[price] = size

    # Binance futures delta updates
    def update_binance_levels(self, bids: List[List[str]], asks: List[List[str]]):
        # bids/asks: [[price_str, qty_str], ...]
        for p_str, q_str in bids:
            self._apply(self.bids, float(p_str), float(q_str))
        for p_str, q_str in asks:
            self._apply(self.asks, float(p_str), float(q_str))
        self.last_update_ts = time.time()

    # Coinbase snapshots + updates
    def snapshot_coinbase(self, bids: List[List[str]], asks: List[List[str]]):
        self.bids = {float(p): float(q) for p, q in bids}
        self.asks = {float(p): float(q) for p, q in asks}
        self.last_update_ts = time.time()

    def update_coinbase(self, changes: List[List[str]]):
        # changes: [["buy"/"sell", price, size], ...]
        for side, p_str, q_str in changes:
            if side == "buy":
                self._apply(self.bids, float(p_str), float(q_str))
            else:
                self._apply(self.asks, float(p_str), float(q_str))
        self.last_update_ts = time.time()

    def best_bid(self) -> Tuple[float, float]:
        if not self.bids:
            return 0.0, 0.0
        p = max(self.bids.keys())
        return p, self.bids[p]

    def best_ask(self) -> Tuple[float, float]:
        if not self.asks:
            return 0.0, 0.0
        p = min(self.asks.keys())
        return p, self.asks[p]

    def spread(self) -> float:
        b, _ = self.best_bid()
        a, _ = self.best_ask()
        return 0.0 if (b == 0.0 or a == 0.0) else (a - b)

    def cum_depth_bps(self, side: str, mid: float, bps: float = 10.0) -> float:
        """Sum base size within ±bps of mid on the chosen side."""
        if mid <= 0:
            return 0.0
        if side == "bid":
            lo = mid * (1.0 - bps / 10000.0)
            return sum(q for p, q in self.bids.items() if lo <= p <= mid)
        else:
            hi = mid * (1.0 + bps / 10000.0)
            return sum(q for p, q in self.asks.items() if mid <= p <= hi)

# ========= Alerting logic =========
def maybe_alert(symbol: str, spread: float, cum_bid: float, cum_ask: float, venue: Optional[str] = None):
    """
    Emit LONG/SHORT alert when:
      • cumulative depth within ±10 bps exceeds a floor, and
      • order‑book imbalance crosses thresholds.
    Debounced by COOLOFF_SEC per symbol. Also logs to alerts.log / alerts.csv.
    """
    total = cum_bid + cum_ask
    if total <= 0:
        return

    imb = (cum_bid - cum_ask) / total  # -1..+1 (positive = bid‑heavy)
    now = time.time()

    if now - _last_alert_ts.get(symbol, 0.0) < COOLOFF_SEC:
        return

    sym_up = symbol.upper()
    min_depth = MIN_DEPTH_BTC if sym_up.startswith("BTC") else MIN_DEPTH_XRP
    if total < min_depth:
        return

    signal = None
    emoji = ""
    if imb >= IMBALANCE_GO_LONG:
        signal, emoji = "LONG", "📈"
    elif imb <= IMBALANCE_GO_SHORT:
        signal, emoji = "SHORT", "📉"

    if not signal:
        return

    venue_tag = f"[{venue}] " if venue else ""
    msg = (
        f"{venue_tag}{emoji} {symbol} {signal} signal\n"
        f"imbalance={imb:.3f}  spread={spread:.6f}\n"
        f"cum±10bps  bid={cum_bid:.0f}  ask={cum_ask:.0f}"
    )
    send_telegram(msg)

    # Log to files (best‑effort)
    try:
        ts_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        with open("alerts.log", "a") as f:
            f.write(
                f"{ts_iso} {venue or ''} {symbol} {signal} "
                f"imb={imb:.4f} spr={spread:.6f} bid10={cum_bid:.2f} ask10={cum_ask:.2f}\n"
            )
        if not os.path.exists("alerts.csv"):
            with open("alerts.csv", "w") as fcsv:
                fcsv.write("ts,symbol,venue,signal,imbalance,spread,cum_bid10,cum_ask10\n")
        with open("alerts.csv", "a") as fcsv:
            fcsv.write(
                f"{ts_iso},{symbol},{venue or ''},{signal},"
                f"{imb:.6f},{spread:.6f},{cum_bid:.4f},{cum_ask:.4f}\n"
            )
    except Exception:
        pass

    _last_alert_ts[symbol] = now

# ========= Binance Futures WS =========
async def binance_futures_loop(books: Dict[str, L2Book]):
    streams = []
    for s in BINANCE_F_SYMBOLS:
        streams += [f"{s}@depth@100ms", f"{s}@aggTrade"]
    url = "wss://fstream.binance.com/stream?streams=" + "/".join(streams)

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
                while True:
                    raw = await ws.recv()
                    msg = jloads(raw)
                    data = msg.get("data", {})
                    stream = msg.get("stream", "")

                    if stream.endswith("@depth@100ms"):
                        sym = stream.split("@")[0].upper()  # BTCUSDT / XRPUSDT
                        key = f"BINANCEFUT:{sym}"
                        book = books.setdefault(key, L2Book(symbol=key))
                        bids = data.get("b", [])
                        asks = data.get("a", [])
                        book.update_binance_levels(bids, asks)
                    elif stream.endswith("@aggTrade"):
                        # trades feed (not used in v1)
                        pass
        except Exception as e:
            print("[binance_futures_loop] reconnecting after:", e, flush=True)
            await asyncio.sleep(1.5)

# ========= Coinbase Spot WS (Advanced Trade) =========
async def coinbase_spot_loop(books: Dict[str, L2Book]):
    url = "wss://advanced-trade-ws.coinbase.com"
    prods = COINBASE_SPOT

    sub_l2 = {"type": "subscribe", "product_ids": prods, "channel": "level2"}
    sub_tk = {"type": "subscribe", "product_ids": prods, "channel": "ticker"}

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
                await ws.send(jdumps(sub_l2))
                await ws.send(jdumps(sub_tk))

                while True:
                    raw = await ws.recv()
                    msg = jloads(raw)
                    typ = msg.get("type")
                    ch  = msg.get("channel")
                    pid = msg.get("product_id")

                    if typ == "snapshot" and ch == "level2":
                        key = f"COINBASE:{pid}"  # e.g., COINBASE:BTC-USD
                        book = books.setdefault(key, L2Book(symbol=key))
                        book.snapshot_coinbase(msg.get("bids", []), msg.get("asks", []))

                    elif typ == "l2update" and ch == "level2":
                        key = f"COINBASE:{pid}"
                        book = books.setdefault(key, L2Book(symbol=key))
                        book.update_coinbase(msg.get("changes", []))

                    elif typ == "ticker" and ch == "ticker":
                        # optional; we rely on L2 for BB/BA
                        pass
        except Exception as e:
            print("[coinbase_spot_loop] reconnecting after:", e, flush=True)
            await asyncio.sleep(1.5)

# ========= (optional) top walls helper (not used in v1 JSON, handy later) =========
def _top_walls(book: "L2Book", mid: float, pct_window: float = 0.5, top_n: int = 3):
    if mid <= 0:
        return [], []
    lo = mid * (1.0 - pct_window / 100.0)
    hi = mid * (1.0 + pct_window / 100.0)
    bids = sorted([(p, q) for p, q in book.bids.items() if lo <= p <= mid],
                  key=lambda x: x[1], reverse=True)[:top_n]
    asks = sorted([(p, q) for p, q in book.asks.items() if mid <= p <= hi],
                  key=lambda x: x[1], reverse=True)[:top_n]
    return bids, asks

# ========= Printer task (writes liquidity_snapshot.json) =========
def _mid(book: L2Book) -> float:
    b, _ = book.best_bid(); a, _ = book.best_ask()
    return 0.0 if (b == 0 or a == 0) else 0.5 * (a + b)

async def printer(books: Dict[str, L2Book]):
    print(f"[printer] writing snapshot to: {os.path.abspath(LIQ_SNAPSHOT_PATH)}", flush=True)
    notified_once = False
    while True:
        try:
            lines = []
            snapshot = {"ts": int(time.time()), "symbols": {}}

            for name, book in sorted(books.items()):
                b, bq = book.best_bid(); a, aq = book.best_ask()
                if b == 0 or a == 0:
                    continue

                mid   = _mid(book)
                spr   = book.spread()
                bid10 = book.cum_depth_bps("bid", mid, 10.0)
                ask10 = book.cum_depth_bps("ask", mid, 10.0)
                total = bid10 + ask10
                imb   = 0.0 if total <= 0 else (bid10 - ask10) / total

                lines.append(
                    f"{name:<18} bbid={b:.6f}({bq:.4f})  bask={a:.6f}({aq:.4f})  "
                    f"spr={spr:.6f}  mid={mid:.6f}  cum±10bps bid={bid10:.2f} ask={ask10:.2f}"
                )

                venue = "BINANCEFUT" if name.startswith("BINANCEFUT:") else ("COINBASE" if name.startswith("COINBASE:") else "")
                snapshot["symbols"][name] = {
                    "venue": venue,
                    "spread": float(spr),
                    "imbalance": float(imb),
                    "cum_bid10": float(bid10),
                    "cum_ask10": float(ask10),
                    "nearest_bid_wall": 0.0,
                    "nearest_ask_wall": 0.0,
                }

                base_symbol = name.split(":", 1)[1] if ":" in name else name
                maybe_alert(base_symbol, spr, bid10, ask10, venue=venue)

            if lines:
                print("—" * 110)
                print("\n".join(lines))

            # Write/overwrite snapshot atomically (and ensure directory exists)
        try:
    path = os.path.abspath(LIQ_SNAPSHOT_PATH)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)

    tmp_path = f"{path}.tmp"
    payload = json.dumps(snapshot)  # orjson -> bytes

    with open(tmp_path, "wb") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())

    os.replace(tmp_path, path)
    print(f"[printer] snapshot written to: {path}", flush=True)

except Exception as e:
    print(f"[printer] write failed: {e}", flush=True)

            await asyncio.sleep(PRINT_EVERY_SEC)
        except Exception as e:
            print(f"[printer] loop error: {e}", flush=True)
            await asyncio.sleep(1.0)

# ========= Main =========
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