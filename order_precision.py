#cat > order_precision.py <<'EOF'
from decimal import Decimal, ROUND_DOWN
import ccxt

_CCXT = None

def _ex():
    global _CCXT
    if _CCXT is None:
        _CCXT = ccxt.coinbase()
        _CCXT.load_markets()
    return _CCXT

def _to_ccxt(sym: str) -> str:
    return sym.replace("-", "/")

def _get_market(sym: str):
    ex = _ex()
    return ex.markets.get(_to_ccxt(sym))

def truncate_to_step(value: float, step: float) -> float:
    if not step or step <= 0:
        return float(value)
    q = Decimal(str(step))
    v = Decimal(str(value))
    return float((v // q) * q)

def apply_amount_precision(sym: str, amount: float) -> float:
    """
    Rounds DOWN to the exchange's allowed increment/precision for base size.
    """
    m = _get_market(sym)
    if not m:
        return float(amount)
    # Prefer step size over decimals
    step = None
    try:
        step = m.get("limits", {}).get("amount", {}).get("min")
        # coinbase often exposes "baseMinSize" in info; fallback
        info = m.get("info", {})
        step = float(info.get("base_increment", step)) if info else step
    except Exception:
        step = None

    if step:
        return truncate_to_step(amount, float(step))

    # Fallback to precision decimals
    prec = None
    try:
        prec = m.get("precision", {}).get("amount")
    except Exception:
        prec = None

    if isinstance(prec, int) and prec >= 0:
        q = Decimal(1).scaleb(-prec)  # 10^-prec
        return float(Decimal(str(amount)).quantize(q, rounding=ROUND_DOWN))

    return float(amount)
#EOF