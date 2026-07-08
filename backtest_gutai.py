"""Backtest 股泰多方/空方訊號 (gutai_bull / gutai_bear) against historical
data. Standalone, read-only script — never writes to expert_scores and
does not touch experts.py's live _build_context() (used by the daily
production scoring job), so nothing here can affect live scoring.

Methodology
-----------
For each weekly sample date T going back N weeks, rebuild each tracked
stock's scoring context using ONLY data that would have been knowable by
T (monthly revenue / quarterly financials are gated by their real
disclosure deadlines; daily/weekly market data is simply bounded to
<= T), then call experts.py's own score_gutai_bull()/score_gutai_bear()
unmodified against that context. A stock enters the day's cohort when
passed=True AND score >= --min-score. Forward return = close price
--horizon trading days later vs. T's close, compared against the
equal-weighted average forward return of every stock with a valid
forward price on that date (the benchmark).

Performance note: technical indicators (EMA/MACD/RSI/KD, daily + weekly
+ monthly) are computed ONCE per stock over its full price history, not
recomputed per sample date — recomputing from scratch ~150 times per
stock (once per weekly sample) would be the same cost as ~150 full
compute_expert_scores() runs and take hours longer than necessary.

Known caveats (discussed with the project owner before building this):
- Survivorship bias: the stock universe is TODAY's `stocks` table, not a
  historical point-in-time universe — delisted stocks are invisible to
  this backtest.
- No trading cost / slippage modeled — pure close-to-close price return.
- 股泰's real TU/TM/TD formula is proprietary/undisclosed; this reuses
  experts.py's own approximation (technical.py), so the backtest
  inherits the same approximation the live scoring already carries.

Usage:
    python backtest_gutai.py --weeks-back 156 --horizon 20 --min-score 80
"""
import argparse
import bisect
import json
import logging
import multiprocessing as mp
import os
import statistics
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from database import SessionLocal
import technical
from experts import score_gutai_bull, score_gutai_bear, _mean

_TZ = ZoneInfo('Asia/Taipei')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def _to_date(d):
    if isinstance(d, date):
        return d
    return datetime.strptime(d, '%Y-%m-%d').date()


def _revenue_known_by(as_of, year, month):
    """Monthly revenue for (year, month) is legally due by the 10th of
    the following month (same disclosure-deadline convention already
    used elsewhere in this project) — anything after that date can
    safely assume the number was public."""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return as_of >= date(ny, nm, 10)


_QUARTER_DEADLINE = {1: (5, 15), 2: (8, 14), 3: (11, 14)}


def _quarter_known_by(as_of, year, quarter):
    """Same quarterly disclosure deadlines scheduler.py already encodes
    for _quarterly_job (Q4's deadline is Mar 31 of the following year)."""
    if quarter == 4:
        return as_of >= date(year + 1, 3, 31)
    m, d = _QUARTER_DEADLINE[quarter]
    return as_of >= date(year, m, d)


# ── bulk load ────────────────────────────────────────────────────────────────

def _load_prices(db, since):
    """{code: {'dates': [...], 'open': [...], 'high': [...], 'low': [...],
    'close': [...], 'volume': [...]}}, ascending by date."""
    out = {}
    res = db.execute(text('''
        SELECT stock_code, date, open, high, low, close, volume FROM daily_prices
        WHERE date >= :since ORDER BY stock_code, date ASC
    '''), {'since': since})
    for r in res.mappings():
        c = out.setdefault(r['stock_code'], {'dates': [], 'open': [], 'high': [], 'low': [], 'close': [], 'volume': []})
        c['dates'].append(_to_date(r['date']))
        c['open'].append(r['open'])
        c['high'].append(r['high'])
        c['low'].append(r['low'])
        c['close'].append(r['close'])
        c['volume'].append(r['volume'] or 0)
    return out


def _load_institutional(db, since):
    out = {}
    res = db.execute(text('''
        SELECT stock_code, date, foreign_buy, foreign_sell, trust_buy, trust_sell,
               dealer_buy, dealer_sell FROM institutional_trades
        WHERE date >= :since ORDER BY stock_code, date ASC
    '''), {'since': since})
    for r in res.mappings():
        out.setdefault(r['stock_code'], []).append({
            'date': _to_date(r['date']),
            'foreign_net': (r['foreign_buy'] or 0) - (r['foreign_sell'] or 0),
            'trust_net': (r['trust_buy'] or 0) - (r['trust_sell'] or 0),
            'dealer_net': (r['dealer_buy'] or 0) - (r['dealer_sell'] or 0),
        })
    return out


def _load_holding(db, since):
    out = {}
    res = db.execute(text('''
        SELECT stock_code, date, pct_1000up, pct_800up, pct_600up, pct_400up,
               pct_200down, pct_100down FROM holding_concentration
        WHERE date >= :since ORDER BY stock_code, date ASC
    '''), {'since': since})
    for r in res.mappings():
        out.setdefault(r['stock_code'], []).append({
            'date': _to_date(r['date']),
            'big_pct': _mean([r['pct_1000up'], r['pct_800up'], r['pct_600up'], r['pct_400up']]),
            'small_pct': _mean([r['pct_200down'], r['pct_100down']]),
        })
    return out


def _load_quarterly(db):
    """{code: [{'year','quarter','eps',... , '_known_by': date}, ...]} desc by period."""
    qf, fe = {}, {}
    for r in db.execute(text('''
        SELECT stock_code, year, quarter, revenue, operating_income, net_income, eps
        FROM quarterly_financials
    ''')).mappings():
        qf.setdefault(r['stock_code'], {})[(r['year'], r['quarter'])] = dict(r)
    for r in db.execute(text('''
        SELECT stock_code, year, quarter, inventories, accounts_receivable, current_assets,
               current_liabilities, liabilities, equity, total_assets, long_term_borrowings,
               capital_stock, gross_profit, cost_of_goods_sold, pretax_income,
               operating_cash_flow, interest_expense, capex
        FROM financial_extra
    ''')).mappings():
        fe.setdefault(r['stock_code'], {})[(r['year'], r['quarter'])] = dict(r)

    out = {}
    for code in set(qf) | set(fe):
        keys = sorted(set(qf.get(code, {})) | set(fe.get(code, {})), reverse=True)
        rows = []
        for key in keys:
            row = {'year': key[0], 'quarter': key[1]}
            row.update(qf.get(code, {}).get(key, {}))
            row.update({k: v for k, v in fe.get(code, {}).get(key, {}).items()
                        if k not in ('stock_code', 'year', 'quarter')})
            rows.append(row)
        out[code] = rows
    return out


def _load_revenue(db):
    out = {}
    for r in db.execute(text('''
        SELECT stock_code, year, month, revenue, revenue_yoy FROM monthly_revenue
        ORDER BY stock_code, year DESC, month DESC
    ''')).mappings():
        out.setdefault(r['stock_code'], []).append(dict(r))
    return out


# ── per-stock indicator precompute ──────────────────────────────────────────

def _precompute(price):
    """Compute daily/weekly/monthly indicator series ONCE for one stock's
    full price history. Returns a dict of arrays all aligned to their own
    date axis (price['dates'] for daily, separate axes for weekly/monthly)."""
    closes, highs, lows = price['close'], price['high'], price['low']
    n = len(closes)
    if n < 30 or any(v is None for v in closes):
        return None

    emas = {p: technical.ema_series(closes, p) for p in (3, 5, 8, 13)}
    macd_line, signal_line, hist = technical.macd_series(closes)
    rsi_d = technical.rsi_series(closes)
    k_d, d_d = technical.kd_series(highs, lows, closes)

    rows = [{'date': price['dates'][i], 'open': price['open'][i], 'high': highs[i],
              'low': lows[i], 'close': closes[i]} for i in range(n)]
    weekly = technical.resample(rows, 'W')
    monthly = technical.resample(rows, 'M')
    w_dates = [b['date'] for b in weekly]
    m_dates = [b['date'] for b in monthly]
    w_closes = [b['close'] for b in weekly]
    w_highs = [b['high'] for b in weekly]
    w_lows = [b['low'] for b in weekly]
    m_closes = [b['close'] for b in monthly]
    m_highs = [b['high'] for b in monthly]
    m_lows = [b['low'] for b in monthly]

    rsi_w = technical.rsi_series(w_closes) if len(w_closes) > 15 else [None] * len(w_closes)
    k_w, d_w = technical.kd_series(w_highs, w_lows, w_closes) if len(w_closes) > 9 else ([None] * len(w_closes), [None] * len(w_closes))
    k_m, d_m = technical.kd_series(m_highs, m_lows, m_closes) if len(m_closes) > 9 else ([None] * len(m_closes), [None] * len(m_closes))
    w_hist = technical.macd_series(w_closes)[2] if len(w_closes) > 35 else [None] * len(w_closes)

    ema_align = []
    for i in range(n):
        latest = {p: emas[p][i] for p in (3, 5, 8, 13)}
        ema_align.append(technical.ema_alignment(latest))

    return {
        'dates': price['dates'], 'close': closes, 'low': lows, 'volume': price['volume'],
        'emas': emas, 'macd_hist': hist, 'rsi_daily': rsi_d, 'k_daily': k_d, 'd_daily': d_d,
        'ema_align': ema_align,
        'w_dates': w_dates, 'w_lows': w_lows, 'rsi_weekly': rsi_w, 'k_weekly': k_w, 'd_weekly': d_w,
        'w_hist': w_hist,
        'm_dates': m_dates, 'm_lows': m_lows, 'k_monthly': k_m, 'd_monthly': d_m,
    }


def _asof_idx(dates, as_of):
    """Index of the last entry with date <= as_of, or None."""
    i = bisect.bisect_right(dates, as_of) - 1
    return i if i >= 0 else None


def _tech_asof(pre, as_of):
    """Reconstruct the same snapshot dict shape technical.snapshot() returns
    (see technical.py's docstring for field meaning) by indexing into the
    precomputed series at `as_of`, instead of recomputing from scratch."""
    i = _asof_idx(pre['dates'], as_of)
    if i is None or i < 29:
        return {}
    wi = _asof_idx(pre['w_dates'], as_of)
    mi = _asof_idx(pre['m_dates'], as_of)

    def g(arr, idx):
        return arr[idx] if idx is not None and idx < len(arr) else None

    latest_close = pre['close'][i]
    latest_emas = {p: pre['emas'][p][i] for p in (3, 5, 8, 13)}
    return {
        'close': latest_close,
        'ema_alignment': pre['ema_align'][i],
        'macd_hist': g(pre['macd_hist'], i),
        'macd_hist_prev': g(pre['macd_hist'], i - 1) if i > 0 else None,
        'macd_hist_weekly': g(pre['w_hist'], wi) if wi is not None else None,
        'rsi_daily': g(pre['rsi_daily'], i),
        'rsi_weekly': g(pre['rsi_weekly'], wi) if wi is not None else None,
        'k_weekly': g(pre['k_weekly'], wi) if wi is not None else None,
        'd_weekly': g(pre['d_weekly'], wi) if wi is not None else None,
        'k_monthly': g(pre['k_monthly'], mi) if mi is not None else None,
        'd_monthly': g(pre['d_monthly'], mi) if mi is not None else None,
        'ema13_support': (latest_close >= latest_emas[13]) if latest_emas[13] is not None else None,
        'week_low_4': min(pre['w_lows'][max(0, wi - 3):wi + 1]) if wi is not None and wi >= 3 else None,
        'month_low_20': min(pre['low'][max(0, i - 19):i + 1]) if i >= 19 else None,
    }


def _list_asof(rows, as_of, since, limit):
    """rows: ascending-by-date list of dicts with a 'date' key. Returns up
    to `limit` entries within [since, as_of], newest first — matching the
    ordering _build_context()'s live version reads via `ORDER BY date DESC`."""
    dates = [r['date'] for r in rows]
    hi = _asof_idx(dates, as_of)
    if hi is None:
        return []
    lo = bisect.bisect_left(dates, since)
    window = rows[lo:hi + 1]
    return list(reversed(window[-limit:]))


def _build_ctx_asof(code, as_of, price_pre, inst_by_code, hold_by_code, qf_by_code, rev_by_code):
    pre = price_pre.get(code)
    if pre is None:
        return None
    i = _asof_idx(pre['dates'], as_of)
    if i is None:
        return None

    vol_window = pre['volume'][max(0, i - 9):i + 1]
    avg_vol_10d = _mean(vol_window) if vol_window else None

    q_rows = qf_by_code.get(code, [])
    q_known = [r for r in q_rows if _quarter_known_by(as_of, r['year'], r['quarter'])][:40]

    rev_rows = rev_by_code.get(code, [])
    rev_known = [r for r in rev_rows if _revenue_known_by(as_of, r['year'], r['month'])]
    rev_yoy_recent = [x['revenue_yoy'] for x in rev_known[:2]]

    inst = _list_asof(inst_by_code.get(code, []), as_of, as_of - timedelta(days=12), 5)
    hold = _list_asof(hold_by_code.get(code, []), as_of, as_of - timedelta(days=45), 3)

    return {
        'close': pre['close'][i],
        'avg_vol_10d': avg_vol_10d,
        'q': q_known,
        'inst': inst,
        'hold': hold,
        'rev_yoy_recent': rev_yoy_recent,
        'tech': _tech_asof(pre, as_of),
    }


def _forward_return(pre, as_of, horizon):
    """Close price `horizon` trading days after as_of, vs as_of's own
    close — None if there isn't enough future data yet (this sample date
    is too recent relative to the backtest's most current price data)."""
    i = _asof_idx(pre['dates'], as_of)
    if i is None or i + horizon >= len(pre['close']):
        return None
    base = pre['close'][i]
    fwd = pre['close'][i + horizon]
    if not base:
        return None
    return (fwd / base - 1) * 100


# ── multiprocessing worker ──────────────────────────────────────────────────
# One weekly sample date's full-market scan is independent of every other
# date, so this is the parallelization axis: a Pool initializer stashes the
# (large, read-only) precomputed data as a global ONCE per worker process
# rather than re-pickling it on every task, then each task only ships a
# single date string across the process boundary.
_W = {}


def _init_worker(price_pre, inst_by_code, hold_by_code, qf_by_code, rev_by_code, horizon, min_score):
    _W.update(price_pre=price_pre, inst_by_code=inst_by_code, hold_by_code=hold_by_code,
              qf_by_code=qf_by_code, rev_by_code=rev_by_code, horizon=horizon, min_score=min_score)


def _process_date_worker(T_iso):
    T = date.fromisoformat(T_iso)
    price_pre, inst_by_code, hold_by_code, qf_by_code, rev_by_code, horizon, min_score = (
        _W['price_pre'], _W['inst_by_code'], _W['hold_by_code'], _W['qf_by_code'],
        _W['rev_by_code'], _W['horizon'], _W['min_score'])

    cohorts = {'gutai_bull': [], 'gutai_bear': []}
    all_fwd = []
    for code, pre in price_pre.items():
        ctx = _build_ctx_asof(code, T, price_pre, inst_by_code, hold_by_code, qf_by_code, rev_by_code)
        if ctx is None:
            continue
        fwd = _forward_return(pre, T, horizon)
        if fwd is not None:
            all_fwd.append(fwd)
        for key, fn in (('gutai_bull', score_gutai_bull), ('gutai_bear', score_gutai_bear)):
            try:
                passed, score, max_score, _ = fn(ctx)
            except Exception:
                continue
            if passed and score >= min_score:
                cohorts[key].append((code, score, fwd))

    bench = _mean(all_fwd)
    out = {'date': T_iso, 'benchmark': bench, 'gutai_bull': [], 'gutai_bear': []}
    for key in ('gutai_bull', 'gutai_bear'):
        for code, score, fwd in cohorts[key]:
            out[key].append({'date': T_iso, 'code': code, 'score': score, 'fwd_return': fwd, 'benchmark': bench})
    return out


def run_backtest(weeks_back, horizon, min_score, workers=None):
    db = SessionLocal()
    try:
        latest = db.execute(text('SELECT MAX(date) FROM daily_prices')).scalar()
        latest = _to_date(latest)
        lookback_start = latest - timedelta(days=weeks_back * 7 + 760)

        logger.info('Loading daily_prices from %s ...', lookback_start)
        prices = _load_prices(db, lookback_start)
        logger.info('Loaded %d stocks of price history', len(prices))

        inst_by_code = _load_institutional(db, lookback_start)
        hold_by_code = _load_holding(db, lookback_start)
        qf_by_code = _load_quarterly(db)
        rev_by_code = _load_revenue(db)

        logger.info('Precomputing technical indicators per stock ...')
        price_pre = {}
        for n, (code, p) in enumerate(prices.items(), 1):
            pre = _precompute(p)
            if pre is not None:
                price_pre[code] = pre
            if n % 500 == 0:
                logger.info('  precomputed %d/%d', n, len(prices))
        logger.info('Precompute done: %d stocks usable', len(price_pre))

        sample_dates = [latest - timedelta(days=7 * w) for w in range(weeks_back, -1, -1)]
        date_isos = [d.isoformat() for d in sample_dates]

        n_workers = workers or max(1, (os.cpu_count() or 4) - 2)
        logger.info('Scoring %d sample dates across %d worker processes ...', len(date_isos), n_workers)

        results = {'gutai_bull': [], 'gutai_bear': []}
        with mp.Pool(
            n_workers, initializer=_init_worker,
            initargs=(price_pre, inst_by_code, hold_by_code, qf_by_code, rev_by_code, horizon, min_score),
        ) as pool:
            for si, r in enumerate(pool.imap_unordered(_process_date_worker, date_isos), 1):
                results['gutai_bull'].extend(r['gutai_bull'])
                results['gutai_bear'].extend(r['gutai_bear'])
                if si % 10 == 0 or si == len(date_isos):
                    bench = r['benchmark']
                    logger.info('  sample %d/%d (%s): bull cohort=%d bear cohort=%d benchmark=%s',
                                si, len(date_isos), r['date'], len(r['gutai_bull']),
                                len(r['gutai_bear']), f'{bench:.2f}%' if bench is not None else 'n/a')

        return results
    finally:
        db.close()


def summarize(results, min_score):
    out = {}
    for key, rows in results.items():
        valid = [r for r in rows if r['fwd_return'] is not None]
        out[key] = {'min_score': min_score, 'n_signals_total': len(rows), 'n_with_forward_return': len(valid)}
        if valid:
            fwd = [r['fwd_return'] for r in valid]
            bench = [r['benchmark'] for r in valid if r['benchmark'] is not None]
            wins = sum(1 for r in valid if r['benchmark'] is not None and r['fwd_return'] > r['benchmark'])
            out[key].update({
                'avg_fwd_return_pct': round(statistics.mean(fwd), 2),
                'median_fwd_return_pct': round(statistics.median(fwd), 2),
                'avg_benchmark_pct': round(statistics.mean(bench), 2) if bench else None,
                'win_rate_vs_benchmark_pct': round(wins / len(valid) * 100, 1),
            })

        # score-bucket breakdown
        buckets = [(60, 79), (80, 89), (90, 200)]
        by_bucket = {}
        for lo, hi in buckets:
            b_rows = [r for r in valid if lo <= r['score'] <= hi]
            if b_rows:
                b_fwd = [r['fwd_return'] for r in b_rows]
                by_bucket[f'{lo}-{hi}'] = {
                    'n': len(b_rows),
                    'avg_fwd_return_pct': round(statistics.mean(b_fwd), 2),
                }
        out[key]['by_score_bucket'] = by_bucket
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backtest 股泰多方/空方訊號')
    parser.add_argument('--weeks-back', type=int, default=156, help='how many weeks of history to sample (default 156 = 3 years)')
    parser.add_argument('--horizon', type=int, default=20, help='forward trading days to measure return over (default 20)')
    parser.add_argument('--min-score', type=int, default=80, help='minimum score to count as a signal (default 80)')
    parser.add_argument('--workers', type=int, default=None, help='worker process count (default: CPU count - 2)')
    parser.add_argument('--out', type=str, default='backtest_gutai_result.json')
    args = parser.parse_args()

    results = run_backtest(args.weeks_back, args.horizon, args.min_score, args.workers)
    summary = summarize(results, args.min_score)

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({'summary': summary, 'raw': results}, f, ensure_ascii=False, indent=2)

    logger.info('Summary: %s', json.dumps(summary, ensure_ascii=False, indent=2))
    logger.info('Full results written to %s', args.out)
