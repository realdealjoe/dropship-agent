"""
Pull 6 years of daily OHLCV for crypto from Coinbase Advanced Trade API.
Computes MA20/50/200, ATR14, RSI14 and stores into trading.db price_history table.

Run: python3 scripts/load_crypto_history.py
"""
import sys, os, time, base64, secrets, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import trading_db as tdb

KEY_NAME    = os.getenv("COINBASE_API_KEY_NAME", "")
_KEY_B64    = os.getenv("COINBASE_API_PRIVATE_KEY", "")
BASE_URL    = "https://api.coinbase.com"

SYMBOLS     = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD"]
YEARS       = 6
CHUNK_DAYS  = 300   # safe under Coinbase's 350-candle limit


def _make_jwt(method: str, path: str) -> str:
    raw = base64.b64decode(_KEY_B64)
    pk  = Ed25519PrivateKey.from_private_bytes(raw[:32])
    payload = {
        "sub": KEY_NAME, "iss": "cdp",
        "nbf": int(time.time()), "exp": int(time.time()) + 120,
        "uri": f"{method} api.coinbase.com{path}",
    }
    return jwt.encode(payload, pk, algorithm="EdDSA",
                      headers={"kid": KEY_NAME, "nonce": secrets.token_hex(10)})


def _fetch_candles(product_id: str, start_ts: int, end_ts: int) -> list:
    path  = f"/api/v3/brokerage/products/{product_id}/candles"
    token = _make_jwt("GET", path)
    r     = httpx.get(f"{BASE_URL}{path}",
                      params={"start": str(start_ts), "end": str(end_ts),
                              "granularity": "ONE_DAY"},
                      headers={"Authorization": f"Bearer {token}"},
                      timeout=30)
    if r.status_code != 200:
        print(f"    [{r.status_code}] {r.text[:100]}")
        return []
    return r.json().get("candles", [])


def _compute_indicators(candles: list) -> list:
    closes  = [float(c["close"]) for c in candles]
    highs   = [float(c["high"])  for c in candles]
    lows    = [float(c["low"])   for c in candles]
    volumes = [int(float(c.get("volume", 0))) for c in candles]

    bars = []
    for i, c in enumerate(candles):
        close = closes[i]

        def ma(n):
            window = closes[max(0, i - n + 1): i + 1]
            return round(sum(window) / len(window), 6)

        # ATR14
        atr14 = None
        if i >= 1:
            trs = [max(highs[j] - lows[j],
                       abs(highs[j] - closes[j-1]),
                       abs(lows[j]  - closes[j-1]))
                   for j in range(max(1, i - 13), i + 1)]
            atr14 = round(sum(trs) / len(trs), 6)

        # RSI14
        rsi14 = None
        if i >= 14:
            gains  = [max(0.0, closes[j] - closes[j-1]) for j in range(i-13, i+1)]
            losses = [max(0.0, closes[j-1] - closes[j]) for j in range(i-13, i+1)]
            ag, al = sum(gains) / 14, sum(losses) / 14
            rsi14  = round(100 - (100 / (1 + ag / al)), 2) if al > 0 else 100.0

        # Volume ratio
        vol_window = volumes[max(0, i - 19): i + 1]
        avg_vol    = sum(vol_window) / len(vol_window) if vol_window else 1
        vol_ratio  = round(volumes[i] / avg_vol, 3) if avg_vol > 0 else 1.0

        date_str = datetime.fromtimestamp(int(c["start"]), tz=timezone.utc).strftime("%Y-%m-%d")

        bars.append({
            "date":      date_str,
            "open":      float(c["open"]),
            "high":      float(c["high"]),
            "low":       float(c["low"]),
            "close":     close,
            "volume":    volumes[i],
            "vwap":      float(c.get("vwap", close)),
            "ma20":      ma(20),
            "ma50":      ma(50),
            "ma200":     ma(200),
            "atr14":     atr14,
            "rsi14":     rsi14,
            "vol_ratio": vol_ratio,
        })
    return bars


def load_symbol(symbol: str):
    print(f"  {symbol}:", end=" ", flush=True)
    end_ts   = int(time.time())
    start_ts = end_ts - YEARS * 365 * 86400

    all_candles = []
    chunk_end   = end_ts

    while chunk_end > start_ts:
        chunk_start = max(start_ts, chunk_end - CHUNK_DAYS * 86400)
        candles     = _fetch_candles(symbol, chunk_start, chunk_end)
        if candles:
            all_candles.extend(candles)
        chunk_end = chunk_start - 86400
        time.sleep(0.4)   # rate limit

    # Deduplicate by timestamp
    seen, unique = set(), []
    for c in all_candles:
        if c["start"] not in seen:
            seen.add(c["start"])
            unique.append(c)
    unique.sort(key=lambda x: x["start"])

    if not unique:
        print("no data")
        return

    bars     = _compute_indicators(unique)
    inserted = tdb.store_price_history(symbol, bars)
    print(f"{len(unique)} candles → {inserted} inserted")


def main():
    if not KEY_NAME or not _KEY_B64:
        print("[load_crypto] COINBASE_API_KEY_NAME or COINBASE_API_PRIVATE_KEY not set")
        return
    tdb.init_trading_db()
    print(f"[load_crypto] Loading up to {YEARS} years of daily data...")
    for sym in SYMBOLS:
        load_symbol(sym)
    print("[load_crypto] Done!")


if __name__ == "__main__":
    main()
