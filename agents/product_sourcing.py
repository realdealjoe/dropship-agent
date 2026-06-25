"""
Product Sourcing Agent
Searches suppliers for trending products, generates SEO descriptions with Claude,
and imports approved products into Shopify.
"""
import json
from agents.base_agent import BaseAgent
import shopify_client as shopify
import database as db
from suppliers.cj_dropshipping import CJDropshipping
from suppliers.aliexpress import AliExpress
from config import TARGET_MARGIN_PERCENT

SYSTEM = """You are a dropshipping product sourcing expert. Your job is to:
1. Search suppliers for products matching trending niches
2. Evaluate products based on margin potential, demand signals, and competition
3. Generate compelling, SEO-optimised product titles and descriptions
4. Import the best products into the Shopify store as drafts for review

Rules:
- Only source products with at least {margin}% margin potential
- Prefer products with supplier ratings > 4.0 and < 14 day shipping
- Generate descriptions that highlight benefits, not just features
- Set pricing at cost × {multiplier}x rounded to .99
- Import as DRAFT status so the owner can review before going live
""".format(margin=TARGET_MARGIN_PERCENT, multiplier=round(100 / (100 - TARGET_MARGIN_PERCENT), 2))


class ProductSourcingAgent(BaseAgent):
    name = "product_sourcing"
    system_prompt = SYSTEM

    def __init__(self):
        super().__init__()
        self._cj = CJDropshipping()
        self._ali = AliExpress()
        self._register_tools()

    def _register_tools(self):
        self.register_tool({
            "name": "search_cj_products",
            "description": "Search CJ Dropshipping for products by keyword",
            "input_schema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "max_price": {"type": "number", "description": "Max supplier cost in USD (default 100)"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["keyword"],
            },
        }, self._search_cj)

        self.register_tool({
            "name": "search_aliexpress_products",
            "description": "Search AliExpress for products by keyword",
            "input_schema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "max_price": {"type": "number"},
                    "limit": {"type": "integer", "default": 10},
                },
                "required": ["keyword"],
            },
        }, self._search_ali)

        self.register_tool({
            "name": "import_product_to_shopify",
            "description": "Import a product into Shopify and record the supplier mapping",
            "input_schema": {
                "type": "object",
                "properties": {
                    "supplier": {"type": "string", "enum": ["cj", "aliexpress"]},
                    "supplier_product_id": {"type": "string"},
                    "supplier_sku": {"type": "string"},
                    "cost_price": {"type": "number"},
                    "title": {"type": "string"},
                    "description_html": {"type": "string", "description": "SEO-rich HTML description"},
                    "sell_price": {"type": "number"},
                    "images": {"type": "array", "items": {"type": "string"}},
                    "product_type": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["supplier", "supplier_product_id", "supplier_sku",
                             "cost_price", "title", "description_html", "sell_price", "images"],
            },
        }, self._import_product)

        self.register_tool({
            "name": "get_active_niches",
            "description": "Get the list of product niches currently being targeted",
            "input_schema": {"type": "object", "properties": {}},
        }, lambda: {"niches": db.get_active_niches()})

    def _search_cj(self, keyword: str, max_price: float = 50.0, limit: int = 10):
        products = self._cj.search_products(keyword, max_price, limit)
        return {"products": [p.__dict__ for p in products]}

    def _search_ali(self, keyword: str, max_price: float = 50.0, limit: int = 10):
        products = self._ali.search_products(keyword, max_price, limit)
        return {"products": [p.__dict__ for p in products]}

    def _import_product(self, supplier: str, supplier_product_id: str,
                        supplier_sku: str, cost_price: float, title: str,
                        description_html: str, sell_price: float,
                        images: list[str], product_type: str = "",
                        tags: list[str] = None):
        tags = tags or []
        result = shopify.create_product(
            title=title,
            body_html=description_html,
            vendor=supplier.upper(),
            product_type=product_type,
            price=f"{sell_price:.2f}",
            cost=f"{cost_price:.2f}",
            images=images[:10],
            tags=tags,
        )
        product = result.get("product", {})
        shopify_id = str(product.get("id", ""))
        variant_id = str(product.get("variants", [{}])[0].get("id", ""))

        if shopify_id:
            db.upsert_product(shopify_id, variant_id, supplier,
                              supplier_product_id, supplier_sku, cost_price)

        return {
            "success": bool(shopify_id),
            "shopify_product_id": shopify_id,
            "shopify_url": f"https://{__import__('config').SHOPIFY_STORE_URL}/admin/products/{shopify_id}",
        }

    def source_products(self):
        niches = db.get_active_niches()
        if not niches:
            niches = ["trending gadgets", "home decor", "fitness accessories"]
        task = (
            f"Search for dropshipping products in these niches: {', '.join(niches)}. "
            "For each niche, search both CJ Dropshipping and AliExpress. "
            "Select the top 2-3 products per niche with the best margin potential. "
            "Generate compelling SEO product titles and HTML descriptions for each. "
            "Calculate sell prices at target margin and import them to Shopify as drafts. "
            "Report how many products were imported and any issues encountered."
        )
        return self.run(task)
