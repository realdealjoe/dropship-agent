#!/usr/bin/env python3
"""
Dropship Agent — entry point.

Usage:
  python main.py serve          # Start webhook server + scheduler (production)
  python main.py source         # Run product sourcing once now
  python main.py reprice        # Run repricing once now
  python main.py fulfill <id>   # Manually fulfill a Shopify order by ID
  python main.py support        # Interactive customer support REPL
  python main.py setup          # Register Shopify webhooks
  python main.py niche add <kw> # Add a product niche to source
"""
import sys
import uvicorn
import database as db
from scheduler import build_scheduler


def _bootstrap_trading_history():
    """On first deploy, load 6 years of stock history if DB is empty."""
    import trading_db as tdb
    import sqlite3
    conn  = sqlite3.connect(tdb.DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    conn.close()
    if count == 0:
        print("[main] No price history found — loading 6 years of data from Alpaca...")
        try:
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))
            from scripts.load_history import load_all
            load_all()
        except Exception as e:
            print(f"[main] Warning: stock history load failed: {e}")
    else:
        print(f"[main] Price history ready ({count} rows)")


def _bootstrap_crypto_history():
    """On first deploy, load crypto history if no crypto symbols exist in DB."""
    import trading_db as tdb
    import sqlite3
    from config import COINBASE_API_KEY_NAME
    if not COINBASE_API_KEY_NAME:
        return
    conn  = sqlite3.connect(tdb.DB_PATH)
    count = conn.execute(
        "SELECT COUNT(*) FROM price_history WHERE symbol LIKE '%-USD'"
    ).fetchone()[0]
    conn.close()
    if count == 0:
        print("[main] No crypto history found — loading from Coinbase...")
        try:
            from scripts.load_crypto_history import main as load_crypto
            load_crypto()
        except Exception as e:
            print(f"[main] Warning: crypto history load failed: {e}")


def cmd_serve():
    db.init_db()
    import trading_db as tdb
    tdb.init_trading_db()
    _bootstrap_trading_history()
    _bootstrap_crypto_history()
    scheduler = build_scheduler()
    scheduler.start()
    print("[main] Scheduler started.")
    print("[main] Starting webhook server on port", end=" ")
    from config import WEBHOOK_PORT
    print(WEBHOOK_PORT)
    from webhooks.server import app
    uvicorn.run(app, host="0.0.0.0", port=WEBHOOK_PORT)


def cmd_source():
    db.init_db()
    from agents.product_sourcing import ProductSourcingAgent
    agent = ProductSourcingAgent()
    print("[main] Running product sourcing now...")
    result = agent.source_products()
    print(result)


def cmd_reprice():
    db.init_db()
    from agents.pricing import PricingAgent
    agent = PricingAgent()
    print("[main] Running repricing now...")
    result = agent.run_repricing()
    print(result)


def cmd_fulfill(order_id: str):
    db.init_db()
    import shopify_client as shopify
    from agents.order_fulfillment import OrderFulfillmentAgent
    agent = OrderFulfillmentAgent()
    order = shopify.get_order(order_id)
    if not order:
        print(f"[main] Order {order_id} not found.")
        return
    print(f"[main] Fulfilling order #{order.get('order_number')}...")
    result = agent.fulfill_order(order)
    print(result)


def cmd_support():
    db.init_db()
    from agents.customer_support import CustomerSupportAgent
    agent = CustomerSupportAgent()
    print("[main] Customer Support REPL (type 'quit' to exit)")
    name = input("Customer name: ").strip() or "Customer"
    email = input("Customer email: ").strip()
    order_id = input("Order ID (optional): ").strip()
    while True:
        message = input("\nCustomer message: ").strip()
        if message.lower() in ("quit", "exit", "q"):
            break
        result = agent.handle_message(name, email, message, order_id)
        reply = agent.extract_reply(result)
        print(f"\nAgent reply:\n{reply}\n")


def cmd_setup():
    from config import WEBHOOK_PORT
    import shopify_client as shopify
    base = input("Enter the public URL of this server (e.g. https://abc.ngrok.io): ").strip()
    topics = [
        ("orders/create", f"{base}/webhooks/orders/create"),
        ("customer_queries/create", f"{base}/webhooks/customer_queries/created"),
    ]
    existing = {w["topic"] for w in shopify.list_webhooks()}
    for topic, address in topics:
        if topic in existing:
            print(f"  [skip] {topic} already registered")
        else:
            shopify.register_webhook(topic, address)
            print(f"  [ok]   {topic} → {address}")


def cmd_niche(action: str, keyword: str = ""):
    db.init_db()
    if action == "add" and keyword:
        db.add_niche(keyword)
        print(f"[main] Added niche: {keyword}")
    elif action == "list":
        print("Active niches:", db.get_active_niches())
    else:
        print("Usage: python main.py niche add <keyword>  |  niche list")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] == "serve":
        cmd_serve()
    elif args[0] == "source":
        cmd_source()
    elif args[0] == "reprice":
        cmd_reprice()
    elif args[0] == "fulfill" and len(args) > 1:
        cmd_fulfill(args[1])
    elif args[0] == "support":
        cmd_support()
    elif args[0] == "setup":
        cmd_setup()
    elif args[0] == "niche" and len(args) > 1:
        cmd_niche(args[1], args[2] if len(args) > 2 else "")
    else:
        print(__doc__)
