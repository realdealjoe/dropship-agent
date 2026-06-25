"""
Trading Self-Review Agent
Runs every Sunday at 1am UTC — reads the full week of trades and writes
strategy improvement notes that get injected into every future trading session.

This is how the bot learns:
  1. Reads all closed trades from the past week
  2. Computes win rates, identifies patterns (day, signal type, regime)
  3. Writes specific actionable strategy updates
  4. Saves to strategy_notes table — the morning session reads these every day
"""
from datetime import datetime, timedelta, timezone
from agents.base_agent import BaseAgent
import trading_db as tdb

SYSTEM = """You are a trading performance analyst reviewing the past week of trades
to improve the algorithmic trading strategy.

Your job:
- Be ruthlessly honest about what's working and what isn't
- Identify patterns: which signals win most, which days are dangerous, which stocks behave best
- Write specific, actionable strategy adjustments
- If win rate on a signal type is <40%, recommend stopping that signal type
- If a particular stock is consistently losing, recommend removing it from watchlist
- If a day of week consistently loses, warn to reduce size that day
- Praise what's working and reinforce it

Your output will be stored and fed into every future trading session as guidance.
Write like you're leaving notes for yourself that you'll actually follow.
Be specific, not generic. "NVDA mean-reversion works better when RSI<30" is good.
"Be careful with volatile stocks" is useless.
"""


class TradingReviewAgent(BaseAgent):
    name = "trading_review"
    system_prompt = SYSTEM

    def __init__(self):
        super().__init__()
        self._register_tools()

    def _register_tools(self):
        self.register_tool({
            "name": "get_week_trades",
            "description": "Get all trades closed in the past N days",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days_back": {"type": "integer", "description": "How many days to look back (default 7)"},
                },
            },
        }, self._get_week_trades)

        self.register_tool({
            "name": "get_all_time_stats",
            "description": "Get aggregated stats across all trades ever made",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_all_time_stats)

        self.register_tool({
            "name": "get_past_strategy_notes",
            "description": "Read past strategy review notes to see what was already tried",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_past_notes)

        self.register_tool({
            "name": "save_strategy_review",
            "description": "Save this week's strategy review and recommendations to the database",
            "input_schema": {
                "type": "object",
                "properties": {
                    "review_text": {
                        "type": "string",
                        "description": (
                            "Full review text including: week summary, what worked, what didn't, "
                            "specific rule changes, stocks to watch more/less, signals to prioritize"
                        ),
                    },
                    "win_rate": {"type": "number", "description": "This week's win rate percentage"},
                    "total_trades": {"type": "integer"},
                    "net_pnl": {"type": "number", "description": "Net P&L for the week in dollars"},
                },
                "required": ["review_text", "win_rate", "total_trades", "net_pnl"],
            },
        }, self._save_review)

    def _get_week_trades(self, days_back: int = 7) -> dict:
        all_trades = tdb.get_trade_history(limit=500)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

        week_trades = []
        for t in all_trades:
            if not t.get("exit_time"):
                continue
            try:
                exit_dt = datetime.fromisoformat(
                    t["exit_time"].replace("Z", "+00:00")
                )
                if exit_dt >= cutoff:
                    week_trades.append(t)
            except Exception:
                pass

        if not week_trades:
            return {"message": f"No closed trades in the last {days_back} days", "trades": []}

        wins = [t for t in week_trades if t["won"]]
        losses = [t for t in week_trades if not t["won"]]
        net_pnl = sum(t["pnl"] for t in week_trades if t["pnl"])
        win_rate = round(len(wins) / len(week_trades) * 100, 1) if week_trades else 0

        # Group by various dimensions for the AI to analyze
        by_symbol: dict = {}
        by_signal: dict = {}
        by_day: dict = {}
        by_regime: dict = {}

        for t in week_trades:
            for group, key in [
                (by_symbol, t.get("symbol", "?")),
                (by_signal, t.get("signal_type") or "unknown"),
                (by_day, t.get("day_of_week") or "unknown"),
                (by_regime, t.get("market_regime") or "unknown"),
            ]:
                if key not in group:
                    group[key] = {"wins": 0, "losses": 0, "pnl": 0.0,
                                  "avg_holding_hours": [], "setups": []}
                group[key]["wins" if t["won"] else "losses"] += 1
                group[key]["pnl"] += t.get("pnl") or 0
                if t.get("holding_hours"):
                    group[key]["avg_holding_hours"].append(t["holding_hours"])
                if t.get("setup_notes"):
                    group[key]["setups"].append((t["setup_notes"] or "")[:100])

        # Compute avg holding hours
        for group in [by_symbol, by_signal, by_day, by_regime]:
            for key in group:
                hrs = group[key]["avg_holding_hours"]
                group[key]["avg_holding_hours"] = round(sum(hrs) / len(hrs), 1) if hrs else 0

        return {
            "period_days": days_back,
            "total_trades": len(week_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": win_rate,
            "net_pnl": round(net_pnl, 2),
            "by_symbol": by_symbol,
            "by_signal_type": by_signal,
            "by_day_of_week": by_day,
            "by_market_regime": by_regime,
            "individual_trades": [
                {
                    "symbol": t["symbol"],
                    "signal": t["signal_type"],
                    "regime": t["market_regime"],
                    "entry": t["entry_price"],
                    "exit": t["exit_price"],
                    "pnl": t["pnl"],
                    "pnl_pct": t["pnl_pct"],
                    "won": bool(t["won"]),
                    "exit_reason": t["exit_reason"],
                    "day": t["day_of_week"],
                    "holding_hours": t.get("holding_hours"),
                    "setup": (t["setup_notes"] or "")[:200],
                }
                for t in week_trades
            ],
        }

    def _get_all_time_stats(self) -> dict:
        return tdb.get_performance_stats()

    def _get_past_notes(self) -> dict:
        notes = tdb.get_latest_strategy_notes(4)
        if not notes:
            return {"message": "No past reviews yet — this is the first one"}
        return {
            "past_reviews": [
                {
                    "week_ending": n["week_ending"],
                    "win_rate": n["win_rate"],
                    "net_pnl": n["net_pnl"],
                    "content": n["content"],
                }
                for n in notes
            ]
        }

    def _save_review(self, review_text: str, win_rate: float,
                     total_trades: int, net_pnl: float) -> dict:
        week_ending = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        tdb.save_strategy_notes(
            week_ending=week_ending,
            content=review_text,
            win_rate=win_rate,
            total_trades=total_trades,
            net_pnl=net_pnl,
        )
        print(f"  [review] Strategy notes saved for week ending {week_ending}")
        return {
            "success": True,
            "week_ending": week_ending,
            "message": "Review saved. Will be injected into every future trading session.",
        }

    def run_weekly_review(self) -> str:
        task = """
Run the weekly trading performance review.

Steps:
1. get_past_strategy_notes — see what advice was already given, avoid repeating
2. get_week_trades(days_back=7) — analyze every trade from this past week
3. get_all_time_stats — see the overall picture (cumulative win rates, profit factor)

For your analysis, cover:
a. WHAT WORKED: which signals, symbols, days, and regimes had the best win rates
b. WHAT FAILED: signals/symbols with <50% win rate — be specific about why (if you can tell)
c. PATTERN SPOTTING: any interesting correlations (e.g. "Monday momentum trades all lose")
d. SPECIFIC RULE CHANGES: at least 2 concrete adjustments to make next week
e. STOCKS TO RECONSIDER: any symbol that's been consistently losing — consider dropping it
f. UPCOMING: any known market events next week to watch for (earnings, Fed meetings)

Then: save_strategy_review with your full analysis
  - win_rate: the week's win rate percentage
  - total_trades: number of closed trades this week
  - net_pnl: total P&L for the week
  - review_text: your complete, specific analysis (800-1500 words)

If there were 0 trades this week, still write a brief review noting that and
reaffirming the best strategies based on all-time stats.
"""
        return self.run(task)
