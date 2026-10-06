"""主力吸貨（experts.score_accumulation）門檻驗證回測——僅本機 CLI，不寫 DB。

跟 backtest_expert_signals.py 的「進場＋移動停利」交易模擬不同，這裡用更
直接的**前瞻報酬**檢驗：訊號出現當天收盤進場，量測之後 20／60 個交易日的
報酬，跟同期間「全部股票、全部交易日」的無條件平均報酬（baseline）比較，
看訊號有沒有超額報酬。目的是在正式上線前比較幾組門檻（ChatGPT 原始版 vs
加了最小變動門檻／股價未漲多硬門檻的版本）。

Point-in-time：所有特徵都是「截至當天為止」的滾動視窗（法人 20 日累計、
融資 20 日變化、MA20/60、量價），股權分散只用資料日期 < 當天的最新 5 週
（集保週資料週五才公布，嚴格小於避免偷看）。

去重：同一檔股票訊號連續出現時只算第一天，之後要至少 20 個交易日沒有
訊號才算新事件（避免同一波段重複計數灌水樣本數）。

簡化：未還原除權息、無交易成本、存活者偏差（只含目前仍在 stocks 表的股票）。

Usage:
    python backtest_accumulation.py [--from 2017-01-01] [--limit 200]
"""
import argparse
import bisect
import json
import logging
import sqlite3
from statistics import mean, median

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

DB = 'data/stocks.db'
HORIZONS = (20, 60)
COOLDOWN = 20

# name -> thresholds. big/small: 4-week change in percentage points required to
# count as 增加/下降; dist60_req: hard gate on close/MA60-1 (None = scored only);
# rate: pass threshold on score/max_score.
VARIANTS = {
    'A_原始(任何變動,MA60僅計分)': dict(big=0.0, small=0.0, dist60_req=None, rate=75),
    'B_大戶+0.5/散戶-0.5':          dict(big=0.5, small=-0.5, dist60_req=None, rate=75),
    'C_原始+距MA60<15%硬門檻':      dict(big=0.0, small=0.0, dist60_req=15, rate=75),
    'D_B+距MA60<15%硬門檻':         dict(big=0.5, small=-0.5, dist60_req=15, rate=75),
    'E_D+得分率>=85%':              dict(big=0.5, small=-0.5, dist60_req=15, rate=85),
    # stricter variants (user asked for tighter conditions)
    'F_大戶+1/散戶-1,MA60<15%,85%':  dict(big=1.0, small=-1.0, dist60_req=15, rate=85),
    'G_大戶+0.5,MA60<10%,90%':       dict(big=0.5, small=-0.5, dist60_req=10, rate=90),
    'H_F+外資投信皆買超':            dict(big=1.0, small=-1.0, dist60_req=15, rate=85, inst_req=True),
    'I_H+MA60<10%,90%':              dict(big=1.0, small=-1.0, dist60_req=10, rate=90, inst_req=True),
    'J_大戶+1為硬門檻+H':            dict(big=1.0, small=-1.0, dist60_req=15, rate=85, inst_req=True, big_req=True),
}
_H = VARIANTS['H_F+外資投信皆買超']

# 帶量突破 = close > highest close of the previous 20 sessions AND volume >=
# 1.5x the previous 20-session average volume. Modes:
#   same     - H passes and breakout on the same day
#   setup    - H passed within the last SETUP_WINDOW sessions (incl. today), enter on a breakout day
#   breakout - breakout alone, no chip filter (control group)
SETUP_WINDOW = 20
CONTROL = 'Z_每20日定期進場(對照組)'
STOP, TRAIL = 0.10, 0.10   # same as backtest_expert_signals.py / backtest_chanlun_chippeak.py
BREAKOUT_VARIANTS = {
    'K_H且當天帶量突破':            'same',
    'L_H後20日內等帶量突破進場':    'setup',
    'M_只看帶量突破(對照組)':       'breakout',
    'N1_H且當天沒帶量突破':         'same_no',
    'N2_H且近20日都沒帶量突破':     'recent_no',
}


def score(f, v):
    """Mirror of experts.score_accumulation's points (下跌縮量 10 → total 100)."""
    pts = mx = 0

    def award(cond, p):
        nonlocal pts, mx
        if cond is None:
            return
        mx += p
        if cond:
            pts += p

    award(f['big'] > v['big'] if f['big'] is not None else None, 20)
    award(f['small'] < v['small'] if f['small'] is not None else None, 10)
    award(f['margin'] < 0 if f['margin'] is not None else None, 10)
    award(f['foreign'] > 0 if f['foreign'] is not None else None, 10)
    award(f['trust'] > 0 if f['trust'] is not None else None, 10)
    award(f['dealer'] > 0 if f['dealer'] is not None else None, 5)
    award(f['up_gt_down'], 10)
    award(f['down_dry'], 10)
    award(f['close'] > f['ma20'], 5)
    award(f['ma20'] > f['ma60'], 5)
    award(f['dist60'] < 15, 5)
    if f['avg_vol10'] <= 500_000 or mx < 70:
        return False
    if v['dist60_req'] is not None and not f['dist60'] < v['dist60_req']:
        return False
    if v.get('inst_req') and not (f['foreign'] and f['foreign'] > 0 and f['trust'] and f['trust'] > 0):
        return False
    if v.get('big_req') and not (f['big'] is not None and f['big'] >= v['big']):
        return False
    return pts / mx * 100 >= v['rate']


def _is_breakout(close, vol, i):
    if i < 21 or vol[i] is None:
        return False
    prev_v = [x for x in vol[i - 20:i] if x is not None]
    return bool(prev_v) and close[i] > max(close[i - 20:i]) and vol[i] >= 1.5 * mean(prev_v)


def run_stock(con, code, since):
    px = con.execute('SELECT date, close, volume FROM daily_prices WHERE stock_code=? AND date>=? '
                     'ORDER BY date', (code, since)).fetchall()
    if len(px) < 140:
        return None
    dates = [r[0] for r in px]
    close = [r[1] for r in px]
    vol = [r[2] for r in px]
    if any(c is None for c in close):
        return None
    idx = {d: i for i, d in enumerate(dates)}
    n = len(px)

    inst = [[0, 0, 0] for _ in range(n)]
    has_inst = [False] * n
    for d, fb, fs, tb, ts, db_, ds in con.execute(
            'SELECT date, foreign_buy, foreign_sell, trust_buy, trust_sell, dealer_buy, dealer_sell '
            'FROM institutional_trades WHERE stock_code=? AND date>=?', (code, since)):
        i = idx.get(d)
        if i is not None:
            inst[i] = [(fb or 0) - (fs or 0), (tb or 0) - (ts or 0), (db_ or 0) - (ds or 0)]
            has_inst[i] = True
    margin = [None] * n
    for d, m in con.execute('SELECT date, margin_balance FROM margin_trades WHERE stock_code=? AND date>=?',
                            (code, since)):
        i = idx.get(d)
        if i is not None:
            margin[i] = m
    hold = con.execute('SELECT date, pct_400up, pct_100down FROM holding_concentration '
                       'WHERE stock_code=? ORDER BY date', (code,)).fetchall()
    hdates = [h[0] for h in hold]

    # prefix sums
    def prefix(arr):
        out = [0]
        for x in arr:
            out.append(out[-1] + x)
        return out
    pc = prefix(close)
    pf, pt, pdl = prefix([x[0] for x in inst]), prefix([x[1] for x in inst]), prefix([x[2] for x in inst])
    pcnt = prefix([1 if h else 0 for h in has_inst])

    feats = []
    for i in range(60, n):
        ma20 = (pc[i + 1] - pc[i - 19]) / 20
        ma60 = (pc[i + 1] - pc[i - 59]) / 60
        up_v, down_v, all_v = [], [], []
        for j in range(i - 19, i + 1):
            if vol[j] is None:
                continue
            all_v.append(vol[j])
            if close[j] > close[j - 1]:
                up_v.append(vol[j])
            elif close[j] < close[j - 1]:
                down_v.append(vol[j])
        vol20 = mean(all_v) if all_v else None
        rd = [vol[j] for j in range(i - 4, i + 1) if close[j] < close[j - 1] and vol[j] is not None]
        v10 = [x for x in vol[i - 9:i + 1] if x is not None]
        inst_ok = pcnt[i + 1] - pcnt[i - 19] >= 15
        hk = bisect.bisect_left(hdates, dates[i]) - 1   # latest holding row strictly before today
        big = small = None
        if hk >= 4:
            h0, h4 = hold[hk], hold[hk - 4]
            if None not in (h0[1], h4[1]):
                big = h0[1] - h4[1]
            if None not in (h0[2], h4[2]):
                small = h0[2] - h4[2]
        m0, m20 = margin[i], margin[i - 20]
        feats.append((i, {
            'big': big, 'small': small,
            'margin': (m0 - m20) if m0 is not None and m20 else None,
            'foreign': (pf[i + 1] - pf[i - 19]) if inst_ok else None,
            'trust': (pt[i + 1] - pt[i - 19]) if inst_ok else None,
            'dealer': (pdl[i + 1] - pdl[i - 19]) if inst_ok else None,
            'up_gt_down': (mean(up_v) > mean(down_v)) if up_v and down_v else None,
            'down_dry': ((mean(rd) < vol20) if rd else True) if vol20 else None,
            'close': close[i], 'ma20': ma20, 'ma60': ma60, 'dist60': (close[i] / ma60 - 1) * 100,
            'avg_vol10': mean(v10) if v10 else 0,
            'breakout': _is_breakout(close, vol, i),
        }))

    def fwd(i, h):
        return (close[i + h] / close[i] - 1) * 100 if i + h < n and close[i] else None

    # One boolean hit series per rule, shared by both exit modes
    h_pass = {i: score(f, _H) for i, f in feats}
    hits = {name: [(i, score(f, v)) for i, f in feats] for name, v in VARIANTS.items()}
    for name, mode in BREAKOUT_VARIANTS.items():
        last_setup, last_break, seq = -10**9, -10**9, []
        for i, f in feats:
            if h_pass[i]:
                last_setup = i
            if f['breakout']:
                last_break = i
            if mode == 'same':
                hit = h_pass[i] and f['breakout']
            elif mode == 'setup':
                hit = f['breakout'] and i - last_setup < SETUP_WINDOW
            elif mode == 'same_no':
                hit = h_pass[i] and not f['breakout']
            elif mode == 'recent_no':
                hit = h_pass[i] and i - last_break >= SETUP_WINDOW
            else:
                hit = f['breakout'] and f['avg_vol10'] > 500_000
            seq.append((i, hit))
        hits[name] = seq
    # Control group: enter every 20th session regardless of signal (liquid stocks only)
    hits[CONTROL] = [(i, i % 20 == 0 and f['avg_vol10'] > 500_000) for i, f in feats]

    base = {h: [r for i, _ in feats if (r := fwd(i, h)) is not None] for h in HORIZONS}
    events, trades = {}, {}
    for name, seq in hits.items():
        # fixed horizon: dedupe repeated signals within COOLDOWN sessions
        last, ev = -10**9, []
        for i, hit in seq:
            if hit:
                if i - last > COOLDOWN:
                    ev.append({h: fwd(i, h) for h in HORIZONS} | {'date': dates[i]})
                last = i
        events[name] = ev
        # trailing: same exit as backtest_expert_signals.py — close <= entry*(1-STOP)
        # (fixed stop) or close <= peak*(1-TRAIL) (trailing take-profit), no cap,
        # no pyramiding (signals ignored while holding)
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
                       'open': exit_i is None, 'reason': reason, 'date': dates[i]})
            free_from = end + 1
        trades[name] = tr
    return base, events, trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from', dest='since', default='2016-01-01')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--stop', type=float, default=0.10, help='fixed stop-loss fraction (default 0.10)')
    ap.add_argument('--trail', type=float, default=0.10, help='trailing take-profit fraction (default 0.10)')
    args = ap.parse_args()
    global STOP, TRAIL
    STOP, TRAIL = args.stop, args.trail

    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    codes = [r[0] for r in con.execute('SELECT code FROM stocks ORDER BY code')]
    if args.limit:
        codes = codes[:args.limit]
    base = {h: [] for h in HORIZONS}
    names = list(VARIANTS) + list(BREAKOUT_VARIANTS) + [CONTROL]
    events = {k: [] for k in names}
    trades = {k: [] for k in names}
    for k, code in enumerate(codes, 1):
        r = run_stock(con, code, args.since)
        if r:
            b, e, t = r
            for h in HORIZONS:
                base[h].extend(b[h])
            for name in names:
                events[name].extend(e[name])
                trades[name].extend(t[name])
        if k % 200 == 0:
            logger.info('%d / %d stocks', k, len(codes))

    out = {'baseline': {h: {'n': len(base[h]), 'mean': mean(base[h]), 'median': median(base[h]),
                            'win': sum(x > 0 for x in base[h]) / len(base[h]) * 100} for h in HORIZONS},
           'variants': {}}
    print(f"\nBaseline（全部股票×全部交易日）: " + '  '.join(
        f"{h}日 平均{out['baseline'][h]['mean']:+.2f}% 中位{out['baseline'][h]['median']:+.2f}% 勝率{out['baseline'][h]['win']:.1f}%"
        for h in HORIZONS))
    print(f"{'規則':<30}{'事件數':>8}" + ''.join(f"{f'{h}日平均':>10}{f'{h}日超額':>10}{f'{h}日勝率':>9}" for h in HORIZONS))
    for name, ev in events.items():
        row = {'n': len(ev)}
        line = f'{name:<30}{len(ev):>8}'
        for h in HORIZONS:
            vals = [e[h] for e in ev if e[h] is not None]
            if vals:
                m = mean(vals)
                row[h] = {'mean': m, 'median': median(vals), 'win': sum(x > 0 for x in vals) / len(vals) * 100,
                          'excess': m - out['baseline'][h]['mean']}
                line += f"{m:>+9.2f}%{row[h]['excess']:>+9.2f}%{row[h]['win']:>8.1f}%"
        out['variants'][name] = row
        print(line)
    print(f"\n停損{STOP:.0%}／移動停利（最高點回檔{TRAIL:.0%}）出場，不加碼：")
    print(f"{'規則':<30}{'交易數':>8}{'平均報酬':>10}{'中位數':>9}{'勝率':>8}{'平均持有日':>10}{'停損比例':>9}{'持有中':>7}")
    out['trailing'] = {}
    for name, tr in trades.items():
        if not tr:
            continue
        rets = [t['ret'] for t in tr]
        closed = [t for t in tr if not t['open']]
        row = {'n': len(tr), 'mean': mean(rets), 'median': median(rets),
               'win': sum(x > 0 for x in rets) / len(rets) * 100,
               'days': mean(t['days'] for t in tr),
               'stop_pct': sum(t['reason'] == 'stop' for t in closed) / len(closed) * 100 if closed else None,
               'open': sum(t['open'] for t in tr)}
        out['trailing'][name] = row
        print(f"{name:<30}{row['n']:>8}{row['mean']:>+9.2f}%{row['median']:>+8.2f}%{row['win']:>7.1f}%"
              f"{row['days']:>10.0f}{(row['stop_pct'] or 0):>8.1f}%{row['open']:>7}")
    suffix = '' if (STOP, TRAIL) == (0.10, 0.10) else f'_stop{STOP:g}_trail{TRAIL:g}'
    with open(f'backtest_accumulation_result{suffix}.json', 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)


if __name__ == '__main__':
    main()
