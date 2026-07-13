#!/usr/bin/env python3
"""
Start the full dropship agent stack:
- ngrok tunnel (public HTTPS URL)
- Shopify webhook registration
- FastAPI webhook server
- APScheduler background jobs
"""
import time
import threading
import uvicorn
from pyngrok import ngrok

import database as db
from config import WEBHOOK_PORT
from scheduler import build_scheduler
import shopify_client as shopify
import tiktok_shop_client as tiktok

db.init_db()

# Start ngrok tunnel
print("[serve] Starting ngrok tunnel...")
tunnel = ngrok.connect(WEBHOOK_PORT, "http")
public_url = tunnel.public_url.replace("http://", "https://")
print(f"[serve] Public URL: {public_url}")

# Register Shopify webhooks
SHOPIFY_TOPICS = [
    ("orders/create",    f"{public_url}/webhooks/orders/create"),
    ("orders/paid",      f"{public_url}/webhooks/orders/paid"),
    ("orders/cancelled", f"{public_url}/webhooks/orders/cancelled"),
    ("orders/fulfilled", f"{public_url}/webhooks/orders/fulfilled"),
    ("customer_queries/create", f"{public_url}/webhooks/customer_queries/created"),
]
existing = {w["topic"] for w in shopify.list_webhooks()}
for topic, address in SHOPIFY_TOPICS:
    if topic in existing:
        print(f"[serve] Shopify webhook already registered: {topic}")
    else:
        try:
            shopify.register_webhook(topic, address)
            print(f"[serve] Registered Shopify webhook: {topic} → {address}")
        except Exception as e:
            print(f"[serve] Shopify webhook failed for {topic}: {e}")

# Register Painless Sleep webhook
try:
    from agents.alibaba_fulfillment_agent import _get_ps_token, PS_STORE
    ps_token = _get_ps_token()
    ps_topic = "orders/create"
    ps_address = f"{public_url}/webhooks/painless-sleep/orders/create"
    import urllib.request as _ur, urllib.parse as _up, json as _json
    # Check existing webhooks
    _req = _ur.Request(
        f"https://{PS_STORE}/admin/api/2024-01/webhooks.json",
        headers={"X-Shopify-Access-Token": ps_token}
    )
    with _ur.urlopen(_req, timeout=10) as _r:
        _existing = {w["topic"]: w for w in _json.load(_r).get("webhooks", [])}
    if ps_topic in _existing:
        # Update address if it changed
        wh_id = _existing[ps_topic]["id"]
        _data = _json.dumps({"webhook": {"address": ps_address}}).encode()
        _req2 = _ur.Request(
            f"https://{PS_STORE}/admin/api/2024-01/webhooks/{wh_id}.json",
            data=_data, method="PUT",
            headers={"X-Shopify-Access-Token": ps_token, "Content-Type": "application/json"}
        )
        with _ur.urlopen(_req2, timeout=10) as _r2:
            _json.load(_r2)
        print(f"[serve] Updated Painless Sleep webhook: {ps_topic} → {ps_address}")
    else:
        _data = _json.dumps({"webhook": {"topic": ps_topic, "address": ps_address, "format": "json"}}).encode()
        _req2 = _ur.Request(
            f"https://{PS_STORE}/admin/api/2024-01/webhooks.json",
            data=_data, method="POST",
            headers={"X-Shopify-Access-Token": ps_token, "Content-Type": "application/json"}
        )
        with _ur.urlopen(_req2, timeout=10) as _r2:
            _json.load(_r2)
        print(f"[serve] Registered Painless Sleep webhook: {ps_topic} → {ps_address}")
except Exception as _e:
    print(f"[serve] Painless Sleep webhook registration failed: {_e}")

# Register TikTok Shop webhooks (skipped if credentials not set)
if tiktok._is_configured():
    tiktok_callback = f"{public_url}/webhooks/tiktok/order"
    results = tiktok.register_webhooks(tiktok_callback)
    for r in results:
        if "error" in r:
            print(f"[serve] TikTok webhook failed for {r['event']}: {r['error']}")
        else:
            print(f"[serve] Registered TikTok webhook: {r['event']} → {tiktok_callback}")
else:
    print("[serve] TikTok Shop credentials not set — skipping TikTok webhook registration")

# Start scheduler in background
scheduler = build_scheduler()
scheduler.start()
print("[serve] Scheduler started (repricing daily, sourcing weekly)")

print(f"\n{'='*60}")
print(f"  Dropship Agent running!")
print(f"  Webhook URL: {public_url}")
print(f"  Shopify admin: https://8k0gdf-iz.myshopify.com/admin")
print(f"  Press Ctrl+C to stop")
print(f"{'='*60}\n")

# Start FastAPI (blocks until Ctrl+C)
from webhooks.server import app
uvicorn.run(app, host="0.0.0.0", port=WEBHOOK_PORT, log_level="warning")
