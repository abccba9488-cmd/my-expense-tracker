"""投信買點（experts.score_trust_buy）門檻驗證回測——僅本機 CLI，不寫 DB。

方法同 backtest_accumulation.py：訊號當天收盤進場，量測 20／60 交易日前瞻
報酬 vs 全市場全日無條件平均（baseline），同股 COOLDOWN 日內重複訊號只算一次；
另跑一次停損＋移動停利出場模擬。

兩類訊號：
  A 投信認養初期：前 60 個交易日（不含近5日）投信幾乎沒買，近5日開始連續買超
  B 突破投信成本線：成本線 = 近60日投信「買超日」以買超股數加權的均價
    （均價用 (H+L+C)/3 近似），收盤由下往上穿越成本線

簡化：未還原除權息、無交易成本、存活者偏差（只含目前仍在 stocks 表的股票）。

Usage:
    python backtest_trust.py [--from 2016-01-01] [--limit 200]
"""
import argparse
import json
import logging
import sqlite3
from statistics import mean, median

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

DB = 'data/stocks.db'
HORIZONS = (20, 60)
COOLDOWN = 20
STOP, TRAIL = 0.10, 0.10
COST_WIN = 60
CONTROL = 'Z_每20日定期進場(對照組)'


def _adopt(f, prior_max=3, recent_min=3, dist20=None, ratio=None):
    if f['prior_buy_days'] is None or f['prior_buy_days'] > prior_max:
        return False
    if f['recent_buy_days'] < recent_min or f['net5'] <= 0:
        return False
    if dist20 is not None and not f['dist20'] < dist20:
        return False
    if ratio is not None and not (f['vol5'] and f['net5'] / f['vol5'] * 100 >= ratio):
        return False
    return True


def _pullback(f, near=3, run_up=10):
    """Price ran >= run_up% above the cost line within 20 sessions, now back within
    0..near% above it (投信成本線 acting as support), 投信 still net long."""
    if f['cost'] is None or f['net60'] <= 0 or f['net5'] < 0 or f['max_gap20'] is None:
        return False
    gap = (f['close'] / f['cost'] - 1) * 100
    return 0 <= gap <= near and f['max_gap20'] >= run_up


def _cross(f, need_net5=False, above_ma20=False, net60_min=0):
    if f['cost'] is None or f['prev_cost'] is None or f['net60'] <= net60_min:
        return False
    if not (f['prev_close'] <= f['prev_cost'] and f['close'] > f['cost']):
        return False
    if need_net5 and f['net5'] <= 0:
        return False
    if above_ma20 and not f['close'] > f['ma20']:
        return False
    return True


VARIANTS = {
    'A1_前60日買超<=3日,近5日>=3日':   lambda f: _adopt(f),
    'A2_A1+距MA20<10%':                lambda f: _adopt(f, dist20=10),
    'A3_A2+近5日買超>=成交量5%':       lambda f: _adopt(f, dist20=10, ratio=5),
    'A4_A2+近5日買超>=成交量10%':      lambda f: _adopt(f, dist20=10, ratio=10),
    'A5_前60日<=5日,近5日>=3日,MA20<10%': lambda f: _adopt(f, prior_max=5, dist20=10),
    'A6_前60日=0日,近5日>=3日,MA20<10%':  lambda f: _adopt(f, prior_max=0, dist20=10),
    'A7_A1+收盤>MA20>MA60(順勢)':      lambda f: _adopt(f) and f['close'] > f['ma20'] > f['ma60'],
    'A8_A1+60日漲幅>0':                lambda f: _adopt(f) and f['ret60'] > 0,
    'B1_站上投信成本線':               lambda f: _cross(f),
    'B2_B1+近5日投信仍買超':           lambda f: _cross(f, need_net5=True),
    'B3_B2+收盤>MA20':                 lambda f: _cross(f, need_net5=True, above_ma20=True),
    'B4_B2+MA20>MA60(順勢)':            lambda f: _cross(f, need_net5=True) and f['ma20'] > f['ma60'],
    'E1_回測成本線(曾高10%,現0~3%)':    lambda f: _pullback(f),
    'E2_E1+MA20>MA60':                 lambda f: _pullback(f) and f['ma20'] > f['ma60'],
}


def run_stock(con, code, since, until='9999-12-31'):
    px = con.execute('SELECT date, high, low, close, volume FROM daily_prices WHERE stock_code=? AND date>=? '
                     'AND date<? ORDER BY date', (code, since, until)).fetchall()
    if len(px) < 140:
        return None
    dates = [r[0] for r in px]
    close = [r[3] for r in px]
    if any(c is None for c in close):
        return None
    vol = [r[4] for r in px]
    typ = [((r[1] or r[3]) + (r[2] or r[3]) + r[3]) / 3 for r in px]
    idx = {d: i for i, d in enumerate(dates)}
    n = len(px)

    tnet = [None] * n
    for d, tb, ts in con.execute('SELECT date, trust_buy, trust_sell FROM institutional_trades '
                                 'WHERE stock_code=? AND date>=?', (code, since)):
        i = idx.get(d)
        if i is not None:
            tnet[i] = (tb or 0) - (ts or 0)

    # prefix sums of buy-day shares and shares*price for the cost line
    pw, ps = [0], [0]
    for t, p in zip(tnet, typ):
        b = t if t and t > 0 else 0
        pw.append(pw[-1] + b)
        ps.append(ps[-1] + b * p)

    def cost_at(i):
        """Buy-day-weighted avg price over the COST_WIN sessions ending at i."""
        w = pw[i + 1] - pw[i - COST_WIN + 1]
        return (ps[i + 1] - ps[i - COST_WIN + 1]) / w if w else None

    feats = []
    start = COST_WIN + 6
    for i in range(start, n):
        win65 = tnet[i - 64:i + 1]
        if sum(t is not None for t in win65) < 55:   # tolerate a few missing days
            continue
        prior = tnet[i - 64:i - 4]
        recent = tnet[i - 4:i + 1]
        net5 = sum(t or 0 for t in recent)
        v5 = [v for v in vol[i - 4:i + 1] if v is not None]
        v10 = [v for v in vol[i - 9:i + 1] if v is not None]
        ma20 = sum(close[i - 19:i + 1]) / 20
        cost_now = cost_at(i)
        gaps = [(close[j] / c - 1) * 100 for j in range(i - 19, i + 1) if (c := cost_at(j))]
        feats.append((i, {
            'prior_buy_days': sum(1 for t in prior if t and t > 0),
            'recent_buy_days': sum(1 for t in recent if t and t > 0),
            'net5': net5, 'vol5': sum(v5) if v5 else None,
            'net60': sum(t or 0 for t in tnet[i - COST_WIN + 1:i + 1]),
            'close': close[i], 'prev_close': close[i - 1],
            'cost': cost_now, 'prev_cost': cost_at(i - 1),
            'max_gap20': max(gaps) if gaps else None,
            'ma60': sum(close[i - 59:i + 1]) / 60, 'ret60': (close[i] / close[i - 60] - 1) * 100,
            'ma20': ma20, 'dist20': (close[i] / ma20 - 1) * 100,
            'avg_vol10': mean(v10) if v10 else 0,
        }))

    def fwd(i, h):
        return (close[i + h] / close[i] - 1) * 100 if i + h < n and close[i] else None

    hits = {name: [(i, f['avg_vol10'] > 500_000 and fn(f)) for i, f in feats] for name, fn in VARIANTS.items()}
    # C: adoption (A2) within the last 20 sessions, then cross above the cost line
    a2 = VARIANTS['A2_A1+距MA20<10%']
    last_a, seq = -10**9, []
    for i, f in feats:
        if a2(f):
            last_a = i
        seq.append((i, f['avg_vol10'] > 500_000 and i - last_a < 20 and _cross(f, need_net5=True)))
    hits['C_A2後20日內站上成本線'] = seq
    hits['D_A2或B2'] = [(i, a or b) for (i, a), (_, b) in
                        zip(hits['A2_A1+距MA20<10%'], hits['B2_B1+近5日投信仍買超'])]
    hits['F_A7或E1'] = [(i, a or b) for (i, a), (_, b) in
                        zip(hits['A7_A1+收盤>MA20>MA60(順勢)'], hits['E1_回測成本線(曾高10%,現0~3%)'])]
    hits[CONTROL] = [(i, i % 20 == 0 and f['avg_vol10'] > 500_000) for i, f in feats]

    base = {h: [r for i, _ in feats if (r := fwd(i, h)) is not None] for h in HORIZONS}
    events, trades = {}, {}
    for name, seq in hits.items():
        last, ev = -10**9, []
        for i, hit in seq:
            if hit:
                if i - last > COOLDOWN:
                    ev.append({h: fwd(i, h) for h in HORIZONS} | {'date': dates[i]})
                last = i
        events[name] = ev
        tr, free_from = [], 0
        for i, hit in seq:
            if not hit or i < free_from:
                continue
            entry = peak = close[i]
            exit_i, reason = None, None
            for j in range(i + 1, n):
                peak = max(peak, close[j])
                if close[j] <= entry * (1 - STOP):
                    exit_i, reason = j, 'stop'
                    break
                if close[j] <= peak * (1 - TRAIL):
                    exit_i, reason = j, 'trail'
                    break
            end = exit_i if exit_i is not None else n - 1
            tr.append({'ret': (close[end] / entry - 1) * 100, 'days': end - i,
                       'open': exit_i is None, 'reason': reason})
            free_from = end + 1
        trades[name] = tr
    return base, events, trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='since', default='2016-01-01')
    ap.add_argument('--until', default='9999-12-31', help='exclusive end date')
    ap.add_argument('--limit', type=int, default=None)
    args = ap.parse_args()

    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    codes = [r[0] for r in con.execute('SELECT code FROM stocks ORDER BY code')]
    if args.limit:
        codes = codes[:args.limit]
    base = {h: [] for h in HORIZONS}
    events, trades = {}, {}
    for k, code in enumerate(codes, 1):
        r = run_stock(con, code, args.since, args.until)
        if r:
            b, e, t = r
            for h in HORIZONS:
                base[h].extend(b[h])
            for name in e:
                events.setdefault(name, []).extend(e[name])
                trades.setdefault(name, []).extend(t[name])
        if k % 200 == 0:
            logger.info('%d / %d stocks', k, len(codes))

    out = {'baseline': {h: {'n': len(base[h]), 'mean': mean(base[h]), 'median': median(base[h]),
                            'win': sum(x > 0 for x in base[h]) / len(base[h]) * 100} for h in HORIZONS},
           'variants': {}, 'trailing': {}}
    print('\nBaseline（全部股票×全部交易日）: ' + '  '.join(
        f"{h}日 平均{out['baseline'][h]['mean']:+.2f}% 勝率{out['baseline'][h]['win']:.1f}%" for h in HORIZONS))
    print(f"{'規則':<34}{'事件數':>8}" + ''.join(f"{f'{h}日平均':>10}{f'{h}日超額':>10}{f'{h}日勝率':>9}" for h in HORIZONS))
    for name, ev in events.items():
        row = {'n': len(ev)}
        line = f'{name:<34}{len(ev):>8}'
        for h in HORIZONS:
            vals = [e[h] for e in ev if e[h] is not None]
            if vals:
                m = mean(vals)
                row[h] = {'mean': m, 'median': median(vals), 'win': sum(x > 0 for x in vals) / len(vals) * 100,
                          'excess': m - out['baseline'][h]['mean']}
                line += f"{m:>+9.2f}%{row[h]['excess']:>+9.2f}%{row[h]['win']:>8.1f}%"
        out['variants'][name] = row
        print(line)
    print(f'\n停損{STOP:.0%}／移動停利（最高點回檔{TRAIL:.0%}）出場，不加碼：')
    print(f"{'規則':<34}{'交易數':>8}{'平均報酬':>10}{'中位數':>9}{'勝率':>8}{'平均持有日':>10}")
    for name, tr in trades.items():
        if not tr:
            continue
        rets = [t['ret'] for t in tr]
        row = {'n': len(tr), 'mean': mean(rets), 'median': median(rets),
               'win': sum(x > 0 for x in rets) / len(rets) * 100, 'days': mean(t['days'] for t in tr)}
        out['trailing'][name] = row
        print(f"{name:<34}{row['n']:>8}{row['mean']:>+9.2f}%{row['median']:>+8.2f}%{row['win']:>7.1f}%{row['days']:>10.0f}")
    suffix = '' if args.until == '9999-12-31' else f'_{args.since}_{args.until}'
    with open(f'backtest_trust_result{suffix}.json', 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
