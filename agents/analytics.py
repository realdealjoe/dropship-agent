"""
Analytics Agent
Runs daily at 08:00 UTC and produces a plain-English report:
  - Revenue & profit (yesterday / last 7d / last 30d)
  - Best and worst selling products
  - Products to restock, reprice, or drop
  - Conversion funnel (sessions → orders)
  - Action items for the day
"""
import shopify_client as shopify
from agents.base_agent import BaseAgent
from datetime import datetime, timedelta, timezone
import json

SYSTEM = """You are the head of analytics for an electric vehicle accessories dropshipping store.

Your job is to analyse sales data and give the store owner a clear, actionable daily brief.

Format your report like this:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📊  DAILY STORE REPORT  –  {date}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💰 REVENUE
   Yesterday:  $X.XX   (X orders)
   Last 7d:    $X.XX   (X orders)
   Last 30d:   $X.XX   (X orders)

🏆 TOP SELLERS (last 7d)
   1. Product name — X units — $X.XX revenue

📉 WORST PERFORMERS
   Products with 0 orders in 14+ days

⚠️  ACTION ITEMS
   - [specific, numbered recommendations]

Be concise. Numbers first. Avoid filler words.
"""


class AnalyticsAgent(BaseAgent):
    name = "analytics"
    system_prompt = SYSTEM

    def __init__(self):
        super().__init__()
        self._register_tools()

    def _register_tools(self):
        self.register_tool({
            "name": "get_orders_in_range",
            "description": "Fetch orders placed within a date range",
            "input_schema": {
                "type": "object",
                "properties": {
                    "days_back": {"type": "integer", "description": "How many days back to fetch (e.g. 7, 30)"},
                },
                "required": ["days_back"],
            },
        }, self._get_orders)

        self.register_tool({
            "name": "get_all_products_performance",
            "description": "Get all products with their order counts and revenue",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_product_performance)

        self.register_tool({
            "name": "get_store_summary",
            "description": "Get high-level store stats: total products, total orders, total revenue",
            "input_schema": {"type": "object", "properties": {}},
        }, self._get_store_summary)

    def _get_orders(self, days_back: int = 7) -> dict:
        since = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
        import httpx
        from config import SHOPIFY_BASE_URL, SHOPIFY_ACCESS_TOKEN
        headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
        r = httpx.get(f"{SHOPIFY_BASE_URL}/orders.json",
                      headers=headers,
                      params={"status": "any", "created_at_min": since, "limit": 250,
                              "fields": "id,created_at,total_price,financial_status,line_items"},
                      timeout=30)
        orders = r.json().get("orders", [])
        total_revenue = sum(float(o.get("total_price", 0)) for o in orders)
        product_sales: dict = {}
        for o in orders:
            for item in o.get("line_items", []):
                pid = str(item.get("product_id", "unknown"))
                title = item.get("title", "Unknown")
                qty = item.get("quantity", 0)
                price = float(item.get("price", 0)) * qty
                if pid not in product_sales:
                    product_sales[pid] = {"title": title, "units": 0, "revenue": 0.0}
                product_sales[pid]["units"] += qty
                product_sales[pid]["revenue"] += price

        top = sorted(product_sales.values(), key=lambda x: x["revenue"], reverse=True)
        return {
            "period_days": days_back,
            "order_count": len(orders),
            "total_revenue": round(total_revenue, 2),
            "top_products": top[:10],
        }

    def _get_product_performance(self) -> dict:
        products = shopify.get_products(limit=250)
        # Pull last 30d orders for product-level analysis
        import httpx
        from config import SHOPIFY_BASE_URL, SHOPIFY_ACCESS_TOKEN
        since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
        r = httpx.get(f"{SHOPIFY_BASE_URL}/orders.json",
                      headers=headers,
                      params={"status": "any", "created_at_min": since, "limit": 250,
                              "fields": "line_items"},
                      timeout=30)
        orders = r.json().get("orders", [])
        sales: dict = {}
        for o in orders:
            for item in o.get("line_items", []):
                pid = str(item.get("product_id", ""))
                sales[pid] = sales.get(pid, 0) + item.get("quantity", 0)

        result = []
        for p in products:
            pid = str(p["id"])
            variant = p.get("variants", [{}])[0]
            result.append({
                "id": pid,
                "title": p.get("title", ""),
                "price": variant.get("price", "0"),
                "units_sold_30d": sales.get(pid, 0),
                "status": p.get("status", ""),
            })
        result.sort(key=lambda x: x["units_sold_30d"], reverse=True)
        return {"products": result}

    def _get_store_summary(self) -> dict:
        products = shopify.get_products(limit=250)
        import httpx
        from config import SHOPIFY_BASE_URL, SHOPIFY_ACCESS_TOKEN
        headers = {"X-Shopify-Access-Token": SHOPIFY_ACCESS_TOKEN}
        r = httpx.get(f"{SHOPIFY_BASE_URL}/orders/count.json",
                      headers=headers, params={"status": "any"}, timeout=15)
        order_count = r.json().get("count", 0)
        return {
            "total_products": len(products),
            "total_orders_all_time": order_count,
            "store": "Electric Accessories",
            "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

    def run_daily_report(self) -> str:
        today = datetime.now(timezone.utc).strftime("%A, %B %d %Y")
        task = (
            f"Generate the daily store report for {today}.\n"
            "Steps:\n"
            "1. Call get_store_summary for overall context\n"
            "2. Call get_orders_in_range with days_back=1 (yesterday)\n"
            "3. Call get_orders_in_range with days_back=7 (last week)\n"
            "4. Call get_orders_in_range with days_back=30 (last month)\n"
            "5. Call get_all_products_performance to identify top and bottom performers\n"
            "6. Write the formatted report with revenue figures, top sellers, worst performers, "
            "and 3–5 specific action items (e.g. 'Drop product X — 0 sales in 30d', "
            "'Source more dash cams — top seller this week').\n"
            "Print the full report at the end."
        )
        return self.run(task)
