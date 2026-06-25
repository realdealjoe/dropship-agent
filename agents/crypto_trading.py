"""
Crypto trading agent — runs every 4 hours via scheduler, 24/7.
Watches BTC-USD, ETH-USD, SOL-USD, DOGE-USD, AVAX-USD on Coinbase Advanced Trade.
Logs all trades to trading.db alongside the stock bot's journal.

Edge data sources:
  - Fear & Greed Index (alternative.me)
  - BTC funding rates (Binance perpetuals)
  - Whale alert / on-chain large transfers (whale-alert.io public feed)
  - Multi-timeframe candles (4h + 1d)
  - Coinbase market data
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

WATCHLIST        = ["BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD"]
MAX_POSITION_PCT = 0.25   # up to 25% per trade when conviction is high
CRASH_THRESHOLD  = 0.08   # if BTC drops 8%+ in 24h → skip session
DAILY_LOSS_LIMIT = 0.15   # stop trading if portfolio drops 15% in a day

_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── JWT Auth (Coinbase) ───────────────────────────────────────────────────────

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


def _cb_get(path: str, params: dict = None) -> dict:
    token = _make_jwt("GET", path)
    r = httpx.get(f"{BASE_URL}{path}", params=params,
                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def _cb_post(path: str, body: dict) -> dict:
    token = _make_jwt("POST", path)
    r = httpx.post(f"{BASE_URL}{path}", json=body,
                   headers={"Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    return r.json()


# ── Market Intelligence ────────────────────────────────────────────────────────

def get_fear_and_greed() -> dict:
    """Crypto Fear & Greed Index (0=extreme fear, 100=extreme greed)."""
    try:
        r = httpx.get("https://api.alternative.me/fng/?limit=2", timeout=10)
        data = r.json().get("data", [])
        if data:
            today     = data[0]
            yesterday = data[1] if len(data) > 1 else data[0]
            return {
                "value":           int(today["value"]),
                "classification":  today["value_classification"],
                "yesterday_value": int(yesterday["value"]),
                "trend":           "rising" if int(today["value"]) > int(yesterday["value"]) else "falling",
            }
    except Exception as e:
        return {"error": str(e)}
    return {}


def get_funding_rates() -> dict:
    """BTC/ETH/SOL perpetual futures funding rates from OKX (positive = longs paying shorts)."""
    pairs = {"BTC-USD-SWAP": "BTC-USD", "ETH-USD-SWAP": "ETH-USD", "SOL-USD-SWAP": "SOL-USD"}
    rates = {}
    for inst_id, label in pairs.items():
        try:
            r    = httpx.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}", timeout=10)
            data = r.json().get("data", [{}])[0]
            rate = float(data.get("fundingRate", 0)) * 100
            rates[label] = {
                "funding_rate_pct": round(rate, 4),
                "signal": "bearish" if rate > 0.05 else "bullish" if rate < -0.02 else "neutral",
                "note": "Longs very crowded — reversal risk" if rate > 0.1
                        else "Shorts crowded — squeeze possible" if rate < -0.05
                        else "Neutral funding",
            }
        except Exception as e:
            rates[label] = {"error": str(e)}
    return rates


def get_onchain_signals() -> dict:
    """
    BTC on-chain signals from public Blockchain.com API.
    Tracks exchange netflow proxy and mempool pressure.
    """
    signals = {}
    try:
        # Mempool size (congestion = high activity = bullish pressure)
        r = httpx.get("https://api.blockchain.info/stats", timeout=10)
        stats = r.json()
        signals["btc_hash_rate_th"]     = round(stats.get("hash_rate", 0) / 1e12, 2)
        signals["btc_mempool_size_mb"]  = round(stats.get("mempool_size", 0) / 1e6, 2)
        signals["btc_tx_per_second"]    = round(stats.get("n_tx_per_block", 0) / 600, 2)
        signals["btc_difficulty"]       = stats.get("difficulty", 0)
        # High mempool = network congestion = lots of activity
        signals["network_activity"] = (
            "very_high" if signals["btc_mempool_size_mb"] > 50
            else "high" if signals["btc_mempool_size_mb"] > 20
            else "normal"
        )
    except Exception as e:
        signals["error"] = str(e)
    return signals


def get_market_dominance() -> dict:
    """BTC dominance from CoinGecko (free, no key needed)."""
    try:
        r = httpx.get(
            "https://api.coingecko.com/api/v3/global",
            headers={"Accept": "application/json"},
            timeout=10
        )
        data = r.json().get("data", {})
        dom  = data.get("market_cap_percentage", {})
        total_mcap = data.get("total_market_cap", {}).get("usd", 0)
        return {
            "btc_dominance":    round(dom.get("btc", 0), 1),
            "eth_dominance":    round(dom.get("eth", 0), 1),
            "total_mcap_usd":   total_mcap,
            "market_cap_change_24h": round(data.get("market_cap_change_percentage_24h_usd", 0), 2),
            "signal": (
                "alt_season"   if dom.get("btc", 50) < 42 else
                "btc_season"   if dom.get("btc", 50) > 58 else
                "balanced"
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def get_4h_candles(product_id: str, count: int = 48) -> list:
    """Last N 4-hour candles for intraday momentum."""
    end   = int(time.time())
    start = end - count * 4 * 3600
    try:
        data    = _cb_get(f"/api/v3/brokerage/products/{product_id}/candles",
                          {"start": str(start), "end": str(end), "granularity": "SIX_HOUR"})
        candles = data.get("candles", [])
        candles.sort(key=lambda c: c["start"])
        return candles
    except Exception:
        return []


# ── Coinbase Data ──────────────────────────────────────────────────────────────

def _get_price(product_id: str) -> float:
    try:
        data = _cb_get("/api/v3/brokerage/best_bid_ask", {"product_ids": [product_id]})
        pb   = data.get("pricebooks", [{}])[0]
        bid  = float(pb["bids"][0]["price"]) if pb.get("bids") else 0
        ask  = float(pb["asks"][0]["price"]) if pb.get("asks") else 0
        return (bid + ask) / 2
    except Exception:
        return 0.0


def _get_daily_candles(product_id: str, days: int = 30) -> list:
    end   = int(time.time())
    start = end - days * 86400
    try:
        data    = _cb_get(f"/api/v3/brokerage/products/{product_id}/candles",
                          {"start": str(start), "end": str(end), "granularity": "ONE_DAY"})
        candles = data.get("candles", [])
        candles.sort(key=lambda c: c["start"])
        return candles
    except Exception:
        return []


def _get_portfolio() -> dict:
    data     = _cb_get("/api/v3/brokerage/accounts")
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


def _full_market_data(product_id: str) -> dict:
    daily   = _get_daily_candles(product_id, days=30)
    intra   = get_4h_candles(product_id, count=24)
    closes  = [float(c["close"]) for c in daily]
    highs   = [float(c["high"])  for c in daily]
    lows    = [float(c["low"])   for c in daily]
    price   = _get_price(product_id)
    if not closes:
        return {"error": f"No data for {product_id}"}

    ma7  = sum(closes[-7:])  / min(7,  len(closes))
    ma14 = sum(closes[-14:]) / min(14, len(closes))
    ma30 = sum(closes)       / len(closes)

    change_24h = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
    change_7d  = (closes[-1] - closes[-7]) / closes[-7] * 100 if len(closes) >= 7 else 0

    trs   = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
             for i in range(1, len(daily))]
    atr14 = sum(trs[-14:]) / min(14, len(trs)) if trs else price * 0.05

    gains  = [max(0, closes[i]-closes[i-1]) for i in range(max(1, len(closes)-14), len(closes))]
    losses = [max(0, closes[i-1]-closes[i]) for i in range(max(1, len(closes)-14), len(closes))]
    ag, al = sum(gains)/14 if gains else 0, sum(losses)/14 if losses else 1
    rsi14  = round(100 - (100 / (1 + ag/al)), 1) if al > 0 else 100

    # 4h trend
    intra_closes = [float(c["close"]) for c in intra]
    intra_trend  = "up" if (len(intra_closes) >= 2 and intra_closes[-1] > intra_closes[-4]) else "down"

    # Support / resistance (simple: 14d high and low)
    support    = round(min(lows[-14:]),  4) if len(lows) >= 14 else None
    resistance = round(max(highs[-14:]), 4) if len(highs) >= 14 else None

    return {
        "product_id":     product_id,
        "price":          round(price, 4),
        "change_24h_pct": round(change_24h, 2),
        "change_7d_pct":  round(change_7d, 2),
        "ma7":            round(ma7,  4),
        "ma14":           round(ma14, 4),
        "ma30":           round(ma30, 4),
        "price_vs_ma14":  round((price - ma14) / ma14 * 100, 2) if ma14 else None,
        "atr14":          round(atr14, 4),
        "atr_pct":        round(atr14 / price * 100, 2) if price else None,
        "rsi14":          rsi14,
        "intraday_trend": intra_trend,
        "support_14d":    support,
        "resistance_14d": resistance,
    }


# ── Order Execution ────────────────────────────────────────────────────────────

def _place_order(product_id: str, side: str, usd_amount: float,
                 signal_type: str, setup_notes: str, market_regime: str) -> dict:
    if PAPER:
        fake_id = f"PAPER-CRYPTO-{product_id}-{int(time.time())}"
        price   = _get_price(product_id)
        qty     = usd_amount / price if price > 0 else 0
        print(f"  [PAPER] {side} ${usd_amount:.2f} of {product_id} @ ${price:.4f}")
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
    resp     = _cb_post("/api/v3/brokerage/orders", body)
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
        "description": "Price, RSI, MA, 4h trend, support/resistance for a crypto pair.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "e.g. BTC-USD"}
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "get_market_intelligence",
        "description": "Fear & Greed index, BTC dominance, funding rates, and on-chain signals. Call this every session — it's your market edge.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
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
        "description": "All-time win rate, PnL, breakdown by symbol/signal from every past crypto trade.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "buy_crypto",
        "description": "Buy a crypto pair with a specified USD amount (live market order).",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id":  {"type": "string"},
                "usd_amount":  {"type": "number", "description": "USD to spend"},
                "signal_type": {
                    "type": "string",
                    "enum": ["momentum", "mean_reversion", "breakout", "dip_buy", "fear_greed", "funding_squeeze"]
                },
                "reasoning":   {"type": "string", "description": "Full trade rationale including what intel drove this"},
            },
            "required": ["product_id", "usd_amount", "signal_type", "reasoning"],
        },
    },
    {
        "name": "sell_crypto",
        "description": "Sell a crypto pair (live market order, specify USD worth to sell).",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id":  {"type": "string"},
                "usd_amount":  {"type": "number"},
                "reasoning":   {"type": "string"},
                "exit_reason": {
                    "type": "string",
                    "enum": ["take_profit", "stop_loss", "regime_change", "rebalance", "fear_spike"]
                },
            },
            "required": ["product_id", "usd_amount", "reasoning", "exit_reason"],
        },
    },
    {
        "name": "skip_session",
        "description": "Skip this session. Use when market is unclear or all setups are weak.",
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
            return json.dumps(_full_market_data(inp["product_id"]))

        elif name == "get_market_intelligence":
            fg      = get_fear_and_greed()
            funding = get_funding_rates()
            onchain = get_onchain_signals()
            dom     = get_market_dominance()
            return json.dumps({
                "fear_and_greed":  fg,
                "funding_rates":   funding,
                "onchain_signals": onchain,
                "market_dominance": dom,
                "interpretation": (
                    "EXTREME FEAR = historically best buying opportunity. "
                    "EXTREME GREED = consider taking profits. "
                    "Negative funding = shorts crowded, potential squeeze up. "
                    "Positive funding >0.1% = longs crowded, reversal risk. "
                    "BTC dominance rising = rotate INTO BTC. "
                    "BTC dominance falling = alt season, rotate into alts."
                ),
            })

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
                return json.dumps({"error": "Insufficient USD (< $1)"})
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
        candles    = _get_daily_candles("BTC-USD", days=60)
        closes     = [float(c["close"]) for c in candles]
        if len(closes) < 7:
            return "unknown"
        change_24h = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        if change_24h < -8:
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


# ── System Prompt ─────────────────────────────────────────────────────────────

def _build_system(regime: str) -> str:
    notes = tdb.get_latest_strategy_notes(limit=2)
    stats = tdb.get_performance_stats()
    return f"""You are an aggressive crypto trading agent managing a live Coinbase portfolio.
Your goal is to MAXIMIZE DAILY PROFIT. Crypto runs 24/7 and you run every 4 hours.

## Current Market Regime: {regime}

## Trading Strategy
- ALWAYS call get_market_intelligence first — Fear & Greed + funding rates are your biggest edge
- Fear & Greed < 25 (extreme fear): BUY aggressively — these are the best entries historically
- Fear & Greed > 75 (extreme greed): Take profits, don't chase
- Funding rate > 0.1%: Longs very crowded → mean reversion short or avoid longs
- Funding rate < -0.05%: Shorts crowded → long squeeze setup, buy the dip
- BTC dominance rising: prioritize BTC/ETH over altcoins
- BTC dominance falling: alts outperform, diversify into SOL/DOGE/AVAX
- RSI < 35 on daily + 4h trend turning up = strong buy signal
- RSI > 70 on daily = reduce position, take partial profits

## Risk Rules (non-negotiable)
- Max 25% of portfolio per trade
- Keep at least 20% in USD at all times (dry powder for dips)
- Daily loss limit: if portfolio drops 15% today, stop all trading
- If BTC drops 8%+ in 24h, skip session (already checked before you run)

## Performance Stats
{json.dumps(stats, indent=2) if isinstance(stats, dict) else "No closed trades yet."}

## Strategy Notes from Weekly Self-Review
{chr(10).join(n['content'][:600] for n in notes) if notes else "No reviews yet — still learning."}

Be decisive. When you see a clear setup, take it. When you don't, skip. Don't over-trade.
"""


# ── Main Session ──────────────────────────────────────────────────────────────

def run_crypto_session():
    if not KEY_NAME or not _KEY_B64:
        print("[crypto] Coinbase keys not set — skipping")
        return

    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"\n[crypto] Session start {now_str} | LIVE={not PAPER}")

    regime = _detect_regime()
    print(f"[crypto] Regime: {regime}")

    if regime == "crash_risk":
        print("[crypto] BTC crash detected — skipping to preserve capital")
        return

    messages = [{
        "role": "user",
        "content": (
            f"Time: {now_str}\n"
            f"Regime: {regime} | Mode: {'PAPER' if PAPER else 'LIVE — real money'}\n"
            f"Watchlist: {', '.join(WATCHLIST)}\n\n"
            "Run your analysis and trade if there's a clear opportunity:\n"
            "1. get_market_intelligence — always start here\n"
            "2. get_portfolio — see your balance\n"
            "3. get_market_data for coins that look interesting based on intel\n"
            "4. get_performance_stats to know what setups have worked\n"
            "5. Execute trades or skip_session\n"
            "Goal: maximize profit today. Be aggressive on clear setups."
        ),
    }]

    for _turn in range(16):
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
                    print(f"[crypto] {block.text[:500]}")
            break

        if resp.stop_reason != "tool_use":
            break

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        messages.append({"role": "assistant", "content": resp.content})

        results = []
        for tu in tool_uses:
            print(f"  → {tu.name}({json.dumps(tu.input)[:80]})")
            out = _handle_tool(tu.name, tu.input, regime)
            print(f"    ← {out[:200]}")
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})

        messages.append({"role": "user", "content": results})

    print(f"[crypto] Session complete")


if __name__ == "__main__":
    run_crypto_session()
