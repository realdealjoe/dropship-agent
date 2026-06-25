"""
Customer Support Agent
Handles inbound support requests: order status, tracking, refunds, product questions.
Can be triggered via webhook (Shopify inbox) or called directly with a message.
"""
import json
from agents.base_agent import BaseAgent
import shopify_client as shopify
import database as db

SYSTEM = """You are a friendly and professional customer support agent for an online store.
You have access to order and tracking information to help customers.

Tone: warm, helpful, concise. Use the customer's name when you know it.

You can:
- Look up order status and tracking
- Process refunds for valid requests (damaged, not received after 30 days, wrong item)
- Provide accurate shipping timelines
- Answer product questions based on the product description

You cannot:
- Promise delivery dates you cannot verify
- Issue refunds for buyer's remorse on correctly delivered orders without manager approval (flag these)
- Modify orders after they've been sent to supplier

Always check the order status before responding to a status inquiry.
For refund requests, check order status first — only process if delivered or 30+ days shipped.
"""


class CustomerSupportAgent(BaseAgent):
    name = "customer_support"
    system_prompt = SYSTEM

    def __init__(self):
        super().__init__()
        self._register_tools()

    def _register_tools(self):
        self.register_tool({
            "name": "get_order_by_number",
            "description": "Look up a Shopify order by order number or customer email",
            "input_schema": {
                "type": "object",
                "properties": {
                    "order_number": {"type": "string", "description": "e.g. #1001"},
                    "customer_email": {"type": "string"},
                },
            },
        }, self._get_order_by_number)

        self.register_tool({
            "name": "get_fulfillment_status",
            "description": "Get fulfillment and tracking details for an order from our database",
            "input_schema": {
                "type": "object",
                "properties": {"shopify_order_id": {"type": "string"}},
                "required": ["shopify_order_id"],
            },
        }, lambda shopify_order_id: db.get_order(shopify_order_id) or {"status": "not_fulfilled_yet"})

        self.register_tool({
            "name": "process_refund",
            "description": "Initiate a full refund for an order in Shopify",
            "input_schema": {
                "type": "object",
                "properties": {
                    "shopify_order_id": {"type": "string"},
                    "reason": {"type": "string", "description": "Reason for the refund"},
                },
                "required": ["shopify_order_id", "reason"],
            },
        }, self._process_refund)

        self.register_tool({
            "name": "get_product_info",
            "description": "Fetch product details to answer customer questions",
            "input_schema": {
                "type": "object",
                "properties": {"shopify_product_id": {"type": "string"}},
                "required": ["shopify_product_id"],
            },
        }, lambda shopify_product_id: shopify.get_product(shopify_product_id))

        self.register_tool({
            "name": "flag_for_human_review",
            "description": "Flag a support case that needs human attention",
            "input_schema": {
                "type": "object",
                "properties": {
                    "shopify_order_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "customer_message": {"type": "string"},
                },
                "required": ["reason"],
            },
        }, self._flag_for_review)

    def _get_order_by_number(self, order_number: str = "", customer_email: str = "") -> dict:
        orders = shopify.get_orders(status="any", limit=50)
        if order_number:
            clean = order_number.lstrip("#")
            for o in orders:
                if str(o.get("order_number", "")) == clean or str(o.get("name", "")).lstrip("#") == clean:
                    return {"order": o}
        if customer_email:
            for o in orders:
                if (o.get("email") or "").lower() == customer_email.lower():
                    return {"order": o}
        return {"error": "Order not found"}

    def _process_refund(self, shopify_order_id: str, reason: str) -> dict:
        try:
            result = shopify.create_refund(shopify_order_id, reason)
            db.upsert_order(shopify_order_id, status="refunded", notes=f"Refunded: {reason}")
            return {"success": True, "refund": result.get("refund", {})}
        except Exception as e:
            return {"error": str(e)}

    def _flag_for_review(self, reason: str, shopify_order_id: str = "",
                         customer_message: str = "") -> dict:
        print(f"\n[!] HUMAN REVIEW NEEDED — Order {shopify_order_id}: {reason}")
        if shopify_order_id:
            db.upsert_order(shopify_order_id, status="needs_review",
                            notes=f"Flagged: {reason}")
        return {"flagged": True, "reason": reason}

    def handle_message(self, customer_name: str, customer_email: str,
                       message: str, order_id: str = "") -> str:
        context = {
            "customer_name": customer_name,
            "customer_email": customer_email,
            "shopify_order_id": order_id,
        }
        task = (
            f"A customer has sent the following message:\n\n"
            f"\"{message}\"\n\n"
            "Respond helpfully. Check order/tracking status if relevant. "
            "Draft a complete reply the customer will receive. "
            "If a refund is needed and justified, process it. "
            "End your response with the exact reply text formatted as:\n\n"
            "REPLY:\n<the reply to send to the customer>"
        )
        return self.run(task, context)

    def extract_reply(self, agent_output: str) -> str:
        if "REPLY:" in agent_output:
            return agent_output.split("REPLY:", 1)[1].strip()
        return agent_output
