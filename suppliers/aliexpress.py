"""
AliExpress integration via the AliExpress Affiliate API.
Apply for access: https://portals.aliexpress.com/

Until you have API credentials this falls back to a stub so the
rest of the system keeps working; swap in real credentials to enable it.
"""
import httpx
import os
from suppliers.base_supplier import BaseSupplier, SupplierProduct, SupplierOrder

ALI_APP_KEY = os.getenv("ALIEXPRESS_APP_KEY", "")
ALI_APP_SECRET = os.getenv("ALIEXPRESS_APP_SECRET", "")
ALI_ACCESS_TOKEN = os.getenv("ALIEXPRESS_ACCESS_TOKEN", "")


class AliExpress(BaseSupplier):
    name = "aliexpress"

    def _is_configured(self) -> bool:
        return bool(ALI_APP_KEY and ALI_APP_SECRET and ALI_ACCESS_TOKEN)

    def search_products(self, keyword: str, max_price: float = 50.0,
                        limit: int = 10) -> list[SupplierProduct]:
        if not self._is_configured():
            print("[AliExpress] API not configured — returning empty results. "
                  "Set ALIEXPRESS_APP_KEY, ALIEXPRESS_APP_SECRET, ALIEXPRESS_ACCESS_TOKEN.")
            return []

        # AliExpress Affiliate API: affiliate.aliexpress.ds.product.search
        params = {
            "app_key": ALI_APP_KEY,
            "access_token": ALI_ACCESS_TOKEN,
            "keywords": keyword,
            "page_size": limit,
            "max_sale_price": int(max_price * 100),  # API uses cents
            "fields": "product_id,product_title,sale_price,product_main_image_url,shop_url",
        }
        r = httpx.get("https://api-sg.aliexpress.com/sync", params={
            "method": "aliexpress.affiliate.product.query",
            **params,
        }, timeout=20)
        r.raise_for_status()
        items = (r.json()
                  .get("aliexpress_affiliate_product_query_response", {})
                  .get("resp_result", {})
                  .get("result", {})
                  .get("products", {})
                  .get("product", []))

        results = []
        for p in items:
            price = float(p.get("sale_price", 0))
            if price > max_price:
                continue
            results.append(SupplierProduct(
                supplier=self.name,
                product_id=str(p["product_id"]),
                sku=str(p["product_id"]),
                title=p.get("product_title", ""),
                description="",
                price=price,
                images=[p.get("product_main_image_url", "")],
                shipping_days=14,
                category="",
            ))
        return results

    def place_order(self, product_id: str, sku: str, quantity: int,
                    shipping_address: dict) -> SupplierOrder:
        # AliExpress does not offer a programmatic order placement API for
        # standard sellers. Use DSers (dsers.com) — it provides a REST API
        # to submit AliExpress orders automatically once you connect your
        # AliExpress account there.
        raise NotImplementedError(
            "AliExpress direct order placement is not supported via API. "
            "Integrate DSers (dsers.com) for automated AliExpress ordering."
        )

    def get_order_status(self, supplier_order_id: str) -> SupplierOrder:
        raise NotImplementedError("Use DSers API to track AliExpress orders.")
