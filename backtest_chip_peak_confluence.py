"""Backtest：籌碼峰（chip_peak.py）POC／VAL 與收盤價「三者非常接近」時的
持有期後續股價表現。獨立、唯讀腳本，只讀 daily_prices/stocks，不寫任何
資料庫表，不影響任何正式排程或即時功能（比照 backtest_force_kline.py 同一
種「安全隔離」精神）。

方法論比照 `backtest_force_kline.py`（不是 `backtest_chanlun_chippeak.py`
那種完整進出場模擬）——這裡沒有停損/停利出場條件，單純是「訊號出現後，
固定持有 N 個交易日，股價表現如何」的遠期報酬研究，跟使用者要求的
「持有一段時間後股價的表現」直接對應：
1. **訊號逐日 point-in-time 重算，不是算一次拿來對日期**——`chip_peak.
   compute_chip_peak()` 的時間衰減權重是相對「目前這一天」算的（固定取
   `rows[-lookback:]` 這個滑動視窗），不像技術指標（EMA/MACD）是對全歷史
   一次算好就能索引取值，必須真的逐日呼叫 `compute_chip_peak(rows[:i+1])`
   才能正確重現「當時實際會看到的 POC/VAL」，跟纏論回測（見
   `backtest_chanlun_chippeak.py`）同一個必要性，但原因不同（這裡純粹是
   滑動視窗+相對權重，不是纏論那種「重算會得到不同結果」的演算法不穩定）。
2. **訊號定義**：收盤價、POC（成交量最密集價位）、VAL（籌碼峰下緣）三者
   兩兩價差都在 `--threshold-pct`（預設1%）以內，即 `(max(收盤,POC,VAL) -
   min(收盤,POC,VAL)) / 收盤 <= threshold`。**只看 POC/VAL/收盤三者，不看
   VAH**——使用者原話只提到這三個。
3. **同一段連續符合條件的期間只算一次訊號**（訊號從「不符合」轉為「符合」
   的第一天才記錄）——POC/VAL 是120天滑動視窗算出來的慢變數，符合條件的
   狀態往往連續好幾天甚至好幾週，如果每一天都各自算一次訊號，會產生大量
   高度重複、幾乎是同一個事件的樣本，嚴重膨脹樣本數並扭曲統計量；只在
   「新進入這個狀態」時記一次，比較能代表獨立事件。
4. **多重持有天數**（`--horizons`，預設 5/10/20/40/60 個交易日）：使用者
   沒有指定固定要持有多久，所以同時測多個天期，讓資料自己顯示哪個持有期
   間比較有優勢，不是只挑一個天數。
5. **市場基準**：對每一天，把當天全市場「所有股票」該天期後的報酬率取
   平均，當作那一天的 benchmark（跟 `backtest_gutai.py`/
   `backtest_force_kline.py` 的 `_build_benchmark()` 同一個定義）。訊號
   「獲勝」＝報酬率 > 訊號當天的 benchmark，不是單純報酬率為正就算贏。

已知限制（比照 backtest_force_kline.py/backtest_gutai.py 的 Known caveats
慣例）：
- 無交易成本/滑價，也沒有停損/停利出場（固定天期強制出場，不是真實交易
  系統）
- 存活者偏差：用今天的 `stocks` 表，看不到已下市股票
- 未還原除權息價格，跟全站其他價格類功能一致
- `chip_peak.py` 本身的簡化假設（固定百分比分箱、時間衰減加權、法人品質
  加權在這裡沒有提供三大法人資料因此一律視為中性）一併繼承

Usage:
    python backtest_chip_peak_confluence.py --years-back 5 --threshold-pct 1.0 --horizons 5,10,20,40,60
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

_TZ = ZoneInfo('Asia/Taipei')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def _load_prices(db, since_iso, limit):
    codes = [c for (c,) in db.query(Stock.code).order_by(Stock.code).all()]
    if limit:
        codes = codes[:limit]
    wanted = set(codes)
    out = {}
    result = db.execute(text('''
        SELECT stock_code, date, close, volume FROM daily_prices
        WHERE date >= :since ORDER BY stock_code, date ASC
    '''), {'since': since_iso})
    for r in result.mappings():
        code = r['stock_code']
        if code not in wanted:
            continue
        c = out.setdefault(code, {'dates': [], 'close': [], 'volume': []})
        c['dates'].append(r['date'])
        c['close'].append(r['close'])
        c['volume'].append(r['volume'])
    return out


def _find_confluence_signals(args):
    """Pool worker：單一股票，逐日重算籌碼峰（純CPU，不碰DB），找出
    POC/`level`（'val' 或 'vah'）/收盤三者非常接近的「新進入」訊號日（見
    模組docstring第3點）。"""
    code, dates, closes, volumes, threshold_pct, lookback, half_life, level = args
    n = len(closes)
    rows = [{'date': dates[i], 'close': closes[i], 'volume': volumes[i]} for i in range(n)]
    signals = []
    in_confluence = False
    for i in range(29, n):
        close = closes[i]
        if not close:
            in_confluence = False
            continue
        cp = chip_peak.compute_chip_peak(rows[:i + 1], lookback=lookback, half_life=half_life)
        if not cp:
            in_confluence = False
            continue
        poc, lvl = cp['poc'], cp[level]
        spread_pct = (max(poc, lvl, close) - min(poc, lvl, close)) / close * 100
        is_close = spread_pct <= threshold_pct
        if is_close and not in_confluence:
            signals.append({'date': dates[i], 'i': i, 'entry': close,
                             'poc': poc, level: lvl, 'spread_pct': round(spread_pct, 3)})
        in_confluence = is_close
    return code, signals


def _build_benchmark(stock_data, horizon):
    """date -> 全市場當天該 horizon 天期後平均報酬率（%），比照
    backtest_force_kline.py 的 `_build_benchmark()`。"""
    fwd_by_date = defaultdict(list)
    for data in stock_data.values():
        closes, dates = data['close'], data['dates']
        for i in range(len(closes) - horizon):
            entry, exit_ = closes[i], closes[i + horizon]
            if entry and exit_:
                fwd_by_date[dates[i]].append((exit_ - entry) / entry * 100)
    return {d: statistics.mean(v) for d, v in fwd_by_date.items()}


def run_backtest(years_back, threshold_pct, horizons, lookback, half_life, limit, workers, level='val'):
    db = SessionLocal()
    try:
        since = datetime.now(_TZ).date() - timedelta(days=years_back * 365 + lookback + 30)
        since_iso = since.isoformat()
        logger.info('Loading daily_prices since %s ...', since_iso)
        stock_data = _load_prices(db, since_iso, limit)
        logger.info('Loaded %d stocks', len(stock_data))
    finally:
        db.close()

    tasks = [(code, d['dates'], d['close'], d['volume'], threshold_pct, lookback, half_life, level)
              for code, d in stock_data.items()]
    n_workers = workers or (os.cpu_count() or 4)
    logger.info('Scanning %d stocks for POC/%s/close confluence across %d worker processes...',
                len(tasks), level.upper(), n_workers)
    raw_signals = {}
    with mp.Pool(n_workers) as pool:
        for i, (code, signals) in enumerate(pool.imap_unordered(_find_confluence_signals, tasks), 1):
            if signals:
                raw_signals[code] = signals
            if i % 200 == 0:
                logger.info('  scanned %d/%d stocks', i, len(tasks))

    n_total = sum(len(v) for v in raw_signals.values())
    logger.info('Found %d confluence signals across %d stocks', n_total, len(raw_signals))

    results_by_horizon = {}
    for horizon in horizons:
        bench_by_date = _build_benchmark(stock_data, horizon)
        out = []
        for code, signals in raw_signals.items():
            closes, dates = stock_data[code]['close'], stock_data[code]['dates']
            for s in signals:
                i = s['i']
                if i + horizon >= len(closes):
                    continue
                exit_, entry = closes[i + horizon], s['entry']
                if not exit_ or not entry:
                    continue
                fwd_pct = (exit_ - entry) / entry * 100
                bench = bench_by_date.get(dates[i])
                out.append({
                    'code': code, 'date': str(dates[i]), 'entry': entry,
                    'poc': s['poc'], level: s[level], 'spread_pct': s['spread_pct'],
                    'fwd_return_pct': round(fwd_pct, 2),
                    'benchmark_pct': round(bench, 2) if bench is not None else None,
                })
        results_by_horizon[str(horizon)] = out
    return results_by_horizon


def summarize(signals):
    valid = [s for s in signals if s['benchmark_pct'] is not None]
    out = {'n_signals_total': len(signals), 'n_with_benchmark': len(valid)}
    if valid:
        fwd = [s['fwd_return_pct'] for s in valid]
        bench = [s['benchmark_pct'] for s in valid]
        wins = sum(1 for s in valid if s['fwd_return_pct'] > s['benchmark_pct'])
        out.update({
            'avg_fwd_return_pct': round(statistics.mean(fwd), 2),
            'median_fwd_return_pct': round(statistics.median(fwd), 2),
            'avg_benchmark_pct': round(statistics.mean(bench), 2),
            'win_rate_vs_benchmark_pct': round(wins / len(valid) * 100, 1),
            'positive_return_rate_pct': round(sum(1 for f in fwd if f > 0) / len(fwd) * 100, 1),
        })
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Backtest 籌碼峰POC/VAL/收盤三者接近訊號的持有期後續表現')
    parser.add_argument('--years-back', type=int, default=5)
    parser.add_argument('--threshold-pct', type=float, default=1.0,
                         help='POC/level/收盤三者最大價差佔收盤比例上限(%%)，預設1.0（越小越嚴格）')
    parser.add_argument('--level', type=str, default='val', choices=['val', 'vah'],
                         help='跟POC/收盤比對的籌碼峰價位，val=籌碼峰下緣（預設）、vah=籌碼峰上緣（反向測試）')
    parser.add_argument('--horizons', type=str, default='5,10,20,40,60', help='持有交易日數清單，逗號分隔')
    parser.add_argument('--lookback', type=int, default=120, help='籌碼峰計算窗口天數，跟站上即時功能預設一致')
    parser.add_argument('--half-life', type=int, default=30, help='籌碼峰時間衰減半衰期，跟站上即時功能預設一致')
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--workers', type=int, default=None, help='worker process count (default: all CPU cores)')
    parser.add_argument('--out', type=str, default='backtest_chip_peak_confluence_result.json')
    args = parser.parse_args()

    horizons = [int(x) for x in args.horizons.split(',')]
    results = run_backtest(args.years_back, args.threshold_pct, horizons, args.lookback,
                            args.half_life, args.limit, args.workers, args.level)
    summaries = {h: summarize(sig) for h, sig in results.items()}

    with open(args.out, 'w', encoding='utf-8') as f:
        json.dump({'summary': summaries, 'signals': results}, f, ensure_ascii=False, indent=2)

    logger.info('Summary: %s', json.dumps(summaries, ensure_ascii=False, indent=2))
    logger.info('Full results written to %s', args.out)
