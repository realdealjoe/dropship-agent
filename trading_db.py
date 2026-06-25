"""
Trading database — separate from the dropshipping DB.
Tables:
  price_history   — 6 years of daily OHLCV for every watchlist symbol
  trade_journal   — every trade ever placed, open and closed
  strategy_notes  — weekly self-review outputs that get fed back into the agent
  market_regimes  — daily regime classification (bull/bear/choppy)
"""
import sqlite3
import os

# DATA_DIR is /data on Railway (persistent volume), local dir otherwise
_DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_DATA_DIR, "trading.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_trading_db():
    conn = get_conn()
    c = conn.cursor()

    c.executescript("""
    CREATE TABLE IF NOT EXISTS price_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol      TEXT NOT NULL,
        date        TEXT NOT NULL,
        open        REAL,
        high        REAL,
        low         REAL,
        close       REAL,
        volume      INTEGER,
        vwap        REAL,
        -- computed fields stored for fast retrieval
        ma20        REAL,
        ma50        REAL,
        ma200       REAL,
        atr14       REAL,
        rsi14       REAL,
        vol_ratio   REAL,   -- volume / 20d avg volume
        UNIQUE(symbol, date)
    );

    CREATE TABLE IF NOT EXISTS trade_journal (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id        TEXT UNIQUE,
        symbol          TEXT NOT NULL,
        side            TEXT NOT NULL,       -- buy / sell
        qty             REAL,
        entry_price     REAL,
        exit_price      REAL,
        entry_time      TEXT,
        exit_time       TEXT,
        holding_hours   REAL,
        pnl             REAL,
        pnl_pct         REAL,
        won             INTEGER,             -- 1 = win, 0 = loss, NULL = open
        signal_type     TEXT,               -- momentum / mean_reversion / regime
        market_regime   TEXT,               -- bull_trend / bear_trend / choppy
        setup_notes     TEXT,               -- Claude's full reasoning
        exit_reason     TEXT,               -- stop_loss / take_profit / manual / eod
        day_of_week     TEXT,
        month           INTEGER,
        spy_trend       TEXT,               -- above/below 50ma at entry
        created_at      TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS strategy_notes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        week_ending TEXT NOT NULL,
        content     TEXT NOT NULL,          -- full weekly review text from Claude
        win_rate    REAL,
        total_trades INTEGER,
        net_pnl     REAL,
        created_at  TEXT DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS market_regimes (
        date        TEXT PRIMARY KEY,
        regime      TEXT NOT NULL,          -- bull_trend / bear_trend / choppy
        spy_close   REAL,
        spy_ma50    REAL,
        spy_ma200   REAL,
        vix_proxy   REAL,                   -- SPY ATR14 as VIX proxy
        notes       TEXT
    );
    """)

    conn.commit()
    conn.close()
    print("[trading_db] Schema ready")


# ── Trade Journal ─────────────────────────────────────────────────────────────

def open_trade(order_id: str, symbol: str, side: str, qty: float,
               entry_price: float, signal_type: str, setup_notes: str,
               market_regime: str = "", spy_trend: str = "") -> None:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    conn = get_conn()
    conn.execute("""
        INSERT OR IGNORE INTO trade_journal
        (order_id, symbol, side, qty, entry_price, entry_time,
         signal_type, market_regime, setup_notes, spy_trend,
         day_of_week, month)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (order_id, symbol, side, qty, entry_price,
          now.isoformat(), signal_type, market_regime, setup_notes,
          spy_trend, now.strftime("%A"), now.month))
    conn.commit()
    conn.close()


def close_trade(order_id: str, exit_price: float, exit_reason: str) -> dict:
    from datetime import datetime, timezone
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM trade_journal WHERE order_id=?", (order_id,)
    ).fetchone()
    if not row:
        conn.close()
        return {"error": f"Trade {order_id} not found"}

    pnl = (exit_price - row["entry_price"]) * row["qty"]
    if row["side"] == "sell":
        pnl = -pnl
    pnl_pct = ((exit_price - row["entry_price"]) / row["entry_price"]) * 100
    won = 1 if pnl > 0 else 0
    now = datetime.now(timezone.utc)
    entry_dt = datetime.fromisoformat(row["entry_time"].replace("Z", "+00:00"))
    holding_hours = (now - entry_dt).total_seconds() / 3600

    conn.execute("""
        UPDATE trade_journal SET
            exit_price=?, exit_time=?, pnl=?, pnl_pct=?,
            won=?, exit_reason=?, holding_hours=?
        WHERE order_id=?
    """, (exit_price, now.isoformat(), round(pnl, 2), round(pnl_pct, 3),
          won, exit_reason, round(holding_hours, 1), order_id))
    conn.commit()
    conn.close()
    return {"pnl": round(pnl, 2), "pnl_pct": round(pnl_pct, 3), "won": won}


def get_trade_history(symbol: str = None, limit: int = 100,
                      closed_only: bool = True) -> list:
    conn = get_conn()
    if symbol:
        rows = conn.execute(
            "SELECT * FROM trade_journal WHERE symbol=? "
            + ("AND won IS NOT NULL " if closed_only else "")
            + "ORDER BY created_at DESC LIMIT ?",
            (symbol, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trade_journal "
            + ("WHERE won IS NOT NULL " if closed_only else "")
            + "ORDER BY created_at DESC LIMIT ?",
            (limit,)
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_performance_stats() -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trade_journal WHERE won IS NOT NULL"
    ).fetchall()
    conn.close()
    if not rows:
        return {"message": "No closed trades yet"}

    trades = [dict(r) for r in rows]
    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    total_pnl = sum(t["pnl"] for t in trades)
    gross_wins = sum(t["pnl"] for t in wins)
    gross_losses = abs(sum(t["pnl"] for t in losses))

    # By symbol
    by_symbol: dict = {}
    for t in trades:
        s = t["symbol"]
        if s not in by_symbol:
            by_symbol[s] = {"wins": 0, "losses": 0, "pnl": 0.0}
        by_symbol[s]["wins" if t["won"] else "losses"] += 1
        by_symbol[s]["pnl"] += t["pnl"]

    # By signal type
    by_signal: dict = {}
    for t in trades:
        sig = t["signal_type"] or "unknown"
        if sig not in by_signal:
            by_signal[sig] = {"wins": 0, "losses": 0, "pnl": 0.0}
        by_signal[sig]["wins" if t["won"] else "losses"] += 1
        by_signal[sig]["pnl"] += t["pnl"]

    # By day of week
    by_day: dict = {}
    for t in trades:
        day = t["day_of_week"] or "Unknown"
        if day not in by_day:
            by_day[day] = {"wins": 0, "losses": 0}
        by_day[day]["wins" if t["won"] else "losses"] += 1

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "total_pnl": round(total_pnl, 2),
        "profit_factor": round(gross_wins / gross_losses, 2) if gross_losses else None,
        "avg_win": round(gross_wins / len(wins), 2) if wins else 0,
        "avg_loss": round(gross_losses / len(losses), 2) if losses else 0,
        "by_symbol": by_symbol,
        "by_signal": by_signal,
        "by_day_of_week": by_day,
    }


# ── Price History ─────────────────────────────────────────────────────────────

def store_price_history(symbol: str, bars: list) -> int:
    conn = get_conn()
    inserted = 0
    for b in bars:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO price_history
                (symbol, date, open, high, low, close, volume, vwap,
                 ma20, ma50, ma200, atr14, rsi14, vol_ratio)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (symbol, b.get("date"), b.get("open"), b.get("high"),
                  b.get("low"), b.get("close"), b.get("volume"), b.get("vwap"),
                  b.get("ma20"), b.get("ma50"), b.get("ma200"),
                  b.get("atr14"), b.get("rsi14"), b.get("vol_ratio")))
            inserted += 1
        except Exception:
            pass
    conn.commit()
    conn.close()
    return inserted


def get_historical_stats(symbol: str) -> dict:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM price_history WHERE symbol=? ORDER BY date DESC LIMIT 252",
        (symbol,)
    ).fetchall()
    conn.close()
    if not rows:
        return {"error": f"No history for {symbol}"}

    data = [dict(r) for r in rows]
    closes = [r["close"] for r in data if r["close"]]
    highs = [r["high"] for r in data if r["high"]]
    lows = [r["low"] for r in data if r["low"]]
    volumes = [r["volume"] for r in data if r["volume"]]

    latest = data[0]
    current = latest["close"] or 0

    # 52-week range
    high_52w = max(highs) if highs else 0
    low_52w = min(lows) if lows else 0
    pct_from_high = ((current - high_52w) / high_52w * 100) if high_52w else 0
    pct_from_low = ((current - low_52w) / low_52w * 100) if low_52w else 0

    # Support/resistance: most-tested price clusters
    avg_close_1y = sum(closes) / len(closes) if closes else 0
    avg_vol = sum(volumes) / len(volumes) if volumes else 0

    # Monthly return pattern (last 3 years of same month)
    from datetime import datetime
    current_month = datetime.now().month
    same_month_returns = []
    for i in range(len(data) - 1):
        d = data[i]["date"] or ""
        if d[5:7] == f"{current_month:02d}":
            if data[i]["close"] and data[i + 1]["close"]:
                ret = (data[i]["close"] - data[i + 1]["close"]) / data[i + 1]["close"] * 100
                same_month_returns.append(ret)
    avg_month_return = (sum(same_month_returns) / len(same_month_returns)
                        if same_month_returns else 0)

    return {
        "symbol": symbol,
        "current_price": current,
        "ma20": latest.get("ma20"),
        "ma50": latest.get("ma50"),
        "ma200": latest.get("ma200"),
        "rsi14": latest.get("rsi14"),
        "atr14": latest.get("atr14"),
        "52w_high": round(high_52w, 2),
        "52w_low": round(low_52w, 2),
        "pct_from_52w_high": round(pct_from_high, 1),
        "pct_from_52w_low": round(pct_from_low, 1),
        "avg_close_1y": round(avg_close_1y, 2),
        "avg_daily_volume": round(avg_vol),
        "avg_return_this_month_historically": round(avg_month_return, 2),
        "vol_ratio_today": latest.get("vol_ratio"),
        "price_vs_ma200": round((current - (latest.get("ma200") or current))
                                 / (latest.get("ma200") or current) * 100, 1) if latest.get("ma200") else None,
    }


# ── Strategy Notes ────────────────────────────────────────────────────────────

def save_strategy_notes(week_ending: str, content: str,
                        win_rate: float, total_trades: int, net_pnl: float):
    conn = get_conn()
    conn.execute("""
        INSERT INTO strategy_notes (week_ending, content, win_rate, total_trades, net_pnl)
        VALUES (?,?,?,?,?)
    """, (week_ending, content, win_rate, total_trades, net_pnl))
    conn.commit()
    conn.close()


def get_latest_strategy_notes(limit: int = 4) -> list:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM strategy_notes ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    init_trading_db()
