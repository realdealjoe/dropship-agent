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

db.init_db()

# Start ngrok tunnel
print("[serve] Starting ngrok tunnel...")
tunnel = ngrok.connect(WEBHOOK_PORT, "http")
public_url = tunnel.public_url.replace("http://", "https://")
print(f"[serve] Public URL: {public_url}")

# Register Shopify webhooks
WEBHOOK_TOPICS = [
    ("orders/create", f"{public_url}/webhooks/orders/create"),
    ("customer_queries/create", f"{public_url}/webhooks/customer_queries/created"),
]
existing = {w["topic"] for w in shopify.list_webhooks()}
for topic, address in WEBHOOK_TOPICS:
    if topic in existing:
        print(f"[serve] Webhook already registered: {topic}")
    else:
        try:
            shopify.register_webhook(topic, address)
            print(f"[serve] Registered webhook: {topic} → {address}")
        except Exception as e:
            print(f"[serve] Webhook registration failed for {topic}: {e}")

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
