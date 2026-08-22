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


def _monthly_revenue_watchdog():
    """Every 30 min from 23:00 to 06:00: if today's monthly revenue crawl
    hasn't reported success yet, trigger a fresh one — unless a run from
    today is still genuinely in progress (a 'running' log younger than 40
    min; the normal crawl takes ~10 min). Unlike the other crawl functions,
    crawl_monthly_revenue can get stuck 'running' forever with no failed log
    (e.g. the machine sleeps mid-crawl and the thread never wakes up cleanly
    on a dead connection) — this watchdog is the recovery path for that."""
    from database import SessionLocal
    from sqlalchemy import text
    now = datetime.now(_TZ)
    if not (now.hour >= 23 or now.hour < 6):
        return
    db = SessionLocal()
    try:
        done = db.execute(
            text("SELECT 1 FROM crawler_logs WHERE task='monthly_revenue' AND status='success'"
                 " AND date(created_at) = date('now', 'localtime') LIMIT 1")
        ).first()
        if done:
            return
        stuck = db.execute(
            text("SELECT 1 FROM crawler_logs WHERE task='monthly_revenue' AND status='running'"
                 " AND (julianday('now', 'localtime') - julianday(created_at)) * 1440 < 40"
                 " ORDER BY created_at DESC LIMIT 1")
        ).first()
    finally:
        db.close()
    if stuck:
        return
    logger.info('Watchdog: monthly revenue not completed today — triggering catch-up')
    _monthly_revenue_job()


def _quarterly_watchdog():
    """Same pattern as _monthly_revenue_watchdog, for the quarterly job —
    it loops over every stock too and can get stuck 'running' forever the
    same way. Only active during a disclosure month (mirrors the quarter
    each cron job in start() targets)."""
    from database import SessionLocal
    from sqlalchemy import text
    now = datetime.now(_TZ)
    quarter = {5: 1, 8: 2, 11: 3, 3: 4}.get(now.month)
    if quarter is None or not (now.hour >= 23 or now.hour < 6):
        return
    db = SessionLocal()
    try:
        done = db.execute(
            text("SELECT 1 FROM crawler_logs WHERE task='quarterly' AND status='success'"
                 " AND date(created_at) = date('now', 'localtime') LIMIT 1")
        ).first()
        if done:
            return
        stuck = db.execute(
            text("SELECT 1 FROM crawler_logs WHERE task='quarterly' AND status='running'"
                 " AND (julianday('now', 'localtime') - julianday(created_at)) * 1440 < 40"
                 " ORDER BY created_at DESC LIMIT 1")
        ).first()
    finally:
        db.close()
    if stuck:
        return
    logger.info('Watchdog: quarterly financials not completed today — triggering catch-up')
    _quarterly_job(quarter)


def _announcements_job():
    import crawler
    try:
        crawler.crawl_announcements()
    except Exception as e:
        logger.error('Announcements job failed: %s', e)


def _announcements_test_job():
    """TEMPORARY: crawl TODAY's announcements every 30 min so same-day
    self-disclosure filings show up without waiting for the 05:00 job
    (which only looks at the prior trading day). Remove once testing
    is done — ask before reverting to the daily-only schedule."""
    import crawler
    today_str = datetime.now(_TZ).strftime('%Y%m%d')
    try:
        crawler.crawl_announcements(today_str)
    except Exception as e:
        logger.error('Announcements test job failed: %s', e)


def _finmind_watchdog():
    """Every 30 min (weekday, 17:00 onward): if today's FinMind incremental
    crawl hasn't succeeded yet, trigger it. Mirrors _daily_price_watchdog's
    pattern — this project's normal fix for "process wasn't running at the
    scheduled cron time" (e.g. computer was off, or the autostart server got
    restarted after 17:00), which otherwise leaves institutional_trades/
    holding_concentration silently stale until the *next* day's 17:00 run."""
    from database import SessionLocal
    from sqlalchemy import text
    now = datetime.now(_TZ)
    if now.weekday() >= 5 or now.hour < 17:
        return
    today_str = now.strftime('%Y%m%d')
    db = SessionLocal()
    try:
        done = db.execute(
            text("SELECT 1 FROM crawler_logs WHERE task='finmind_institutional' AND status='success'"
                 " AND message LIKE :pat LIMIT 1"),
            {'pat': f'{today_str}:%'},
        ).first()
    finally:
        db.close()
    if not done:
        logger.info('Watchdog: FinMind data not yet updated for %s — triggering catch-up', today_str)
        _finmind_job()


def _finmind_job():
    """達人選股資料日增量：三大法人買賣超/股權分散/PER-PBR/股利政策/填息事件，
    跑完後重新計算 7 套達人的評分快取（expert_scores）。放在凌晨、股價/月營收/
    公告都跑完之後。"""
    import crawler
    import experts
    today = datetime.now(_TZ).strftime('%Y%m%d')
    for fn in (crawler.crawl_finmind_institutional, crawler.crawl_finmind_holding,
               crawler.crawl_finmind_valuation, crawler.crawl_finmind_dividend,
               crawler.crawl_finmind_dividend_result):
        try:
            fn(today)
        except Exception as e:
            logger.error('FinMind job step %s failed: %s', fn.__name__, e)
    try:
        # TWSE/TPEX OpenAPI, not FinMind — updates monthly upstream but cheap
        # enough (2 HTTP calls) to just refresh daily alongside everything else.
        crawler.crawl_director_holdings()
    except Exception as e:
        logger.error('crawl_director_holdings failed: %s', e)
    try:
        experts.compute_expert_scores()
    except Exception as e:
        logger.error('compute_expert_scores failed: %s', e)


def _taifex_job():
    """期權籌碼：期貨/選擇權每日行情＋十大交易人未沖銷部位＋TW VIX＋CNN
    Fear & Greed Index。這幾個 FinMind dataset 約16:30更新，17:00跟
    _finmind_job 同批次執行是安全的；CNN Fear & Greed 沒有更新時間限制
    （非官方API每次呼叫拿到即時值，見 crawler_fear_greed.py），排在這裡
    純粹是一起跑方便。三大法人期貨/選擇權買賣更新較晚，另外排在
    _taifex_institutional_job。"""
    import crawler_taifex as ct
    import crawler_fear_greed as cfg
    today = datetime.now(_TZ).strftime('%Y%m%d')
    for fn in (ct.crawl_finmind_taifex_futures_daily, ct.crawl_finmind_taifex_option_daily,
               ct.crawl_finmind_taifex_futures_large_traders, ct.crawl_finmind_taifex_option_large_traders,
               ct.crawl_finmind_taifex_option_vix, cfg.crawl_fear_greed_index):
        try:
            fn(today)
        except Exception as e:
            logger.error('Taifex job step %s failed: %s', fn.__name__, e)


def _taifex_institutional_job():
    """三大法人期貨/選擇權買賣（含小台/微台，用於算約當大台），獨立排程晚一
    點跑——FinMind TaiwanOptionInstitutionalInvestors 約16:00更新、
    TaiwanFuturesInstitutionalInvestors（大台/小台/微台皆同）約18:00更新，跟
    _taifex_job 的16:30批次分開，避免在資料還沒更新前就抓到空/舊資料。"""
    import crawler_taifex as ct
    today = datetime.now(_TZ).strftime('%Y%m%d')
    for fn in (ct.crawl_finmind_taifex_futures_institutional, ct.crawl_finmind_taifex_option_institutional,
               ct.crawl_finmind_taifex_futures_institutional_mini):
        try:
            fn(today)
        except Exception as e:
            logger.error('Taifex institutional job step %s failed: %s', fn.__name__, e)


def _taifex_all_job():
    """手動觸發用（POST /api/crawler/run/taifex_data）：一次跑完全部8個期權
    籌碼 dataset，不分開等18:30那個批次——手動觸發是使用者當下想看結果，不
    是排程情境，不需要顧慮資料源更新時間的先後。"""
    _taifex_job()
    _taifex_institutional_job()


def _broker_trades_job():
    """券商分點進出：對目前有出現在任一使用者自選清單的股票做日增量；若某檔
    股票完全沒有歷史資料（例如在這個功能上線前就已經被自選、或先前因
    FINMIND_TOKEN 失效而從未成功抓過），改成補 30 天歷史而不是只抓今天一
    天——不然這種「舊自選股」會一直卡在 0 筆，永遠等不到 app.py 新增自選時
    才會觸發的那次回補。放在 _finmind_job 之後，避開同一時間點打太多外部請求。"""
    import crawler
    from database import SessionLocal
    from database import BrokerTrade
    today = datetime.now(_TZ).strftime('%Y%m%d')
    db = SessionLocal()
    try:
        codes = crawler.watchlisted_stock_codes(db)
        has_data = {c for (c,) in db.query(BrokerTrade.stock_code).distinct().all()}
    finally:
        db.close()
    for code in codes:
        try:
            if code in has_data:
                crawler.crawl_broker_trades(today, code)
            else:
                crawler.backfill_broker_trades(code)
        except Exception as e:
            logger.error('Broker trades job failed for %s: %s', code, e)


def _finmind_financials_job(quarter):
    """資產負債表/現金流量表/毛利項目，跟官方季報同一批公告時間窗（同
    _quarterly_job 的揭露期限邏輯），比官方季報 job 晚 30 分鐘跑。"""
    import crawler
    now = datetime.now(_TZ)
    year = now.year if quarter != 4 else now.year - 1
    try:
        crawler.crawl_finmind_financials(year, quarter)
    except Exception as e:
        logger.error('FinMind financials Q%d job failed: %s', quarter, e)


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

    # Watchdog: every 30 min from 23:00–06:00 — recovers from the job getting
    # stuck 'running' forever (see _monthly_revenue_watchdog docstring).
    _scheduler.add_job(_monthly_revenue_watchdog, 'interval', minutes=30,
                       next_run_time=datetime.now(_TZ))

    # Announcements: weekdays at 05:00 (off-peak, prior-day post-close announcements)
    _scheduler.add_job(_announcements_job, CronTrigger(day_of_week='mon-fri', hour=5, minute=0))

    # TEMPORARY (testing): re-crawl TODAY's announcements every 30 min so
    # same-day filings show up quickly. Remove when testing is done — ask
    # before removing (re-enabled 2026-06-20 after the no-detail-fetch
    # rewrite made each run ~2-3s instead of 1h+).
    _scheduler.add_job(_announcements_test_job, 'interval', minutes=30,
                       next_run_time=datetime.now(_TZ))

    # Quarterly financial reports — every day of the disclosure month at 23:00
    # Q1 (Jan–Mar): all of May (deadline May 15)
    _scheduler.add_job(lambda: _quarterly_job(1), CronTrigger(month=5,  hour=23, minute=0))
    # Q2 (Apr–Jun): all of August (deadline Aug 14)
    _scheduler.add_job(lambda: _quarterly_job(2), CronTrigger(month=8,  hour=23, minute=0))
    # Q3 (Jul–Sep): all of November (deadline Nov 14)
    _scheduler.add_job(lambda: _quarterly_job(3), CronTrigger(month=11, hour=23, minute=0))
    # Q4 (Oct–Dec): all of March of the following year (deadline Mar 31)
    _scheduler.add_job(lambda: _quarterly_job(4), CronTrigger(month=3,  hour=23, minute=0))

    # Watchdog: every 30 min from 23:00–06:00 during a disclosure month —
    # same stuck-'running'-forever recovery as _monthly_revenue_watchdog.
    _scheduler.add_job(_quarterly_watchdog, 'interval', minutes=30,
                       next_run_time=datetime.now(_TZ))

    # 達人選股 (FinMind): daily incremental crawl + score recompute, 17:00 —
    # after the day's price crawls (14:00/15:00) and watchdog window.
    _scheduler.add_job(_finmind_job, CronTrigger(hour=17, minute=0))

    # Watchdog: every 30 min from 17:00 onward, fires immediately on startup
    # too — catches "the process wasn't running at 17:00" the same way
    # _daily_price_watchdog does for daily_price.
    _scheduler.add_job(_finmind_watchdog, 'interval', minutes=30,
                       next_run_time=datetime.now(_TZ))

    # 券商分點進出：只抓自選股，17:30（達人選股 FinMind job 之後）
    _scheduler.add_job(_broker_trades_job, CronTrigger(day_of_week='mon-fri', hour=17, minute=30))

    # 期權籌碼：期貨/選擇權每日行情＋十大交易人，17:00（跟達人選股 FinMind
    # 同批次，資料約16:30更新，17:00執行安全）
    _scheduler.add_job(_taifex_job, CronTrigger(day_of_week='mon-fri', hour=17, minute=0))

    # 期權籌碼：三大法人期貨/選擇權買賣，18:30（該兩個dataset分別約16:00/
    # 18:00才更新，晚一點跑避免抓空）
    _scheduler.add_job(_taifex_institutional_job, CronTrigger(day_of_week='mon-fri', hour=18, minute=30))

    # 達人選股 financial_extra: same disclosure-month cadence as the official
    # quarterly job, 30 min later.
    _scheduler.add_job(lambda: _finmind_financials_job(1), CronTrigger(month=5,  hour=23, minute=30))
    _scheduler.add_job(lambda: _finmind_financials_job(2), CronTrigger(month=8,  hour=23, minute=30))
    _scheduler.add_job(lambda: _finmind_financials_job(3), CronTrigger(month=11, hour=23, minute=30))
    _scheduler.add_job(lambda: _finmind_financials_job(4), CronTrigger(month=3,  hour=23, minute=30))

    _scheduler.start()
    logger.info('Scheduler started')


def shutdown():
    _scheduler.shutdown(wait=False)
