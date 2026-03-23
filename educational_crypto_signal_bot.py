# === EDUCATIONAL CRYPTO SIGNAL BOT — Sniper Futures-Only Fast Breakdown (clean baseline) ===
# Baseline: no Telegram command poller/webhook; no daily scheduler by default → avoids duplicate /daily messages.
# Requires: .env with TOKEN, CHAT_ID, and Coinbase creds used by coinbase_futures.py


import os, time, json, datetime, threading, requests
import html
from collections import deque, defaultdict
import ccxt

from flask import Flask, Response, render_template_string
from dotenv import load_dotenv

# === Winner-Proof add-ons ===
from macro_guard import MacroGuard
from market_stats import get_24h_change_pct, build_vol_flags, fmt_pct
from order_precision import apply_amount_precision

#from typing import Optional, Tuple, Dict, Any
from typing import Optional, Tuple, Dict, Any, Iterable, Union, List

# Try orjson for speed, but fall back to stdlib json
try:
    import orjson as _oj
except Exception:  # pragma: no cover
    _oj = None

# ──────────────────────────────────────────────────────────────────────────────
# Flask app (created early so decorators can bind)
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Env & imports from Coinbase adapter
# ──────────────────────────────────────────────────────────────────────────────

MACRO = MacroGuard()


load_dotenv(dotenv_path=".env", override=True)

from coinbase_futures import (
    auth_smoke_test,
    get_futures_price,
    futures_market_buy,
    futures_market_sell,
    get_usd_balance,
    get_spot_price,
    spot_market_buy,
    spot_market_sell,
    get_order_fills,
)

# --- Liquidity wall watching (end-user clarity) ---
#WALL_MIN_SIZE_RATIO = float(os.getenv("WALL_MIN_SIZE_RATIO", "0.25"))  # fraction of total depth in ±10 bps to count as a wall (e.g., 25%)
#WALL_ALERT_BPS      = int(os.getenv("WALL_ALERT_BPS", "8"))            # alert when price within X bps of a wall
#WALL_CONSUME_DROP   = float(os.getenv("WALL_CONSUME_DROP", "0.35"))    # trigger if a wall shrinks by ≥35% vs last snapshot
#WALL_ALERT_COOLDOWN = int(os.getenv("WALL_ALERT_COOLDOWN", "120"))     # sec to avoid spam per wall



MACRO_GUARD_ENABLED = os.getenv("MACRO_GUARD_ENABLED","true").lower()=="true"
MACRO_EVENTS_FILE   = os.getenv("MACRO_EVENTS_FILE","macro_events.json")
MACRO_BLOCK_WINDOW_MIN = int(os.getenv("MACRO_BLOCK_WINDOW_MIN","45"))
MACRO_BLOCK_IMPACTS = os.getenv("MACRO_BLOCK_IMPACTS","high,medium")
MACRO_CALENDAR_ALERTS_ENABLED = os.getenv("MACRO_CALENDAR_ALERTS_ENABLED", "true").lower() == "true"
MACRO_CALENDAR_LOOKAHEAD_HOURS = int(os.getenv("MACRO_CALENDAR_LOOKAHEAD_HOURS", "168"))
MACRO_CALENDAR_POLL_SEC = int(os.getenv("MACRO_CALENDAR_POLL_SEC", "30"))
MACRO_CALENDAR_ALERT_MINUTES = [
    int(x.strip()) for x in os.getenv("MACRO_CALENDAR_ALERT_MINUTES", "120,60,30,15,5,1").split(",")
    if x.strip().isdigit()
]
DESK_BRIEF_ENABLED = os.getenv("DESK_BRIEF_ENABLED", "true").lower() == "true"
DESK_BRIEF_INTERVAL_SEC = int(os.getenv("DESK_BRIEF_INTERVAL_SEC", "900"))  # 15m
DESK_BRIEF_TIMEFRAME = os.getenv("DESK_BRIEF_TIMEFRAME", "15m").strip().lower() or "15m"
DESK_FLIP_ALERTS_ENABLED = os.getenv("DESK_FLIP_ALERTS_ENABLED", "true").lower() == "true"
DESK_FLIP_COOLDOWN_SEC = int(os.getenv("DESK_FLIP_COOLDOWN_SEC", "180"))
DESK_STYLE = os.getenv("DESK_STYLE", "plain").strip().lower()  # plain | table

PCT_CHANGE_FLAG = float(os.getenv("PCT_CHANGE_ALERT","3.0"))  # show ⚠️ if abs(24h) > this
VOL_RATIO_FLAG  = float(os.getenv("VOL_RATIO_ALERT","2.0"))   # unusual volume if >=
LOW_CONF_THRESHOLD = int(os.getenv("LOW_CONF_THRESHOLD", "25"))  # if conf < this, demote LONG/SHORT to WAIT (UX only)
# ──────────────────────────────────────────────────────────────────────────────
# Telegram config
# ──────────────────────────────────────────────────────────────────────────────
TOKEN = os.getenv("TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

if not TOKEN or not CHAT_ID:
    print("⚠️  Telegram TOKEN/CHAT_ID missing — messages will be skipped.")

# ──────────────────────────────────────────────────────────────────────────────
# Files & liquidity snapshot
# ──────────────────────────────────────────────────────────────────────────────
_LIQ_PATH = os.getenv("LIQ_SNAPSHOT", "liquidity_snapshot.json")
LIQ_SNAPSHOT_MAX_AGE_SEC = float(os.getenv("LIQ_SNAPSHOT_MAX_AGE_SEC", "3"))
# Canonical SLA name for deterministic freshness policy.
LIQ_MAX_AGE_SEC = float(os.getenv("LIQ_MAX_AGE_SEC", str(LIQ_SNAPSHOT_MAX_AGE_SEC)))
print(f"[LIQ] Reading snapshot from: {_LIQ_PATH}", flush=True)

# Venue divergence (multi-venue mid agreement) — BTC-USD only in Phase 1 (snapshot has Binance Fut + Coinbase).
VENUE_DIV_ENABLED = os.getenv("VENUE_DIV_ENABLED", "true").lower() == "true"
VENUE_DIV_BPS_WARN = float(os.getenv("VENUE_DIV_BPS_WARN", "25"))
VENUE_DIV_BPS_HARD = float(os.getenv("VENUE_DIV_BPS_HARD", "80"))
if VENUE_DIV_BPS_HARD <= VENUE_DIV_BPS_WARN:
    VENUE_DIV_BPS_HARD = VENUE_DIV_BPS_WARN + 1.0
VENUE_DIV_CONF_PENALTY = float(os.getenv("VENUE_DIV_CONF_PENALTY", "0.65"))
print(
    f"[VENUE] Divergence guard: enabled={VENUE_DIV_ENABLED} warn>{VENUE_DIV_BPS_WARN:.0f}bps "
    f"hard>{VENUE_DIV_BPS_HARD:.0f}bps penalty={VENUE_DIV_CONF_PENALTY:.2f} (BTC-USD)",
    flush=True,
)

# ──────────────────────────────────────────────────────────────────────────────
# Feature flags & knobs
# ──────────────────────────────────────────────────────────────────────────────
STARTUP_HEALTHCHECK = os.getenv("STARTUP_HEALTHCHECK", "true").lower() == "true"
HEALTHCHECK_SYMBOLS = [s.strip() for s in os.getenv("HEALTHCHECK_SYMBOLS", "BTC-USD,SOL-USD,XRP-USD,ETH-USD").split(",") if s.strip()]

FUTURES_SIGNALS_ENABLED = os.getenv("FUTURES_SIGNALS_ENABLED", "false").lower() == "true"
AUTO_TRADE_ENABLED      = os.getenv("AUTO_TRADE_ENABLED", "false").lower() == "true"
TEST_MODE               = os.getenv("TEST_MODE", "false").lower() == "true"

# Spot autopilot (off by default)
SPOT_AUTOPILOT_ENABLED = os.getenv("SPOT_AUTOPILOT_ENABLED", "false").lower() == "true"
SPOT_SYMBOLS       = [s.strip() for s in os.getenv("SPOT_SYMBOLS", "BTC-USD,SOL-USD,ETH-USD,XRP-USD").split(",") if s.strip()]
SPOT_TRADE_USD     = float(os.getenv("SPOT_TRADE_USD", "50"))
SPOT_FEE_PCT       = float(os.getenv("SPOT_FEE_PCT", "0.35"))   # %/side
SPOT_TP_PCT        = float(os.getenv("SPOT_TP_PCT", "0.8"))
SPOT_SL_PCT        = float(os.getenv("SPOT_SL_PCT", "0.5"))
SPOT_COOLDOWN_SEC  = int(os.getenv("SPOT_COOLDOWN_SEC", "900"))
SPOT_MAX_OPEN      = int(os.getenv("SPOT_MAX_OPEN", "2"))


# ── As-If Order Tickets / Live Spot toggle ───────────────────────────────────
AS_IF_TICKETS_ENABLED   = os.getenv("AS_IF_TICKETS_ENABLED", "true").lower() == "true"
AS_IF_RISK_PCT          = float(os.getenv("AS_IF_RISK_PCT", "0.5"))      # % of USD balance at risk if SL hit
AS_IF_MAX_TRADE_USD     = float(os.getenv("AS_IF_MAX_TRADE_USD", "250")) # cap per symbol
AS_IF_MIN_TRADE_USD     = float(os.getenv("AS_IF_MIN_TRADE_USD", "25"))  # floor
AS_IF_LIVE_SPOT_DEFAULT = os.getenv("AS_IF_LIVE_SPOT_DEFAULT", "false").lower() == "true"


# Watchlist
symbols_to_watch = ["BTC-USD","SOL-USD", "ETH-USD", "XRP-USD"]
TEST_SYMBOL      = "BTC-USD"

# Perp alert parameters (alerts only; no live orders unless AUTO_TRADE_ENABLED)
PERP_TP_PCT         = float(os.getenv("PERP_TP_PCT", "1.2"))
PERP_SL_PCT         = float(os.getenv("PERP_SL_PCT", "0.6"))
SIGNAL_COOLOFF_SEC  = int(os.getenv("SIGNAL_COOLOFF_SEC", "900"))

# Liquidity guardrails (imbalance + spread)
_LIQ_IMB_LONG_MIN  = float(os.getenv("LIQ_IMB_LONG_MIN", "0.05"))  # need +5% bid tilt for longs
_LIQ_IMB_SHORT_MAX = float(os.getenv("LIQ_IMB_SHORT_MAX", "-0.05")) # need -5% ask tilt for shorts
_LIQ_MAX_SPR_BTC = float(os.getenv("LIQ_MAX_SPR_BTC", "5.0"))
_LIQ_MAX_SPR_ETH = float(os.getenv("LIQ_MAX_SPR_ETH", "2.5"))
_LIQ_MAX_SPR_XRP = float(os.getenv("LIQ_MAX_SPR_XRP", "0.002"))
print("[LIQ] Liquidity guardrails active:", flush=True)
print(f"     • Long trades only trigger when buyers show at least {_LIQ_IMB_LONG_MIN*100:.1f}% stronger bids than asks.", flush=True)
print(f"     • Short trades only trigger when sellers show at least {abs(_LIQ_IMB_SHORT_MAX)*100:.1f}% stronger asks than bids.", flush=True)
print(f"     • Max spreads allowed: BTC ≤ ${_LIQ_MAX_SPR_BTC}, ETH ≤ ${_LIQ_MAX_SPR_ETH}, XRP ≤ ${_LIQ_MAX_SPR_XRP}", flush=True)
print("     → This avoids weak setups on balanced books or illiquid spikes.", flush=True)

# Anti‑whipsaw (momentum)
MOM_UP_ENTER = 0.25
MOM_UP_EXIT  = 0.10
MOM_DN_ENTER = -0.25
MOM_DN_EXIT  = -0.10
CONFIRM_TICKS   = 3
FLIP_GUARD_SEC  = 120
MIN_MOVE_BPS    = 5
MAX_SPREAD_BPS  = 2
ATR_WINDOW      = 12
MIN_ATR_BPS     = 3

# Liquidity magnet / sweep heuristics (used in notes)
MAGNET_MAX_BPS     = 30
ROUND_STEP = {"BTC":100.0, "ETH":10.0, "XRP":0.01}
SWEEP_LOOKBACK_SEC = 45*60
SWEEP_WICK_BPS     = 8
SWEEP_CONFIRM_SEC  = 45
W_IMB, W_MAG, W_SWEEP = 0.45, 0.30, 0.25

# News pause (optional)
NEWS_FILTER_ENABLED = True
NEWS_API_URL = "https://cryptopanic.com/api/v1/posts/?auth_token=demo&filter=important"
NEWS_COOLDOWN = 180

# Risk sizing
FAST_BREAKDOWN_CHECK_INTERVAL = 2
TRADE_AMOUNT_RISK_PERCENT = 1
MIN_CONTRACTS = 1
MAX_DAILY_LOSS = -500
RISK_THROTTLE_LOSSES = 2


# ──────────────────────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────────────────────
last_sent_time: dict = {}
open_trades, daily_pnl = {}, {}
loss_streak = 0
risk_scale  = 1.0
last_mode = None
last_news_check = 0
pause_trades_until = 0
manual_paused = False

# — Morning overview state —
_DAILY_OPEN = {}             # symbol -> price at UTC day open
_DAILY_OPEN_DATE = None      # datetime.date of the open snapshot
LAST_SIGNALS = deque(maxlen=200)
_PRICE_HISTORY = defaultdict(lambda: deque(maxlen=12))
_TICKS = {}  # sym -> deque[(ts, price)] for swing detection
_recent_prices = {s: deque(maxlen=60) for s in SPOT_SYMBOLS}
_macro_alert_sent = set()  # {(event_key, tag)}
_desk_prev_state = {}      # sym -> state dict
_desk_last_flip_ts = defaultdict(float)

# Spot state
spot_positions = {}        # sym -> list[{entry, qty_base, tp, sl, opened_ts, buy_order_id, fee_pct}]
_last_spot_entry_ts = {}   # sym -> ts

# warn-once helper
_warned = set()

def warn_once(key: str, msg: str):
    if key in _warned:
        return
    _warned.add(key)
    print(msg, flush=True)
    send_telegram(f"⚠️ {msg}")

# ──────────────────────────────────────────────────────────────────────────────
# Telegram helpers
# ──────────────────────────────────────────────────────────────────────────────

def _tg_post(payload: dict):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        r = requests.post(url, data=payload, timeout=8)
        if r.status_code != 200:
            print(f"[TG] send error {r.status_code}: {r.text[:300]}", flush=True)
        return r
    except requests.exceptions.RequestException as e:
        print(f"[TG] send exception: {e}", flush=True)
        return None

def _tg_send_chunked(text: str, parse_mode: Optional[str] = "HTML", chunk_size: int = 3500):
    """
    Telegram hard limit ~4096 chars; we chunk to 3500 to be safe.
    If HTML fails (bad tag), we retry the SAME chunk without parse_mode.
    """
    if not TOKEN or not CHAT_ID:
        return

    try_html = bool(parse_mode)
    start = 0
    n = len(text)

    while start < n:
        part = text[start:start + chunk_size]

        # If we’re sending as HTML, escape reserved chars first
        part_to_send = html.escape(part, quote=False) if try_html else part

        payload = {"chat_id": CHAT_ID, "text": part_to_send}
        if try_html:
            payload["parse_mode"] = parse_mode

        r = _tg_post(payload)

        # If Telegram says “can't parse entities…”, resend this same chunk as plain text
        if r is not None and r.status_code == 400 and try_html:
            try_html = False
            # re-send same slice without advancing 'start'
            continue

        # advance to next chunk
        start += chunk_size

def send_telegram(text: str):
    # Use the chunked sender everywhere
    try:
        _tg_send_chunked(text, parse_mode="HTML", chunk_size=3500)
    except Exception as e:
        print(f"[TG] exception: {e}", flush=True)


def send_telegram_throttled(key: str, text: str, cooldown: int = 300):
    now = time.time()
    if key in last_sent_time and (now - last_sent_time[key]) < cooldown:
        return
    send_telegram(text)
    last_sent_time[key] = now

# ──────────────────────────────────────────────────────────────────────────────
# Telegram webhook clear (required when using long-polling)
# ──────────────────────────────────────────────────────────────────────────────
def _telegram_clear_webhook():
    if not TOKEN:
        return
    try:
        # If a webhook is set, getUpdates (long polling) won’t receive messages.
        requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook", timeout=8)
        info = requests.get(f"https://api.telegram.org/bot{TOKEN}/getWebhookInfo", timeout=8).json()
        print(f"[TG] Webhook cleared. getWebhookInfo: {info}", flush=True)
    except Exception as e:
        print(f"[TG] Webhook clear error: {e}", flush=True)

# ──────────────────────────────────────────────────────────────────────────────
# Liquidity helpers
# ──────────────────────────────────────────────────────────────────────────────
# Liquidity helpers
# ──────────────────────────────────────────────────────────────────────────────

def _lookup_symbols_for(sym: str):
    a = sym.split("-")[0]
    return [f"BINANCEFUT:{a}USDT", f"COINBASE:{a}-USD", f"{a}USDT", f"{a}-USD"]

def _fmt_compact(n: float) -> str:
    n = float(n); a = abs(n)
    if a >= 1e9: return f"{n/1e9:.2f}B"
    if a >= 1e6: return f"{n/1e6:.2f}M"
    if a >= 1e3: return f"{n/1e3:.1f}k"
    return f"{n:.0f}"

# ──────────────────────────────────────────────────────────────────────────────
# Spread formatting helpers (prefer bps when price is known)
# ──────────────────────────────────────────────────────────────────────────────

def _is_fx_pair(sym: str) -> bool:
    base = sym.split("-")[0].upper()
    # Crypto majors that benefit from bps display at higher prices
    return base in {"BTC", "SOL"}

def _fmt_spread_bps(price: float, spr_abs: float) -> str:
    # If "spr" is absolute (quote units), convert to basis points via price
    try:
        if not price or price <= 0:
            return f"{spr_abs:.6f}"
        bps = (float(spr_abs) / float(price)) * 1e4
        return f"{bps:.1f} bps"
    except Exception:
        return str(spr_abs)

# def _fmt_spread(spr: float, sym: str, price: float = None) -> str:
    # """
    # If we know current price, show spread in bps; otherwise fall back to absolute.
    # For BTC/ETH (large notional), bps is usually more readable intraday.
    # """
    # try:
        # if price is not None and price > 0:
            # return _fmt_spread_bps(float(price), float(spr))
        # return f"{spr:.6f}"
    # except Exception:
        # return str(spr)


def _liq_snapshot_brief(sym: str, px: float) -> Tuple[str, float, float, str]:
    """
    Returns (brief_text, imb, spr_bps, venue)
    NOTE: spr here is BPS already — we format it directly (no _fmt_spread).
    """
    ok, imb, spr_bps, venue = _liquidity_gate(sym, "LONG")
    txt = f"{(venue or '—')} | imb {imb:+.2f} | spr {abs(float(spr_bps)):.1f} bps"
    return txt, float(imb), float(spr_bps), str(venue or "—")



def _asset_from_sym(sym: str) -> str:
    return sym.split("-", 1)[0].upper()

def _imb_gauge(imb: float) -> str:
    blocks = 5
    idx = int(round((imb + 1.0) * 0.5 * blocks))
    idx = max(0, min(blocks, idx))
    return "█" * idx + "░" * (blocks - idx)

def _open_json(path: str):
    try:
        if _oj:
            with open(path, "rb") as f:
                return _oj.loads(f.read())
        else:
            with open(path, "r") as f:
                return json.load(f)
    except Exception:
        return None

def _liq_snapshot_age_sec(path: str = None) -> Optional[float]:
    """
    Returns snapshot file age in seconds, or None when unavailable.
    """
    p = path or _LIQ_PATH
    try:
        return max(0.0, time.time() - os.path.getmtime(p))
    except Exception:
        return None

def _liq_data_state(path: str = None) -> tuple:
    """
    Deterministic liquidity data state.
    Returns: (state, age_sec, reason)
      - state: "fresh" | "stale"
    """
    age = _liq_snapshot_age_sec(path or _LIQ_PATH)
    if age is None:
        return "stale", None, "liq-missing"
    if age > LIQ_MAX_AGE_SEC:
        return "stale", age, f"liq-stale:{age:.1f}s>{LIQ_MAX_AGE_SEC:.1f}s"
    return "fresh", age, "ok"


def _venue_btc_usd_keys() -> tuple:
    """Deterministic snapshot keys written by liquidity_phase1_free.py."""
    return ("BINANCEFUT:BTCUSDT", "COINBASE:BTC-USD")


def _venue_divergence_state(sym: str, snap: dict = None) -> dict:
    """
    Compare venue mid prices from the liquidity snapshot (basis points).
    Scope: BTC-USD only (Binance USDT-perp vs Coinbase spot USD).
    Returns machine-readable trust/quality state for decisions and tests.
    """
    sym_u = (sym or "").upper().strip()
    out: Dict[str, Any] = {
        "enabled": bool(VENUE_DIV_ENABLED),
        "symbol": sym_u,
        "status": "skipped",
        "divergence_bps": None,
        "venues": {},
        "block_decisions": False,
        "conf_factor": 1.0,
        "reason": "",
        "warn_bps": float(VENUE_DIV_BPS_WARN),
        "hard_bps": float(VENUE_DIV_BPS_HARD),
    }
    if not VENUE_DIV_ENABLED:
        out["reason"] = "venue-div:disabled"
        return out
    if sym_u != "BTC-USD":
        out["reason"] = "venue-div:scope-BTC-USD-only"
        return out

    snap = snap if isinstance(snap, dict) else (_open_json(_LIQ_PATH) or {})
    symbols = snap.get("symbols", {}) or {}
    k_bin, k_cb = _venue_btc_usd_keys()
    r_bin = symbols.get(k_bin) or {}
    r_cb = symbols.get(k_cb) or {}
    try:
        m_bin = float(r_bin.get("mid", 0.0) or 0.0)
    except Exception:
        m_bin = 0.0
    try:
        m_cb = float(r_cb.get("mid", 0.0) or 0.0)
    except Exception:
        m_cb = 0.0
    out["venues"] = {k_bin: m_bin, k_cb: m_cb}

    if m_bin <= 0.0 or m_cb <= 0.0:
        out["status"] = "incomplete"
        out["block_decisions"] = True
        out["reason"] = "venue-div:incomplete-mids"
        return out

    mid_ref = 0.5 * (m_bin + m_cb)
    div_bps = abs(m_bin - m_cb) / max(mid_ref, 1e-9) * 10000.0
    out["divergence_bps"] = float(div_bps)

    if div_bps > VENUE_DIV_BPS_HARD:
        out["status"] = "hard"
        out["block_decisions"] = True
        out["reason"] = f"venue-div:{div_bps:.1f}bps>{VENUE_DIV_BPS_HARD:.0f}bps-hard"
        return out
    if div_bps > VENUE_DIV_BPS_WARN:
        out["status"] = "warn"
        out["conf_factor"] = float(VENUE_DIV_CONF_PENALTY)
        out["reason"] = f"venue-div:{div_bps:.1f}bps>{VENUE_DIV_BPS_WARN:.0f}bps-warn"
        return out

    out["status"] = "ok"
    out["reason"] = "venue-div:ok"
    return out


def _coinbase_feed_health(snap: dict = None) -> str:
    """
    Non-blocking trust annotation written by liquidity collector.
    Returns one of: healthy_l2 | fallback_only | degraded.
    """
    snap = snap if isinstance(snap, dict) else (_open_json(_LIQ_PATH) or {})
    meta = snap.get("meta", {}) if isinstance(snap, dict) else {}
    h = str(meta.get("coinbase_feed_health", "")).strip().lower()
    if h in {"healthy_l2", "fallback_only", "degraded"}:
        return h
    return "degraded"


def _coinbase_feed_meta(snap: dict = None) -> dict:
    """
    Deterministic, BTC-focused Coinbase feed trust meta from liquidity snapshot.
    """
    snap = snap if isinstance(snap, dict) else (_open_json(_LIQ_PATH) or {})
    meta = snap.get("meta", {}) if isinstance(snap, dict) else {}
    h = _coinbase_feed_health(snap)
    return {
        "coinbase_feed_health": h,
        "coinbase_l2_active": bool(meta.get("coinbase_l2_active", False)),
        "coinbase_ticker_fallback_active": bool(meta.get("coinbase_ticker_fallback_active", False)),
        "coinbase_btc_row_valid": bool(meta.get("coinbase_btc_row_valid", False)),
        "collector_build": str(meta.get("collector_build", "")),
    }


def _liquidity_bookview(sym: str):
    snap = _open_json(_LIQ_PATH) or {}
    allrows = snap.get("symbols", {}) if isinstance(snap, dict) else {}
    out = {}
    for k in _lookup_symbols_for(sym):
        row = allrows.get(k)
        if row:
            out[k] = row
    return out

def _liquidity_note(sym: str, mode: str = "standard") -> str:
    """
    Human-friendly liquidity note.
    mode:
      - "standard": detailed, multi-line explanation
      - "intraday": concise, trader-facing one-liner
    """
    snap = _open_json(_LIQ_PATH)
    if not isinstance(snap, dict):
        return ""

    keys = _lookup_symbols_for(sym)
    row = next((snap.get("symbols", {}).get(k) for k in keys if snap.get("symbols", {}).get(k)), None)
    if not row:
        return ""

    imb   = float(row.get("imbalance", 0.0))            # -1 .. +1
    bid10 = float(row.get("cum_bid10", 0.0))
    ask10 = float(row.get("cum_ask10", 0.0))
    spr   = float(row.get("spread", 0.0))
    # Support both liquidity snapshot schemas:
    # - v1: nearest_bid_wall / nearest_ask_wall
    # - v2: nearest_bid_wall_price / nearest_ask_wall_price
    n_ask = float(row.get("nearest_ask_wall", 0.0) or row.get("nearest_ask_wall_price", 0.0) or 0.0)
    n_bid = float(row.get("nearest_bid_wall", 0.0) or row.get("nearest_bid_wall_price", 0.0) or 0.0)
    venue = row.get("venue", "")
    asset = _asset_from_sym(sym)

    # Try to fetch current spot price so we can render spread in bps
    try:
        price = float(get_spot_price(sym) or 0.0)
    except Exception:
        price = 0.0
    spr_txt = _fmt_spread(spr, sym, price)

    # Tilt & gauge
    tilt  = "Buy side stronger" if imb > 0.02 else ("Sell side stronger" if imb < -0.02 else "Balanced")
    gauge = _imb_gauge(imb)

    # Bias message
    if imb >= 0.30:
        bias_hint = "🚀 Strong Buy Support"
    elif imb <= -0.30:
        bias_hint = "🔻 Strong Sell Pressure"
    elif abs(imb) >= 0.15:
        bias_hint = "⚠️ Moderate Imbalance"
    else:
        bias_hint = "≋ Neutral / Two-way"

    # Spread label (clearer than raw decimals)
    spr_abs = abs(spr)
    if spr_abs <= 0.0005:
        spr_label = "Ultra-tight"
    elif spr_abs <= 0.002:
        spr_label = "Tight"
    elif spr_abs <= 0.01:
        spr_label = "Normal"
    else:
        spr_label = "Wide"

    # --- Intraday (one-liner) ---
    if mode == "intraday":
        parts = [
            f"\n🧭 {venue or '—'} | {bias_hint} | {tilt} ({imb:+.0%}) {gauge} | Spread: {spr_label} ({spr_txt})"
        ]
        walls = []
        if n_bid: walls.append(f"Bid wall ≈ {n_bid:.4f}")
        if n_ask: walls.append(f"Ask wall ≈ {n_ask:.4f}")
        if walls: parts.append(" • " + " / ".join(walls))
        return "".join(parts)

    # --- Standard (multi-line) ---
    lines = [
        f"\n🧭 Liquidity [{venue or '—'}]",
        f"Tilt: {tilt} ({imb:+.1%}) {gauge}",
        f"Depth ±10bps: Bids {_fmt_compact(bid10)} {asset} / Asks {_fmt_compact(ask10)} {asset}",
        f"Spread: {spr_label} (≈{spr_txt})",
    ]
    if n_bid or n_ask:
        wall_bits = []
        if n_bid: wall_bits.append(f"↘ Bid wall ≈ {n_bid:.4f}")
        if n_ask: wall_bits.append(f"↗ Ask wall ≈ {n_ask:.4f}")
        lines.append(" • " + " | ".join(wall_bits))
    lines.append(f"Signal: {bias_hint}")
    return "\n".join(lines)


#_last_wall_ping = {}  # (sym, side, level_rounded) -> ts
#_prev_walls = {}      # sym -> {"bid":[(lvl,sz)], "ask":[(lvl,sz)]}

def _fmt_bps(d_price: float, price: float) -> float:
    return abs(d_price) / max(price, 1e-9) * 10000.0



#cool function and these below were delated




def _liquidity_gate(sym: str, bias: str):
    # Hard freshness guard: stale -> no decision/execution.
    state, age, reason = _liq_data_state(_LIQ_PATH)
    if state != "fresh":
        return False, 0.0, 0.0, reason

    snap = _open_json(_LIQ_PATH)
    if not isinstance(snap, dict):
        return False, 0.0, 0.0, "liq-invalid"
    symbols = snap.get("symbols", {})
    row = next((symbols.get(k) for k in _lookup_symbols_for(sym) if symbols.get(k)), None)
    if not row:
        return False, 0.0, 0.0, "liq-empty"

    # imbalance ∈ [-1, +1]
    try:
        imb = float(row.get("imbalance", 0.0))
    except Exception:
        imb = 0.0
    imb = max(-1.0, min(1.0, imb))

    venue = row.get("venue", "") or "—"

    # --- spread → bps (ALWAYS POSITIVE) ---
    spr_bps = None
    try:
        if "spread_bps" in row and row["spread_bps"] is not None:
            spr_bps = float(row["spread_bps"])
        else:
            # If only absolute spread is present, convert to bps using current price.
            spr_abs = float(row.get("spread", 0.0) or 0.0)
            px = get_spot_price(sym) or get_futures_price(sym)
            px = float(px) if px else 0.0
            spr_bps = (abs(spr_abs) / max(px, 1e-9)) * 1e4 if px > 0 else abs(spr_abs)
    except Exception:
        spr_bps = 0.0

    spr_bps = abs(float(spr_bps))  # normalize

    # --- venue divergence (BTC-USD): hard/incomplete blocks decisions; warn handled via confidence ---
    vd = _venue_divergence_state(sym, snap)
    if vd.get("block_decisions"):
        return False, imb, spr_bps, str(vd.get("reason") or "venue-div-block")

    # --- directional imbalance gates ---
    if bias == "LONG" and imb < _LIQ_IMB_LONG_MIN:
        return False, imb, spr_bps, venue
    if bias == "SHORT" and imb > _LIQ_IMB_SHORT_MAX:
        return False, imb, spr_bps, venue

    # --- spread gate (bps) ---
    if spr_bps > MAX_SPREAD_BPS:
        return False, imb, spr_bps, venue

    return True, imb, spr_bps, venue


def _liquidity_ta(sym: str) -> str:
    snap = _open_json(_LIQ_PATH)
    if not isinstance(snap, dict):
        return ""
    symbols = snap.get("symbols", {})
    row = next((symbols.get(k) for k in _lookup_symbols_for(sym) if symbols.get(k)), None)
    if not row: return ""

    imb   = float(row.get("imbalance", 0.0))
    bid10 = float(row.get("cum_bid10", 0.0))
    ask10 = float(row.get("cum_ask10", 0.0))
    spr   = float(row.get("spread", 0.0))
    venue = row.get("venue", "")

    total = bid10 + ask10
    if total <= 0: return f"\n🔎 [{venue}] Book empty; skip."

    dom_side  = "bids" if imb > 0 else ("asks" if imb < 0 else "balanced")
    stronger  = max(bid10, ask10)
    weaker    = max(min(bid10, ask10), 1e-9)
    dominance = stronger / weaker

    if abs(imb) >= 0.25 and dominance >= 1.5:
        bias_line = f"likely {'up' if imb>0 else 'down'}side sweep"
    elif abs(imb) >= 0.10 and dominance >= 1.25:
        bias_line = f"risk of {'up' if imb>0 else 'down'}side sweep"
    else:
        bias_line = "two-way chop likely"

    return (f"\n🔎 [{venue}] {dom_side.capitalize()} dominate ({imb*100:+.0f}%, {dominance:.1f}x); "
            f"spr={spr:.6f}; {bias_line}.")



def _pick_walls(sym: str):
    """
    Returns (venue, bid_wall_px, bid_depth_proxy, ask_wall_px, ask_depth_proxy, spread_abs)
    Depth proxy uses ±10bps cum volume (bids/asks) as a stable proxy since the snapshot may not have wall sizes.
    """
    snap = _open_json(_LIQ_PATH)
    if not isinstance(snap, dict):
        return "", None, 0.0, None, 0.0, None

    row = next(
        (snap.get("symbols", {}).get(k)
         for k in _lookup_symbols_for(sym)
         if snap.get("symbols", {}).get(k)),
        None
    )
    if not row:
        return "", None, 0.0, None, 0.0, None

    venue = row.get("venue", "")
    bid_px = row.get("nearest_bid_wall") or row.get("nearest_bid_wall_price")
    ask_px = row.get("nearest_ask_wall") or row.get("nearest_ask_wall_price")
    spr_abs = row.get("spread")

    # depth proxies
    bid_depth = float(row.get("cum_bid10", 0.0) or 0.0)
    ask_depth = float(row.get("cum_ask10", 0.0) or 0.0)

    try:
        bid_px = float(bid_px) if bid_px is not None else None
    except Exception:
        bid_px = None
    try:
        ask_px = float(ask_px) if ask_px is not None else None
    except Exception:
        ask_px = None
    try:
        spr_abs = float(spr_abs) if spr_abs is not None else None
    except Exception:
        spr_abs = None

    return venue, bid_px, bid_depth, ask_px, ask_depth, spr_abs


#was deleated #def _format_liq_for_user(sym: str, last_price: float) -> str:


# was delated - def _wall_watch(sym: str, last_price: float):
    
# ──────────────────────────────────────────────────────────────────────────────
# Momentum & signal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _push_tick(sym: str, price: float, ts: float = None, keep_sec: int = 3600):
    if price is None: return
    ts = ts or time.time()
    dq = _TICKS.setdefault(sym, deque())
    dq.append((ts, float(price)))
    cut = ts - keep_sec
    while dq and dq[0][0] < cut:
        dq.popleft()


def _recent_swing(sym: str, lookback_sec: int):
    dq = _TICKS.get(sym, ())
    if not dq: return None, None, None, None
    now = time.time()
    lo_px, lo_ts = float("inf"), None
    hi_px, hi_ts = -float("inf"), None
    for ts, px in dq:
        if ts >= now - lookback_sec:
            if px < lo_px: lo_px, lo_ts = px, ts
            if px > hi_px: hi_px, hi_ts = px, ts
    return (lo_px if lo_ts else None), lo_ts, (hi_px if hi_ts else None), hi_ts


def _nearby_rounds(sym: str, price: float):
    asset = _asset_from_sym(sym)
    step = ROUND_STEP.get(asset, 1.0)
    if step <= 0: return []
    base = round(price / step) * step
    cands = [base - step, base, base + step]
    out = []
    for lv in cands:
        bps = abs(lv - price) / max(price, 1e-9) * 10000.0
        if bps <= MAGNET_MAX_BPS:
            out.append((float(lv), "round"))
    return out


def _slope_percent(prices: deque) -> float:
    if len(prices) < 8: return 0.0
    half = len(prices)//2
    older = list(prices)[:half]; newer = list(prices)[half:]
    oa = sum(older)/max(half,1); na = sum(newer)/max(len(prices)-half,1)
    if oa <= 0: return 0.0
    return (na/oa - 1.0) * 100.0


def _atr_bps(prices: deque) -> float:
    arr = list(prices)[-ATR_WINDOW:] if len(prices) >= 2 else list(prices)
    if len(arr) < 2: return 0.0
    trs = [abs(arr[i]-arr[i-1]) / max(arr[i-1],1e-9) for i in range(1,len(arr))]
    return (sum(trs)/len(trs)) * 10000.0


def _momentum_side(prices: deque, up_thresh=0.20, down_thresh=-0.20):
    """
    Simple slope-based momentum (% change newer half vs older half avg).
    Returns "LONG", "SHORT", or None.
    Spot Autopilot only opens LONG, but we keep SHORT for symmetry.
    """
    if len(prices) < 8:
        return None
    half = len(prices) // 2
    older = list(prices)[:half]
    newer = list(prices)[half:]
    older_avg = sum(older) / max(half, 1)
    newer_avg = sum(newer) / max(len(prices) - half, 1)
    if older_avg <= 0:
        return None
    slope_pct = (newer_avg / older_avg - 1.0) * 100.0
    if slope_pct >= up_thresh:
        return "LONG"
    if slope_pct <= down_thresh:
        return "SHORT"
    return None


def _spread_bps(bid: float, ask: float) -> float:
    mid = 0.5*(bid+ask)
    if mid <= 0: return 9999.0
    return ((ask-bid)/mid) * 10000.0

_sig_state = {}
_confirm_buf = {}


def robust_momentum_signal(sym: str, prices: deque, bid: float, ask: float) -> Optional[str]:
    # Guards
    if _spread_bps(bid, ask) > MAX_SPREAD_BPS:
        return None
    if _atr_bps(prices) < MIN_ATR_BPS:
        return None

    slope = _slope_percent(prices)
    buf = _confirm_buf.setdefault(sym, deque(maxlen=CONFIRM_TICKS))
    buf.append(slope)

    up_ok   = all(v >= MOM_UP_ENTER for v in buf)
    down_ok = all(v <= MOM_DN_ENTER for v in buf)

    st  = _sig_state.setdefault(sym, {"side": None, "last_ts": 0.0, "last_px": 0.0})
    now = time.time()
    mid = 0.5 * (bid + ask)

    if st["side"] == "LONG":
        if slope >= MOM_UP_EXIT:
            if abs(mid - st["last_px"]) / max(st["last_px"], 1e-9) * 10000.0 >= MIN_MOVE_BPS:
                st["last_px"] = mid; st["last_ts"] = now
            return None
        if (now - st["last_ts"]) >= FLIP_GUARD_SEC and down_ok:
            st.update({"side":"SHORT", "last_ts":now, "last_px":mid})
            return "SHORT"
        return None

    if st["side"] == "SHORT":
        if slope <= MOM_DN_EXIT:
            if abs(mid - st["last_px"]) / max(st["last_px"], 1e-9) * 10000.0 >= MIN_MOVE_BPS:
                st["last_px"] = mid; st["last_ts"] = now
            return None
        if (now - st["last_ts"]) >= FLIP_GUARD_SEC and up_ok:
            st.update({"side":"LONG", "last_ts":now, "last_px":mid})
            return "LONG"
        return None

    if up_ok:
        st.update({"side":"LONG", "last_ts":now, "last_px":mid}); return "LONG"
    if down_ok:
        st.update({"side":"SHORT", "last_ts":now, "last_px":mid}); return "SHORT"
    return None


_LAST_SIGNAL_TS = defaultdict(float)


def _record_signal(**item):
    item = {
        "ts": int(time.time()),
        **item,
    }
    LAST_SIGNALS.appendleft(item)


def _suggest_levels(entry: float, tp_pct: float, sl_pct: float, side: str):
    if side == "LONG":
        tp = entry * (1 + tp_pct/100.0); sl = entry * (1 - sl_pct/100.0)
    else:
        tp = entry * (1 - tp_pct/100.0); sl = entry * (1 + sl_pct/100.0)
    return round(tp, 2), round(sl, 2)


def maybe_emit_perp_signal(sym: str, price: float, bid: float = None, ask: float = None):
    if not FUTURES_SIGNALS_ENABLED or price is None:
        return
    now = time.time()
    if (now - _LAST_SIGNAL_TS[sym]) < SIGNAL_COOLOFF_SEC:
        return

    _PRICE_HISTORY[sym].append(float(price))
    if len(_PRICE_HISTORY[sym]) < 8:
        return

    # Use zero-width synthetic spread to avoid tripping MAX_SPREAD_BPS
    if bid is None or ask is None:
        bid = ask = float(price)

    side = robust_momentum_signal(sym, _PRICE_HISTORY[sym], bid, ask)
    if not side:
        return

    ok, imb, spr, venue = _liquidity_gate(sym, side)
    if not ok:
        send_telegram(
            f"⏸ Skipped {sym} {side} — weak liquidity (imb={imb:+.2f}, spr={spr:.6f} [{venue}])"
        )
        return

    tp, sl = _suggest_levels(float(price), PERP_TP_PCT, PERP_SL_PCT, side)
    liq = _liquidity_note(sym)
    liq_ta = _liquidity_ta(sym)

    msg = (
        f"📈 PERP Signal {sym}\n"
        f"Side: {side}\n"
        f"Entry: {price:.2f}\n"
        f"TP: {tp:.2f}   SL: {sl:.2f}\n"
        f"(Alert only — no live orders)\n"
        f"TP {PERP_TP_PCT:.2f}% | SL {PERP_SL_PCT:.2f}% | Cooloff {SIGNAL_COOLOFF_SEC}s"
        f"{liq}{liq_ta}"
    )

    _record_signal(
        symbol=sym, side=side, entry=float(price), tp=tp, sl=sl,
        imbalance=float(imb), spread=float(spr), venue=venue or "",
        sentiment="n/a", sentiment_score=0.5, macro="none", confidence=0.5,
    )

    send_telegram(msg)
    _LAST_SIGNAL_TS[sym] = now

# ──────────────────────────────────────────────────────────────────────────────
# Spot autopilot (optional)
# ──────────────────────────────────────────────────────────────────────────────

def _spot_size_in_base(usd_notional: float, price: float) -> float:
    if price <= 0: return 0.0
    return round(usd_notional / price, 8)


def _spot_has_room(sym: str) -> bool:
    return len(spot_positions.get(sym, [])) < SPOT_MAX_OPEN


def _spot_try_open(sym: str, price: float, bias: str):
    if bias != "LONG":
        return

    _ok, _imb, _spr, _venue = _liquidity_gate(sym, "LONG")
    if not _ok:
        send_telegram(
            f"⏸️ Skipped SPOT BUY {sym}: liquidity filter (imb={_imb:+.2f}, spr={_spr:.6f} {_venue or ''})"
        )
        return

    now = time.time()
    if not _spot_has_room(sym):
        return
    last = _last_spot_entry_ts.get(sym, 0.0)
    if (now - last) < SPOT_COOLDOWN_SEC:
        return

    qty = _spot_size_in_base(SPOT_TRADE_USD, price)
    if qty <= 0:
        return

    tp = round(price * (1 + SPOT_TP_PCT/100.0), 2)
    sl = round(price * (1 - SPOT_SL_PCT/100.0), 2)
    per_side_fee = (SPOT_FEE_PCT/100.0)
    breakeven = round(price * (1.0 + 2 * per_side_fee), 2)
    tpd = (tp / price - 1.0) * 100.0
    sld = (1.0 - sl / price) * 100.0

    try:
        buy_resp = spot_market_buy(sym, SPOT_TRADE_USD)
        buy_order_id = (
            buy_resp.get("order_id")
            or buy_resp.get("orderId")
            or buy_resp.get("success_response", {}).get("order_id")
            or buy_resp.get("success", {}).get("order_id")
            or buy_resp.get("_order_id", "")
        )

        pos = {
            "entry": float(price),
            "qty_base": float(qty),
            "tp": tp,
            "sl": sl,
            "opened_ts": now,
            "fee_pct": SPOT_FEE_PCT,
            "buy_order_id": buy_order_id,
        }
        spot_positions.setdefault(sym, []).append(pos)
        _last_spot_entry_ts[sym] = now

        liq = _liquidity_note(sym, mode="intraday")
        send_telegram(
            f"✅ SPOT BUY {sym}\n"
            f"Qty≈{qty} @ {price}\n"
            f"TP={tp} (+{tpd:.2f}%) | SL={sl} ({sld:.2f}%)\n"
            f"Breakeven≈{breakeven} (incl est. {SPOT_FEE_PCT:.2f}%/side)\n"
            f"buy_order_id={buy_order_id or 'n/a'}"
            f"{liq}\n"
            f"🧭 Liquidity check → imbalance={_imb:+.2f}, spread={abs(_spr):.1f} bps {_venue or ''}"
        )
    except Exception as e:
        send_telegram(f"❌ Spot BUY error {sym}: {e}")


def _spot_manage_exits(sym: str, price: float):
    if sym not in spot_positions or not spot_positions[sym]:
        return

    remaining = []
    for lot in spot_positions[sym]:
        entry = float(lot["entry"]); qty = float(lot["qty_base"])\
        ; tp = float(lot["tp"]); sl = float(lot["sl"])

        hit_tp = price >= tp
        hit_sl = price <= sl
        if not (hit_tp or hit_sl):
            remaining.append(lot); continue

        try:
            sell_resp = spot_market_sell(sym, qty)
            sell_order_id = sell_resp.get("_order_id", "")

            buy_fills  = get_order_fills(lot.get("buy_order_id", "")) if lot.get("buy_order_id") else []
            sell_fills = get_order_fills(sell_order_id) if sell_order_id else []

            def _sum_cost(fills):
                cost, base = 0.0, 0.0
                for f in fills:
                    if (f.get("side") or "").upper() == "BUY":
                        cost += f["price"] * f["size"] + f.get("fee", 0.0)
                        base += f["size"]
                return cost, base

            def _sum_proceeds(fills):
                rev, base = 0.0, 0.0
                for f in fills:
                    if (f.get("side") or "").upper() == "SELL":
                        rev += f["price"] * f["size"] - f.get("fee", 0.0)
                        base += f["size"]
                return rev, base

            buy_cost_usdc, buy_base   = _sum_cost(buy_fills)
            sell_rev_usdc, sell_base  = _sum_proceeds(sell_fills)

            if buy_base <= 0:
                buy_cost_usdc = entry * qty; buy_base = qty
            if sell_base <= 0:
                sell_rev_usdc = price * qty; sell_base = qty

            qty_used = min(buy_base, sell_base, qty)
            avg_entry_usdc_per_base = buy_cost_usdc / max(buy_base, 1e-9)
            entry_cost_used = avg_entry_usdc_per_base * qty_used

            realized_pnl_usdc = sell_rev_usdc - entry_cost_used
            pct = ((sell_rev_usdc / max(entry_cost_used, 1e-9)) - 1.0) * 100.0

            tag = "TP" if hit_tp else "SL"
            emoji = "✅" if realized_pnl_usdc >= 0 else "❌"
            send_telegram(
                f"{emoji} SPOT EXIT {sym} ({tag})\n"
                f"Qty={qty_used}\n"
                f"PNL (net of actual fees) ≈ {realized_pnl_usdc:.2f} USDC  ({pct:+.2f}%)"
            )
        except Exception as e:
            remaining.append(lot)
            send_telegram(f"❌ Spot SELL error {sym}: {e}")

    spot_positions[sym] = remaining

# ──────────────────────────────────────────────────────────────────────────────
# News filter (optional)
# ──────────────────────────────────────────────────────────────────────────────

def classify_news_ai(title: str) -> bool:
    if not OPENAI_API_KEY:
        return False
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a trading risk filter AI."},
                {"role": "user", "content": f"Is this crypto news headline critical and likely to cause volatility? '{title}'. Answer ONLY yes or no."},
            ],
            max_tokens=5,
            temperature=0,
        )
        content = resp.choices[0].message.content or ""
        return "yes" in content.lower()
    except Exception:
        return False


def check_market_news():
    global pause_trades_until
    try:
        resp = requests.get(NEWS_API_URL, timeout=5)
        data = resp.json()
        headlines = [p.get("title", "") for p in data.get("results", [])]
        for title in headlines:
            if not title: continue
            if classify_news_ai(title):
                send_telegram(f"📰 AI News Alert: {title}\n⏸ Pausing trades for {NEWS_COOLDOWN//60} min")
                pause_trades_until = time.time() + NEWS_COOLDOWN
                break
    except Exception:
        pass


def news_allows_trade():
    return (not NEWS_FILTER_ENABLED) or (time.time() > pause_trades_until)

# ──────────────────────────────────────────────────────────────────────────────
# Macro calendar alerts (Telegram) with first-principles playbook
# ──────────────────────────────────────────────────────────────────────────────
def _parse_iso_utc(ts: str) -> Optional[datetime.datetime]:
    if not ts:
        return None
    try:
        t = str(ts).strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.astimezone(datetime.timezone.utc)
    except Exception:
        return None

def _macro_events_within(hours: int = 24):
    try:
        with open(MACRO_EVENTS_FILE, "r") as f:
            raw = json.load(f)
    except Exception:
        return []

    if isinstance(raw, dict):
        raw = raw.get("events", [])
    if not isinstance(raw, list):
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    horizon = now + datetime.timedelta(hours=max(1, int(hours)))
    out = []
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        when = _parse_iso_utc(ev.get("time_utc") or ev.get("time"))
        if not when:
            continue
        if now <= when <= horizon:
            out.append((when, ev))
    out.sort(key=lambda x: x[0])
    return out

def _macro_events_all():
    try:
        with open(MACRO_EVENTS_FILE, "r") as f:
            raw = json.load(f)
    except Exception:
        return []
    if isinstance(raw, dict):
        raw = raw.get("events", [])
    if not isinstance(raw, list):
        return []
    out = []
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        when = _parse_iso_utc(ev.get("time_utc") or ev.get("time"))
        if when:
            out.append((when, ev))
    out.sort(key=lambda x: x[0])
    return out

def _macro_recent_past(hours_back: int = 14):
    """
    Latest event from macro_events.json that already occurred (within last hours_back).
    Helps explain "we had claims today" even after the print time passed.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    start = now - datetime.timedelta(hours=max(1, int(hours_back)))
    best = None  # (when, ev)
    for when, ev in _macro_events_all():
        if start <= when <= now:
            if best is None or when > best[0]:
                best = (when, ev)
    return best

def _fmt_macro_eta_line(when: datetime.datetime, ev: dict, *, past: bool) -> str:
    title = ev.get("name") or ev.get("title") or "Macro Event"
    impact = str(ev.get("impact", "unknown")).upper()
    now = datetime.datetime.now(datetime.timezone.utc)
    delta = int(abs((when - now).total_seconds()) // 60)
    if past:
        return f"Macro (passed): {title} [{impact}] was {when.strftime('%a %Y-%m-%d %H:%M')} UTC (~{delta}m ago)."
    return f"Macro (next 24h): {title} [{impact}] at {when.strftime('%a %H:%M')} UTC in {delta}m."

def _macro_desk_context_lines():
    """
    Clear macro context for the desk: manual file disclaimer + past print + next 24h + next in file.
    """
    lines = [
        "Source: macro_events.json only (not a live economic calendar). Add dates you care about.",
    ]
    recent = _macro_recent_past(14)
    if recent:
        when, ev = recent
        lines.append(_fmt_macro_eta_line(when, ev, past=True))
    r24 = _macro_events_within(24)
    if r24:
        when, ev = r24[0]
        lines.append(_fmt_macro_eta_line(when, ev, past=False))
    else:
        lines.append("Macro (next 24h): none listed in JSON (still check real calendar).")
    # Next event overall (may be >24h) — compact
    all_up = [(w, e) for w, e in _macro_events_all() if w >= datetime.datetime.now(datetime.timezone.utc)]
    if all_up:
        when, ev = min(all_up, key=lambda x: x[0])
        title = ev.get("name") or ev.get("title") or "Event"
        mins = int((when - datetime.datetime.now(datetime.timezone.utc)).total_seconds() // 60)
        lines.append(f"Next in file: {title} in {max(0, mins)}m ({when.strftime('%a %H:%M')} UTC).")
    return lines

def _desk_vote_triple(mom_pct: float, imb: float, ta: dict) -> tuple:
    """Returns (mom_s, liq_s, ta_s) each in {-1,0,+1} matching _action_from_signals."""
    mom_s = 1 if mom_pct > 0.05 else (-1 if mom_pct < -0.05 else 0)
    liq_s = 1 if imb >= 0.10 else (-1 if imb <= -0.10 else 0)
    ta_s = 0
    if isinstance(ta, dict) and "headline" in ta:
        ta_s = 1 if "LONG" in ta["headline"] else (-1 if "SHORT" in ta["headline"] else 0)
    return mom_s, liq_s, ta_s

def _desk_vote_compact(mom_s: int, liq_s: int, ta_s: int) -> str:
    def ch(x: int) -> str:
        if x > 0:
            return "+"
        if x < 0:
            return "−"
        return "·"
    return f"{ch(mom_s)}{ch(liq_s)}{ch(ta_s)}"

def _desk_narrative_context(
    mom: float,
    imb: float,
    ta: Optional[dict],
    action_raw: str,
    decision: str,
    conf: int,
) -> Dict[str, str]:
    """
    Deterministic desk copy: cause language (no fake % odds).
    Thresholds align with _desk_vote_triple / _action_from_signals (mom ±0.05, liq ±0.10).
    """
    if abs(mom) <= 0.05:
        mtxt = "mixed / flat momentum"
    elif mom > 0.05:
        mtxt = "upward momentum"
    else:
        mtxt = "downward momentum"

    if abs(imb) < 0.05:
        ltxt = "balanced book (two-way)"
    elif abs(imb) < 0.10:
        ltxt = "mild bid support only" if imb > 0 else "mild ask pressure only"
    else:
        ltxt = "firm bid skew" if imb > 0 else "firm ask skew"

    if isinstance(ta, dict) and ta.get("headline") not in (None, "", "warming up"):
        pl = float(ta.get("prob_long", 50))
        if 42 < pl < 58:
            ttxt = "TA balanced / no edge"
        elif pl >= 58:
            ttxt = "TA leaning long"
        else:
            ttxt = "TA leaning short"
    else:
        ttxt = "TA still warming up"

    if decision == "WAIT":
        if action_raw in ("LONG?", "SHORT?"):
            stxt = "soft lean only — need aligned momentum + liquidity + close"
        elif conf < 20:
            stxt = "low conviction — waiting for expansion"
        else:
            stxt = "no breakout confirmation — need 5m close vs triggers"
    else:
        stxt = "directional setup active — manage risk to invalidation"

    driver_one = f"{mtxt}; {ltxt}; {ttxt}; {stxt}"
    return {
        "momentum_txt": mtxt,
        "liquidity_txt": ltxt,
        "ta_txt": ttxt,
        "setup_txt": stxt,
        "driver_line": driver_one,
    }


def _desk_wait_note(
    decision: str,
    action_raw: str,
    mom_s: int,
    liq_s: int,
    ta_s: int,
    conf: int,
    blocked: bool,
) -> str:
    if decision in ("LONG", "SHORT"):
        return "ok to consider" if conf >= LOW_CONF_THRESHOLD else f"edge weak (conf {conf}% < {LOW_CONF_THRESHOLD}%)"
    total = mom_s + liq_s + ta_s
    bits = []
    if blocked:
        bits.append("macro window")
    if action_raw in ("LONG?", "SHORT?"):
        bits.append("soft lean only")
    if abs(total) < 2:
        bits.append(f"votes {total:+d}/3 (need +2 LONG or −2 SHORT)")
    if conf < 20:
        bits.append("very low conf")
    elif conf < LOW_CONF_THRESHOLD:
        bits.append(f"conf {conf}% below {LOW_CONF_THRESHOLD}% for size")
    return "; ".join(bits) if bits else "mixed"

def _macro_bias_rules(event_name: str):
    n = (event_name or "").lower()
    if "jobless" in n or "initial claims" in n or "continuing claims" in n:
        return (
            "Claims HIGH (weaker labor) -> often dovish tilt -> BTC supportive reaction.",
            "Claims LOW (stronger labor) -> often hawkish hold -> BTC headwind.",
        )
    if "cpi" in n or "inflation" in n or "ppi" in n or "pce" in n:
        return (
            "Inflation HOTTER -> yields up / hawkish -> often BTC bearish.",
            "Inflation COOLER -> yields down / dovish -> often BTC bullish.",
        )
    if "nonfarm" in n or "nfp" in n or "payroll" in n:
        return (
            "Jobs STRONGER -> hotter econ -> hawkish bias -> often BTC bearish.",
            "Jobs WEAKER -> cooler econ -> dovish odds up -> often BTC bullish.",
        )
    if "unemployment" in n:
        return (
            "Unemployment HIGHER -> growth cooling -> dovish odds up -> often BTC supportive.",
            "Unemployment LOWER -> tighter labor -> hawkish tilt -> often BTC headwind.",
        )
    if "fomc" in n or "rate decision" in n or "fed" in n:
        return (
            "More HAWKISH tone -> usually BTC bearish.",
            "More DOVISH tone -> usually BTC bullish.",
        )
    if "retail sales" in n or "ism" in n or "pmi" in n:
        return (
            "Stronger growth print -> yields up / hawkish tilt -> often BTC bearish.",
            "Weaker growth print -> yields down / dovish tilt -> often BTC bullish.",
        )
    if "etf" in n and "flow" in n:
        return (
            "Stronger net inflows than trend -> spot demand tailwind -> often BTC supportive.",
            "Net outflows / weak flows -> demand air-pocket risk -> often BTC headwind.",
        )
    return (
        "Hotter macro -> USD/yields up -> often BTC risk-off first.",
        "Softer macro -> USD/yields down -> often BTC risk-on first.",
    )

def _fmt_macro_value(v, unit: str = "") -> str:
    if v is None or v == "":
        return "n/a"
    try:
        fv = float(v)
        if unit:
            return f"{fv:g}{unit}"
        return f"{fv:g}"
    except Exception:
        return f"{v}{unit or ''}"

def _macro_event_key(ev: dict, when: datetime.datetime) -> str:
    title = ev.get("name") or ev.get("title") or "Macro Event"
    return f"{title}|{when.isoformat()}"

def _macro_case_labels(event_name: str, has_expected: bool) -> tuple:
    n = (event_name or "").lower()
    if has_expected:
        return "If print > exp", "If print < exp"
    if "jobless" in n or "initial claims" in n or "continuing claims" in n:
        return "If claims HIGH", "If claims LOW"
    if "cpi" in n or "inflation" in n or "ppi" in n or "pce" in n:
        return "If inflation HOTTER", "If inflation COOLER"
    if "nonfarm" in n or "nfp" in n or "payroll" in n:
        return "If jobs STRONGER", "If jobs WEAKER"
    if "unemployment" in n:
        return "If unemployment HIGHER", "If unemployment LOWER"
    if "etf" in n and "flow" in n:
        return "If net inflows strong", "If net outflows strong"
    if "fomc" in n or "rate decision" in n or "fed" in n:
        return "If tone is more hawkish", "If tone is more dovish"
    return "If print is stronger", "If print is weaker"

def build_macro_calendar_brief(within_hours: int = 168, include_playbook: bool = True) -> str:
    rows = _macro_events_within(within_hours)
    if not rows:
        now = datetime.datetime.now(datetime.timezone.utc)
        future = [(w, ev) for (w, ev) in _macro_events_all() if w >= now]
        if not future:
            return (
                f"No upcoming macro events found in `{MACRO_EVENTS_FILE}`.\n"
                "Add future-dated events with `time_utc`, `impact`, and optional `expected/previous/actual/unit`."
            )
        when, ev = future[0]
        title = ev.get("name") or ev.get("title") or "Macro Event"
        expected = _fmt_macro_value(ev.get("expected"), ev.get("unit", ""))
        previous = _fmt_macro_value(ev.get("previous"), ev.get("unit", ""))
        up_case, down_case = _macro_bias_rules(title)
        mins = int((when - now).total_seconds() // 60)
        return (
            f"No macro events in next {within_hours}h.\n"
            f"Next scheduled: {title} at {when.strftime('%Y-%m-%d %H:%M')} UTC (in {max(0, mins)}m)\n"
            f"Expected: {expected} | Previous: {previous}\n"
            f"If print > exp: {up_case}\n"
            f"If print < exp: {down_case}"
        )

    now = datetime.datetime.now(datetime.timezone.utc)
    lines = [
        f"🗓️ Macro Calendar (next {within_hours}h, UTC)",
        "Note: data is from macro_events.json only — not a live Forex/econ feed. Add or edit events there.",
    ]
    for when, ev in rows:
        title = ev.get("name") or ev.get("title") or "Macro Event"
        impact = str(ev.get("impact", "unknown")).upper()
        mins = int((when - now).total_seconds() // 60)
        eta = "now" if mins <= 0 else f"in {mins}m"
        expected = _fmt_macro_value(ev.get("expected"), ev.get("unit", ""))
        previous = _fmt_macro_value(ev.get("previous"), ev.get("unit", ""))
        actual = _fmt_macro_value(ev.get("actual"), ev.get("unit", ""))
        has_expected = ev.get("expected") not in (None, "")
        lines.append(f"\n• {title} [{impact}]")
        lines.append(f"  Time: {when.strftime('%Y-%m-%d %H:%M')} UTC ({eta})")
        lines.append(f"  Expected: {expected} | Previous: {previous} | Actual: {actual}")
        if include_playbook:
            up_case, down_case = _macro_bias_rules(title)
            lbl_up, lbl_dn = _macro_case_labels(title, has_expected)
            lines.append(f"  {lbl_up}: {up_case}")
            lines.append(f"  {lbl_dn}: {down_case}")
            lines.append("  TA playbook: wait 1-5m candle close; trade only with liquidity tilt and momentum confirmation.")
    return "\n".join(lines)

def _next_macro_event_line(within_hours: int = 72) -> str:
    rows = _macro_events_within(within_hours)
    if not rows:
        return "Macro: no scheduled event in next 72h."
    now = datetime.datetime.now(datetime.timezone.utc)
    when, ev = rows[0]
    title = ev.get("name") or ev.get("title") or "Macro Event"
    mins = int((when - now).total_seconds() // 60)
    impact = str(ev.get("impact", "unknown")).upper()
    return f"Macro next: {title} [{impact}] in {max(0, mins)}m ({when.strftime('%a %H:%M')} UTC)."

def _desk_compute_symbol_state(sym: str, tf: str = "15m") -> dict:
    px = _safe_price(sym)
    if px <= 0:
        return {"symbol": sym, "ok": False, "reason": "no price"}

    _update_ta_buffer(sym, px)
    ta = _safe_ta_bias(sym)
    _ok_liq, imb, _spr, _venue = _safe_liq(sym)
    mom = _safe_hist_mom(sym)
    r, s = _safe_sr(sym, tf)
    blocked, macro_lbl, macro_factor = _macro_penalty(time.time(), sym)
    liq_state, liq_age, liq_reason = _liq_data_state(_LIQ_PATH)
    vd = _venue_divergence_state(sym)
    cb_health = _coinbase_feed_health()

    action_raw = _action_from_signals(mom, imb, ta or {})
    mom_s, liq_s, ta_s = _desk_vote_triple(mom, imb, ta or {})
    vote_sum = mom_s + liq_s + ta_s
    vote_compact = _desk_vote_compact(mom_s, liq_s, ta_s)

    conf = _conf_from_signals(mom, imb, ta or {})
    conf = int(round(conf * macro_factor))
    if liq_state != "fresh":
        conf = 0
    elif (sym or "").upper().strip() == "BTC-USD" and vd.get("status") == "warn":
        conf = int(round(conf * float(vd.get("conf_factor", 1.0))))

    if liq_state != "fresh":
        decision = "WAIT"
    elif (sym or "").upper().strip() == "BTC-USD" and vd.get("block_decisions"):
        decision = "WAIT"
    elif conf < 20 or action_raw in ("LONG?", "SHORT?"):
        decision = "WAIT"
    else:
        decision = "LONG" if action_raw.startswith("LONG") else ("SHORT" if action_raw.startswith("SHORT") else "WAIT")
    if decision in ("LONG", "SHORT") and conf < LOW_CONF_THRESHOLD:
        decision = "WAIT"

    wait_note = _desk_wait_note(decision, action_raw, mom_s, liq_s, ta_s, conf, blocked)
    if liq_state != "fresh":
        age_txt = "n/a" if liq_age is None else f"{liq_age:.1f}s"
        wait_note = f"stale liquidity data ({age_txt}); hard-stop decisions"
    elif (sym or "").upper().strip() == "BTC-USD" and vd.get("block_decisions"):
        wait_note = f"venue divergence guard — {vd.get('reason', 'blocked')}"
    elif (sym or "").upper().strip() == "BTC-USD" and cb_health == "degraded":
        wait_note = "coinbase feed quality degraded (informational; non-blocking)"

    _nctx = _desk_narrative_context(mom, imb, ta or {}, action_raw, decision, conf)
    driver = _nctx["driver_line"]

    # "Point no" / invalidation zone: ATR-based (PA-style kill level).
    atrp = _safe_atr_price(sym, px, tf)
    tp = None
    sl = None
    no_txt = "NO n/a"
    if decision == "LONG":
        sl = px - 1.00 * atrp
        tp = px + 1.50 * atrp
        no_txt = f"NO<{sl:.2f}"
    elif decision == "SHORT":
        sl = px + 1.00 * atrp
        tp = px - 1.50 * atrp
        no_txt = f"NO>{sl:.2f}"
    else:
        # When WAIT, show nearest structure as the practical "wrong-way" zone.
        if s is not None and r is not None:
            no_txt = f"S {s:.2f} / R {r:.2f}"
        elif s is not None:
            no_txt = f"S {s:.2f}"
        elif r is not None:
            no_txt = f"R {r:.2f}"

    return {
        "symbol": sym,
        "ok": True,
        "price": float(px),
        "decision": decision,
        "action_raw": action_raw,
        "confidence": int(conf),
        "momentum": float(mom),
        "imbalance": float(imb),
        "ta_headline": (ta.get("headline", "warming up") if isinstance(ta, dict) else "warming up"),
        "macro_label": macro_lbl,
        "macro_blocked": bool(blocked),
        "resistance": r,
        "support": s,
        "driver": driver,
        "vote_mlt": vote_compact,
        "vote_sum": int(vote_sum),
        "wait_note": wait_note,
        "liq_state": liq_state,
        "liq_age": liq_age,
        "liq_reason": liq_reason,
        "coinbase_feed_health": cb_health,
        "venue_div": vd,
        "atrp": float(atrp) if atrp is not None else 0.0,
        "tp": float(tp) if tp is not None else None,
        "sl": float(sl) if sl is not None else None,
        "no_txt": no_txt,
    }

def _desk_state_changed(prev: dict, cur: dict) -> bool:
    if not prev:
        return False
    if prev.get("decision") != cur.get("decision"):
        return True
    if abs(float(prev.get("confidence", 0)) - float(cur.get("confidence", 0))) >= 12:
        return True
    if bool(prev.get("macro_blocked")) != bool(cur.get("macro_blocked")):
        return True
    return False

def _desk_symbol_line(st: dict, prev: dict = None, *, include_note: bool = False) -> str:
    if not st.get("ok"):
        return f"• {st.get('symbol','?')}: no price."
    sym = st["symbol"]
    d = st["decision"]
    conf = st["confidence"]
    px = st["price"]
    mom = st["momentum"]
    imb = st["imbalance"]
    driver = st["driver"]
    vmlt = st.get("vote_mlt", "···")
    vs = st.get("vote_sum", 0)
    move_note = ""
    if prev:
        if prev.get("decision") != d:
            move_note = f" | flip {prev.get('decision','?')}→{d}"
        elif abs(float(prev.get("confidence", 0)) - conf) >= 8:
            sign = "↑" if conf > float(prev.get("confidence", 0)) else "↓"
            move_note = f" | conf {sign}"
    # vote_mlt = momentum/liquidity/TA: +/−/·  ; vote_sum ∈ [-3..+3], need ±2 for directional bias
    no_txt = st.get("no_txt", "NO n/a")
    row = (
        f"{sym} | {d:<4} | {conf:>3}% | {vmlt} sum{vs:+d} | px {px:.2f} | mom {mom:+.2f}% | liq {imb:+.0%}"
        f" | {driver}{move_note} | {no_txt}"
    )
    if include_note and st.get("wait_note"):
        row += f"\n   → {st['wait_note']}"
    return row

def _fmt_level(px: float) -> str:
    try:
        p = float(px)
    except Exception:
        return "n/a"
    return f"{p:.2f}" if abs(p) >= 1 else f"{p:.4f}"


def _plain_ta_macro_token(st: dict) -> str:
    if st.get("macro_blocked"):
        return "blocked"
    lbl = str(st.get("macro_label") or "").lower()
    if "soon" in lbl:
        return "soon"
    return "clear"


def _plain_ta_venue_token(st: dict) -> str:
    vd = st.get("venue_div") or {}
    if not vd.get("enabled"):
        return "n/a"
    s = str(vd.get("status") or "skipped")
    if s == "skipped":
        return "n/a"
    return s


def _plain_ta_regime_lines(
    decision: str,
    conf: int,
    mom: float,
    imb: float,
    ta_prob: float,
    liq_state: str,
) -> List[str]:
    """Institutional regime copy — no fake win-rate / odds language."""
    if liq_state != "fresh":
        return [
            "• Chop / range-bound (stale book — read is observational only)",
            "• No directional edge for execution until data is fresh",
        ]
    if decision in ("LONG", "SHORT") and conf >= LOW_CONF_THRESHOLD:
        return [
            f"• Directional — {decision} bias with desk conf {conf}%",
            "• Size only if your plan agrees; honor invalidation",
        ]
    if decision in ("LONG", "SHORT") and conf < LOW_CONF_THRESHOLD:
        return [
            "• Directional lean — conf below size gate",
            "• Low conviction until expansion + higher conf",
        ]
    if conf < 20 or (abs(mom) <= 0.05 and abs(imb) < 0.10):
        return [
            "• Chop / range-bound",
            "• Low conviction until expansion confirms",
        ]
    if abs(ta_prob - 50.0) <= 10.0 and abs(imb) < 0.12:
        return [
            "• Balanced / two-way",
            "• No directional edge — need a clean break + follow-through",
        ]
    return [
        "• Mixed inputs / transition",
        "• No directional edge until momentum + book skew align on a break",
    ]


def _plain_ta_conclusion_line(st: dict, decision: str, conf: int) -> str:
    if st.get("liq_state") != "fresh":
        return "No trade — liquidity snapshot not fresh; decisions are hard-stopped."
    sym_u = (st.get("symbol") or "").upper()
    vd = st.get("venue_div") or {}
    if sym_u == "BTC-USD" and vd.get("block_decisions"):
        return "No trade — venue divergence guard active (two-venue agreement required)."
    if st.get("macro_blocked"):
        return "No trade — macro guard blocking window."
    if decision in ("LONG", "SHORT") and conf >= LOW_CONF_THRESHOLD:
        return "Actionable directional bias per desk rules — still size to risk."
    if conf < 20:
        return "No real edge yet — stand aside until the confidence path clears."
    if decision in ("LONG", "SHORT"):
        return "Lean present but below conf gate — wait for stronger agreement."
    return "Stand aside — wait for confirmed close vs triggers."


def _plain_ta_confidence_path_block(
    st: dict,
    timeframe: str,
    long_trigger: float,
    short_trigger: float,
    px: float,
) -> str:
    """
    Display-only PASS/FAIL checklist. Thresholds match the retired single-line path:
    LIQ_MAX_AGE_SEC, venue_div (BTC-USD), macro blocked / “soon”, |mom|>0.10%, |imb|>=0.10,
    LOW_CONF_THRESHOLD, spot vs long/short triggers (no new rules).
    """
    _MOM_PATH = 0.10
    _TILT_PATH = 0.10

    rows: List[str] = ["Confidence path", "• Conditions:"]

    liq_state = st.get("liq_state")
    liq_age = st.get("liq_age")
    if liq_state == "fresh":
        age_txt = f"{float(liq_age):.1f}s" if liq_age is not None else "n/a"
        rows.append(
            f"  - Liquidity snapshot: PASS (age {age_txt} ≤ {LIQ_MAX_AGE_SEC:.0f}s)"
        )
    else:
        age_txt = "n/a" if liq_age is None else f"{float(liq_age):.1f}s"
        rows.append(
            f"  - Liquidity snapshot: FAIL (age {age_txt} > {LIQ_MAX_AGE_SEC:.0f}s)"
        )

    sym_u = (st.get("symbol") or "").upper()
    vd = st.get("venue_div") or {}
    if sym_u != "BTC-USD" or not vd.get("enabled"):
        rows.append("  - Venue agreement: N/A (BTC-USD guard only)")
    elif vd.get("block_decisions"):
        stt = str(vd.get("status") or "?")
        div = vd.get("divergence_bps")
        div_txt = f"{float(div):.1f} bps" if div is not None else "n/a"
        rows.append(
            f"  - Venue: FAIL ({stt}; div {div_txt} — {vd.get('reason', 'blocked')})"
        )
    elif str(vd.get("status")) == "warn":
        div = float(vd.get("divergence_bps") or 0.0)
        wb = float(vd.get("warn_bps") or 0.0)
        rows.append(
            f"  - Venue: FAIL ({div:.1f} bps > {wb:.0f} bps warn ceiling)"
        )
    else:
        div = vd.get("divergence_bps")
        div_txt = f"{float(div):.1f} bps" if div is not None else "n/a"
        rows.append(f"  - Venue: PASS (ok; divergence {div_txt})")

    if st.get("macro_blocked"):
        rows.append("  - Macro block: FAIL (trading paused in macro window)")
    else:
        rows.append("  - Macro block: PASS (not blocked)")

    tok = _plain_ta_macro_token(st)
    if st.get("macro_blocked"):
        rows.append("  - Macro proximity: N/A (blocked)")
    elif tok == "soon":
        rows.append(
            f"  - Macro proximity: FAIL (high-impact soon — {st.get('macro_label', '')})"
        )
    else:
        rows.append("  - Macro proximity: PASS (not in “soon” window)")

    mom = float(st.get("momentum", 0.0))
    am = abs(mom)
    if am > _MOM_PATH:
        rows.append(f"  - Momentum: PASS ({am:.2f}% > {_MOM_PATH:.2f}%)")
    else:
        rows.append(f"  - Momentum: FAIL ({am:.2f}% < {_MOM_PATH:.2f}%)")

    imb = float(st.get("imbalance", 0.0))
    aim = abs(imb)
    if aim >= _TILT_PATH:
        rows.append(
            f"  - Liquidity tilt: PASS ({imb:+.0%} ≥ {_TILT_PATH:.0%} book skew)"
        )
    else:
        rows.append(
            f"  - Liquidity tilt: FAIL ({imb:+.0%} < {_TILT_PATH:.0%} required)"
        )

    conf = int(st.get("confidence", 0))
    if conf >= LOW_CONF_THRESHOLD:
        rows.append(
            f"  - Desk confidence: PASS ({conf}% ≥ {LOW_CONF_THRESHOLD}%)"
        )
    else:
        rows.append(
            f"  - Desk confidence: FAIL ({conf}% < {LOW_CONF_THRESHOLD}%)"
        )

    beyond = (px >= long_trigger) or (px <= short_trigger)
    if beyond:
        if px >= long_trigger:
            rows.append(
                f"  - Close beyond trigger: PASS (price {_fmt_level(px)} ≥ long {_fmt_level(long_trigger)})"
            )
        else:
            rows.append(
                f"  - Close beyond trigger: PASS (price {_fmt_level(px)} ≤ short {_fmt_level(short_trigger)})"
            )
    else:
        rows.append(
            f"  - Close beyond trigger: FAIL (price inside range {_fmt_level(short_trigger)}–{_fmt_level(long_trigger)}; need {timeframe} close beyond)"
        )

    return "\n".join(rows)


def _plain_ta_read(sym: str, timeframe: str = "5m") -> str:
    """
    Plain-English TA note: regime, control-plane honesty, triggers, maps (no fake odds).
    """
    st = _desk_compute_symbol_state(sym, tf=timeframe)
    if not st.get("ok"):
        return f"📌 {sym}: no price right now."

    px = float(st["price"])
    mom = float(st.get("momentum", 0.0))
    imb = float(st.get("imbalance", 0.0))
    conf = int(st.get("confidence", 0))
    decision = st.get("decision", "WAIT")
    action_raw = str(st.get("action_raw", "WAIT"))
    r = st.get("resistance")
    s = st.get("support")
    atrp = float(st.get("atrp", max(px * 0.0045, 0.0005 * max(px, 1.0))))
    ta = _safe_ta_bias(sym)
    ta_prob = float(ta.get("prob_long", 50)) if isinstance(ta, dict) else 50.0

    nctx = _desk_narrative_context(mom, imb, ta or {}, action_raw, decision, conf)

    liq_state = str(st.get("liq_state", "fresh"))
    liq_age = st.get("liq_age")

    # Trigger map (unchanged numerics)
    hold_up = px + 0.15 * atrp
    press_up = px + 0.35 * atrp
    warn_dn = px - 0.20 * atrp
    shift_dn = px - 0.55 * atrp

    if isinstance(r, (int, float)) and r > px:
        break_up = float(r)
    else:
        break_up = px + 0.65 * atrp

    if isinstance(s, (int, float)) and s < px:
        shift_dn = float(s)

    target_candidates = [px + 0.8 * atrp, px + 1.2 * atrp, px + 1.8 * atrp, break_up]
    targets_up = sorted({round(float(t), 6) for t in target_candidates if float(t) > px + 1e-9})
    while len(targets_up) < 3:
        last = targets_up[-1] if targets_up else px + 0.8 * atrp
        targets_up.append(last + 0.4 * atrp)
    target1, target2, target3 = targets_up[:3]

    long_trigger = max(hold_up, px + 0.10 * atrp)
    short_trigger = min(warn_dn, px - 0.10 * atrp)
    long_inval = px - 1.00 * atrp
    short_inval = px + 1.00 * atrp

    if liq_state != "fresh":
        action_block = (
            f"Action now\n"
            f"• NO TRADE (STALE DATA)\n"
            f"• Liquidity state: stale (age {'n/a' if liq_age is None else f'{liq_age:.1f}s'})\n"
            f"• Wait until snapshot age <= {LIQ_MAX_AGE_SEC:.1f}s"
        )
    elif decision == "LONG":
        action_block = (
            f"Action now\n"
            f"• Bias: LONG setup active\n"
            f"• Trigger: hold above {_fmt_level(long_trigger)}\n"
            f"• Invalidation: below {_fmt_level(long_inval)}\n"
            f"• TP path: {_fmt_level(target1)} / {_fmt_level(target2)} / {_fmt_level(target3)}"
        )
    elif decision == "SHORT":
        action_block = (
            f"Action now\n"
            f"• Bias: SHORT setup active\n"
            f"• Trigger: hold below {_fmt_level(short_trigger)}\n"
            f"• Invalidation: above {_fmt_level(short_inval)}\n"
            f"• TP path: {_fmt_level(px - 0.8*atrp)} / {_fmt_level(px - 1.2*atrp)} / {_fmt_level(px - 1.8*atrp)}"
        )
    else:
        action_block = (
            f"Action now\n"
            f"• NO TRADE\n"
            f"• Long only on {timeframe} close above {_fmt_level(long_trigger)}\n"
            f"• Short only on {timeframe} close below {_fmt_level(short_trigger)}\n"
            f"• If no break: stand aside (chop risk)"
        )

    regime_lines = _plain_ta_regime_lines(decision, conf, mom, imb, ta_prob, liq_state)
    regime_block = "Regime\n" + "\n".join(regime_lines)

    if liq_state == "fresh" and liq_age is not None:
        data_line = f"• Data: fresh (liq age {float(liq_age):.1f}s)"
    elif liq_state == "fresh":
        data_line = "• Data: fresh"
    else:
        data_line = f"• Data: stale ({'n/a' if liq_age is None else f'{float(liq_age):.1f}s'})"

    control_block = (
        "Control\n"
        f"{data_line}\n"
        f"• Venue: {_plain_ta_venue_token(st)}\n"
        f"• Macro: {_plain_ta_macro_token(st)}"
    )
    if (sym or "").upper().strip() == "BTC-USD":
        control_block += f"\n• Coinbase feed: {st.get('coinbase_feed_health', 'degraded')}"

    why_block = (
        "Why\n"
        f"• Price: {_fmt_level(px)}\n"
        f"• Momentum: {nctx['momentum_txt']} ({mom:+.2f}%)\n"
        f"• Liquidity: {nctx['liquidity_txt']} (tilt {imb:+.0%})\n"
        f"• TA: {nctx['ta_txt']}\n"
        f"• Setup: {nctx['setup_txt']}\n"
        f"• Desk decision: {decision} (conf {conf}%)\n"
        f"• Conclusion: {_plain_ta_conclusion_line(st, decision, conf)}"
    )
    if (sym or "").upper().strip() == "BTC-USD" and st.get("coinbase_feed_health") == "degraded":
        why_block += "\n• Feed note: Coinbase health is degraded (informational, non-blocking)."

    conf_path = _plain_ta_confidence_path_block(
        st, timeframe, long_trigger, short_trigger, px
    )

    matters = (
        "What matters next\n"
        "Bullish\n"
        f"• {timeframe} close above {_fmt_level(hold_up)} = first hold\n"
        f"• {timeframe} close above {_fmt_level(press_up)} = stronger continuation\n"
        f"• Break/hold above {_fmt_level(break_up)} = expansion confirmed\n"
        f"• Next targets: {_fmt_level(target1)} / {_fmt_level(target2)} / {_fmt_level(target3)}\n\n"
        "Bearish\n"
        f"• {timeframe} close below {_fmt_level(warn_dn)} = warning\n"
        f"• {timeframe} close below {_fmt_level(shift_dn)} = bearish pressure returning\n"
        f"• 15m close below {_fmt_level(shift_dn)} = stronger downside confirmation"
    )

    map_block = (
        "Map\n"
        f"• Above {_fmt_level(hold_up)} = improving for bulls\n"
        f"• Above {_fmt_level(press_up)} = bullish continuation\n"
        f"• Above {_fmt_level(break_up)} = bullish expansion\n"
        f"• Below {_fmt_level(warn_dn)} = warning\n"
        f"• Below {_fmt_level(shift_dn)} = bearish shift"
    )

    return (
        f"📌 {sym} (Now, {timeframe})\n\n"
        f"{regime_block}\n\n"
        f"{control_block}\n\n"
        f"{action_block}\n\n"
        f"{why_block}\n\n"
        f"{conf_path}\n\n"
        f"{matters}\n\n"
        f"{map_block}"
    )

def build_desk_brief(symbols: Iterable[str], timeframe: str = "15m") -> tuple:
    tf = timeframe if timeframe in {"5m", "15m", "1h", "4h"} else "15m"
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    try:
        MACRO.reload()
    except Exception:
        pass
    # Compute states first so we can generate an "Action Now" line.
    states = {}
    for sym in (s.strip().upper() for s in symbols if s and s.strip()):
        states[sym] = _desk_compute_symbol_state(sym, tf=tf)

    # Pick the best LONG and best SHORT by confidence (if any).
    longs = [st for st in states.values() if st.get("ok") and st.get("decision") == "LONG"]
    shorts = [st for st in states.values() if st.get("ok") and st.get("decision") == "SHORT"]
    best_long = max(longs, key=lambda x: x.get("confidence", 0), default=None)
    best_short = max(shorts, key=lambda x: x.get("confidence", 0), default=None)

    if best_long and best_short:
        action_now = f"Action Now: LONG={best_long['symbol']} ({best_long['confidence']}%) / SHORT={best_short['symbol']} ({best_short['confidence']}%)"
    elif best_long:
        action_now = f"Action Now: LONG {best_long['symbol']} ({best_long['confidence']}%)"
    elif best_short:
        action_now = f"Action Now: SHORT {best_short['symbol']} ({best_short['confidence']}%)"
    else:
        action_now = "Action Now: WAIT (no strong aligned LONG/SHORT)"

    # Build message
    if DESK_STYLE == "plain":
        primary = None
        syms = [s.strip().upper() for s in symbols if s and s.strip()]
        # Prefer BTC as the headline read.
        if "BTC-USD" in syms:
            primary = "BTC-USD"
        elif syms:
            primary = syms[0]
        plain = _plain_ta_read(primary or "BTC-USD", timeframe="5m")
        plain += "\n\n(Use /ta ETH-USD 5m, /ta SOL-USD 5m, /ta XRP-USD 5m for same format.)"
        return plain, states

    lines = [f"🧠 TA Desk Brief ({tf}) • {now} UTC"]
    liq_state, liq_age, _liq_reason = _liq_data_state(_LIQ_PATH)
    if liq_state == "fresh":
        lines.append(f"Data state: fresh (liq age {liq_age:.1f}s)")
    else:
        age_txt = "n/a" if liq_age is None else f"{liq_age:.1f}s"
        lines.append(f"Data state: stale (liq age {age_txt}) — decisions hard-stopped")
    lines.extend(_macro_desk_context_lines())
    lines.append(action_now)
    lines.append(
        "How to read: CONF = strength of signals (not “trade yes/no”). "
        f"LONG/SHORT needs vote sum ≤−2 or ≥+2 on (Mom/Liq/TA) + conf ≥{LOW_CONF_THRESHOLD}%. "
        "Votes: + = LONG, − = SHORT, · = flat/neutral. Mom ≈0 = chop."
    )
    lines.append("—")
    lines.append("SYMBOL | DEC | CONF | VOTE | PRICE | MOM | LIQ | DRIVER | POINT NO")
    for sym in (s.strip().upper() for s in symbols if s and s.strip()):
        st = states[sym]
        lines.append(_desk_symbol_line(st, _desk_prev_state.get(sym), include_note=True))
    lines.append("—")
    lines.append("Plan: trade only when vote sum ±2 AND momentum confirms; respect macro window.")
    return "\n".join(lines), states

def desk_brief_loop():
    if not DESK_BRIEF_ENABLED:
        return
    send_telegram(
        f"🧠 Desk updates enabled (every {max(60, DESK_BRIEF_INTERVAL_SEC)//60}m, tf={DESK_BRIEF_TIMEFRAME})."
    )
    last_full = 0.0
    while True:
        try:
            now = time.time()
            # periodic full digest
            if now - last_full >= max(60, DESK_BRIEF_INTERVAL_SEC):
                note, states = build_desk_brief(tuple(symbols_to_watch), timeframe=DESK_BRIEF_TIMEFRAME)
                send_telegram(note)
                _desk_prev_state.update(states)
                last_full = now
            # flip alerts between digests
            if DESK_FLIP_ALERTS_ENABLED:
                for sym in symbols_to_watch:
                    cur = _desk_compute_symbol_state(sym, tf=DESK_BRIEF_TIMEFRAME)
                    prev = _desk_prev_state.get(sym)
                    if not cur.get("ok"):
                        continue
                    if _desk_state_changed(prev, cur):
                        if now - _desk_last_flip_ts[sym] >= max(30, DESK_FLIP_COOLDOWN_SEC):
                            send_telegram(
                                f"🔄 TA Shift {sym}\n"
                                f"{_desk_symbol_line(cur, prev, include_note=True)}\n"
                                f"Macro: {cur.get('macro_label','n/a')}"
                            )
                            _desk_last_flip_ts[sym] = now
                    _desk_prev_state[sym] = cur
            time.sleep(30)
        except Exception as e:
            print(f"[desk_brief_loop] {e}", flush=True)
            time.sleep(5)

def macro_calendar_loop():
    if not MACRO_CALENDAR_ALERTS_ENABLED:
        return
    send_telegram(f"🗓️ Macro calendar alerts enabled (lookahead={MACRO_CALENDAR_LOOKAHEAD_HOURS}h).")
    while True:
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            events = _macro_events_within(MACRO_CALENDAR_LOOKAHEAD_HOURS)
            for when, ev in events:
                title = ev.get("name") or ev.get("title") or "Macro Event"
                key = _macro_event_key(ev, when)
                delta_sec = (when - now).total_seconds()
                mins_left = int(max(0, delta_sec // 60))

                expected = _fmt_macro_value(ev.get("expected"), ev.get("unit", ""))
                previous = _fmt_macro_value(ev.get("previous"), ev.get("unit", ""))
                actual = _fmt_macro_value(ev.get("actual"), ev.get("unit", ""))
                has_expected = ev.get("expected") not in (None, "")
                up_case, down_case = _macro_bias_rules(title)
                lbl_up, lbl_dn = _macro_case_labels(title, has_expected)

                for mark in sorted(set(MACRO_CALENDAR_ALERT_MINUTES), reverse=True):
                    if delta_sec >= 0 and mins_left <= mark and (key, f"t-{mark}") not in _macro_alert_sent:
                        _macro_alert_sent.add((key, f"t-{mark}"))
                        send_telegram(
                            f"⏰ Macro in {mark}m: {title}\n"
                            f"Impact: {str(ev.get('impact', 'unknown')).upper()} | Time: {when.strftime('%H:%M')} UTC\n"
                            f"Expected: {expected} | Previous: {previous}\n"
                            f"{lbl_up}: {up_case}\n"
                            f"{lbl_dn}: {down_case}\n"
                            "TA mindset: avoid pre-news chase; trade post-print only after direction + liquidity confirm."
                        )

                if abs(delta_sec) <= 45 and (key, "release") not in _macro_alert_sent:
                    _macro_alert_sent.add((key, "release"))
                    send_telegram(
                        f"📣 Macro release now: {title}\n"
                        f"Actual: {actual} | Expected: {expected} | Previous: {previous}\n"
                        f"{lbl_up}: {up_case}\n"
                        f"{lbl_dn}: {down_case}\n"
                        "Execution: first impulse can fake out; prefer retest + momentum alignment before entry."
                    )
            time.sleep(max(10, MACRO_CALENDAR_POLL_SEC))
        except Exception as e:
            print(f"[macro_calendar_loop] {e}", flush=True)
            time.sleep(5)

# ──────────────────────────────────────────────────────────────────────────────
# Utils
# ──────────────────────────────────────────────────────────────────────────────

def calculate_contract_size(balance_usd, price):
    global risk_scale
    risk_amount = balance_usd * TRADE_AMOUNT_RISK_PERCENT / 100 * risk_scale
    return max(MIN_CONTRACTS, int(risk_amount / max(price, 1e-8)))


def update_daily_pnl(symbol, pnl):
    global loss_streak, risk_scale
    daily = daily_pnl.get(symbol, {"pnl": 0, "trades": 0})
    daily["pnl"] += pnl; daily["trades"] += 1
    daily_pnl[symbol] = daily
    loss_streak = loss_streak + 1 if pnl < 0 else 0
    risk_scale = 0.5 if loss_streak >= RISK_THROTTLE_LOSSES else 1.0


def check_daily_loss():
    total_pnl = sum(d.get("pnl", 0) for d in daily_pnl.values())
    if total_pnl <= MAX_DAILY_LOSS:
        send_telegram("🚫 Max Daily Loss Hit — Bot Paused for Today.")
        return False
    return True


def in_active_session():
    hour = datetime.datetime.utcnow().hour
    return 6 <= hour <= 20

_last_px = {}

def market_trending_price(sym, price, threshold=0.001):  # 0.1%
    prev = _last_px.get(sym); _last_px[sym] = price
    return False if prev is None else (abs(price - prev) / max(prev, 1e-8) > threshold)


def _ensure_daily_open(sym: str, now: float = None) -> Optional[float]:
    """
    Tracks the UTC daily open price per symbol.
    Resets at each new UTC day.
    """
    global _DAILY_OPEN, _DAILY_OPEN_DATE
    now = now or time.time()
    today = datetime.datetime.utcfromtimestamp(now).date()
    if _DAILY_OPEN_DATE != today:
        _DAILY_OPEN_DATE = today
        _DAILY_OPEN = {}
    if sym not in _DAILY_OPEN:
        px = get_spot_price(sym) or get_futures_price(sym)
        if px:
            _DAILY_OPEN[sym] = float(px)
    return _DAILY_OPEN.get(sym)

def _overnight_change_pct(sym: str, last_px: float) -> Optional[float]:
    """
    % change from UTC daily open to current price.
    """
    open_px = _ensure_daily_open(sym)
    if not open_px or not last_px:
        return None
    try:
        return (float(last_px) / float(open_px) - 1.0) * 100.0
    except Exception:
        return None

def _trend_label_from_history(sym: str) -> str:
    """
    Uses your existing _PRICE_HISTORY + _slope_percent to label trend.
    """
    hist = _PRICE_HISTORY.get(sym)
    if not hist or len(hist) < 8:
        return "n/a"
    slope = _slope_percent(hist)  # (% over window)
    if slope >= 0.30:
        return "Up (strong)"
    if slope >= 0.10:
        return "Up (mild)"
    if slope <= -0.30:
        return "Down (strong)"
    if slope <= -0.10:
        return "Down (mild)"
    return "Sideways"

def _sr_levels(sym: str, lookback_sec: int = 6 * 3600) -> Tuple[Optional[float], Optional[float]]:
    """
    Nearest intraday resistance/support from recent swing high/low.
    Requires we push ticks (see step #4).
    """
    lo, _, hi, _ = _recent_swing(sym, lookback_sec)
    return hi, lo  # (resistance, support)

def _tp_sl_from_atr(price: float, prices: deque, side: str, atr_mult_tp=1.5, atr_mult_sl=1.0) -> Tuple[float, float]:
    """
    Turn ATR(bps) into TP/SL prices for a quick day plan.
    """
    atr_bps = _atr_bps(prices)  # bps
    if atr_bps <= 0:
        # fallback: 0.5% SL, 0.8% TP
        tp = price * (1.008 if side == "LONG" else 0.992)
        sl = price * (0.995 if side == "LONG" else 1.005)
        return round(tp, 2), round(sl, 2)
    atr_frac = atr_bps / 10000.0
    tp_move = price * atr_frac * atr_mult_tp
    sl_move = price * atr_frac * atr_mult_sl
    if side == "LONG":
        return round(price + tp_move, 2), round(price - sl_move, 2)
    else:
        return round(price - tp_move, 2), round(price + sl_move, 2)


# ──────────────────────────────────────────────────────────────────────────────
# Startup checks
# ──────────────────────────────────────────────────────────────────────────────

def run_startup_health_check():
    """
    Light sanity check that never forces futures lookups unless futures signals are enabled.
    - Verifies JWT auth
    - Verifies we can fetch a price per symbol (spot-first; futures only if enabled)
    - Never halts because a PERP isn’t visible
    """
    try:
        ok = auth_smoke_test()
        if not ok:
            globals()["AUTO_TRADE_ENABLED"] = False
            send_telegram("❌ Startup Health Check: Coinbase auth failed.\n➡️ Auto-trading DISABLED. Alerts-only mode.")
            return False

        bad = []
        for sym in [s.strip() for s in HEALTHCHECK_SYMBOLS if s.strip()]:
            px = None
            # Spot first (quiet, reliable)
            try:
                px = get_spot_price(sym)
            except Exception:
                px = None

            # Only touch futures if signals are enabled and spot failed
            if px is None and FUTURES_SIGNALS_ENABLED:
                try:
                    px = get_futures_price(sym)
                except Exception:
                    px = None

            if px is None:
                bad.append(sym)

        if bad:
            send_telegram("⚠️ Startup Health Check: could not fetch price for: "
                          + ", ".join(bad) + ". Continuing alerts-only.")
            globals()["AUTO_TRADE_ENABLED"] = False
            return True

        send_telegram("✅ Startup Health Check: auth OK, price feed OK (spot-first).")
        return True

    except Exception as e:
        globals()["AUTO_TRADE_ENABLED"] = False
        send_telegram(f"❌ Startup Health Check exception: {e}\n➡️ Auto-trading DISABLED. Alerts-only.")
        return False



# ──────────────────────────────────────────────────────────────────────────────
# Timeframe helpers + OHLCV access + TA/ATR/SR utilities
# ──────────────────────────────────────────────────────────────────────────────

_VALID_TFS = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}

def _parse_tf_and_symbols(arg, default_syms):
    """
    Parses '/decision 15m BTC,SOL,ETH' or '/decision BTC,SOL,ETH' etc.
    Returns (timeframe_str, [symbols])
    """
    tf = "15m"
    if not arg:
        return tf, list(default_syms)
    toks = [t.strip() for t in arg.replace(",", " ").split() if t.strip()]
    if toks and toks[0].lower() in _VALID_TFS:
        tf = _VALID_TFS[toks.pop(0).lower()]
    syms = []
    for t in toks:
        syms.append(t.upper() if "-" in t else f"{t.upper()}-USD")
    return tf, (syms or list(default_syms))

# Lazy ccxt client
_CCXT = None
def _ex():
    global _CCXT
    if _CCXT is None:
        _CCXT = ccxt.coinbase()
    return _CCXT

def _ohlcv_tf(sym, tf="15m", limit=240):
    """Return list of [ts, o, h, l, c, v] or [] on error."""
    try:
        return _ex().fetch_ohlcv(sym.replace("-", "/"), timeframe=tf, limit=limit)
    except Exception:
        return []

def _atr_from_rows(rows, window=14):
    """
    Wilder-like ATR (simple mean) computed from raw rows.
    rows: [[ts,o,h,l,c,v], ...]
    """
    if len(rows) < window + 2:
        return None
    trs = []
    prev_close = float(rows[0][4])
    for r in rows[1:]:
        h = float(r[2]); l = float(r[3]); c = float(r[4])
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    if len(trs) < window:
        return None
    return sum(trs[-window:]) / float(window)

def _pivot_levels(rows, lookback=40):
    """
    Very simple nearest S/R from last <lookback> bars (exclude the latest forming bar).
    Returns (resistance, support) or (None, None).
    """
    if len(rows) < lookback + 2:
        return None, None
    highs = [float(r[2]) for r in rows[-lookback-1:-1]]
    lows  = [float(r[3]) for r in rows[-lookback-1:-1]]
    if not highs or not lows:
        return None, None
    return (max(highs), min(lows))
# ──────────────────────────────────────────────────────────────────────────────
# Intraday levels & scalp plan helpers
# ──────────────────────────────────────────────────────────────────────────────

def _get_liq_row(sym: str) -> dict:
    snap = _open_json(_LIQ_PATH)
    if not isinstance(snap, dict):
        return {}
    symbols = snap.get("symbols", {}) or {}
    for k in _lookup_symbols_for(sym):
        row = symbols.get(k)
        if row:
            return row
    return {}

def _nearest_levels(sym: str, price: float) -> dict:
    """
    Returns a compact dict with nearest bid/ask walls, imbalance and spread (bps).
    """
    row = _get_liq_row(sym) or {}

    # spread → bps (prefer snapshot bps, else compute from absolute)
    try:
        if row.get("spread_bps") is not None:
            spr_bps = abs(float(row["spread_bps"]))
        else:
            spr_abs = float(row.get("spread", 0.0) or 0.0)
            spr_bps = (abs(spr_abs) / max(float(price), 1e-9)) * 1e4 if price else 0.0
    except Exception:
        spr_bps = 0.0

    return {
        "venue": str(row.get("venue") or ""),
        "imb": float(row.get("imbalance", 0.0)),
        "spr_bps": float(spr_bps),
        "bid_wall": float(row.get("nearest_bid_wall", 0.0) or 0.0),
        "ask_wall": float(row.get("nearest_ask_wall", 0.0) or 0.0),
        "bid10": float(row.get("cum_bid10", 0.0) or 0.0),
        "ask10": float(row.get("cum_ask10", 0.0) or 0.0),
        "px": float(price),
    }

def _fmt_levels_for_user(sym: str, price: float) -> str:
    d = _nearest_levels(sym, price)
    venue = d["venue"] or "—"
    bidw  = d["bid_wall"]; askw = d["ask_wall"]

    tilt  = "Bid-heavy" if d["imb"] > 0.02 else ("Ask-heavy" if d["imb"] < -0.02 else "Balanced")
    gauge = _imb_gauge(d["imb"])
    spr_s = _spr_bps_phrase(d["spr_bps"])  # human-friendly bps/percent

    parts = [f"📊 {sym} @ {price:.4f} [{venue}] | {tilt} {d['imb']:+.0%} {gauge} | spr {spr_s}"]
    if venue == "—" and d["spr_bps"] == 0:
        parts.append(" (Liquidity data not available for this symbol.)")
    if bidw: parts.append(f" | 🟩 Bid wall {bidw:.4f}")
    if askw: parts.append(f" | 🟥 Ask wall {askw:.4f}")
    return "".join(parts)

def _scalp_plan(sym: str) -> str:
    """
    Quick intraday plan using spot price + recent ATR (bps) with your existing
    _PRICE_HISTORY and _atr_bps(). Long/short bias from imbalance + momentum slope.
    """
    px = get_spot_price(sym)
    if not px:
        return f"{sym}: (no spot price)"
    px = float(px)

    # ATR (bps) from your history if present; fallback to 8bps minimum
    hist = _PRICE_HISTORY.get(sym)
    atr_bps = _atr_bps(hist) if hist and len(hist) >= 4 else 8.0
    atr_pct = atr_bps / 100.0
    risk = max(atr_pct, 0.06)   # ~0.06% floor

    # Momentum lean (reuse your slope helper)
    slope = _slope_percent(hist) if hist and len(hist) >= 8 else 0.0
    # Liquidity lean
    d = _nearest_levels(sym, px)
    bias_liq = "LONG" if d["imb"] >= 0.10 else ("SHORT" if d["imb"] <= -0.10 else "NEUTRAL")
    bias_mom = "LONG" if slope >= 0.20 else ("SHORT" if slope <= -0.20 else "NEUTRAL")

    # Combine (very simple rule)
    bias = "LONG" if ("LONG" in (bias_liq, bias_mom) and bias_liq != "SHORT") else (
           "SHORT" if ("SHORT" in (bias_liq, bias_mom) and bias_liq != "LONG") else "NEUTRAL")

    # Entry/TP/SL bands
    if bias == "LONG":
        entry_lo = px * (1 - 0.25*atr_pct)
        entry_hi = px * (1 + 0.10*atr_pct)
        tp1 = px * (1 + 1.0*atr_pct)
        tp2 = px * (1 + 2.0*atr_pct)
        sl  = px * (1 - 0.8*atr_pct)
    elif bias == "SHORT":
        entry_lo = px * (1 - 0.10*atr_pct)
        entry_hi = px * (1 + 0.25*atr_pct)
        tp1 = px * (1 - 1.0*atr_pct)
        tp2 = px * (1 - 2.0*atr_pct)
        sl  = px * (1 + 0.8*atr_pct)
    else:
        entry_lo = px * (1 - 0.10*atr_pct)
        entry_hi = px * (1 + 0.10*atr_pct)
        tp1 = px * (1 + 0.8*atr_pct)
        tp2 = px * (1 - 0.8*atr_pct)
        sl  = px * (1 - 0.6*atr_pct)  # informational

    lvl_line = _fmt_levels_for_user(sym, px)
    plan = [
        f"🎯 {sym} SCALP PLAN",
        f"{lvl_line}",
        f"Bias: {bias}  (mom={slope:+.2f}%, liq={d['imb']:+.0%})",
        f"Entry zone: {entry_lo:.4f} → {entry_hi:.4f}",
        f"Targets: {tp1:.4f} / {tp2:.4f}",
        f"Stop: {sl:.4f}",
        "Notes: respect walls; if bid wall gets pulled, tighten risk; if ask wall consumed, trail into strength."
    ]
    return "\n".join(plan)


# Put this near your other plan builders (right after _scalp_plan is fine)

from typing import Iterable

def build_decision_cards(symbols: Iterable[str], timeframe: str = "15m") -> str:
    """
    TA + Liquidity decision cards per symbol.
    timeframe: one of "5m","15m","1h","4h"
    """
    lines = []
    tf = timeframe if timeframe in {"5m","15m","1h","4h"} else "15m"

    for sym in (s.strip().upper() for s in symbols if s and s.strip()):
        try:
            # --- Price ---
            px = get_spot_price(sym) or get_futures_price(sym)
            if not px:
                lines.append(f"🎛️ {sym}\n⚠️ no price\n"); continue
            px = float(px)

            # --- Orderbook snapshot (for tilt / venue / spread) ---
            row = _get_liq_row(sym) or {}
            imb   = float(row.get("imbalance", 0.0))
            spr   = float(row.get("spread", 0.0))
            venue = row.get("venue", "") or "—"

            tilt = "Bid tilt" if imb > 0.02 else ("Ask tilt" if imb < -0.02 else "Balanced")
            gauge = _imb_gauge(imb)
            # --- TA buffer (EMA/RSI/MACD scoring) ---
            _update_ta_buffer(sym, px)
            ta = _ta_bias_from_buffer(sym)  # may be None while warming
            ta_head = (ta["headline"] if ta else "warming up…")
            ta_prob_long = float(ta["prob_long"]) if ta else 50.0

            # --- Short-term momentum ---
            mom_pct = _quick_trend_safe(sym)

            # --- OHLCV / ATR / S-R ---
            rows = _ohlcv_tf(sym, tf, limit=240)
            atr_abs = _atr_from_rows(rows, window=14) or (px * 0.0045)
            R, S = _pivot_levels(rows, lookback=40) if rows else (None, None)

            # --- Macro ---
            blocked, macro_lbl, macro_factor = _macro_penalty(time.time(), sym)

            # --- Action + Confidence (harmonize with Morning) ---
            liq_state, liq_age, _liq_reason = _liq_data_state(_LIQ_PATH)
            vd = _venue_divergence_state(sym)
            action_raw = _action_from_signals(mom_pct, imb, ta or {})
            conf = _conf_from_signals(mom_pct, imb, ta or {})
            conf = int(round(conf * macro_factor))
            if liq_state != "fresh":
                conf = 0
            elif sym.upper().strip() == "BTC-USD" and vd.get("status") == "warn":
                conf = int(round(conf * float(vd.get("conf_factor", 1.0))))
            if sym.upper().strip() == "BTC-USD" and vd.get("block_decisions"):
                conf = 0

            # Confidence floor / soft-lean demotion
            soft = action_raw in ("LONG?", "SHORT?")
            if liq_state != "fresh":
                decision = "WAIT"
            elif sym.upper().strip() == "BTC-USD" and vd.get("block_decisions"):
                decision = "WAIT"
            elif conf < 20 or soft:
                decision = "WAIT"
            else:
                decision = "LONG" if action_raw.startswith("LONG") else ("SHORT" if action_raw.startswith("SHORT") else "WAIT")

            # UX safety: if confidence is still low, avoid presenting LONG/SHORT as an action.
            if decision in ("LONG", "SHORT") and conf < LOW_CONF_THRESHOLD:
                decision = "WAIT"

            # --- Triggers / TP / Invalidation (ATR-based) ---
            if decision == "LONG":
                entry_lo = px - 0.25 * atr_abs
                entry_hi = px + 0.10 * atr_abs
                tp = px + 1.2 * atr_abs
                inv = px - 1.0 * atr_abs
            elif decision == "SHORT":
                entry_lo = px - 0.10 * atr_abs
                entry_hi = px + 0.25 * atr_abs
                tp = px - 1.2 * atr_abs
                inv = px + 1.0 * atr_abs
            else:  # WAIT
                entry_lo = px - 0.20 * atr_abs
                entry_hi = px + 0.20 * atr_abs
                tp = px + 0.8 * atr_abs
                inv = px - 0.8 * atr_abs

            # --- Pretty liquidity one-liner ---
            liq_line = _format_liq_for_user(sym, px)

            # --- Assemble card ---
            lines.append(f"🎛️ {sym}")
            badge = "🟢" if decision == "LONG" else ("🔴" if decision == "SHORT" else "⏸️")
            wait_note = " (no trade)" if decision == "WAIT" else ""
            lines.append(f"{badge} **Decision:** {decision}{wait_note}  •  **Conf:** {conf}%")
            if liq_state != "fresh":
                age_txt = "n/a" if liq_age is None else f"{liq_age:.1f}s"
                lines.append(f"🧪 **Data:** stale (liq age {age_txt}) — hard-stop decisions")
            elif sym == "BTC-USD" and vd.get("status") == "warn" and vd.get("divergence_bps") is not None:
                lines.append(
                    f"🧭 **Venue:** Δ {float(vd['divergence_bps']):.1f} bps "
                    f"(warn>{float(vd.get('warn_bps', 0)):.0f}) — conf ×{float(vd.get('conf_factor', 1)):.2f}"
                )
            elif sym == "BTC-USD" and vd.get("block_decisions"):
                lines.append(f"🧭 **Venue:** {vd.get('reason', 'blocked')} — hard-stop decisions")

            if decision != "WAIT":
                lines.append(
                    f"⏱️ **Trigger:** {_fmt_price(entry_lo)} → {_fmt_price(entry_hi)}   "
                    f"🎯 **TP:** {_fmt_price(tp)}   🛑 **Invalidation:** {_fmt_price(inv)}"
                )
            else:
                lines.append(
                    f"⏱️ **Trigger:** arm on clear shift beyond {_fmt_price(entry_hi)} / "
                    f"below {_fmt_price(entry_lo)}"
                )

            extra_bits = []
            extra_bits.append(f"TA {ta_head}")
            if abs(imb) >= 0.02:
                extra_bits.append(f"{'Bid' if imb>0 else 'Ask'} tilt {imb:+.0%}")
            if abs(mom_pct) >= 0.05:
                extra_bits.append(f"Mom {mom_pct:+.2f}%")
            if R or S:
                rs = f"R {R:.4f}" if R else "R n/a"
                ss = f"S {S:.4f}" if S else "S n/a"
                extra_bits.append(f"{rs} / {ss}")
            # add macro label
            extra_bits.append(macro_lbl)

            if extra_bits:
                lines.append("ℹ️ " + " | ".join(extra_bits))

            if liq_line:
                lines.append(liq_line.rstrip("\n"))

            lines.append("")  # spacer

        except Exception as e:
            lines.append(f"🎛️ {sym}\n❌ decision error: {e}\n")

    return "\n".join(lines).rstrip()


# ──────────────────────────────────────────────────────────────────────────────
# TA Advisor: turn raw signals into a human decision (play + confidence)
# ──────────────────────────────────────────────────────────────────────────────

_ADVISE_W = {
    "ta": 0.40,         # TA-bias (EMA/RSI/MACD) from _ta_bias_from_buffer
    "liq": 0.30,        # Orderbook tilt + spread gate
    "sr": 0.15,         # Distance to S/R (structure context)
    "mom": 0.10,        # Short-horizon momentum slope (your _slope_percent)
    "macro": 0.05,      # Macro guard (blocked reduces confidence)
}

def _safe_ta_bias(sym: str) -> dict:
    try:
        ta = _ta_bias_from_buffer(sym)
        return ta or {}
    except Exception:
        return {}

def _safe_liq(sym: str) -> tuple:
    try:
        ok, imb, spr, venue = _liquidity_gate(sym, "LONG")
        return ok, float(imb), float(spr), (venue or "—")
    except Exception:
        return True, 0.0, 0.0, "—"

def _safe_price(sym: str) -> float:
    try:
        px = get_spot_price(sym) or get_futures_price(sym)
        return float(px) if px is not None else 0.0
    except Exception:
        return 0.0

def _safe_hist_mom(sym: str) -> float:
    try:
        hist = _PRICE_HISTORY.get(sym)
        return float(_slope_percent(hist)) if hist and len(hist) >= 8 else 0.0
    except Exception:
        return 0.0

def _safe_sr(sym: str, tf: str = "15m") -> tuple:
    """
    Pull nearest structure (pivot) from OHLC if available; otherwise fall back to recent swings.
    Returns (resistance, support).
    """
    try:
        rows = _ohlcv_tf(sym, tf, 240) if "_ohlcv_tf" in globals() else []
        if rows:
            r, s = _pivot_levels(rows, lookback=40)
            return r, s
    except Exception:
        pass
    try:
        r, s = _sr_levels(sym, lookback_sec=6*3600)
        return r, s
    except Exception:
        return None, None

def _safe_atr_price(sym: str, px: float, tf: str = "15m") -> float:
    """
    ATR in price terms (not percent). Uses OHLCV if available, otherwise your ATR(bps) proxy.
    """
    try:
        rows = _ohlcv_tf(sym, tf, 200) if "_ohlcv_tf" in globals() else []
        if rows:
            atr_abs = _atr_from_rows(rows, window=14)
            return float(atr_abs) if atr_abs else max(px * 0.0045, 0.0005 * max(px, 1.0))
    except Exception:
        pass
    # fallback: ATR% from your bps helper
    hist = _PRICE_HISTORY.get(sym)
    atr_bps = _atr_bps(hist) if hist and len(hist) >= 4 else 45.0  # 45 bps fallback
    return (atr_bps / 10000.0) * max(px, 1e-9)

def _macro_penalty(now_ts: float, sym: str) -> tuple:
    """
    Returns (blocked_flag, label_for_card, penalty_factor)
    penalty_factor reduces confidence when macro is near.
    """
    try:
        from datetime import datetime, timezone
        now = datetime.fromtimestamp(now_ts, timezone.utc)
        blocked, reason = MACRO.is_blocked(now, sym)
        if blocked:
            return True, f"⛔ {reason}", 0.50
        # not blocked but show upcoming within 8h as heads-up
        txt = MACRO.format_upcoming(now, hours=8).strip()
        if txt:
            return False, "🕒 Macro soon", 0.80
    except Exception:
        pass
    return False, f"No high-impact macro events in the next {MACRO_BLOCK_WINDOW_MIN} min", 1.00

def _choose_playbook(side_hint: str, mom_pct: float, imb: float, px: float, r: float, s: float) -> str:
    """
    Simple heuristic: pick the play the human would.
    """
    near_r = (r is not None) and px > 0 and (r - px)/max(px,1e-9) < 0.003   # <30 bps to R
    near_s = (s is not None) and px > 0 and (px - s)/max(px,1e-9) < 0.003   # <30 bps to S

    if side_hint == "LONG" and imb >= 0.10 and mom_pct >= 0.10:
        return "Trend-Follow LONG"
    if side_hint == "SHORT" and imb <= -0.10 and mom_pct <= -0.10:
        return "Trend-Follow SHORT"

    if near_r and imb <= -0.05:
        return "Mean-Revert SHORT (fade resistance)"
    if near_s and imb >= +0.05:
        return "Mean-Revert LONG (buy support)"

    # If momentum strong near level → breakout
    if near_r and mom_pct >= 0.10:
        return "Breakout LONG (over resistance)"
    if near_s and mom_pct <= -0.10:
        return "Breakdown SHORT (through support)"

    return "WAIT"

def _as_if_order_ticket(sym, side, ent_lo, ent_hi, tp, sl, px):
    if not AS_IF_TICKETS_ENABLED:
        return ""
    # Minimal text ticket so callers don’t crash.
    return (f"🎫 As-If Ticket {sym} {side}\n"
            f"Entry {ent_lo:.4f}→{ent_hi:.4f}  TP {tp:.4f}  SL {sl:.4f}\n")

def _advise_for_symbol(sym: str, tf: str = "15m") -> str:
    px  = _safe_price(sym)
    if px <= 0:
        return f"🎛️ {sym}\n⚠️ No price.\n"

    # Ingredients
    ta   = _safe_ta_bias(sym)                  # {'headline','prob_long','ema_bias','rsi_bias','macd_bias',...}
    ok, imb, spr, venue = _safe_liq(sym)       # liquidity gate snapshot
    mom = _safe_hist_mom(sym)                  # short-horizon momentum (%)
    r, s = _safe_sr(sym, tf)                   # structure
    atrp  = _safe_atr_price(sym, px, tf)       # ATR in price terms
    blocked, macro_lbl, macro_factor = _macro_penalty(time.time(), sym)
    liq_state, liq_age, _liq_reason = _liq_data_state(_LIQ_PATH)
    vd = _venue_divergence_state(sym)

    # Side hint from TA
    ta_prob = float(ta.get("prob_long", 50)) / 100.0
    side_hint = "LONG" if ta_prob >= 0.55 else ("SHORT" if ta_prob <= 0.45 else "NEUTRAL")

    # Confidence blend (0..1)
    liq_score = 0.5 + 0.5 * max(min(imb, 1.0), -1.0)  # tilt → [0..1], center 0.5
    sr_score = 0.5
    try:
        if r and s:
            # prefer when room exists in the intended direction
            if side_hint == "LONG" and (r - px) > 0:
                sr_score = min((r - px) / max(2*atrp,1e-9), 1.0)  # more room to R is better
            elif side_hint == "SHORT" and (px - s) > 0:
                sr_score = min((px - s) / max(2*atrp,1e-9), 1.0)
    except Exception:
        pass

    ta_score  = ta_prob
    mom_score = 0.5 + (mom / 2.0) / 100.0      # +1% slope → +0.5; clamp
    mom_score = max(0.0, min(1.0, mom_score))
    macro_score = 1.0 if not blocked else 0.2

    conf = (
        _ADVISE_W["ta"]   * ta_score  +
        _ADVISE_W["liq"]  * liq_score +
        _ADVISE_W["sr"]   * sr_score  +
        _ADVISE_W["mom"]  * mom_score +
        _ADVISE_W["macro"]* macro_score
    ) * macro_factor
    if liq_state != "fresh":
        conf = 0.0
    elif (sym or "").upper().strip() == "BTC-USD" and vd.get("status") == "warn":
        conf = conf * float(vd.get("conf_factor", 1.0))
    if (sym or "").upper().strip() == "BTC-USD" and vd.get("block_decisions"):
        conf = 0.0

    conf_pct = int(round(max(0.0, min(1.0, conf)) * 100))

    # Choose playbook
    play = _choose_playbook(side_hint, mom, imb, px, r, s)
    if liq_state != "fresh":
        play = "WAIT"
    if (sym or "").upper().strip() == "BTC-USD" and vd.get("block_decisions"):
        play = "WAIT"

    # UX safety: if confidence is low, avoid presenting LONG/SHORT as an action.
    if play in ("Trend-Follow LONG", "Breakout LONG", "Mean-Revert LONG",
                "Trend-Follow SHORT", "Breakdown SHORT", "Mean-Revert SHORT") and conf_pct < LOW_CONF_THRESHOLD:
        play = "WAIT"

    # Build levels (entry/TP/SL) using ATR & structure
    tp = sl = ent_lo = ent_hi = None
    if play.startswith("Trend-Follow LONG") or play.startswith("Breakout LONG") or play.startswith("Mean-Revert LONG"):
        ent_lo = px - 0.25*atrp; ent_hi = px + 0.10*atrp
        tp     = px + 1.50*atrp
        sl     = px - 1.00*atrp
    elif play.startswith("Trend-Follow SHORT") or play.startswith("Breakdown SHORT") or play.startswith("Mean-Revert SHORT"):
        ent_lo = px - 0.10*atrp; ent_hi = px + 0.25*atrp
        tp     = px - 1.50*atrp
        sl     = px + 1.00*atrp

    # One-liner liquidity for context
    liq_line = _format_liq_for_user(sym, px) if "_format_liq_for_user" in globals() else ""

    # Compose advice
    info_bits = []
    if ta: info_bits.append(f"TA {ta.get('headline','—')}")
    info_bits.append(f"Tilt {imb:+.0%}")
    if r: info_bits.append(f"R {r:.2f}")
    if s: info_bits.append(f"S {s:.2f}")
    info_bits.append(macro_lbl)
    if liq_state != "fresh":
        age_txt = "n/a" if liq_age is None else f"{liq_age:.1f}s"
        info_bits.append(f"Data stale ({age_txt})")
    if (sym or "").upper().strip() == "BTC-USD" and vd.get("status") == "warn" and vd.get("divergence_bps") is not None:
        info_bits.append(f"Venue Δ {float(vd['divergence_bps']):.1f} bps (warn>{float(vd.get('warn_bps', 0)):.0f})")
    if (sym or "").upper().strip() == "BTC-USD" and vd.get("block_decisions"):
        info_bits.append(f"Venue guard: {vd.get('reason', 'block')}")

    hdr = "⏸️ WAIT (no trade)" if play == "WAIT" else ("🟢 LONG" if play.endswith("LONG") else "🔴 SHORT")
    plan = []
    if ent_lo and ent_hi and tp and sl:
        plan.append(
            f"⏱️ Trigger: {_fmt_price(ent_lo)} → {_fmt_price(ent_hi)}   "
            f"🎯 TP: {_fmt_price(tp)}   🛑 Invalidation: {_fmt_price(sl)}"
        )

    ticket = ""
    if ent_lo and ent_hi and tp and sl:
        ticket = _as_if_order_ticket(sym, "LONG" if "LONG" in play else ("SHORT" if "SHORT" in play else "WAIT"), ent_lo, ent_hi, tp, sl, px)

    return (
        f"🎛️ {sym}\n"
        f"{hdr} • Conf: {conf_pct}% • {play}\n"
        + (plan[0] + "\n" if plan else "")
        + ticket
        + "ℹ️ " + " | ".join(info_bits) + "\n"
        + (liq_line or "")
    )


def build_advice_cards(symbols=("BTC-USD","SOL-USD","ETH-USD","XRP-USD"), timeframe="15m") -> str:
    out = []
    for sym in symbols:
        try:
            out.append(_advise_for_symbol(sym, tf=timeframe))
        except Exception as e:
            out.append(f"🎛️ {sym}\n❌ advice error: {e}\n")
        out.append("")  # spacer
    return "\n".join(out).rstrip()





# ──────────────────────────────────────────────────────────────────────────────
# Liquidity wall watch (approach / consumed) + user one-liner
# ──────────────────────────────────────────────────────────────────────────────

# Tunables
WALL_APPROACH_BPS     = int(os.getenv("WALL_APPROACH_BPS", "7"))       # alert when price within N bps of a wall
WALL_CONSUME_BPS      = int(os.getenv("WALL_CONSUME_BPS", "3"))        # consider "consumed" when price goes past by N bps
WALL_ALERT_COOLDOWN   = int(os.getenv("WALL_ALERT_COOLDOWN", "120"))   # sec throttle per symbol/side/event

# Internal state for throttling + continuity
_last_wall_alert_ts: dict = {}     # key: (sym, side, event, level_rounded) -> ts
_last_seen_wall: dict = {}         # key: (sym, side) -> last_price_level
WALL_SPOOF_DROP_FRAC      = float(os.getenv("WALL_SPOOF_DROP_FRAC", "0.35"))  # 0.35 = 35% drop
WALL_SPOOF_MIN_NEAR_SEC   = int(os.getenv("WALL_SPOOF_MIN_NEAR_SEC", "12"))  # must be near for N sec
_wall_spoof_state: dict = {}     # key: (sym, side, level_rounded) -> {'start_ts','start_size','last_size'}

def _format_liq_for_user(sym: str, last_px: float) -> str:
    """
    One-liner: "📊 BTC-USD @ 112050 [VENUE] | 🟩 Bid 111900 | 🟥 Ask 112600 | Tilt +42% ████░ | spr 7.4 bps"
    Robust to missing fields.
    """
    snap = _open_json(_LIQ_PATH) or {}
    symbols = snap.get("symbols", {})
    row = next((symbols.get(k) for k in _lookup_symbols_for(sym) if symbols.get(k)), None) or {}

    venue = row.get("venue", "") or "—"
    try:
        imb = float(row.get("imbalance", 0.0))
    except Exception:
        imb = 0.0
    imb = max(-1.0, min(1.0, imb))  # clamp

    # --- spread in bps: prefer snapshot's spread_bps; otherwise derive from absolute spread ---
    try:
        if row.get("spread_bps") is not None:
            spr_bps = abs(float(row["spread_bps"]))
        else:
            spr_abs = float(row.get("spread", 0.0) or 0.0)
            px = float(last_px) if last_px else 0.0
            spr_bps = (abs(spr_abs) / max(px, 1e-9)) * 1e4 if px > 0 else 0.0
    except Exception:
        spr_bps = 0.0

    # Support both liquidity snapshot schemas.
    bid_w = row.get("nearest_bid_wall") or row.get("nearest_bid_wall_price")
    ask_w = row.get("nearest_ask_wall") or row.get("nearest_ask_wall_price")
    bid_label = f"🟩 Bid {float(bid_w):.4f}" if bid_w else "🟩 Bid n/a"
    ask_label = f"🟥 Ask {float(ask_w):.4f}" if ask_w else "🟥 Ask n/a"

    tilt  = "Bid-heavy" if imb > 0.02 else ("Ask-heavy" if imb < -0.02 else "Balanced")
    gauge = _imb_gauge(imb)

    note = ""
    if venue == "—" and spr_bps == 0:
        note = " (Liquidity data not available for this symbol.)"
    return (
        f"📊 {sym} @ {float(last_px):.4f} [{venue}] | "
        f"{bid_label} | {ask_label} | "
        f"{tilt} {imb:+.0%} {gauge} | spr {_spr_bps_phrase(spr_bps)}{note}\n"
    )
def _pick_walls(sym: str) -> tuple:
    """
    Returns (venue, bid_wall_price_or_None, ask_wall_price_or_None, bid_size_or_None, ask_size_or_None)
    Sizes are optional; many snapshots won’t have them.
    """
    snap = _open_json(_LIQ_PATH) or {}
    symbols = snap.get("symbols", {})
    row = next((symbols.get(k) for k in _lookup_symbols_for(sym) if symbols.get(k)), None) or {}
    venue = row.get("venue", "") or "—"
    # Support both liquidity snapshot schemas:
    # - v1: nearest_bid_wall / nearest_ask_wall
    # - v2: nearest_bid_wall_price / nearest_ask_wall_price
    bw = row.get("nearest_bid_wall") or row.get("nearest_bid_wall_price")
    aw = row.get("nearest_ask_wall") or row.get("nearest_ask_wall_price")

    # Optional: if your snapshot contains sizes, try to pick them up
    bsz = row.get("nearest_bid_wall_size") or row.get("bid_wall_size") or None
    asz = row.get("nearest_ask_wall_size") or row.get("ask_wall_size") or None
    try:
        bw = float(bw) if bw is not None else None
        aw = float(aw) if aw is not None else None
        bsz = float(bsz) if bsz not in (None, "") else None
        asz = float(asz) if asz not in (None, "") else None
    except Exception:
        pass
    return venue, bw, aw, bsz, asz

def _should_throttle_wall(sym: str, side: str, event: str, level: float, cooldown: int) -> bool:
    key = (sym, side, event, round(float(level), 4))
    now = time.time()
    last = _last_wall_alert_ts.get(key, 0.0)
    if (now - last) < cooldown:
        return True
    _last_wall_alert_ts[key] = now
    return False

def _bps(a: float, b: float) -> float:
    mid = max(float(b), 1e-9)
    return abs(float(a) - float(b)) / mid * 10000.0

def _wall_watch(sym: str, px: float):
    """
    Emits Telegram pings:
      • ⚠️ Approaching BID/ASK wall @ <lvl> (within N bps)
      • 🚨 BID/ASK wall likely consumed @ <lvl> (price moved beyond by M bps)
    Uses nearest wall levels from snapshot and simple continuity.
    """
    venue, bid_w, ask_w, bsz, asz = _pick_walls(sym)
    if bid_w is None and ask_w is None:
        return

    # Remember last seen levels (for disappearance / continuity if you later extend)
    if bid_w is not None:
        _last_seen_wall[(sym, "bid")] = bid_w
    if ask_w is not None:
        _last_seen_wall[(sym, "ask")] = ask_w

    now = time.time()

    # Approach alerts
    if bid_w is not None:
        dist_bps = _bps(px, bid_w)
        is_approach = px >= bid_w and dist_bps <= WALL_APPROACH_BPS
        passed = (px < bid_w) and (_bps(px, bid_w) >= WALL_CONSUME_BPS)

        # Spoof/cancel risk: wall size drops a lot while price is still near the level (not passed/consumed yet).
        if is_approach and bsz and float(bsz) > 0:
            key = (sym, "bid", round(float(bid_w), 4))
            st = _wall_spoof_state.get(key)
            if not st:
                _wall_spoof_state[key] = {"start_ts": now, "start_size": float(bsz), "last_size": float(bsz)}
            else:
                start_sz = max(float(st.get("start_size", 0.0)), 1e-12)
                cur_sz = float(bsz)
                drop = (start_sz - cur_sz) / start_sz
                if drop >= WALL_SPOOF_DROP_FRAC and (now - float(st.get("start_ts", now))) >= WALL_SPOOF_MIN_NEAR_SEC:
                    if not _should_throttle_wall(sym, "bid", "spoof", bid_w, WALL_ALERT_COOLDOWN):
                        send_telegram(
                            f"🕵️ Possible BID wall spoof/cancel @ {bid_w:.4f}\n"
                            f"{sym} price still near (dist {dist_bps:.1f} bps), wall size shrank {drop*100:.0f}% "
                            f"({st.get('start_size'):.0f} -> {cur_sz:.0f})."
                        )
                    _wall_spoof_state.pop(key, None)
                else:
                    st["last_size"] = cur_sz

        # If we moved away (or were consumed), clear spoof state for this level.
        if (not is_approach or passed):
            _wall_spoof_state.pop((sym, "bid", round(float(bid_w), 4)), None)

        if is_approach:
            if not _should_throttle_wall(sym, "bid", "approach", bid_w, WALL_ALERT_COOLDOWN):
                size_txt = f" (size≈{_fmt_compact(bsz)} {sym.split('-')[0]})" if bsz else ""
                send_telegram(
                    f"⚠️ Approaching BID wall @ {bid_w:.4f}{size_txt}\n"
                    f"{sym} @ {px:.4f} [{venue}] • {dist_bps:.1f} bps away\n"
                    f"Watch for bounce or absorption."
                )

    if ask_w is not None:
        dist_bps = _bps(px, ask_w)
        is_approach = px <= ask_w and dist_bps <= WALL_APPROACH_BPS
        passed = (px > ask_w) and (_bps(px, ask_w) >= WALL_CONSUME_BPS)

        if is_approach and asz and float(asz) > 0:
            key = (sym, "ask", round(float(ask_w), 4))
            st = _wall_spoof_state.get(key)
            if not st:
                _wall_spoof_state[key] = {"start_ts": now, "start_size": float(asz), "last_size": float(asz)}
            else:
                start_sz = max(float(st.get("start_size", 0.0)), 1e-12)
                cur_sz = float(asz)
                drop = (start_sz - cur_sz) / start_sz
                if drop >= WALL_SPOOF_DROP_FRAC and (now - float(st.get("start_ts", now))) >= WALL_SPOOF_MIN_NEAR_SEC:
                    if not _should_throttle_wall(sym, "ask", "spoof", ask_w, WALL_ALERT_COOLDOWN):
                        send_telegram(
                            f"🕵️ Possible ASK wall spoof/cancel @ {ask_w:.4f}\n"
                            f"{sym} price still near (dist {dist_bps:.1f} bps), wall size shrank {drop*100:.0f}% "
                            f"({st.get('start_size'):.0f} -> {cur_sz:.0f})."
                        )
                    _wall_spoof_state.pop(key, None)
                else:
                    st["last_size"] = cur_sz

        if (not is_approach or passed):
            _wall_spoof_state.pop((sym, "ask", round(float(ask_w), 4)), None)

        if is_approach:
            if not _should_throttle_wall(sym, "ask", "approach", ask_w, WALL_ALERT_COOLDOWN):
                size_txt = f" (size≈{_fmt_compact(asz)} {sym.split('-')[0]})" if asz else ""
                send_telegram(
                    f"⚠️ Approaching ASK wall @ {ask_w:.4f}{size_txt}\n"
                    f"{sym} @ {px:.4f} [{venue}] • {dist_bps:.1f} bps away\n"
                    f"Watch for rejection or sweep."
                )

    # Consumed alerts (price moved decisively through the wall)
    if bid_w is not None:
        passed = (px < bid_w) and (_bps(px, bid_w) >= WALL_CONSUME_BPS)
        if passed and not _should_throttle_wall(sym, "bid", "consumed", bid_w, WALL_ALERT_COOLDOWN):
            send_telegram(
                f"🚨 BID wall likely consumed @ {bid_w:.4f}\n"
                f"{sym} traded through to {px:.4f} [{venue}] • {_bps(px, bid_w):.1f} bps past.\n"
                f"Risk of follow-through lower; watch for next bid cluster."
            )

    if ask_w is not None:
        passed = (px > ask_w) and (_bps(px, ask_w) >= WALL_CONSUME_BPS)
        if passed and not _should_throttle_wall(sym, "ask", "consumed", ask_w, WALL_ALERT_COOLDOWN):
            send_telegram(
                f"🚨 ASK wall likely consumed @ {ask_w:.4f}\n"
                f"{sym} traded through to {px:.4f} [{venue}] • {_bps(px, ask_w):.1f} bps past.\n"
                f"Potential momentum higher; watch next supply cluster."
            )


# ──────────────────────────────────────────────────────────────────────────────
# Core loops

def fast_breakdown_loop():
    """
    Send LONG/SHORT bias alerts only, using SPOT price as source.
    NO futures orders are ever placed here.
    Spot Autopilot runs in its own loop (if enabled).
    """
    global last_mode, last_news_check

    while True:
        try:
            if manual_paused:
                time.sleep(FAST_BREAKDOWN_CHECK_INTERVAL)
                continue

            # Mode banner
            session_active = in_active_session()
            if last_mode != session_active:
                last_mode = session_active
                send_telegram("🟢 Peak-Hours mode enabled" if session_active else "🟡 Off-Hours (stricter filters)")

            # Light news gate
            if time.time() - last_news_check > 60:
                check_market_news()
                last_news_check = time.time()

            # Watchlist loop (alerts only)
            for sym in symbols_to_watch:
                # --- SPOT price is our single source for alerts ---
                spot_price = get_spot_price(sym)
                if spot_price is None:
                    warn_once(f"no_spot_{sym}", f"[WARN] No SPOT price for {sym}.")
                    continue

                # keep TA buffer updated on every tick
                _update_ta_buffer(sym, float(spot_price))

                # Feed momentum engine with our chosen price
                maybe_emit_perp_signal(sym, float(spot_price))  # uses our momentum & liquidity gates

                # Optional: simple momentum break alert (kept, but NEVER trades futures)
                atr = float(spot_price) * 0.005
                if market_trending_price(sym, float(spot_price)) and news_allows_trade():
                    direction = "LONG"  # your simple trend proxy was long-only here; keep as-is
                    entry = float(spot_price)
                    stop = round(entry - atr, 4)
                    target = round(entry + 1.5 * atr, 4)

                    ok, imb, spr, venue = _liquidity_gate(sym, direction)

                    if not ok:
                        send_telegram_throttled(
                            f"liqskip_{sym}",
                            f"⏸️ Skipped {direction} {sym}: liquidity filter (imb={imb:+.2f}, spr={spr:.6f} {venue or ''})",
                            cooldown=120,
                        )
                        continue
                                        # concise intraday liquidity line in live alerts
                    user_liq = _format_liq_for_user(sym, float(spot_price))  # one-liner wall view
                    liq_note = _liquidity_note(sym, mode="intraday") or ""   # compact tail

                    msg = (
                        f"⚡ {direction} {sym}\n"
                        f"Source=SPOT | Entry={entry}\n"
                        f"SL={stop} | TP={target}\n"
                        f"{user_liq}"
                        f"{liq_note}"
                    )
                    send_telegram_throttled(f"fast_{sym}", msg)

                    # Live wall-watch pings (approach / consumed) — short & actionable
                    try:
                        _wall_watch(sym, float(spot_price))
                    except Exception:
                        pass

            time.sleep(FAST_BREAKDOWN_CHECK_INTERVAL)

        except Exception as e:
            send_telegram(f"[Fast Error] {e}")
            time.sleep(3)


# === Newbie-friendly Daily Brief helpers =====================================

def _tilt_label(imb: float) -> str:
    """
    Turn imbalance (-1..+1) into a friendly label with an emoji + strength.
    """
    mag = abs(float(imb))
    if mag >= 0.50:
        strength = "Strong"
        bar = "█████"
    elif mag >= 0.25:
        strength = "Moderate"
        bar = "███░░"
    elif mag >= 0.10:
        strength = "Mild"
        bar = "██░░░"
    else:
        strength = "Balanced"
        bar = "░░░░░"

    if imb > 0:
        side = "buyers"; badge = "🟢"
    elif imb < 0:
        side = "sellers"; badge = "🔴"
    else:
        side = "both sides"; badge = "⚖️"

    return f"{badge} {strength} {side} ({imb:+.0%})"

def _spread_bucket_bps(spr_bps: float) -> str:
    """
    Friendly text for spread in basis points. Always shows absolute value.
    """
    b = abs(float(spr_bps))
    if b <= 5:   label = "ultra-tight"
    elif b <= 10: label = "tight"
    elif b <= 25: label = "normal"
    elif b <= 60: label = "wide"
    else:        label = "very wide"
    return f"{label} (~{b:.1f} bps)"

def _spr_bps_phrase(spr_bps: float) -> str:
    """
    Human-friendly phrase for spread in bps, including approximate percent.
    Example: 950 bps -> "very wide (~950.0 bps, ~9.5%)".
    """
    b = abs(float(spr_bps))
    pct = b / 100.0  # 100 bps = 1%
    if b <= 5:   label = "ultra-tight"
    elif b <= 10: label = "tight"
    elif b <= 25: label = "normal"
    elif b <= 60: label = "wide"
    else:        label = "very wide"
    return f"{label} (~{b:.1f} bps, ~{pct:.1f}%)"

def _fmt_price(p: float) -> str:
    """
    Display helper: fewer decimals for easier reading.
    - >= 1 → 2 decimals
    - < 1  → 4 decimals
    """
    p = float(p)
    return f"{p:.2f}" if abs(p) >= 1 else f"{p:.4f}"

def _walls_label_from_row(row: dict) -> str:
    """
    Return 'Walls: none' or 'Walls: bid @ x / ask @ y'.
    Suppresses the awkward '—' placeholders.
    """
    # Support both liquidity snapshot schemas.
    bw = row.get("nearest_bid_wall") or row.get("nearest_bid_wall_price")
    aw = row.get("nearest_ask_wall") or row.get("nearest_ask_wall_price")
    try: bw = float(bw) if bw not in (None, "") else None
    except Exception: bw = None
    try: aw = float(aw) if aw not in (None, "") else None
    except Exception: aw = None

    if bw is None and aw is None:
        return "Walls: none"
    if bw is not None and aw is not None:
        return f"Walls: bid @ {bw:.4f} / ask @ {aw:.4f}"
    if bw is not None:
        return f"Walls: bid @ {bw:.4f}"
    return f"Walls: ask @ {aw:.4f}"

def _sr_text_pair(r: Optional[float], s: Optional[float]) -> str:
    r_txt = f"R={r:.2f}" if isinstance(r, (int, float)) else "R=n/a"
    s_txt = f"S={s:.2f}" if isinstance(s, (int, float)) else "S=n/a"
    return f"{r_txt} / {s_txt}"

def _what_it_means_line(imb: float, spr_bps: float) -> str:
    """
    One sentence in plain English from tilt + spread.
    """
    if abs(spr_bps) > 60:
        spread_note = "trading is costly right now"
    elif abs(spr_bps) > 25:
        spread_note = "execution is a bit expensive"
    else:
        spread_note = "execution cost looks normal"

    if imb >= 0.25:
        tilt = "buyers dominate"
    elif imb <= -0.25:
        tilt = "sellers dominate"
    elif abs(imb) >= 0.10:
        tilt = "one side has a mild edge"
    else:
        tilt = "book looks balanced"

    return f"{tilt} and {spread_note}."

def _what_to_do_line(imb: float, spr_bps: float, r: Optional[float], s: Optional[float]) -> str:
    """
    Clear action sentence for new traders. Default = WAIT with simple triggers.
    """
    # If spread is wide, we mostly recommend waiting.
    if abs(spr_bps) > 60:
        base = "WAIT — spread is very wide"
    elif abs(spr_bps) > 25:
        base = "WAIT — spread is wide"
    else:
        # Light lean only if tilt is strong
        if imb >= 0.50:
            base = "Lean LONG — buyers control"
        elif imb <= -0.50:
            base = "Lean SHORT — sellers control"
        else:
            base = "WAIT"

    r_txt = f"{r:.2f}" if isinstance(r, (int, float)) else "R"
    s_txt = f"{s:.2f}" if isinstance(s, (int, float)) else "S"
    return f"{base}. Safer to act on a clean move: below S={s_txt} (bearish) or back above R={r_txt} (bullish)."

def build_youtube_style_story(sym: str,
                              px: float,
                              imb: float,
                              spr_bps: float,
                              trend_label: str) -> str:
    """
    Observation/narration helper.
    Always available, even when liquidity data is stale.
    Must explicitly state data state and degradation notice when stale.
    """
    liq_state, liq_age, _liq_reason = _liq_data_state(_LIQ_PATH)
    state_txt = "fresh" if liq_state == "fresh" else "stale"
    age_txt = "n/a" if liq_age is None else f"{liq_age:.1f}s"
    meaning = _what_it_means_line(imb, spr_bps)

    if liq_state != "fresh":
        return (
            f"Story ({sym}): Data state = {state_txt} (liq age {age_txt}). "
            f"Price/trend observation still active ({trend_label}), but liquidity-dependent interpretation is degraded."
        )

    return (
        f"Story ({sym}): Data state = {state_txt} (liq age {age_txt}). "
        f"Price around {_fmt_price(px)} with trend {trend_label}; {meaning}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Daily brief (on-demand helper)
# ──────────────────────────────────────────────────────────────────────────────
def build_daily_brief(symbols):
    """
    Beginner-friendly Daily Brief:
      - Clean, plain-English summary per symbol
      - Fixes spread formatting (bps handled directly)
      - Adds 'What it means' and 'What to do' lines
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    lines = [f"🧭 Daily Brief", f"🕒 {now.strftime('%Y-%m-%d %H:%M UTC')}", ""]
    liq_state, liq_age, _liq_reason = _liq_data_state(_LIQ_PATH)
    if liq_state == "fresh":
        lines.append(f"🧪 Data state: fresh (liq age {liq_age:.1f}s)")
    else:
        age_txt = "n/a" if liq_age is None else f"{liq_age:.1f}s"
        lines.append(f"🧪 Data state: stale (liq age {age_txt}) — decisions hard-stopped")
    lines.append("")

    for sym in symbols:
        try:
            # Price
            try:
                price = get_spot_price(sym)
            except Exception:
                price = None
            if not price:
                lines.append(f"{sym} — price: n/a\n")
                continue
            px = float(price)

            # Liquidity snapshot (for tilt/spread/walls/venue)
            row = _get_liq_row(sym) or {}
            imb = float(row.get("imbalance", 0.0))
            spr_bps = None
            # Prefer explicit bps if present; else compute from absolute spread
            if row.get("spread_bps") is not None:
                try: spr_bps = float(row["spread_bps"])
                except Exception: spr_bps = None
            if spr_bps is None:
                try:
                    spr_abs = float(row.get("spread", 0.0) or 0.0)
                    spr_bps = (abs(spr_abs) / max(px, 1e-9)) * 1e4
                except Exception:
                    spr_bps = 0.0

            # R/S (hybrid)
            r, s = _sr_levels_hybrid(sym)

            # 24h change + vol flags / ATR%
            chg = get_24h_change_pct(sym)  # may be None
            vf  = build_vol_flags(sym) or {}
            atr_pct = vf.get("atr_pct_15m")
            vol_ratio = vf.get("vol_ratio")
            trend_lbl = _trend_label_from_history(sym)

            # Headline
            pretty_px = f"{px:,.3f}" if px < 100 else f"{px:,.0f}"
            lines.append(f"{sym} — {pretty_px}")

            # What to do (put action near the top)
            lines.append(f"• What to do: {_what_to_do_line(imb, spr_bps, r, s)}")

            # Orderbook line (tilt + spread + walls)
            tilt_txt = _tilt_label(imb)
            spread_txt = _spread_bucket_bps(spr_bps)
            walls_txt = _walls_label_from_row(row)
            venue = (row.get("venue") or "").strip() or "—"
            lines.append(f"• Orderbook: {tilt_txt} — Spread {spread_txt}")
            if venue == "—" and spr_bps == 0:
                lines.append("• (Liquidity data not available for this symbol.)")

            # Stats line (24h, ATR%, macro)
            chg_txt = (f"{chg:+.2f}%" if chg is not None else "n/a")
            atr_txt = (f"{atr_pct:.2f}%" if isinstance(atr_pct, (int, float)) else "—")
            macro_txt = f"No high-impact macro in next {MACRO_BLOCK_WINDOW_MIN} min"
            try:
                blocked, reason = MACRO.is_blocked(now, sym)
                if blocked:
                    macro_txt = f"blocked: {reason}"
            except Exception:
                pass

            vol_note = ""
            if isinstance(vol_ratio, (int, float)):
                if vol_ratio >= VOL_RATIO_FLAG:
                    vol_note = f" | volume high (x{vol_ratio:.1f})"
                elif vol_ratio > 0:
                    vol_note = f" | volume x{vol_ratio:.1f}"

            lines.append(f"• 24h: {chg_txt} | Typical 15m move: ~{atr_txt} | Macro: {macro_txt}{vol_note}")

            # What it means (supporting explanation)
            lines.append(f"• What it means: {_what_it_means_line(imb, spr_bps)}")
            lines.append(f"• Story: {build_youtube_style_story(sym, px, imb, spr_bps, trend_lbl)}")

            lines.append("")  # spacer

        except Exception as e:
            lines.append(f"{sym}: (brief error: {e})\n")

    # Tiny legend (keeps message self-explanatory)
    lines.append("Legend: “Orderbook” = who’s heavier (buyers/sellers) and cost to trade (spread). “Typical 15m move” ≈ ATR on 15m.")
    lines.append("R = resistance, S = support | bps = basis points (100 bps = 1%) | imb = orderbook imbalance, spr = spread.")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Morning Overview helpers (modular + testable)
# ──────────────────────────────────────────────────────────────────────────────

def _sr_levels_hybrid(sym: str) -> Tuple[Optional[float], Optional[float]]:
    """
    Nearest S/R with a resilient fallback:
      1) Try in-memory recent swing levels (ticks)
      2) Fallback to 15m pivots (ccxt OHLCV)
    Returns (R, S) possibly (None, None) if both unavailable.
    """
    # Try swing levels from your live tick buffer (6h lookback)
    try:
        r_swing, s_swing = _sr_levels(sym, lookback_sec=6 * 3600)
    except Exception:
        r_swing, s_swing = None, None

    if (r_swing is not None) or (s_swing is not None):
        return r_swing, s_swing

    # Fallback: 15m pivots
    pr = ps = None
    try:
        rows15 = _ohlcv_tf(sym, "15m", 120)
        if rows15:
            pr, ps = _pivot_levels(rows15, lookback=40)
    except Exception:
        pass

    r = round(pr, 2) if pr is not None else None
    s = round(ps, 2) if ps is not None else None
    return r, s

def _liq_snapshot_for_display(sym: str, px: float) -> dict:
    """
    Read raw snapshot (not the gate) so spread is correct for display.
    Returns dict with venue, imb, spr_bps (float), text.
    """
    row = _get_liq_row(sym) or {}
    venue = row.get("venue") or "—"
    imb = float(row.get("imbalance", 0.0))
    # Prefer provided spread_bps; else compute from absolute spread
    spr_bps = None
    try:
        if "spread_bps" in row and row["spread_bps"] is not None:
            spr_bps = float(row["spread_bps"])
        else:
            spr_abs = float(row.get("spread", 0.0) or 0.0)
            spr_bps = (abs(spr_abs) / max(float(px), 1e-9)) * 1e4
    except Exception:
        spr_bps = 0.0
    text = f"{venue} | imb {imb:+.2f} | spr {_spr_bps_phrase(spr_bps)}"
    return {"venue": venue, "imb": imb, "spr_bps": spr_bps, "text": text}


def _ta_and_trend(sym: str, px: float) -> Tuple[Optional[Dict[str, Any]], str, float]:
    """
    Returns (ta_dict_or_None, trend_label, momentum_pct)
      - ta_dict from _ta_bias_from_buffer(sym)
      - trend_label from _trend_label_from_history(sym)
      - momentum_pct from _quick_trend_safe(sym)
    """
    # Warm TA buffer on each tick you touch this
    try:
        _update_ta_buffer(sym, px)
    except Exception:
        pass

    ta   = None
    try:
        ta = _ta_bias_from_buffer(sym)
    except Exception:
        ta = None

    try:
        trend = _trend_label_from_history(sym)
    except Exception:
        trend = "n/a"

    try:
        mom_pct = _quick_trend_safe(sym)
    except Exception:
        mom_pct = 0.0

    return ta, trend, float(mom_pct)


#def _harmonized_bias_and_plan(sym: str, px: float, imb: float, ta: dict|None) -> dict:
def _harmonized_bias_and_plan(sym: str, px: float, imb: float, ta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Use the same action/confidence logic as Decision Cards, with macro penalty.
    Returns dict with fields:
      bias ("LONG"/"SHORT"/"Neutral"), conf, macro_lbl, entry_lo, entry_hi, tp, sl
    """
    mom_pct = _quick_trend_safe(sym)
    action_raw = _action_from_signals(mom_pct, imb, ta or {})
    conf = _conf_from_signals(mom_pct, imb, ta or {})
    blocked, macro_lbl, macro_factor = _macro_penalty(time.time(), sym)
    conf = int(round(conf * macro_factor))

    soft = action_raw in ("LONG?", "SHORT?")
    if conf < 20 or soft:
        bias = "Neutral"
    else:
        bias = "LONG" if action_raw.startswith("LONG") else ("SHORT" if action_raw.startswith("SHORT") else "Neutral")

    # ATR in price terms (robust)
    atrp = _safe_atr_price(sym, px, tf="15m")

    if bias == "LONG":
        entry_lo = px - 0.25 * atrp
        entry_hi = px + 0.10 * atrp
        tp       = px + 1.50 * atrp
        sl       = px - 1.00 * atrp
    elif bias == "SHORT":
        entry_lo = px - 0.10 * atrp
        entry_hi = px + 0.25 * atrp
        tp       = px - 1.50 * atrp
        sl       = px + 1.00 * atrp
    else:
        # Neutral → show an "arming" band; TP/SL are informational only
        entry_lo = px - 0.20 * atrp
        entry_hi = px + 0.20 * atrp
        tp       = px + 0.80 * atrp
        sl       = px - 0.80 * atrp

    return {
        "bias": bias, "conf": conf, "macro_lbl": macro_lbl,
        "entry_lo": entry_lo, "entry_hi": entry_hi, "tp": tp, "sl": sl
    }


#def _format_levels(r: float|None, s: float|None) -> tuple[str, str]:
def _format_levels(r: Optional[float], s: Optional[float]) -> Tuple[str, str]:
    rs = f"R: {r:.2f}" if isinstance(r, (int, float)) else "R: n/a"
    ss = f"S: {s:.2f}" if isinstance(s, (int, float)) else "S: n/a"
    return rs, ss


def _flags_24h_vol(sym: str) -> str:
    """
    Add 24h unusual move / volume flags if available.
    """
    try:
        chg24 = get_24h_change_pct(sym)  # may be None
    except Exception:
        chg24 = None

    try:
        vf = build_vol_flags(sym)  # {'vol_ratio': x, 'atr_pct_15m': y, ...}
    except Exception:
        vf = {}

    bits = []
    if chg24 is not None and abs(chg24) >= PCT_CHANGE_FLAG:
        bits.append(f"🚨 24h {chg24:+.2f}%")
    vr = vf.get("vol_ratio")
    if vr is not None and vr >= VOL_RATIO_FLAG:
        bits.append(f"⚡ vol x{vr:.1f}")
    return (" • " + " | ".join(bits)) if bits else ""

def _liq_snapshot_brief(sym: str, px: float) -> Tuple[str, float, float, str]:
    """
    Returns (brief_text, imb, spr_bps, venue)
    spr_bps is ALWAYS positive bps for consistent display.
    """
    # Observation path intentionally bypasses execution gate so narration continues on stale data.
    d = _liq_snapshot_for_display(sym, px)
    venue = str(d.get("venue") or "—")
    imb = float(d.get("imb", 0.0))
    spr_bps = abs(float(d.get("spr_bps", 0.0)))
    txt = f"{venue} | imb {imb:+.2f} | spr {_spr_bps_phrase(spr_bps)}"
    return txt, imb, spr_bps, venue

def _bias_and_plan(px: float,
                   imb: float,
                   trend_label: str,
                   hist_deque) -> Tuple[str, float, float, Tuple[float, float]]:
    """
    Decide bias from (imbalance + trend), then compute TP/SL via ATR helper.
    Returns (bias_label, tp, sl, (entry_lo, entry_hi))
    """
    if imb >= 0.10 and trend_label.startswith("Up"):
        bias = "LONG"
    elif imb <= -0.10 and trend_label.startswith("Down"):
        bias = "SHORT"
    else:
        bias = "Neutral"

    tp, sl = _tp_sl_from_atr(px, hist_deque, "LONG" if bias == "LONG" else "SHORT")
    if bias == "LONG":
        entry_lo = round(px * 0.998, 2)
        entry_hi = round(px * 1.001, 2)
    elif bias == "SHORT":
        entry_lo = round(px * 0.999, 2)
        entry_hi = round(px * 0.997, 2)
    else:
        entry_lo = entry_hi = 0.0  # informational only
    return bias, float(tp), float(sl), (float(entry_lo), float(entry_hi))


def _overnight_pct_safe(sym: str, px: float) -> Optional[float]:
    try:
        return _overnight_change_pct(sym, px)
    except Exception:
        return None


#def build_morning_overview(symbols: Iterable[str] = ("BTC-USD", "ETH-USD", "XRP-USD")) -> str:
def build_morning_overview(symbols: Iterable[str] = ("BTC-USD","SOL-USD", "ETH-USD", "XRP-USD")) -> str:

    """
    Morning overview: overnight % move, current trend, liquidity highlights (imbalance & spread),
    nearest support/resistance levels, and a simple day plan (bias, entry, TP/SL).
    Also flags >3% 24h move and macro pause.
    """
    lines: list[str] = []
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"Time: {now_utc}\n")
    liq_state, liq_age, _liq_reason = _liq_data_state(_LIQ_PATH)
    if liq_state == "fresh":
        lines.append(f"Data state: fresh (liq age {liq_age:.1f}s)\n")
    else:
        age_txt = "n/a" if liq_age is None else f"{liq_age:.1f}s"
        lines.append(f"Data state: stale (liq age {age_txt}) — decisions hard-stopped\n")
    lines.append("R = resistance, S = support | bps = basis points (100 bps = 1%) | imb = orderbook imbalance, spr = spread.\n")

    for sym in symbols:
        try:
            px_raw = get_spot_price(sym) or get_futures_price(sym)
            if not px_raw:
                lines.append(f"{sym}: (no price)\n")
                continue
            px = float(px_raw)

            _ensure_daily_open(sym)

            # TA + trend + momentum
            ta, trend, mom = _ta_and_trend(sym, px)
            ta_txt = f" | TA {ta['headline']}" if isinstance(ta, dict) and 'headline' in ta else ""

            # Overnight %
            ovc = _overnight_pct_safe(sym, px)
            ovc_s = f"{ovc:+.2f}%" if ovc is not None else "n/a"

            # Liquidity snapshot brief
            liq_txt, imb, liq_spr, liq_venue = _liq_snapshot_brief(sym, px)

            # Nearest S/R (hybrid)
            r, s = _sr_levels_hybrid(sym)
            rs = f"R: {r:.2f}" if r is not None else "R: n/a"
            ss = f"S: {s:.2f}" if s is not None else "S: n/a"

            # Bias + plan
            hist = _PRICE_HISTORY.get(sym, deque())
            bias, tp, sl, (ent_lo, ent_hi) = _bias_and_plan(px, imb, trend, hist)

            # Render block
            lines.append(f"{sym}")
            lines.append(f"• Price: {px:.2f} | Overnight: {ovc_s} | Trend: {trend}{ta_txt}")

            # Plan near the top (action first)
            r_txt = f"{r:.2f}" if r is not None else "n/a"
            s_txt = f"{s:.2f}" if s is not None else "n/a"
            vals = (round(ent_lo, 2), round(ent_hi, 2), round(tp, 2), round(sl, 2))
            collapsed = len(set(vals)) <= 1
            if bias == "Neutral" or collapsed:
                # Use numeric R/S in the text without “R:/S:” labels
                r_txt = f"{r:.2f}" if r is not None else "n/a"
                s_txt = f"{s:.2f}" if s is not None else "n/a"
                lines.append(f"• Plan: Neutral. Consider waiting for a clean break of {r_txt} or loss of {s_txt}.")
            elif bias == "LONG":
                lines.append(f"• Plan: Bias LONG. Entry {ent_lo:.2f}–{ent_hi:.2f}, TP {tp:.2f}, SL {sl:.2f}")
            else:
                # SHORT
                # Note: previous text showed the higher number first; keep that UX
                entry_hi, entry_lo = max(ent_lo, ent_hi), min(ent_lo, ent_hi)
                lines.append(f"• Plan: Bias SHORT. Entry {entry_hi:.2f}–{entry_lo:.2f}, TP {tp:.2f}, SL {sl:.2f}")

            # Liquidity / levels follow the plan
            lines.append(f"• Liquidity: {liq_txt}")
            if liq_venue == "—" and liq_spr == 0:
                lines.append("• (Liquidity data not available for this symbol.)")
            lines.append(f"• Levels: {rs} | {ss}")

            # Unusual volatility flag (>3% overnight)
            if ovc is not None and abs(ovc) >= 3.0:
                lines.append("• ⚠️ Unusual volatility (>3% overnight)")

            lines.append("")  # spacer

        except Exception as e:
            lines.append(f"{sym}: (overview error: {e})\n")

    # Macro note (same behavior as your existing gate)
    if NEWS_FILTER_ENABLED:
        if time.time() < pause_trades_until:
            mins = int((pause_trades_until - time.time()) // 60)
            lines.append(f"📰 Macro/News: Trading paused for ~{mins} min due to recent headline filter.")
        else:
            lines.append("📰 Macro/News: No critical headlines flagged.")
    else:
        lines.append("📰 Macro/News: Filter disabled.")

    return "\n".join(lines)
# ──────────────────────────────────────────────────────────────────────────────
# HTTP endpoints (dashboard & utilities)
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/signals.json")
def signals_json():
    return {"signals": list(LAST_SIGNALS)}


@app.get("/signals.csv")
def signals_csv():
    import csv, io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=[
        "ts","symbol","side","entry","tp","sl",
        "imbalance","spread","venue","sentiment","sentiment_score","macro","confidence"
    ])
    writer.writeheader()
    for row in LAST_SIGNALS:
        writer.writerow(row)
    buf.seek(0)
    return buf.getvalue(), 200, {
        "Content-Type": "text/csv",
        "Content-Disposition": "attachment; filename=signals.csv",
    }


@app.get("/")
def dashboard():
    liq_state, liq_age, _liq_reason = _liq_data_state(_LIQ_PATH)
    data_badge = (
        f"fresh (age {liq_age:.1f}s)"
        if liq_state == "fresh" and liq_age is not None
        else f"stale (age {'n/a' if liq_age is None else f'{liq_age:.1f}s'})"
    )
    html = """
    <html><head><meta http-equiv="refresh" content="10">
    <style>
      body { font-family: Arial, sans-serif; margin: 20px; }
      table { border-collapse: collapse; width: 100%; }
      th, td { border: 1px solid #ccc; padding: 6px; }
      th { background: #eee; }
      .LONG { color: green; font-weight: bold; }
      .SHORT { color: red; font-weight: bold; }
    </style>
    </head><body>
      <h2>Sniper Bot — Live Signals</h2>
      <p><strong>Data state:</strong> {{ data_badge }}</p>
      <table>
      <tr><th>Time</th><th>Symbol</th><th>Side</th><th>Entry</th><th>TP</th><th>SL</th><th>Imb</th><th>Spr</th><th>Venue</th><th>Sentiment</th><th>Macro</th><th>Conf</th></tr>
      {% for s in signals %}
        <tr>
          <td>{{ s.ts | int | datetime }}</td>
          <td>{{ s.symbol }}</td>
          <td class="{{ s.side }}">{{ s.side }}</td>
          <td>{{ '%.4f'|format(s.entry) }}</td>
          <td>{{ '%.4f'|format(s.tp) }}</td>
          <td>{{ '%.4f'|format(s.sl) }}</td>
          <td>{{ '%.2f'|format(s.imbalance) }}</td>
          <td>{{ '%.6f'|format(s.spread) }}</td>
          <td>{{ s.venue }}</td>
          <td>{{ s.sentiment }} ({{ '%.2f'|format(s.sentiment_score) }})</td>
          <td>{{ s.macro }}</td>
          <td>{{ '%.2f'|format(s.confidence) }}</td>
        </tr>
      {% endfor %}
      </table>
    </body></html>"""
    # jinja filter for timestamps
    app.jinja_env.filters['datetime'] = (
        lambda ts: datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    )
    return render_template_string(html, signals=list(LAST_SIGNALS), data_badge=data_badge)


@app.get("/liq/<sym>")
def liq_preview(sym: str):
    try:
        sym = (sym or "").upper()
        note = _liquidity_note(sym)
        return Response((note or f"(no liquidity data for {sym})") + "\n", mimetype="text/plain")
    except Exception as e:
        print(f"[liq_preview error] {e}", flush=True)
        return Response(f"(liquidity error for {sym}: {e})\n", mimetype="text/plain")


@app.get("/daily_now")
def daily_now():
    try:
        syms = symbols_to_watch
        note = build_daily_brief(syms)
        send_telegram("🧭 Daily Brief (on demand)\n" + note)
        return Response(note + "\n", mimetype="text/plain")
    except Exception as e:
        print(f"[daily_now error] {e}", flush=True)
        return Response(f"(daily brief error: {e})\n", mimetype="text/plain", status=500)


@app.get("/healthz")
def healthz():
    liq_state, liq_age, liq_reason = _liq_data_state(_LIQ_PATH)
    vd_btc = _venue_divergence_state("BTC-USD")
    cb_health = _coinbase_feed_health()
    return {
        "status": "ok",
        "data_state": liq_state,
        "liquidity_snapshot_age_sec": liq_age,
        "liquidity_reason": liq_reason,
        "liq_max_age_sec": LIQ_MAX_AGE_SEC,
        "coinbase_feed_health": cb_health,
        "venue_divergence_btc_usd": {
            "status": vd_btc.get("status"),
            "divergence_bps": vd_btc.get("divergence_bps"),
            "block_decisions": bool(vd_btc.get("block_decisions")),
            "reason": vd_btc.get("reason"),
            "warn_bps": vd_btc.get("warn_bps"),
            "hard_bps": vd_btc.get("hard_bps"),
        },
    }

@app.get("/test/freshness")
def test_freshness():
    """
    Deterministic freshness test endpoint (BTC-USD only).
    Uses existing state helpers and enforces stale output constraints.
    """
    symbol = "BTC-USD"
    data_state, liq_age, liq_reason = _liq_data_state(_LIQ_PATH)
    st = _desk_compute_symbol_state(symbol, tf="15m")

    decision = str(st.get("decision", "WAIT"))
    confidence = int(st.get("confidence", 0))
    macro_blocked = bool(st.get("macro_blocked", False))

    # Hard override for deterministic stale behavior contract.
    if data_state != "fresh":
        decision = "WAIT"
        confidence = 0
        blocked = True
    else:
        blocked = bool(macro_blocked)

    return {
        "data_state": data_state,
        "liq_age": liq_age,
        "liq_reason": liq_reason,
        "decision": decision,
        "confidence": max(0, min(100, confidence)),
        "blocked": blocked,
    }


@app.get("/test/venue_divergence")
def test_venue_divergence():
    """
    Deterministic venue divergence test (BTC-USD): Binance fut vs Coinbase spot mids from liquidity snapshot.
    """
    symbol = "BTC-USD"
    vd = _venue_divergence_state(symbol)
    data_state, liq_age, liq_reason = _liq_data_state(_LIQ_PATH)
    st = _desk_compute_symbol_state(symbol, tf="15m")
    cb_health = _coinbase_feed_health()

    decision = str(st.get("decision", "WAIT"))
    confidence = int(st.get("confidence", 0))
    macro_blocked = bool(st.get("macro_blocked", False))
    venue_blocked = bool(vd.get("block_decisions"))

    if data_state != "fresh":
        decision = "WAIT"
        confidence = 0
        blocked = True
    elif venue_blocked:
        decision = "WAIT"
        confidence = 0
        blocked = True
    else:
        blocked = bool(macro_blocked)

    return {
        "symbol": symbol,
        "data_state": data_state,
        "liq_age": liq_age,
        "liq_reason": liq_reason,
        "coinbase_feed_health": cb_health,
        "venue_div_status": vd.get("status"),
        "divergence_bps": vd.get("divergence_bps"),
        "venues": vd.get("venues"),
        "warn_bps": vd.get("warn_bps"),
        "hard_bps": vd.get("hard_bps"),
        "venue_reason": vd.get("reason"),
        "decision": decision,
        "confidence": max(0, min(100, confidence)),
        "blocked": blocked,
    }


@app.get("/test/coinbase_feed_health")
def test_coinbase_feed_health():
    """
    Deterministic Coinbase feed trust endpoint (snapshot/meta only, BTC-focused).
    """
    data_state, liq_age, liq_reason = _liq_data_state(_LIQ_PATH)
    snap = _open_json(_LIQ_PATH) or {}
    cb = _coinbase_feed_meta(snap)
    return {
        "coinbase_feed_health": cb.get("coinbase_feed_health", "degraded"),
        "coinbase_l2_active": bool(cb.get("coinbase_l2_active", False)),
        "coinbase_ticker_fallback_active": bool(cb.get("coinbase_ticker_fallback_active", False)),
        "coinbase_btc_row_valid": bool(cb.get("coinbase_btc_row_valid", False)),
        "collector_build": cb.get("collector_build", ""),
        "data_state": data_state,
        "liq_age": liq_age,
        "liq_reason": liq_reason,
    }


@app.get("/test/runtime_contract")
def test_runtime_contract():
    """
    Deterministic BTC runtime trust contract:
    freshness + venue divergence + Coinbase feed health (informational).
    """
    symbol = "BTC-USD"
    data_state, liq_age, liq_reason = _liq_data_state(_LIQ_PATH)
    vd = _venue_divergence_state(symbol)
    snap = _open_json(_LIQ_PATH) or {}
    cb = _coinbase_feed_meta(snap)
    blocked = (data_state != "fresh") or bool(vd.get("block_decisions"))
    return {
        "data_state": data_state,
        "liq_age": liq_age,
        "liq_reason": liq_reason,
        "venue_div_status": vd.get("status"),
        "venue_reason": vd.get("reason"),
        "divergence_bps": vd.get("divergence_bps"),
        "blocked": bool(blocked),
        "coinbase_feed_health": cb.get("coinbase_feed_health", "degraded"),
        "coinbase_l2_active": bool(cb.get("coinbase_l2_active", False)),
        "coinbase_ticker_fallback_active": bool(cb.get("coinbase_ticker_fallback_active", False)),
        "coinbase_btc_row_valid": bool(cb.get("coinbase_btc_row_valid", False)),
        "collector_build": cb.get("collector_build", ""),
    }


def spot_autopilot_loop():
    """
    24/7 spot momentum scalper:
      - Reuses the same tick momentum as futures (LONG bias only for spot).
      - Opens small position on signal if cooldown/slots allow.
      - Manages TP/SL with market exits.
    """
    from collections import deque
    _hist = {s: deque(maxlen=12) for s in SPOT_SYMBOLS}
    while True:
        try:
            for sym in SPOT_SYMBOLS:
                px = get_spot_price(sym)
                if px is None:
                    continue
                _hist[sym].append(float(px))
                bias = _momentum_side(_hist[sym])
                if bias:
                    _spot_try_open(sym, float(px), bias)
                _spot_manage_exits(sym, float(px))
            time.sleep(2)
        except Exception as e:
            send_telegram(f"[SpotLoop Error] {e}")
            time.sleep(3)



# ──────────────────────────────────────────────────────────────────────────────
# Minimal Telegram command poller (only /daily). No scheduler, no duplicates.
# ──────────────────────────────────────────────────────────────────────────────
def _norm_cmd(txt: str) -> str:
    t = (txt or "").strip()
    if t.startswith("/"):
        t = t.split("@", 1)[0]
    return t.lower()


def _normalize_symbols_arg(arg: str, default_syms: list) -> list:
    """
    Accepts things like: '/daily', '/daily BTC,ETH', '/daily xrp btc'
    Returns normalized symbols with -USD suffix.
    """
    if not arg:
        return list(default_syms)
    raw = [p.strip().upper() for p in arg.replace(",", " ").split() if p.strip()]
    out = []
    for s in raw:
        out.append(s if "-" in s else f"{s}-USD")
    return out

def send_telegram_chunked(prefix: str, body: str, chunk_size: int = 3500):
    """
    Telegram has a ~4096 char cap per message.
    This helper sends long briefs in multiple messages.
    """
    if not body:
        return send_telegram(prefix)
    head = body[:chunk_size]
    tail = body[chunk_size:]
    send_telegram(prefix + head)
    while tail:
        part = tail[:chunk_size]
        tail = tail[chunk_size:]
        send_telegram(part)



# ──────────────────────────────────────────────────────────────────────────────
# Scalp plan builder (BTC/ETH/XRP or any symbol) → used by /scalp
# ──────────────────────────────────────────────────────────────────────────────

def _quick_trend_safe(sym: str) -> float:
    """
    Uses the existing _PRICE_HISTORY[sym] and your _slope_percent() helper
    to estimate short-term momentum (%). Returns 0.0 if not enough data.
    """
    hist = _PRICE_HISTORY.get(sym)
    if not hist or len(hist) < 8:
        return 0.0
    try:
        return _slope_percent(hist)  # already defined in your file
    except Exception:
        return 0.0

def _atr_proxy(price: float) -> float:
    """
    Simple ATR proxy for scalps. Tune if desired.
    ~0.45% of price with a small floor for tiny quotes.
    """
    return max(price * 0.0045, 0.0005 * max(price, 1.0))

def _choose_bias(momentum_pct: float, imb: float) -> str:
    """
    Combine momentum and orderbook tilt into a simple bias.
    """
    if momentum_pct >= 0.05 or imb >= 0.20:
        return "LONG"
    if momentum_pct <= -0.05 or imb <= -0.20:
        return "SHORT"
    return "NEUTRAL"


# ──────────────────────────────────────────────────────────────────────────────
# Lightweight TA (no external deps): EMA, RSI, MACD from price buffer
# ──────────────────────────────────────────────────────────────────────────────
from collections import defaultdict, deque

# Long-ish rolling buffer so we can compute EMA50/EMA200 etc.
_TA_BUF = defaultdict(lambda: deque(maxlen=300))  # symbol -> deque of closes

def _update_ta_buffer(sym: str, price: float):
    """Append the latest close to the TA buffer."""
    try:
        _TA_BUF[sym].append(float(price))
    except Exception:
        pass

def _ema(values: list, n: int) -> float:
    """Exponential moving average of the entire series; returns the latest EMA."""
    if not values or len(values) < 2:
        return float('nan')
    n = max(int(n), 1)
    k = 2.0 / (n + 1.0)
    ema = float(values[0])
    for v in values[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema

def _rsi(values: list, n: int = 14) -> float:
    """Classic RSI; returns the latest RSI value."""
    if len(values) <= n:
        return float('nan')
    gains, losses = 0.0, 0.0
    # seed
    for i in range(1, n + 1):
        ch = values[i] - values[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    avg_gain = gains / n
    avg_loss = losses / n
    # Wilder smoothing
    for i in range(n + 1, len(values)):
        ch = values[i] - values[i - 1]
        gain = max(ch, 0.0)
        loss = max(-ch, 0.0)
        avg_gain = (avg_gain * (n - 1) + gain) / n
        avg_loss = (avg_loss * (n - 1) + loss) / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / max(avg_loss, 1e-12)
    return 100.0 - (100.0 / (1.0 + rs))

def _macd(values: list, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple:
    """
    Returns (macd_line, signal_line, hist) for the latest bar.
    """
    if len(values) < slow + signal:
        return float('nan'), float('nan'), float('nan')
    ema_fast = _ema(values, fast)
    ema_slow = _ema(values, slow)
    macd_line = ema_fast - ema_slow

    # Build a small macd series to get signal EMA
    # We approximate by taking the last (signal * ~3) closes to reduce cost.
    tail_len = max(slow + signal * 3, 60)
    tail_vals = values[-tail_len:]
    macd_series = []
    for i in range(len(tail_vals)):
        ema_f = _ema(tail_vals[: i + 1], fast)
        ema_s = _ema(tail_vals[: i + 1], slow)
        macd_series.append(ema_f - ema_s)
    signal_line = _ema(macd_series, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist

def _ta_bias_from_buffer(sym: str) -> Optional[Dict[str, Any]]:
    """
    Compute simple TA biases & a % bias score from the rolling buffer.
    Returns dict or None if warming up.
    """
    buf = _TA_BUF.get(sym)
    if not buf or len(buf) < 30:  # need some data to say anything meaningful
        return None

    closes = list(buf)

    # Dynamic long EMA length: prefer 200 if we can, else 100, else 50
    long_n = 200 if len(closes) >= 220 else (100 if len(closes) >= 120 else 50)

    ema21  = _ema(closes, 21)
    emalng = _ema(closes, long_n)
    rsi14  = _rsi(closes, 14)
    macd_line, signal_line, hist = _macd(closes, 12, 26, 9)

    ema_bias  = "LONG" if ema21 > emalng else "SHORT"
    if rsi14 >= 70:
        rsi_bias = "SHORT"
    elif rsi14 <= 30:
        rsi_bias = "LONG"
    else:
        rsi_bias = "NEUTRAL"
    macd_bias = "LONG" if hist > 0 else "SHORT"

    # Score: LONG=+1, SHORT=-1, NEUTRAL=0 → convert to % long probability
    score = 0
    for b in (ema_bias, rsi_bias, macd_bias):
        if b == "LONG":
            score += 1
        elif b == "SHORT":
            score -= 1

    # score ∈ [-3..+3] → prob_long = (score+3)/6
    prob_long = (score + 3) / 6.0
    prob_pct = int(round(prob_long * 100))
    headline = f"{prob_pct}% LONG" if prob_pct >= 50 else f"{100 - prob_pct}% SHORT"

    return {
        "ema_bias": ema_bias,
        "rsi_bias": rsi_bias,
        "macd_bias": macd_bias,
        "score": score,
        "prob_long": prob_pct,
        "headline": headline,
        "rsi": float(rsi14),
        "ema_s": float(ema21),
        "ema_l": float(emalng),
        "macd_hist": float(hist),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Decision engine: compress signals → action/trigger/invalidation/confidence
# ──────────────────────────────────────────────────────────────────────────────

def _conf_from_signals(mom_pct: float, imb: float, ta: dict) -> int:
    """
    Blend momentum, orderbook tilt, and TA score into 0..100 confidence.
    mom_pct is % slope (small in your calc), imb ∈ [-1..+1], ta['prob_long'] if available.
    """
    # Normalize momentum to 0..1 band around ±0.30% slope (tweak if your slope scale differs)
    mom_unit = max(0.0, min(1.0, abs(mom_pct) / 0.30))
    # Orderbook tilt magnitude 0..1
    liq_unit = max(0.0, min(1.0, abs(imb)))
    # TA probability centered around 50
    ta_unit = 0.0
    if isinstance(ta, dict) and "prob_long" in ta:
        ta_unit = abs((ta["prob_long"] / 100.0) - 0.5) * 2.0  # 0..1 from neutrality

    # Weights (tweakable)
    w_mom, w_liq, w_ta = 0.35, 0.40, 0.25
    score = w_mom * mom_unit + w_liq * liq_unit + w_ta * ta_unit  # 0..1
    return int(round(score * 100))


def _action_from_signals(mom_pct: float, imb: float, ta: dict) -> str:
    """
    Decide Long/Short/Wait from sign alignment.
    """
    # Signs from components
    mom_s = 1 if mom_pct > 0.05 else (-1 if mom_pct < -0.05 else 0)
    liq_s = 1 if imb >= 0.10 else (-1 if imb <= -0.10 else 0)
    ta_s  = 0
    if isinstance(ta, dict) and "headline" in ta:
        ta_s = 1 if "LONG" in ta["headline"] else (-1 if "SHORT" in ta["headline"] else 0)

    total = mom_s + liq_s + ta_s
    if total >= 2:
        return "LONG"
    if total <= -2:
        return "SHORT"
    # Soft lean if 1 or -1, otherwise wait
    if total == 1:
        return "LONG?"
    if total == -1:
        return "SHORT?"
    return "WAIT"


def _fmt_decision_card(sym: str,
                       px: float,
                       bias_band: tuple,
                       tp: float,
                       sl: float,
                       mom_pct: float,
                       imb: float,
                       ta: dict) -> str:
    """
    Trader-facing decision card.
    - Soft leans (LONG?/SHORT?) render as WAIT with a lean.
    - Confidence bucket + bar for quick read.
    - WAIT shows arming thresholds; actionable states show full trigger + TP/Invalidation.
    """
    action_raw = _action_from_signals(mom_pct, imb, ta)
    conf = _conf_from_signals(mom_pct, imb, ta)

    # Confidence bucket + mini bar (5 blocks)
    conf_bucket = "Low" if conf < 40 else ("Med" if conf < 70 else "High")
    blocks = max(0, min(5, int(round(conf / 20.0))))
    conf_bar = "█" * blocks + "░" * (5 - blocks)

    # Normalize action for display
    if action_raw in ("LONG?", "SHORT?"):
        action = "WAIT"
        lean = " (lean LONG)" if action_raw == "LONG?" else " (lean SHORT)"
    else:
        action = action_raw
        lean = ""

    # Emoji
    emoji = "🟢" if action.startswith("LONG") else ("🔴" if action.startswith("SHORT") else "⏸️")

    # Reason line
    reason_bits = []
    if isinstance(ta, dict) and "headline" in ta:
        reason_bits.append(f"TA {ta['headline']}")
    # Imbalance
    if imb >= 0.10:
        reason_bits.append(f"Bid tilt {imb:+.0%}")
    elif imb <= -0.10:
        reason_bits.append(f"Ask tilt {imb:+.0%}")
    # Momentum
    if abs(mom_pct) >= 0.05:
        reason_bits.append(f"Mom {mom_pct:+.2f}%")
    reason = " | ".join(reason_bits) if reason_bits else "Mixed signals"

    entry_lo, entry_hi = bias_band

    # Build lines
    header = f"{emoji} **Decision:** {action}{lean}  •  **Conf:** {conf}% ({conf_bucket}) {conf_bar}"

    if action == "WAIT":
        trigger_line = f"⏱️ **Arm when:** > {entry_hi:.4f}  or  < {entry_lo:.4f}"
        target_line = ""  # informational triggers only while waiting
    else:
        trigger_line = f"⏱️ **Trigger:** {entry_lo:.4f} → {entry_hi:.4f}"
        target_line  = f"   🎯 **TP:** {tp:.4f}   🛑 **Invalidation:** {sl:.4f}"

    tail = f"ℹ️ {reason}"

    # Compose
    return (
        f"{header}\n"
        f"{trigger_line}{('' if action=='WAIT' else '')}\n"
        f"{target_line}\n"
        f"{tail}"
    ).rstrip()

def build_scalp_plans(symbols: Iterable[str]) -> str:
    lines = []
    symbols = [s.upper().strip() for s in symbols if s and s.strip()]
    for sym in symbols:
        try:
            px = get_spot_price(sym)
            if px is None:
                lines.append(f"⚠️ {sym}: no price available right now.")
                continue
            px = float(px)

            # Orderbook snapshot (for tilt/spread text) and momentum/TA for decision
            _ok, imb, spr, venue = _liquidity_gate(sym, "LONG")
            mom = _quick_trend_safe(sym)
            ta  = _ta_bias_from_buffer(sym) if "_ta_bias_from_buffer" in globals() else None

            # Sizing (same as before)
            atr = _atr_proxy(px)
            entry_lo = px - 0.5 * atr
            entry_hi = px + 0.5 * atr
            tp1_long = px + 2.0 * atr
            sl_long  = px - 1.5 * atr
            tp1_short = px - 2.0 * atr
            sl_short  = px + 1.5 * atr

            # Default (neutral) set
            tp1 = tp1_long
            sl  = sl_long
            # If action is short, we’ll flip after computing the decision
            action_peek = _action_from_signals(mom, imb, ta)
            if action_peek.startswith("SHORT"):
                tp1, sl = tp1_short, sl_short

            # User-facing one-liner of the book
            user_liq = _format_liq_for_user(sym, px) if "_format_liq_for_user" in globals() else ""
            liq_note = _liquidity_note(sym, mode="intraday") or ""
            header = f"🎯 {sym} SCALP PLAN"

            # Decision card (on top)
            card = _fmt_decision_card(sym, px, (entry_lo, entry_hi), tp1, sl, mom, imb, ta or {})

            # Legacy bias line (kept, but card is what traders will use)
            bias = "LONG" if imb >= 0.20 or mom >= 0.20 else ("SHORT" if imb <= -0.20 or mom <= -0.20 else "NEUTRAL")

            lines.append(
                f"{header}\n"
                f"{card}\n\n"
                f"{user_liq or liq_note}\n"
                #f"Bias (legacy): {bias}  (mom={mom:+.02f}%, liq tilt={imb:+.0%})\n"
                f"Entry zone: {entry_lo:.4f} → {entry_hi:.4f}\n"
                f"Targets: {tp1:.4f}\n"
                f"Stop: {sl:.4f}\n"
                f"📈 TA Bias: "
                + (f"{ta['headline']} (EMA:{ta['ema_bias']} / RSI:{ta['rsi_bias']} / MACD:{ta['macd_bias']})"
                   if isinstance(ta, dict) and 'headline' in ta else "warming up…")
                + "\n"
                "Notes: watch nearest walls; if bid wall pulls, tighten risk; if ask wall consumes, trail into strength.\n"
            )
        except Exception as e:
            lines.append(f"❌ {sym} plan error: {e}")

    return "\n".join(lines).rstrip()





def telegram_poller():
    if not TOKEN or not CHAT_ID:
        return
    import time, requests

    # IMPORTANT: long-polling only works if webhook is not set
    try:
        _telegram_clear_webhook()
    except Exception:
        pass

    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    last_update_id = None

    while True:
        try:
            # Use long poll ~50s; request timeout a bit higher
            params = {"timeout": 50}
            if last_update_id is not None:
                params["offset"] = last_update_id + 1

            r = requests.get(url, params=params, timeout=65)
            data = r.json() if r.ok else {}

            for upd in data.get("result", []):
                last_update_id = upd.get("update_id", last_update_id)

                # Support message and edited_message
                msg = upd.get("message") or upd.get("edited_message") or {}
                chat = msg.get("chat") or {}
                chat_id = str(chat.get("id", ""))
                text = (msg.get("text") or "").strip()

                # Only your configured chat
                if not chat_id or chat_id != str(CHAT_ID):
                    continue

                # Debug incoming
                print(f"[TG] update from chat_id={chat_id} text={text}", flush=True)

                cmd = _norm_cmd(text)

                # /start
                if cmd == "/start":
                    send_telegram(
                        "Hi! I’m online.\n"
                        "• Send /daily to get the Daily Brief.\n"
                        "• Add tickers: /daily BTC,SOL,ETH,XRP (defaults to BTC-USD,SOL-USD, ETH-USD, XRP-USD).\n"
                        "• /levels — show nearest bid/ask walls & spread.\n"
                        "• /scalp — quick intraday plan (bias, entry, TP/SL).\n"
                        "• /calendar — macro events + expected/actual + likely BTC reaction.\n"
                        "• You’ll also get live intraday alerts here."
                    )
                    continue

                # /help
                if cmd == "/help":
                    send_telegram(
                        "Commands:\n"
                        "• /daily — Daily Brief for default watchlist\n"
                        "• /daily BTC,SOL,ETH,XRP — Brief for specific symbols\n"
                        "• /levels — Nearest liquidity walls for defaults\n"
                        "• /levels BTC,SOL,ETH,XRP — Walls for specific symbols\n"
                        "• /scalp — Intraday scalp plan for defaults\n"
                        "• /scalp BTC,SOL,ETH,XRP — Scalp plans for specific symbols\n"
                        "• /morning — Full morning overview (OVN %, trend, liq, S/R, plan)\n"
                        "• /morning BTC,SOL,ETH — Same but for specific symbols\n"
                        "• /decision — TA+Liquidity decision cards (default 15m)\n"
                        "• /decision 5m BTC,SOL,ETH — Decision cards for TF & symbols\n"
                        "• /calendar — Macro calendar with expected/actual playbook (default 7d)\n"
                        "• /calendar 48 — Same, next 48h\n"
                        "• /desk — Pro-style TA desk snapshot (ongoing model)\n"
                        "• /ta BTC-USD 5m — Plain-English TA read (less confusing)\n"
                        "• /health — Bot health check\n"
                    )
                    print("[TG] sent /help", flush=True)
                    continue



                # /advise (optional timeframe + symbols, e.g., "/advise 5m BTC,ETH")
                if cmd == "/advise" or text.lower().startswith("/advise"):
                    try:
                        parts = text.split(None, 1)
                        arg = parts[1] if len(parts) > 1 else ""
                        tf, syms = _parse_tf_and_symbols(arg, symbols_to_watch)
                        note = build_advice_cards(tuple(syms), timeframe=tf)
                        send_telegram("🧠 Trade Advice\n" + note)
                        print(f"[TG] sent /advise {tf} for {', '.join(syms)}", flush=True)
                    except Exception as e:
                        send_telegram(f"(advise error: {e})")
                        print(f"[TG] /advise error: {e}", flush=True)
                    continue

                # /daily (optional args)
                if cmd == "/daily" or text.lower().startswith("/daily"):
                    try:
                        parts = text.split(None, 1)
                        syms = _normalize_symbols_arg(parts[1] if len(parts) > 1 else "", symbols_to_watch)
                        note = build_daily_brief(tuple(syms))
                        send_telegram_chunked("🧭 Daily Brief (via Telegram)\n", note)
                        print(f"[TG] sent /daily brief for {', '.join(syms)}", flush=True)
                    except Exception as e:
                        send_telegram(f"(daily brief error: {e})")
                        print(f"[TG] /daily error: {e}", flush=True)
                    continue

                # /levels (optional args)
                if cmd == "/levels" or text.lower().startswith("/levels"):
                    try:
                        parts = text.split(None, 1)
                        syms = _normalize_symbols_arg(parts[1] if len(parts) > 1 else "", symbols_to_watch)
                        lines = []
                        for s in syms:
                            px = get_spot_price(s)
                            if not px:
                                lines.append(f"{s}: (no spot price)")
                                continue
                            lines.append(_fmt_levels_for_user(s, float(px)))
                        body = "\n".join(lines) if lines else "(no symbols)"
                        send_telegram_chunked("📍 Nearest Liquidity Levels\n", body)
                        print(f"[TG] sent /levels for {', '.join(syms)}", flush=True)
                    except Exception as e:
                        send_telegram(f"(levels error: {e})")
                        print(f"[TG] /levels error: {e}", flush=True)
                    continue

                # /decision (optional timeframe + symbols)
                # examples:
                #   /decision                  → defaults to 15m and your default watchlist
                #   /decision 5m               → 5m for default symbols
                #   /decision 4h BTC,ETH,XRP   → 4h for specific symbols
                if cmd == "/decision" or text.lower().startswith("/decision"):
                    try:
                        parts = text.split(None, 1)
                        arg = parts[1] if len(parts) > 1 else ""
                        tf, syms = _parse_tf_and_symbols(arg, symbols_to_watch)
                        # primary call; if an older build_decision_cards without 'timeframe' exists, fall back
                        try:
                            note = build_decision_cards(tuple(syms), timeframe=tf)
                        except TypeError:
                            note = build_decision_cards(tuple(syms))
                        send_telegram("🎚️ Decision Cards\n" + note)
                        print(f"[TG] sent /decision {tf} for {', '.join(syms)}", flush=True)
                    except Exception as e:
                        send_telegram(f"(decision error: {e})")
                        print(f"[TG] /decision error: {e}", flush=True)
                    continue

                # /scalp (optional timeframe + symbols) — advisor style, short horizon
                if cmd == "/scalp" or text.lower().startswith("/scalp"):
                    try:
                        parts = text.split(None, 1)
                        arg = parts[1] if len(parts) > 1 else ""
                        tf, syms = _parse_tf_and_symbols(arg, symbols_to_watch)
                        note = build_advice_cards(tuple(syms), timeframe=tf)
                        send_telegram("⚡ Scalp Advisor\n" + note)
                        print(f"[TG] sent /scalp {tf} for {', '.join(syms)}", flush=True)
                    except Exception as e:
                        send_telegram(f"(scalp error: {e})")
                        print(f"[TG] /scalp error: {e}", flush=True)
                    continue

                # /morning (optional args like "/morning BTC,ETH")
                if cmd == "/morning" or text.lower().startswith("/morning"):
                    try:
                        parts = text.split(None, 1)
                        syms = _normalize_symbols_arg(parts[1] if len(parts) > 1 else "", symbols_to_watch)
                        note = build_morning_overview(tuple(syms))
                        send_telegram_chunked("🌅 Morning Overview\n", note)
                        print(f"[TG] sent /morning for {', '.join(syms)}", flush=True)
                    except Exception as e:
                        send_telegram(f"(morning overview error: {e})")
                        print(f"[TG] /morning error: {e}", flush=True)
                    continue

                # /calendar (optional hours, e.g. "/calendar 48")
                if cmd == "/calendar" or text.lower().startswith("/calendar"):
                    try:
                        parts = text.split(None, 1)
                        hours = MACRO_CALENDAR_LOOKAHEAD_HOURS
                        if len(parts) > 1:
                            try:
                                hours = max(1, min(168, int(parts[1].strip().split()[0])))
                            except Exception:
                                hours = MACRO_CALENDAR_LOOKAHEAD_HOURS
                        note = build_macro_calendar_brief(within_hours=hours, include_playbook=True)
                        send_telegram_chunked("🗓️ Macro Calendar\n", note)
                        print(f"[TG] sent /calendar {hours}h", flush=True)
                    except Exception as e:
                        send_telegram(f"(calendar error: {e})")
                        print(f"[TG] /calendar error: {e}", flush=True)
                    continue

                # /desk (on-demand desk snapshot)
                if cmd == "/desk" or text.lower().startswith("/desk"):
                    try:
                        note, _states = build_desk_brief(tuple(symbols_to_watch), timeframe=DESK_BRIEF_TIMEFRAME)
                        send_telegram(note)
                        print("[TG] sent /desk snapshot", flush=True)
                    except Exception as e:
                        send_telegram(f"(desk error: {e})")
                        print(f"[TG] /desk error: {e}", flush=True)
                    continue

                # /ta [symbol] [timeframe] -> plain-English TA read
                # examples: /ta, /ta ETH-USD, /ta BTC-USD 5m
                if cmd == "/ta" or text.lower().startswith("/ta"):
                    try:
                        parts = text.split()
                        sym = "BTC-USD"
                        tf = "5m"
                        if len(parts) >= 2:
                            p = parts[1].upper().strip()
                            sym = p if "-" in p else f"{p}-USD"
                        if len(parts) >= 3:
                            p2 = parts[2].lower().strip()
                            if p2 in {"5m", "15m", "1h", "4h"}:
                                tf = p2
                        send_telegram(_plain_ta_read(sym, timeframe=tf))
                        print(f"[TG] sent /ta {sym} {tf}", flush=True)
                    except Exception as e:
                        send_telegram(f"(ta error: {e})")
                        print(f"[TG] /ta error: {e}", flush=True)
                    continue

                # /health
                if cmd == "/health":
                    send_telegram("ok")
                    continue

        # --- Specific network exceptions for calmer logs ---
        except requests.exceptions.ReadTimeout:
            # Normal on long-poll when no messages arrive
            time.sleep(0.5)
            continue
        except requests.exceptions.ConnectionError as e:
            print(f"[TG] poll connection error: {e}", flush=True)
            time.sleep(3)
            continue

        # --- Catch-all (kept, slightly longer backoff) ---
        except Exception as e:
            print(f"[TG] poll error: {e}", flush=True)
            time.sleep(5)
            continue
# ──────────────────────────────────────────────────────────────────────────────
# Boot
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Re-resolve flags per run
    FUTURES_SIGNALS_ENABLED = os.getenv("FUTURES_SIGNALS_ENABLED", "false").lower() == "true"
    POLLER_ENABLED = (
        os.getenv("TELEGRAM_POLLER_ENABLED", "false").lower() == "true"
        or os.getenv("TELEGRAM_POLLING_ENABLED", "false").lower() == "true"
    )

    # Startup banner reflects poller mode
    send_telegram(
        "✅ Sniper Bot Started "
        + ("(Telegram poller ON: /daily enabled)" if POLLER_ENABLED else "(baseline: no Telegram poller)")
    )

    # Startup health check (optional fail-fast)
    if STARTUP_HEALTHCHECK and not run_startup_health_check():
        send_telegram("⛔ Exiting due to failed startup health check.")
        raise SystemExit(1)

    # Perp signals mode banner
    send_telegram(
        "🟢 Perp Signals ENABLED (alerts only; no live orders)"
        if FUTURES_SIGNALS_ENABLED
        else "🟡 Perp Signals DISABLED (set FUTURES_SIGNALS_ENABLED=true to enable)"
    )

    # Start Telegram poller (only /daily), if enabled
    if POLLER_ENABLED:
        # Ensure no webhook is set; otherwise getUpdates won’t deliver messages
        try:
            requests.get(f"https://api.telegram.org/bot{TOKEN}/deleteWebhook", timeout=8)
        except Exception:
            pass
        threading.Thread(target=telegram_poller, daemon=True).start()
        send_telegram("🤖 Telegram /daily command enabled")

    # Optional test probe (unchanged)
    if TEST_MODE:
        send_telegram("🔍 TEST MODE is ON: attempting test trade on Futures…")
        try:
            price = get_futures_price(TEST_SYMBOL)
            if not price:
                raise ValueError(f"Could not fetch price for {TEST_SYMBOL}")
            balance_usd = get_usd_balance()
            contracts = calculate_contract_size(balance_usd, price)
            usd_size = max(10, contracts * price)
            if AUTO_TRADE_ENABLED:
                futures_market_buy(TEST_SYMBOL, usd_size)
                send_telegram(f"🚀 TEST FUTURES TRADE: BUY {TEST_SYMBOL} {contracts} contracts at {price}")
            else:
                send_telegram(f"(DRY-RUN) Would BUY {TEST_SYMBOL} {contracts} contracts at {price}")
        except Exception as e:
            send_telegram(f"❌ TEST FUTURES TRADE Error: {e}")

    # Workers
    threading.Thread(target=fast_breakdown_loop, daemon=True).start()
    print("✅ [BOOT] fast_breakdown_loop thread started")

    if MACRO_CALENDAR_ALERTS_ENABLED:
        threading.Thread(target=macro_calendar_loop, daemon=True).start()
        send_telegram("🟢 Macro calendar worker started")
    else:
        send_telegram("🟡 Macro calendar alerts disabled (set MACRO_CALENDAR_ALERTS_ENABLED=true)")

    if DESK_BRIEF_ENABLED:
        threading.Thread(target=desk_brief_loop, daemon=True).start()
        send_telegram("🟢 TA Desk worker started")
    else:
        send_telegram("🟡 TA Desk updates disabled (set DESK_BRIEF_ENABLED=true)")

    if SPOT_AUTOPILOT_ENABLED:
        threading.Thread(target=spot_autopilot_loop, daemon=True).start()
        send_telegram("🟢 Spot Autopilot ENABLED")
    else:
        send_telegram("🟡 Spot Autopilot DISABLED (set SPOT_AUTOPILOT_ENABLED=true to enable)")

    # Liquidity snapshot sanity
    try:
        print(f"[LIQ] Reading snapshot from: {_LIQ_PATH}", flush=True)
        if not os.path.isfile(_LIQ_PATH):
            warn_once("liq_missing", f"[WARN] Liquidity snapshot not found at {_LIQ_PATH}. Run liquidity_phase1_free.py or set LIQ_SNAPSHOT.")
        else:
            age = time.time() - os.path.getmtime(_LIQ_PATH)
            if age > 15:
                warn_once("liq_stale", f"[WARN] Liquidity snapshot looks stale ({age:.1f}s old).")
    except Exception as _e:
        warn_once("liq_path", f"[WARN] Could not inspect liquidity snapshot: {_e}")

    # Run web server last
    app.run(host="0.0.0.0", port=5001, use_reloader=False)