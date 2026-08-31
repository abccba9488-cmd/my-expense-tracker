"""Backtest 10 套非纏論達人選股規則（gutai_bull/bear、flag888_1~4、guyu、
laoniu、haogongsi、momentum_guard），套用跟 backtest_chanlun_chippeak.py
完全相同的「訊號進場＋移動停利/固定停損出場」框架，讓使用者可以把全部
達人選股規則放進同一張表比較。chanlun_buy／chanlun_star 已經在
backtest_chanlun_chippeak.py 用這套框架跑過（chanlun／chanlun_star 兩個
tier），這裡不重跑，最終比較表直接合併那份結果的數字（見本檔 __main__
最後的合併輸出）；chanlun_sell 是賣出訊號，不適用「進場+移動停利」的
多方交易框架，排除在比較之外。

方法
----
1. **Point-in-time context 重用 backtest_gutai.py 已驗證的架構**：技術指標
   （EMA/MACD/RSI/KD）用 `_precompute()` 對每支股票的完整價格歷史算「一次」，
   再用 `_tech_asof()` 對任一 as_of 日期做索引取值——這個優化成立的前提是
   這些指標都是「純因果遞迴」（第i天的值只取決於[0..i]，不會因為之後多算
   幾天而改變），跟纏論的筆/中樞判斷不同（那個每次都是拿當下歷史從頭重新
   判斷全部筆的位置，同一段歷史用不同截止日重算會得到不同結果，所以
   backtest_chanlun_chippeak.py 才需要真的逐日呼叫 compute_chanlun()）。
2. **改成逐日取樣**（不是 backtest_gutai.py 原本的逐週），提高跟纏論那份
   逐日回測的可比性——這裡10套規則都不含纏論，沒有 backtest_gutai.py
   當初選擇逐週的效能顧慮（那份省的是「技術指標」這塊，這裡技術指標一樣
   用索引取值，逐日成本增加的只是季報/月營收/持股等 point-in-time 過濾，
   相對便宜）。
3. **進場條件**＝該規則 `score_*(ctx)` 回傳 `passed=True`（ScoreCard 的
   `require()` 選股門檻全部通過，不額外設最低分數）→ 當天收盤價進場。
   沒有持倉時才檢查新訊號（no-pyramiding，比照 chanlun 那份）。
4. **出場條件**：跟 backtest_chanlun_chippeak.py 完全一致的移動停利/固定
   停損（見該檔 `simulate_stock()` docstring 的完整說明，這裡不重複貼）。
5. **10套規則各自獨立完整模擬一輪**，同一天用同一份 point-in-time ctx
   評估全部10套（ctx 只在「至少一套規則空手」時才建構，持倉中的規則不
   需要 ctx，比照 chanlun 那份的持倉期間省算優化）。

Point-in-time 資料來源與簡化（除了繼承 backtest_gutai.py/
backtest_chanlun_chippeak.py 已經寫過的：無交易成本、存活者偏差、未還原
除權息、停損停利都用收盤價判斷）：
- 季報/月營收：沿用 backtest_gutai.py 的法定揭露截止日規則，但這裡改用
  `backtest_chanlun_chippeak.py` 的 `_qf_disclosure_date()`/
  `_mr_disclosure_date()`（回傳實際日期，用雙指標逐日推進 `_advance_ptr()`，
  比 backtest_gutai.py 原本的「每天重新 filter 整個列表」更快）。
- 股利政策（div_by_year）：`dividend_policy` 表本身就有逐筆 `event_date`，
  直接用 `event_date <= as_of` 做 gating，比 backtest_gutai.py 近似財報
  截止日的做法更精確（不用近似，這個表天生就是事件制）。
- 淨值比歷史均值（pbr_avg_hist）：即時計分版本是「全歷史平均，不分日期」
  （見 experts.py `_build_context()`），回測版本必須改成「累積到 as_of
  為止的歷史平均」，否則會把未來的均值洩漏回過去——這是本檔新增的
  point-in-time 修正，即時計分那份不受影響（它本來就只看「今天」）。
- **董監持股（director_holding_pct）在回測期間一律當作 None（未知）**：
  `director_holdings` 表目前只有 3 個月的歷史（爬蟲 2026-08 才開始記錄，
  無法回溯），對 flag888_2（用 award()，None 只是不計分，不影響選股門檻）
  影響有限；但 haogongsi 把它做成 `require()` 選股門檻，回測期間幾乎全部
  會被判定不通過——**haogongsi 的回測結果請視為「資料不足、暫不可信」，
  不是「這套規則沒用」**，之後若 director_holdings 累積足夠歷史應該重跑。

Usage:
    python backtest_expert_signals.py --years-back 5 [--limit 50] [--workers N]
"""
import argparse
import bisect
import json
import logging
import multiprocessing as mp
import os
from datetime import date, datetime, timedelta
from itertools import islice
from zoneinfo import ZoneInfo

from sqlalchemy import text

from database import SessionLocal, Stock
from experts import (
    score_gutai_bull, score_gutai_bear, score_flag888_1, score_flag888_2,
    score_flag888_3, score_flag888_4, score_guyu, score_laoniu,
    score_haogongsi, score_momentum_guard, _mean,
)
from backtest_gutai import (
    _to_date, _load_prices, _load_institutional, _load_holding,
    _load_quarterly, _load_revenue, _precompute, _asof_idx, _tech_asof,
    _list_asof,
)
from backtest_chanlun_chippeak import _mr_disclosure_date, _qf_disclosure_date, _advance_ptr, summarize

_TZ = ZoneInfo('Asia/Taipei')
_STOP_PCT = -0.10   # 見 backtest_chanlun_chippeak.py 的固定停損定義
_TRAIL_PCT = 0.10   # 見 backtest_chanlun_chippeak.py 的移動停利定義
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

_RULES = {
    'gutai_bull': score_gutai_bull,
    'gutai_bear': score_gutai_bear,
    'flag888_1': score_flag888_1,
    'flag888_2': score_flag888_2,
    'flag888_3': score_flag888_3,
    'flag888_4': score_flag888_4,
    'guyu': score_guyu,
    'laoniu': score_laoniu,
    'haogongsi': score_haogongsi,
    'momentum_guard': score_momentum_guard,
}


# ── extra point-in-time loaders (beyond what backtest_gutai.py already has) ──

def _load_pbr_series(db):
    """{code: {'dates','per','pbr','dividend_yield','cum_avg'}}，只收 pbr
    不是 NULL 的列（跟 experts.py `_build_context()` 一致），`cum_avg` 是
    累積到當筆為止的 pbr 平均——回測要用「當時已知的歷史均值」，不能像
    即時計分那樣直接用全歷史平均（見 module docstring）。"""
    out = {}
    for r in db.execute(text('''
        SELECT stock_code, date, per, pbr, dividend_yield FROM daily_prices
        WHERE pbr IS NOT NULL ORDER BY stock_code, date ASC
    ''')).mappings():
        c = out.setdefault(r['stock_code'], {'dates': [], 'per': [], 'pbr': [], 'dividend_yield': []})
        c['dates'].append(_to_date(r['date']))
        c['per'].append(r['per'])
        c['pbr'].append(r['pbr'])
        c['dividend_yield'].append(r['dividend_yield'])
    for c in out.values():
        total, n, cum = 0.0, 0, []
        for v in c['pbr']:
            total += v
            n += 1
            cum.append(total / n)
        c['cum_avg'] = cum
    return out


def _load_dividend_policy(db):
    out = {}
    for r in db.execute(text('''
        SELECT stock_code, event_date, fiscal_year, cash_dividend, stock_dividend
        FROM dividend_policy ORDER BY stock_code, event_date ASC
    ''')).mappings():
        out.setdefault(r['stock_code'], []).append({
            'event_date': _to_date(r['event_date']), 'fiscal_year': r['fiscal_year'],
            'cash_dividend': r['cash_dividend'], 'stock_dividend': r['stock_dividend'],
        })
    return out


def _load_dividend_fill(db):
    out = {}
    for r in db.execute(text('''
        SELECT stock_code, ex_date, filled FROM dividend_fill_events ORDER BY stock_code, ex_date ASC
    ''')).mappings():
        out.setdefault(r['stock_code'], []).append({'ex_date': _to_date(r['ex_date']), 'filled': r['filled']})
    return out


def _advance_div_ptr(div_sorted, ptr, today, div_by_year):
    """`_advance_ptr()` 的股利版：股利是「累加」語意（一年可能配息多次），
    推進指標的同時要把新揭露的事件金額加進 `div_by_year`（原地修改），不是
    單純回傳新指標而已——跟純查值的 `_advance_ptr()` 不同，股利需要side
    effect 才能維持累積狀態。"""
    while ptr + 1 < len(div_sorted) and div_sorted[ptr + 1][0] <= today:
        ptr += 1
        r = div_sorted[ptr][1]
        e = div_by_year.setdefault(r['fiscal_year'], {'cash': 0.0, 'stock': 0.0})
        e['cash'] += r['cash_dividend'] or 0.0
        e['stock'] += r['stock_dividend'] or 0.0
    return ptr


def _fill_events_window(rows, dates, as_of, since):
    lo = bisect.bisect_left(dates, since)
    hi = bisect.bisect_right(dates, as_of)
    return rows[lo:hi]


# ── per-stock simulation: all 10 rules in one pass ──────────────────────────

def simulate_stock_all_rules(pre, inst_rows, hold_rows, q_rows, rev_rows, div_rows, fill_rows, pbr_series):
    n = len(pre['dates']) if pre else 0
    if n < 30:
        return {key: [] for key in _RULES}

    q_sorted = sorted(((_qf_disclosure_date(r['year'], r['quarter']), r) for r in q_rows), key=lambda x: x[0])
    rev_sorted = sorted(((_mr_disclosure_date(r['year'], r['month']), r) for r in rev_rows), key=lambda x: x[0])
    div_sorted = [(r['event_date'], r) for r in div_rows]
    fill_dates = [r['ex_date'] for r in fill_rows]

    qptr = revptr = divptr = -1
    div_by_year = {}

    states = {key: {'pos': False, 'entry_i': None, 'entry_price': None, 'entry_date': None, 'peak': None}
              for key in _RULES}
    trades = {key: [] for key in _RULES}

    for i in range(29, n):
        as_of = pre['dates'][i]
        close = pre['close'][i]
        if close is None:
            continue

        ctx = None
        if any(not st['pos'] for st in states.values()):
            qptr = _advance_ptr(q_sorted, qptr, as_of)
            revptr = _advance_ptr(rev_sorted, revptr, as_of)
            divptr = _advance_div_ptr(div_sorted, divptr, as_of, div_by_year)

            q_known = [r for _, r in islice(reversed(q_sorted[:qptr + 1]), 40)]
            rev_known = [r for _, r in islice(reversed(rev_sorted[:revptr + 1]), 12)]

            vol_window = pre['volume'][max(0, i - 9):i + 1]
            avg_vol_10d = _mean(vol_window) if vol_window else None

            revenue = rev_known[0]['revenue'] if rev_known else None
            revenue_yoy = rev_known[0]['revenue_yoy'] if rev_known else None
            rev_yoy_recent = [x['revenue_yoy'] for x in rev_known[:2]]
            rev3 = [x['revenue'] for x in rev_known[:3] if x['revenue'] is not None]
            rev12 = [x['revenue'] for x in rev_known[:12] if x['revenue'] is not None]
            rev3m_avg = sum(rev3) / len(rev3) if rev3 else None
            rev12m_avg = sum(rev12) / len(rev12) if rev12 else None

            inst = _list_asof(inst_rows, as_of, as_of - timedelta(days=12), 5)
            hold = _list_asof(hold_rows, as_of, as_of - timedelta(days=45), 3)
            div_fill_events = [{'filled': r['filled']} for r in
                                _fill_events_window(fill_rows, fill_dates, as_of, as_of - timedelta(days=5 * 365))]

            per = pbr = dividend_yield = pbr_avg_hist = None
            if pbr_series:
                j = _asof_idx(pbr_series['dates'], as_of)
                if j is not None:
                    per, pbr = pbr_series['per'][j], pbr_series['pbr'][j]
                    dividend_yield = pbr_series['dividend_yield'][j]
                    pbr_avg_hist = pbr_series['cum_avg'][j]

            ctx = {
                'close': close, 'volume': pre['volume'][i], 'avg_vol_10d': avg_vol_10d,
                'per': per, 'pbr': pbr, 'pbr_avg_hist': pbr_avg_hist, 'dividend_yield': dividend_yield,
                'revenue': revenue, 'revenue_yoy': revenue_yoy, 'rev_yoy_recent': rev_yoy_recent,
                'rev3m_avg': rev3m_avg, 'rev12m_avg': rev12m_avg,
                'q': q_known, 'inst': inst, 'hold': hold,
                'div_by_year': dict(div_by_year), 'div_fill_events': div_fill_events,
                'director_holding_pct': None,  # 見 module docstring：資料庫無足夠歷史
                'tech': _tech_asof(pre, as_of),
            }

        for key, fn in _RULES.items():
            st = states[key]
            if not st['pos']:
                try:
                    passed = fn(ctx)[0]
                except Exception:
                    logger.exception('%s failed at %s', key, as_of)
                    passed = False
                if passed:
                    st.update(pos=True, entry_i=i, entry_price=close, entry_date=as_of, peak=close)
            else:
                peak = max(st['peak'], close)
                st['peak'] = peak
                ret = (close - st['entry_price']) / st['entry_price']
                if peak > st['entry_price'] and close <= peak * (1 - _TRAIL_PCT):
                    exit_reason = 'trailing'
                elif ret <= _STOP_PCT:
                    exit_reason = 'stop'
                else:
                    exit_reason = None
                if exit_reason:
                    trades[key].append({
                        'entry_date': str(st['entry_date']), 'exit_date': str(as_of),
                        'entry_price': st['entry_price'], 'exit_price': close,
                        'return_pct': round(ret * 100, 2),
                        'holding_days': i - st['entry_i'],
                        'exit_reason': exit_reason,
                    })
                    st.update(pos=False, entry_i=None, entry_price=None, entry_date=None, peak=None)

    last_close = pre['close'][-1]
    for key, st in states.items():
        if st['pos']:
            trades[key].append({
                'entry_date': str(st['entry_date']), 'exit_date': None,
                'entry_price': st['entry_price'], 'exit_price': last_close,
                'return_pct': round((last_close - st['entry_price']) / st['entry_price'] * 100, 2) if last_close else None,
                'holding_days': (n - 1) - st['entry_i'],
                'exit_reason': 'open',
            })
    return trades


# ── multiprocessing worker ──────────────────────────────────────────────────

def _compute_one(args):
    code, pre, inst_rows, hold_rows, q_rows, rev_rows, div_rows, fill_rows, pbr_series = args
    return code, simulate_stock_all_rules(pre, inst_rows, hold_rows, q_rows, rev_rows, div_rows, fill_rows, pbr_series)


def _load_and_run(db, since_iso, limit, workers):
    codes = [c for (c,) in db.query(Stock.code).order_by(Stock.code).all()]
    if limit:
        codes = codes[:limit]
    wanted = set(codes)
    since = _to_date(since_iso)

    logger.info('Loading daily_prices since %s ...', since_iso)
    prices = {c: p for c, p in _load_prices(db, since_iso).items() if c in wanted}
    logger.info('Loaded %d stocks of price history', len(prices))

    inst_by_code = _load_institutional(db, since_iso)
    hold_by_code = _load_holding(db, since_iso)
    qf_by_code = _load_quarterly(db)
    rev_by_code = _load_revenue(db)
    div_by_code = _load_dividend_policy(db)
    fill_by_code = _load_dividend_fill(db)
    pbr_by_code = _load_pbr_series(db)

    logger.info('Precomputing technical indicators per stock ...')
    price_pre = {}
    for n, (code, p) in enumerate(prices.items(), 1):
        pre = _precompute(p)
        if pre is not None:
            price_pre[code] = pre
        if n % 500 == 0:
            logger.info('  precomputed %d/%d', n, len(prices))
    logger.info('Precompute done: %d stocks usable', len(price_pre))

    tasks = [
        (code, pre, inst_by_code.get(code, []), hold_by_code.get(code, []),
         qf_by_code.get(code, []), rev_by_code.get(code, []),
         div_by_code.get(code, []), fill_by_code.get(code, []), pbr_by_code.get(code))
        for code, pre in price_pre.items()
    ]
    n_workers = workers or (os.cpu_count() or 4)
    logger.info('Simulating %d stocks x %d rules across %d worker processes...', len(tasks), len(_RULES), n_workers)
    results = {key: {} for key in _RULES}
    with mp.Pool(n_workers) as pool:
        for i, (code, by_rule) in enumerate(pool.imap_unordered(_compute_one, tasks), 1):
            for key, trades in by_rule.items():
                if trades:
                    results[key][code] = trades
            if i % 200 == 0:
                logger.info('  simulated %d/%d stocks', i, len(tasks))
    return results


def run_backtest(years_back, limit, workers):
    db = SessionLocal()
    try:
        since = datetime.now(_TZ).date() - timedelta(days=years_back * 365 + 760)
        since_iso = since.isoformat()
        logger.info('Loading history since %s (years_back=%d + warmup buffer)...', since_iso, years_back)
        return _load_and_run(db, since_iso, limit, workers)
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backtest 10套達人選股規則（非纏論），訊號進場+移動停利/固定停損出場')
    parser.add_argument('--years-back', type=int, default=5, help='how many years of history to scan (default 5)')
    parser.add_argument('--limit', type=int, default=None, help='only test the first N stocks (for quick runs)')
    parser.add_argument('--workers', type=int, default=None, help='worker process count (default: all CPU cores)')
    parser.add_argument('--out', type=str, default='backtest_expert_signals_result.json')
    parser.add_argument('--chanlun-json', type=str, default='backtest_chanlun_chippeak_result.json',
                         help='既有的纏論回測結果檔，用來合併 chanlun_buy/chanlun_star 進最終比較表')
    args = parser.parse_args()

    results = run_backtest(args.years_back, args.limit, args.workers)
    summaries = {key: summarize(by_code) for key, by_code in results.items()}

    combined = dict(summaries)
    if os.path.exists(args.chanlun_json):
        with open(args.chanlun_json, encoding='utf-8') as f:
            chanlun_data = json.load(f)
        chanlun_summary = chanlun_data.get('summary', {})
        if 'chanlun' in chanlun_summary:
            combined['chanlun_buy'] = chanlun_summary['chanlun']
        if 'chanlun_star' in chanlun_summary:
            combined['chanlun_star'] = chanlun_summary['chanlun_star']

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({'summary': summaries, 'trades': results}, f, ensure_ascii=False, indent=2)

    label = {
        'gutai_bull': '股泰多方', 'gutai_bear': '股泰空方', 'flag888_1': '888標準1營收轉機',
        'flag888_2': '888標準2高股息', 'flag888_3': '888標準3低價價值', 'flag888_4': '888標準4填息穩定',
        'guyu': '股魚價值K線', 'laoniu': '股海老牛抱緊股', 'haogongsi': '好公司7指標(資料不足)',
        'momentum_guard': '動能防雷', 'chanlun_buy': '纏論買點', 'chanlun_star': '纏論買點+營收飆股',
    }
    header = f"{'規則':<20}{'交易數':>8}{'勝率%':>8}{'平均報酬%':>10}{'中位數%':>10}{'平均持有天':>10}"
    logger.info(header)
    for key, s in combined.items():
        n = s.get('n_closed', 0)
        if n:
            logger.info(f"{label.get(key, key):<20}{n:>8}{s['win_rate_pct']:>8}{s['avg_return_pct']:>10}"
                        f"{s['median_return_pct']:>10}{s['avg_holding_days']:>10}")
        else:
            logger.info(f"{label.get(key, key):<20}{'0 (無交易)':>8}")

    logger.info('Full results written to %s (10套新規則); 纏論2套沿用 %s', args.out, args.chanlun_json)
