"""
FastAPI webhook server — receives Shopify and TikTok Shop events and dispatches to agents.
"""
import hashlib
import hmac
import json
import threading
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from config import SHOPIFY_WEBHOOK_SECRET
from agents.order_fulfillment import OrderFulfillmentAgent
from agents.customer_support import CustomerSupportAgent
from agents.alibaba_fulfillment_agent import handle_new_order as ps_handle_order, verify_shopify_webhook as ps_verify
import tiktok_shop_client as tiktok
import database as db
from suppliers.cj_dropshipping import CJDropshipping
from suppliers.aliexpress import AliExpress

app = FastAPI(title="Dropship Agent Webhooks")

_fulfillment_agent = OrderFulfillmentAgent()
_support_agent     = CustomerSupportAgent()
_cj                = CJDropshipping()
_ali               = AliExpress()


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


# ── Painless Sleep — AliExpress auto-fulfillment ──────────────────────────────

@app.post("/webhooks/painless-sleep/orders/create")
async def ps_order_created(request: Request, background_tasks: BackgroundTasks):
    """Receives Shopify orders/create webhook from Painless Sleep store."""
    body = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    if hmac_header and not ps_verify(body, hmac_header):
        raise HTTPException(status_code=401, detail="Invalid HMAC")
    order = json.loads(body)
    background_tasks.add_task(ps_handle_order, order)
    return {"status": "accepted"}


# ── TikTok Shop webhooks ──────────────────────────────────────────────────────

def _tiktok_order_created(order: dict):
    """Synthesise a Shopify-like payload and run the fulfillment agent."""
    try:
        tiktok_order_id = str(order.get("order_id", ""))
        synthetic = {
            "id":           tiktok_order_id,
            "order_number": tiktok_order_id,
            "source":       "tiktok",
            "_raw":         order,
        }
        db.upsert_order(tiktok_order_id, tiktok_order_id=tiktok_order_id, status="pending")
        result = _fulfillment_agent.fulfill_order(synthetic)
        print(f"[tiktok-fulfillment] Order {tiktok_order_id}: {result}")
    except Exception as e:
        print(f"[tiktok-fulfillment] Error: {e}")


def _tiktok_order_cancelled(order: dict):
    """Cancel the supplier order linked to this TikTok order."""
    try:
        tiktok_order_id = str(order.get("order_id", ""))
        record = db.get_order_by_tiktok_id(tiktok_order_id)
        if not record:
            print(f"[tiktok-cancel] No supplier order found for TikTok order {tiktok_order_id}")
            return
        supplier         = record.get("supplier", "")
        supplier_order_id = record.get("supplier_order_id", "")
        if not supplier_order_id:
            print(f"[tiktok-cancel] No supplier_order_id for TikTok order {tiktok_order_id}")
            return
        if supplier == "cj":
            result = _cj.cancel_order(supplier_order_id)
        elif supplier == "aliexpress":
            result = _ali.cancel_order(supplier_order_id)
        else:
            result = {"error": f"Unknown supplier: {supplier}"}
        db.upsert_order(tiktok_order_id, status="cancelled")
        print(f"[tiktok-cancel] Order {tiktok_order_id} → supplier cancel result: {result}")
    except Exception as e:
        print(f"[tiktok-cancel] Error: {e}")


def _tiktok_tracking_push(order: dict):
    """When supplier tracking is available, push it back to TikTok Shop."""
    try:
        tiktok_order_id = str(order.get("order_id", ""))
        record = db.get_order_by_tiktok_id(tiktok_order_id)
        if not record or not record.get("tracking_number"):
            return
        result = tiktok.push_tracking(tiktok_order_id, record["tracking_number"])
        print(f"[tiktok-tracking] Pushed tracking for {tiktok_order_id}: {result}")
    except Exception as e:
        print(f"[tiktok-tracking] Error: {e}")


# TikTok order statuses that should trigger fulfillment
_FULFILLMENT_STATUSES = {"AWAITING_SHIPMENT", "ON_HOLD"}
# TikTok order statuses that should trigger cancellation
_CANCEL_STATUSES = {"CANCELLED"}


@app.post("/webhooks/tiktok/order")
async def tiktok_order_event(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()

    # Verify TikTok signature
    timestamp = request.headers.get("timestamp", "")
    nonce     = request.headers.get("nonce", "")
    signature = request.headers.get("Authorization", "")
    if tiktok.APP_SECRET and not tiktok.verify_webhook(body, timestamp, nonce, signature):
        raise HTTPException(status_code=401, detail="Invalid TikTok signature")

    payload = json.loads(body)
    event_type = payload.get("type", "")
    data       = payload.get("data", {})

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            pass

    order_status = data.get("order_status", "")

    print(f"[tiktok] Event: {event_type} | Status: {order_status} | Order: {data.get('order_id')}")

    if event_type == "ORDER_STATUS_CHANGE":
        if order_status in _FULFILLMENT_STATUSES:
            background_tasks.add_task(_tiktok_order_created, data)
        elif order_status in _CANCEL_STATUSES:
            background_tasks.add_task(_tiktok_order_cancelled, data)
    elif event_type == "PACKAGE_UPDATE":
        background_tasks.add_task(_tiktok_tracking_push, data)

    return {"status": "accepted"}
