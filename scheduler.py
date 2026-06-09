import logging
from datetime import datetime
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)
_scheduler = BackgroundScheduler(timezone='Asia/Taipei')


def _daily_price_job():
    import crawler
    today = datetime.today().strftime('%Y%m%d')
    try:
        crawler.crawl_daily_prices(today)
    except Exception as e:
        logger.error('Daily price job failed: %s', e)


def _stock_list_job():
    import crawler
    try:
        crawler.crawl_stock_list()
    except Exception as e:
        logger.error('Stock list job failed: %s', e)


def _monthly_revenue_job():
    import crawler
    now = datetime.today()
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
    now = datetime.today()
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

    # Daily prices on weekdays at 14:00 and 15:00
    _scheduler.add_job(_daily_price_job, CronTrigger(day_of_week='mon-fri', hour=14, minute=0))
    _scheduler.add_job(_daily_price_job, CronTrigger(day_of_week='mon-fri', hour=15, minute=0))

    # Monthly revenue: days 1–10 of each month at 23:00
    _scheduler.add_job(_monthly_revenue_job, CronTrigger(day='1-10', hour=23, minute=0))

    # Quarterly financial reports — every day within the disclosure window at 23:00
    # Q1 (Jan–Mar): May  1–15
    _scheduler.add_job(lambda: _quarterly_job(1), CronTrigger(month=5,  day='1-15', hour=23, minute=0))
    # Q2 (Apr–Jun): Aug  1–14
    _scheduler.add_job(lambda: _quarterly_job(2), CronTrigger(month=8,  day='1-14', hour=23, minute=0))
    # Q3 (Jul–Sep): Nov  1–14
    _scheduler.add_job(lambda: _quarterly_job(3), CronTrigger(month=11, day='1-14', hour=23, minute=0))
    # Q4 (Oct–Dec): Mar  1–31 of the following year (deadline: Mar 31)
    _scheduler.add_job(lambda: _quarterly_job(4), CronTrigger(month=3,  day='1-31', hour=23, minute=0))

    _scheduler.start()
    logger.info('Scheduler started')


def shutdown():
    _scheduler.shutdown(wait=False)
