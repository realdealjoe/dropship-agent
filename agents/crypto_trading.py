"""
Crypto trading agent — runs every 2 hours, 24/7.
Watches 10 coins. Holds positions across sessions when thesis is intact.
Scans Reddit, CoinDesk, CoinTelegraph, Decrypt, Google News every session.
"""
import os, time, json, secrets, base64, re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx, jwt
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
CG_TO_CB = {
    "btc":"BTC-USD","eth":"ETH-USD","sol":"SOL-USD","xrp":"XRP-USD",
    "ada":"ADA-USD","doge":"DOGE-USD","avax":"AVAX-USD","link":"LINK-USD",
    "dot":"DOT-USD","uni":"UNI-USD",
}
COIN_KEYWORDS = {
    "BTC-USD":  ["bitcoin","btc"],
    "ETH-USD":  ["ethereum","eth"],
    "SOL-USD":  ["solana","sol"],
    "XRP-USD":  ["xrp","ripple"],
    "ADA-USD":  ["cardano","ada"],
    "DOGE-USD": ["dogecoin","doge"],
    "AVAX-USD": ["avalanche","avax"],
    "LINK-USD": ["chainlink","link"],
    "DOT-USD":  ["polkadot","dot"],
    "UNI-USD":  ["uniswap","uni"],
}
BULL_WORDS = {"surge","rally","breakout","bullish","bull","moon","pump","adoption",
              "partnership","upgrade","launch","etf","institutional","accumulate",
              "ath","all-time high","approve","listing","buy","bounce","recovery",
              "record","growth","mainstream","integrate","legal","sec approved"}
BEAR_WORDS = {"crash","dump","bearish","bear","hack","ban","regulation","fraud",
              "bankruptcy","liquidate","drop","plunge","sell","fear","panic","scam",
              "illegal","lawsuit","fine","exploit","vulnerability","delist","warning"}

MAX_POSITION_PCT = 0.25
MIN_USD_RESERVE  = 0.20
CRASH_THRESHOLD  = 0.08
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── JWT / Coinbase ────────────────────────────────────────────────────────────

def _make_jwt(method: str, path: str) -> str:
    raw = base64.b64decode(_KEY_B64)
    pk  = Ed25519PrivateKey.from_private_bytes(raw[:32])
    payload = {"sub":KEY_NAME,"iss":"cdp","nbf":int(time.time()),
               "exp":int(time.time())+120,"uri":f"{method} api.coinbase.com{path}"}
    return jwt.encode(payload, pk, algorithm="EdDSA",
                      headers={"kid":KEY_NAME,"nonce":secrets.token_hex(10)})

def _cb_get(path, params=None):
    tok = _make_jwt("GET", path)
    r   = httpx.get(f"{BASE_URL}{path}", params=params,
                    headers={"Authorization":f"Bearer {tok}"}, timeout=30)
    r.raise_for_status(); return r.json()

def _cb_post(path, body):
    tok = _make_jwt("POST", path)
    r   = httpx.post(f"{BASE_URL}{path}", json=body,
                     headers={"Authorization":f"Bearer {tok}","Content-Type":"application/json"},
                     timeout=30)
    r.raise_for_status(); return r.json()


# ── Internet Scanner ──────────────────────────────────────────────────────────

def _parse_rss(url: str, limit: int = 15) -> list[str]:
    """Fetch RSS feed, return list of headline strings."""
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=10, follow_redirects=True)
        if r.status_code != 200:
            return []
        root = ET.fromstring(r.text)
        # Try standard RSS <item><title>
        titles = [t.text for t in root.findall(".//item/title") if t.text]
        # Fallback: Atom <entry><title>
        if not titles:
            ns = {"a": "http://www.w3.org/2005/Atom"}
            titles = [t.text for t in root.findall("a:entry/a:title", ns) if t.text]
        return titles[:limit]
    except Exception:
        return []


def _score_headline(text: str, keywords: list[str]) -> tuple[int, int]:
    """Returns (bull_score, bear_score) for a headline given coin keywords."""
    low = text.lower()
    if not any(kw in low for kw in keywords):
        return 0, 0
    bull = sum(1 for w in BULL_WORDS if w in low)
    bear = sum(1 for w in BEAR_WORDS if w in low)
    return bull, bear


def scan_internet(product_id: str = None) -> dict:
    """
    Pull headlines from 8 sources. Score each coin's sentiment.
    If product_id given, also do a targeted Google News search for that coin.
    """
    SOURCES = [
        ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("CoinTelegraph", "https://cointelegraph.com/rss"),
        ("Decrypt",       "https://decrypt.co/feed"),
        ("BitcoinMag",    "https://bitcoinmagazine.com/feed"),
        ("Google-Crypto", "https://news.google.com/rss/search?q=bitcoin+ethereum+solana+crypto&hl=en-US&gl=US&ceid=US:en"),
        ("Reddit-Crypto", "https://www.reddit.com/r/CryptoCurrency/hot/.rss?limit=25"),
        ("Reddit-Bitcoin","https://www.reddit.com/r/Bitcoin/hot/.rss?limit=15"),
        ("Reddit-SSB",    "https://www.reddit.com/r/SatoshiStreetBets/hot/.rss?limit=15"),
    ]

    if product_id:
        coin_name = COIN_KEYWORDS.get(product_id, [product_id.split("-")[0].lower()])[0]
        SOURCES.append(
            (f"Google-{product_id}",
             f"https://news.google.com/rss/search?q={coin_name}+crypto+price&hl=en-US&gl=US&ceid=US:en")
        )
        SOURCES.append(
            (f"Reddit-{coin_name}",
             f"https://www.reddit.com/r/{coin_name}/hot/.rss?limit=15")
        )

    all_headlines = []
    by_source     = {}
    for src_name, url in SOURCES:
        headlines = _parse_rss(url)
        by_source[src_name] = len(headlines)
        all_headlines.extend(headlines)

    # Score each coin
    coin_sentiment = {}
    for pid, keywords in COIN_KEYWORDS.items():
        relevant = [h for h in all_headlines if any(k in h.lower() for k in keywords)]
        if not relevant:
            continue
        bull_total = bear_total = 0
        for h in relevant:
            b, br = _score_headline(h, keywords)
            bull_total += b; bear_total += br
        net   = bull_total - bear_total
        score = "VERY_BULLISH" if net >= 4 else "BULLISH" if net >= 2 else \
                "VERY_BEARISH" if net <= -3 else "BEARISH" if net <= -1 else "NEUTRAL"
        coin_sentiment[pid] = {
            "mentions":    len(relevant),
            "bull_signals": bull_total,
            "bear_signals": bear_total,
            "net_score":    net,
            "sentiment":   score,
            "top_headlines": relevant[:4],
        }

    # Find hottest coin (most mentions + bullish)
    hottest = sorted(
        [(pid, d) for pid, d in coin_sentiment.items() if d["net_score"] > 0],
        key=lambda x: (x[1]["net_score"], x[1]["mentions"]),
        reverse=True
    )

    return {
        "total_headlines_scanned": len(all_headlines),
        "sources_hit":             by_source,
        "coin_sentiment":          coin_sentiment,
        "hottest_coins":           [pid for pid, _ in hottest[:4]],
        "summary": (
            f"Scanned {len(all_headlines)} headlines across {len(SOURCES)} sources. "
            f"Hottest: {', '.join(pid for pid,_ in hottest[:3]) or 'nothing bullish'}"
        ),
    }


# ── Market Intelligence ───────────────────────────────────────────────────────

def get_fear_and_greed() -> dict:
    try:
        r    = httpx.get("https://api.alternative.me/fng/?limit=2", timeout=10)
        data = r.json().get("data", [])
        if len(data) >= 2:
            t, y = data[0], data[1]
            v    = int(t["value"])
            return {
                "value": v, "classification": t["value_classification"],
                "yesterday": int(y["value"]),
                "trend": "rising" if v > int(y["value"]) else "falling",
                "signal": (
                    "STRONG BUY — extreme fear = historically best entry" if v < 20 else
                    "BUY — fear zone"                                      if v < 40 else
                    "NEUTRAL"                                              if v < 60 else
                    "CAUTION — greed, consider trimming"                   if v < 80 else
                    "SELL — extreme greed, tops form here"
                ),
            }
    except Exception as e:
        return {"error": str(e)}
    return {}


def get_funding_rates() -> dict:
    pairs = {"BTC-USD-SWAP":"BTC-USD","ETH-USD-SWAP":"ETH-USD",
             "SOL-USD-SWAP":"SOL-USD","XRP-USD-SWAP":"XRP-USD"}
    rates = {}
    for inst_id, label in pairs.items():
        try:
            r    = httpx.get(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}", timeout=10)
            data = r.json().get("data",[{}])[0]
            rate = float(data.get("fundingRate",0)) * 100
            rates[label] = {
                "rate_pct": round(rate,4),
                "signal": (
                    "BEARISH — longs very crowded" if rate > 0.1 else
                    "bearish"                      if rate > 0.05 else
                    "BULLISH — short squeeze setup" if rate < -0.05 else
                    "neutral"
                ),
            }
        except Exception as e:
            rates[label] = {"error": str(e)}
    return rates


def get_market_dominance() -> dict:
    try:
        r   = httpx.get("https://api.coingecko.com/api/v3/global",
                        headers={"Accept":"application/json"}, timeout=10)
        d   = r.json().get("data",{})
        dom = d.get("market_cap_percentage",{})
        return {
            "btc_dominance": round(dom.get("btc",0),1),
            "eth_dominance": round(dom.get("eth",0),1),
            "mcap_24h_pct":  round(d.get("market_cap_change_percentage_24h_usd",0),2),
            "signal": (
                "ALT SEASON — rotate into alts" if dom.get("btc",50) < 42 else
                "BTC SEASON — stick to BTC/ETH" if dom.get("btc",50) > 58 else
                "balanced"
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def get_trending_coins() -> dict:
    result = {}
    try:
        r = httpx.get("https://api.coingecko.com/api/v3/search/trending", timeout=10)
        if r.status_code == 200:
            coins = r.json().get("coins",[])
            result["trending"] = [
                {"symbol": c["item"]["symbol"].upper(), "name": c["item"]["name"],
                 "rank": c["item"]["market_cap_rank"]}
                for c in coins[:8]
            ]
    except Exception as e:
        result["error"] = str(e)
    try:
        r2 = httpx.get(
            "https://api.coingecko.com/api/v3/coins/markets"
            "?vs_currency=usd&order=market_cap_desc&per_page=20&page=1"
            "&sparkline=false&price_change_percentage=24h,7d",
            timeout=10
        )
        if r2.status_code == 200:
            movers = []
            for c in r2.json():
                cb_pair = CG_TO_CB.get(c["symbol"].lower())
                if not cb_pair: continue
                ch24 = c.get("price_change_percentage_24h") or 0
                vol  = c.get("total_volume") or 0
                mcap = c.get("market_cap") or 1
                movers.append({
                    "symbol": cb_pair, "price": c["current_price"],
                    "change_24h": round(ch24,2),
                    "vol_ratio": round(vol/mcap,3),
                })
            movers.sort(key=lambda x: abs(x["change_24h"]), reverse=True)
            result["top_movers"] = movers[:8]
    except Exception as e:
        result["movers_error"] = str(e)
    return result


# ── Position Management ───────────────────────────────────────────────────────

def get_open_positions() -> dict:
    """Check all open trades in journal + current prices = live P&L."""
    conn  = tdb.get_conn()
    rows  = conn.execute(
        "SELECT * FROM trade_journal WHERE won IS NULL ORDER BY entry_time DESC"
    ).fetchall()
    conn.close()

    positions = []
    for row in rows:
        r       = dict(row)
        pid     = r["symbol"]
        curr    = _get_price(pid)
        entry   = r["entry_price"] or 0
        qty     = r["qty"] or 0
        if entry > 0:
            pnl_pct = (curr - entry) / entry * 100
            pnl_usd = (curr - entry) * qty
        else:
            pnl_pct = pnl_usd = 0
        positions.append({
            "order_id":   r["order_id"],
            "symbol":     pid,
            "side":       r["side"],
            "qty":        round(qty, 8),
            "entry_price":round(entry, 6),
            "curr_price": round(curr, 6),
            "pnl_pct":   round(pnl_pct, 2),
            "pnl_usd":   round(pnl_usd, 4),
            "signal_type":r["signal_type"],
            "entry_time": r["entry_time"],
            "status": (
                "TAKE_PROFIT ✅ (>8%)" if pnl_pct >= 8 else
                "STOP_LOSS ❌ (<-3%)"  if pnl_pct <= -3 else
                f"HOLD ({pnl_pct:+.1f}%)"
            ),
        })
    return {"open_positions": positions, "count": len(positions)}


# ── Coin Analysis ─────────────────────────────────────────────────────────────

def _get_price(product_id: str) -> float:
    try:
        data = _cb_get("/api/v3/brokerage/best_bid_ask", {"product_ids":[product_id]})
        pb   = data.get("pricebooks",[{}])[0]
        bid  = float(pb["bids"][0]["price"]) if pb.get("bids") else 0
        ask  = float(pb["asks"][0]["price"]) if pb.get("asks") else 0
        return (bid + ask) / 2
    except Exception:
        return 0.0


def _get_candles(product_id, granularity="ONE_DAY", count=30):
    secs = {"ONE_DAY":86400,"SIX_HOUR":21600,"TWO_HOUR":7200,"ONE_HOUR":3600}
    end  = int(time.time()); start = end - count * secs.get(granularity, 86400)
    try:
        data = _cb_get(f"/api/v3/brokerage/products/{product_id}/candles",
                       {"start":str(start),"end":str(end),"granularity":granularity})
        c    = data.get("candles",[]); c.sort(key=lambda x: x["start"]); return c
    except Exception:
        return []


def _get_portfolio() -> dict:
    data = _cb_get("/api/v3/brokerage/accounts")
    usd  = 0.0; holdings = {}
    for acc in data.get("accounts",[]):
        cur   = acc.get("currency","")
        avail = float(acc.get("available_balance",{}).get("value",0))
        if cur == "USD": usd = avail
        elif avail > 0:  holdings[cur] = avail
    return {"usd": round(usd,2), "holdings": holdings}


def analyze_coin(product_id: str) -> dict:
    daily  = _get_candles(product_id,"ONE_DAY",30)
    intra  = _get_candles(product_id,"TWO_HOUR",24)
    if not daily: return {"error": f"No data for {product_id}"}

    closes = [float(c["close"]) for c in daily]
    highs  = [float(c["high"]) for c in daily]
    lows   = [float(c["low"]) for c in daily]
    vols   = [float(c.get("volume",0)) for c in daily]
    price  = _get_price(product_id)

    def ma(n): w=closes[-n:]; return sum(w)/len(w) if w else price
    ma7,ma14,ma30 = ma(7),ma(14),ma(30)

    trs   = [max(highs[i]-lows[i],abs(highs[i]-closes[i-1]),abs(lows[i]-closes[i-1]))
             for i in range(1,len(daily))]
    atr14 = sum(trs[-14:])/min(14,len(trs)) if trs else price*0.05

    diffs  = [closes[i]-closes[i-1] for i in range(max(1,len(closes)-15),len(closes))]
    gains  = [max(0,d) for d in diffs]; losses=[max(0,-d) for d in diffs]
    ag,al  = sum(gains)/14, sum(losses)/14
    rsi14  = round(100-(100/(1+ag/al)),1) if al>0 else 100

    avg_vol   = sum(vols[-20:])/min(20,len(vols)) if vols else 1
    vol_spike = round(vols[-1]/avg_vol,2) if avg_vol>0 else 1

    i_closes  = [float(c["close"]) for c in intra]
    trend_2h  = "up" if (len(i_closes)>=4 and i_closes[-1]>i_closes[-4]) else "down"

    ch24 = round((closes[-1]-closes[-2])/closes[-2]*100,2) if len(closes)>=2 else 0
    ch7  = round((closes[-1]-closes[-7])/closes[-7]*100,2) if len(closes)>=7 else 0

    setup = "none"
    if rsi14 < 30 and trend_2h=="up":           setup = "STRONG_DIP_BUY — RSI<30, reversing"
    elif rsi14 < 38 and price < ma14*0.94:       setup = "DIP_BUY — oversold below MA14"
    elif price>ma7>ma14 and vol_spike>1.8 and trend_2h=="up": setup = "MOMENTUM_BREAKOUT"
    elif rsi14 > 72:                             setup = "OVERBOUGHT — trim position"

    return {
        "product_id":product_id, "price":round(price,6),
        "change_24h":ch24,"change_7d":ch7,
        "rsi14":rsi14,"ma7":round(ma7,4),"ma14":round(ma14,4),"ma30":round(ma30,4),
        "price_vs_ma14":round((price-ma14)/ma14*100,2) if ma14 else None,
        "atr14_pct":round(atr14/price*100,2) if price else None,
        "vol_spike":vol_spike,"trend_2h":trend_2h,
        "support_14d":round(min(lows[-14:]),6) if len(lows)>=14 else None,
        "resistance_14d":round(max(highs[-14:]),6) if len(highs)>=14 else None,
        "setup":setup,
    }


# ── Orders ────────────────────────────────────────────────────────────────────

def _place_order(product_id, side, usd_amount, signal_type, setup_notes, regime):
    price = _get_price(product_id)
    qty   = usd_amount / price if price > 0 else 0
    if PAPER:
        fake_id = f"PAPER-{product_id}-{int(time.time())}"
        print(f"  [PAPER] {side} ${usd_amount:.2f} {product_id} @ ${price:.4f}")
        tdb.open_trade(fake_id,product_id,side.lower(),qty,price,signal_type,setup_notes,regime)
        return {"order_id":fake_id,"price":price,"qty":qty,"paper":True}
    oid = secrets.token_hex(16)
    resp = _cb_post("/api/v3/brokerage/orders",{
        "client_order_id":oid,"product_id":product_id,"side":side.upper(),
        "order_configuration":{"market_market_ioc":{"quote_size":f"{usd_amount:.2f}"}},
    })
    order_id = resp.get("success_response",{}).get("order_id",oid)
    print(f"  [LIVE] {side} ${usd_amount:.2f} {product_id} @ ${price:.4f} → {order_id}")
    tdb.open_trade(order_id,product_id,side.lower(),qty,price,signal_type,setup_notes,regime)
    return {"order_id":order_id,"price":price,"qty":qty}


def _close_position(order_id: str, product_id: str, usd_amount: float,
                    exit_reason: str, reasoning: str, regime: str) -> dict:
    result = _place_order(product_id,"SELL",usd_amount,exit_reason,reasoning,regime)
    tdb.close_trade(order_id, result["price"], exit_reason)
    return result


# ── Tools ─────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name":"get_portfolio",
        "description":"Current USD balance and crypto holdings.",
        "input_schema":{"type":"object","properties":{},"required":[]},
    },
    {
        "name":"get_open_positions",
        "description":"All open trades with live P&L, entry price, and hold/exit recommendation.",
        "input_schema":{"type":"object","properties":{},"required":[]},
    },
    {
        "name":"scan_internet",
        "description":(
            "Scan 8+ sources: CoinDesk, CoinTelegraph, Decrypt, Bitcoin Magazine, "
            "Google News, Reddit r/CryptoCurrency, r/Bitcoin, r/SatoshiStreetBets. "
            "Returns per-coin sentiment scores and hottest coins. "
            "Pass product_id to also search coin-specific subreddits and news."
        ),
        "input_schema":{
            "type":"object",
            "properties":{
                "product_id":{
                    "type":"string",
                    "description":"Optional — e.g. SOL-USD for deeper SOL-specific scan"
                }
            },
            "required":[],
        },
    },
    {
        "name":"get_market_intelligence",
        "description":"Fear & Greed, funding rates, BTC dominance, trending coins, volume movers.",
        "input_schema":{"type":"object","properties":{},"required":[]},
    },
    {
        "name":"analyze_coin",
        "description":"RSI, MAs, volume spike, 2h trend, support/resistance for a single coin.",
        "input_schema":{
            "type":"object",
            "properties":{"product_id":{"type":"string"}},
            "required":["product_id"],
        },
    },
    {
        "name":"get_historical_context",
        "description":"6-year historical stats from DB for a coin.",
        "input_schema":{
            "type":"object",
            "properties":{"symbol":{"type":"string"}},
            "required":["symbol"],
        },
    },
    {
        "name":"get_performance_stats",
        "description":"Win rate, PnL, breakdown from all past trades.",
        "input_schema":{"type":"object","properties":{},"required":[]},
    },
    {
        "name":"buy_crypto",
        "description":"Open a new BUY position on Coinbase (live market order).",
        "input_schema":{
            "type":"object",
            "properties":{
                "product_id":{"type":"string"},
                "usd_amount":{"type":"number"},
                "signal_type":{
                    "type":"string",
                    "enum":["momentum","mean_reversion","breakout","dip_buy",
                            "fear_greed","funding_squeeze","volume_spike","news_catalyst","trending"]
                },
                "reasoning":{"type":"string","description":"Include what intel (news, reddit, F&G, etc.) drove this"},
                "hold_target_pct":{"type":"number","description":"% gain target before selling (e.g. 8, 15, 25)"},
            },
            "required":["product_id","usd_amount","signal_type","reasoning"],
        },
    },
    {
        "name":"close_position",
        "description":"Close an open position (sell). Use order_id from get_open_positions.",
        "input_schema":{
            "type":"object",
            "properties":{
                "order_id":   {"type":"string"},
                "product_id": {"type":"string"},
                "usd_amount": {"type":"number","description":"USD worth to sell"},
                "exit_reason":{
                    "type":"string",
                    "enum":["take_profit","stop_loss","news_negative","regime_change","rebalance"]
                },
                "reasoning":  {"type":"string"},
            },
            "required":["order_id","product_id","usd_amount","exit_reason","reasoning"],
        },
    },
    {
        "name":"skip_session",
        "description":"Skip trading this session — use when no clear setup or news catalyst.",
        "input_schema":{
            "type":"object",
            "properties":{"reason":{"type":"string"}},
            "required":["reason"],
        },
    },
]


def _handle_tool(name, inp, regime):
    try:
        if name == "get_portfolio":
            return json.dumps(_get_portfolio())
        elif name == "get_open_positions":
            return json.dumps(get_open_positions())
        elif name == "scan_internet":
            return json.dumps(scan_internet(inp.get("product_id")))
        elif name == "get_market_intelligence":
            return json.dumps({
                "fear_and_greed": get_fear_and_greed(),
                "funding_rates":  get_funding_rates(),
                "dominance":      get_market_dominance(),
                "trending":       get_trending_coins(),
            })
        elif name == "analyze_coin":
            return json.dumps(analyze_coin(inp["product_id"]))
        elif name == "get_historical_context":
            return json.dumps(tdb.get_historical_stats(inp["symbol"]))
        elif name == "get_performance_stats":
            return json.dumps(tdb.get_performance_stats())
        elif name == "buy_crypto":
            pid  = inp["product_id"]; usd = float(inp["usd_amount"])
            port = _get_portfolio()
            avail = port["usd"] * (1 - MIN_USD_RESERVE)
            usd   = min(usd, port["usd"] * MAX_POSITION_PCT, avail)
            if usd < 1.0:
                return json.dumps({"error":"Insufficient free USD (<$1 after reserve)"})
            return json.dumps(_place_order(pid,"BUY",usd,
                inp["signal_type"],inp["reasoning"],regime))
        elif name == "close_position":
            return json.dumps(_close_position(
                inp["order_id"],inp["product_id"],float(inp["usd_amount"]),
                inp["exit_reason"],inp["reasoning"],regime))
        elif name == "skip_session":
            print(f"  [SKIP] {inp['reason']}")
            return json.dumps({"status":"skipped","reason":inp["reason"]})
    except Exception as e:
        return json.dumps({"error": str(e)})
    return json.dumps({"error": f"Unknown tool: {name}"})


# ── Regime ────────────────────────────────────────────────────────────────────

def _detect_regime():
    try:
        candles = _get_candles("BTC-USD","ONE_DAY",60)
        closes  = [float(c["close"]) for c in candles]
        if len(closes) < 7: return "unknown"
        ch24 = (closes[-1]-closes[-2])/closes[-2]*100 if len(closes)>=2 else 0
        if ch24 < -CRASH_THRESHOLD*100: return "crash_risk"
        ma7  = sum(closes[-7:])/7
        ma30 = sum(closes[-30:])/30 if len(closes)>=30 else sum(closes)/len(closes)
        if ma7 > ma30*1.03: return "bull_trend"
        elif ma7 < ma30*0.97: return "bear_trend"
        return "choppy"
    except Exception:
        return "unknown"


# ── System Prompt ─────────────────────────────────────────────────────────────

def _build_system(regime):
    notes = tdb.get_latest_strategy_notes(limit=2)
    stats = tdb.get_performance_stats()
    return f"""You are an aggressive crypto trading agent managing a live Coinbase portfolio.
Goal: MAXIMIZE DAILY PROFIT. You run every 2 hours across 10 coins, 24/7.
You can HOLD positions across multiple sessions — don't rush to close winners.

## Market Regime: {regime}
## Watchlist: {', '.join(WATCHLIST)}

## Session Workflow (follow this order)
1. scan_internet — scan Reddit + 7 news sources for sentiment and catalysts
2. get_market_intelligence — Fear & Greed, funding rates, dominance, trending
3. get_open_positions — review current holdings, decide hold vs close
4. analyze_coin on the best opportunities found in steps 1-2
5. Execute: buy new positions, close stops/profits, or skip

## When to HOLD vs CLOSE
- Hold if: news is still bullish, RSI not overbought, thesis intact
- Hold if: position is +3-7% and fundamentals improving — target 10-20%+ gains
- Close if: hit 8%+ profit AND news turning neutral/bearish
- Close if: down 3%+ AND news negative or new bearish catalyst found
- Close if: better opportunity elsewhere needs the capital

## Buy Signals (strongest first)
1. Coin mentioned positively in Reddit + news + F&G < 30 + RSI < 35 = HIGHEST CONVICTION
2. News catalyst (partnership, listing, upgrade) + volume spike > 2x
3. F&G < 20 (extreme fear) + BTC oversold = contrarian buy
4. Trending on CoinGecko + breaking out above MA14 with volume
5. Short squeeze: funding rate very negative + coin oversold

## Risk Rules
- Max 25% of portfolio per new trade
- Keep 20% in USD minimum at all times
- Stop loss at -3% from entry (close_position next session if hit)
- Take profit: 8% minimum, hold for 15-25% if thesis still bullish
- Max 3 open positions at once

## Performance
{json.dumps(stats, indent=2) if isinstance(stats,dict) else "No trades yet."}

## Strategy Notes
{chr(10).join(n['content'][:400] for n in notes) if notes else "Still learning."}
"""


# ── Main ─────────────────────────────────────────────────────────────────────

def run_crypto_session():
    if not KEY_NAME or not _KEY_B64:
        print("[crypto] Coinbase keys not set — skipping"); return

    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"\n[crypto] Session start {now_str} | LIVE={not PAPER}")

    regime = _detect_regime()
    print(f"[crypto] Regime: {regime}")
    if regime == "crash_risk":
        print("[crypto] BTC crash — skipping"); return

    messages = [{"role":"user","content":(
        f"Time: {now_str} | Regime: {regime} | {'LIVE' if not PAPER else 'PAPER'}\n"
        f"Watchlist: {', '.join(WATCHLIST)}\n\n"
        "Start your session. Scan the internet first, then markets, then make your moves. "
        "Hold winners if the thesis is still intact. Be aggressive on clear setups."
    )}]

    for _turn in range(24):
        resp = _client.messages.create(
            model="claude-opus-4-8", max_tokens=4096,
            system=_build_system(regime), tools=TOOLS, messages=messages,
        )
        if resp.stop_reason == "end_turn":
            for block in resp.content:
                if hasattr(block,"text") and block.text:
                    print(f"[crypto] {block.text[:800]}")
            break
        if resp.stop_reason != "tool_use": break

        tool_uses = [b for b in resp.content if b.type=="tool_use"]
        messages.append({"role":"assistant","content":resp.content})
        results = []
        for tu in tool_uses:
            print(f"  → {tu.name}({json.dumps(tu.input)[:80]})")
            out = _handle_tool(tu.name, tu.input, regime)
            print(f"    ← {out[:250]}")
            results.append({"type":"tool_result","tool_use_id":tu.id,"content":out})
        messages.append({"role":"user","content":results})

    print(f"[crypto] Session complete")


if __name__ == "__main__":
    run_crypto_session()
