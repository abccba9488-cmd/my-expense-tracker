"""Diagnostic (read-only, no DB writes), round 2: for a single stock with
data_id set, TaiwanStockBalanceSheet should behave like a normal FinMind
time-series query (unlike bulk mode, which only honors start_date). Query a
wide date range spanning each problem year to see what dates FinMind
actually has data under for that year — this tells us whether our
_QUARTER_END assumption (03-31/06-30/09-30/12-31) is simply the wrong date
for some years, or whether the data is missing entirely even for a single
well-covered stock (2330, TSMC).

Usage: python diagnose_balance_sheet2.py
"""
import finmind_client

# One (year, wide range) probe per problem cluster.
PROBES = [
    ('2013-01-01', '2013-12-31'),
    ('2016-01-01', '2016-12-31'),
    ('2017-01-01', '2017-12-31'),
    ('2018-01-01', '2018-12-31'),
    ('2019-01-01', '2019-12-31'),
    ('2022-01-01', '2022-12-31'),
    ('2023-01-01', '2023-12-31'),
    ('2024-01-01', '2024-12-31'),
]

for start, end in PROBES:
    rows = finmind_client.fetch('TaiwanStockBalanceSheet', start_date=start, end_date=end, data_id='2330')
    dates = sorted(set(r.get('date') for r in rows))
    print(f'--- 2330, {start[:4]}: {len(rows)} rows, dates found: {dates} ---')
