"""
Order Fulfillment Agent
Triggered by Shopify webhook on new orders. Routes each line item to the
correct supplier, places the supplier order, and updates Shopify with tracking.
"""
from agents.base_agent import BaseAgent
import shopify_client as shopify
import database as db
from suppliers.cj_dropshipping import CJDropshipping

SYSTEM = """You are an order fulfillment specialist for a dropshipping store.
When a new order arrives:
1. Look up which supplier carries each product in the order
2. Place orders with the correct supplier(s)
3. Update the Shopify order notes with fulfillment details
4. When tracking becomes available, add it to the Shopify order

Always confirm supplier order placement before updating Shopify.
Handle split orders (multiple suppliers) by placing separate supplier orders.
If a product has no supplier mapping, flag it in the order notes.
"""


class OrderFulfillmentAgent(BaseAgent):
    name = "order_fulfillment"
    system_prompt = SYSTEM

    def __init__(self):
        super().__init__()
        self._cj = CJDropshipping()
        self._register_tools()

    def _register_tools(self):
        self.register_tool({
            "name": "get_shopify_order",
            "description": "Fetch full order details from Shopify",
            "input_schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        }, lambda order_id: shopify.get_order(order_id))

        self.register_tool({
            "name": "get_product_supplier",
            "description": "Look up which supplier and supplier product ID maps to a Shopify product",
            "input_schema": {
                "type": "object",
                "properties": {"shopify_product_id": {"type": "string"}},
                "required": ["shopify_product_id"],
            },
        }, lambda shopify_product_id: db.get_product(shopify_product_id) or {"error": "No supplier mapping found"})

        self.register_tool({
            "name": "place_cj_order",
            "description": "Place a fulfillment order with CJ Dropshipping",
            "input_schema": {
                "type": "object",
                "properties": {
                    "product_id": {"type": "string"},
                    "sku": {"type": "string"},
                    "quantity": {"type": "integer"},
                    "shipping_address": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "phone": {"type": "string"},
                            "address1": {"type": "string"},
                            "address2": {"type": "string"},
                            "city": {"type": "string"},
                            "province": {"type": "string"},
                            "country": {"type": "string"},
                            "zip": {"type": "string"},
                        },
                    },
                },
                "required": ["product_id", "sku", "quantity", "shipping_address"],
            },
        }, self._place_cj_order)

        self.register_tool({
            "name": "update_order_record",
            "description": "Save fulfillment details to our database and update Shopify order notes",
            "input_schema": {
                "type": "object",
                "properties": {
                    "shopify_order_id": {"type": "string"},
                    "shopify_order_number": {"type": "string"},
                    "supplier": {"type": "string"},
                    "supplier_order_id": {"type": "string"},
                    "status": {"type": "string"},
                    "notes": {"type": "string"},
                },
                "required": ["shopify_order_id", "supplier", "supplier_order_id", "status"],
            },
        }, self._update_order_record)

        self.register_tool({
            "name": "add_tracking_to_order",
            "description": "Add shipping tracking to a Shopify order and notify the customer",
            "input_schema": {
                "type": "object",
                "properties": {
                    "shopify_order_id": {"type": "string"},
                    "tracking_number": {"type": "string"},
                    "tracking_url": {"type": "string"},
                    "carrier": {"type": "string"},
                },
                "required": ["shopify_order_id", "tracking_number"],
            },
        }, self._add_tracking)

        self.register_tool({
            "name": "get_cj_order_status",
            "description": "Check the current status and tracking of a CJ order",
            "input_schema": {
                "type": "object",
                "properties": {"supplier_order_id": {"type": "string"}},
                "required": ["supplier_order_id"],
            },
        }, lambda supplier_order_id: self._cj.get_order_status(supplier_order_id).__dict__)

    def _place_cj_order(self, product_id: str, sku: str, quantity: int,
                        shipping_address: dict):
        order = self._cj.place_order(product_id, sku, quantity, shipping_address)
        return order.__dict__

    def _update_order_record(self, shopify_order_id: str, supplier: str,
                             supplier_order_id: str, status: str,
                             shopify_order_number: str = "", notes: str = ""):
        db.upsert_order(
            shopify_order_id,
            shopify_order_number=shopify_order_number,
            supplier=supplier,
            supplier_order_id=supplier_order_id,
            status=status,
            notes=notes,
        )
        if notes:
            try:
                shopify.add_order_note(shopify_order_id, notes)
            except Exception as e:
                return {"db_saved": True, "shopify_note_error": str(e)}
        return {"db_saved": True, "shopify_updated": True}

    def _add_tracking(self, shopify_order_id: str, tracking_number: str,
                      tracking_url: str = "", carrier: str = ""):
        try:
            fulfillment_orders = shopify.get_fulfillment_orders(shopify_order_id)
            if not fulfillment_orders:
                return {"error": "No fulfillment orders found"}
            fo_id = str(fulfillment_orders[0]["id"])
            shopify.add_tracking(shopify_order_id, fo_id, tracking_number, tracking_url, carrier)
            db.upsert_order(shopify_order_id, tracking_number=tracking_number,
                            tracking_url=tracking_url, status="shipped")
            return {"success": True}
        except Exception as e:
            return {"error": str(e)}

    def fulfill_order(self, order_payload: dict) -> str:
        order_id = str(order_payload.get("id", ""))
        order_number = str(order_payload.get("order_number", ""))
        task = (
            f"Process and fulfill Shopify order #{order_number} (ID: {order_id}).\n"
            "Steps:\n"
            "1. Fetch the full order details\n"
            "2. For each line item, look up the supplier mapping\n"
            "3. Place supplier order(s) with the customer's shipping address\n"
            "4. Update the order record and Shopify notes with fulfillment details\n"
            "5. Report the outcome for each line item"
        )
        return self.run(task)

    def sync_tracking(self, shopify_order_id: str, supplier_order_id: str) -> str:
        task = (
            f"Check the CJ order status for supplier_order_id={supplier_order_id} "
            f"linked to Shopify order {shopify_order_id}. "
            "If tracking is available, add it to the Shopify order. "
            "Report the current status."
        )
        return self.run(task)
