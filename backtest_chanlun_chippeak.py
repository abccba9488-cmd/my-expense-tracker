"""Backtest：纏論買點（chanlun.py）當進場訊號，搭配籌碼峰（chip_peak.py）VAL/VAH
兩種濾網，只做買進訊號（不測纏論賣點）。獨立唯讀腳本，不寫任何DB表。

**跟 backtest_force_kline.py 的方法論刻意不同**：force_kline 那份用固定
horizon天數的遠期報酬衡量訊號好壞，沒有真正的出場條件；這裡改成完整的
進出場模擬（比照 `backtest_sweet_spot.py` 的 no-pyramiding 精神）——
每次進場後追蹤到真正出場（停利/停損）才能再進場，且用實際成交報酬而非
固定天數快照。

方法
----
1. **纏論訊號逐日 point-in-time 重算**（這是必要的，不是偷懶）：已知纏論
   訊號會隨後續K棒重新計算而「時有時無」（見 CLAUDE.md「力道K線」跟
   「纏論」章節的既有記錄——同一個歷史訊號，用不同截止日重算會有不同結果，
   因為 `compute_chanlun()` 每次都是拿當下全部歷史從頭算，沒有鎖定狀態）。
   所以不能只算一次訊號列表拿來對日期比對，必須真的每一天用
   `chanlun.compute_chanlun(rows[:i+1])` 重算一次，才能正確重現「使用者
   在那一天實際會看到什麼訊號」。`compute_chanlun()` 內部固定只取
   `rows[-lookback:]`（預設250天），所以每次呼叫成本跟 `i` 無關（不會
   越算越慢），實測單次呼叫約0.7ms，全市場5年（近2000檔×約1250個交易日）
   仍可在數分鐘內用多核心跑完。
2. **進場條件**：當天 `compute_chanlun()` 的 `latest_signal` 是買點
   （type 以 'b' 結尾：1b/2b/3b，任一類皆算，不分類型）且在近30曆日內
   （比照 experts.py `_score_chanlun()` 的 `_CHANLUN_RECENT_DAYS=30` 同一個
   新鮮度定義）→ 當天收盤價進場。**沒有持倉時才檢查新訊號**（no
   pyramiding，持倉中不重複進場，也因此持倉期間可以跳過當天的纏論重算，
   省下一部分計算量）。
3. **出場條件**（使用者指定，2026-08-24 修正為移動停利）：
   - **固定停損 -10%**：收盤跌破「進場價」10%——但只在股價從進場後**從未
     上漲過**時才會實際觸發（見下一點，一旦漲過就換移動停利邏輯接手判斷）。
   - **移動停利**：追蹤進場後每天的收盤價高點 `peak`（起始值＝進場價），
     只要 `peak > 進場價`（代表曾經賺錢過），收盤從 `peak` 回檔10%就出場，
     不管出場當下相對進場價是賺是賠都算移動停利出場（可能只小賺甚至小賠，
     這是移動停利機制本來就會有的正常情況，不是bug）。
   - 兩者都用**收盤價**判斷（跟 `backtest_sweet_spot.py` 一致的慣例，不用
     日內高低點），用實際觸發那天的收盤價計算已實現報酬，不做人為封頂。
     回測結束時仍持有的部位標記「持有中」，用最後一天收盤價計算未實現
     報酬。**勝率定義＝已實現報酬 > 0 的比例**，不是「有沒有觸發移動停利」
     ——因為移動停利出場不保證賺錢（見上面說明）。
4. **四個 tier，各自獨立完整模擬一輪**（不是先算 `chanlun` 再拿其他去
   篩選）：`chanlun`（純纏論買點，無濾網）、`chanlun_val`（進場當下收盤
   <= 該股票當時的籌碼峰VAL，便宜區買）、`chanlun_vah`（進場當下收盤 >=
   該股票當時的籌碼峰VAH，突破貴的一端買）、`chanlun_star`（進場當下同時
   符合「營收飆股」入榜條件，2026-08-25 新增，見下方第5點）。**必須各自
   獨立模擬**，不能像 `backtest_force_kline.py` 那樣事後篩選同一批訊號——
   因為濾網會改變「哪些訊號真的進場」，進場了才會有持倉期間去阻擋後續
   訊號，不同濾網下同一支股票在同一段期間持倉與否可能完全不同，事後篩選
   會得到錯誤的持倉序列。
5. **`chanlun_star` 的營收飆股條件必須用「揭露當時已公開」的月營收/季報**
   （不是資料庫裡最新的那筆，那樣會有未來函數偷看）——月營收法定揭露截止
   日是次月10日、季報依季別有固定截止日（`_mr_disclosure_date()`／
   `_qf_disclosure_date()`，同一套規則 `backtest_gutai.py` 的
   `_revenue_known_by()`/`_quarter_known_by()` 已經在用，只是這裡需要算出
   實際日期而不只是布林值，所以沒有直接呼叫那兩個函式，另外寫了回傳日期
   的版本）。`_build_disclosure_series()` 把月營收/季報都轉成依揭露日期
   排序的序列，`simulate_stock()` 逐日用雙指標（`_advance_ptr()`）推進，
   取得「as of 今天」實際已公開的最新一筆，再用 `_calc_est_ratio()`（比照
   `app.js` `calcEst()`／`_getStarBase()` 的公式：`est = (月營收/季營收) ×
   季EPS × 240`，`ratio = est/收盤`）判斷是否符合「`ratio >= 1.5` 且
   `月營收年增 >= 20%`」的營收飆股入榜條件。

已知限制（比照 backtest_gutai.py/backtest_sweet_spot.py 的 Known caveats 慣例）：
- 無交易成本/滑價
- 存活者偏差：用今天的 `stocks` 表，看不到已下市股票
- 未還原除權息價格
- 停損/停利都用收盤價判斷，不是日內觸價（可能少算一些日內就觸發但收盤
  沒破的情況，這是刻意簡化，跟全站其他價格類回測一致）
- chanlun.py/chip_peak.py 本身的簡化假設（跳過線段層級、單一日線級別、
  時間衰減加權等）一併繼承

Usage:
    python backtest_chanlun_chippeak.py --years-back 5 [--limit 50] [--workers N]
"""
import argparse
import json
import logging
import multiprocessing as mp
import os
import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from database import SessionLocal, Stock
import chanlun
import chip_peak

_TZ = ZoneInfo('Asia/Taipei')
_CHANLUN_RECENT_DAYS = 30  # 比照 experts.py _score_chanlun 的新鮮度定義
_STOP_PCT = -0.10   # 固定停損：收盤跌破進場價10%（只在股價從未上漲過時才會觸發，見 simulate_stock）
_TRAIL_PCT = 0.10   # 移動停利：收盤從進場後最高點回檔10%
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def _parse_date(d):
    if isinstance(d, str):
        return datetime.strptime(d, '%Y-%m-%d').date()
    return d


# 「營收飆股」point-in-time 揭露日期——跟 backtest_gutai.py 的
# _revenue_known_by()/_quarter_known_by() 同一套法定截止日規則（月營收次月
# 10日前、季報依 _QUARTER_DEADLINE），差別是這裡要直接算出「揭露日期」本身
# 拿來排序＋雙指標推進，backtest_gutai.py 那兩個函式只回傳布林值，不夠用。
_QUARTER_DEADLINE = {1: (5, 15), 2: (8, 14), 3: (11, 14)}


def _mr_disclosure_date(year, month):
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    return date(ny, nm, 10)


def _qf_disclosure_date(year, quarter):
    if quarter == 4:
        return date(year + 1, 3, 31)
    m, d = _QUARTER_DEADLINE[quarter]
    return date(year, m, d)


def _build_disclosure_series(mr_rows, qf_rows):
    """回傳依揭露日期升冪排序的 (disclosure_date, payload) 序列，供
    simulate_stock() 用雙指標逐日推進，取得「as of 某天」實際已公開的最新
    月營收／季報列——不是資料庫裡最新的那筆（那樣會有 lookahead bias）。"""
    mr_series = sorted(
        ((_mr_disclosure_date(r['year'], r['month']), r) for r in mr_rows),
        key=lambda x: x[0],
    )
    qf_series = sorted(
        ((_qf_disclosure_date(r['year'], r['quarter']), r) for r in qf_rows),
        key=lambda x: x[0],
    )
    return mr_series, qf_series


def _advance_ptr(series, ptr, today):
    """雙指標推進：series 已依 disclosure_date 升冪排序，回傳「揭露日期 <=
    today 的最後一筆」對應的新 ptr（-1 表示還沒有任何一筆公開過）。"""
    while ptr + 1 < len(series) and series[ptr + 1][0] <= today:
        ptr += 1
    return ptr


def _calc_est_ratio(mr_row, qf_row, close):
    """比照 app.js calcEst()／_getStarBase() 的公式：
    est = (mr.revenue / qf.revenue) * qf.eps * 240；ratio = est / close。
    任一必要欄位缺值或非正數就回傳 (None, None)，跟前端邏輯一致。"""
    if not mr_row or not qf_row or not close:
        return None, None
    qf_revenue, eps = qf_row.get('revenue'), qf_row.get('eps')
    if not qf_revenue or qf_revenue <= 0 or eps is None or eps <= 0:
        return None, None
    revenue = mr_row.get('revenue')
    if revenue is None:
        return None, None
    est = (revenue / qf_revenue) * eps * 240
    return est, est / close


def _is_fresh_buy_signal(cl, as_of_date):
    """cl: compute_chanlun() 的回傳值（可能是 {}）。as_of_date: 這次重算所在
    的那一天（date 物件）。回傳該天是否有新鮮的買點訊號。"""
    if not cl:
        return False
    sig = cl.get('latest_signal')
    if not sig or not sig['type'].endswith('b'):
        return False
    days_ago = (as_of_date - _parse_date(sig['date'])).days
    return days_ago <= _CHANLUN_RECENT_DAYS


def simulate_stock(rows, tier, mr_series=None, qf_series=None):
    """單一股票、單一 tier 的完整 no-pyramiding 進出場模擬。rows: 依日期
    升冪，dict 含 date/high/low/close/volume。tier: 'chanlun'/'chanlun_val'/
    'chanlun_vah'/'chanlun_star'。`chanlun_star` 才需要 mr_series/qf_series
    （`_build_disclosure_series()` 的輸出）。回傳這支股票這個 tier 底下的
    完整交易紀錄列表。"""
    n = len(rows)
    if n < 30:
        return []

    trades = []
    in_position = False
    entry_i = entry_price = entry_date = peak = None
    mr_ptr = qf_ptr = -1
    i = 29  # compute_chanlun/compute_chip_peak 都需要至少30筆

    while i < n:
        if not in_position:
            close = rows[i]['close']
            today = _parse_date(rows[i]['date'])
            if tier == 'chanlun_star':
                mr_ptr = _advance_ptr(mr_series, mr_ptr, today)
                qf_ptr = _advance_ptr(qf_series, qf_ptr, today)
            if close:
                cl = chanlun.compute_chanlun(rows[:i + 1])
                if _is_fresh_buy_signal(cl, today):
                    take = True
                    if tier in ('chanlun_val', 'chanlun_vah'):
                        cp = chip_peak.compute_chip_peak(rows[:i + 1])
                        if not cp:
                            take = False
                        elif tier == 'chanlun_val':
                            take = close <= cp['val']
                        elif tier == 'chanlun_vah':
                            take = close >= cp['vah']
                    elif tier == 'chanlun_star':
                        # 比照 app.js _getStarBase() 的「營收飆股」入榜條件：
                        # 預估股價/收盤 >= 1.5 且 月營收年增 >= 20%
                        mr_row = mr_series[mr_ptr][1] if mr_ptr >= 0 else None
                        qf_row = qf_series[qf_ptr][1] if qf_ptr >= 0 else None
                        _, ratio = _calc_est_ratio(mr_row, qf_row, close)
                        yoy = mr_row.get('revenue_yoy') if mr_row else None
                        take = ratio is not None and ratio >= 1.5 and yoy is not None and yoy >= 20
                    if take:
                        in_position = True
                        entry_i, entry_price, entry_date = i, close, today
                        peak = close  # 移動停利的高點起算點＝進場價本身
        else:
            close = rows[i]['close']
            if close:
                peak = max(peak, close)
                ret = (close - entry_price) / entry_price
                # 移動停利要優先判斷：只要股價曾經漲上去過（peak > entry_price）
                # 就代表「有過一段獲利」，這時從高點回檔 _TRAIL_PCT 就出場，不管
                # 出場當下相對進場價是賺是賠都算「移動停利」出場，跟「股價從進場
                # 那天起就沒漲過、直接跌破固定停損價」是不同的兩件事，分開標記
                # exit_reason 才看得出哪種出場比較常見。同一天理論上不會兩個條件
                # 同時判斷不出先後——peak > entry_price 時，移動停利門檻
                # （peak*(1-TRAIL)）恆 >= 固定停損門檻（entry*(1+STOP)），股價逐日
                # 下滑一定會先碰到移動停利門檻，不會有「先跌破固定停損才發現其實
                # 也跌破移動停利」的情況。
                if peak > entry_price and close <= peak * (1 - _TRAIL_PCT):
                    exit_reason = 'trailing'
                elif ret <= _STOP_PCT:
                    exit_reason = 'stop'
                else:
                    exit_reason = None
                if exit_reason:
                    trades.append({
                        'entry_date': str(entry_date), 'exit_date': str(rows[i]['date']),
                        'entry_price': entry_price, 'exit_price': close,
                        'return_pct': round(ret * 100, 2),
                        'holding_days': i - entry_i,
                        'exit_reason': exit_reason,
                    })
                    in_position = False
        i += 1

    if in_position:
        last_close = rows[-1]['close']
        trades.append({
            'entry_date': str(entry_date), 'exit_date': None,
            'entry_price': entry_price, 'exit_price': last_close,
            'return_pct': round((last_close - entry_price) / entry_price * 100, 2) if last_close else None,
            'holding_days': (n - 1) - entry_i,
            'exit_reason': 'open',
        })
    return trades


_TIERS = ('chanlun', 'chanlun_val', 'chanlun_vah', 'chanlun_star')


def _compute_one(args):
    """Pool worker: 一支股票跑完四個 tier 的完整模擬，純CPU不碰DB。"""
    code, rows, mr_rows, qf_rows = args
    mr_series, qf_series = _build_disclosure_series(mr_rows, qf_rows)
    return code, {
        tier: simulate_stock(rows, tier, mr_series, qf_series)
        for tier in _TIERS
    }


def _load_and_run(db, since_iso, limit, workers):
    codes = [c for (c,) in db.query(Stock.code).order_by(Stock.code).all()]
    if limit:
        codes = codes[:limit]
    wanted = set(codes)

    price_by_code = defaultdict(list)
    result = db.execute(text('''
        SELECT stock_code, date, high, low, close, volume FROM daily_prices
        WHERE date >= :since ORDER BY stock_code, date ASC
    '''), {'since': since_iso})
    for r in result.mappings():
        code = r['stock_code']
        if code not in wanted:
            continue
        price_by_code[code].append({
            'date': r['date'], 'high': r['high'], 'low': r['low'],
            'close': r['close'], 'volume': r['volume'],
        })

    # 月營收/季報資料量遠小於日K，不用像 daily_prices 那樣加日期篩選——整段
    # 歷史直接撈回來，wanted 代碼過濾在迴圈內做，跟上面 daily_prices 一致。
    mr_by_code = defaultdict(list)
    result = db.execute(text('''
        SELECT stock_code, year, month, revenue, revenue_yoy FROM monthly_revenue
        ORDER BY stock_code, year, month
    '''))
    for r in result.mappings():
        code = r['stock_code']
        if code in wanted:
            mr_by_code[code].append(dict(r))

    qf_by_code = defaultdict(list)
    result = db.execute(text('''
        SELECT stock_code, year, quarter, revenue, eps FROM quarterly_financials
        ORDER BY stock_code, year, quarter
    '''))
    for r in result.mappings():
        code = r['stock_code']
        if code in wanted:
            qf_by_code[code].append(dict(r))

    tasks = [(code, price_by_code[code], mr_by_code.get(code, []), qf_by_code.get(code, []))
             for code in codes if code in price_by_code]
    n_workers = workers or (os.cpu_count() or 4)
    logger.info('Simulating %d stocks x %d tiers across %d worker processes...', len(tasks), len(_TIERS), n_workers)
    results = {tier: {} for tier in _TIERS}
    with mp.Pool(n_workers) as pool:
        for i, (code, by_tier) in enumerate(pool.imap_unordered(_compute_one, tasks), 1):
            for tier, trades in by_tier.items():
                if trades:
                    results[tier][code] = trades
            if i % 200 == 0:
                logger.info('  simulated %d/%d stocks', i, len(tasks))
    return results


def summarize(results_by_code):
    trades = [t for trades in results_by_code.values() for t in trades]
    closed = [t for t in trades if t['exit_reason'] != 'open']
    out = {
        'n_stocks_with_trades': len(results_by_code),
        'n_trades_total': len(trades),
        'n_closed': len(closed),
        'n_still_open': len(trades) - len(closed),
    }
    if closed:
        n_trailing = sum(1 for t in closed if t['exit_reason'] == 'trailing')
        n_stop = sum(1 for t in closed if t['exit_reason'] == 'stop')
        returns = [t['return_pct'] for t in closed]
        holding = [t['holding_days'] for t in closed]
        # 移動停利出場不保證賺錢（股價只小漲一點就回檔10%，出場價可能還在
        # 進場價之下），所以「勝率」改成直接看已實現報酬是不是正的，不是
        # 用 exit_reason=='trailing' 當代理指標。
        n_wins = sum(1 for t in closed if t['return_pct'] > 0)
        out.update({
            'n_trailing_exit': n_trailing,
            'n_stop_exit': n_stop,
            'win_rate_pct': round(n_wins / len(closed) * 100, 1),
            'avg_return_pct': round(statistics.mean(returns), 2),
            'median_return_pct': round(statistics.median(returns), 2),
            'avg_holding_days': round(statistics.mean(holding), 1),
        })
    return out


def run_backtest(years_back, limit, workers):
    db = SessionLocal()
    try:
        since = datetime.now(_TZ).date() - timedelta(days=years_back * 365 + 400)
        since_iso = since.isoformat()
        logger.info('Loading price history since %s (years_back=%d + warmup buffer)...', since_iso, years_back)
        return _load_and_run(db, since_iso, limit, workers)
    finally:
        db.close()


def run_single_stock(db, code, years=5, tier='chanlun_star'):
    """個股詳情頁「歷史訊號」卡片用的單股、同步、on-demand 版本——比照
    `backtest_sweet_spot.run_backtest(db, code, years)` 的既有模式（app.py
    直接 import 整個模組呼叫，不用另外拉一支批次腳本）。跟 `run_backtest()`
    的差別只在於範圍縮小成一支股票、單一 tier，邏輯（`simulate_stock()`／
    `_build_disclosure_series()`）完全共用，不重寫。

    回傳 None 表示資料不足；否則回傳
    `{code, tier, years, trades: [...], summary: {...}}`（`summary` 重用
    `summarize({code: trades})`，單股當作「只有一檔股票的批次結果」處理，
    不用另外寫聚合邏輯）。"""
    since = datetime.now(_TZ).date() - timedelta(days=years * 365 + 400)
    rows = [dict(r) for r in db.execute(text('''
        SELECT date, high, low, close, volume FROM daily_prices
        WHERE stock_code = :code AND date >= :since ORDER BY date ASC
    '''), {'code': code, 'since': since.isoformat()}).mappings()]
    if len(rows) < 30:
        return None

    mr_rows = [dict(r) for r in db.execute(text('''
        SELECT year, month, revenue, revenue_yoy FROM monthly_revenue
        WHERE stock_code = :code ORDER BY year, month
    '''), {'code': code}).mappings()]
    qf_rows = [dict(r) for r in db.execute(text('''
        SELECT year, quarter, revenue, eps FROM quarterly_financials
        WHERE stock_code = :code ORDER BY year, quarter
    '''), {'code': code}).mappings()]
    mr_series, qf_series = _build_disclosure_series(mr_rows, qf_rows)

    trades = simulate_stock(rows, tier, mr_series, qf_series)
    summary = summarize({code: trades} if trades else {})
    return {'code': code, 'tier': tier, 'years': years, 'trades': trades, 'summary': summary}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backtest 纏論買點 + 籌碼峰VAL/VAH/營收飆股，固定停損-10%/移動停利(從高點回檔10%)')
    parser.add_argument('--years-back', type=int, default=5, help='how many years of history to scan (default 5)')
    parser.add_argument('--limit', type=int, default=None, help='only test the first N stocks (for quick runs)')
    parser.add_argument('--workers', type=int, default=None,
                        help='worker process count (default: all CPU cores, see backtest_force_kline.py for why)')
    parser.add_argument('--out', type=str, default='backtest_chanlun_chippeak_result.json')
    args = parser.parse_args()

    results = run_backtest(args.years_back, args.limit, args.workers)
    summaries = {tier: summarize(by_code) for tier, by_code in results.items()}

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({'summary': summaries, 'trades': results}, f, ensure_ascii=False, indent=2)

    logger.info('Summary: %s', json.dumps(summaries, ensure_ascii=False, indent=2))
    logger.info('Full results written to %s', args.out)
