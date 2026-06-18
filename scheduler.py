import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
_TZ = ZoneInfo('Asia/Taipei')
_scheduler = BackgroundScheduler(timezone='Asia/Taipei')


def _daily_price_job():
    import crawler
    today = datetime.now(_TZ).strftime('%Y%m%d')
    try:
        crawler.crawl_daily_prices(today)
    except Exception as e:
        logger.error('Daily price job failed: %s', e)


def _daily_price_watchdog():
    """Every 30 min: if it's a weekday 14:00–17:00 Taipei and today's price is missing, crawl."""
    from database import SessionLocal
    from sqlalchemy import text
    now = datetime.now(_TZ)
    if now.weekday() >= 5 or not (14 <= now.hour < 17):
        return
    today_str = now.strftime('%Y%m%d')
    db = SessionLocal()
    try:
        done = db.execute(
            text("SELECT 1 FROM crawler_logs WHERE task='daily_price' AND status='success'"
                 " AND message LIKE :pat LIMIT 1"),
            {'pat': f'{today_str}:%'},
        ).first()
    finally:
        db.close()
    if not done:
        logger.info('Watchdog: no daily price for %s — triggering crawl', today_str)
        import crawler
        try:
            crawler.crawl_daily_prices(today_str)
        except Exception as e:
            logger.error('Watchdog crawl failed: %s', e)


def _stock_list_job():
    import crawler
    try:
        crawler.crawl_stock_list()
    except Exception as e:
        logger.error('Stock list job failed: %s', e)


def _monthly_revenue_job():
    import crawler
    now = datetime.now(_TZ)
    if now.month == 1:
        y, m = now.year - 1, 12
    else:
        y, m = now.year, now.month - 1
    try:
        crawler.crawl_monthly_revenue(y, m)
    except Exception as e:
        logger.error('Monthly revenue job failed: %s', e)


def _quarterly_job(quarter):
    import crawler
    now = datetime.now(_TZ)
    # Q1 by May 15  → year = now.year,   Q = 1
    # Q2 by Aug 14  → year = now.year,   Q = 2
    # Q3 by Nov 14  → year = now.year,   Q = 3
    # Q4 by Mar 31  → year = now.year-1, Q = 4  (runs Jan–Mar of following year)
    year = now.year if quarter != 4 else now.year - 1
    try:
        crawler.crawl_quarterly_financials(year, quarter)
    except Exception as e:
        logger.error('Quarterly Q%d job failed: %s', quarter, e)


def start():
    # Update stock list every Sunday at 01:00
    _scheduler.add_job(_stock_list_job, CronTrigger(day_of_week='sun', hour=1, minute=0))

    # Daily prices on weekdays at 14:00 and 15:00 (primary triggers)
    _scheduler.add_job(_daily_price_job, CronTrigger(day_of_week='mon-fri', hour=14, minute=0))
    _scheduler.add_job(_daily_price_job, CronTrigger(day_of_week='mon-fri', hour=15, minute=0))

    # Watchdog: every 30 min, fires immediately on startup
    _scheduler.add_job(_daily_price_watchdog, 'interval', minutes=30,
                       next_run_time=datetime.now(_TZ))

    # Monthly revenue: every day at 23:00 (some companies publish late, keep retrying)
    _scheduler.add_job(_monthly_revenue_job, CronTrigger(hour=23, minute=0))

    # Quarterly financial reports — every day of the disclosure month at 23:00
    # Q1 (Jan–Mar): all of May (deadline May 15)
    _scheduler.add_job(lambda: _quarterly_job(1), CronTrigger(month=5,  hour=23, minute=0))
    # Q2 (Apr–Jun): all of August (deadline Aug 14)
    _scheduler.add_job(lambda: _quarterly_job(2), CronTrigger(month=8,  hour=23, minute=0))
    # Q3 (Jul–Sep): all of November (deadline Nov 14)
    _scheduler.add_job(lambda: _quarterly_job(3), CronTrigger(month=11, hour=23, minute=0))
    # Q4 (Oct–Dec): all of March of the following year (deadline Mar 31)
    _scheduler.add_job(lambda: _quarterly_job(4), CronTrigger(month=3,  hour=23, minute=0))

    _scheduler.start()
    logger.info('Scheduler started')


def shutdown():
    _scheduler.shutdown(wait=False)
