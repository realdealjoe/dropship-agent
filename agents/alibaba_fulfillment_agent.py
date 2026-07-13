"""
AliExpress Auto-Fulfillment Agent — Painless Sleep
===================================================
Monitors Shopify (Painless Sleep) for new orders and automatically
places them on AliExpress using the official DS API.

Flow:
  1. Shopify webhook → new order received
  2. Agent maps Shopify product → AliExpress product ID
  3. Calls aliexpress.ds.order.create to place order with customer shipping address
  4. Saves AliExpress order ID + tracking
  5. Pushes tracking back to Shopify, fulfills the order

.env keys needed:
  PS_SHOPIFY_STORE            — 5b3ugw-mv.myshopify.com
  PS_SHOPIFY_CLIENT_ID        — from Dev Dashboard → Settings
  PS_SHOPIFY_CLIENT_SECRET    — from Dev Dashboard → Settings
  PS_SHOPIFY_WEBHOOK_SECRET   — same as PS_SHOPIFY_CLIENT_SECRET
  ALIEXPRESS_APP_KEY          — from AliExpress Open Platform console
  ALIEXPRESS_APP_SECRET       — from AliExpress Open Platform console
  ALIEXPRESS_ACCESS_TOKEN     — current OAuth token
  NOTIFY_EMAIL                — plimpery@gmail.com
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS

Note: Shopify tokens are obtained via client credentials grant (expires 24h) and
auto-refreshed — no static shpat token needed.
"""

import os, json, hmac as hmac_lib, hashlib, time, smtplib, urllib.request, urllib.parse
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

# ── Config ────────────────────────────────────────────────────────────────────
PS_STORE         = os.getenv("PS_SHOPIFY_STORE", "5b3ugw-mv.myshopify.com")
PS_CLIENT_ID     = os.getenv("PS_SHOPIFY_CLIENT_ID", "")
PS_CLIENT_SECRET = os.getenv("PS_SHOPIFY_CLIENT_SECRET", "")
WEBHOOK_SECRET   = os.getenv("PS_SHOPIFY_WEBHOOK_SECRET", "")

# ── Shopify token cache (client credentials — refreshed every 24h) ────────────
_ps_token: str = ""
_ps_token_expiry: float = 0.0

def _get_ps_token() -> str:
    global _ps_token, _ps_token_expiry
    if _ps_token and time.time() < _ps_token_expiry - 300:
        return _ps_token
    payload = urllib.parse.urlencode({
        "client_id":     PS_CLIENT_ID,
        "client_secret": PS_CLIENT_SECRET,
        "grant_type":    "client_credentials",
    }).encode()
    req = urllib.request.Request(
        f"https://{PS_STORE}/admin/oauth/access_token",
        data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.load(r)
    _ps_token = data["access_token"]
    _ps_token_expiry = time.time() + data.get("expires_in", 86400)
    return _ps_token
AE_APP_KEY     = os.getenv("ALIEXPRESS_APP_KEY", "538176")
AE_APP_SECRET  = os.getenv("ALIEXPRESS_APP_SECRET", "")
AE_TOKEN       = os.getenv("ALIEXPRESS_ACCESS_TOKEN", "")
NOTIFY_EMAIL   = os.getenv("NOTIFY_EMAIL", "plimpery@gmail.com")
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "")
SMTP_PASS      = os.getenv("SMTP_PASS", "")

# ── Product Map: Shopify variant → AliExpress product ID + SKU ───────────────
# Find AliExpress product IDs by searching for the pillow on aliexpress.com
# then copying the numeric ID from the URL: aliexpress.com/item/XXXXXXXXXX.html
# Fill these in once you've identified the exact AliExpress listing.
PRODUCT_MAP = {
    # "Shopify variant title": { "product_id": "...", "sku_id": "..." }
    # Example (update with real AliExpress IDs):
    "default": {
        "product_id": os.getenv("AE_CERVICAL_PILLOW_ID", ""),  # fill in .env
        "sku_id": os.getenv("AE_CERVICAL_PILLOW_SKU", ""),
    }
}

_ORDERS_FILE = Path(__file__).parent.parent / ".ae_orders.json"


def _load_orders() -> dict:
    if _ORDERS_FILE.exists():
        return json.loads(_ORDERS_FILE.read_text())
    return {}


def _save_orders(orders: dict):
    _ORDERS_FILE.write_text(json.dumps(orders, indent=2))


# ── AliExpress DS API ─────────────────────────────────────────────────────────

def _ae_sign(params: dict) -> str:
    # AliExpress HMAC-SHA256: key=secret, msg=sorted_kv (no secret wrapping)
    sorted_kv = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    return hmac_lib.new(AE_APP_SECRET.encode(), sorted_kv.encode(), hashlib.sha256).hexdigest().upper()


def ae_call(method: str, params: dict) -> dict:
    base = {
        "method": method,
        "app_key": AE_APP_KEY,
        "access_token": AE_TOKEN,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "v": "2.0",
    }
    base.update(params)
    base["sign"] = _ae_sign(base)
    data = urllib.parse.urlencode(base).encode()
    req = urllib.request.Request(
        "https://api-sg.aliexpress.com/sync", data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def ae_search_product(keywords: str, page_size: int = 10) -> dict:
    return ae_call("aliexpress.ds.text.search", {
        "keywords": keywords,
        "page_no": "1",
        "page_size": str(page_size),
        "countryCode": "US",
        "currency": "USD",
        "local": "US",
    })


def ae_get_product(product_id: str) -> dict:
    return ae_call("aliexpress.ds.product.get", {
        "product_id": product_id,
        "ship_to_country": "US",
        "local_country": "US",
        "local_currency": "USD",
    })


def ae_place_order(order: dict, ae_product_id: str, ae_sku_id: str) -> dict:
    """Place an order on AliExpress DS for a Shopify order."""
    shipping = order["shipping_address"]

    # Build product list
    product_items = [{
        "product_id": ae_product_id,
        "sku_id": ae_sku_id,
        "quantity": sum(item["quantity"] for item in order["line_items"]),
    }]

    # Map Shopify country code → AliExpress country code (usually same ISO-2)
    country_code = shipping.get("country_code", "US")

    order_payload = {
        "logistics_address": {
            "contact_person": f"{shipping.get('first_name','')} {shipping.get('last_name','')}".strip(),
            "mobile_no": shipping.get("phone", ""),
            "detail_address": shipping.get("address1", ""),
            "address2": shipping.get("address2", ""),
            "city": shipping.get("city", ""),
            "province": shipping.get("province", ""),
            "zip": shipping.get("zip", ""),
            "country": country_code,
        },
        "product_items": product_items,
        "out_order_id": str(order["order_number"]),
    }

    return ae_call("aliexpress.ds.order.create", {
        "param_place_order_request4_open_api_d_t_o": json.dumps(order_payload)
    })


def ae_get_tracking(ae_order_id: str) -> dict:
    return ae_call("aliexpress.ds.order.tracking.info.query", {
        "order_id": ae_order_id,
        "out_ref": ae_order_id,
    })


# ── Shopify API ───────────────────────────────────────────────────────────────

def shopify_get(path: str) -> dict:
    url = f"https://{PS_STORE}/admin/api/2024-01/{path}"
    req = urllib.request.Request(url,
        headers={"X-Shopify-Access-Token": _get_ps_token(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def shopify_post(path: str, payload: dict) -> dict:
    url = f"https://{PS_STORE}/admin/api/2024-01/{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
        headers={"X-Shopify-Access-Token": _get_ps_token(), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except Exception as e:
        print(f"  [shopify] POST error: {e}")
        return {}


def fulfill_shopify_order(order_id: str, tracking_number: str, carrier: str):
    """Mark Shopify order as fulfilled with tracking."""
    # First get fulfillment order ID
    fo_result = shopify_get(f"orders/{order_id}/fulfillment_orders.json")
    fulfillment_orders = fo_result.get("fulfillment_orders", [])
    if not fulfillment_orders:
        print(f"  [shopify] No fulfillment orders found for {order_id}")
        return

    fo_id = fulfillment_orders[0]["id"]
    payload = {
        "fulfillment": {
            "line_items_by_fulfillment_order": [{"fulfillment_order_id": fo_id}],
            "tracking_info": {
                "number": tracking_number,
                "company": carrier,
            },
            "notify_customer": True,
        }
    }
    result = shopify_post("fulfillments.json", payload)
    if result.get("fulfillment"):
        print(f"  [shopify] Order {order_id} fulfilled ✓ — tracking: {tracking_number}")
    else:
        print(f"  [shopify] Fulfillment response: {result}")


# ── Email ─────────────────────────────────────────────────────────────────────

def _send_email(to: str, subject: str, body: str):
    if not all([SMTP_USER, SMTP_PASS]):
        return
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, to, msg.as_string())
    except Exception as e:
        print(f"  [email] Failed: {e}")


def notify_owner(subject: str, body: str):
    _send_email(NOTIFY_EMAIL, subject, body)


# ── Webhook Verification ──────────────────────────────────────────────────────

def verify_shopify_webhook(raw_body: bytes, hmac_header: str) -> bool:
    import base64
    digest = hmac_lib.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode()
    return hmac_lib.compare_digest(computed, hmac_header)


# ── Order Handler ─────────────────────────────────────────────────────────────

def handle_new_order(order: dict):
    """Main handler: called when Shopify fires orders/create webhook."""
    order_id  = str(order["id"])
    order_num = order["order_number"]
    orders    = _load_orders()

    if order_id in orders:
        print(f"  [fulfillment] #{order_num} already processed")
        return

    print(f"\n[fulfillment] New order #{order_num} from {order.get('shipping_address',{}).get('first_name','')} — placing on AliExpress...")

    # Look up AliExpress product ID
    ae_product_id = PRODUCT_MAP["default"]["product_id"]
    ae_sku_id     = PRODUCT_MAP["default"]["sku_id"]

    if not ae_product_id:
        msg = f"Order #{order_num} received but AE_CERVICAL_PILLOW_ID not set — MANUAL FULFILLMENT NEEDED"
        print(f"  [fulfillment] WARNING: {msg}")
        notify_owner(f"[Painless Sleep] MANUAL ORDER NEEDED #{order_num}", msg)
        orders[order_id] = {
            "order_number": order_num, "ae_order_id": None,
            "status": "manual_needed", "fulfilled": False,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        _save_orders(orders)
        return

    # Place order on AliExpress
    try:
        result = ae_place_order(order, ae_product_id, ae_sku_id)
        ae_response = result.get("aliexpress_ds_order_create_response", {})
        ae_order_id = ae_response.get("order_id") or ae_response.get("result", {}).get("order_id")

        if ae_order_id:
            print(f"  [fulfillment] AliExpress order placed: {ae_order_id} ✓")
            notify_owner(
                f"[Painless Sleep] Order #{order_num} placed on AliExpress",
                f"AliExpress order ID: {ae_order_id}\nCustomer: {order.get('shipping_address',{}).get('first_name','')} {order.get('shipping_address',{}).get('last_name','')}"
            )
            orders[order_id] = {
                "order_number": order_num,
                "ae_order_id": str(ae_order_id),
                "status": "placed",
                "fulfilled": False,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
        else:
            print(f"  [fulfillment] AliExpress order failed: {result}")
            notify_owner(
                f"[Painless Sleep] FAILED to place order #{order_num}",
                f"AliExpress API response:\n{json.dumps(result, indent=2)}"
            )
            orders[order_id] = {
                "order_number": order_num, "ae_order_id": None,
                "status": "ae_failed", "ae_response": result,
                "fulfilled": False, "created_at": datetime.now(timezone.utc).isoformat()
            }

    except Exception as e:
        print(f"  [fulfillment] Exception placing order: {e}")
        orders[order_id] = {
            "order_number": order_num, "ae_order_id": None,
            "status": f"error: {e}", "fulfilled": False,
            "created_at": datetime.now(timezone.utc).isoformat()
        }

    _save_orders(orders)


# ── Tracking Sync ─────────────────────────────────────────────────────────────

def sync_tracking():
    """Check AliExpress tracking for all unfulfilled orders and update Shopify."""
    orders = _load_orders()
    pending = {k: v for k, v in orders.items() if not v.get("fulfilled") and v.get("ae_order_id")}

    if not pending:
        return

    print(f"\n[fulfillment] Checking tracking for {len(pending)} pending orders...")

    for shopify_order_id, record in pending.items():
        ae_order_id = record["ae_order_id"]
        try:
            result = ae_get_tracking(ae_order_id)
            tracking_info = (
                result
                .get("aliexpress_ds_order_tracking_info_query_response", {})
                .get("result", {})
            )

            tracking_number = tracking_info.get("logistics_no") or tracking_info.get("tracking_number")
            carrier = tracking_info.get("logistics_company") or tracking_info.get("carrier", "")

            if tracking_number:
                print(f"  [fulfillment] #{record['order_number']} tracking: {tracking_number} ({carrier})")
                fulfill_shopify_order(shopify_order_id, tracking_number, carrier)
                orders[shopify_order_id]["tracking_number"] = tracking_number
                orders[shopify_order_id]["carrier"] = carrier
                orders[shopify_order_id]["fulfilled"] = True
                _save_orders(orders)
            else:
                print(f"  [fulfillment] #{record['order_number']} — no tracking yet")

        except Exception as e:
            print(f"  [fulfillment] Tracking check error for #{record['order_number']}: {e}")


# ── Scheduler Entry ───────────────────────────────────────────────────────────

def run_fulfillment_check():
    """Called by scheduler every hour to sync tracking."""
    print(f"\n[ae-fulfillment] {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    sync_tracking()
    orders = _load_orders()
    pending = [v for v in orders.values() if not v.get("fulfilled")]
    print(f"  Pending: {len(pending)} | Total: {len(orders)}")


# ── CLI Helpers ───────────────────────────────────────────────────────────────

def search_cervical_pillow():
    """Search AliExpress for the cervical pillow — run this to find the product ID."""
    print("Searching AliExpress for cervical memory foam pillow...")
    result = ae_search_product("contour pillow neck cervical support memory foam")
    items = (result
        .get("aliexpress_ds_text_search_response", {})
        .get("data", {})
        .get("products", {})
        .get("selection_search_product", []))

    print(f"\nFound {len(items)} results:")
    for item in items[:8]:
        print(f"  ID: {item.get('itemId')} | {item.get('title','')[:60]}")
        print(f"      Price: ${item.get('targetSalePrice','?')} | Sold: {item.get('orders','?')}")
    return items


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "search":
        search_cervical_pillow()
    else:
        run_fulfillment_check()
