import httpx
from config import SHOPIFY_BASE_URL, SHOPIFY_ACCESS_TOKEN

_headers = {
    "X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN,
    "Content-Type": "application/json",
}


def _get(path: str, params: dict = None) -> dict:
    r = httpx.get(f"{SHOPIFY_BASE_URL}{path}", headers=_headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, body: dict) -> dict:
    r = httpx.post(f"{SHOPIFY_BASE_URL}{path}", headers=_headers, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def _put(path: str, body: dict) -> dict:
    r = httpx.put(f"{SHOPIFY_BASE_URL}{path}", headers=_headers, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


# ── Products ──────────────────────────────────────────────────────────────────

def create_product(title: str, body_html: str, vendor: str,
                   product_type: str, price: str, cost: str,
                   images: list[str], tags: list[str]) -> dict:
    payload = {
        "product": {
            "title": title,
            "body_html": body_html,
            "vendor": vendor,
            "product_type": product_type,
            "tags": ", ".join(tags),
            "variants": [{"price": price, "cost": cost, "inventory_management": "shopify"}],
            "images": [{"src": url} for url in images],
            "status": "draft",
        }
    }
    return _post("/products.json", payload)


def update_product_price(product_id: str, variant_id: str, new_price: str) -> dict:
    return _put(f"/variants/{variant_id}.json", {"variant": {"id": variant_id, "price": new_price}})


def get_products(limit: int = 250) -> list[dict]:
    data = _get("/products.json", params={"limit": limit, "fields": "id,title,variants,tags"})
    return data.get("products", [])


def get_product(product_id: str) -> dict:
    return _get(f"/products/{product_id}.json").get("product", {})


# ── Orders ────────────────────────────────────────────────────────────────────

def get_order(order_id: str) -> dict:
    return _get(f"/orders/{order_id}.json").get("order", {})


def get_orders(status: str = "open", limit: int = 50) -> list[dict]:
    data = _get("/orders.json", params={"status": status, "limit": limit})
    return data.get("orders", [])


def add_order_note(order_id: str, note: str) -> dict:
    return _put(f"/orders/{order_id}.json", {"order": {"id": order_id, "note": note}})


def add_tracking(order_id: str, fulfillment_order_id: str,
                 tracking_number: str, tracking_url: str, company: str) -> dict:
    payload = {
        "fulfillment": {
            "line_items_by_fulfillment_order": [{"fulfillment_order_id": fulfillment_order_id}],
            "tracking_info": {
                "number": tracking_number,
                "url": tracking_url,
                "company": company,
            },
            "notify_customer": True,
        }
    }
    return _post(f"/fulfillments.json", payload)


def get_fulfillment_orders(order_id: str) -> list[dict]:
    data = _get(f"/orders/{order_id}/fulfillment_orders.json")
    return data.get("fulfillment_orders", [])


def create_refund(order_id: str, reason: str) -> dict:
    payload = {"refund": {"note": reason, "notify": True}}
    return _post(f"/orders/{order_id}/refunds.json", payload)


# ── Customer ──────────────────────────────────────────────────────────────────

def register_webhook(topic: str, address: str) -> dict:
    payload = {"webhook": {"topic": topic, "address": address, "format": "json"}}
    return _post("/webhooks.json", payload)


def list_webhooks() -> list[dict]:
    return _get("/webhooks.json").get("webhooks", [])
