"""Backtest 力道K線（force_kline.py）「PowerK 翻正」訊號的歷史勝率/報酬/回撤。
獨立、唯讀腳本——只讀 daily_prices/institutional_trades/stocks，不寫任何資料庫
表，不影響任何正式排程或即時功能（跟 backtest_gutai.py 同一種「安全隔離」精神）。

**故意不比照 backtest_gutai.py 的 point-in-time 揭露日期 gating 與 weekly
resample**：股泰依賴季報/月營收這種有法定揭露截止日的基本面資料，需要嚴格
控制「這個時間點該股票的真實可知資訊」才不會有 lookahead bias；力道K線只用
價格/成交量/三大法人日資料，三者都是「當天發生、當天即為已知」，沒有揭露
時間差問題，所以可以直接逐日回測，不需要那兩層複雜度。

方法
----
1. 逐股票（`stocks` 表）用一次性 bulk query（`ORDER BY stock_code, date`
   串流分組，比照 experts.py `_build_context()` 的既有慣例）撈出
   daily_prices + institutional_trades，對每支股票呼叫一次
   force_kline.compute_force_score()（每支股票只算一次，不重複計算）。
   這一步是實際瓶頸所在（bulk query 本身只要幾秒，但近2000檔股票的
   rolling zscore 迴圈單執行緒要跑好幾分鐘），所以用 `multiprocessing.Pool`
   平行算——DB讀取仍在主行程一次做完（SQLite 不適合多行程同時打），平行
   的只有純CPU、不碰DB的 `compute_force_score()` 本身。`--workers` 預設吃
   滿全部 CPU 核心（不是 `backtest_gutai.py` 的「核心數-2」保守預設，這支
   腳本刻意設成全速，見 CLI 說明）。
2. 訊號 = PowerK 由 <=0 翻到 >0（力道翻正），比照 ChatGPT 對話裡「第一根
   翻紅」的精神——實戰版公式還沒有 A/S/SS 分級，先用最基本的翻正訊號驗證
   這套公式對台股有沒有優勢。
3. 每個訊號往後看 `--horizon` 個交易日的報酬率，同時記錄持有期間最大回撤
   （期間最低點相對進場價的跌幅）。
4. 市場基準：對每一天，把當天全市場「所有股票」的 horizon 日後報酬率取
   平均，當作那一天的 benchmark（跟 backtest_gutai.py 的 `_mean(all_fwd)`
   同一個定義）。訊號「獲勝」= 該次訊號的報酬率 > 訊號當天的 benchmark，
   不是單純報酬率為正就算贏。
5. 選配的籌碼峰疊加 tier（`base_val`/`s_val`/`base_vah`/`s_vah`）：在對應
   base tier 的訊號上，額外要求收盤價相對籌碼峰（chip_peak.py）某個價位
   的位置——`_val` 系列要求「收盤 <= VAL」（便宜區買），`_vah` 系列要求
   「收盤 >= VAH」（突破貴的一端才買，使用者接著想比較這個相反方向）。
   重用專案既有的 `chip_peak.compute_chip_peak()`，同樣的 point-in-time
   原則，只傳 `rows[:i+1]`（訊號當天及之前的資料），不會看到未來的成交量
   分佈。

已知限制（比照 backtest_gutai.py/backtest_sweet_spot.py 的 Known caveats 慣例）：
- 無交易成本/滑價
- 存活者偏差：用今天的 `stocks` 表，看不到已下市股票
- 未還原除權息價格，跟全站其他價格類功能一致
- force_kline.py 本身的簡化假設（ATR/ZScore視窗/資金流向退回機制）一併繼承

Usage:
    python backtest_force_kline.py --years-back 5 --horizon 10 --zscore-window 60 [--limit 50]
"""
import argparse
import json
import logging
import multiprocessing as mp
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from database import SessionLocal, Stock
import chip_peak
import force_kline

_TZ = ZoneInfo('Asia/Taipei')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def _compute_one(args):
    """Pool worker: pure CPU, no DB access — takes one stock's already-
    fetched rows/inst_rows and runs compute_force_score() on them. Module-
    level (not a closure) because multiprocessing needs to pickle it."""
    code, rows, inst_rows, zscore_window = args
    out = force_kline.compute_force_score(rows, inst_rows, zscore_window=zscore_window)
    if not out:
        return code, None
    return code, {
        'dates': out['dates'],
        'closes': [r['close'] for r in rows],
        'power_k': out['power_k'],
        'force_score': out['force_score'],
        'ma20': out['ma20'],
        'rvol': out['rvol'],
        'rows': rows,  # kept for chip_peak.compute_chip_peak(rows[:i+1]) — see apply_val_filter()
    }


def _load_stock_series(db, since_iso, limit, zscore_window, workers=None):
    """Bulk-load daily_prices + institutional_trades (both streamed and
    grouped by stock_code, ORDER BY stock_code, date — same pattern as
    experts.py's _build_context()), run compute_force_score() once per
    stock. Returns {code: {'dates': [...], 'closes': [...], 'power_k': [...]}}
    for stocks with enough history; stocks with too little data are silently
    skipped (force_kline.compute_force_score returns {} for them)."""
    codes = [c for (c,) in db.query(Stock.code).order_by(Stock.code).all()]
    if limit:
        codes = codes[:limit]
    wanted = set(codes)

    price_by_code = defaultdict(list)
    current_code, current_rows = None, []
    result = db.execute(text('''
        SELECT stock_code, date, open, high, low, close, volume FROM daily_prices
        WHERE date >= :since ORDER BY stock_code, date ASC
    '''), {'since': since_iso})
    for r in result.mappings():
        code = r['stock_code']
        if code not in wanted:
            continue
        price_by_code[code].append({
            'date': r['date'], 'open': r['open'], 'high': r['high'],
            'low': r['low'], 'close': r['close'], 'volume': r['volume'],
        })

    inst_by_code = defaultdict(list)
    result = db.execute(text('''
        SELECT stock_code, date, foreign_buy, foreign_sell, trust_buy, trust_sell,
               dealer_buy, dealer_sell FROM institutional_trades
        WHERE date >= :since ORDER BY stock_code, date ASC
    '''), {'since': since_iso})
    for r in result.mappings():
        code = r['stock_code']
        if code not in wanted:
            continue
        inst_by_code[code].append(dict(r))

    # compute_force_score() is pure CPU (no DB access) once rows/inst_rows
    # are in hand, and every stock is independent — the ideal shape for a
    # process pool. This is the actual bottleneck (the two bulk queries
    # above take seconds; the rolling-zscore loops over ~2000 stocks take
    # minutes single-threaded), so this is what --workers parallelizes.
    tasks = [(code, price_by_code[code], inst_by_code.get(code), zscore_window)
             for code in codes if code in price_by_code]
    stock_data = {}
    n_workers = workers or (os.cpu_count() or 4)
    logger.info('Computing force scores for %d stocks across %d worker processes...', len(tasks), n_workers)
    with mp.Pool(n_workers) as pool:
        for i, (code, entry) in enumerate(pool.imap_unordered(_compute_one, tasks), 1):
            if entry is not None:
                stock_data[code] = entry
            if i % 200 == 0:
                logger.info('  computed force score for %d/%d stocks', i, len(tasks))
    return stock_data


def _build_benchmark(stock_data, horizon):
    """date -> average horizon-day forward return (%) across every stock
    with a valid close on that date and horizon days later."""
    fwd_by_date = defaultdict(list)
    for data in stock_data.values():
        closes, dates = data['closes'], data['dates']
        for i in range(len(closes) - horizon):
            entry = closes[i]
            exit_ = closes[i + horizon]
            if entry and exit_:
                fwd_by_date[dates[i]].append((exit_ - entry) / entry * 100)
    return {d: statistics.mean(v) for d, v in fwd_by_date.items()}


def _is_signal(data, i, tier):
    """`tier='base'`: PowerK 由 <=0 翻到 >0（見模組docstring）。
    `tier='s'`: 在 base 條件之上，額外套用 ChatGPT 對話裡「1.0完整版」的
    S級「強勢起漲點」確認條件——ForceScore > +30、收盤 > MA20、MA20 上彎
    （比前一日高，「上彎」原始設計沒有給精確門檻，這裡取字面意思：最近
    一天轉為上升）、RVOL > 1.3。這是 Phase A 的第二輪測試：基礎翻正訊號
    回測結果沒有優勢（見 CLAUDE.md「力道K線」章節），想看看加上這組
    確認條件之後篩選出來的訊號品質是否比較好。"""
    power_k = data['power_k']
    prev, cur = power_k[i - 1], power_k[i]
    if prev is None or cur is None or not (prev <= 0 < cur):
        return False
    if tier == 'base':
        return True
    # tier == 's'
    force_score, closes, ma20, rvol = data['force_score'], data['closes'], data['ma20'], data['rvol']
    if force_score[i] is None or force_score[i] <= 30:
        return False
    if ma20[i] is None or ma20[i - 1] is None or closes[i] is None:
        return False
    if not (closes[i] > ma20[i] and ma20[i] > ma20[i - 1]):
        return False
    if rvol[i] is None or rvol[i] <= 1.3:
        return False
    return True


def find_signals(stock_data, horizon, bench_by_date, tier='base'):
    signals = []
    for code, data in stock_data.items():
        closes, dates, power_k = data['closes'], data['dates'], data['power_k']
        n = len(power_k)
        for i in range(1, n - horizon):
            if not _is_signal(data, i, tier):
                continue
            entry = closes[i]
            if not entry:
                continue
            fwd_pct = (closes[i + horizon] - entry) / entry * 100
            path = closes[i + 1:i + horizon + 1]
            drawdown_pct = min(((c - entry) / entry * 100 for c in path if c), default=0.0)
            bench = bench_by_date.get(dates[i])
            signals.append({
                'code': code, 'date': str(dates[i]), '_i': i,
                'fwd_return_pct': round(fwd_pct, 2),
                'max_drawdown_pct': round(drawdown_pct, 2),
                'benchmark_pct': round(bench, 2) if bench is not None else None,
            })
    return signals


def apply_chip_peak_filter(stock_data, signals, level, comparison):
    """Keep only the signals whose entry-day close satisfies `comparison`
    against that stock's point-in-time 籌碼峰 `level` ('val' or 'vah',
    chip_peak.compute_chip_peak() — same formula the site's chip-peak
    feature already uses). Point-in-time: only ever passes rows up to and
    including the signal day, so this can't see future volume distribution.

    `comparison`: 'below' (close <= level, "還在便宜區買") or 'above'
    (close >= level, "站上貴的一端才買"，使用者接著問的 VAH 突破版本)."""
    assert comparison in ('below', 'above')
    filtered = []
    for s in signals:
        rows = stock_data[s['code']]['rows']
        i = s['_i']
        cp = chip_peak.compute_chip_peak(rows[:i + 1])
        if not cp:
            continue
        close = rows[i]['close']
        if close is None:
            continue
        threshold = cp[level]
        if (comparison == 'below' and close <= threshold) or (comparison == 'above' and close >= threshold):
            filtered.append(s)
    return filtered


def summarize(signals):
    valid = [s for s in signals if s['benchmark_pct'] is not None]
    out = {'n_signals_total': len(signals), 'n_with_benchmark': len(valid)}
    if valid:
        fwd = [s['fwd_return_pct'] for s in valid]
        dd = [s['max_drawdown_pct'] for s in valid]
        bench = [s['benchmark_pct'] for s in valid]
        wins = sum(1 for s in valid if s['fwd_return_pct'] > s['benchmark_pct'])
        out.update({
            'avg_fwd_return_pct': round(statistics.mean(fwd), 2),
            'median_fwd_return_pct': round(statistics.median(fwd), 2),
            'avg_benchmark_pct': round(statistics.mean(bench), 2),
            'win_rate_vs_benchmark_pct': round(wins / len(valid) * 100, 1),
            'positive_return_rate_pct': round(sum(1 for f in fwd if f > 0) / len(fwd) * 100, 1),
            'avg_max_drawdown_pct': round(statistics.mean(dd), 2),
            'worst_max_drawdown_pct': round(min(dd), 2),
        })
    return out


_BASE_TIERS = ('base', 's')  # scanned directly via find_signals()
# derived tier name -> (source base tier, chip_peak level, comparison direction)
_DERIVED_TIERS = {
    'base_val': ('base', 'val', 'below'),  # 力道翻正 + 收盤在便宜區（VAL）以下
    's_val':    ('s',    'val', 'below'),
    'base_vah': ('base', 'vah', 'above'),  # 力道翻正 + 收盤突破貴的一端（VAH）以上
    's_vah':    ('s',    'vah', 'above'),
}


def run_backtest(years_back, horizon, zscore_window, limit, tiers, workers=None):
    db = SessionLocal()
    try:
        since = datetime.now(_TZ).date() - timedelta(days=years_back * 365 + zscore_window * 2 + 90)
        since_iso = since.isoformat()
        logger.info('Loading price/institutional history since %s (years_back=%d + warmup buffer)...',
                    since_iso, years_back)
        stock_data = _load_stock_series(db, since_iso, limit, zscore_window, workers=workers)
        logger.info('force_score computed for %d stocks', len(stock_data))

        logger.info('Building market-wide %d-day forward-return benchmark...', horizon)
        bench_by_date = _build_benchmark(stock_data, horizon)

        # Scan every base tier that's either requested directly or needed as
        # the source for a requested derived tier, so each is only scanned once.
        needed_base = {t for t in tiers if t in _BASE_TIERS} | \
                      {_DERIVED_TIERS[t][0] for t in tiers if t in _DERIVED_TIERS}
        base_results = {}
        for tier in needed_base:
            logger.info('Scanning for tier=%s signals...', tier)
            signals = find_signals(stock_data, horizon, bench_by_date, tier=tier)
            logger.info('tier=%s: found %d signals', tier, len(signals))
            base_results[tier] = signals

        results = {}
        for tier in tiers:
            if tier in _BASE_TIERS:
                results[tier] = base_results[tier]
            elif tier in _DERIVED_TIERS:
                src_tier, level, comparison = _DERIVED_TIERS[tier]
                logger.info('Applying chip_peak %s (%s) filter on top of tier=%s...', level.upper(), comparison, src_tier)
                results[tier] = apply_chip_peak_filter(stock_data, base_results[src_tier], level, comparison)
                logger.info('tier=%s: %d signals survive the %s filter', tier, len(results[tier]), level.upper())
            else:
                raise ValueError(f'unknown tier: {tier!r} (valid: {list(_BASE_TIERS) + list(_DERIVED_TIERS)})')
        return results
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Backtest 力道K線訊號：base／s（1.0 S級確認條件）／'
                    'base_val／s_val（+ 收盤<=籌碼峰VAL，便宜區買）／'
                    'base_vah／s_vah（+ 收盤>=籌碼峰VAH，突破貴的一端買）')
    parser.add_argument('--years-back', type=int, default=5, help='how many years of history to scan (default 5)')
    parser.add_argument('--horizon', type=int, default=10, help='forward trading days to measure return over (default 10)')
    parser.add_argument('--zscore-window', type=int, default=60, help='rolling zscore window in trading days (default 60)')
    parser.add_argument('--limit', type=int, default=None, help='only test the first N stocks (for quick runs)')
    parser.add_argument('--tiers', type=str, default='base,s,base_val,s_val,base_vah,s_vah',
                        help='comma-separated tiers to test: base,s,base_val,s_val,base_vah,s_vah (default all six)')
    parser.add_argument('--workers', type=int, default=None,
                        help='worker process count for the per-stock force-score computation '
                             '(default: all CPU cores — unlike backtest_gutai.py this defaults to '
                             'max, not count-2, per explicit request to use full machine performance '
                             'for this backtest)')
    parser.add_argument('--out', type=str, default='backtest_force_kline_result.json')
    args = parser.parse_args()

    tiers = args.tiers.split(',')
    results = run_backtest(args.years_back, args.horizon, args.zscore_window, args.limit, tiers, args.workers)
    summaries = {tier: summarize(signals) for tier, signals in results.items()}

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({'summary': summaries, 'signals': results}, f, ensure_ascii=False, indent=2)

    logger.info('Summary: %s', json.dumps(summaries, ensure_ascii=False, indent=2))
    logger.info('Full results written to %s', args.out)
