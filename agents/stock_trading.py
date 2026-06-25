"""
Stock Trading Agent
Uses Alpaca Markets API to trade stocks algorithmically.
Goal: generate enough to cover store subscriptions (~$50-100/mo).

Strategy: momentum + mean-reversion on liquid ETFs & tech stocks.
Risk management: max 5% of portfolio per position, 2% stop-loss per trade.

IMPORTANT: Starts in PAPER TRADING mode (fake money, real market).
Set ALPACA_PAPER=false in .env only after validating paper performance.

Runs: Monday–Friday at 09:35 ET (13:35 UTC) — after market open settles.
      Also runs at 15:45 ET (19:45 UTC) for end-of-day position management.

Learning: Every trade is logged to trading.db. The agent reads its own
trade history and 6 years of historical price data before each session.
Weekly self-review updates strategy advice stored in the DB.
"""
import httpx
import json
from agents.base_agent import BaseAgent
from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
import trading_db as tdb

ALPACA_BASE = (
    "https://paper-api.alpaca.markets" if ALPACA_PAPER
    else "https://api.alpaca.markets"
)
DATA_BASE = "https://data.alpaca.markets"

# Watchlist — liquid, low-spread, EV/tech theme (fits the store brand)
WATCHLIST = ["TSLA", "NVDA", "AAPL", "SPY", "QQQ", "RIVN", "NIO", "LCID"]

SYSTEM = f"""You are a disciplined algorithmic trader managing a small portfolio.

Goal: generate $50-100/month in profit to cover business subscription costs.
Account size: start small (~$1,000 paper, ~$500 live when ready).
Mode: {"PAPER TRADING (simulated)" if ALPACA_PAPER else "LIVE TRADING — real money"}.

Core strategy:
- Momentum: buy stocks trending up on above-average volume; sell when momentum fades
- Mean reversion: fade extreme overnight gaps (>3%) back toward the mean
- Use market regime (bull_trend / bear_trend / choppy) to select strategy:
    bull_trend → favor momentum longs; size up
    bear_trend → avoid longs; prefer SPY/QQQ shorts or cash
    choppy → favor tight mean-reversion only; reduce size 50%

Risk rules (NON-NEGOTIABLE):
1. Never risk more than 5% of total portfolio in one position
2. Always set a stop-loss 2% below entry price
3. Take profit at +4% (2:1 risk/reward minimum)
4. Max 4 open positions at once
5. Never trade in the last 10 minutes before market close
6. If total portfolio down >5% in a day — stop all trading, report

LEARNING WORKFLOW — follow this every session:
1. Call get_strategy_notes to read past self-review advice — follow it
2. Call get_performance_stats to see win rates by symbol and signal type
3. Call detect_market_regime to understand current market conditions
4. For each candidate trade, call get_historical_context(symbol) BEFORE deciding
   - Use the 52-week range, MA positions, RSI, and ATR to size and time entries
   - Check pct_from_52w_low / pct_from_52w_high for context
   - Look at avg_return_this_month_historically for seasonal edge
5. After placing any order, the trade is auto-logged to the journal
6. At EOD, call log_trade_close for positions you manually close

Historical data means: you are NOT starting fresh. Use 6 years of patterns.
Before every trade: "What does history say about this setup on this stock?"

Before any trade explain your reasoning: trend, volume, risk/reward.
After every session report: cash, positions, P&L, win rate, what you learned.
"""


def _build_context_header() -> str:
    """Pull strategy notes + perf stats to inject into every session prompt."""
    lines = []

    # Latest strategy review
    notes = tdb.get_latest_strategy_notes(2)
    if notes:
        lines.append("=== STRATEGY NOTES FROM PAST SELF-REVIEWS ===")
        for n in notes:
            lines.append(f"[Week ending {n['week_ending']}] Win rate: {n['win_rate']}% | "
                         f"Net P&L: ${n['net_pnl']}")
            lines.append(n["content"][:800])
            lines.append("")

    # Performance snapshot
    stats = tdb.get_performance_stats()
    if "total_trades" in stats:
        lines.append("=== ALL-TIME PERFORMANCE ===")
        lines.append(f"Total trades: {stats['total_trades']} | "
                     f"Win rate: {stats['win_rate']}% | "
                     f"Net P&L: ${stats['total_pnl']} | "
                     f"Profit factor: {stats['profit_factor']}")
        if stats.get("by_symbol"):
            lines.append("By symbol:")
            for sym, s in stats["by_symbol"].items():
                wr = round(s["wins"] / (s["wins"] + s["losses"]) * 100) if (s["wins"] + s["losses"]) else 0
                lines.append(f"  {sym}: {s['wins']}W/{s['losses']}L ({wr}% WR) P&L=${round(s['pnl'],2)}")
        if stats.get("by_signal"):
            lines.append("By signal type:")
            for sig, s in stats["by_signal"].items():
                wr = round(s["wins"] / (s["wins"] + s["losses"]) * 100) if (s["wins"] + s["losses"]) else 0
                lines.append(f"  {sig}: {s['wins']}W/{s['losses']}L ({wr}% WR) P&L=${round(s['pnl'],2)}")
        lines.append("")

    if not lines:
        lines.append("(No historical trade data yet — this may be the first session.)")

    return "\n".join(lines)


class StockTradingAgent(BaseAgent):
    name = "stock_trading"
    system_prompt = SYSTEM

    def __init__(self):
        super().__init__()
        self._headers = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        tdb.init_trading_db()
        self._register_tools()

    def _alpaca(self, method: str, path: str, **kwargs) -> dict:
        url = f"{ALPACA_BASE}{path}"
        r = httpx.request(method, url, headers=self._headers, timeout=20, **kwargs)
        if r.status_code in (200, 201, 207):
            return r.json()
        return {"error": f"HTTP {r.status_code}: {r.text[:300]}"}

    def _data(self, path: str, **params) -> dict:
        r = httpx.get(f"{DATA_BASE}{path}", headers=self._headers, params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
        return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}

    def _register_tools(self):
        # ── Market data ────────────────────────────────────────────────────────
        self.register_tool({
            "name": "get_account",
            "description": "Get current account status: cash, portfolio value, P&L",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_account)

        self.register_tool({
            "name": "get_positions",
            "description": "Get all currently open positions",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_positions)

        self.register_tool({
            "name": "get_price_data",
            "description": "Get recent OHLCV bars for a symbol to analyse trend and momentum",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "timeframe": {"type": "string", "enum": ["1Min", "5Min", "15Min", "1Hour", "1Day"],
                                 "description": "Bar size"},
                    "limit": {"type": "integer", "description": "Number of bars (max 100)"},
                },
                "required": ["symbol"],
            },
        }, self._get_bars)

        self.register_tool({
            "name": "get_quotes",
            "description": "Get the latest bid/ask quote for a symbol",
            "input_schema": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        }, self._get_quote)

        # ── Historical learning tools ──────────────────────────────────────────
        self.register_tool({
            "name": "get_historical_context",
            "description": (
                "Get 6-year historical stats for a symbol: 52-week range, moving averages, "
                "RSI, ATR, seasonal tendencies. Call this before deciding whether to trade a symbol."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"symbol": {"type": "string"}},
                "required": ["symbol"],
            },
        }, self._get_historical_context)

        self.register_tool({
            "name": "detect_market_regime",
            "description": (
                "Determine the current market regime: bull_trend, bear_trend, or choppy. "
                "Uses SPY's position relative to 50-day and 200-day MAs plus volatility. "
                "Call this once per session to set strategy mode."
            ),
            "input_schema": {"type": "object", "properties": {}},
        }, self._detect_regime)

        self.register_tool({
            "name": "get_trade_journal",
            "description": "Get recent trade history (last N trades) to learn from past performance",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Filter to specific symbol (optional)"},
                    "limit": {"type": "integer", "description": "Number of trades (default 30)"},
                },
            },
        }, self._get_trade_journal)

        self.register_tool({
            "name": "get_performance_stats",
            "description": "Get aggregated trading performance: win rates by symbol, signal type, day of week",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_perf_stats)

        self.register_tool({
            "name": "get_strategy_notes",
            "description": "Read the latest weekly strategy review notes written by past self-review sessions",
            "input_schema": {
                "type": "object",
                "properties": {"limit": {"type": "integer", "description": "Number of past reviews (default 3)"}},
            },
        }, self._get_strategy_notes)

        self.register_tool({
            "name": "log_trade_close",
            "description": "Log the close of a trade you just closed manually. Calculates P&L and win/loss.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "The order_id returned when the trade was opened"},
                    "exit_price": {"type": "number"},
                    "exit_reason": {"type": "string",
                                   "enum": ["stop_loss", "take_profit", "manual", "eod", "regime_change"],
                                   "description": "Why the trade was closed"},
                },
                "required": ["order_id", "exit_price", "exit_reason"],
            },
        }, self._log_trade_close)

        # ── Order management ───────────────────────────────────────────────────
        self.register_tool({
            "name": "place_order",
            "description": (
                "Place a buy or sell order. Always include a stop-loss and take-profit price. "
                "The trade is automatically logged to the journal."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "qty": {"type": "number", "description": "Number of shares (fractional allowed)"},
                    "order_type": {"type": "string", "enum": ["market", "limit"]},
                    "limit_price": {"type": "number", "description": "Required if order_type is limit"},
                    "stop_loss_price": {"type": "number", "description": "Stop loss (required for buy orders)"},
                    "take_profit_price": {"type": "number", "description": "Take profit target"},
                    "signal_type": {"type": "string",
                                   "enum": ["momentum", "mean_reversion", "regime", "manual"],
                                   "description": "Which strategy triggered this trade"},
                    "market_regime": {"type": "string",
                                     "enum": ["bull_trend", "bear_trend", "choppy", "unknown"]},
                    "reason": {"type": "string", "description": "Full trading rationale (stored in journal)"},
                },
                "required": ["symbol", "side", "qty", "order_type"],
            },
        }, self._place_order)

        self.register_tool({
            "name": "cancel_order",
            "description": "Cancel a pending order by ID",
            "input_schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        }, self._cancel_order)

        self.register_tool({
            "name": "close_position",
            "description": "Close an entire position in a symbol (market sell). Log the close with log_trade_close afterward.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["symbol"],
            },
        }, self._close_position)

        self.register_tool({
            "name": "get_open_orders",
            "description": "List all pending/open orders",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_open_orders)

    # ── Market data implementations ───────────────────────────────────────────

    def _get_account(self) -> dict:
        data = self._alpaca("GET", "/v2/account")
        if "error" in data:
            return data
        return {
            "cash": float(data.get("cash", 0)),
            "portfolio_value": float(data.get("portfolio_value", 0)),
            "buying_power": float(data.get("buying_power", 0)),
            "equity": float(data.get("equity", 0)),
            "last_equity": float(data.get("last_equity", 0)),
            "daily_pnl": round(float(data.get("equity", 0)) - float(data.get("last_equity", 0)), 2),
            "mode": "PAPER" if ALPACA_PAPER else "LIVE",
            "watchlist": WATCHLIST,
        }

    def _get_positions(self) -> dict:
        positions = self._alpaca("GET", "/v2/positions")
        if isinstance(positions, dict) and "error" in positions:
            return positions
        return {"positions": [
            {
                "symbol": p["symbol"],
                "qty": float(p["qty"]),
                "avg_entry": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "unrealized_pnl": float(p["unrealized_pl"]),
                "unrealized_pnl_pct": round(float(p["unrealized_plpc"]) * 100, 2),
                "market_value": float(p["market_value"]),
                "side": p["side"],
            }
            for p in positions
        ]}

    def _get_bars(self, symbol: str, timeframe: str = "1Day", limit: int = 30) -> dict:
        data = self._data(f"/v2/stocks/{symbol}/bars",
                          timeframe=timeframe, limit=limit,
                          adjustment="split", feed="iex")
        bars = data.get("bars", [])
        if not bars:
            return {"error": f"No bars for {symbol}"}
        closes = [b["c"] for b in bars]
        volumes = [b["v"] for b in bars]
        avg_vol = sum(volumes) / len(volumes) if volumes else 0
        latest = bars[-1]
        prev = bars[-2] if len(bars) > 1 else bars[-1]
        change_pct = ((latest["c"] - prev["c"]) / prev["c"]) * 100 if prev["c"] else 0
        return {
            "symbol": symbol,
            "latest_close": latest["c"],
            "latest_volume": latest["v"],
            "avg_volume_20": round(avg_vol),
            "volume_ratio": round(latest["v"] / avg_vol, 2) if avg_vol else 0,
            "change_pct_1bar": round(change_pct, 2),
            "high_20": max(b["h"] for b in bars),
            "low_20": min(b["l"] for b in bars),
            "bars": [{"t": b["t"], "o": b["o"], "h": b["h"],
                      "l": b["l"], "c": b["c"], "v": b["v"]} for b in bars[-10:]],
        }

    def _get_quote(self, symbol: str) -> dict:
        data = self._data(f"/v2/stocks/{symbol}/quotes/latest")
        q = data.get("quote", {})
        return {
            "symbol": symbol,
            "bid": q.get("bp", 0),
            "ask": q.get("ap", 0),
            "spread": round(q.get("ap", 0) - q.get("bp", 0), 4),
        }

    # ── Learning tool implementations ─────────────────────────────────────────

    def _get_historical_context(self, symbol: str) -> dict:
        stats = tdb.get_historical_stats(symbol)
        if "error" in stats:
            return {
                "warning": f"No historical data for {symbol} yet. Run scripts/load_history.py",
                "symbol": symbol,
            }
        return stats

    def _detect_regime(self) -> dict:
        spy_stats = tdb.get_historical_stats("SPY")
        if "error" in spy_stats:
            return {
                "regime": "unknown",
                "reason": "No SPY historical data — run scripts/load_history.py",
            }

        price = spy_stats.get("current_price", 0)
        ma50 = spy_stats.get("ma50") or price
        ma200 = spy_stats.get("ma200") or price
        atr = spy_stats.get("atr14") or 0
        rsi = spy_stats.get("rsi14") or 50
        vol_ratio = spy_stats.get("vol_ratio_today") or 1.0

        # Volatility as % of price (proxy for fear/stress)
        volatility_pct = (atr / price * 100) if price else 0

        above_ma50 = price > ma50
        above_ma200 = price > ma200
        ma50_above_ma200 = ma50 > ma200

        if above_ma50 and ma50_above_ma200 and rsi > 50:
            if volatility_pct > 2.0:
                regime = "choppy"
                reason = "Price above MAs but high volatility — uncertain"
            else:
                regime = "bull_trend"
                reason = "SPY above 50MA and 200MA, RSI bullish"
        elif not above_ma50 and not ma50_above_ma200:
            regime = "bear_trend"
            reason = "SPY below both MAs — defensive posture"
        else:
            regime = "choppy"
            reason = "Mixed signals: MAs conflicting or RSI neutral"

        return {
            "regime": regime,
            "reason": reason,
            "spy_price": price,
            "spy_ma50": round(ma50, 2),
            "spy_ma200": round(round(ma200, 2) if ma200 else 0, 2),
            "spy_rsi": rsi,
            "spy_volatility_pct": round(volatility_pct, 2),
            "spy_volume_ratio": vol_ratio,
            "strategy_implication": {
                "bull_trend": "Favor momentum longs on breakouts. Size up to 5% per position.",
                "bear_trend": "Avoid longs. Hold cash or very small positions only. Wait for reversal.",
                "choppy": "Mean-reversion only. Cut position sizes in half. Tighter stops.",
            }.get(regime, "Assess each trade individually."),
        }

    def _get_trade_journal(self, symbol: str = None, limit: int = 30) -> dict:
        trades = tdb.get_trade_history(symbol=symbol, limit=limit)
        if not trades:
            return {"message": "No trade history yet", "trades": []}
        return {
            "count": len(trades),
            "trades": [
                {
                    "symbol": t["symbol"],
                    "side": t["side"],
                    "entry": t["entry_price"],
                    "exit": t["exit_price"],
                    "pnl": t["pnl"],
                    "pnl_pct": t["pnl_pct"],
                    "won": bool(t["won"]),
                    "signal": t["signal_type"],
                    "regime": t["market_regime"],
                    "exit_reason": t["exit_reason"],
                    "day": t["day_of_week"],
                    "notes": (t["setup_notes"] or "")[:200],
                }
                for t in trades
            ],
        }

    def _get_perf_stats(self) -> dict:
        return tdb.get_performance_stats()

    def _get_strategy_notes(self, limit: int = 3) -> dict:
        notes = tdb.get_latest_strategy_notes(limit)
        if not notes:
            return {"message": "No strategy reviews yet — first review runs Sunday 1am UTC"}
        return {
            "count": len(notes),
            "reviews": [
                {
                    "week_ending": n["week_ending"],
                    "win_rate": n["win_rate"],
                    "net_pnl": n["net_pnl"],
                    "advice": n["content"],
                }
                for n in notes
            ],
        }

    def _log_trade_close(self, order_id: str, exit_price: float, exit_reason: str) -> dict:
        result = tdb.close_trade(order_id, exit_price, exit_reason)
        print(f"  [journal] Closed trade {order_id}: {result}")
        return result

    # ── Order management implementations ──────────────────────────────────────

    def _place_order(self, symbol: str, side: str, qty: float,
                     order_type: str = "market", limit_price: float = None,
                     stop_loss_price: float = None, take_profit_price: float = None,
                     signal_type: str = "manual", market_regime: str = "unknown",
                     reason: str = "") -> dict:
        print(f"  [trade] {side.upper()} {qty} {symbol} @ {order_type} "
              f"| signal={signal_type} regime={market_regime} | {reason[:80]}")

        payload: dict = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": "day",
        }
        if order_type == "limit" and limit_price:
            payload["limit_price"] = str(limit_price)

        if side == "buy" and stop_loss_price and take_profit_price:
            payload["order_class"] = "bracket"
            payload["stop_loss"] = {"stop_price": str(stop_loss_price)}
            payload["take_profit"] = {"limit_price": str(take_profit_price)}
        elif side == "buy" and stop_loss_price:
            payload["order_class"] = "oto"
            payload["stop_loss"] = {"stop_price": str(stop_loss_price)}

        result = self._alpaca("POST", "/v2/orders", json=payload)
        if "error" in result:
            return result

        order_id = result.get("id", "")
        entry_price = float(limit_price or 0)

        # Determine SPY trend for journal context
        spy_stats = tdb.get_historical_stats("SPY")
        spy_trend = "unknown"
        if "current_price" in spy_stats and spy_stats.get("ma50"):
            spy_trend = "above_ma50" if spy_stats["current_price"] > spy_stats["ma50"] else "below_ma50"

        # Auto-log to trade journal when a buy opens
        if side == "buy" and order_id:
            tdb.open_trade(
                order_id=order_id,
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=entry_price,
                signal_type=signal_type,
                setup_notes=reason,
                market_regime=market_regime,
                spy_trend=spy_trend,
            )
            print(f"  [journal] Opened trade {order_id} for {symbol}")

        return {
            "success": True,
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "type": order_type,
            "status": result.get("status"),
            "signal_type": signal_type,
            "reason": reason,
            "note": "Trade logged to journal. Call log_trade_close when position exits.",
        }

    def _cancel_order(self, order_id: str) -> dict:
        r = httpx.delete(f"{ALPACA_BASE}/v2/orders/{order_id}",
                         headers=self._headers, timeout=15)
        return {"success": r.status_code == 204, "order_id": order_id}

    def _close_position(self, symbol: str, reason: str = "") -> dict:
        result = self._alpaca("DELETE", f"/v2/positions/{symbol}")
        if "error" in result:
            return result
        exit_price = float(result.get("avg_entry_price", 0) or 0)
        return {
            "success": True,
            "symbol": symbol,
            "reason": reason,
            "exit_price": exit_price,
            "note": "Call log_trade_close with the order_id to record P&L in the journal",
        }

    def _get_open_orders(self) -> dict:
        orders = self._alpaca("GET", "/v2/orders", params={"status": "open"})
        if isinstance(orders, dict) and "error" in orders:
            return orders
        return {"open_orders": [
            {"id": o["id"], "symbol": o["symbol"], "side": o["side"],
             "qty": o["qty"], "type": o["type"], "status": o["status"]}
            for o in orders
        ]}

    # ── Session runners ───────────────────────────────────────────────────────

    def run_morning_session(self) -> str:
        context = _build_context_header()
        task = f"""
{context}

=== MORNING TRADING SESSION ===

You have access to 6 years of historical data and your full trade journal.
Use them. Do not trade blind.

Steps:
1. get_strategy_notes — read past self-review advice first
2. detect_market_regime — set your strategy mode for today
3. get_account — check cash, buying power, daily P&L
   - If daily P&L is already down >5%: STOP, report, do not trade
4. get_positions — review overnight positions
5. get_open_orders — cancel any stale orders
6. For each symbol in {WATCHLIST}:
   a. get_historical_context(symbol) — check 52w range, MAs, RSI, seasonal tendency
   b. get_price_data(symbol, timeframe=1Day, limit=20) — fresh momentum check
   c. Identify signals:
      - Momentum: price near 52w high + volume_ratio > 1.5 + above MA50 + RSI 50-70
      - Mean-reversion: price >3% below MA20 + RSI < 35 + historically bounces here
7. Pick the 1-2 best setups (only if regime supports it):
   a. get_quotes — check bid/ask
   b. Position size: min(5% portfolio, buying_power/2)
   c. Calculate stops: stop_loss = entry × 0.98, take_profit = entry × 1.04
   d. place_order with signal_type, market_regime, and full reasoning in reason field
8. Report what was traded, what was skipped, regime, and what history told you
"""
        return self.run(task)

    def run_eod_session(self) -> str:
        task = """
=== END-OF-DAY SESSION ===

Steps:
1. get_account — current P&L
2. get_positions — review all open positions
3. For each position:
   - If unrealized_pnl_pct > +3%: close_position (take profit)
     Then log_trade_close with exit_reason=take_profit
   - If unrealized_pnl_pct < -2%: close_position (stop-loss)
     Then log_trade_close with exit_reason=stop_loss
   - Otherwise: hold if conviction is still high (momentum intact)
4. get_open_orders — cancel any unfilled day orders
5. Report: final P&L for the day, any lessons, what positions remain overnight
"""
        return self.run(task)
