"""One-off repair for 15 (year, quarter) periods where financial_extra's
balance-sheet columns (current_assets/current_liabilities/liabilities/
equity/total_assets/inventories/accounts_receivable/long_term_borrowings/
capital_stock) are 0% populated across the whole tracked universe, while the
income-statement and cash-flow columns for the same periods are ~98%
populated as normal.

Root cause: crawl_finmind_financials() writes one row per stock_code found
in ANY of its three FinMind pivots (balance sheet / income statement / cash
flow) — so if the TaiwanStockBalanceSheet API call failed or returned empty
for a given period while the other two succeeded, rows still get written
with every balance-sheet field NULL. backfill_finmind.py's own skip guard
only checks "does financial_extra have >100 rows for this quarter", which
those rows satisfy, so a normal `python backfill_finmind.py --financials`
re-run silently skips these periods forever without this targeted repair.

crawl_finmind_financials() does `INSERT OR REPLACE`, so simply calling it
again for exactly these periods re-fetches and overwrites every column
(not just the missing ones) — safe and idempotent regardless of how many
times it's re-run.

Usage (run on Zeabur terminal — needs FINMIND_TOKEN + network):
    python backfill_missing_financial_extra.py
"""
import logging
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# Confirmed via `SELECT year, quarter, COUNT(current_assets) ... GROUP BY
# year, quarter` against the live DB on 2026-07-11 — every other quarter on
# record is ~98%+ populated; only these are exactly 0%.
MISSING_PERIODS = [
    (2013, 1), (2013, 2),
    (2016, 4),
    (2017, 3), (2017, 4),
    (2018, 1), (2018, 2), (2018, 3),
    (2019, 1), (2019, 2),
    (2022, 4),
    (2023, 3), (2023, 4),
    (2024, 1), (2024, 2),
]


def main():
    from database import SessionLocal
    from sqlalchemy import text
    import crawler

    db = SessionLocal()
    try:
        for year, quarter in MISSING_PERIODS:
            before = db.execute(text(
                'SELECT COUNT(current_assets) FROM financial_extra WHERE year=:y AND quarter=:q'
            ), {'y': year, 'q': quarter}).scalar()
            try:
                n = crawler.crawl_finmind_financials(year, quarter)
            except Exception as e:
                logger.warning('%dQ%d failed: %s', year, quarter, e)
                continue
            after = db.execute(text(
                'SELECT COUNT(current_assets) FROM financial_extra WHERE year=:y AND quarter=:q'
            ), {'y': year, 'q': quarter}).scalar()
            logger.info('%dQ%d: %d records written, balance-sheet rows %d -> %d',
                         year, quarter, n, before, after)
            time.sleep(0.5)
    finally:
        db.close()
    logger.info('Done.')


if __name__ == '__main__':
    main()
