"""
Social Media Agent
Automatically posts product content to Instagram and generates TikTok scripts.
Runs every Tuesday and Friday at 12:00 UTC (peak engagement windows).

Setup required:
  INSTAGRAM_USER_ID  — your Instagram Business Account ID
  INSTAGRAM_ACCESS_TOKEN — long-lived token from Facebook Graph API
  (See README for how to get these — takes ~10 min in Meta Business Suite)
"""
import random
import httpx
import shopify_client as shopify
from agents.base_agent import BaseAgent
from config import INSTAGRAM_USER_ID, INSTAGRAM_ACCESS_TOKEN

GRAPH_BASE = "https://graph.facebook.com/v19.0"

SYSTEM = """You are a social media manager for "Electric Accessories" — a premium EV and Tesla accessories brand.

Brand voice: confident, technical, aspirational. Think Tesla's own marketing style.
Audience: Tesla owners, EV enthusiasts, tech-forward car people aged 25–45.

For each product post you must produce:
1. Instagram caption (150–200 chars) — punchy opener, 1–2 sentences, 5–8 hashtags at the end
2. TikTok script (30-second hook format):
   - Hook (0–3s): Bold claim or visual description
   - Problem (3–10s): Pain point the product solves
   - Solution (10–25s): How the product fixes it, features
   - CTA (25–30s): "Link in bio" or "Comment LIGHTS and I'll DM you the link"
3. Hashtag set: mix of niche (#teslamodel3) and broad (#electriccar #evlife) — 8 tags total

Tone: Never salesy. Show the lifestyle. Let the product speak.
"""


class SocialMediaAgent(BaseAgent):
    name = "social_media"
    system_prompt = SYSTEM

    def __init__(self):
        super().__init__()
        self._register_tools()

    def _register_tools(self):
        self.register_tool({
            "name": "get_products_to_promote",
            "description": "Get a list of active products to choose from for today's post",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_products)

        self.register_tool({
            "name": "post_to_instagram",
            "description": "Post an image with caption to the Instagram Business account",
            "input_schema": {
                "type": "object",
                "properties": {
                    "image_url": {"type": "string", "description": "Public URL of the product image"},
                    "caption": {"type": "string", "description": "Instagram caption with hashtags"},
                },
                "required": ["image_url", "caption"],
            },
        }, self._post_instagram)

        self.register_tool({
            "name": "save_tiktok_script",
            "description": "Save a TikTok script to the content queue file for review",
            "input_schema": {
                "type": "object",
                "properties": {
                    "product_title": {"type": "string"},
                    "script": {"type": "string", "description": "Full 30-second TikTok script"},
                    "hashtags": {"type": "string"},
                },
                "required": ["product_title", "script"],
            },
        }, self._save_tiktok)

    def _get_products(self) -> dict:
        products = shopify.get_products(limit=50)
        result = []
        for p in products:
            imgs = p.get("images", [])
            img_url = imgs[0]["src"] if imgs else ""
            variant = p.get("variants", [{}])[0]
            result.append({
                "id": str(p["id"]),
                "title": p.get("title", ""),
                "price": variant.get("price", ""),
                "image_url": img_url,
                "tags": p.get("tags", ""),
            })
        # Shuffle so we don't always promote the same products
        random.shuffle(result)
        return {"products": result[:10]}

    def _post_instagram(self, image_url: str, caption: str) -> dict:
        if not INSTAGRAM_USER_ID or not INSTAGRAM_ACCESS_TOKEN:
            return {"error": "Instagram credentials not configured. Set INSTAGRAM_USER_ID and INSTAGRAM_ACCESS_TOKEN in .env"}

        # Step 1: Create media container
        r1 = httpx.post(
            f"{GRAPH_BASE}/{INSTAGRAM_USER_ID}/media",
            params={
                "image_url": image_url,
                "caption": caption,
                "access_token": INSTAGRAM_ACCESS_TOKEN,
            },
            timeout=30,
        )
        if r1.status_code != 200:
            return {"error": f"Container creation failed: {r1.text[:200]}"}

        container_id = r1.json().get("id")
        if not container_id:
            return {"error": "No container ID returned"}

        # Step 2: Publish
        r2 = httpx.post(
            f"{GRAPH_BASE}/{INSTAGRAM_USER_ID}/media_publish",
            params={"creation_id": container_id, "access_token": INSTAGRAM_ACCESS_TOKEN},
            timeout=30,
        )
        if r2.status_code == 200:
            return {"success": True, "post_id": r2.json().get("id"), "caption_preview": caption[:80]}
        return {"error": f"Publish failed: {r2.text[:200]}"}

    def _save_tiktok(self, product_title: str, script: str, hashtags: str = "") -> dict:
        import os
        from datetime import datetime, timezone
        queue_file = os.path.join(os.path.dirname(__file__), "..", "tiktok_queue.txt")
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        entry = (
            f"\n{'='*60}\n"
            f"📱 TIKTOK SCRIPT — {timestamp}\n"
            f"Product: {product_title}\n"
            f"{'='*60}\n"
            f"{script}\n\n"
            f"Hashtags: {hashtags}\n"
        )
        with open(queue_file, "a") as f:
            f.write(entry)
        return {"success": True, "saved_to": "tiktok_queue.txt"}

    def run_post_cycle(self) -> str:
        task = (
            "Run today's social media post cycle.\n"
            "1. Call get_products_to_promote — pick the 2 most visually interesting products "
            "(good image, compelling product — lights, cams, chargers work great)\n"
            "2. For each product:\n"
            "   a. Write an Instagram caption (punchy, brand voice, 5-8 hashtags)\n"
            "   b. Call post_to_instagram with the product's image_url and caption\n"
            "   c. Write a 30-second TikTok script using the hook/problem/solution/CTA format\n"
            "   d. Call save_tiktok_script to queue it\n"
            "3. Report what was posted and any errors."
        )
        return self.run(task)
