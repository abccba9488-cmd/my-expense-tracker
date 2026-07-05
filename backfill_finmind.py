"""
Historical backfill for the 達人選股 (expert stock-picking) FinMind data.
Mirrors backfill.py's style: already-populated dates/quarters are skipped
automatically, safe to Ctrl+C and restart.

Usage:
    python backfill_finmind.py --institutional --holding --financials --dividend --valuation
    python backfill_finmind.py --financials --from-year 2013

Requires the FINMIND_TOKEN environment variable to be set.
"""
import argparse
import logging
import time
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

_TZ = ZoneInfo('Asia/Taipei')

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def backfill_institutional(from_year: int):
    from database import SessionLocal
    import crawler

    db = SessionLocal()
    today = datetime.now(_TZ).date()
    d = date(from_year, 1, 1)
    skipped = crawled = 0

    logger.info('=== Institutional trades %d -> %d ===', from_year, today.year)
    while d <= today:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        count = db.execute(text('SELECT COUNT(*) FROM institutional_trades WHERE date=:d'), {'d': d}).scalar()
        if count and count > 100:
            skipped += 1
            d += timedelta(days=1)
            continue
        date_str = d.strftime('%Y%m%d')
        try:
            n = crawler.crawl_finmind_institutional(date_str)
            crawled += 1
            logger.info('Institutional %s: %d records (done=%d skip=%d)', date_str, n, crawled, skipped)
        except Exception as e:
            logger.warning('Institutional %s failed: %s', date_str, e)
        time.sleep(0.3)
        d += timedelta(days=1)

    db.close()
    logger.info('Institutional backfill complete: crawled=%d skipped=%d', crawled, skipped)


def backfill_valuation(from_year: int):
    from database import SessionLocal
    import crawler

    db = SessionLocal()
    today = datetime.now(_TZ).date()
    d = date(from_year, 1, 1)
    skipped = crawled = 0

    logger.info('=== PER/PBR valuation %d -> %d ===', from_year, today.year)
    while d <= today:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        count = db.execute(
            text('SELECT COUNT(*) FROM daily_prices WHERE date=:d AND per IS NOT NULL'), {'d': d}
        ).scalar()
        if count and count > 100:
            skipped += 1
            d += timedelta(days=1)
            continue
        date_str = d.strftime('%Y%m%d')
        try:
            n = crawler.crawl_finmind_valuation(date_str)
            crawled += 1
            logger.info('Valuation %s: %d records (done=%d skip=%d)', date_str, n, crawled, skipped)
        except Exception as e:
            logger.warning('Valuation %s failed: %s', date_str, e)
        time.sleep(0.3)
        d += timedelta(days=1)

    db.close()
    logger.info('Valuation backfill complete: crawled=%d skipped=%d', crawled, skipped)


def backfill_holding(from_year: int):
    from database import SessionLocal
    import crawler

    db = SessionLocal()
    today = datetime.now(_TZ).date()
    d = date(from_year, 1, 1)
    skipped = crawled = 0

    logger.info('=== Holding concentration %d -> %d ===', from_year, today.year)
    while d <= today:
        window_end = min(d + timedelta(days=6), today)
        count = db.execute(
            text('SELECT COUNT(*) FROM holding_concentration WHERE date BETWEEN :a AND :b'),
            {'a': d, 'b': window_end}
        ).scalar()
        if count and count > 100:
            skipped += 1
            d += timedelta(days=7)
            continue
        date_str = window_end.strftime('%Y%m%d')
        try:
            n = crawler.crawl_finmind_holding(date_str, lookback_days=6)
            crawled += 1
            logger.info('Holding %s: %d records (done=%d skip=%d)', date_str, n, crawled, skipped)
        except Exception as e:
            logger.warning('Holding %s failed: %s', date_str, e)
        time.sleep(0.3)
        d += timedelta(days=7)

    db.close()
    logger.info('Holding backfill complete: crawled=%d skipped=%d', crawled, skipped)


def backfill_dividend(from_year: int):
    from database import SessionLocal
    import crawler

    db = SessionLocal()
    today = datetime.now(_TZ).date()
    d = date(from_year, 1, 1)
    skipped = crawled = 0

    logger.info('=== Dividend policy + fill events %d -> %d ===', from_year, today.year)
    while d <= today:
        window_end = min(d + timedelta(days=6), today)
        count = db.execute(
            text('SELECT COUNT(*) FROM dividend_policy WHERE event_date BETWEEN :a AND :b'),
            {'a': d, 'b': window_end}
        ).scalar()
        date_str = window_end.strftime('%Y%m%d')
        if not (count and count > 20):
            try:
                n = crawler.crawl_finmind_dividend(date_str, lookback_days=6)
                crawled += 1
                logger.info('Dividend %s: %d records (done=%d skip=%d)', date_str, n, crawled, skipped)
            except Exception as e:
                logger.warning('Dividend %s failed: %s', date_str, e)
            time.sleep(0.3)
        else:
            skipped += 1

        count2 = db.execute(
            text('SELECT COUNT(*) FROM dividend_fill_events WHERE ex_date BETWEEN :a AND :b'),
            {'a': d, 'b': window_end}
        ).scalar()
        if not (count2 and count2 > 20):
            try:
                n2 = crawler.crawl_finmind_dividend_result(date_str, lookback_days=6)
                logger.info('DividendResult %s: %d events', date_str, n2)
            except Exception as e:
                logger.warning('DividendResult %s failed: %s', date_str, e)
            time.sleep(0.3)

        d += timedelta(days=7)

    db.close()
    logger.info('Dividend backfill complete: crawled=%d skipped=%d', crawled, skipped)


def backfill_financials(from_year: int):
    from database import SessionLocal
    import crawler

    db = SessionLocal()
    today = datetime.now(_TZ).date()
    skipped = crawled = 0

    def disclosed(year, quarter):
        deadlines = {1: date(year, 5, 15), 2: date(year, 8, 14),
                     3: date(year, 11, 14), 4: date(year + 1, 3, 31)}
        return deadlines[quarter] <= today

    logger.info('=== Financial extra (balance sheet/cash flow/gross margin) %d -> %d ===', from_year, today.year)
    for year in range(from_year, today.year + 1):
        for quarter in range(1, 5):
            if not disclosed(year, quarter):
                continue
            count = db.execute(
                text('SELECT COUNT(*) FROM financial_extra WHERE year=:y AND quarter=:q'),
                {'y': year, 'q': quarter}
            ).scalar()
            if count and count > 100:
                skipped += 1
                logger.info('Financials %dQ%d: skip (%d records exist)', year, quarter, count)
                continue
            try:
                n = crawler.crawl_finmind_financials(year, quarter)
                crawled += 1
                logger.info('Financials %dQ%d: %d records (done=%d skip=%d)', year, quarter, n, crawled, skipped)
            except Exception as e:
                logger.warning('Financials %dQ%d failed: %s', year, quarter, e)
            time.sleep(0.5)

    db.close()
    logger.info('Financials backfill complete: crawled=%d skipped=%d', crawled, skipped)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backfill FinMind data for 達人選股')
    parser.add_argument('--from-year', type=int, default=2013,
                        help='Start year (default: 2013, matches MOPS IFRS reliability floor)')
    parser.add_argument('--institutional', action='store_true', help='三大法人買賣超')
    parser.add_argument('--holding',       action='store_true', help='股權分散表（大戶/散戶持股）')
    parser.add_argument('--financials',    action='store_true', help='資產負債表/現金流量表/毛利')
    parser.add_argument('--dividend',      action='store_true', help='股利政策 + 填息事件')
    parser.add_argument('--valuation',     action='store_true', help='PER/PBR/殖利率')
    parser.add_argument('--all', action='store_true', help='全部一起跑')
    args = parser.parse_args()

    if not any([args.institutional, args.holding, args.financials, args.dividend, args.valuation, args.all]):
        parser.error('Specify at least one of: --institutional --holding --financials --dividend --valuation --all')

    logger.info('FinMind backfill start: from_year=%d', args.from_year)

    if args.institutional or args.all:
        backfill_institutional(args.from_year)
    if args.holding or args.all:
        backfill_holding(args.from_year)
    if args.financials or args.all:
        backfill_financials(args.from_year)
    if args.dividend or args.all:
        backfill_dividend(args.from_year)
    if args.valuation or args.all:
        backfill_valuation(args.from_year)

    logger.info('All done.')
