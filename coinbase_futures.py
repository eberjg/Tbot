# coinbase_futures.py — Coinbase Advanced Trade JWT (spec‑aligned, resilient)

import os
import json
import time
import base64
from typing import Dict, Optional, Tuple

import requests
import jwt
from dotenv import load_dotenv
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import ed25519
from functools import lru_cache

# ──────────────────────────────────────────────────────────────────────────────
# Env & constants
# ──────────────────────────────────────────────────────────────────────────────
load_dotenv(dotenv_path=".env", override=True)
PORTFOLIO_ID = os.getenv("COINBASE_PORTFOLIO_ID", "").strip()
KEY_PATH     = os.getenv("COINBASE_API_SECRET_PATH", "jwt-cdp-api-key.json").strip()

_RAW_HOST    = os.getenv("COINBASE_API_HOST", "https://api.coinbase.com").strip()
if _RAW_HOST.startswith(("http://", "https://")):
    BASE_ORIGIN = _RAW_HOST.rstrip("/")
else:
    BASE_ORIGIN = f"https://{_RAW_HOST.lstrip('/')}"

BASE_HOST   = BASE_ORIGIN.replace("https://", "").replace("http://", "")
HTTP_TIMEOUT = 10
DEBUG_JWT    = os.getenv("DEBUG_JWT", "0") == "1"
KEY_NAME     = os.getenv("COINBASE_API_KEY_NAME", "").strip()  # optional, only used when provided

print(f"[PATH] Using key at: {os.path.abspath(KEY_PATH)}")
print(f"[HOST] Using origin: {BASE_ORIGIN}")

# throttle noisy 401/accepted logs
AUTH_LOG_COOLDOWN = float(os.getenv("AUTH_LOG_COOLDOWN", "30"))  # seconds
_last_auth_log = {}  # (tag, variant, uri_mode, path) -> last_ts

def _auth_log(tag: str, variant: str, uri_mode: str, path: str, msg: str):
    now = time.time()
    key = (tag, variant, uri_mode, path.split("?", 1)[0])
    last = _last_auth_log.get(key, 0.0)
    if (now - last) >= AUTH_LOG_COOLDOWN:
        print(msg, flush=True)
        _last_auth_log[key] = now

# Filled during key load when ECDSA JSON uses "name"/"keyName"
KEY_FULL_NAME = ""  # organizations/.../apiKeys/<uuid> OR projects/.../apiKeys/<uuid>

_warned = set()
def warn_once(key: str, msg: str):
    if key in _warned:
        return
    _warned.add(key)
    print(msg, flush=True)

def _abs_url(path: str) -> str:
    if not path.startswith("/"):
        path = "/" + path
    return BASE_ORIGIN + path

# ──────────────────────────────────────────────────────────────────────────────
# Key loading (Ed25519 JSON OR ECDSA JSON OR raw PEM/DER)
# ──────────────────────────────────────────────────────────────────────────────
def _load_key_and_keyid(path: str) -> Tuple[object, str, str]:
    """
    Returns (PRIVATE_KEY_OBJECT, KEY_ID (uuid str), JWT_ALG ('EdDSA'|'ES256'))

    Supports:
      • Ed25519 JSON: {"id":"<uuid>","privateKey":"<base64 seed or seed+pub>"}   -> EdDSA
      • ECDSA JSON  : {"name"/"keyName":".../apiKeys/<uuid>","privateKey":"-----BEGIN ..."} -> ES256
      • Raw PEM/DER (ECDSA): requires COINBASE_API_KEY_ID env                     -> ES256
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"❌ Key file not found: {path}")

    if path.endswith(".json"):
        with open(path, "r") as f:
            data = json.load(f)

        # Case 1: Ed25519 JSON (no PEM header)
        if "id" in data and isinstance(data.get("privateKey"), str) and not data["privateKey"].startswith("-----BEGIN"):
            key_id = data["id"]
            raw = base64.b64decode(data["privateKey"])
            # 32 bytes seed or 64 bytes seed+pub are common
            if len(raw) == 32:
                pk = ed25519.Ed25519PrivateKey.from_private_bytes(raw)
                return pk, key_id, "EdDSA"
            if len(raw) == 64:
                pk = ed25519.Ed25519PrivateKey.from_private_bytes(raw[:32])
                return pk, key_id, "EdDSA"
            # Sometimes ECDSA is base64-wrapped in JSON; try DER/PEM loaders
            try:
                pk = serialization.load_der_private_key(raw, password=None, backend=default_backend())
                return pk, key_id, "ES256"
            except Exception:
                pk = serialization.load_pem_private_key(raw, password=None, backend=default_backend())
                return pk, key_id, "ES256"

        # Case 2: ECDSA JSON (PEM + 'name' or 'keyName')
        pem = data.get("privateKey")
        name_field = (data.get("name") or data.get("keyName") or "").strip()
        if isinstance(pem, str) and pem.startswith("-----BEGIN") and name_field:
            key_id = name_field.split("/")[-1]
            pk = serialization.load_pem_private_key(pem.encode(), password=None, backend=default_backend())
            global KEY_FULL_NAME
            KEY_FULL_NAME = name_field  # for kid/sub
            return pk, key_id, "ES256"

        raise ValueError("❌ Invalid JSON key. Expected Ed25519 {id, privateKey(b64)} or ECDSA {name/keyName, privateKey(PEM)}")

    # Raw PEM/DER (ECDSA)
    blob = open(path, "rb").read()
    for loader in (serialization.load_pem_private_key, serialization.load_der_private_key):
        try:
            pk = loader(blob, password=None, backend=default_backend())
            key_id = os.getenv("COINBASE_API_KEY_ID", "").strip()
            if not key_id:
                raise EnvironmentError("❌ COINBASE_API_KEY_ID must be set when using a raw PEM/DER key")
            return pk, key_id, "ES256"
        except Exception:
            continue
    raise ValueError("❌ Could not parse key file (not valid PEM/DER or wrong password).")

# Load key now
PRIVATE_KEY, KEY_ID, JWT_ALG = _load_key_and_keyid(KEY_PATH)
print(f"[KEY] id={KEY_ID} alg={JWT_ALG}")
# throttle noisy 401 logs
AUTH_LOG_COOLDOWN = float(os.getenv("AUTH_LOG_COOLDOWN", "30"))  # seconds
_last_auth_log = {}  # (variant, uri_mode, path) -> last_ts

# ──────────────────────────────────────────────────────────────────────────────
# JWT payload/header builders (variants A/B/C) + two uri modes
# ──────────────────────────────────────────────────────────────────────────────
def _ensure_leading_slash(p: str) -> str:
    return p if p.startswith("/") else f"/{p}"

def _uri_value(uri_mode: str, method: str, path: str) -> str:
    """
    Coinbase JWT 'uri' can be either:
      - 'path' mode:  '/api/v3/brokerage/...?(...)'
      - 'host' mode:  'GET api.coinbase.com/api/v3/brokerage/...' (no scheme, no query)
    """
    p = _ensure_leading_slash(path)
    if uri_mode == "path":
        return p
    # host mode
    p_no_query = p.split("?", 1)[0]
    return f"{method.upper()} {BASE_HOST}{p_no_query}"

def _jwt_payload_headers(variant: str, method: str, path: str, body: str = "", *, uri_mode: str = "path"):
    now = int(time.time())
    uri = _uri_value(uri_mode, method, path)
    full_subject = (KEY_FULL_NAME or KEY_NAME or KEY_ID)

    if variant in ("A", "B"):
        payload = {
            "iss": "cdp",
            "sub": full_subject,
            "aud": "retail_rest_api",
            "nbf": now, "iat": now, "exp": now + 120,
            "uri": uri,
            "method": method.upper(),
        }
        if body:
            payload["body"] = body
        headers = {"kid": full_subject, "typ": "JWT"}
        return payload, headers

    # C: legacy-ish
    payload = {
        "iss": KEY_ID,
        "sub": KEY_ID,
        "aud": "coinbase",
        "nbf": now, "iat": now, "exp": now + 120,
        "uri": uri,
        "method": method.upper(),
    }
    if body:
        payload["body"] = body
    headers = {"kid": KEY_ID, "typ": "JWT"}
    return payload, headers

def _auth_headers_variant(variant: str, method: str, path: str, body: str, uri_mode: str) -> Dict[str, str]:
    payload, headers = _jwt_payload_headers(variant, method, path, body, uri_mode=uri_mode)
    token = jwt.encode(payload, PRIVATE_KEY, algorithm=JWT_ALG, headers=headers)
    tok = token if isinstance(token, str) else token.decode("utf-8")
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    if PORTFOLIO_ID:
        h["x-cb-portfolio"] = PORTFOLIO_ID
    return h

# ──────────────────────────────────────────────────────────────────────────────
# Resilient HTTP sender
# ──────────────────────────────────────────────────────────────────────────────
def _send(
    method: str,
    path: str,
    *,
    body: str = "",
    timeout: int = HTTP_TIMEOUT,
    extra_headers: Optional[Dict[str, str]] = None
) -> Optional[requests.Response]:
    url = _abs_url(path)
    # Prefer B when KEY_FULL_NAME present (key “name” case), else start with A (id case)
    variant_order = ("B", "A", "C") if (KEY_FULL_NAME or KEY_NAME) else ("A", "B", "C")
    uri_modes = ("path", "host")
    last: Optional[requests.Response] = None

    for uri_mode in uri_modes:
        for v in variant_order:
            try:
                headers = _auth_headers_variant(v, method, path, body, uri_mode=uri_mode)
                if extra_headers:
                    headers.update(extra_headers)
                data = body if (method.upper() != "GET" and body) else None

                resp = requests.request(method, url, headers=headers, data=data, timeout=timeout)

                # 401 handling & throttled logging
                if resp.status_code == 401:
                    if DEBUG_JWT:
                        _auth_log("401", v, uri_mode, path, f"[AUTH] {v}/{uri_mode} -> 401 Unauthorized")
                        wa = resp.headers.get("WWW-Authenticate")
                        if wa:
                            _auth_log("401wa", v, uri_mode, path, f"[AUTH] 401 WWW-Authenticate: {wa}")
                    # single quick retry with the same variant/mode
                    time.sleep(0.15)
                    resp = requests.request(method, url, headers=headers, data=data, timeout=timeout)
                    if resp.status_code == 401:
                        last = resp
                        continue  # try next variant/mode

                # Accepted → also throttle printing
                if DEBUG_JWT:
                    _auth_log("OK", v, uri_mode, path, f"[AUTH] accepted with {v}/{uri_mode} — status {resp.status_code}")

                return resp
            except Exception as e:
                if DEBUG_JWT:
                    _auth_log("EXC", v, uri_mode, path, f"[AUTH] {v}/{uri_mode} exception: {e}")
                last = None
                continue
    return last



from functools import lru_cache
def _round_to_increment(value: float, increment: float, *, up: bool = False) -> float:
    if increment <= 0:
        return value
    q = value / increment
    q = (int(q) + (1 if up and q != int(q) else 0))
    return round(q * increment, 12)

@lru_cache(maxsize=256)
def _get_product_increments(product_id: str) -> dict:
    resp = _send("GET", f"/api/v3/brokerage/products/{product_id}", timeout=max(HTTP_TIMEOUT, 10))
    if not resp or resp.status_code != 200:
        return {"base_increment": None, "quote_increment": None}
    j = resp.json() or {}
    md = j.get("price_increment") or {}
    # Coinbase mixes naming across payloads; try multiple keys
    base_inc  = j.get("base_increment")  or j.get("baseIncrement")  or j.get("base_min_size")
    quote_inc = j.get("quote_increment") or j.get("quoteIncrement")
    # Some products expose via "trading_rules"
    tr = j.get("trading_rules") or j.get("tradingRules") or {}
    base_inc  = base_inc  or tr.get("base_increment")  or tr.get("baseIncrement")
    quote_inc = quote_inc or tr.get("quote_increment") or tr.get("quoteIncrement")
    try: base_inc = float(base_inc)
    except Exception: base_inc = None
    try: quote_inc = float(quote_inc)
    except Exception: quote_inc = None
    return {"base_increment": base_inc, "quote_increment": quote_inc}
# ──────────────────────────────────────────────────────────────────────────────
# Perp resolver (builds a map for keys like 'BTC-USD')
# ──────────────────────────────────────────────────────────────────────────────
_PERP_BY_KEY: Dict[str, list] = {}
_PERP_CACHE_TS: float = 0.0
_PERP_TTL_SEC = 60.0

def _is_perp(p: dict) -> bool:
    s  = (p.get("contract_type") or p.get("contractType") or "").lower()
    pt = (p.get("product_type")  or p.get("productType")  or "").upper()
    mk = (p.get("market_type")   or p.get("marketType")   or "").upper()
    if s == "perpetual": return True
    if pt in {"FUTURES_PERPETUAL", "PERPETUAL_FUTURE", "PERPETUAL"}: return True
    if mk in {"FUTURES_PERPETUAL", "PERPETUAL_FUTURE"}: return True
    if p.get("expiry") or p.get("expiry_time") or p.get("contractExpiry"): return False
    sym = (p.get("symbol") or p.get("product_id") or "").upper()
    return sym.endswith("-PERP") or "PERP" in sym

def _base_quote_for(p: dict) -> Tuple[str, str]:
    base = (p.get("base_currency") or p.get("baseCurrency") or p.get("base") or "").upper()
    quote = (p.get("quote_currency") or p.get("quoteCurrency") or p.get("quote") or "").upper()
    if not base or not quote:
        sym = (p.get("symbol") or p.get("product_id") or "").upper()
        if "-" in sym:
            parts = sym.split("-")
            if len(parts) >= 2:
                base = base or parts[0]
                quote = quote or parts[1]
    return base, quote

def _score_candidate(p: dict) -> tuple:
    status = str(p.get("status", "")).lower()
    online_rank = 0 if status in ("online", "active", "tradable") else 1
    _, q = _base_quote_for(p)
    quote_rank = 0 if q == "USD" else 1  # prefer USD over USDC, but USDC is fine
    try:
        tick = float(p.get("quote_increment") or p.get("tick_size") or 0.0)
    except Exception:
        tick = 1e9
    return (online_rank, quote_rank, tick)

def _refresh_perp_map() -> None:
    global _PERP_BY_KEY, _PERP_CACHE_TS
    resp = _send("GET", "/api/v3/brokerage/products", timeout=HTTP_TIMEOUT)
    if not resp or resp.status_code != 200:
        warn_once("perp_refresh", f"[WARN] products list failed: {getattr(resp,'status_code','NA')} — {getattr(resp,'text','')[:160]}")
        return
    try:
        items = resp.json().get("products", []) or []
    except Exception as e:
        warn_once("perp_json", f"[WARN] products JSON parse error: {e}")
        return

    by_key: Dict[str, list] = {}
    for p in items:
        if not _is_perp(p):
            continue
        b, q = _base_quote_for(p)
        if not b or not q:
            continue
        if q not in {"USD", "USDC"}:
            continue
        k = f"{b}-{q}"
        by_key.setdefault(k, []).append(p)
        alt = f"{b}-USD" if q == "USDC" else f"{b}-USDC"
        by_key.setdefault(alt, []).append(p)

    for k, arr in by_key.items():
        arr.sort(key=_score_candidate)

    _PERP_BY_KEY = by_key
    _PERP_CACHE_TS = time.time()

def resolve_perp_product_id(symbol: str) -> str:
    symbol = symbol.upper().strip()
    if (time.time() - _PERP_CACHE_TS) > _PERP_TTL_SEC or not _PERP_BY_KEY:
        _refresh_perp_map()
    cands = _PERP_BY_KEY.get(symbol) or []
    if not cands and symbol.endswith("-USD"):
        cands = _PERP_BY_KEY.get(symbol.replace("-USD", "-USDC")) or []
    if not cands:
        raise RuntimeError(f"No PERPETUAL found for {symbol}. Check venue access or listing.")
    best = cands[0]
    return best.get("product_id") or best.get("productId") or best.get("symbol") or best.get("id") or ""

# ──────────────────────────────────────────────────────────────────────────────
# Public helpers
# ──────────────────────────────────────────────────────────────────────────────
def get_futures_price(symbol: str) -> Optional[float]:
    try:
        product_id = resolve_perp_product_id(symbol)
    except Exception as e:
        print(f"[ERROR] resolve_perp_product_id({symbol}): {e}")
        return None
    resp = _send("GET", f"/api/v3/brokerage/products/{product_id}", timeout=HTTP_TIMEOUT)
    if not resp or resp.status_code != 200:
        print(f"[ERROR] get_futures_price: {getattr(resp,'status_code','NA')} — {getattr(resp,'text','')[:200]}")
        return None
    j = resp.json()
    for k in ("price", "current_price", "mark_price"):
        v = j.get(k)
        if v is not None:
            try: return float(v)
            except Exception: pass
    md = j.get("market_data") or j.get("marketData") or {}
    for k in ("price", "mark_price", "best_bid", "best_ask"):
        v = md.get(k)
        if v is not None:
            try: return float(v)
            except Exception: pass
    print(f"[WARN] Unexpected product payload (no price): {j}")
    return None

# === Spot helpers ===
def _spot_product_from_symbol(symbol: str) -> str:
    base, _ = symbol.split("-")
    return f"{base}-USDC"

def get_spot_price(symbol: str) -> Optional[float]:
    pid = _spot_product_from_symbol(symbol)
    for attempt in (1, 2):
        resp = _send("GET", f"/api/v3/brokerage/products/{pid}", timeout=max(HTTP_TIMEOUT, 10))
        if resp and resp.status_code == 200:
            j = resp.json()
            for k in ("price", "current_price", "mark_price"):
                v = j.get(k)
                if v is not None:
                    try: return float(v)
                    except Exception: pass
            md = j.get("market_data") or j.get("marketData") or {}
            for k in ("price", "mark_price", "best_bid", "best_ask"):
                v = md.get(k)
                if v is not None:
                    try: return float(v)
                    except Exception: pass
            print(f"[WARN] Unexpected SPOT payload (no price): {j}")
            return None
        # transient backoff
        time.sleep(0.15)
    print(f"[ERROR] get_spot_price: {getattr(resp,'status_code','NA')} — {getattr(resp,'text','')[:200]}")
    return None

def spot_market_buy(symbol: str, usd_size: float) -> dict:
    pid  = _spot_product_from_symbol(symbol)
    incs = _get_product_increments(pid)
    qinc = incs.get("quote_increment") or 0.01  # fallback safe
    qsize = _round_to_increment(float(usd_size), qinc)
    body = {
        "client_order_id": f"spot_buy_{int(time.time()*1000)}",
        "product_id": pid,
        "side": "BUY",
        "order_configuration": {"market_market_ioc": {"quote_size": f"{qsize:.8f}"}},
    }
    resp = _send("POST", "/api/v3/brokerage/orders", body=json.dumps(body, separators=(",", ":")), timeout=HTTP_TIMEOUT)
    print(f"[DEBUG] Spot Buy Response: {resp.status_code if resp else 'NA'} — {resp.text if resp else ''}")
    if not resp or resp.status_code not in (200, 201):
        raise Exception(f"[ERROR] Spot BUY failed: {getattr(resp,'status_code','NA')} — {getattr(resp,'text','')[:240]}")
    rj = resp.json()
    rj["_order_id"] = _get_order_id(rj)
    return rj

def spot_market_sell(symbol: str, base_size: float) -> dict:
    pid  = _spot_product_from_symbol(symbol)
    incs = _get_product_increments(pid)
    binc = incs.get("base_increment") or 1e-8  # conservative fallback
    bsize = _round_to_increment(float(base_size), binc)
    if bsize <= 0:
        raise Exception(f"[ERROR] Spot SELL size rounds to zero (base_increment={binc})")
    body = {
        "client_order_id": f"spot_sell_{int(time.time()*1000)}",
        "product_id": pid,
        "side": "SELL",
        "order_configuration": {"market_market_ioc": {"base_size": f"{bsize:.8f}"}},
    }
    resp = _send("POST", "/api/v3/brokerage/orders", body=json.dumps(body, separators=(",", ":")), timeout=HTTP_TIMEOUT)
    print(f"[DEBUG] Spot Sell Response: {resp.status_code if resp else 'NA'} — {resp.text if resp else ''}")
    if not resp or resp.status_code not in (200, 201):
        raise Exception(f"[ERROR] Spot SELL failed: {getattr(resp,'status_code','NA')} — {getattr(resp,'text','')[:240]}")
    rj = resp.json()
    rj["_order_id"] = _get_order_id(rj)
    return rj

def get_usd_balance() -> float:
    resp = _send("GET", "/api/v3/brokerage/accounts", timeout=HTTP_TIMEOUT)
    if not resp or resp.status_code != 200:
        raise Exception(f"[ERROR] Balance fetch failed: {getattr(resp,'status_code','NA')} — {getattr(resp,'text','')[:240]}")
    for acct in resp.json().get("accounts", []):
        if str(acct.get("currency")).upper() == "USD":
            try:
                return float(acct["available_balance"]["value"])
            except Exception:
                pass
    return 0.0

def futures_market_buy(symbol: str, usd_size: float) -> dict:
    pid  = resolve_perp_product_id(symbol)
    body = {
        "client_order_id": f"buy_{int(time.time()*1000)}",
        "product_id": pid,
        "side": "BUY",
        "order_configuration": {"market_market_ioc": {"quote_size": f"{usd_size:.2f}"}},
    }
    extra = {"x-cb-portfolio": PORTFOLIO_ID} if PORTFOLIO_ID else None
    resp = _send("POST", "/api/v3/brokerage/orders", body=json.dumps(body, separators=(",", ":")), timeout=HTTP_TIMEOUT, extra_headers=extra)
    print(f"[DEBUG] Futures Buy Response: {resp.status_code if resp else 'NA'} — {resp.text if resp else ''}")
    if not resp or resp.status_code not in (200, 201):
        raise Exception(f"[ERROR] Futures BUY failed: {getattr(resp,'status_code','NA')} — {getattr(resp,'text','')[:240]}")
    return resp.json()

def futures_market_sell(symbol: str, usd_size: float) -> dict:
    pid  = resolve_perp_product_id(symbol)
    body = {
        "client_order_id": f"sell_{int(time.time()*1000)}",
        "product_id": pid,
        "side": "SELL",
        "order_configuration": {"market_market_ioc": {"quote_size": f"{usd_size:.2f}"}},
    }
    extra = {"x-cb-portfolio": PORTFOLIO_ID} if PORTFOLIO_ID else None
    resp = _send("POST", "/api/v3/brokerage/orders", body=json.dumps(body, separators=(",", ":")), timeout=HTTP_TIMEOUT, extra_headers=extra)
    print(f"[DEBUG] Futures Sell Response: {resp.status_code if resp else 'NA'} — {resp.text if resp else ''}")
    if not resp or resp.status_code not in (200, 201):
        raise Exception(f"[ERROR] Futures SELL failed: {getattr(resp,'status_code','NA')} — {getattr(resp,'text','')[:240]}")
    return resp.json()

# ──────────────────────────────────────────────────────────────────────────────
# Orders/Fills utilities
# ──────────────────────────────────────────────────────────────────────────────
def _get_order_id(resp_json: dict) -> str:
    return (
        resp_json.get("order_id")
        or resp_json.get("orderId")
        or resp_json.get("success_response", {}).get("order_id")
        or resp_json.get("order", {}).get("order_id")
        or ""
    )

def get_order_fills(order_id: str) -> list:
    if not order_id:
        return []
    resp = _send("GET", f"/api/v3/brokerage/orders/historical/{order_id}", timeout=max(HTTP_TIMEOUT, 10))
    if not resp or resp.status_code != 200:
        print(f"[ERROR] get_order_fills: {getattr(resp,'status_code','NA')} — {getattr(resp,'text','')[:240]}")
        return []
    j = resp.json() or {}
    raw = j.get("fills") or j.get("order", {}).get("fills") or j.get("data", {}).get("fills") or []
    out = []
    for f in raw:
        price = f.get("price") or f.get("executed_price") or 0
        size  = f.get("size")  or f.get("executed_size")  or 0
        fee   = f.get("fee")   or f.get("commission")     or f.get("fees") or 0
        side  = (f.get("side") or "").upper()
        try:
            price = float(price); size = float(size); fee = float(fee)
        except Exception:
            continue
        if price > 0 and size > 0:
            out.append({"price": price, "size": size, "fee": fee, "side": side})
    return out

# ──────────────────────────────────────────────────────────────────────────────
# Diagnostics
# ──────────────────────────────────────────────────────────────────────────────
def auth_smoke_test() -> bool:
    """
    Minimal authenticated GET to verify JWT signing works.
    Uses /accounts (no futures permission needed).
    """
    try:
        resp = _send("GET", "/api/v3/brokerage/accounts", timeout=HTTP_TIMEOUT)
        snippet = (resp.text[:200].replace("\n", " ") if resp and hasattr(resp, "text") else "")
        if resp is None:
            print("[AUTH] status=NA")
            return False
        if resp.status_code == 401:
            print("[AUTH] 401 WWW-Authenticate:", resp.headers.get("WWW-Authenticate", "<none>"))
        print(f"[AUTH] status={resp.status_code} body[:200]={snippet}")
        return resp.status_code == 200
    except Exception as e:
        print(f"[AUTH] exception: {e}")
        return False