# Phase 1 Liquidity Collector (free, no ccxt.pro)
# Binance Futures (BTCUSDT, XRPUSDT) + Coinbase Spot (BTC-USD, XRP-USD)
# Streams order book and trades, prints best bid/ask, spread, and cum depth ±10 bps.

import asyncio
import math
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Tuple

import json
import websockets
import aiohttp
import os  # ← add this

try:
    import orjson as _oj
except Exception:  # pragma: no cover
    _oj = None

def _json_loads(raw):
    """
    Deterministic JSON parser with optional orjson acceleration.
    Works with str/bytes input.
    """
    if _oj is not None:
        return _oj.loads(raw)
    return json.loads(raw)

def _json_dumps_text(obj) -> str:
    """
    Deterministic JSON serializer for websocket text frames.
    Always returns str.
    """
    if _oj is not None:
        return _oj.dumps(obj).decode("utf-8")
    return json.dumps(obj, separators=(",", ":"))

def _json_dumps_bytes(obj) -> bytes:
    """
    Deterministic JSON serializer for file writes in binary mode.
    Always returns bytes.
    """
    if _oj is not None:
        return _oj.dumps(obj)
    return json.dumps(obj, separators=(",", ":")).encode("utf-8")
# -----------------------
# -----------------------
# Config
# -----------------------
BINANCE_F_SYMBOLS = ["btcusdt", "ethusdt", "xrpusdt"]   # futures, USDT‑margined
COINBASE_SPOT     = ["BTC-USD", "ETH-USD", "XRP-USD"]   # spot, Advanced Trade WS
COINBASE_WS_MAX_SIZE = int(os.getenv("COINBASE_WS_MAX_SIZE", str(8 * 1024 * 1024)))  # 8 MiB
# Safety fanout control: default BTC only; opt in others via COINBASE_EXTRA_PRODUCTS="ETH-USD,XRP-USD"
_cb_extra = [s.strip().upper() for s in os.getenv("COINBASE_EXTRA_PRODUCTS", "").split(",") if s.strip()]
COINBASE_SPOT_ACTIVE = ["BTC-USD"] + [s for s in _cb_extra if s in COINBASE_SPOT and s != "BTC-USD"]

PRINT_EVERY_SEC = 1.0
LIQ_SNAPSHOT_PATH = os.getenv("LIQ_SNAPSHOT", "liquidity_snapshot.json")
# Operator diagnostics: log + periodic summary (set COINBASE_DIAG=0 to reduce noise)
COINBASE_DIAG = os.getenv("COINBASE_DIAG", "1").lower() not in ("0", "false", "no")
DIAG_PRINT_SEC = float(os.getenv("COINBASE_DIAG_INTERVAL_SEC", "10"))
_printer_cb_diag_ts = 0.0
COINBASE_DEBUG_BUILD = "coinbase-debug-build-v4-maxsize-fanout"
_coinbase_prev_diag = {
    "btc_l2_events": 0,
    "btc_ticker_bbo_ok": 0,
}
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

    def apply_coinbase_l2_updates(self, updates: List[dict], is_snapshot: bool):
        """
        Coinbase Advanced Trade level2 (l2_data channel): each update is
        { side, price_level, new_quantity } — new_quantity is level size, 0 = remove.
        side is "bid" or "offer" (asks).
        """
        if is_snapshot:
            self.bids = {}
            self.asks = {}
        if not updates:
            self.last_update_ts = time.time()
            return
        for u in updates:
            if not isinstance(u, dict):
                continue
            p_raw = u.get("price_level", u.get("price"))
            q_raw = u.get("new_quantity", u.get("size", "0"))
            side = str(u.get("side", "")).strip().lower()
            try:
                p = float(p_raw)
                q = float(q_raw)
            except (TypeError, ValueError):
                continue
            # Advanced Trade uses bid/offer; some feeds use buy/sell.
            if side in ("bid", "buy", "b", "purchase"):
                self._clean(self.bids, p, q)
            elif side in ("offer", "ask", "sell", "a"):
                self._clean(self.asks, p, q)
        self.last_update_ts = time.time()

    def seed_top_of_book_from_ticker(
        self, bid: float, ask: float, bid_q: float = 1e-8, ask_q: float = 1e-8
    ):
        """
        When level2 is slow or one-sided, BBO from ticker keeps a valid mid for snapshots.
        Only writes the best bid / best ask levels (deterministic minimal book).
        """
        try:
            b = float(bid)
            a = float(ask)
        except (TypeError, ValueError):
            return
        if b <= 0 or a <= 0 or a <= b:
            return
        self._clean(self.bids, b, max(float(bid_q), 1e-12))
        self._clean(self.asks, a, max(float(ask_q), 1e-12))
        self.last_update_ts = time.time()

    def repair_crossed_book(self) -> Dict[str, Any]:
        """
        Deterministic crossed-book repair:
        1) remove asks priced <= current best bid (likely stale opposite side)
        2) if still crossed, remove bids priced >= current best ask
        Returns diagnostics; does not guarantee solvable book.
        """
        bb, _ = self.best_bid()
        ba, _ = self.best_ask()
        out = {
            "crossed_before": bool(bb > 0 and ba > 0 and bb > ba),
            "removed_asks": 0,
            "removed_bids": 0,
            "crossed_after": False,
            "bb_after": 0.0,
            "ba_after": 0.0,
        }
        if not out["crossed_before"]:
            out["bb_after"], out["ba_after"] = bb, ba
            return out

        # Pass 1: stale asks below/at current best bid
        rm_asks = [p for p in list(self.asks.keys()) if p <= bb]
        for p in rm_asks:
            self.asks.pop(p, None)
        out["removed_asks"] = len(rm_asks)

        bb2, _ = self.best_bid()
        ba2, _ = self.best_ask()
        if bb2 > 0 and ba2 > 0 and bb2 > ba2:
            # Pass 2: stale bids above/at current best ask
            rm_bids = [p for p in list(self.bids.keys()) if p >= ba2]
            for p in rm_bids:
                self.bids.pop(p, None)
            out["removed_bids"] = len(rm_bids)

        bb3, _ = self.best_bid()
        ba3, _ = self.best_ask()
        out["bb_after"], out["ba_after"] = bb3, ba3
        out["crossed_after"] = bool(bb3 > 0 and ba3 > 0 and bb3 > ba3)
        return out

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
                msg = _json_loads(raw)
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
# Coinbase Spot WS (Advanced Trade) — diagnostics + multi-path parser
# -----------------------
_coinbase_diag: Dict[str, Any] = {
    "connects": 0,
    "sub_heartbeat": 0,
    "sub_level2": 0,
    "sub_ticker": 0,
    "msg_total": 0,
    "raw_frames": 0,
    "by_channel": defaultdict(int),
    "l2_events_applied": 0,
    "ticker_bbo_applied": 0,
    "heartbeats": 0,
    "sub_ack_frames": 0,
    "last_error": "",
    "unknown_samples": deque(maxlen=8),
    "first_non_hb_logged": 0,
    "unknown_live_logged": 0,
    "btc_ticker_frames": 0,
    "btc_ticker_bbo_ok": 0,
    "btc_l2_events": 0,
    "btc_l2_update_rows": 0,
    "json_parse_empty": 0,
    "conn_open_ts": 0.0,
    "conn_open_mono": 0.0,
    "first_frame_ts": 0.0,
    "first_frame_seen": False,
    "first_wait_logged": False,
    "no_frames_10s_warned": False,
    "raw_non_hb_logged": 0,
    "active_conn_id": 0,
    "crossed_detected": 0,
    "crossed_resolved": 0,
    "crossed_dropped": 0,
    "l2_side_bid_rows": 0,
    "l2_side_ask_rows": 0,
    "l2_side_unknown_rows": 0,
    "btc_l2_last_ts": 0.0,
    "btc_ticker_last_ts": 0.0,
    "health": "degraded",
}


async def _coinbase_first_frame_timeout_probe(
    conn_id: int, url: str, channels: List[str]
) -> None:
    """
    Transport-level liveness probe: if recv loop has no first frame after 10s,
    emit an explicit timeout diagnostic.
    """
    await asyncio.sleep(10.0)
    if int(_coinbase_diag.get("active_conn_id", 0)) != int(conn_id):
        return
    if not bool(_coinbase_diag.get("first_frame_seen", False)):
        _coinbase_diag["no_frames_10s_warned"] = True
        print(
            "[coinbase] FIRST-FRAME TIMEOUT 10s "
            f"(conn_id={conn_id}, url={url}, channels={channels}, "
            f"raw_frames={_coinbase_diag.get('raw_frames', 0)}, "
            f"json_msgs={_coinbase_diag.get('msg_total', 0)}, "
            f"sub_ack_frames={_coinbase_diag.get('sub_ack_frames', 0)})",
            flush=True,
        )


def _coinbase_endpoint_profile(profile_name: str, prods: List[str]) -> Dict[str, Any]:
    """
    Deterministic transport profile:
      - advanced_trade: docs endpoint/channels (heartbeats, level2, ticker)
      - exchange: public exchange feed fallback if advanced_trade yields no frames
    """
    if profile_name == "exchange":
        return {
            "name": "exchange",
            "url": "wss://ws-feed.exchange.coinbase.com",
            "channels": ["heartbeat", "level2", "ticker"],
            "subs": [
                {"type": "subscribe", "channels": [{"name": "heartbeat", "product_ids": prods}]},
                {"type": "subscribe", "channels": [{"name": "level2", "product_ids": prods}]},
                {"type": "subscribe", "channels": [{"name": "ticker", "product_ids": prods}]},
            ],
        }
    return {
        "name": "advanced_trade",
        "url": "wss://advanced-trade-ws.coinbase.com",
        "channels": ["heartbeats", "level2", "ticker"],
        "subs": [
            {"type": "subscribe", "channel": "heartbeats"},
            {"type": "subscribe", "product_ids": prods, "channel": "level2"},
            {"type": "subscribe", "product_ids": prods, "channel": "ticker"},
        ],
    }


def _normalize_coinbase_product_id(pid: Any) -> str:
    """Deterministic product id for book keys (must match bot: COINBASE:BTC-USD)."""
    if pid is None:
        return ""
    s = str(pid).strip()
    if not s:
        return ""
    return s.upper().replace(" ", "")


def _coinbase_split_json_frames(raw_s: str) -> List[Any]:
    """
    One WebSocket text frame may contain one JSON object or NDJSON (multiple lines).
    """
    raw_s = (raw_s or "").strip()
    if not raw_s:
        return []
    out: List[Any] = []
    try:
        out.append(_json_loads(raw_s))
        return out
    except Exception:
        pass
    for line in raw_s.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(_json_loads(line))
        except Exception:
            continue
    return out


def _coinbase_is_subscription_frame(msg: dict) -> bool:
    typ = str(msg.get("type") or "").lower()
    ch = str(msg.get("channel") or "").lower()
    if typ == "subscriptions" or ch == "subscriptions":
        return True
    if isinstance(msg.get("subscriptions"), list):
        return True
    return False


def _coinbase_log_unknown(msg: dict, ch, typ) -> None:
    if not COINBASE_DIAG:
        return
    try:
        s = _json_dumps_text(msg)
        if len(s) > 320:
            s = s[:320] + "…"
        _coinbase_diag["unknown_samples"].append(f"ch={ch!r} type={typ!r} {s}")
    except Exception:
        pass


def _coinbase_side_bucket(side_raw: Any) -> str:
    s = str(side_raw or "").strip().lower()
    if s in ("bid", "buy", "b", "purchase"):
        return "bid"
    if s in ("offer", "ask", "sell", "a"):
        return "ask"
    return "unknown"


def _coinbase_try_apply_l2_event(ev: dict, books: Dict[str, L2Book], diag: Dict[str, Any]) -> bool:
    if not isinstance(ev, dict) or "updates" not in ev:
        return False
    updates = ev.get("updates")
    if not isinstance(updates, list):
        return False
    pid = _normalize_coinbase_product_id(ev.get("product_id"))
    if not pid:
        return False
    ev_type = str(ev.get("type") or "").lower()
    if ev_type not in ("snapshot", "update"):
        if ev_type in ("l2_snapshot", "full_snapshot"):
            ev_type = "snapshot"
        elif updates:
            ev_type = "update"
        else:
            return False
    if ev_type == "snapshot" and len(updates) == 0:
        return False
    for u in updates:
        if isinstance(u, dict):
            b = _coinbase_side_bucket(u.get("side"))
            if b == "bid":
                diag["l2_side_bid_rows"] += 1
            elif b == "ask":
                diag["l2_side_ask_rows"] += 1
            else:
                diag["l2_side_unknown_rows"] += 1
    book = books.setdefault(f"COINBASE:{pid}", L2Book(symbol=f"COINBASE:{pid}"))
    book.apply_coinbase_l2_updates(updates, is_snapshot=(ev_type == "snapshot"))
    diag["l2_events_applied"] += 1
    if pid == "BTC-USD":
        diag["btc_l2_events"] += 1
        diag["btc_l2_update_rows"] += len(updates)
        diag["btc_l2_last_ts"] = time.monotonic()
    return True


def _coinbase_walk_l2(obj: Any, books: Dict[str, L2Book], diag: Dict[str, Any]) -> int:
    """Find nested { product_id, updates: [...] } shapes (feed layout drift)."""
    n = 0
    if isinstance(obj, dict):
        if _coinbase_try_apply_l2_event(obj, books, diag):
            n += 1
        for v in obj.values():
            n += _coinbase_walk_l2(v, books, diag)
    elif isinstance(obj, list):
        for x in obj:
            n += _coinbase_walk_l2(x, books, diag)
    return n


def _coinbase_try_apply_ticker_row(t: dict, books: Dict[str, L2Book], diag: Dict[str, Any]) -> bool:
    pid = _normalize_coinbase_product_id(t.get("product_id"))
    if not pid:
        return False
    try:
        bb = float(t.get("best_bid") or t.get("best_bid_price") or 0)
        ba = float(t.get("best_ask") or t.get("best_ask_price") or 0)
        bbq = float(
            t.get("best_bid_quantity")
            or t.get("best_bid_size")
            or t.get("bid_quantity")
            or 0
        )
        baq = float(
            t.get("best_ask_quantity")
            or t.get("best_ask_size")
            or t.get("ask_quantity")
            or 0
        )
    except (TypeError, ValueError):
        return False
    if bb <= 0 or ba <= 0:
        return False
    if pid == "BTC-USD":
        diag["btc_ticker_bbo_ok"] += 1
        diag["btc_ticker_last_ts"] = time.monotonic()
    book = books.setdefault(f"COINBASE:{pid}", L2Book(symbol=f"COINBASE:{pid}"))
    book.seed_top_of_book_from_ticker(bb, ba, bbq, baq)
    diag["ticker_bbo_applied"] += 1
    return True


def _coinbase_walk_ticker_bbo(obj: Any, books: Dict[str, L2Book], diag: Dict[str, Any]) -> int:
    """Find nested ticker rows with product_id + best_bid/best_ask."""
    n = 0
    if isinstance(obj, dict):
        if "product_id" in obj and ("best_bid" in obj or "best_bid_price" in obj):
            if "best_ask" in obj or "best_ask_price" in obj:
                if _coinbase_try_apply_ticker_row(obj, books, diag):
                    n += 1
        for v in obj.values():
            n += _coinbase_walk_ticker_bbo(v, books, diag)
    elif isinstance(obj, list):
        for x in obj:
            n += _coinbase_walk_ticker_bbo(x, books, diag)
    return n


async def coinbase_spot_loop(books: Dict[str, L2Book]):
    """
    Coinbase Advanced Trade market WebSocket.
    - Subscribe to heartbeats (keeps channel subscriptions alive per Coinbase docs).
    - level2 → l2_data events with updates[] (primary book).
    - ticker → best_bid/best_ask BBO when L2 is one-sided or slow (valid mid for snapshots).
    """
    prods = COINBASE_SPOT_ACTIVE
    profile_order = ["advanced_trade", "exchange"]
    profile_idx = 0

    while True:
        try:
            profile_name = profile_order[profile_idx % len(profile_order)]
            profile = _coinbase_endpoint_profile(profile_name, prods)
            url = profile["url"]
            subs = profile["subs"]
            channels = profile["channels"]
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_size=COINBASE_WS_MAX_SIZE,
            ) as ws:
                _coinbase_diag["connects"] += 1
                conn_id = int(_coinbase_diag["connects"])
                _coinbase_diag["active_conn_id"] = conn_id
                _coinbase_diag["first_non_hb_logged"] = 0
                _coinbase_diag["unknown_live_logged"] = 0
                _coinbase_diag["raw_non_hb_logged"] = 0
                _coinbase_diag["conn_open_ts"] = time.time()
                _coinbase_diag["conn_open_mono"] = time.monotonic()
                _coinbase_diag["first_frame_ts"] = 0.0
                _coinbase_diag["first_frame_seen"] = False
                _coinbase_diag["first_wait_logged"] = False
                _coinbase_diag["no_frames_10s_warned"] = False
                print(f"[coinbase] connection OPEN (id #{conn_id}) → {url}", flush=True)

                for payload in subs:
                    raw_payload = _json_dumps_text(payload)
                    ch_name = payload.get("channel")
                    if not ch_name and isinstance(payload.get("channels"), list):
                        try:
                            ch_name = ",".join(
                                str(x.get("name", "?")) for x in payload["channels"] if isinstance(x, dict)
                            )
                        except Exception:
                            ch_name = "unknown"
                    await ws.send(raw_payload)
                    if "heart" in str(ch_name):
                        _coinbase_diag["sub_heartbeat"] += 1
                    elif "level2" in str(ch_name):
                        _coinbase_diag["sub_level2"] += 1
                    elif "ticker" in str(ch_name):
                        _coinbase_diag["sub_ticker"] += 1
                _coinbase_diag["first_wait_logged"] = True
                asyncio.create_task(
                    _coinbase_first_frame_timeout_probe(
                        conn_id, url, channels
                    )
                )
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                    except asyncio.TimeoutError:
                        if not _coinbase_diag.get("first_frame_seen", False):
                            _coinbase_diag["no_frames_10s_warned"] = True
                            print(
                                "[coinbase] recv timeout (10s) before first frame; "
                                f"conn_id={conn_id} url={url} channels={channels} "
                                f"raw_frames={_coinbase_diag.get('raw_frames', 0)} "
                                f"json_msgs={_coinbase_diag.get('msg_total', 0)} "
                                f"sub_ack_frames={_coinbase_diag.get('sub_ack_frames', 0)}. "
                                "Reconnecting with next transport profile.",
                                flush=True,
                            )
                            break
                        # If stream was active, keep waiting.
                        continue
                    if not _coinbase_diag.get("first_frame_seen", False):
                        _coinbase_diag["first_frame_seen"] = True
                        _coinbase_diag["first_frame_ts"] = time.time()
                    raw_s = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                    _coinbase_diag["raw_frames"] += 1
                    split_msgs = _coinbase_split_json_frames(raw_s)
                    if not split_msgs and raw_s.strip():
                        _coinbase_diag["json_parse_empty"] += 1
                    for msg in split_msgs:
                        if not isinstance(msg, dict):
                            continue
                        _coinbase_diag["msg_total"] += 1
                        ch = msg.get("channel")
                        typ = msg.get("type")
                        ch_l = str(ch or "").lower()
                        if ch is not None:
                            _coinbase_diag["by_channel"][str(ch)] += 1

                        # --- Subscription ack (log full content every time; operator-visible) ---
                        if _coinbase_is_subscription_frame(msg):
                            _coinbase_diag["sub_ack_frames"] += 1
                            print(f"[coinbase] SUBSCRIBE confirmed (ack #{_coinbase_diag['sub_ack_frames']})", flush=True)
                            continue

                        if str(typ).lower() == "error" or msg.get("message"):
                            err = str(msg.get("message") or msg)
                            _coinbase_diag["last_error"] = err
                            print(f"[coinbase] ERROR frame: {msg}", flush=True)
                            continue

                        if ch_l == "heartbeats":
                            _coinbase_diag["heartbeats"] += 1
                            continue

                        # --- Ticker: channel match (case-insensitive) + events[].tickers[] ---
                        if ch_l == "ticker" and isinstance(msg.get("events"), list):
                            any_ticker_applied = False
                            for ev in msg["events"]:
                                if not isinstance(ev, dict):
                                    continue
                                for t in ev.get("tickers") or []:
                                    if not isinstance(t, dict):
                                        continue
                                    if (
                                        _normalize_coinbase_product_id(t.get("product_id"))
                                        == "BTC-USD"
                                    ):
                                        _coinbase_diag["btc_ticker_frames"] += 1
                                    if _coinbase_try_apply_ticker_row(t, books, _coinbase_diag):
                                        any_ticker_applied = True
                            if any_ticker_applied:
                                continue

                        # --- Level2: events[].updates (l2_data / level2) ---
                        handled_l2 = False
                        if isinstance(msg.get("events"), list):
                            for ev in msg["events"]:
                                if isinstance(ev, dict) and _coinbase_try_apply_l2_event(
                                    ev, books, _coinbase_diag
                                ):
                                    handled_l2 = True
                            if handled_l2:
                                continue

                        # --- Legacy flat level2 ---
                        pid = msg.get("product_id")
                        if typ == "snapshot" and ch_l == "level2" and pid:
                            np = _normalize_coinbase_product_id(pid)
                            book = books.setdefault(
                                f"COINBASE:{np}", L2Book(symbol=f"COINBASE:{np}")
                            )
                            book.snapshot_coinbase(msg.get("bids", []), msg.get("asks", []))
                            _coinbase_diag["l2_events_applied"] += 1
                            if np == "BTC-USD":
                                _coinbase_diag["btc_l2_events"] += 1
                            continue
                        if typ == "l2update" and ch_l == "level2" and pid:
                            np = _normalize_coinbase_product_id(pid)
                            book = books.setdefault(
                                f"COINBASE:{np}", L2Book(symbol=f"COINBASE:{np}")
                            )
                            book.update_coinbase(msg.get("changes", []))
                            _coinbase_diag["l2_events_applied"] += 1
                            if np == "BTC-USD":
                                _coinbase_diag["btc_l2_events"] += 1
                            continue

                        # --- Fallback: nested ticker / L2 (channel naming drift, extra wrappers) ---
                        ft = _coinbase_walk_ticker_bbo(msg, books, _coinbase_diag)
                        fl = _coinbase_walk_l2(msg, books, _coinbase_diag)
                        if ft or fl:
                            continue

                        _coinbase_log_unknown(msg, ch, typ)

                # Deterministic transport fallback if current profile is silent.
                if (
                    not _coinbase_diag.get("first_frame_seen", False)
                    and _coinbase_diag.get("no_frames_10s_warned", False)
                ):
                    profile_idx += 1
                    next_name = profile_order[profile_idx % len(profile_order)]
                    print(
                        f"[coinbase] transport switch: {profile_name} -> {next_name}",
                        flush=True,
                    )
                else:
                    # Keep stable profile when frames flow.
                    profile_idx = 0

        except Exception as e:
            _coinbase_diag["last_error"] = str(e)
            _coinbase_diag["active_conn_id"] = 0
            print(f"[coinbase] connection closed / error: {e!r} — reconnecting…", flush=True)
            await asyncio.sleep(1.5)
            continue

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


def _coinbase_prepare_for_emit(name: str, book: L2Book) -> tuple:
    """
    Final Coinbase emission gate on the exact output path.
    Returns (ok, bb, bbq, ba, baq, spread, note).
    """
    bb, bbq = book.best_bid()
    ba, baq = book.best_ask()
    if bb <= 0 or ba <= 0:
        return False, bb, bbq, ba, baq, 0.0, "missing-side"

    if bb > ba:
        rep = book.repair_crossed_book()
        if rep.get("crossed_before"):
            _coinbase_diag["crossed_detected"] += 1
            if not rep.get("crossed_after"):
                _coinbase_diag["crossed_resolved"] += 1
            print(
                f"[coinbase-integrity] crossed book detected {name}: "
                f"removed_asks={rep.get('removed_asks',0)} removed_bids={rep.get('removed_bids',0)} "
                f"bb_after={rep.get('bb_after',0.0):.2f} ba_after={rep.get('ba_after',0.0):.2f} "
                f"resolved={not rep.get('crossed_after', False)}",
                flush=True,
            )
        bb, bbq = book.best_bid()
        ba, baq = book.best_ask()
        if bb <= 0 or ba <= 0:
            return False, bb, bbq, ba, baq, 0.0, "missing-after-repair"
        if bb > ba:
            return False, bb, bbq, ba, baq, ba - bb, "crossed-after-repair"

    spr = ba - bb
    if spr < 0:
        return False, bb, bbq, ba, baq, spr, "negative-spread"
    return True, bb, bbq, ba, baq, spr, "ok"


async def printer(books: Dict[str, L2Book]):
    global _printer_cb_diag_ts, _coinbase_prev_diag
    path = os.getenv("LIQ_SNAPSHOT", "liquidity_snapshot.json")
    print(f"[printer] writing snapshot to: {os.path.abspath(path)}", flush=True)
    while True:
        try:
            lines = []
            snapshot = {
                "ts": int(time.time()),
                "symbols": {},
                # Always present deterministic meta contract (even before symbols are populated)
                "meta": {
                    "coinbase_feed_health": "degraded",
                    "coinbase_l2_active": False,
                    "coinbase_ticker_fallback_active": False,
                    "coinbase_btc_row_valid": False,
                    "collector_build": str(COINBASE_DEBUG_BUILD or ""),
                },
            }

            for name, book in sorted(books.items()):
                if name.startswith("COINBASE:"):
                    ok_emit, bb, bbq, ba, baq, spr, note = _coinbase_prepare_for_emit(name, book)
                    if not ok_emit:
                        _coinbase_diag["crossed_dropped"] += 1
                        print(
                            f"[coinbase-integrity] dropped invalid Coinbase row {name}: "
                            f"reason={note} bbid={bb:.2f} bask={ba:.2f} spr={spr:.6f}",
                            flush=True,
                        )
                        continue
                else:
                    bb, bbq = book.best_bid(); ba, baq = book.best_ask()
                    if bb == 0 or ba == 0:
                        continue
                    if bb > ba:
                        # Never emit invalid crossed books from any venue.
                        print(
                            f"[book-integrity] drop snapshot row {name}: crossed bbid={bb:.2f} bask={ba:.2f}",
                            flush=True,
                        )
                        continue

                mid   = _mid(book)
                # Use the exact checked top-of-book spread for deterministic integrity.
                spr   = ba - bb
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

            # Non-blocking trust annotation exported to bot layer.
            cb_key = "COINBASE:BTC-USD"
            inc_cb = cb_key in snapshot["symbols"]
            now_mono = time.monotonic()
            l2_active_recent = (now_mono - float(_coinbase_diag.get("btc_l2_last_ts", 0.0) or 0.0)) <= max(6.0, DIAG_PRINT_SEC * 2.0)
            ticker_active_recent = (now_mono - float(_coinbase_diag.get("btc_ticker_last_ts", 0.0) or 0.0)) <= max(6.0, DIAG_PRINT_SEC * 2.0)
            if inc_cb and l2_active_recent:
                cb_health = "healthy_l2"
            elif inc_cb and (not l2_active_recent) and ticker_active_recent:
                cb_health = "fallback_only"
            else:
                cb_health = "degraded"
            _coinbase_diag["health"] = cb_health
            snapshot["meta"] = {
                "coinbase_feed_health": cb_health,
                "coinbase_l2_active": bool(l2_active_recent),
                "coinbase_ticker_fallback_active": bool(ticker_active_recent),
                "coinbase_btc_row_valid": bool(inc_cb),
                "collector_build": str(COINBASE_DEBUG_BUILD or ""),
            }

            # Operator-visible Coinbase health (rate-limited)
            if COINBASE_DIAG and (time.time() - _printer_cb_diag_ts) >= DIAG_PRINT_SEC:
                _printer_cb_diag_ts = time.time()
                bcb = books.get(cb_key)
                ch_hist = dict(sorted(_coinbase_diag["by_channel"].items(), key=lambda x: (-x[1], x[0])))
                d_l2 = int(_coinbase_diag.get("btc_l2_events", 0)) - int(_coinbase_prev_diag.get("btc_l2_events", 0))
                d_tk = int(_coinbase_diag.get("btc_ticker_bbo_ok", 0)) - int(_coinbase_prev_diag.get("btc_ticker_bbo_ok", 0))
                _coinbase_prev_diag["btc_l2_events"] = int(_coinbase_diag.get("btc_l2_events", 0))
                _coinbase_prev_diag["btc_ticker_bbo_ok"] = int(_coinbase_diag.get("btc_ticker_bbo_ok", 0))
                l2_active = l2_active_recent or (d_l2 > 0)
                ticker_active = ticker_active_recent or (d_tk > 0)
                btc_valid_emit = bool(inc_cb)
                health = cb_health
                if bcb:
                    bb0, bbq0 = bcb.best_bid()
                    ba0, baq0 = bcb.best_ask()
                    print(
                        f"[coinbase-diag] health={health} l2_active={l2_active} ticker_fallback_active={ticker_active} "
                        f"btc_row_valid={btc_valid_emit} "
                        f"bbid={bb0:.2f} bask={ba0:.2f} spr={ba0-bb0:.6f} "
                        f"delta_l2={d_l2} delta_ticker={d_tk} "
                        f"crossed_resolved={_coinbase_diag.get('crossed_resolved',0)} dropped={_coinbase_diag.get('crossed_dropped',0)} "
                        f"ack={_coinbase_diag.get('sub_ack_frames', 0)} hb={_coinbase_diag['heartbeats']} "
                        f"channels={ch_hist}",
                        flush=True,
                    )
                else:
                    print(
                        f"[coinbase-diag] health={health} l2_active={l2_active} ticker_fallback_active={ticker_active} "
                        f"btc_row_valid={btc_valid_emit} book={cb_key}:missing "
                        f"delta_l2={d_l2} delta_ticker={d_tk} "
                        f"crossed_resolved={_coinbase_diag.get('crossed_resolved',0)} dropped={_coinbase_diag.get('crossed_dropped',0)} "
                        f"ack={_coinbase_diag.get('sub_ack_frames', 0)} hb={_coinbase_diag['heartbeats']} "
                        f"channels={ch_hist}",
                        flush=True,
                    )
                if not _coinbase_diag.get("first_frame_seen", False):
                    open_mono = float(_coinbase_diag.get("conn_open_mono", 0.0) or 0.0)
                    dt = (time.monotonic() - open_mono) if open_mono > 0 else None
                    if dt is not None and dt >= 10.0:
                        _coinbase_diag["no_frames_10s_warned"] = True
                    print(
                        f"[coinbase-diag] recv status: no frames received yet "
                        f"(elapsed={'n/a' if dt is None else f'{dt:.1f}s'}, "
                        f"warned_10s={_coinbase_diag.get('no_frames_10s_warned', False)}, "
                        f"first_wait_logged={_coinbase_diag.get('first_wait_logged', False)})",
                        flush=True,
                    )
                print(
                    f"[coinbase-diag] snapshot includes {cb_key} row: {inc_cb}",
                    flush=True,
                )

            if lines:
                print("—" * 140)
                print("\n".join(lines))

            # atomic write
            try:
                directory = os.path.dirname(path)
                if directory:
                    os.makedirs(directory, exist_ok=True)
                m = snapshot.get("meta", {}) if isinstance(snapshot, dict) else {}
                print(
                    "[snapshot-meta] "
                    f"coinbase_feed_health={m.get('coinbase_feed_health','degraded')} "
                    f"l2_active={bool(m.get('coinbase_l2_active', False))} "
                    f"ticker_fallback_active={bool(m.get('coinbase_ticker_fallback_active', False))} "
                    f"btc_row_valid={bool(m.get('coinbase_btc_row_valid', False))} "
                    f"collector_build={m.get('collector_build','')}",
                    flush=True,
                )
                tmp = f"{path}.tmp"
                payload = _json_dumps_bytes(snapshot)
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
    print(
        f"[coinbase-debug-build] active version: {COINBASE_DEBUG_BUILD}",
        flush=True,
    )
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
