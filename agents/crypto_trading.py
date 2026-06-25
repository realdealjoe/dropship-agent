"""
Crypto trading agent — runs every 4 hours via scheduler, 24/7.
Watches BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD on Coinbase Advanced Trade.
Logs all trades to trading.db alongside the stock bot's journal.
"""
import os
import time
import json
import secrets
import base64
from datetime import datetime, timezone

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import anthropic
import trading_db as tdb
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

PAPER        = os.getenv("COINBASE_PAPER", "true").lower() == "true"
KEY_NAME     = os.getenv("COINBASE_API_KEY_NAME", "")
_KEY_B64     = os.getenv("COINBASE_API_PRIVATE_KEY", "")
BASE_URL     = "https://api.coinbase.com"

WATCHLIST         = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD"]
MAX_POSITION_PCT  = 0.20    # max 20% of portfolio per trade
STOP_LOSS_PCT     = 0.03    # 3% stop loss (wider than stocks)
TAKE_PROFIT_PCT   = 0.08    # 8% take profit → 2.6x reward/risk
CRASH_THRESHOLD   = 0.08    # if BTC drops 8%+ in 24h → skip session

_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── JWT Auth ──────────────────────────────────────────────────────────────────

def _make_jwt(method: str, path: str) -> str:
    raw = base64.b64decode(_KEY_B64)
    pk  = Ed25519PrivateKey.from_private_bytes(raw[:32])
    payload = {
        "sub": KEY_NAME,
        "iss": "cdp",
        "nbf": int(time.time()),
        "exp": int(time.time()) + 120,
        "uri": f"{method} api.coinbase.com{path}",
    }
    return jwt.encode(payload, pk, algorithm="EdDSA",
                      headers={"kid": KEY_NAME, "nonce": secrets.token_hex(10)})


def _get(path: str, params: dict = None) -> dict:
    token = _make_jwt("GET", path)
    r = httpx.get(f"{BASE_URL}{path}", params=params,
                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    token = _make_jwt("POST", path)
    r = httpx.post(f"{BASE_URL}{path}", json=body,
                   headers={"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json()


# ── Market Data ───────────────────────────────────────────────────────────────

def _get_price(product_id: str) -> float:
    try:
        data = _get("/api/v3/brokerage/best_bid_ask", {"product_ids": [product_id]})
        pb   = data.get("pricebooks", [{}])[0]
        bid  = float(pb["bids"][0]["price"]) if pb.get("bids") else 0
        ask  = float(pb["asks"][0]["price"]) if pb.get("asks") else 0
        return (bid + ask) / 2
    except Exception:
        return 0.0


def _get_candles(product_id: str, days: int = 30) -> list:
    end   = int(time.time())
    start = end - days * 86400
    data  = _get(f"/api/v3/brokerage/products/{product_id}/candles", {
        "start": str(start), "end": str(end), "granularity": "ONE_DAY",
    })
    candles = data.get("candles", [])
    candles.sort(key=lambda c: c["start"])
    return candles


def _get_portfolio() -> dict:
    data     = _get("/api/v3/brokerage/accounts")
    accounts = data.get("accounts", [])
    usd      = 0.0
    holdings = {}
    for acc in accounts:
        cur   = acc.get("currency", "")
        avail = float(acc.get("available_balance", {}).get("value", 0))
        if cur == "USD":
            usd = avail
        elif avail > 0.0:
            holdings[cur] = avail
    return {"usd": usd, "holdings": holdings}


def _market_data(product_id: str) -> dict:
    candles = _get_candles(product_id, days=30)
    closes  = [float(c["close"]) for c in candles]
    highs   = [float(c["high"])  for c in candles]
    lows    = [float(c["low"])   for c in candles]
    price   = _get_price(product_id)
    if not closes:
        return {"error": f"No candle data for {product_id}"}

    ma7  = sum(closes[-7:])  / min(7,  len(closes))
    ma14 = sum(closes[-14:]) / min(14, len(closes))
    ma30 = sum(closes)       / len(closes)

    change_24h = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0

    # ATR14
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = highs[i], lows[i], closes[i - 1]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr14 = sum(trs[-14:]) / min(14, len(trs)) if trs else price * 0.05

    # RSI14
    gains  = [max(0, closes[i] - closes[i-1]) for i in range(max(1, len(closes)-14), len(closes))]
    losses = [max(0, closes[i-1] - closes[i]) for i in range(max(1, len(closes)-14), len(closes))]
    ag     = sum(gains) / 14 if gains else 0
    al     = sum(losses) / 14 if losses else 1
    rsi14  = 100 - (100 / (1 + ag / al)) if al > 0 else 100

    return {
        "product_id":    product_id,
        "price":         round(price, 6),
        "change_24h_pct": round(change_24h, 2),
        "ma7":           round(ma7,  6),
        "ma14":          round(ma14, 6),
        "ma30":          round(ma30, 6),
        "price_vs_ma14": round((price - ma14) / ma14 * 100, 2) if ma14 else None,
        "atr14":         round(atr14, 6),
        "atr_pct":       round(atr14 / price * 100, 2) if price else None,
        "rsi14":         round(rsi14, 1),
    }


# ── Order Execution ────────────────────────────────────────────────────────────

def _place_order(product_id: str, side: str, usd_amount: float,
                 signal_type: str, setup_notes: str, market_regime: str) -> dict:
    if PAPER:
        fake_id  = f"PAPER-CRYPTO-{product_id}-{int(time.time())}"
        price    = _get_price(product_id)
        qty      = usd_amount / price if price > 0 else 0
        print(f"  [PAPER] {side} ${usd_amount:.2f} of {product_id} @ ${price:.4f} ({qty:.6f} units)")
        tdb.open_trade(order_id=fake_id, symbol=product_id, side=side.lower(),
                       qty=qty, entry_price=price, signal_type=signal_type,
                       setup_notes=setup_notes, market_regime=market_regime)
        return {"order_id": fake_id, "price": price, "qty": qty, "paper": True}

    client_order_id = secrets.token_hex(16)
    body = {
        "client_order_id": client_order_id,
        "product_id": product_id,
        "side": side.upper(),
        "order_configuration": {"market_market_ioc": {"quote_size": f"{usd_amount:.2f}"}},
    }
    resp     = _post("/api/v3/brokerage/orders", body)
    order    = resp.get("success_response", {})
    order_id = order.get("order_id", client_order_id)
    price    = _get_price(product_id)
    qty      = usd_amount / price if price > 0 else 0
    tdb.open_trade(order_id=order_id, symbol=product_id, side=side.lower(),
                   qty=qty, entry_price=price, signal_type=signal_type,
                   setup_notes=setup_notes, market_regime=market_regime)
    return {"order_id": order_id, "price": price, "qty": qty}


# ── Tools ─────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_portfolio",
        "description": "Get current USD cash balance and all crypto holdings.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_market_data",
        "description": "Current price, 24h change, MA7/14/30, ATR, RSI for a crypto pair.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "e.g. BTC-USD, ETH-USD"}
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "get_historical_context",
        "description": "6-year historical stats (52w range, avg returns, MA levels) from DB.",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "e.g. BTC-USD"}
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "get_performance_stats",
        "description": "All-time win rate, PnL, and breakdown by symbol/signal from every past crypto trade.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "buy_crypto",
        "description": "Buy a crypto pair with a specified USD amount (market order).",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id":  {"type": "string"},
                "usd_amount":  {"type": "number", "description": "USD to spend"},
                "signal_type": {
                    "type": "string",
                    "enum": ["momentum", "mean_reversion", "breakout", "dip_buy"]
                },
                "reasoning":   {"type": "string", "description": "Full trade rationale"},
            },
            "required": ["product_id", "usd_amount", "signal_type", "reasoning"],
        },
    },
    {
        "name": "sell_crypto",
        "description": "Sell a crypto pair (market order, specify USD worth to sell).",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string"},
                "usd_amount": {"type": "number", "description": "USD worth to sell"},
                "reasoning":  {"type": "string"},
                "exit_reason": {
                    "type": "string",
                    "enum": ["take_profit", "stop_loss", "regime_change", "rebalance"]
                },
            },
            "required": ["product_id", "usd_amount", "reasoning", "exit_reason"],
        },
    },
    {
        "name": "skip_session",
        "description": "Skip trading this 4-hour session. Use when no clear opportunity or crash risk.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


def _handle_tool(name: str, inp: dict, market_regime: str) -> str:
    try:
        if name == "get_portfolio":
            return json.dumps(_get_portfolio())

        elif name == "get_market_data":
            return json.dumps(_market_data(inp["product_id"]))

        elif name == "get_historical_context":
            return json.dumps(tdb.get_historical_stats(inp["symbol"]))

        elif name == "get_performance_stats":
            return json.dumps(tdb.get_performance_stats())

        elif name == "buy_crypto":
            pid  = inp["product_id"]
            usd  = float(inp["usd_amount"])
            port = _get_portfolio()
            max_trade = port["usd"] * MAX_POSITION_PCT
            if usd > max_trade:
                usd = round(max_trade, 2)
            if usd < 1.0:
                return json.dumps({"error": "Insufficient USD balance (< $1)"})
            result = _place_order(pid, "BUY", usd,
                                  inp["signal_type"], inp["reasoning"], market_regime)
            return json.dumps(result)

        elif name == "sell_crypto":
            pid    = inp["product_id"]
            usd    = float(inp["usd_amount"])
            result = _place_order(pid, "SELL", usd,
                                  inp.get("exit_reason", "exit"), inp["reasoning"], market_regime)
            return json.dumps(result)

        elif name == "skip_session":
            print(f"  [SKIP] {inp['reason']}")
            return json.dumps({"status": "skipped", "reason": inp["reason"]})

    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({"error": f"Unknown tool: {name}"})


# ── Regime Detection ──────────────────────────────────────────────────────────

def _detect_regime() -> str:
    try:
        candles = _get_candles("BTC-USD", days=60)
        closes  = [float(c["close"]) for c in candles]
        if len(closes) < 7:
            return "unknown"
        change_24h = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        if change_24h < -CRASH_THRESHOLD * 100:
            return "crash_risk"
        ma7  = sum(closes[-7:])  / 7
        ma30 = sum(closes[-30:]) / 30 if len(closes) >= 30 else sum(closes) / len(closes)
        if ma7 > ma30 * 1.03:
            return "bull_trend"
        elif ma7 < ma30 * 0.97:
            return "bear_trend"
        return "choppy"
    except Exception:
        return "unknown"


# ── Context Header ─────────────────────────────────────────────────────────────

def _build_system(regime: str) -> str:
    notes = tdb.get_latest_strategy_notes(limit=2)
    stats = tdb.get_performance_stats()
    return f"""You are a crypto trading agent managing a Coinbase portfolio.
Crypto markets run 24/7 — you run every 4 hours.

## Current Market Regime
{regime}

## Trading Rules
- Max 20% of portfolio per single trade
- Stop loss: 3% below entry (crypto is volatile — wider stops needed)
- Take profit: 8% gain (2.6x reward/risk ratio)
- If BTC crashed 8%+ in 24h → skip ALL trades (already checked — safe if you see this)
- Always keep ≥30% in USD as dry powder for dip opportunities
- Prefer mean-reversion on oversold dips (RSI < 35) + momentum on confirmed breakouts
- Max 2 trades per session — quality over quantity
- Only trade when the setup is clear and compelling. Skip if uncertain.

## Performance Stats
{json.dumps(stats, indent=2) if isinstance(stats, dict) else "No closed trades yet."}

## Strategy Notes from Weekly Review
{chr(10).join(n['content'][:600] for n in notes) if notes else "No weekly reviews yet — still learning."}
"""


# ── Main Session ──────────────────────────────────────────────────────────────

def run_crypto_session():
    if not KEY_NAME or not _KEY_B64:
        print("[crypto] COINBASE_API_KEY_NAME or COINBASE_API_PRIVATE_KEY not set — skipping")
        return

    print(f"\n[crypto] Session start {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")

    regime = _detect_regime()
    print(f"[crypto] Regime: {regime}")

    if regime == "crash_risk":
        print("[crypto] BTC crash detected — skipping session to preserve capital")
        return

    mode_str = "PAPER (simulated)" if PAPER else "LIVE"
    messages = [{
        "role": "user",
        "content": (
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
            f"Regime: {regime} | Mode: {mode_str}\n"
            f"Watchlist: {', '.join(WATCHLIST)}\n\n"
            "Review the market. Steps:\n"
            "1. get_portfolio — see your cash and holdings\n"
            "2. get_market_data for any pairs that look interesting\n"
            "3. get_performance_stats to see what setups have worked\n"
            "4. Make 0-2 trades max (or skip_session if nothing compelling)\n"
            "Preserve capital first. Only trade clear setups."
        ),
    }]

    for _turn in range(14):
        resp = _client.messages.create(
            model="claude-opus-4-8",
            max_tokens=4096,
            system=_build_system(regime),
            tools=TOOLS,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if hasattr(block, "text") and block.text:
                    print(f"[crypto] {block.text[:400]}")
            break

        if resp.stop_reason != "tool_use":
            break

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": resp.content})

        results = []
        for tu in tool_uses:
            print(f"  → {tu.name}({json.dumps(tu.input)[:80]})")
            out = _handle_tool(tu.name, tu.input, regime)
            print(f"    ← {out[:150]}")
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})

        messages.append({"role": "user", "content": results})

    print(f"[crypto] Session complete")


if __name__ == "__main__":
    run_crypto_session()
