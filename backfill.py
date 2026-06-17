"""
Historical data backfill script.
Usage:
    python backfill.py --prices              # daily prices only
    python backfill.py --revenue             # monthly revenue only
    python backfill.py --quarterly           # quarterly financials only
    python backfill.py --prices --revenue --quarterly  # all three
    python backfill.py --from-year 2020 --prices       # prices from 2020 only

Already-populated dates/months/quarters are skipped automatically.
Safe to stop (Ctrl+C) and restart — progress is preserved.
"""

import argparse
import logging
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

_TZ = ZoneInfo('Asia/Taipei')

from sqlalchemy import text

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
)
logger = logging.getLogger(__name__)


def backfill_prices(from_year: int):
    from database import SessionLocal
    import crawler

    db = SessionLocal()
    today = datetime.now(_TZ).date()
    d = date(from_year, 1, 1)
    skipped = crawled = 0

    logger.info('=== Daily prices %d → %d ===', from_year, today.year)
    while d <= today:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue

        count = db.execute(
            text('SELECT COUNT(*) FROM daily_prices WHERE date = :dt'),
            {'dt': d}
        ).scalar()

        if count and count > 0:
            skipped += 1
            d += timedelta(days=1)
            continue

        date_str = d.strftime('%Y%m%d')
        try:
            n = crawler.crawl_daily_prices(date_str)
            crawled += 1
            if n:
                logger.info('Prices %s: %d records (done=%d skip=%d)', date_str, n, crawled, skipped)
            else:
                logger.info('Prices %s: no data (holiday) (done=%d skip=%d)', date_str, crawled, skipped)
        except Exception as e:
            logger.warning('Prices %s failed: %s', date_str, e)

        time.sleep(1.2)
        d += timedelta(days=1)

    db.close()
    logger.info('Price backfill complete: crawled=%d skipped=%d', crawled, skipped)


def backfill_revenue(from_year: int):
    from database import SessionLocal
    import crawler

    db = SessionLocal()
    today = datetime.now(_TZ).date()
    skipped = crawled = 0

    logger.info('=== Monthly revenue %d → %d ===', from_year, today.year)
    for year in range(from_year, today.year + 1):
        for month in range(1, 13):
            if date(year, month, 1) > today:
                break

            count = db.execute(
                text('SELECT COUNT(*) FROM monthly_revenue WHERE year=:y AND month=:m'),
                {'y': year, 'm': month}
            ).scalar()

            # Skip if already have substantial data (>100 records means a real crawl happened)
            if count and count > 100:
                skipped += 1
                logger.info('Revenue %d/%02d: skip (%d records exist)', year, month, count)
                continue

            try:
                n = crawler.crawl_monthly_revenue(year, month)
                crawled += 1
                logger.info('Revenue %d/%02d: %d records (done=%d skip=%d)', year, month, n, crawled, skipped)
            except Exception as e:
                logger.warning('Revenue %d/%02d failed: %s', year, month, e)

            time.sleep(2.0)

    db.close()
    logger.info('Revenue backfill complete: crawled=%d skipped=%d', crawled, skipped)


def backfill_quarterly(from_year: int):
    from database import SessionLocal
    import crawler

    db = SessionLocal()
    today = datetime.now(_TZ).date()
    skipped = crawled = 0

    # Disclosure deadlines: quarter must be disclosed before we crawl it
    def disclosed(year, quarter):
        deadlines = {
            1: date(year, 5, 15),
            2: date(year, 8, 14),
            3: date(year, 11, 14),
            4: date(year + 1, 3, 31),
        }
        return deadlines[quarter] <= today

    logger.info('=== Quarterly financials %d → %d ===', from_year, today.year)
    for year in range(from_year, today.year + 1):
        for quarter in range(1, 5):
            if not disclosed(year, quarter):
                continue

            count = db.execute(
                text('SELECT COUNT(*) FROM quarterly_financials WHERE year=:y AND quarter=:q'),
                {'y': year, 'q': quarter}
            ).scalar()

            if count and count > 100:
                skipped += 1
                logger.info('Quarterly %dQ%d: skip (%d records exist)', year, quarter, count)
                continue

            try:
                n = crawler.crawl_quarterly_financials(year, quarter)
                crawled += 1
                logger.info('Quarterly %dQ%d: %d records (done=%d skip=%d)', year, quarter, n, crawled, skipped)
            except Exception as e:
                logger.warning('Quarterly %dQ%d failed: %s', year, quarter, e)

            time.sleep(2.0)

    db.close()
    logger.info('Quarterly backfill complete: crawled=%d skip=%d', crawled, skipped)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill historical stock data')
    parser.add_argument('--from-year', type=int, default=2011,
                        help='Start year (default: 2011; MOPS IFRS data reliable from 2013)')
    parser.add_argument('--prices',    action='store_true', help='Backfill daily prices')
    parser.add_argument('--revenue',   action='store_true', help='Backfill monthly revenue')
    parser.add_argument('--quarterly', action='store_true', help='Backfill quarterly financials')
    args = parser.parse_args()

    if not any([args.prices, args.revenue, args.quarterly]):
        parser.error('Specify at least one of: --prices  --revenue  --quarterly')

    logger.info('Backfill start: from_year=%d', args.from_year)

    if args.prices:
        backfill_prices(args.from_year)
    if args.revenue:
        backfill_revenue(args.from_year)
    if args.quarterly:
        backfill_quarterly(args.from_year)

    logger.info('All done.')
