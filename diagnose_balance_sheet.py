"""Diagnostic (read-only, no DB writes): inspect what FinMind's
TaiwanStockBalanceSheet actually returns for a handful of the periods that
came back with 0 balance-sheet rows even after a re-crawl, to tell apart
two possibilities:
  (a) the raw API response for that date is genuinely empty, vs
  (b) it returns rows, but with 'type' values that don't match _BS_KEYS,
      so crawler.py's _pivot() silently filters everything out.

RESULT (2026-07-11): every tested date came back with 0 raw rows — genuinely
empty API response, not a type-name filtering issue. See
diagnose_balance_sheet2.py for the follow-up that confirmed this is an
upstream FinMind data gap (see CLAUDE.md's "已知未修復問題").

Usage: python diagnose_balance_sheet.py
"""
import finmind_client

TEST_DATES = ['2013-03-31', '2018-03-31', '2019-03-31', '2022-12-31', '2024-03-31', '2024-06-30']
_BS_KEYS = ('Inventories', 'AccountsReceivableNet', 'CurrentAssets', 'CurrentLiabilities',
            'Liabilities', 'Equity', 'TotalAssets', 'LongtermBorrowings', 'CapitalStock')

for d in TEST_DATES:
    rows = finmind_client.fetch('TaiwanStockBalanceSheet', start_date=d, end_date=d)
    print(f'--- {d}: {len(rows)} raw rows ---')
    if not rows:
        print('  (empty response)')
        continue
    types_seen = sorted(set(r.get('type') for r in rows))[:40]
    print('  distinct types (up to 40):', types_seen)
    matched = sum(1 for r in rows if r.get('type') in _BS_KEYS)
    print('  rows matching _BS_KEYS:', matched, '/', len(rows))
    print('  sample row:', rows[0])
