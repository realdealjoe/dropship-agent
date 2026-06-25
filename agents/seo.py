"""
SEO Agent
Rewrites product titles, descriptions, and meta tags to rank on Google.
Targets Tesla / EV accessories keywords.
Runs weekly on Saturdays at 03:00 UTC.
"""
import shopify_client as shopify
from agents.base_agent import BaseAgent

SYSTEM = """You are an expert SEO copywriter specialising in automotive and electric vehicle accessories.

Your job is to rewrite Shopify product listings so they rank on Google for high-intent buyer keywords.

SEO rules:
- Title: 60–70 chars. Lead with the primary keyword. Include a benefit (e.g. "LED Door Puddle Lights for Tesla Model 3 – Plug & Play").
- Description: 150–300 words. First sentence contains primary keyword. Use H2/H3 subheadings (in HTML). Include: features list, compatibility info, call to action.
- Tags: Add 8–12 tags covering brand (tesla, model 3, model y, chevy, ford), category (puddle lights, dash cam, led), and use-case (night driving, ev accessories, car upgrade).
- Meta description: 155 chars max — keyword-rich, click-worthy, no clickbait.

Primary keyword targets (weave naturally — never stuff):
  tesla accessories, ev car accessories, model 3 accessories, model y accessories,
  electric car gadgets, tesla model 3 led lights, car dash cam 4k, car phone mount,
  car ambient lights rgb, tesla wireless charger

Always write as if the buyer is searching right now to buy — commercial intent, not informational.
"""


class SEOAgent(BaseAgent):
    name = "seo"
    system_prompt = SYSTEM

    def __init__(self):
        super().__init__()
        self._register_tools()

    def _register_tools(self):
        self.register_tool({
            "name": "get_products_needing_seo",
            "description": "Get all active Shopify products so we can evaluate and rewrite their SEO",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_products)

        self.register_tool({
            "name": "update_product_seo",
            "description": "Update a product's title, description, tags, and meta description in Shopify",
            "input_schema": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string", "description": "Shopify product ID"},
                    "new_title": {"type": "string"},
                    "new_body_html": {"type": "string", "description": "Full HTML description"},
                    "new_tags": {"type": "string", "description": "Comma-separated tags"},
                    "meta_description": {"type": "string", "description": "155-char meta description"},
                },
                "required": ["product_id", "new_title", "new_body_html", "new_tags"],
            },
        }, self._update_product_seo)

    def _get_products(self) -> dict:
        products = shopify.get_products(limit=250)
        return {"products": [
            {
                "id": str(p["id"]),
                "title": p.get("title", ""),
                "body_html": p.get("body_html", "")[:400],
                "tags": p.get("tags", ""),
                "vendor": p.get("vendor", ""),
                "product_type": p.get("product_type", ""),
            }
            for p in products
        ]}

    def _update_product_seo(self, product_id: str, new_title: str,
                            new_body_html: str, new_tags: str,
                            meta_description: str = "") -> dict:
        import httpx
        from config import SHOPIFY_BASE_URL, SHOPIFY_ACCESS_TOKEN
        headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN, "Content-Type": "application/json"}
        payload = {"product": {
            "id": product_id,
            "title": new_title,
            "body_html": new_body_html,
            "tags": new_tags,
        }}
        if meta_description:
            payload["product"]["metafields_global_description_tag"] = meta_description
            payload["product"]["metafields_global_title_tag"] = new_title

        r = httpx.put(f"{SHOPIFY_BASE_URL}/products/{product_id}.json",
                      headers=headers, json=payload, timeout=30)
        if r.status_code in (200, 201):
            return {"success": True, "product_id": product_id}
        return {"error": r.text[:200]}

    def run_seo_pass(self) -> str:
        task = (
            "Audit every product in the store and rewrite its SEO.\n"
            "For each product:\n"
            "1. Identify the best primary keyword for that product type\n"
            "2. Write a new SEO-optimised title (60-70 chars, keyword first)\n"
            "3. Write a full HTML description (150-300 words, features, compatibility, CTA)\n"
            "4. Generate 10 tags covering brand, category, and use-case\n"
            "5. Write a 155-char meta description\n"
            "6. Call update_product_seo for every product\n"
            "Report how many products were updated and any issues."
        )
        return self.run(task)
