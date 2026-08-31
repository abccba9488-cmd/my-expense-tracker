"""隨機組合達人選股訊號（2~3套規則同時成立才算進場，AND邏輯），用跟
backtest_expert_signals.py／backtest_chanlun_chippeak.py 完全相同的「訊號
進場＋移動停利/固定停損出場」框架回測，目的是找有沒有「多個訊號同時出現」
比單一規則報酬更好的組合——跟 chanlun_star 當初的由來一樣（chanlun_star
本身就是「纏論買點 AND 營收飆股」這種2訊號AND組合，回測驗證有效後才被
收進 experts.py 變成正式規則，見 CLAUDE.md「纏論買點×籌碼峰回測」章節）。

候選訊號池（10個，見 `_BASE_SIGNALS`）＝ `backtest_expert_signals.py` 跑過
的10套裡拿掉2個不適合這個框架的：
  - `gutai_bear` 排除：這套語意是「看空、適合放空」，拿來當「買進」訊號
    回測方向是反的（已在前次報告跟使用者說明過）。
  - `haogongsi` 排除：依賴 director_holdings 歷史資料（目前只有近3個月），
    回測期間幾乎全部無法通過門檻，見前次報告。
其餘8套（gutai_bull/flag888_1~4/guyu/laoniu/momentum_guard）直接從
experts.py 匯入 score_* 函式；`chanlun_buy`／`chanlun_star` 這兩個不是從
experts.py 匯入（那兩個直接讀 `ctx['chanlun']`，不適合這裡的架構），改成
現場重算——沿用 `backtest_chanlun_chippeak.py` 已經驗證過的
`chanlun.compute_chanlun()` 逐日重算 + `_is_fresh_buy_signal()`／
`_calc_est_ratio()`，跟該檔 `chanlun`／`chanlun_star` tier 用同一套邏輯。

組合方式：從10個候選訊號裡隨機抽 `--n-combos`（預設20）組不重複的
2~3套規則AND組合（`random.seed` 固定，見 `--seed`，方便重現），每組獨立
完整跑一輪 no-pyramiding 模擬（不是先跑10套基礎規則再事後交集——原因
跟 backtest_chanlun_chippeak.py docstring 說明的一樣：濾網會改變「哪些
訊號真的進場」，進場了才會擋住同一支股票後續的訊號，事後交集會得到
錯誤的持倉序列）。

Point-in-time 架構完全沿用 `backtest_expert_signals.py`（技術指標索引取值、
季報/月營收/股利用揭露日期雙指標推進），不重複說明，只多了 chanlun 的
逐日重算——chanlun 的「筆」判斷不是純因果遞迴（同一段歷史用不同截止日
重算會有不同結果，見 backtest_chanlun_chippeak.py docstring 第1點），
所以沒辦法用 `_tech_asof()` 那種索引取值的方式偷懶，只能真的逐日呼叫
`compute_chanlun()`（單次約0.7ms，全市場5年在44核心下仍可在數分鐘內跑完）。

支援 `--since`/`--until` 指定明確日期區間（不是只能用「距今幾年」），
用來做「不同時間窗口」交叉驗證——例如把資料切成互不重疊的前後兩段，各自
獨立跑一次，檢查某個組合的優勢是不是兩段都成立，還是只有其中一段運氣好
（見 `_truncate_prices()`／`walk_since` 的用法）。用 `--since`/`--until`時
仍會往回多載約400天價格資料當技術指標暖機緩衝，但**實際計入交易的
天數範圍嚴格限制在 [--since, --until]**，不會讓緩衝期間的訊號混進統計。

Usage:
    python backtest_signal_combos.py --years-back 5 --n-combos 20 --seed 42 [--limit 50] [--workers N]
    python backtest_signal_combos.py --since 2016-01-01 --until 2021-01-01 --anchor flag888_3
"""
import argparse
import bisect
import itertools
import json
import logging
import multiprocessing as mp
import os
import random
from datetime import datetime, timedelta
from itertools import islice
from zoneinfo import ZoneInfo

from sqlalchemy import text

from database import SessionLocal, Stock
import chanlun
from experts import (
    score_gutai_bull, score_flag888_1, score_flag888_2, score_flag888_3,
    score_flag888_4, score_guyu, score_laoniu, score_momentum_guard, _mean,
)
from backtest_gutai import (
    _to_date, _load_prices, _load_institutional, _load_holding,
    _load_quarterly, _load_revenue, _precompute, _asof_idx, _tech_asof,
    _list_asof,
)
from backtest_chanlun_chippeak import (
    _mr_disclosure_date, _qf_disclosure_date, _advance_ptr,
    _is_fresh_buy_signal, _calc_est_ratio, summarize,
)
from backtest_expert_signals import (
    _load_pbr_series, _load_dividend_policy, _load_dividend_fill,
    _advance_div_ptr, _fill_events_window,
)

_TZ = ZoneInfo('Asia/Taipei')
_STOP_PCT = -0.10
_TRAIL_PCT = 0.10
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

_FUNDAMENTAL_RULES = {
    'gutai_bull': score_gutai_bull,
    'flag888_1': score_flag888_1,
    'flag888_2': score_flag888_2,
    'flag888_3': score_flag888_3,
    'flag888_4': score_flag888_4,
    'guyu': score_guyu,
    'laoniu': score_laoniu,
    'momentum_guard': score_momentum_guard,
}
_BASE_SIGNALS = list(_FUNDAMENTAL_RULES) + ['chanlun_buy', 'chanlun_star']


def _generate_combos(n_combos, seed, anchor=None):
    """`anchor=None`：從10個候選訊號隨機抽 n_combos 組不重複的2~3套AND組合，
    seed固定方便重現。`anchor='flag888_3'` 這種指定時改成**窮舉**（不是
    隨機抽）——把 anchor 固定當其中一個訊號，跟其餘9個訊號的所有2套/3套
    組合都跑一次（C(9,1)=9個2套 + C(9,2)=36個3套 = 45組），用來系統性回答
    「這個表現最好的單一規則，疊加任何一個或兩個其他規則會不會更好」，
    跟隨機抽樣是兩種不同的探索方式：隨機抽樣是廣度優先掃全部10套的可能
    組合；窮舉錨定是針對已知最佳規則做深度優先的完整鄰域搜尋。"""
    others = [s for s in _BASE_SIGNALS if s != anchor]
    if anchor:
        pool = ([(anchor,) + c for c in itertools.combinations(others, 1)]
                 + [(anchor,) + c for c in itertools.combinations(others, 2)])
        return {f"combo{i+1:02d}[{'+'.join(c)}]": c for i, c in enumerate(pool)}

    rng = random.Random(seed)
    pool = list(itertools.combinations(_BASE_SIGNALS, 2)) + list(itertools.combinations(_BASE_SIGNALS, 3))
    chosen = rng.sample(pool, min(n_combos, len(pool)))
    return {f"combo{i+1:02d}[{'+'.join(c)}]": c for i, c in enumerate(chosen)}


# ── per-stock simulation: 10 base signals + N random combos in one pass ─────

def simulate_stock(pre, price, inst_rows, hold_rows, q_rows, rev_rows, div_rows, fill_rows, pbr_series, combos,
                    walk_since=None):
    n = len(pre['dates']) if pre else 0
    if n < 30:
        return {}
    start_i = 29 if walk_since is None else max(29, bisect.bisect_left(pre['dates'], walk_since))
    if start_i >= n:
        return {}

    q_sorted = sorted(((_qf_disclosure_date(r['year'], r['quarter']), r) for r in q_rows), key=lambda x: x[0])
    rev_sorted = sorted(((_mr_disclosure_date(r['year'], r['month']), r) for r in rev_rows), key=lambda x: x[0])
    div_sorted = [(r['event_date'], r) for r in div_rows]
    fill_dates = [r['ex_date'] for r in fill_rows]
    chanlun_rows = [{'date': price['dates'][j], 'high': price['high'][j], 'low': price['low'][j],
                      'close': price['close'][j], 'volume': price['volume'][j]} for j in range(n)]

    qptr = revptr = divptr = -1
    div_by_year = {}

    all_keys = _BASE_SIGNALS + list(combos)
    states = {key: {'pos': False, 'entry_i': None, 'entry_price': None, 'entry_date': None, 'peak': None}
              for key in all_keys}
    trades = {key: [] for key in all_keys}

    for i in range(start_i, n):
        as_of = pre['dates'][i]
        close = pre['close'][i]
        if close is None:
            continue

        passed_map = {}
        if any(not states[k]['pos'] for k in all_keys):
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
                'director_holding_pct': None,
                'tech': _tech_asof(pre, as_of),
            }
            for key, fn in _FUNDAMENTAL_RULES.items():
                try:
                    passed_map[key] = fn(ctx)[0]
                except Exception:
                    logger.exception('%s failed at %s', key, as_of)
                    passed_map[key] = False

            cl = chanlun.compute_chanlun(chanlun_rows[:i + 1])
            is_buy = _is_fresh_buy_signal(cl, as_of)
            passed_map['chanlun_buy'] = is_buy
            mr_row = rev_known[0] if rev_known else None
            qf_row = q_known[0] if q_known else None
            _, ratio = _calc_est_ratio(mr_row, qf_row, close)
            yoy = mr_row.get('revenue_yoy') if mr_row else None
            is_star = ratio is not None and ratio >= 1.5 and yoy is not None and yoy >= 20
            passed_map['chanlun_star'] = is_buy and is_star

            for combo_key, members in combos.items():
                passed_map[combo_key] = all(passed_map[m] for m in members)

        for key in all_keys:
            st = states[key]
            if not st['pos']:
                if passed_map.get(key):
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
# combos 是唯讀共用資料，用 Pool initializer 存一次到每個 worker process的
# global，避免每個 task 都重新 pickle 同一份 20 組合定義（比照
# backtest_gutai.py 的既有慣例）。
_W = {}


def _init_worker(combos, walk_since):
    _W['combos'] = combos
    _W['walk_since'] = walk_since


def _compute_one(args):
    code, pre, price, inst_rows, hold_rows, q_rows, rev_rows, div_rows, fill_rows, pbr_series = args
    trades = simulate_stock(pre, price, inst_rows, hold_rows, q_rows, rev_rows, div_rows, fill_rows,
                             pbr_series, _W['combos'], _W['walk_since'])
    return code, trades


def _truncate_prices(prices, until):
    """`_load_prices()` 沒有 until 參數（只有 since），這裡回測時間窗口的
    右邊界另外用 bisect 截斷——跟 experts.py 即時計分無關，純粹是這個腳本
    做「不同時間窗口交叉驗證」時才需要的功能。"""
    out = {}
    for code, p in prices.items():
        idx = bisect.bisect_right(p['dates'], until)
        if idx < 30:
            continue
        out[code] = {k: v[:idx] for k, v in p.items()}
    return out


def _load_and_run(db, since_iso, limit, workers, combos, until=None, walk_since=None):
    codes = [c for (c,) in db.query(Stock.code).order_by(Stock.code).all()]
    if limit:
        codes = codes[:limit]
    wanted = set(codes)

    logger.info('Loading daily_prices since %s ...', since_iso)
    prices = {c: p for c, p in _load_prices(db, since_iso).items() if c in wanted}
    if until is not None:
        prices = _truncate_prices(prices, until)
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

    all_keys = _BASE_SIGNALS + list(combos)
    tasks = [
        (code, pre, prices[code], inst_by_code.get(code, []), hold_by_code.get(code, []),
         qf_by_code.get(code, []), rev_by_code.get(code, []),
         div_by_code.get(code, []), fill_by_code.get(code, []), pbr_by_code.get(code))
        for code, pre in price_pre.items()
    ]
    n_workers = workers or (os.cpu_count() or 4)
    logger.info('Simulating %d stocks x %d signals (%d base + %d combos) across %d worker processes...',
                len(tasks), len(all_keys), len(_BASE_SIGNALS), len(combos), n_workers)
    results = {key: {} for key in all_keys}
    with mp.Pool(n_workers, initializer=_init_worker, initargs=(combos, walk_since)) as pool:
        for i, (code, by_key) in enumerate(pool.imap_unordered(_compute_one, tasks), 1):
            for key, trades in by_key.items():
                if trades:
                    results[key][code] = trades
            if i % 200 == 0:
                logger.info('  simulated %d/%d stocks', i, len(tasks))
    return results


def run_backtest(years_back, limit, workers, n_combos, seed, anchor=None, since=None, until=None):
    """`since`/`until`（date 物件，可選）指定明確時間窗口時，會覆蓋
    `years_back`-from-today 的算法——載入資料仍會往回多抓約400天當技術
    指標暖機緩衝，但實際計入交易的範圍嚴格限制在 [since, until]（見
    `simulate_stock()` 的 `walk_since` 參數／`_truncate_prices()`），讓
    不同窗口之間的結果乾淨不重疊，才能拿來做交叉驗證。"""
    combos = _generate_combos(n_combos, seed, anchor)
    db = SessionLocal()
    try:
        if since is not None:
            load_since_iso = (since - timedelta(days=400)).isoformat()
            logger.info('Loading history %s (with 400d warmup buffer) through %s ...', load_since_iso, until)
        else:
            load_since_iso = (datetime.now(_TZ).date() - timedelta(days=years_back * 365 + 760)).isoformat()
            logger.info('Loading history since %s (years_back=%d + warmup buffer)...', load_since_iso, years_back)
        results = _load_and_run(db, load_since_iso, limit, workers, combos, until=until, walk_since=since)
        return results, combos
    finally:
        db.close()


def run_single_stock(db, code, years=5, combo_key='flag888_guyu', members=('flag888_3', 'guyu')):
    """個股詳情頁「歷史訊號」卡片用的單股、同步、on-demand 版本——比照
    `backtest_chanlun_chippeak.run_single_stock()` 的既有模式（app.py 直接
    import 整個模組呼叫，不用另外拉一支批次腳本）。跟 `run_backtest()` 的
    差別只在於範圍縮小成一支股票、單一組合，邏輯（`simulate_stock()`）
    完全共用，不重寫；資料改成逐股查詢（不用 `backtest_gutai.py` 那些
    全市場批次 loader，單股沒有批次撈取的效能顧慮）。

    回傳 None 表示資料不足；否則回傳
    `{code, years, trades: [...], summary: {...}}`（`summary` 重用
    `summarize({code: trades})`，單股當作「只有一檔股票的批次結果」處理，
    不用另外寫聚合邏輯，見 chippeak 版本的同一個慣例）。"""
    since = datetime.now(_TZ).date() - timedelta(days=years * 365 + 760)
    since_iso = since.isoformat()

    rows = [dict(r) for r in db.execute(text('''
        SELECT date, open, high, low, close, volume FROM daily_prices
        WHERE stock_code = :code AND date >= :since ORDER BY date ASC
    '''), {'code': code, 'since': since_iso}).mappings()]
    if len(rows) < 30:
        return None
    price = {
        'dates': [_to_date(r['date']) for r in rows], 'open': [r['open'] for r in rows],
        'high': [r['high'] for r in rows], 'low': [r['low'] for r in rows],
        'close': [r['close'] for r in rows], 'volume': [r['volume'] or 0 for r in rows],
    }
    pre = _precompute(price)
    if pre is None:
        return None

    inst_rows = [{
        'date': _to_date(r['date']),
        'foreign_net': (r['foreign_buy'] or 0) - (r['foreign_sell'] or 0),
        'trust_net': (r['trust_buy'] or 0) - (r['trust_sell'] or 0),
        'dealer_net': (r['dealer_buy'] or 0) - (r['dealer_sell'] or 0),
    } for r in db.execute(text('''
        SELECT date, foreign_buy, foreign_sell, trust_buy, trust_sell, dealer_buy, dealer_sell
        FROM institutional_trades WHERE stock_code = :code AND date >= :since ORDER BY date ASC
    '''), {'code': code, 'since': since_iso}).mappings()]

    hold_rows = [{
        'date': _to_date(r['date']),
        'big_pct': _mean([r['pct_1000up'], r['pct_800up'], r['pct_600up'], r['pct_400up']]),
        'small_pct': _mean([r['pct_200down'], r['pct_100down']]),
    } for r in db.execute(text('''
        SELECT date, pct_1000up, pct_800up, pct_600up, pct_400up, pct_200down, pct_100down
        FROM holding_concentration WHERE stock_code = :code AND date >= :since ORDER BY date ASC
    '''), {'code': code, 'since': since_iso}).mappings()]

    qf, fe = {}, {}
    for r in db.execute(text('''
        SELECT year, quarter, revenue, operating_income, net_income, eps
        FROM quarterly_financials WHERE stock_code = :code
    '''), {'code': code}).mappings():
        qf[(r['year'], r['quarter'])] = dict(r)
    for r in db.execute(text('''
        SELECT year, quarter, inventories, accounts_receivable, current_assets, current_liabilities,
               liabilities, equity, total_assets, long_term_borrowings, capital_stock, gross_profit,
               cost_of_goods_sold, pretax_income, operating_cash_flow, interest_expense, capex
        FROM financial_extra WHERE stock_code = :code
    '''), {'code': code}).mappings():
        fe[(r['year'], r['quarter'])] = dict(r)
    q_rows = []
    for key in sorted(set(qf) | set(fe), reverse=True):
        row = {'year': key[0], 'quarter': key[1]}
        row.update(qf.get(key, {}))
        row.update({k: v for k, v in fe.get(key, {}).items() if k not in ('stock_code', 'year', 'quarter')})
        q_rows.append(row)

    rev_rows = [dict(r) for r in db.execute(text('''
        SELECT year, month, revenue, revenue_yoy FROM monthly_revenue
        WHERE stock_code = :code ORDER BY year DESC, month DESC
    '''), {'code': code}).mappings()]

    div_rows = [{
        'event_date': _to_date(r['event_date']), 'fiscal_year': r['fiscal_year'],
        'cash_dividend': r['cash_dividend'], 'stock_dividend': r['stock_dividend'],
    } for r in db.execute(text('''
        SELECT event_date, fiscal_year, cash_dividend, stock_dividend FROM dividend_policy
        WHERE stock_code = :code ORDER BY event_date ASC
    '''), {'code': code}).mappings()]

    fill_rows = [{'ex_date': _to_date(r['ex_date']), 'filled': r['filled']} for r in db.execute(text('''
        SELECT ex_date, filled FROM dividend_fill_events WHERE stock_code = :code ORDER BY ex_date ASC
    '''), {'code': code}).mappings()]

    pbr_rows = [dict(r) for r in db.execute(text('''
        SELECT date, per, pbr, dividend_yield FROM daily_prices
        WHERE stock_code = :code AND pbr IS NOT NULL ORDER BY date ASC
    '''), {'code': code}).mappings()]
    pbr_series = None
    if pbr_rows:
        pbr_series = {'dates': [_to_date(r['date']) for r in pbr_rows],
                       'per': [r['per'] for r in pbr_rows], 'pbr': [r['pbr'] for r in pbr_rows],
                       'dividend_yield': [r['dividend_yield'] for r in pbr_rows]}
        total, n, cum = 0.0, 0, []
        for v in pbr_series['pbr']:
            total += v
            n += 1
            cum.append(total / n)
        pbr_series['cum_avg'] = cum

    combos = {combo_key: members}
    trades = simulate_stock(pre, price, inst_rows, hold_rows, q_rows, rev_rows, div_rows, fill_rows,
                             pbr_series, combos)
    combo_trades = trades.get(combo_key, [])
    summary = summarize({code: combo_trades} if combo_trades else {})
    return {'code': code, 'years': years, 'trades': combo_trades, 'summary': summary}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='隨機組合10套達人選股訊號（AND邏輯），訊號進場+移動停利/固定停損出場')
    parser.add_argument('--years-back', type=int, default=5)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--workers', type=int, default=None)
    parser.add_argument('--n-combos', type=int, default=20)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--anchor', type=str, default=None,
                         help='指定時改成窮舉該訊號跟其餘9套的所有2~3套組合（見 _generate_combos docstring），忽略 --n-combos/--seed')
    parser.add_argument('--since', type=str, default=None, help='YYYY-MM-DD，指定明確時間窗口起點（覆蓋 --years-back）')
    parser.add_argument('--until', type=str, default=None, help='YYYY-MM-DD，指定明確時間窗口終點（預設今天）')
    parser.add_argument('--out', type=str, default='backtest_signal_combos_result.json')
    args = parser.parse_args()

    since_d = datetime.strptime(args.since, '%Y-%m-%d').date() if args.since else None
    until_d = datetime.strptime(args.until, '%Y-%m-%d').date() if args.until else None
    results, combos = run_backtest(args.years_back, args.limit, args.workers, args.n_combos, args.seed, args.anchor,
                                    since=since_d, until=until_d)
    summaries = {key: summarize(by_code) for key, by_code in results.items()}

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({'combos': {k: list(v) for k, v in combos.items()}, 'summary': summaries}, f,
                   ensure_ascii=False, indent=2)

    rows = sorted(summaries.items(), key=lambda kv: kv[1].get('avg_return_pct', -999) if kv[1].get('n_closed') else -999,
                  reverse=True)
    logger.info(f"{'訊號':<45}{'交易數':>8}{'勝率%':>8}{'平均%':>8}{'中位%':>8}{'持有天':>8}")
    for key, s in rows:
        n = s.get('n_closed', 0)
        if n:
            logger.info(f"{key:<45}{n:>8}{s['win_rate_pct']:>8}{s['avg_return_pct']:>8}"
                        f"{s['median_return_pct']:>8}{s['avg_holding_days']:>8}")
        else:
            logger.info(f"{key:<45}{'0 (無交易)':>8}")

    logger.info('Full results written to %s', args.out)
