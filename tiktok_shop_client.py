"""
TikTok Shop Open API client.
Handles order sync, tracking push, webhook registration, and signature verification.
Docs: https://partner.tiktokshop.com/docv2/page/63fea6e2defece02be7d4c71
"""
import hashlib
import hmac
import json
import os
import time
import httpx

APP_KEY    = os.getenv("TIKTOK_APP_KEY", "")
APP_SECRET = os.getenv("TIKTOK_APP_SECRET", "")
ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN", "")
SHOP_ID    = os.getenv("TIKTOK_SHOP_ID", "")

BASE_URL = "https://open-api.tiktokglobalshop.com"


def _is_configured() -> bool:
    return bool(APP_KEY and APP_SECRET and ACCESS_TOKEN and SHOP_ID)


def _sign(path: str, params: dict) -> str:
    """TikTok Shop request signing: SHA256(secret + path + sorted_params + secret)."""
    param_str = "".join(f"{k}{v}" for k, v in sorted(params.items())
                        if k not in ("sign", "access_token"))
    raw = APP_SECRET + path + param_str + APP_SECRET
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _base_params() -> dict:
    return {
        "app_key":   APP_KEY,
        "shop_id":   SHOP_ID,
        "timestamp": str(int(time.time())),
        "version":   "202309",
    }


def _get(path: str, extra_params: dict = None) -> dict:
    params = {**_base_params(), **(extra_params or {})}
    params["sign"] = _sign(path, params)
    r = httpx.get(
        BASE_URL + path,
        params=params,
        headers={"x-tts-access-token": ACCESS_TOKEN},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict, extra_params: dict = None) -> dict:
    params = {**_base_params(), **(extra_params or {})}
    params["sign"] = _sign(path, params)
    r = httpx.post(
        BASE_URL + path,
        params=params,
        json=body,
        headers={"x-tts-access-token": ACCESS_TOKEN, "Content-Type": "application/json"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


# ── Webhook verification ──────────────────────────────────────────────────────

def verify_webhook(raw_body: bytes, timestamp: str, nonce: str, signature: str) -> bool:
    """Verify a TikTok Shop webhook using HMAC-SHA256."""
    msg = APP_SECRET + timestamp + nonce + raw_body.decode("utf-8")
    computed = hashlib.sha256(msg.encode("utf-8")).hexdigest()
    try:
        return hmac.compare_digest(computed, signature)
    except Exception:
        return False


# ── Orders ────────────────────────────────────────────────────────────────────

def get_order(order_id: str) -> dict:
    """Fetch full order detail from TikTok Shop."""
    if not _is_configured():
        return {}
    data = _post("/order/202309/orders/search", {"order_id_list": [order_id]})
    orders = data.get("data", {}).get("order_list", [])
    return orders[0] if orders else {}


def cancel_order(order_id: str, cancel_reason: str = "OUT_OF_STOCK") -> dict:
    """Request cancellation of a TikTok Shop order (only works before shipment)."""
    if not _is_configured():
        return {"error": "TikTok Shop not configured"}
    return _post(f"/fulfillment/202309/orders/{order_id}/cancel", {
        "cancel_reason": cancel_reason,
    })


# ── Tracking ──────────────────────────────────────────────────────────────────

def push_tracking(order_id: str, tracking_number: str, shipping_provider: str = "Other") -> dict:
    """Push tracking info back to TikTok Shop so the buyer gets notified."""
    if not _is_configured():
        return {"error": "TikTok Shop not configured"}
    return _post("/fulfillment/202309/packages", {
        "order_id":          order_id,
        "tracking_number":   tracking_number,
        "shipping_provider": shipping_provider,
    })


# ── Webhook registration ──────────────────────────────────────────────────────

TIKTOK_EVENT_TYPES = [
    "ORDER_STATUS_CHANGE",   # new, paid, cancelled, shipped, delivered
    "PACKAGE_UPDATE",        # tracking updates
]

def register_webhooks(callback_url: str) -> list[dict]:
    """Subscribe to TikTok Shop events. Returns list of results."""
    if not _is_configured():
        return [{"error": "TikTok Shop credentials not set in .env"}]
    results = []
    for event in TIKTOK_EVENT_TYPES:
        try:
            res = _post("/event/202309/subscriptions", {
                "event_type": event,
                "address":    callback_url,
            })
            results.append({"event": event, "result": res})
            print(f"[TikTok] Registered webhook: {event} → {callback_url}")
        except Exception as e:
            results.append({"event": event, "error": str(e)})
            print(f"[TikTok] Webhook registration failed for {event}: {e}")
    return results
