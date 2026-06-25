"""
CJ Dropshipping API integration.
Docs: https://developers.cjdropshipping.com/
"""
import time
import httpx
from config import CJ_EMAIL, CJ_API_KEY
from suppliers.base_supplier import BaseSupplier, SupplierProduct, SupplierOrder

CJ_BASE = "https://developers.cjdropshipping.com/api2.0/v1"


class CJDropshipping(BaseSupplier):
    name = "cj"

    def __init__(self):
        self._token: str | None = None

    def _get(self, path: str, **kwargs) -> dict:
        for attempt in range(3):
            r = httpx.get(f"{CJ_BASE}{path}", headers=self._auth_header(), timeout=20, **kwargs)
            if r.status_code == 429:
                time.sleep(2)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError("CJ API rate limit exceeded after retries")

    def _auth_header(self) -> dict:
        if not self._token:
            r = httpx.post(f"{CJ_BASE}/authentication/getAccessToken", json={
                "apiKey": CJ_API_KEY,
            }, timeout=15)
            r.raise_for_status()
            data = r.json()
            if not data.get("result"):
                raise RuntimeError(f"CJ auth failed: {data.get('message')}")
            self._token = data["data"]["accessToken"]
        return {"CJ-Access-Token": self._token}

    def search_products(self, keyword: str, max_price: float = 50.0,
                        limit: int = 10) -> list[SupplierProduct]:
        time.sleep(1)  # respect 1 QPS rate limit
        data = self._get("/product/list", params={
            "productName": keyword, "pageNum": 1, "pageSize": limit,
        })
        items = data.get("data", {}).get("list", [])
        results = []
        for p in items:
            raw_price = str(p.get("sellPrice", "0"))
            price = float(raw_price.split("--")[0].strip())
            if price > max_price:
                continue
            image = p.get("productImage", "")
            results.append(SupplierProduct(
                supplier=self.name,
                product_id=p["pid"],
                sku=p.get("productSku", p["pid"]),
                title=p.get("productNameEn") or p.get("productName", ""),
                description="",
                price=price,
                images=[image] if image else [],
                shipping_days=7,
                category=p.get("categoryName", ""),
            ))
        return results

    def place_order(self, product_id: str, sku: str, quantity: int,
                    shipping_address: dict) -> SupplierOrder:
        payload = {
            "products": [{"vid": sku, "quantity": quantity}],
            "shippingAddress": {
                "name": shipping_address.get("name"),
                "phone": shipping_address.get("phone", ""),
                "address": shipping_address.get("address1"),
                "address2": shipping_address.get("address2", ""),
                "city": shipping_address.get("city"),
                "province": shipping_address.get("province"),
                "country": shipping_address.get("country"),
                "zip": shipping_address.get("zip"),
            },
            "shippingMethod": "CJPacket",
        }
        r = httpx.post(f"{CJ_BASE}/shopping/order/createOrder",
                       headers=self._auth_header(), json=payload, timeout=30)
        r.raise_for_status()
        data = r.json().get("data", {})
        return SupplierOrder(
            supplier_order_id=data.get("orderId", ""),
            status=data.get("orderStatus", "created"),
        )

    def get_order_status(self, supplier_order_id: str) -> SupplierOrder:
        r = httpx.get(f"{CJ_BASE}/shopping/order/getOrderDetail",
                      headers=self._auth_header(),
                      params={"orderId": supplier_order_id}, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", {})
        return SupplierOrder(
            supplier_order_id=supplier_order_id,
            status=data.get("orderStatus", "unknown"),
            tracking_number=data.get("trackNumber", ""),
            tracking_url=data.get("trackUrl", ""),
            carrier=data.get("shippingNameEn", ""),
        )
