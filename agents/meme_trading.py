"""
Meme coin trading agent — runs every hour, 24/7.
Trades SHIB, PEPE, FLOKI, WIF, BONK, TURBO, POPCAT on Coinbase.

Meme coin rules are DIFFERENT from regular crypto:
  - Social momentum is the #1 signal (Reddit virality, trending searches)
  - Volume spikes are everything — memes run on hype
  - In fast, out fast — hold hours to 2 days, not weeks
  - Wider stops: 10% (memes are volatile)
  - Bigger targets: 25-50%+ (memes can 10x overnight)
  - Tiny positions: 8% max (high risk = small size)
"""
import os, time, json, secrets, base64
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx, jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import anthropic
import trading_db as tdb
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

PAPER    = os.getenv("COINBASE_PAPER", "true").lower() == "true"
KEY_NAME = os.getenv("COINBASE_API_KEY_NAME", "")
_KEY_B64 = os.getenv("COINBASE_API_PRIVATE_KEY", "")
BASE_URL = "https://api.coinbase.com"

MEME_COINS = ["SHIB-USD", "PEPE-USD", "FLOKI-USD", "WIF-USD",
              "BONK-USD", "TURBO-USD", "POPCAT-USD"]

MEME_KEYWORDS = {
    "SHIB-USD":   ["shib","shiba","shiba inu"],
    "PEPE-USD":   ["pepe","pepecoin"],
    "FLOKI-USD":  ["floki","floki inu"],
    "WIF-USD":    ["wif","dogwifhat","wifhat"],
    "BONK-USD":   ["bonk","bonkcoin"],
    "TURBO-USD":  ["turbo","turbocoin"],
    "POPCAT-USD": ["popcat"],
}

MEME_SUBREDDITS = [
    "https://www.reddit.com/r/memecoin/hot/.rss?limit=25",
    "https://www.reddit.com/r/SatoshiStreetBets/hot/.rss?limit=25",
    "https://www.reddit.com/r/CryptoCurrency/hot/.rss?limit=20",
    "https://www.reddit.com/r/shib/hot/.rss?limit=15",
    "https://www.reddit.com/r/pepecoin/hot/.rss?limit=15",
    "https://www.reddit.com/r/dogecoin/hot/.rss?limit=10",
]

HYPE_WORDS  = {"moon","pump","100x","1000x","gem","viral","trending","🚀","fire",
               "breakout","surge","run","rocket","lambo","buy","accumulate","launch",
               "listing","partnership","bullish","bull","ape","aping","degen","send it"}
DUMP_WORDS  = {"dump","crash","rug","rugpull","scam","dead","sell","bearish","rekt",
               "exit","warning","fraud","honeypot","avoid","worthless"}

MAX_POSITION_PCT = 0.08   # 8% of portfolio per meme trade (high risk = small size)
STOP_LOSS_PCT    = 0.10   # 10% stop (memes are volatile)
TAKE_PROFIT_PCT  = 0.30   # 30% target (memes can run hard)
_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# ── JWT / Coinbase ────────────────────────────────────────────────────────────

def _make_jwt(method, path):
    raw = base64.b64decode(_KEY_B64)
    pk  = Ed25519PrivateKey.from_private_bytes(raw[:32])
    payload = {"sub":KEY_NAME,"iss":"cdp","nbf":int(time.time()),
               "exp":int(time.time())+120,"uri":f"{method} api.coinbase.com{path}"}
    return jwt.encode(payload, pk, algorithm="EdDSA",
                      headers={"kid":KEY_NAME,"nonce":secrets.token_hex(10)})

def _cb_get(path, params=None):
    r = httpx.get(f"{BASE_URL}{path}", params=params,
                  headers={"Authorization":f"Bearer {_make_jwt('GET',path)}"}, timeout=30)
    r.raise_for_status(); return r.json()

def _cb_post(path, body):
    r = httpx.post(f"{BASE_URL}{path}", json=body,
                   headers={"Authorization":f"Bearer {_make_jwt('POST',path)}",
                            "Content-Type":"application/json"}, timeout=30)
    r.raise_for_status(); return r.json()


# ── Social Scanner ────────────────────────────────────────────────────────────

def _rss_titles(url, limit=25):
    try:
        r = httpx.get(url, headers={"User-Agent":_UA}, timeout=10, follow_redirects=True)
        if r.status_code != 200: return []
        root = ET.fromstring(r.text)
        titles = [t.text for t in root.findall(".//item/title") if t.text]
        if not titles:
            ns = {"a":"http://www.w3.org/2005/Atom"}
            titles = [t.text for t in root.findall("a:entry/a:title", ns) if t.text]
        return titles[:limit]
    except Exception:
        return []


def scan_meme_social() -> dict:
    """
    Scan Reddit meme subreddits + Google News + CoinGecko trending.
    Score each meme coin by hype vs dump signals.
    """
    all_titles = []
    sources_hit = {}
    for url in MEME_SUBREDDITS:
        name    = url.split("/r/")[1].split("/")[0]
        titles  = _rss_titles(url)
        sources_hit[f"r/{name}"] = len(titles)
        all_titles.extend(titles)

    # Google News for meme coins
    for term in ["meme coin crypto","shiba pepe bonk wif crypto","memecoin pump"]:
        url    = f"https://news.google.com/rss/search?q={term.replace(' ','+')}&hl=en-US&gl=US&ceid=US:en"
        titles = _rss_titles(url, limit=10)
        sources_hit["google_news"] = sources_hit.get("google_news",0) + len(titles)
        all_titles.extend(titles)

    # Score each meme coin
    coin_scores = {}
    for pid, keywords in MEME_KEYWORDS.items():
        relevant = [t for t in all_titles if any(k in t.lower() for k in keywords)]
        if not relevant:
            coin_scores[pid] = {"mentions":0,"hype":0,"dump":0,"net":0,
                                "signal":"NO_BUZZ","headlines":[]}
            continue
        hype = sum(1 for t in relevant for w in HYPE_WORDS if w in t.lower())
        dump = sum(1 for t in relevant for w in DUMP_WORDS if w in t.lower())
        net  = hype - dump
        coin_scores[pid] = {
            "mentions":  len(relevant),
            "hype":      hype,
            "dump":      dump,
            "net":       net,
            "signal": (
                "🚀 VERY_HOT"  if net >= 5 and len(relevant) >= 4 else
                "🔥 HOT"       if net >= 3 or len(relevant) >= 5  else
                "📈 WARMING"   if net >= 1 and len(relevant) >= 2  else
                "💀 DUMP_RISK" if dump > hype                      else
                "😴 COLD"
            ),
            "headlines": relevant[:4],
        }

    hot = sorted(
        [(pid,d) for pid,d in coin_scores.items() if d["net"]>0],
        key=lambda x:(x[1]["net"],x[1]["mentions"]), reverse=True
    )
    return {
        "total_posts_scanned": len(all_titles),
        "sources":             sources_hit,
        "coin_scores":         coin_scores,
        "hottest":             [pid for pid,_ in hot[:4]],
        "verdict": (
            f"Hottest right now: {', '.join(pid for pid,_ in hot[:3])}"
            if hot else "No meme coin has buzz right now — skip session"
        ),
    }


def get_meme_market_data(product_id: str) -> dict:
    """Price, volume spike, RSI on 1h candles — fast timeframe for meme coins."""
    try:
        # 1h candles, last 48h
        end   = int(time.time()); start = end - 48*3600
        data  = _cb_get(f"/api/v3/brokerage/products/{product_id}/candles",
                        {"start":str(start),"end":str(end),"granularity":"ONE_HOUR"})
        candles = sorted(data.get("candles",[]), key=lambda c: c["start"])
        if not candles:
            return {"error": f"No 1h data for {product_id}"}

        closes = [float(c["close"]) for c in candles]
        vols   = [float(c.get("volume",0)) for c in candles]
        price  = closes[-1]

        ma6  = sum(closes[-6:])  / min(6,  len(closes))
        ma24 = sum(closes[-24:]) / min(24, len(closes))

        diffs  = [closes[i]-closes[i-1] for i in range(max(1,len(closes)-15),len(closes))]
        gains  = [max(0,d) for d in diffs]; losses=[max(0,-d) for d in diffs]
        ag,al  = sum(gains)/14, sum(losses)/14
        rsi    = round(100-(100/(1+ag/al)),1) if al>0 else 100

        avg_vol   = sum(vols[-24:])/min(24,len(vols)) if vols else 1
        vol_spike = round(vols[-1]/avg_vol, 2) if avg_vol>0 else 1

        ch1h  = round((closes[-1]-closes[-2])/closes[-2]*100,2) if len(closes)>=2 else 0
        ch6h  = round((closes[-1]-closes[-6])/closes[-6]*100,2) if len(closes)>=6 else 0
        ch24h = round((closes[-1]-closes[-24])/closes[-24]*100,2) if len(closes)>=24 else 0

        trend = "up" if closes[-1]>closes[-4] else "down"

        return {
            "product_id": product_id,
            "price":      price,
            "change_1h":  ch1h,
            "change_6h":  ch6h,
            "change_24h": ch24h,
            "rsi_1h":     rsi,
            "ma6h":       round(ma6,8),
            "ma24h":      round(ma24,8),
            "vol_spike":  vol_spike,
            "trend_4h":   trend,
            "momentum_signal": (
                "🚀 STRONG BUY — volume + uptrend"  if vol_spike>2.0 and trend=="up" else
                "📈 BUY — vol spike building"        if vol_spike>1.5 and ch6h>0     else
                "⚠️ OVERSOLD BOUNCE"                 if rsi<25 and trend=="up"        else
                "🔴 OVERBOUGHT — wait for dip"       if rsi>75                        else
                "😴 WAIT"
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def get_meme_positions() -> dict:
    conn  = tdb.get_conn()
    rows  = conn.execute(
        "SELECT * FROM trade_journal WHERE won IS NULL AND symbol IN ({})".format(
            ",".join("?"*len(MEME_COINS))
        ), MEME_COINS
    ).fetchall()
    conn.close()

    positions = []
    for row in rows:
        r     = dict(row)
        pid   = r["symbol"]
        curr  = _get_price(pid)
        entry = r["entry_price"] or 0
        qty   = r["qty"] or 0
        pnl_pct = (curr-entry)/entry*100 if entry>0 else 0
        pnl_usd = (curr-entry)*qty if entry>0 else 0
        positions.append({
            "order_id":    r["order_id"],
            "symbol":      pid,
            "qty":         qty,
            "entry_price": entry,
            "curr_price":  curr,
            "pnl_pct":     round(pnl_pct,2),
            "pnl_usd":     round(pnl_usd,4),
            "entry_time":  r["entry_time"],
            "status": (
                "🎯 TAKE_PROFIT (>30%)"   if pnl_pct >= 30 else
                "💰 PARTIAL_PROFIT (>15%)" if pnl_pct >= 15 else
                "🔴 STOP_LOSS (<-10%)"     if pnl_pct <= -10 else
                f"HOLD ({pnl_pct:+.1f}%)"
            ),
        })
    return {"meme_positions": positions, "count": len(positions)}


def _get_price(product_id):
    try:
        data = _cb_get("/api/v3/brokerage/best_bid_ask", {"product_ids":[product_id]})
        pb   = data.get("pricebooks",[{}])[0]
        bid  = float(pb["bids"][0]["price"]) if pb.get("bids") else 0
        ask  = float(pb["asks"][0]["price"]) if pb.get("asks") else 0
        return (bid+ask)/2
    except Exception:
        return 0.0


def _get_portfolio():
    data = _cb_get("/api/v3/brokerage/accounts")
    usd  = 0.0; holdings={}
    for acc in data.get("accounts",[]):
        cur   = acc.get("currency","")
        avail = float(acc.get("available_balance",{}).get("value",0))
        if cur=="USD": usd=avail
        elif avail>0:  holdings[cur]=avail
    return {"usd":round(usd,2),"holdings":holdings}


def _place_order(product_id, side, usd_amount, signal_type, notes, regime="meme"):
    price = _get_price(product_id)
    qty   = usd_amount/price if price>0 else 0
    if PAPER:
        fid = f"MEME-PAPER-{product_id}-{int(time.time())}"
        print(f"  [PAPER-MEME] {side} ${usd_amount:.2f} {product_id} @ {price}")
        tdb.open_trade(fid,product_id,side.lower(),qty,price,signal_type,notes,regime)
        return {"order_id":fid,"price":price,"qty":qty,"paper":True}
    oid  = secrets.token_hex(16)
    resp = _cb_post("/api/v3/brokerage/orders",{
        "client_order_id":oid,"product_id":product_id,"side":side.upper(),
        "order_configuration":{"market_market_ioc":{"quote_size":f"{usd_amount:.2f}"}},
    })
    order_id = resp.get("success_response",{}).get("order_id",oid)
    print(f"  [LIVE-MEME] {side} ${usd_amount:.2f} {product_id} @ {price} → {order_id}")
    tdb.open_trade(order_id,product_id,side.lower(),qty,price,signal_type,notes,regime)
    return {"order_id":order_id,"price":price,"qty":qty}


# ── Tools ─────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name":"get_portfolio",
        "description":"Current USD balance and holdings.",
        "input_schema":{"type":"object","properties":{},"required":[]},
    },
    {
        "name":"get_meme_positions",
        "description":"All open meme coin positions with live P&L and hold/close recommendation.",
        "input_schema":{"type":"object","properties":{},"required":[]},
    },
    {
        "name":"scan_meme_social",
        "description":(
            "Scan Reddit r/memecoin, r/SatoshiStreetBets, r/CryptoCurrency, r/shib, "
            "r/pepecoin, r/dogecoin + Google News meme coin searches. "
            "Scores each coin by hype vs dump signals. THIS IS YOUR PRIMARY SIGNAL."
        ),
        "input_schema":{"type":"object","properties":{},"required":[]},
    },
    {
        "name":"get_meme_market_data",
        "description":"1h candles: price, RSI, volume spike, 6h/24h change. Use after social scan to confirm.",
        "input_schema":{
            "type":"object",
            "properties":{"product_id":{"type":"string","description":"e.g. PEPE-USD"}},
            "required":["product_id"],
        },
    },
    {
        "name":"buy_meme",
        "description":"Buy a meme coin. Only when social buzz is HOT or VERY_HOT + volume confirming.",
        "input_schema":{
            "type":"object",
            "properties":{
                "product_id": {"type":"string"},
                "usd_amount": {"type":"number","description":"Keep small — 8% of portfolio max"},
                "signal_type":{
                    "type":"string",
                    "enum":["social_hype","volume_spike","reddit_viral",
                            "trending_search","news_catalyst","degen_play"]
                },
                "reasoning":  {"type":"string","description":"What specific social signal drove this"},
                "target_pct": {"type":"number","description":"% gain target, e.g. 30, 50, 100"},
            },
            "required":["product_id","usd_amount","signal_type","reasoning"],
        },
    },
    {
        "name":"close_meme_position",
        "description":"Close a meme coin position. Be quick — meme pumps reverse fast.",
        "input_schema":{
            "type":"object",
            "properties":{
                "order_id":   {"type":"string"},
                "product_id": {"type":"string"},
                "usd_amount": {"type":"number"},
                "exit_reason":{
                    "type":"string",
                    "enum":["take_profit","stop_loss","hype_fading",
                            "dump_signal","rug_warning","rebalance"]
                },
                "reasoning":{"type":"string"},
            },
            "required":["order_id","product_id","usd_amount","exit_reason","reasoning"],
        },
    },
    {
        "name":"skip_session",
        "description":"Skip — no meme coin has buzz right now. Better to wait than FOMO.",
        "input_schema":{
            "type":"object",
            "properties":{"reason":{"type":"string"}},
            "required":["reason"],
        },
    },
]


def _handle_tool(name, inp, regime):
    try:
        if name=="get_portfolio":
            return json.dumps(_get_portfolio())
        elif name=="get_meme_positions":
            return json.dumps(get_meme_positions())
        elif name=="scan_meme_social":
            return json.dumps(scan_meme_social())
        elif name=="get_meme_market_data":
            return json.dumps(get_meme_market_data(inp["product_id"]))
        elif name=="buy_meme":
            pid  = inp["product_id"]; usd=float(inp["usd_amount"])
            port = _get_portfolio()
            max_trade = port["usd"] * MAX_POSITION_PCT
            usd = min(usd, max_trade)
            if usd < 1.0:
                return json.dumps({"error":"Need at least $1 to trade"})
            return json.dumps(_place_order(pid,"BUY",usd,
                inp["signal_type"],inp["reasoning"],"meme"))
        elif name=="close_meme_position":
            result = _place_order(inp["product_id"],"SELL",float(inp["usd_amount"]),
                inp["exit_reason"],inp["reasoning"],"meme")
            tdb.close_trade(inp["order_id"], result["price"], inp["exit_reason"])
            return json.dumps(result)
        elif name=="skip_session":
            print(f"  [MEME-SKIP] {inp['reason']}")
            return json.dumps({"status":"skipped","reason":inp["reason"]})
    except Exception as e:
        return json.dumps({"error":str(e)})
    return json.dumps({"error":f"Unknown tool: {name}"})


# ── System Prompt ─────────────────────────────────────────────────────────────

def _build_system():
    stats = tdb.get_performance_stats()
    return f"""You are a degen meme coin trading agent. Your entire edge is social momentum.
You run every hour on Coinbase trading: {', '.join(MEME_COINS)}

## Meme Coin Rules (COMPLETELY DIFFERENT from normal crypto)
- Social buzz is your #1 signal — if Reddit isn't talking about it, don't buy it
- Volume spike on 1h chart CONFIRMS the social signal — no volume = no trade
- Get in EARLY (before the pump peaks), get out FAST (before the dump)
- Target 25-50% gains, stop at -10% — memes are binary: moon or rug
- Max 8% of portfolio per trade (these are high-risk degen plays)
- Hold 2-24 hours MAX unless momentum is still clearly building
- If social buzz fades → EXIT immediately even if not at target

## Buy Checklist (need 3+ of these)
☑ Reddit buzz: HOT or VERY_HOT signal
☑ Volume spike > 1.5x on 1h chart
☑ Price not already pumped 50%+ in last 24h (too late)
☑ RSI < 65 on 1h (not overbought yet)
☑ 4h trend = up (momentum confirming)
☑ No dump/rug warning signals in headlines

## Session Flow
1. scan_meme_social → find what's buzzing
2. get_meme_positions → check open trades (close if at target/stop/hype fading)
3. get_meme_market_data on the hottest 1-2 coins
4. Buy if checklist passes, skip if nothing qualifies
5. Never buy more than 1-2 meme coins at once

## Performance
{json.dumps(stats, indent=2) if isinstance(stats,dict) else "No meme trades yet — let's get it."}

FOMO is your enemy. Only buy real buzz. Skip when in doubt.
"""


# ── Main ─────────────────────────────────────────────────────────────────────

def run_meme_session():
    if not KEY_NAME or not _KEY_B64:
        print("[meme] Coinbase keys not set — skipping"); return

    now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    print(f"\n[meme] Session start {now_str} | LIVE={not PAPER}")

    messages = [{"role":"user","content":(
        f"Time: {now_str} | Coins: {', '.join(MEME_COINS)} | {'LIVE' if not PAPER else 'PAPER'}\n\n"
        "Scan the meme subreddits and news. Find what's buzzing. "
        "Check your open positions. Buy if there's real social momentum + volume. "
        "Skip if nothing qualifies — don't force it."
    )}]

    for _turn in range(16):
        resp = _client.messages.create(
            model="claude-opus-4-8", max_tokens=3000,
            system=_build_system(), tools=TOOLS, messages=messages,
        )
        if resp.stop_reason=="end_turn":
            for block in resp.content:
                if hasattr(block,"text") and block.text:
                    print(f"[meme] {block.text[:600]}")
            break
        if resp.stop_reason!="tool_use": break

        tool_uses = [b for b in resp.content if b.type=="tool_use"]
        messages.append({"role":"assistant","content":resp.content})
        results = []
        for tu in tool_uses:
            print(f"  → {tu.name}({json.dumps(tu.input)[:80]})")
            out = _handle_tool(tu.name, tu.input, "meme")
            print(f"    ← {out[:250]}")
            results.append({"type":"tool_result","tool_use_id":tu.id,"content":out})
        messages.append({"role":"user","content":results})

    print("[meme] Session complete")


if __name__ == "__main__":
    run_meme_session()
