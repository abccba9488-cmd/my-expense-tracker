"""Single-stock, point-in-time backtest of the "甜蜜點" (sweet spot) signal
— the same four price/moving-average conditions app.js's sweetSpotCell()
uses to color a table cell red/yellow/green/purple — with a profit-target
exit rule and no pyramiding. Triggered on-demand from a stock's detail
page (see app.py's GET /api/stocks/<code>/backtest/sweet-spot) — unlike
backtest_gutai.py (a standalone, full-market, multiprocessed script), this
is scoped to one stock and cheap enough to run synchronously inside a
single Flask request.

History: started 2026-07-28 as a purple/ma240-only backtest that also
required experts.py's score_flag888_4() (888/巔峰 標準4「填息穩定」) to
pass. Both of those were dropped the same day per project owner request,
leaving just the ma240 condition; this revision (still 2026-07-28)
generalizes that single condition into all 4 sweetSpotCell() tiers
(ma20/60/120/240) so each can be backtested the same way, and renames the
module/route accordingly since "flag888_4" no longer describes anything
this file does.

Methodology
-----------
For each of the 4 tiers (ma20/60/120/240) × 5 profit targets (+10/15/20/25/30%
by default) — 20 independent simulations — walk the trailing `years`
(default 5) of weekly sample dates in order and simulate **no pyramiding**:
while a position from an earlier entry is still open (hasn't reached that
target yet), later weekly signals are ignored outright — not even
evaluated — and the next entry can only happen once that position has
closed (reached its target). This is why each (tier, target) pair ends up
with its own independent, non-overlapping trade log.

An entry fires at sample date T when the stock is in that tier's sweet-spot
zone — `_is_signal()`:
  - ma20/ma60/ma120 (symmetric, matching sweetSpotCell()'s red/yellow/green
    tiers): `abs(close - ma) / ma <= 0.03`
  - ma240 (asymmetric, matching sweetSpotCell()'s purple tier): price
    at/below ma240 (any distance below counts), or up to 3% above it —
    treating it as a "long-term value zone" rather than a support/
    resistance test, same as the live table cell
Each ma is computed point-in-time from whatever price history exists up to
and including T (same "last <=N closes, no minimum-count gate" window
_SUMMARY_SQL itself uses). Entry price = T's close. Exit = the first day
afterward the close reaches entry_price * (1 + target%); if that never
happens before the latest available price, the trade stays open
("持有中") and blocks all further entries for that (tier, target) pair for
the rest of the backtest (mirroring "you can't buy a second lot while
still holding" for real).

Caveats (same spirit as backtest_gutai.py):
- No trading cost / slippage.
- Raw (non dividend/split-adjusted) closing prices, same as every other
  price-based feature on this site.
- Survivorship bias N/A here (single already-tracked stock, not a market
  universe like backtest_gutai.py).

CLI usage (for local testing without going through the web UI):
    python backtest_sweet_spot.py 2330 --years 5
"""
import bisect
from datetime import date, datetime, timedelta

from sqlalchemy import text

DEFAULT_TARGETS = (10, 15, 20, 25, 30)

# key -> (lookback window, symmetric ±3% band?, Chinese label)
TIER_INFO = {
    'ma20':  {'window': 20,  'symmetric': True,  'label': '20日'},
    'ma60':  {'window': 60,  'symmetric': True,  'label': '60日'},
    'ma120': {'window': 120, 'symmetric': True,  'label': '120日'},
    'ma240': {'window': 240, 'symmetric': False, 'label': '240日'},
}
DEFAULT_TIERS = ('ma20', 'ma60', 'ma120', 'ma240')


def _to_date(d):
    if isinstance(d, date):
        return d
    return datetime.strptime(d, '%Y-%m-%d').date()


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _asof_idx(dates, as_of):
    """Index of the last entry with date <= as_of, or None."""
    i = bisect.bisect_right(dates, as_of) - 1
    return i if i >= 0 else None


def _ma_asof(closes, i, window):
    """Same window app.py's _SUMMARY_SQL uses for ma20/60/120/240 (last
    <=`window` closes up to and including index i — averages however many
    exist if the stock doesn't have `window` yet, no minimum-count gate,
    matching the live SQL's plain `ORDER BY date DESC LIMIT N`)."""
    slice_ = [c for c in closes[max(0, i - window + 1):i + 1] if c is not None]
    return sum(slice_) / len(slice_) if slice_ else None


def _is_signal(price, ma, symmetric):
    """Matches sweetSpotCell() in app.js exactly:
    - symmetric tiers (ma20/60/120, red/yellow/green): price within ±3% of
      the average — a support/resistance test.
    - asymmetric tier (ma240, purple): price at/below the average (any
      distance below counts), or up to 3% above it — a "long-term value
      zone" test, not a support/resistance test. See CLAUDE.md's 甜蜜點
      section for the full rationale."""
    if ma is None or ma <= 0:
        return False
    if symmetric:
        return abs(price - ma) / ma <= 0.03
    return (price - ma) / ma <= 0.03


def _load_prices(db, code, since):
    dates, closes = [], []
    for r in db.execute(text('''
        SELECT date, close FROM daily_prices
        WHERE stock_code = :code AND date >= :since ORDER BY date ASC
    '''), {'code': code, 'since': since}).mappings():
        dates.append(_to_date(r['date']))
        closes.append(r['close'])
    return dates, closes


def _run_one(dates, closes, sample_dates, window, symmetric, pct):
    """No-pyramiding simulation for one (tier, target) pair. Returns
    (summary_dict, trades_list_most_recent_first)."""
    n = len(closes)
    last_close = closes[-1]
    trades = []
    next_allowed_idx = 0  # can't enter again until any open position closes
    for T in sample_dates:
        i = _asof_idx(dates, T)
        if i is None or i < next_allowed_idx:
            continue
        price = closes[i]
        if not price:
            continue
        ma = _ma_asof(closes, i, window)
        if not _is_signal(price, ma, symmetric):
            continue

        target_price = price * (1 + pct / 100)
        hit_idx = None
        for j in range(i + 1, n):
            c = closes[j]
            if c is not None and c >= target_price:
                hit_idx = j
                break

        if hit_idx is not None:
            trades.append({
                'date': T.isoformat(), 'price': round(price, 2), 'ma': round(ma, 2),
                'hit': True, 'days': hit_idx - i, 'exit_date': dates[hit_idx].isoformat(),
            })
            next_allowed_idx = hit_idx + 1
        else:
            open_ret = (last_close / price - 1) * 100 if last_close else None
            trades.append({
                'date': T.isoformat(), 'price': round(price, 2), 'ma': round(ma, 2),
                'hit': False, 'open_return_pct': round(open_ret, 2) if open_ret is not None else None,
            })
            next_allowed_idx = n  # still holding, unresolved — blocks every remaining sample date

    n_entries = len(trades)
    n_hit = sum(1 for t in trades if t['hit'])
    days_list = [t['days'] for t in trades if t['hit']]
    open_rets = [t['open_return_pct'] for t in trades if not t['hit'] and t['open_return_pct'] is not None]
    summary = {
        'n_entries': n_entries,
        'n_hit': n_hit,
        'win_rate_pct': round(n_hit / n_entries * 100, 1) if n_entries else None,
        'avg_days_to_hit': round(_mean(days_list), 1) if days_list else None,
        'n_open': n_entries - n_hit,
        'avg_open_return_pct': round(_mean(open_rets), 2) if open_rets else None,
    }
    return summary, list(reversed(trades))  # most-recent-first


def run_backtest(db, code, years=5, targets=DEFAULT_TARGETS, tiers=DEFAULT_TIERS):
    """Returns None if there isn't enough price history to backtest
    anything meaningful; otherwise a JSON-serializable dict:
    {..., 'results': {tier: {'summary': {pct: {...}}, 'trades_by_target': {pct: [...]}}}}"""
    latest = db.execute(
        text('SELECT MAX(date) FROM daily_prices WHERE stock_code = :code'), {'code': code}
    ).scalar()
    if not latest:
        return None
    latest = _to_date(latest)

    price_since = latest - timedelta(days=years * 365 + 400)  # buffer so ma240 has room to look back
    dates, closes = _load_prices(db, code, price_since)
    if len(dates) < 60:
        return None

    weeks_back = years * 52
    sample_dates = [latest - timedelta(days=7 * w) for w in range(weeks_back, -1, -1)]

    results = {}
    for tier in tiers:
        info = TIER_INFO[tier]
        summary, trades_by_target = {}, {}
        for pct in targets:
            s, t = _run_one(dates, closes, sample_dates, info['window'], info['symmetric'], pct)
            summary[str(pct)] = s
            trades_by_target[str(pct)] = t
        results[tier] = {'summary': summary, 'trades_by_target': trades_by_target}

    return {
        'code': code,
        'years': years,
        'targets': list(targets),
        'tiers': list(tiers),
        'tier_labels': {k: TIER_INFO[k]['label'] for k in tiers},
        'results': results,
        'latest_price_date': latest.isoformat(),
    }


if __name__ == '__main__':
    import argparse
    import json
    import logging

    from database import SessionLocal

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')

    parser = argparse.ArgumentParser(description='回測 甜蜜點（20/60/120/240日）訊號 + 獲利目標出場、不加碼（單一股票）')
    parser.add_argument('code', help='股票代號，如 2330')
    parser.add_argument('--years', type=int, default=5)
    parser.add_argument('--targets', type=int, nargs='+', default=list(DEFAULT_TARGETS))
    parser.add_argument('--tiers', type=str, nargs='+', default=list(DEFAULT_TIERS))
    args = parser.parse_args()

    db = SessionLocal()
    try:
        result = run_backtest(db, args.code, years=args.years, targets=tuple(args.targets), tiers=tuple(args.tiers))
    finally:
        db.close()

    if result is None:
        print('資料不足，無法回測')
    else:
        summary_only = {tier: r['summary'] for tier, r in result['results'].items()}
        print(json.dumps({'code': result['code'], 'years': result['years'], 'results': summary_only},
                          ensure_ascii=False, indent=2))
