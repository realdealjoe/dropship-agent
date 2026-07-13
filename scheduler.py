"""
APScheduler jobs — all recurring agent tasks.

Schedule (all times UTC):
  00:00 daily     Analytics report
  01:00 Sunday    Trading self-review (before product sourcing)
  02:00 Sunday    Product sourcing
  03:00 Saturday  SEO pass
  06:00 daily     Repricing
  08:00 daily     Gambling — morning lines scan
  12:00 Tue/Fri   Social media posts
  13:35 Mon-Fri   Stock trading — morning session
  19:00 daily     Gambling — evening games scan
  19:45 Mon-Fri   Stock trading — end-of-day session
  */2 hours       Crypto trading
  */1 hour :30    Meme coin trading
  */4 hours       Order tracking sync
"""
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import database as db
import trading_db as tdb


# ── Dropshipping agents ───────────────────────────────────────────────────────

def _source_products():
    from agents.product_sourcing import ProductSourcingAgent
    agent = ProductSourcingAgent()
    print("\n[scheduler] Running product sourcing...")
    result = agent.source_products()
    print(f"[scheduler] Product sourcing done: {result[:200]}\n")


def _run_repricing():
    from agents.pricing import PricingAgent
    agent = PricingAgent()
    print("\n[scheduler] Running repricing...")
    result = agent.run_repricing()
    print(f"[scheduler] Repricing done: {result[:200]}\n")


def _sync_tracking():
    from agents.order_fulfillment import OrderFulfillmentAgent
    print("[scheduler] Syncing order tracking...")
    # TODO: pull open CJ orders from DB and update tracking in Shopify


def _run_seo():
    from agents.seo import SEOAgent
    agent = SEOAgent()
    print("\n[scheduler] Running weekly SEO pass...")
    result = agent.run_seo_pass()
    print(f"[scheduler] SEO done: {result[:200]}\n")


def _run_analytics():
    from agents.analytics import AnalyticsAgent
    agent = AnalyticsAgent()
    print("\n[scheduler] Running daily analytics report...")
    result = agent.run_daily_report()
    print(f"\n[scheduler] Analytics report:\n{result}\n")


def _run_social_media():
    from agents.social_media import SocialMediaAgent
    from config import INSTAGRAM_USER_ID, INSTAGRAM_ACCESS_TOKEN
    if not INSTAGRAM_USER_ID or not INSTAGRAM_ACCESS_TOKEN:
        print("[scheduler] Social media: Instagram credentials not set, skipping")
        return
    agent = SocialMediaAgent()
    print("\n[scheduler] Running social media post cycle...")
    result = agent.run_post_cycle()
    print(f"[scheduler] Social media done: {result[:200]}\n")


# ── Stock trading agent ───────────────────────────────────────────────────────

def _trading_morning():
    from config import ALPACA_API_KEY
    if not ALPACA_API_KEY:
        print("[scheduler] Trading: ALPACA_API_KEY not set, skipping")
        return
    from agents.stock_trading import StockTradingAgent
    agent = StockTradingAgent()
    print("\n[scheduler] Running morning trading session...")
    result = agent.run_morning_session()
    print(f"[scheduler] Morning session done:\n{result[:500]}\n")


def _trading_eod():
    from config import ALPACA_API_KEY
    if not ALPACA_API_KEY:
        return
    from agents.stock_trading import StockTradingAgent
    agent = StockTradingAgent()
    print("\n[scheduler] Running end-of-day trading session...")
    result = agent.run_eod_session()
    print(f"[scheduler] EOD session done:\n{result[:500]}\n")


def _trading_weekly_review():
    from config import ALPACA_API_KEY
    if not ALPACA_API_KEY:
        return
    from agents.trading_review import TradingReviewAgent
    agent = TradingReviewAgent()
    print("\n[scheduler] Running weekly trading self-review...")
    result = agent.run_weekly_review()
    print(f"[scheduler] Weekly review done:\n{result[:500]}\n")


# ── Gambling / Sports betting agent ──────────────────────────────────────────

def _gambling_session():
    from config import ODDS_API_KEY
    if not ODDS_API_KEY:
        print("[scheduler] Gambling: ODDS_API_KEY not set, skipping")
        return
    from agents.gambling_bot import run_gambling_session
    print("\n[scheduler] Running gambling session...")
    run_gambling_session()


def _kalshi_session():
    import os
    if not os.getenv("KALSHI_API_KEY"):
        return
    from agents.kalshi_bot import run_kalshi_session
    print("\n[scheduler] Running Kalshi session...")
    run_kalshi_session()


# ── Painless Sleep AliExpress fulfillment ────────────────────────────────────

def _ps_tracking_sync():
    from agents.alibaba_fulfillment_agent import run_fulfillment_check
    run_fulfillment_check()


# ── Brooke affiliate agent ───────────────────────────────────────────────────

def _brooke_affiliate_session():
    from agents.brooke_affiliate_agent import run_affiliate_session
    print("\n[scheduler] Running Brooke affiliate session...")
    run_affiliate_session()


# ── Meme coin trading agent ───────────────────────────────────────────────────

def _meme_session():
    from config import COINBASE_API_KEY_NAME
    if not COINBASE_API_KEY_NAME:
        return
    from agents.meme_trading import run_meme_session
    print("\n[scheduler] Running meme coin session...")
    run_meme_session()


# ── Crypto trading agent ─────────────────────────────────────────────────────

def _crypto_session():
    from config import COINBASE_API_KEY_NAME
    if not COINBASE_API_KEY_NAME:
        print("[scheduler] Crypto: COINBASE_API_KEY_NAME not set, skipping")
        return
    from agents.crypto_trading import run_crypto_session
    print("\n[scheduler] Running crypto trading session...")
    run_crypto_session()


# ── Build scheduler ───────────────────────────────────────────────────────────

def build_scheduler() -> BackgroundScheduler:
    # Initialize trading DB on startup
    tdb.init_trading_db()

    scheduler = BackgroundScheduler(timezone="UTC")

    # Dropshipping
    scheduler.add_job(_source_products,  CronTrigger(day_of_week="sun", hour=2),
                      id="product_sourcing",  replace_existing=True)
    scheduler.add_job(_run_repricing,    CronTrigger(hour=6),
                      id="repricing",         replace_existing=True)
    scheduler.add_job(_sync_tracking,    CronTrigger(hour="*/4"),
                      id="tracking_sync",     replace_existing=True)

    # SEO — weekly Saturday 03:00
    scheduler.add_job(_run_seo,          CronTrigger(day_of_week="sat", hour=3),
                      id="seo",               replace_existing=True)

    # Analytics — daily 00:00
    scheduler.add_job(_run_analytics,    CronTrigger(hour=0),
                      id="analytics",         replace_existing=True)

    # Social media — Tuesday and Friday at noon
    scheduler.add_job(_run_social_media, CronTrigger(day_of_week="tue,fri", hour=12),
                      id="social_media",      replace_existing=True)

    # Stock trading — market hours Mon-Fri
    # 13:35 UTC = 09:35 ET (after open)
    scheduler.add_job(_trading_morning,  CronTrigger(day_of_week="mon-fri", hour=13, minute=35),
                      id="trading_morning",   replace_existing=True)
    # 19:45 UTC = 15:45 ET (before close)
    scheduler.add_job(_trading_eod,      CronTrigger(day_of_week="mon-fri", hour=19, minute=45),
                      id="trading_eod",       replace_existing=True)

    # Trading self-review — Sunday 01:00 UTC (runs before product sourcing at 02:00)
    scheduler.add_job(_trading_weekly_review, CronTrigger(day_of_week="sun", hour=1),
                      id="trading_review",    replace_existing=True)

    # Crypto trading — every 2 hours, 24/7
    scheduler.add_job(_crypto_session,        CronTrigger(hour="*/2"),
                      id="crypto_trading",    replace_existing=True)

    # Meme coin trading — every hour, 24/7
    scheduler.add_job(_meme_session,          CronTrigger(minute=30),
                      id="meme_trading",      replace_existing=True)

    # Gambling — 5 sessions covering all major game windows (UTC):
    #   07:00 pre-European/morning lines
    #   12:00 midday — early afternoon kickoffs
    #   16:00 pre-US afternoon games
    #   19:00 US evening games / UEFA kickoffs
    #   23:00 late US games / overnight lines
    scheduler.add_job(_gambling_session, CronTrigger(hour=7),
                      id="gambling_0700", replace_existing=True)
    scheduler.add_job(_gambling_session, CronTrigger(hour=12),
                      id="gambling_1200", replace_existing=True)
    scheduler.add_job(_gambling_session, CronTrigger(hour=16),
                      id="gambling_1600", replace_existing=True)
    scheduler.add_job(_gambling_session, CronTrigger(hour=19),
                      id="gambling_1900", replace_existing=True)
    scheduler.add_job(_gambling_session, CronTrigger(hour=23),
                      id="gambling_2300", replace_existing=True)

    # Painless Sleep — check AliExpress tracking every 2 hours
    scheduler.add_job(_ps_tracking_sync, CronTrigger(minute="0", hour="*/2"),
                      id="ps_tracking_sync", replace_existing=True)

    # Brooke affiliate — every 30 min to catch new posts quickly
    scheduler.add_job(_brooke_affiliate_session, CronTrigger(minute="0,30"),
                      id="brooke_affiliate", replace_existing=True)

    # Kalshi — runs alongside Polymarket at same times
    scheduler.add_job(_kalshi_session, CronTrigger(hour=7),
                      id="kalshi_0700", replace_existing=True)
    scheduler.add_job(_kalshi_session, CronTrigger(hour=12),
                      id="kalshi_1200", replace_existing=True)
    scheduler.add_job(_kalshi_session, CronTrigger(hour=16),
                      id="kalshi_1600", replace_existing=True)
    scheduler.add_job(_kalshi_session, CronTrigger(hour=19),
                      id="kalshi_1900", replace_existing=True)
    scheduler.add_job(_kalshi_session, CronTrigger(hour=23),
                      id="kalshi_2300", replace_existing=True)

    return scheduler
