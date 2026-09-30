"""One-time repair script (not part of regular crawler flow).

Bug: crawl_quarterly_financials() only subtracts Q1+Q2+Q3 from the raw MOPS
annual Q4 figure when ALL THREE rows already exist in the DB at crawl time.
If any of Q1/Q2/Q3 was missing back then (later filled in by backfill.py,
which skips years/quarters that already have a row and therefore never
revisits the already-stored Q4), the Q4 row is permanently stuck holding the
raw annual cumulative value instead of the individual quarter value.

Ground-truth verification (NOT a magnitude heuristic -- an earlier version of
this script used "is the subtracted candidate a plausible quarter value?"
which produces false positives whenever any of Q1/Q2/Q3 is negative, since
subtracting a negative number flips the sign and can land in a "plausible"
range purely by chance for already-correct data. Loss-making/cyclical
companies like 1326台化 triggered this. Do not reintroduce that approach.):

For each candidate row, re-fetch the CURRENT raw MOPS annual figure via
_mops_quarterly_one(code, roc_year, 4). If it is stored raw (bug), the raw
annual value will match the stored Q4 value exactly (since nothing was ever
subtracted from it). If Q4 was already correctly adjusted, the raw annual
value will differ from stored Q4 (because stored Q4 is the individual
quarter, not the annual sum). Only rows where this exact match holds are
corrected, using freshly-fetched-annual minus current Q1+Q2+Q3.

Run in --dry-run mode first (default) to review, then --apply to write.
"""
import argparse
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import crawler  # noqa: E402  (reuses _mops_quarterly_one + existing rate-limit/jitter/retry logic)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='write changes (default is dry-run)')
    ap.add_argument('--since-year', type=int, default=2013)
    ap.add_argument('--until-year', type=int, default=2100)
    args = ap.parse_args()

    conn = sqlite3.connect('data/stocks.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute('''
        SELECT q4.stock_code, q4.year,
               q1.revenue as q1_rev, q2.revenue as q2_rev, q3.revenue as q3_rev, q4.revenue as q4_rev,
               q1.operating_income as q1_oi, q2.operating_income as q2_oi, q3.operating_income as q3_oi, q4.operating_income as q4_oi,
               q1.net_income as q1_ni, q2.net_income as q2_ni, q3.net_income as q3_ni, q4.net_income as q4_ni,
               q1.eps as q1_eps, q2.eps as q2_eps, q3.eps as q3_eps, q4.eps as q4_eps
        FROM quarterly_financials q4
        JOIN quarterly_financials q1 ON q1.stock_code=q4.stock_code AND q1.year=q4.year AND q1.quarter=1
        JOIN quarterly_financials q2 ON q2.stock_code=q4.stock_code AND q2.year=q4.year AND q2.quarter=2
        JOIN quarterly_financials q3 ON q3.stock_code=q4.stock_code AND q3.year=q4.year AND q3.quarter=3
        WHERE q4.quarter=4 AND q4.year >= ? AND q4.year <= ?
    ''', (args.since_year, args.until_year))
    rows = c.fetchall()
    print(f'candidate rows (complete Q1-Q3 quartet, year>={args.since_year}): {len(rows)}', flush=True)

    fixes = []
    checked = 0
    errors = 0
    t0 = time.time()
    for r in rows:
        checked += 1
        roc_year = r['year'] - 1911
        try:
            fresh_rev, fresh_oi, fresh_ni, fresh_eps = crawler._mops_quarterly_one(r['stock_code'], roc_year, 4)
        except Exception as e:
            errors += 1
            print(f'  ERROR {r["stock_code"]} {r["year"]}: {e}', flush=True)
            continue

        update = {}
        pairs = [
            ('revenue', fresh_rev, r['q1_rev'], r['q2_rev'], r['q3_rev'], r['q4_rev']),
            ('operating_income', fresh_oi, r['q1_oi'], r['q2_oi'], r['q3_oi'], r['q4_oi']),
            ('net_income', fresh_ni, r['q1_ni'], r['q2_ni'], r['q3_ni'], r['q4_ni']),
            ('eps', fresh_eps, r['q1_eps'], r['q2_eps'], r['q3_eps'], r['q4_eps']),
        ]
        for field, fresh_annual, q1v, q2v, q3v, stored_q4 in pairs:
            if fresh_annual is None or q1v is None or q2v is None or q3v is None or stored_q4 is None:
                continue
            # bug signature: stored Q4 is EXACTLY the raw annual figure (never subtracted)
            still_raw = (fresh_annual == stored_q4) if field != 'eps' else (abs(fresh_annual - stored_q4) < 0.001)
            if still_raw:
                corrected = fresh_annual - q1v - q2v - q3v
                if field == 'eps':
                    corrected = round(corrected, 2)
                update[field] = (stored_q4, corrected)

        if update:
            fixes.append((r['stock_code'], r['year'], update))

        if checked % 100 == 0:
            elapsed = time.time() - t0
            print(f'  progress {checked}/{len(rows)}  fixes so far: {len(fixes)}  errors: {errors}  elapsed: {elapsed:.0f}s', flush=True)
        time.sleep(0.3)

    print(f'\ntotal checked: {checked}  errors: {errors}  confirmed bugs: {len(fixes)}')
    by_year = {}
    for code, year, _ in fixes:
        by_year[year] = by_year.get(year, 0) + 1
    for y in sorted(by_year):
        print(f'  {y}: {by_year[y]}')

    print('\nall confirmed fixes:')
    for code, year, update in fixes:
        parts = ', '.join(f'{f}: {old}->{new}' for f, (old, new) in update.items())
        print(f'  {code} {year}Q4  {parts}')

    if not args.apply:
        print('\nDRY RUN ONLY -- rerun with --apply to write changes.')
        return

    print('\nApplying fixes...')
    applied = 0
    for code, year, update in fixes:
        sets = ', '.join(f'{f} = ?' for f in update)
        params = [new for (_old, new) in update.values()]
        params += [code, year]
        c.execute(
            f'UPDATE quarterly_financials SET {sets}, updated_at = CURRENT_TIMESTAMP '
            f'WHERE stock_code = ? AND year = ? AND quarter = 4',
            params,
        )
        applied += 1
    conn.commit()
    print(f'Applied {applied} row corrections.')


if __name__ == '__main__':
    main()
