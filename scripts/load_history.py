"""
Pull 6 years of daily stock data from Alpaca and store in trading.db.
Run this once (or re-run to top-up with latest bars).

Usage:
    python scripts/load_history.py

Requires ALPACA_API_KEY + ALPACA_SECRET_KEY in .env
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from dotenv import load_dotenv
load_dotenv()

import trading_db as tdb

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() != "false"
DATA_BASE = "https://data.alpaca.markets"

WATCHLIST = ["TSLA", "NVDA", "AAPL", "SPY", "QQQ", "RIVN", "NIO", "LCID"]

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}


def fetch_bars(symbol: str, start: str, end: str) -> list:
    """Fetch all daily bars for a symbol between start and end dates."""
    all_bars = []
    page_token = None
    params = {
        "timeframe": "1Day",
        "start": start,
        "end": end,
        "limit": 1000,
        "adjustment": "split",
        "feed": "iex",  # Use IEX feed (free tier compatible)
    }

    while True:
        if page_token:
            params["page_token"] = page_token

        try:
            r = httpx.get(
                f"{DATA_BASE}/v2/stocks/{symbol}/bars",
                headers=HEADERS,
                params=params,
                timeout=30,
            )
            if r.status_code != 200:
                print(f"  Error fetching {symbol}: {r.status_code} {r.text[:200]}")
                break

            data = r.json()
            bars = data.get("bars", [])
            all_bars.extend(bars)

            page_token = data.get("next_page_token")
            if not page_token:
                break

            time.sleep(0.3)  # Be polite to the API

        except Exception as e:
            print(f"  Exception fetching {symbol}: {e}")
            break

    return all_bars


def compute_sma(values: list, period: int) -> list:
    """Compute simple moving average. Returns list same length as input (None for early values)."""
    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
        else:
            window = values[i - period + 1:i + 1]
            result.append(round(sum(window) / period, 4))
    return result


def compute_atr(highs: list, lows: list, closes: list, period: int = 14) -> list:
    """Average True Range over period days."""
    trs = []
    result = []
    for i in range(len(closes)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        trs.append(tr)
        if i < period - 1:
            result.append(None)
        elif i == period - 1:
            result.append(round(sum(trs) / period, 4))
        else:
            prev_atr = result[-1]
            result.append(round((prev_atr * (period - 1) + tr) / period, 4))
    return result


def compute_rsi(closes: list, period: int = 14) -> list:
    """RSI using Wilder's smoothing."""
    if len(closes) < period + 1:
        return [None] * len(closes)

    gains, losses = [], []
    result = [None] * period

    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(round(100 - (100 / (1 + rs)), 2))

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(round(100 - (100 / (1 + rs)), 2))

    return result


def compute_vol_ratio(volumes: list, period: int = 20) -> list:
    """Volume relative to N-day average."""
    result = []
    for i in range(len(volumes)):
        if i < period - 1:
            result.append(None)
        else:
            avg = sum(volumes[i - period + 1:i + 1]) / period
            result.append(round(volumes[i] / avg, 2) if avg else None)
    return result


def process_symbol(symbol: str, bars: list) -> list:
    """Attach computed indicators to each bar."""
    if not bars:
        return []

    opens = [b["o"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    closes = [b["c"] for b in bars]
    volumes = [int(b.get("v", 0)) for b in bars]
    vwaps = [b.get("vw", b["c"]) for b in bars]
    dates = [b["t"][:10] for b in bars]  # YYYY-MM-DD

    ma20 = compute_sma(closes, 20)
    ma50 = compute_sma(closes, 50)
    ma200 = compute_sma(closes, 200)
    atr14 = compute_atr(highs, lows, closes, 14)
    rsi14 = compute_rsi(closes, 14)
    vol_ratio = compute_vol_ratio(volumes, 20)

    result = []
    for i, b in enumerate(bars):
        result.append({
            "date": dates[i],
            "open": opens[i],
            "high": highs[i],
            "low": lows[i],
            "close": closes[i],
            "volume": volumes[i],
            "vwap": vwaps[i],
            "ma20": ma20[i],
            "ma50": ma50[i],
            "ma200": ma200[i],
            "atr14": atr14[i],
            "rsi14": rsi14[i],
            "vol_ratio": vol_ratio[i],
        })
    return result


def load_all():
    tdb.init_trading_db()

    start_date = "2019-01-01"
    from datetime import date
    end_date = date.today().isoformat()

    total_inserted = 0
    for symbol in WATCHLIST:
        print(f"\n[load_history] Fetching {symbol} from {start_date} to {end_date}...")
        raw_bars = fetch_bars(symbol, start_date, end_date)
        print(f"  Got {len(raw_bars)} raw bars")

        if not raw_bars:
            print(f"  WARNING: no data for {symbol}")
            continue

        processed = process_symbol(symbol, raw_bars)
        inserted = tdb.store_price_history(symbol, processed)
        total_inserted += inserted
        print(f"  Stored {inserted} bars (skipped duplicates)")
        time.sleep(0.5)

    print(f"\n[load_history] Done. Total rows inserted: {total_inserted}")
    print(f"[load_history] Database: {tdb.DB_PATH}")

    # Quick sanity check
    for symbol in WATCHLIST:
        stats = tdb.get_historical_stats(symbol)
        if "error" not in stats:
            print(f"  {symbol}: price={stats['current_price']}, "
                  f"ma200={stats['ma200']}, rsi={stats['rsi14']}, "
                  f"52w_range=[{stats['52w_low']}-{stats['52w_high']}]")


if __name__ == "__main__":
    if not ALPACA_API_KEY:
        print("ERROR: ALPACA_API_KEY not found in .env")
        sys.exit(1)
    load_all()
