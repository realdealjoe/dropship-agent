import os
from dotenv import load_dotenv

load_dotenv()

SHOPIFY_STORE_URL = os.environ["SHOPIFY_STORE_URL"]
SHOPIFY_ACCESS_TOKEN = os.environ["SHOPIFY_ACCESS_TOKEN"]
SHOPIFY_WEBHOOK_SECRET = os.getenv("SHOPIFY_WEBHOOK_SECRET", "")

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

CJ_EMAIL = os.getenv("CJ_EMAIL", "")
CJ_API_KEY = os.getenv("CJ_API_KEY", "")

TARGET_MARGIN_PERCENT = float(os.getenv("TARGET_MARGIN_PERCENT", "35"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "9.99"))
MAX_PRICE_MULTIPLIER = float(os.getenv("MAX_PRICE_MULTIPLIER", "3.0"))

WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8000"))

SHOPIFY_API_VERSION = "2024-10"
SHOPIFY_BASE_URL = f"https://{SHOPIFY_STORE_URL}/admin/api/{SHOPIFY_API_VERSION}"

# Alpaca (stock trading)
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() != "false"

# Instagram / Meta
INSTAGRAM_USER_ID = os.getenv("INSTAGRAM_USER_ID", "")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")

# Coinbase Advanced Trade (crypto)
COINBASE_API_KEY_NAME    = os.getenv("COINBASE_API_KEY_NAME", "")
COINBASE_API_PRIVATE_KEY = os.getenv("COINBASE_API_PRIVATE_KEY", "")
COINBASE_PAPER           = os.getenv("COINBASE_PAPER", "true").lower() != "false"
