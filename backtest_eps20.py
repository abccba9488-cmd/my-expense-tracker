"""「自結eps<20」自選清單回測——僅本機 CLI，不寫 DB。

這份自選清單由 /rate-announcements 依「自結公告的預估本益比介於 0~20」自動
加入（見 CLAUDE.md「自結公告」「自結eps<20 清單的入榜日期」），這裡用
announcements 表全部歷史（2023 年起）重現同一條件：

  進場：公告日的「下一個交易日」開盤價買進（開盤價缺值時退用收盤價）。
        公告多在盤後（15:00 後）發布，隔日開盤是實際可成交的最早時點。
  出場：跟 backtest_expert_signals.py / backtest_accumulation.py 相同——收盤
        跌破進場價 (1-STOP) 停損，或收盤自進場後最高收盤回檔 TRAIL 移動停利，
        先到先出、不設封頂；到資料最後一天仍未觸發則以最後收盤價計為「持有中」。
  不加碼：同一檔持倉期間的新公告忽略。

比較組：預估本益比 0~20（目標）／>=20／虧損或無法計算／全部自結公告，以及
對照組「同期流動股（近10日均量>500張）每20個交易日隔日開盤定期進場」。
另附固定持有 60 個交易日（以進場價計）的平均報酬供參考。

簡化：未還原除權息、無交易成本、存活者偏差。

Usage:
    python backtest_eps20.py [--stop 0.1 --trail 0.1]
"""
import argparse
import bisect
import json
import sqlite3
from statistics import mean, median

DB = 'data/stocks.db'
HOLD_FIXED = 60


def load_prices(con, code, cache):
    if code not in cache:
        rows = con.execute('SELECT date, open, close, volume FROM daily_prices WHERE stock_code=? '
                           'AND date >= ? ORDER BY date', (code, '2022-10-01')).fetchall()
        cache[code] = ([r[0] for r in rows], rows)
    return cache[code]


def simulate(rows, k, stop, trail):
    """Enter at rows[k] open; walk closes from day k. Returns (ret%, days, exit_idx, open?, reason)."""
    entry = rows[k][1] or rows[k][2]
    if not entry:
        return None
    peak = entry
    for j in range(k, len(rows)):
        c = rows[j][2]
        if c is None:
            continue
        peak = max(peak, c)
        if c <= entry * (1 - stop):
            return (c / entry - 1) * 100, j - k, j, False, 'stop'
        if c <= peak * (1 - trail):
            return (c / entry - 1) * 100, j - k, j, False, 'trail'
    last = rows[-1][2]
    return (last / entry - 1) * 100, len(rows) - 1 - k, len(rows) - 1, True, None


def fixed_ret(rows, k):
    entry = rows[k][1] or rows[k][2]
    j = k + HOLD_FIXED
    if j >= len(rows) or not entry or rows[j][2] is None:
        return None
    return (rows[j][2] / entry - 1) * 100


def summarize(trades):
    if not trades:
        return None
    rets = [t['ret'] for t in trades]
    closed = [t for t in trades if not t['open']]
    fx = [t['fixed'] for t in trades if t['fixed'] is not None]
    return {
        'n': len(trades), 'mean': mean(rets), 'median': median(rets),
        'win': sum(r > 0 for r in rets) / len(rets) * 100,
        'days': mean(t['days'] for t in trades),
        'stop_pct': sum(t['reason'] == 'stop' for t in closed) / len(closed) * 100 if closed else 0,
        'open': sum(t['open'] for t in trades),
        'fixed60': mean(fx) if fx else None, 'fixed60_n': len(fx),
    }


def run_group(con, signals, stop, trail, cache):
    """signals: list of (code, signal_date) sorted by date. Entry = first trading day > signal_date."""
    trades, busy_until = [], {}
    for code, sdate in signals:
        dates, rows = load_prices(con, code, cache)
        k = bisect.bisect_right(dates, sdate)
        if k >= len(rows):
            continue
        if busy_until.get(code, -1) >= k:
            continue   # still holding this stock — no pyramiding
        r = simulate(rows, k, stop, trail)
        if not r:
            continue
        ret, days, exit_idx, is_open, reason = r
        busy_until[code] = exit_idx
        trades.append({'code': code, 'date': dates[k], 'ret': ret, 'days': days, 'open': is_open,
                       'reason': reason, 'fixed': fixed_ret(rows, k)})
    return trades


def control_signals(con, start, end):
    """Every 20th trading day per liquid stock (10-day avg volume > 500 張) within [start, end]."""
    out = []
    for (code,) in con.execute('SELECT code FROM stocks'):
        rows = con.execute('SELECT date, volume FROM daily_prices WHERE stock_code=? AND date>=? AND date<=? '
                           'ORDER BY date', (code, start, end)).fetchall()
        for i in range(10, len(rows), 20):
            v = [r[1] for r in rows[i - 10:i] if r[1] is not None]
            if v and mean(v) > 500_000:
                out.append((code, rows[i][0]))
    out.sort(key=lambda x: x[1])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--stop', type=float, default=0.10)
    ap.add_argument('--trail', type=float, default=0.10)
    args = ap.parse_args()

    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    anns = con.execute('SELECT stock_code, announce_date, estimated_pe FROM announcements '
                       'ORDER BY announce_date, announce_time').fetchall()
    start, end = anns[0][1], anns[-1][1]
    groups = {
        '目標：預估本益比0~20（自結eps<20）': [(c, d) for c, d, pe in anns if pe is not None and 0 < pe < 20],
        '預估本益比>=20':                      [(c, d) for c, d, pe in anns if pe is not None and pe >= 20],
        '虧損／無法計算本益比':                [(c, d) for c, d, pe in anns if pe is None or pe <= 0],
        '全部自結公告':                        [(c, d) for c, d, _ in anns],
        '對照組：流動股每20日定期進場':        control_signals(con, start, end),
    }
    cache = {}
    out = {'stop': args.stop, 'trail': args.trail, 'period': [start, end], 'groups': {}}
    print(f'\n期間 {start} ~ {end}｜隔日開盤進場｜停損{args.stop:.0%}／移動停利（最高收盤回檔{args.trail:.0%}）｜不加碼')
    print(f"{'組別':<30}{'交易數':>7}{'平均報酬':>10}{'中位數':>9}{'勝率':>8}{'平均持有日':>10}{'停損比例':>9}{'持有中':>7}{'固定60日平均':>13}")
    for name, sig in groups.items():
        s = summarize(run_group(con, sig, args.stop, args.trail, cache))
        out['groups'][name] = s
        if s:
            print(f"{name:<30}{s['n']:>7}{s['mean']:>+9.2f}%{s['median']:>+8.2f}%{s['win']:>7.1f}%{s['days']:>10.0f}"
                  f"{s['stop_pct']:>8.1f}%{s['open']:>7}{(s['fixed60'] if s['fixed60'] is not None else float('nan')):>+12.2f}%")
    suffix = '' if (args.stop, args.trail) == (0.10, 0.10) else f'_stop{args.stop:g}_trail{args.trail:g}'
    with open(f'backtest_eps20_result{suffix}.json', 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
