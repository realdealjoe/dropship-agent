"""
Pricing Agent
Runs on a schedule to review and adjust product prices based on:
- Current supplier costs (margin protection)
- Competitor pricing (scraped from Google Shopping)
- Store-configured min/max rules
"""
import httpx
from bs4 import BeautifulSoup
from agents.base_agent import BaseAgent
import shopify_client as shopify
import database as db
from config import TARGET_MARGIN_PERCENT, MIN_PRICE, MAX_PRICE_MULTIPLIER

SYSTEM = f"""You are a pricing strategist for a dropshipping store.
Your goal is to maximise profit while staying competitive.

Rules:
- Minimum margin: {TARGET_MARGIN_PERCENT}%
- Minimum price: ${MIN_PRICE}
- Maximum price: cost × {MAX_PRICE_MULTIPLIER}x
- Round all prices to X.99
- Never price below cost + {TARGET_MARGIN_PERCENT}% margin
- If competitor price is available, price 5-10% below it (but never below min margin)
- Flag products that cannot be priced competitively (competitor undercuts our cost)
"""


class PricingAgent(BaseAgent):
    name = "pricing"
    system_prompt = SYSTEM

    def __init__(self):
        super().__init__()
        self._register_tools()

    def _register_tools(self):
        self.register_tool({
            "name": "get_all_tracked_products",
            "description": "Get all products in our store with their current Shopify prices and supplier costs",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_all_products)

        self.register_tool({
            "name": "get_competitor_price",
            "description": "Scrape Google Shopping for competitor prices for a product",
            "input_schema": {
                "type": "object",
                "properties": {
                    "product_title": {"type": "string"},
                },
                "required": ["product_title"],
            },
        }, self._get_competitor_price)

        self.register_tool({
            "name": "update_price",
            "description": "Update a product's price in Shopify",
            "input_schema": {
                "type": "object",
                "properties": {
                    "shopify_product_id": {"type": "string"},
                    "shopify_variant_id": {"type": "string"},
                    "new_price": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["shopify_product_id", "shopify_variant_id", "new_price"],
            },
        }, self._update_price)

    def _get_all_products(self) -> dict:
        db_products = {p["shopify_product_id"]: p for p in db.get_all_products()}
        shopify_products = shopify.get_products()
        merged = []
        for sp in shopify_products:
            pid = str(sp["id"])
            if pid not in db_products:
                continue
            variant = sp.get("variants", [{}])[0]
            merged.append({
                "shopify_product_id": pid,
                "shopify_variant_id": str(variant.get("id", "")),
                "title": sp.get("title", ""),
                "current_price": float(variant.get("price", 0)),
                "cost_price": db_products[pid]["cost_price"],
                "supplier": db_products[pid]["supplier"],
            })
        return {"products": merged}

    def _get_competitor_price(self, product_title: str) -> dict:
        try:
            query = product_title.replace(" ", "+")
            url = f"https://www.google.com/search?q={query}&tbm=shop"
            headers = {"User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )}
            r = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
            soup = BeautifulSoup(r.text, "lxml")
            prices = []
            for el in soup.select("[data-price]"):
                try:
                    prices.append(float(el["data-price"]))
                except (ValueError, KeyError):
                    pass
            if not prices:
                for el in soup.select(".a8Pemb"):
                    text = el.get_text().replace("$", "").replace(",", "").strip()
                    try:
                        prices.append(float(text))
                    except ValueError:
                        pass
            if prices:
                prices.sort()
                return {
                    "min_price": prices[0],
                    "median_price": prices[len(prices) // 2],
                    "sample_count": len(prices),
                }
            return {"error": "No prices found", "note": "Google may have blocked the scrape"}
        except Exception as e:
            return {"error": str(e)}

    def _update_price(self, shopify_product_id: str, shopify_variant_id: str,
                      new_price: float, reason: str = "") -> dict:
        rounded = round(new_price - 0.01, 0) + 0.99
        rounded = max(rounded, MIN_PRICE)
        try:
            shopify.update_product_price(shopify_product_id, shopify_variant_id, f"{rounded:.2f}")
            return {"success": True, "new_price": rounded, "reason": reason}
        except Exception as e:
            return {"error": str(e)}

    def run_repricing(self) -> str:
        task = (
            "Review all tracked products and reprice them optimally.\n"
            "For each product:\n"
            "1. Check current price vs supplier cost (ensure minimum margin)\n"
            "2. Get competitor prices from Google Shopping\n"
            "3. Calculate optimal price (competitive but above min margin)\n"
            "4. Update price in Shopify if it differs by more than $0.50\n"
            "5. Report all price changes and any products that can't be priced competitively"
        )
        return self.run(task)
