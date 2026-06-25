"""
FastAPI webhook server — receives Shopify events and dispatches to agents.
"""
import hashlib
import hmac
import json
import threading
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from config import SHOPIFY_WEBHOOK_SECRET
from agents.order_fulfillment import OrderFulfillmentAgent
from agents.customer_support import CustomerSupportAgent

app = FastAPI(title="Dropship Agent Webhooks")

_fulfillment_agent = OrderFulfillmentAgent()
_support_agent = CustomerSupportAgent()


def _verify_shopify_hmac(body: bytes, hmac_header: str) -> bool:
    if not SHOPIFY_WEBHOOK_SECRET:
        return True  # skip verification if secret not configured
    digest = hmac.new(
        SHOPIFY_WEBHOOK_SECRET.encode("utf-8"),
        body, hashlib.sha256
    ).digest()
    import base64
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, hmac_header)


def _fulfill_in_background(order: dict):
    try:
        result = _fulfillment_agent.fulfill_order(order)
        print(f"[fulfillment] Order #{order.get('order_number')}: {result}")
    except Exception as e:
        print(f"[fulfillment] Error: {e}")


def _support_in_background(payload: dict):
    try:
        customer = payload.get("customer", {})
        name = f"{customer.get('first_name', '')} {customer.get('last_name', '')}".strip() or "Customer"
        email = customer.get("email", "")
        order_id = str(payload.get("order_id", ""))
        message = payload.get("body", "")
        result = _support_agent.handle_message(name, email, message, order_id)
        reply = _support_agent.extract_reply(result)
        print(f"[support] Draft reply for {email}:\n{reply}\n")
    except Exception as e:
        print(f"[support] Error: {e}")


@app.post("/webhooks/orders/create")
async def order_created(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not _verify_shopify_hmac(body, hmac_header):
        raise HTTPException(status_code=401, detail="Invalid HMAC")
    order = json.loads(body)
    background_tasks.add_task(_fulfill_in_background, order)
    return {"status": "accepted"}


@app.post("/webhooks/customer_queries/created")
async def customer_query(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if not _verify_shopify_hmac(body, hmac_header):
        raise HTTPException(status_code=401, detail="Invalid HMAC")
    payload = json.loads(body)
    background_tasks.add_task(_support_in_background, payload)
    return {"status": "accepted"}


@app.post("/support/message")
async def manual_support(request: Request):
    """Direct endpoint to test the support agent without a Shopify webhook."""
    data = await request.json()
    name = data.get("name", "Customer")
    email = data.get("email", "")
    message = data.get("message", "")
    order_id = data.get("order_id", "")
    result = _support_agent.handle_message(name, email, message, order_id)
    reply = _support_agent.extract_reply(result)
    return {"reply": reply, "full_output": result}


@app.get("/health")
async def health():
    return {"status": "ok"}
