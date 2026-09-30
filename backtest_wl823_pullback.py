"""Backtest：自選股「8/23」拉回5日線（experts.py score_wl823_pullback()）
選股規則的歷史勝率／報酬，預設近1年（`--years-back` 可調整）。獨立、唯讀
腳本，只讀 daily_prices/watchlists/watchlist_stocks，不寫任何資料庫表。

方法論比照 `backtest_chanlun_chippeak.py`（完整進出場模擬，固定停損-10%／
移動停利回檔10%），不是像 `backtest_chip_peak_confluence.py`／
`backtest_force_kline.py` 那種固定天期遠期報酬研究。**訊號本身不需要
逐日重算完整演算法**——不像纏論的筆/中樞會隨新K棒重算而改變過去的判定，
SMA 是對固定歷史窗口的單純算術平均，同一天的 SMA 值不會因為之後多了新
資料而改變，所以每支股票的 SMA 序列只需要算一次、逐日索引取值即可。

**已知限制、務必先讀（跟站上其他回測的「誠實揭露」慣例一致）**：
- **股票池只能用「今天」的自選股清單「8/23」成員（`watchlist_stocks` 表
  沒有存加入清單的日期），套用到過去一年**——這代表清單本身可能帶有選股
  偏誤（清單裡留下的股票，可能就是這段期間表現不錯才被留下來/加進去），
  回測結果沒辦法回答「如果一年前就用這條規則+這份清單去選股會賺多少」，
  只能回答「用這條技術面規則套在『今天這份清單』上，過去一年表現如何」，
  是股票池選擇本身的限制，不是程式錯誤，也不是可以修的 bug。
- 未還原除權息的原始收盤價、無交易成本/滑價，跟站內其他回測一致。

Usage:
    python backtest_wl823_pullback.py --years-back 1
"""
import argparse
import json
import logging
import statistics
from bisect import bisect_right
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from database import SessionLocal
import technical
import experts

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)
_TZ = ZoneInfo('Asia/Taipei')

_STOP_PCT = -0.10     # 固定停損：收盤跌破進場價10%
_TRAIL_PCT = 0.10     # 移動停利：收盤從進場後最高點回檔10%
_PULLBACK_PCT = 0.03  # 貼近5日線正負3%以內，跟 score_wl823_pullback() 一致


def _load_wl823_codes(db):
    """跟 experts._build_context() 同一份查詢，取管理員自選股清單「8/23」
    目前的成員（見 experts._WL823_OWNER_USERNAME/_WL823_NAME）。"""
    rows = db.execute(text('''
        SELECT ws.stock_code FROM watchlist_stocks ws
        INNER JOIN watchlists w ON w.id = ws.watchlist_id
        INNER JOIN users u ON u.id = w.user_id
        WHERE u.username = :uname AND w.name = :wname
    '''), {'uname': experts._WL823_OWNER_USERNAME, 'wname': experts._WL823_NAME}).all()
    return sorted(r[0] for r in rows)


def _load_institutional_net(db, code, since_iso):
    """依日期升冪回傳 (dates, nets) 兩個等長陣列——`nets` 是該股每個交易日
    三大法人合計買賣超（股，正負號跟一般慣例一致，正＝買超），用來算
    `--require-inst-positive` 的近5日合計。跟 experts.py `_build_context()`
    的三大法人欄位是同一張表、同樣的加總方式（外資+投信+自營商淨額），
    只是這裡要整段歷史而不是近5日快照。"""
    rows = db.execute(text('''
        SELECT date, foreign_buy, foreign_sell, trust_buy, trust_sell, dealer_buy, dealer_sell
        FROM institutional_trades WHERE stock_code = :code AND date >= :since ORDER BY date ASC
    '''), {'code': code, 'since': since_iso}).mappings().all()
    dates, nets = [], []
    for r in rows:
        d = r['date']
        if isinstance(d, str):
            d = datetime.strptime(d, '%Y-%m-%d').date()
        net = ((r['foreign_buy'] or 0) - (r['foreign_sell'] or 0)
               + (r['trust_buy'] or 0) - (r['trust_sell'] or 0)
               + (r['dealer_buy'] or 0) - (r['dealer_sell'] or 0))
        dates.append(d)
        nets.append(net)
    return dates, nets


def _inst_5d_sum(dates, nets, as_of_date):
    """回傳 as_of_date（含）之前最近5筆三大法人合計買賣超的和（股）。
    `bisect_right` 找出「日期 <= as_of_date」的筆數，不會看到未來資料。
    不足5筆（資料剛起始，或該股當時尚未被任何人自選過導致沒有法人資料）
    回傳 None——呼叫端把 None 當「未知」處理、不阻擋進場，因為沒有證據
    顯示是負的，不應該武斷擋掉。"""
    idx = bisect_right(dates, as_of_date)
    if idx < 5:
        return None
    return sum(nets[idx - 5:idx])


def simulate_stock(rows, cutoff_date, exit_mode='trailing', inst_dates=None, inst_nets=None):
    """單一股票的完整 no-pyramiding 進出場模擬。rows：依日期升冪，dict含
    date/close，涵蓋 cutoff_date 之前的暖機資料（算SMA60用）。只在
    date >= cutoff_date 那天才允許開新倉，回傳這支股票的完整交易紀錄
    （欄位形狀比照 backtest_chanlun_chippeak.py 的 simulate_stock()，方便
    橫向比較不同規則的回測結果）。

    `exit_mode`：
    - `'trailing'`（預設，跟 score_wl823_pullback() 上線時的假設一致）：
      固定停損-10% + 移動停利回檔10%（見 _STOP_PCT/_TRAIL_PCT）。
    - `'sma5_break'`（2026-09-22 新增，使用者要求比較「跌破5日線就出場」
      這種更貼合訊號本身邏輯的出場條件）：收盤跌破當天SMA5就出場，沒有
      停損/停利百分比門檻——訊號本身就是「貼近5日線」，出場對稱地用
      「不再貼在5日線之上」當條件，比固定百分比更貼合這條規則的原始
      邏輯，但代價是沒有停損保護、下跌段可能持有更久。

    `inst_dates`/`inst_nets`（2026-09-22 新增，`_load_institutional_net()`
    的輸出，選填）：有帶入時，額外要求進場當天往前5個交易日的三大法人
    合計買賣超總和不是負的（`_inst_5d_sum() < 0` 就跳過這次進場機會，
    等下一次訊號再檢查一次，不是整支股票直接排除）；資料不足5筆時視為
    「未知」不擋（見 `_inst_5d_sum()` 說明）。
    """
    n = len(rows)
    if n < 60:
        return []

    closes = [r['close'] for r in rows]
    smas = {p: technical.sma_series(closes, p) for p in (5, 10, 20, 60)}

    trades = []
    in_position = False
    entry_i = entry_price = entry_date = peak = None

    for i in range(n):
        close = rows[i]['close']
        today = rows[i]['date']
        s5 = smas[5][i]

        if not in_position:
            if today < cutoff_date or close is None:
                continue
            s10, s20, s60 = smas[10][i], smas[20][i], smas[60][i]
            if None in (s5, s10, s20, s60):
                continue
            bull = s5 > s10 > s20 > s60
            near_sma5 = abs(close - s5) / s5 <= _PULLBACK_PCT
            if bull and near_sma5 and inst_dates is not None:
                inst_5d = _inst_5d_sum(inst_dates, inst_nets, today)
                if inst_5d is not None and inst_5d < 0:
                    continue  # 近5日三大法人合計是賣超，這次訊號跳過不進場
            if bull and near_sma5:
                in_position = True
                entry_i, entry_price, entry_date = i, close, today
                peak = close  # 移動停利的高點起算點＝進場價本身
        else:
            if close is None:
                continue
            exit_reason = None
            if exit_mode == 'sma5_break':
                if s5 is not None and close < s5:
                    exit_reason = 'sma5_break'
            else:
                peak = max(peak, close)
                ret = (close - entry_price) / entry_price
                if peak > entry_price and close <= peak * (1 - _TRAIL_PCT):
                    exit_reason = 'trailing'
                elif ret <= _STOP_PCT:
                    exit_reason = 'stop'
            if exit_reason:
                ret = (close - entry_price) / entry_price
                trades.append({
                    'entry_date': str(entry_date), 'exit_date': str(today),
                    'entry_price': entry_price, 'exit_price': close,
                    'return_pct': round(ret * 100, 2),
                    'holding_days': i - entry_i,
                    'exit_reason': exit_reason,
                })
                in_position = False

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


def summarize(results_by_code):
    """跟 backtest_chanlun_chippeak.py 的 summarize() 完全同一套統計定義。"""
    trades = [t for trades in results_by_code.values() for t in trades]
    closed = [t for t in trades if t['exit_reason'] != 'open']
    out = {
        'n_stocks_with_trades': len(results_by_code),
        'n_trades_total': len(trades),
        'n_closed': len(closed),
        'n_still_open': len(trades) - len(closed),
    }
    if closed:
        by_reason = {}
        for t in closed:
            by_reason[t['exit_reason']] = by_reason.get(t['exit_reason'], 0) + 1
        returns = [t['return_pct'] for t in closed]
        holding = [t['holding_days'] for t in closed]
        # 出場方式不保證賺錢（移動停利/跌破5日線都可能小賺小賠就出場），
        # 「勝率」一律直接看已實現報酬是不是正的，不用 exit_reason 當代理指標。
        n_wins = sum(1 for t in closed if t['return_pct'] > 0)
        out.update({
            'exit_reason_counts': by_reason,
            'win_rate_pct': round(n_wins / len(closed) * 100, 1),
            'avg_return_pct': round(statistics.mean(returns), 2),
            'median_return_pct': round(statistics.median(returns), 2),
            'avg_holding_days': round(statistics.mean(holding), 1),
        })
    return out


def run_backtest(years_back=1, limit=None, exit_mode='trailing', require_inst_positive=False):
    db = SessionLocal()
    try:
        codes = _load_wl823_codes(db)
        if limit:
            codes = codes[:limit]
        logger.info('Loaded %d stocks from watchlist "%s"', len(codes), experts._WL823_NAME)

        cutoff = datetime.now(_TZ).date() - timedelta(days=years_back * 365)
        since = cutoff - timedelta(days=120)  # SMA60 暖機緩衝
        # 法人5日合計要在 cutoff 當天就能算，額外多留12個曆日緩衝
        # （比照 experts.py _build_context 抓近5日法人資料的既有窗口寬度）。
        inst_since = cutoff - timedelta(days=12)

        results = {}
        for i, code in enumerate(codes, 1):
            rows = [dict(r) for r in db.execute(text('''
                SELECT date, close FROM daily_prices
                WHERE stock_code = :code AND date >= :since ORDER BY date ASC
            '''), {'code': code, 'since': since.isoformat()}).mappings()]
            for r in rows:
                if isinstance(r['date'], str):
                    r['date'] = datetime.strptime(r['date'], '%Y-%m-%d').date()

            inst_dates = inst_nets = None
            if require_inst_positive:
                inst_dates, inst_nets = _load_institutional_net(db, code, inst_since.isoformat())

            trades = simulate_stock(rows, cutoff, exit_mode, inst_dates, inst_nets)
            if trades:
                results[code] = trades
            if i % 50 == 0:
                logger.info('  simulated %d/%d stocks', i, len(codes))
        return results
    finally:
        db.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Backtest 自選股「8/23」拉回5日線選股規則，可切換出場方式')
    parser.add_argument('--years-back', type=int, default=1, help='how many years of history to scan (default 1)')
    parser.add_argument('--limit', type=int, default=None, help='only test the first N stocks (for quick runs)')
    parser.add_argument('--exit-mode', type=str, default='trailing', choices=['trailing', 'sma5_break'],
                         help='trailing=固定停損-10%%/移動停利回檔10%%（預設）；sma5_break=跌破當天SMA5就出場')
    parser.add_argument('--require-inst-positive', action='store_true',
                         help='進場當天往前5個交易日三大法人合計買賣超為負則跳過該次訊號')
    parser.add_argument('--out', type=str, default=None,
                         help='default: backtest_wl823_pullback_result[_<exit-mode>][_instfilter].json')
    args = parser.parse_args()
    suffix = ('' if args.exit_mode == 'trailing' else f'_{args.exit_mode}') + \
             ('_instfilter' if args.require_inst_positive else '')
    out_path = args.out or f'backtest_wl823_pullback_result{suffix}.json'

    results = run_backtest(args.years_back, args.limit, args.exit_mode, args.require_inst_positive)
    summary = summarize(results)

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'summary': summary, 'trades': results}, f, ensure_ascii=False, indent=2)

    logger.info('Summary: %s', json.dumps(summary, ensure_ascii=False, indent=2))
    logger.info('Full results written to %s', out_path)
