"""
Crypto trading agent — runs every 2 hours via scheduler, 24/7.
Watches 10 coins on Coinbase Advanced Trade.
Logs all trades to trading.db alongside the stock bot's journal.

Intelligence sources:
  - Fear & Greed Index (alternative.me)
  - OKX funding rates (crowded position detector)
  - CoinGecko trending coins + market movers + volume spikes
  - BTC dominance + alt season detection
  - Multi-timeframe: 6h + daily candles
  - On-chain: BTC network activity
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

WATCHLIST = [
    "BTC-USD", "ETH-USD", "SOL-USD", "XRP-USD", "ADA-USD",
    "DOGE-USD", "AVAX-USD", "LINK-USD", "DOT-USD", "UNI-USD",
]

# CoinGecko symbol → Coinbase product_id mapping
CG_TO_CB = {
    "btc": "BTC-USD", "eth": "ETH-USD", "sol": "SOL-USD",
    "xrp": "XRP-USD", "ada": "ADA-USD", "doge": "DOGE-USD",
    "avax": "AVAX-USD", "link": "LINK-USD", "dot": "DOT-USD", "uni": "UNI-USD",
}

MAX_POSITION_PCT = 0.25   # up to 25% of portfolio per trade
MIN_USD_RESERVE  = 0.20   # always keep 20% in USD
CRASH_THRESHOLD  = 0.08   # skip session if BTC drops 8%+ in 24h
DAILY_LOSS_LIMIT = 0.15   # stop trading if portfolio drops 15% in a session

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
    try:
        r    = httpx.get("https://api.alternative.me/fng/?limit=3", timeout=10)
        data = r.json().get("data", [])
        if len(data) >= 2:
            today, yesterday = data[0], data[1]
            return {
                "value":          int(today["value"]),
                "classification": today["value_classification"],
                "yesterday":      int(yesterday["value"]),
                "trend":          "rising" if int(today["value"]) > int(yesterday["value"]) else "falling",
                "signal": (
                    "STRONG BUY — extreme fear, historically best entry"  if int(today["value"]) < 20 else
                    "BUY — fear zone"                                      if int(today["value"]) < 40 else
                    "NEUTRAL"                                              if int(today["value"]) < 60 else
                    "CAUTION — greed zone, consider taking profits"        if int(today["value"]) < 80 else
                    "SELL — extreme greed, markets historically top here"
                ),
            }
    except Exception as e:
        return {"error": str(e)}
    return {}


def get_funding_rates() -> dict:
    pairs = {"BTC-USD-SWAP": "BTC-USD", "ETH-USD-SWAP": "ETH-USD",
             "SOL-USD-SWAP": "SOL-USD", "XRP-USD-SWAP": "XRP-USD"}
    rates = {}
    for inst_id, label in pairs.items():
        try:
            r    = httpx.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}", timeout=10)
            data = r.json().get("data", [{}])[0]
            rate = float(data.get("fundingRate", 0)) * 100
            rates[label] = {
                "rate_pct": round(rate, 4),
                "signal": (
                    "BEARISH — longs very crowded, reversal risk" if rate > 0.1 else
                    "bearish"                                      if rate > 0.05 else
                    "BULLISH — shorts crowded, squeeze likely"     if rate < -0.05 else
                    "neutral"
                ),
            }
        except Exception as e:
            rates[label] = {"error": str(e)}
    return rates


def get_market_dominance() -> dict:
    try:
        r    = httpx.get("https://api.coingecko.com/api/v3/global",
                         headers={"Accept": "application/json"}, timeout=10)
        data = r.json().get("data", {})
        dom  = data.get("market_cap_percentage", {})
        return {
            "btc_dominance": round(dom.get("btc", 0), 1),
            "eth_dominance": round(dom.get("eth", 0), 1),
            "total_mcap":    data.get("total_market_cap", {}).get("usd", 0),
            "mcap_24h_pct":  round(data.get("market_cap_change_percentage_24h_usd", 0), 2),
            "signal": (
                "ALT SEASON — BTC dominance low, alts outperforming" if dom.get("btc", 50) < 42 else
                "BTC SEASON — rotate into BTC/ETH"                   if dom.get("btc", 50) > 58 else
                "balanced"
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def get_trending_and_sentiment() -> dict:
    """CoinGecko: trending searches + top movers + volume spikes = crowd sentiment."""
    result = {}
    try:
        # Trending search coins (what people are searching right now)
        r = httpx.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
        if r.status_code == 200:
            trending = r.json().get("coins", [])
            result["trending_searches"] = [
                {"symbol": c["item"]["symbol"].upper(),
                 "name":   c["item"]["name"],
                 "rank":   c["item"]["market_cap_rank"]}
                for c in trending[:7]
            ]
    except Exception as e:
        result["trending_error"] = str(e)

    try:
        # Top 20 coins by market cap with 24h/7d changes + volume
        r2 = httpx.get(
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&order=market_cap_desc&per_page=20&page=1"
            "&sparkline=false&price_change_percentage=24h,7d",
            timeout=10,
        )
        if r2.status_code == 200:
            coins = r2.json()
            # Find volume spikes (volume > 2x normal using 24h volume vs mcap ratio)
            movers = []
            for c in coins:
                sym = c["symbol"].upper()
                change_24h = c.get("price_change_percentage_24h") or 0
                change_7d  = c.get("price_change_percentage_7d_in_currency") or 0
                vol        = c.get("total_volume") or 0
                mcap       = c.get("market_cap") or 1
                vol_ratio  = vol / mcap   # >0.3 = high volume day

                cb_pair = CG_TO_CB.get(c["symbol"].lower())
                if cb_pair:
                    movers.append({
                        "symbol":     cb_pair,
                        "price":      c["current_price"],
                        "change_24h": round(change_24h, 2),
                        "change_7d":  round(change_7d, 2),
                        "vol_ratio":  round(vol_ratio, 3),
                        "volume_signal": (
                            "HIGH VOLUME — strong conviction move" if vol_ratio > 0.3 else
                            "normal"
                        ),
                    })

            # Sort by absolute 24h move — biggest movers first
            movers.sort(key=lambda x: abs(x["change_24h"]), reverse=True)
            result["top_movers"]    = movers[:8]
            result["biggest_drop"]  = min(movers, key=lambda x: x["change_24h"]) if movers else None
            result["biggest_gainer"]= max(movers, key=lambda x: x["change_24h"]) if movers else None
    except Exception as e:
        result["movers_error"] = str(e)

    result["interpretation"] = (
        "Trending coins = crowd interest, often precede pumps. "
        "High vol_ratio (>0.3) = strong institutional move. "
        "Coins down 5-15% on high volume but NOT trending = potential dip buy. "
        "Coins up 10%+ already = usually too late, wait for pullback."
    )
    return result


def get_onchain_signals() -> dict:
    try:
        r = httpx.get("https://api.blockchain.info/stats", timeout=10)
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}"}
        stats = r.json()
        mempool_mb = round(stats.get("mempool_size", 0) / 1e6, 2)
        return {
            "btc_mempool_mb":   mempool_mb,
            "btc_difficulty":   stats.get("difficulty", 0),
            "network_activity": "high" if mempool_mb > 20 else "normal",
        }
    except Exception as e:
        return {"error": str(e)}


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


def _get_candles(product_id: str, granularity: str = "ONE_DAY", count: int = 30) -> list:
    secs_per = {"ONE_DAY": 86400, "SIX_HOUR": 21600, "TWO_HOUR": 7200, "ONE_HOUR": 3600}
    end      = int(time.time())
    start    = end - count * secs_per.get(granularity, 86400)
    try:
        data    = _cb_get(f"/api/v3/brokerage/products/{product_id}/candles",
                          {"start": str(start), "end": str(end), "granularity": granularity})
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
    return {"usd": round(usd, 2), "holdings": holdings}


def _analyze_coin(product_id: str) -> dict:
    daily = _get_candles(product_id, "ONE_DAY",  30)
    intra = _get_candles(product_id, "TWO_HOUR", 24)
    if not daily:
        return {"error": f"No data for {product_id}"}

    closes = [float(c["close"]) for c in daily]
    highs  = [float(c["high"])  for c in daily]
    lows   = [float(c["low"])   for c in daily]
    vols   = [float(c.get("volume", 0)) for c in daily]
    price  = _get_price(product_id)

    def ma(n):
        w = closes[-n:]
        return sum(w) / len(w) if w else price

    ma7, ma14, ma30 = ma(7), ma(14), ma(30)

    # ATR14
    trs   = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
             for i in range(1, len(daily))]
    atr14 = sum(trs[-14:]) / min(14, len(trs)) if trs else price * 0.05

    # RSI14
    diffs  = [closes[i] - closes[i-1] for i in range(max(1, len(closes)-15), len(closes))]
    gains  = [max(0, d) for d in diffs]
    losses = [max(0, -d) for d in diffs]
    ag, al = sum(gains)/14, sum(losses)/14
    rsi14  = round(100 - (100/(1 + ag/al)), 1) if al > 0 else 100

    # Volume spike (today vs 20d avg)
    avg_vol  = sum(vols[-20:]) / min(20, len(vols)) if vols else 1
    vol_spike = round(vols[-1] / avg_vol, 2) if avg_vol > 0 else 1

    # 2h trend
    i_closes = [float(c["close"]) for c in intra]
    trend_2h = "up" if (len(i_closes) >= 4 and i_closes[-1] > i_closes[-4]) else "down"

    change_24h = round((closes[-1] - closes[-2]) / closes[-2] * 100, 2) if len(closes) >= 2 else 0
    change_7d  = round((closes[-1] - closes[-7]) / closes[-7] * 100, 2) if len(closes) >= 7 else 0

    # Simple setup classification
    setup = "none"
    if rsi14 < 32 and trend_2h == "up":
        setup = "OVERSOLD_DIP_BUY — RSI<32, 2h turning up"
    elif rsi14 < 40 and price < ma14 * 0.95:
        setup = "DIP_BUY — oversold below MA14"
    elif price > ma7 > ma14 and vol_spike > 1.5 and trend_2h == "up":
        setup = "MOMENTUM_BREAKOUT — above MAs, high volume, 2h up"
    elif rsi14 > 70:
        setup = "OVERBOUGHT — consider taking profits if holding"

    return {
        "product_id":     product_id,
        "price":          round(price, 6),
        "change_24h_pct": change_24h,
        "change_7d_pct":  change_7d,
        "rsi14":          rsi14,
        "ma7":            round(ma7, 4),
        "ma14":           round(ma14, 4),
        "ma30":           round(ma30, 4),
        "price_vs_ma14":  round((price - ma14) / ma14 * 100, 2) if ma14 else None,
        "atr14_pct":      round(atr14 / price * 100, 2) if price else None,
        "vol_spike":      vol_spike,
        "trend_2h":       trend_2h,
        "support_14d":    round(min(lows[-14:]), 6) if len(lows) >= 14 else None,
        "resistance_14d": round(max(highs[-14:]), 6) if len(highs) >= 14 else None,
        "setup":          setup,
    }


# ── Order Execution ────────────────────────────────────────────────────────────

def _place_order(product_id: str, side: str, usd_amount: float,
                 signal_type: str, setup_notes: str, market_regime: str) -> dict:
    price = _get_price(product_id)
    qty   = usd_amount / price if price > 0 else 0

    if PAPER:
        fake_id = f"PAPER-{product_id}-{int(time.time())}"
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
    print(f"  [LIVE] {side} ${usd_amount:.2f} of {product_id} @ ${price:.4f} → order {order_id}")
    tdb.open_trade(order_id=order_id, symbol=product_id, side=side.lower(),
                   qty=qty, entry_price=price, signal_type=signal_type,
                   setup_notes=setup_notes, market_regime=market_regime)
    return {"order_id": order_id, "price": price, "qty": qty}


# ── Tools ─────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "get_portfolio",
        "description": "Current USD balance and all crypto holdings.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_market_intelligence",
        "description": (
            "Fear & Greed index, funding rates, BTC dominance, on-chain signals, "
            "trending coins, top movers, volume spikes. Call this FIRST every session."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "analyze_coin",
        "description": "Deep analysis of a single coin: RSI, MAs, volume spike, 2h trend, support/resistance, setup classification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id": {"type": "string", "description": "e.g. BTC-USD, SOL-USD, XRP-USD"}
            },
            "required": ["product_id"],
        },
    },
    {
        "name": "get_historical_context",
        "description": "6-year historical stats for a coin from the trading DB.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "get_performance_stats",
        "description": "Win rate, PnL, and breakdown by symbol/signal from all past trades.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "buy_crypto",
        "description": "Execute a live BUY market order on Coinbase.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id":  {"type": "string"},
                "usd_amount":  {"type": "number"},
                "signal_type": {
                    "type": "string",
                    "enum": ["momentum", "mean_reversion", "breakout", "dip_buy",
                             "fear_greed", "funding_squeeze", "volume_spike", "trending"]
                },
                "reasoning": {"type": "string"},
            },
            "required": ["product_id", "usd_amount", "signal_type", "reasoning"],
        },
    },
    {
        "name": "sell_crypto",
        "description": "Execute a live SELL market order on Coinbase.",
        "input_schema": {
            "type": "object",
            "properties": {
                "product_id":  {"type": "string"},
                "usd_amount":  {"type": "number"},
                "reasoning":   {"type": "string"},
                "exit_reason": {
                    "type": "string",
                    "enum": ["take_profit", "stop_loss", "regime_change",
                             "rebalance", "fear_spike", "overbought"]
                },
            },
            "required": ["product_id", "usd_amount", "reasoning", "exit_reason"],
        },
    },
    {
        "name": "skip_session",
        "description": "Skip this session when no clear setup exists.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]


def _handle_tool(name: str, inp: dict, regime: str) -> str:
    try:
        if name == "get_portfolio":
            return json.dumps(_get_portfolio())

        elif name == "get_market_intelligence":
            fg      = get_fear_and_greed()
            funding = get_funding_rates()
            dom     = get_market_dominance()
            trend   = get_trending_and_sentiment()
            onchain = get_onchain_signals()
            return json.dumps({
                "fear_and_greed":   fg,
                "funding_rates":    funding,
                "dominance":        dom,
                "sentiment":        trend,
                "onchain":          onchain,
            })

        elif name == "analyze_coin":
            return json.dumps(_analyze_coin(inp["product_id"]))

        elif name == "get_historical_context":
            return json.dumps(tdb.get_historical_stats(inp["symbol"]))

        elif name == "get_performance_stats":
            return json.dumps(tdb.get_performance_stats())

        elif name == "buy_crypto":
            pid  = inp["product_id"]
            usd  = float(inp["usd_amount"])
            port = _get_portfolio()
            total = port["usd"]
            # reserve MIN_USD_RESERVE of current balance
            max_trade = total * MAX_POSITION_PCT
            min_reserve = (total + sum(
                _get_price(f"{cur}-USD") * qty
                for cur, qty in port.get("holdings", {}).items()
            )) * MIN_USD_RESERVE
            available = max(0, total - min_reserve)
            usd = min(usd, max_trade, available)
            if usd < 1.0:
                return json.dumps({"error": "Insufficient free USD (< $1 after reserve)"})
            result = _place_order(pid, "BUY", usd, inp["signal_type"],
                                  inp["reasoning"], regime)
            return json.dumps(result)

        elif name == "sell_crypto":
            result = _place_order(inp["product_id"], "SELL", float(inp["usd_amount"]),
                                  inp.get("exit_reason", "exit"), inp["reasoning"], regime)
            return json.dumps(result)

        elif name == "skip_session":
            print(f"  [SKIP] {inp['reason']}")
            return json.dumps({"status": "skipped", "reason": inp["reason"]})

    except Exception as e:
        return json.dumps({"error": str(e)})

    return json.dumps({"error": f"Unknown tool: {name}"})


# ── Regime ────────────────────────────────────────────────────────────────────

def _detect_regime() -> str:
    try:
        candles    = _get_candles("BTC-USD", "ONE_DAY", 60)
        closes     = [float(c["close"]) for c in candles]
        if len(closes) < 7:
            return "unknown"
        change_24h = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
        if change_24h < -CRASH_THRESHOLD * 100:
            return "crash_risk"
        ma7  = sum(closes[-7:]) / 7
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
Goal: MAXIMIZE DAILY PROFIT. You run every 2 hours, 24/7 across 10 coins.

## Market Regime: {regime}
## Watchlist: {', '.join(WATCHLIST)}

## Session Playbook
1. ALWAYS call get_market_intelligence first — this is your edge
2. Use trending_searches + top_movers to spot which coins have momentum RIGHT NOW
3. For any promising coin, call analyze_coin to get RSI, volume spike, 2h trend
4. Cross-reference: coin trending + oversold RSI + volume spike = highest conviction buy
5. Execute 1-3 trades max per session — quality over quantity

## Signal Interpretation
- Fear & Greed < 20: Extreme fear = STRONG BUY signal (historically best entries)
- Fear & Greed > 80: Extreme greed = take profits, don't open new longs
- Funding rate > 0.1%: Longs crowded → avoid new longs, look for short squeeze on alts
- Funding rate < -0.05%: Shorts crowded → long squeeze incoming, buy the dip
- Coin in trending_searches + RSI < 40: High probability setup
- vol_spike > 2.0: Institutional buying, strong signal
- setup = "OVERSOLD_DIP_BUY": Highest conviction — RSI<32 with 2h reversal
- BTC dominance falling: Rotate into alts (SOL, XRP, LINK, ADA)
- BTC dominance rising: Stick to BTC/ETH

## Tightened Risk Rules
- Max 25% of portfolio per trade
- Always keep 20% in USD (dry powder)
- Stop loss: 3% below entry (mentally — close losing positions next session)
- Take profit: 8% gain target
- If BTC dropped 8%+ in last 24h → already blocked, won't reach you
- Daily loss limit: 15% of portfolio → stop all trading

## Performance Stats
{json.dumps(stats, indent=2) if isinstance(stats, dict) else "No closed trades yet."}

## Strategy Notes from Weekly Self-Review
{chr(10).join(n['content'][:500] for n in notes) if notes else "Still learning — no reviews yet."}

Be aggressive on clear setups. Skip when uncertain. Every trade needs a reason.
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
        print("[crypto] BTC crash detected — preserving capital, skipping")
        return

    messages = [{
        "role": "user",
        "content": (
            f"Time: {now_str} | Regime: {regime} | {'LIVE' if not PAPER else 'PAPER'}\n"
            f"Watchlist: {', '.join(WATCHLIST)}\n\n"
            "Run your session:\n"
            "1. get_market_intelligence → identify best opportunities\n"
            "2. analyze_coin on the most promising ones\n"
            "3. get_portfolio → check available cash\n"
            "4. Execute trades or skip_session\n"
            "Maximize profit. Be decisive."
        ),
    }]

    for _turn in range(20):
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
                    print(f"[crypto] {block.text[:600]}")
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
