# market_stats.py
import statistics
from datetime import datetime, timezone
import ccxt
import pandas as pd
from ta.volatility import AverageTrueRange
from typing import Optional, Dict, Any

_CCXT = None

def _ex():
    global _CCXT
    if _CCXT is None:
        _CCXT = ccxt.coinbase()
    return _CCXT

def _to_ccxt(sym: str) -> str:
    # "BTC-USD" -> "BTC/USD"
    return sym.replace("-", "/")

def _ohlcv(sym: str, timeframe: str = "1h", limit: int = 50):
    ex = _ex()
    return ex.fetch_ohlcv(_to_ccxt(sym), timeframe=timeframe, limit=limit)

def get_24h_change_pct(sym: str) -> Optional[float]:
    """
    Uses 1h candles: compares current close vs close 24h ago.
    """
    try:
        rows = _ohlcv(sym, "1h", 50)
        if len(rows) < 26:
            return None
        prev = rows[-25][4]  # close 24h ago
        last = rows[-1][4]   # last close
        if prev == 0:
            return None
        return (last - prev) / prev * 100.0
    except Exception:
        return None

def build_vol_flags(sym: str) -> Dict[str, Optional[float]]:
    """
    Returns dict: {
      "unusual_vol": bool,
      "vol_ratio": float,
      "atr_pct_15m": float
    }
    """
    out: Dict[str, Any] = {"unusual_vol": False, "vol_ratio": None, "atr_pct_15m": None}
    try:
        # Unusual volume: last 1h vs median of prior 24h
        rows = _ohlcv(sym, "1h", 50)
        if len(rows) >= 26:
            last_vol = rows[-1][5]
            base = [r[5] for r in rows[-25:-1]]
            med = statistics.median(base) if base else None
            if med and med > 0:
                ratio = last_vol / med
                out["vol_ratio"] = ratio
                out["unusual_vol"] = ratio >= 2.0  # tune threshold if needed

        # ATR% on 15m (past ~1 day)
        rows15 = _ohlcv(sym, "15m", 100)
        if len(rows15) >= 20:
            df = pd.DataFrame(rows15, columns=["ts","o","h","l","c","v"])
            atr = AverageTrueRange(df["h"], df["l"], df["c"], window=14, fillna=True)
            last_close = float(df["c"].iloc[-1])
            atrp = float(atr.average_true_range().iloc[-1]) / last_close * 100.0 if last_close > 0 else None
            out["atr_pct_15m"] = atrp
    except Exception:
        pass
    return out

def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    sign = "+" if x >= 0 else ""
    return f"{sign}{x:.2f}%"