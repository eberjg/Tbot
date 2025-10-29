# macro_guard.py
import os, json, datetime
from typing import Any, Dict, List, Optional, Tuple

def _parse_iso_ts(s: str) -> datetime.datetime:
    """
    Robust ISO8601 -> aware UTC datetime
    Accepts 'Z' or '+00:00'.
    """
    if not s:
        raise ValueError("empty timestamp")
    s = s.strip()
    # Normalize 'Z' to '+00:00'
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    # Some files may omit colon in TZ; let fromisoformat handle common cases
    dt = datetime.datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # assume UTC if naive
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)

def _load_json(path: str) -> List[Dict[str, Any]]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        # Accept dict with 'events' key too
        if isinstance(data, dict) and isinstance(data.get("events"), list):
            return data["events"]
    except Exception:
        pass
    return []

def _base_from_symbol(sym: str) -> str:
    s = (sym or "").upper()
    return s.split("-", 1)[0] if s else s

def _symbol_matches(event_symbols: List[str], query_symbol: str) -> bool:
    """
    Match rules:
      - "*" matches everything
      - "BTC" matches BTC-USD, BTC-USDT, etc.
      - Full symbol must match exactly (e.g., "BTC-USD").
      - If the event omits 'symbols', treat as global (matches all).
    """
    if not event_symbols:
        return True  # global event
    q_full = (query_symbol or "").upper()
    q_base = _base_from_symbol(q_full)
    for es in event_symbols:
        es = (es or "").upper()
        if es == "*" or es == q_full or es == q_base:
            return True
    return False

class MacroGuard:
    """
    Loads a JSON file with macro events and decides whether trading should be paused
    around those events. Supports two schemas:

    Your schema:
      { "name": "US CPI", "time_utc": "2025-09-11T12:30:00Z", "impact": "high",
        "symbols": ["*"], "window_min": 45 }

    Alternate schema:
      { "title": "US CPI", "time": "2025-09-11T12:30:00+00:00", "impact": "high",
        "symbols": ["BTC-USD"], "window_min": 60 }
    """

    def __init__(self,
                 path: Optional[str] = None,
                 default_window_min: Optional[int] = None,
                 impacts: Optional[List[str]] = None) -> None:
        self.path = path or os.getenv("MACRO_EVENTS_FILE", "macro_events.json")
        self.default_window_min = (
            default_window_min
            if default_window_min is not None
            else int(os.getenv("MACRO_BLOCK_WINDOW_MIN", "45"))
        )
        self.impacts = (
            impacts
            if impacts is not None
            else [s.strip().lower() for s in os.getenv("MACRO_BLOCK_IMPACTS", "high,medium").split(",") if s.strip()]
        )
        self.enabled = os.getenv("MACRO_GUARD_ENABLED", "true").lower() == "true"
        self._events: List[Dict[str, Any]] = []
        self._load()

    # ---------- Public API ----------

    def is_blocked(self, now_utc: datetime.datetime, symbol: str) -> Tuple[bool, str]:
        """
        Returns (blocked, reason_text).
        Block if an event with acceptable 'impact' is within its window around now_utc
        and matches 'symbol'.
        """
        if not self.enabled:
            return (False, "")
        try:
            if now_utc.tzinfo is None:
                now_utc = now_utc.replace(tzinfo=datetime.timezone.utc)
            now_utc = now_utc.astimezone(datetime.timezone.utc)
        except Exception:
            now_utc = datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)

        for ev in self._events:
            impact = str(ev.get("impact", "")).lower()
            if self.impacts and impact not in self.impacts:
                continue

            # Accept both 'time_utc' and 'time'
            when_str = ev.get("time_utc") or ev.get("time") or ""
            try:
                when = _parse_iso_ts(when_str)
            except Exception:
                continue

            # Per-event window override; fallback to default
            wmin = ev.get("window_min")
            try:
                window_min = int(wmin) if wmin is not None else self.default_window_min
            except Exception:
                window_min = self.default_window_min

            start = when - datetime.timedelta(minutes=window_min)
            end   = when + datetime.timedelta(minutes=window_min)

            if start <= now_utc <= end:
                ev_syms = ev.get("symbols") or []
                if _symbol_matches(ev_syms, symbol):
                    title = ev.get("name") or ev.get("title") or "Macro Event"
                    reason = f"{title} [{impact}] — within ±{window_min}m of {when.isoformat()}"
                    return (True, reason)

        return (False, "")

    def upcoming(self,
             now: Optional[datetime.datetime] = None,
             within_hours: int = 72) -> List[Dict[str, Any]]:
        """
        Return upcoming events (within `within_hours`) sorted by time.
        """
        now = now or datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc)
        horizon = now + datetime.timedelta(hours=within_hours)
        out: List[Dict[str, Any]] = []
        for ev in self._events:
            when_str = ev.get("time_utc") or ev.get("time") or ""
            try:
                when = _parse_iso_ts(when_str)
            except Exception:
                continue
            if now <= when <= horizon:
                out.append({
                    "time": when.isoformat(),
                    "title": ev.get("name") or ev.get("title") or "",
                    "impact": str(ev.get("impact", "")).lower(),
                    "symbols": ev.get("symbols") or [],
                    "window_min": ev.get("window_min", self.default_window_min),
                })
        out.sort(key=lambda x: x["time"])
        return out

    def reload(self) -> None:
        """Reload events from disk."""
        self._load()

    # ---------- Internal ----------

    def _load(self) -> None:
        self._events = _load_json(self.path)