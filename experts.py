"""達人選股 scoring engine — encodes the 7 public stock-picking rulesets from
股泰 (gutai, bull + bear), 888/巔峰 (flag888, 4 rulesets), 股魚 (guyu), and
股海老牛 (laoniu) against this project's own DB (quarterly_financials,
financial_extra, institutional_trades, holding_concentration, dividend_policy,
dividend_fill_events, daily_prices) plus technical.py's indicator snapshot.

Known, deliberate simplifications (all discussed with the project owner):
  - Every "近N年平均" ratio (ROE/ROA/margins/turnover days/current & quick
    ratio) is approximated as the mean of the last N*4 quarterly ratios,
    not a strict calendar-year bucket average — avoids extra complexity
    around fiscal-year boundaries for a negligible accuracy cost.
  - 股泰's TU/TM/TD price bands and 週守/月守 support levels are proprietary,
    undocumented formulas. The 10 scoring lines built on them are collapsed
    into 3 clearly-labeled approximate checks (same total point value) using
    technical.py's `ema13_support` / `week_low_4` / `month_low_20`.
  - 888 標準2's 董監持股 scoring item has no public data source (FinMind
    doesn't carry it) — skipped entirely, not counted toward score or
    max_score.
  - "累計月營收年增率" (股泰) and "累積淨利年增率" (老牛) reuse the single
    latest period's YoY instead of a true cumulative-to-date YoY (no
    cumulative-revenue/profit column exists in this project's schema).

Each score_* function takes one stock's context dict (see _build_context)
and returns (passed, score, max_score, breakdown). compute_expert_scores()
runs all 8 rulesets over every tracked stock and upserts into expert_scores.
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from database import SessionLocal, Stock, ExpertScore
import technical

logger = logging.getLogger(__name__)
_TZ = ZoneInfo('Asia/Taipei')

EXPERT_LABELS = {
    'gutai_bull':  '股泰｜多方訊號',
    'gutai_bear':  '股泰｜空方訊號',
    'flag888_1':   '888｜標準1 營收轉機',
    'flag888_2':   '888｜標準2 高股息',
    'flag888_3':   '888｜標準3 低價價值',
    'flag888_4':   '888｜標準4 填息穩定',
    'guyu':        '股魚｜價值K線',
    'laoniu':      '股海老牛｜抱緊股',
}


# ── small math helpers ──────────────────────────────────────────────────────

def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _ratio_series(q_list, num_field, den_field, annualize=False):
    """One % ratio per quarter (num/den*100), None where either side missing."""
    out = []
    for row in q_list:
        num, den = row.get(num_field), row.get(den_field)
        if num is None or den is None or den == 0:
            out.append(None)
            continue
        r = num / den * (4 if annualize else 1)
        out.append(r * 100)
    return out


def _quick_ratio_series(q_list):
    out = []
    for row in q_list:
        ca, inv, cl = row.get('current_assets'), row.get('inventories'), row.get('current_liabilities')
        if ca is None or inv is None or not cl:
            out.append(None)
            continue
        out.append((ca - inv) / cl * 100)
    return out


def _turnover_days(q_list, balance_field, flow_field, period_days=91):
    """Avg-balance turnover days per quarter: needs this + prior quarter's
    balance, so yields one fewer point than len(q_list). Index-aligned with
    q_list (None where not computable) — some callers read index 0 as
    "latest quarter" (e.g. for a YoY comparison), which a skip-don't-append
    would silently shift onto a stale quarter."""
    out = []
    for i in range(len(q_list) - 1):
        cur, prev = q_list[i], q_list[i + 1]
        bal_cur, bal_prev = cur.get(balance_field), prev.get(balance_field)
        flow = cur.get(flow_field)
        if bal_cur is None or bal_prev is None or not flow:
            out.append(None)
            continue
        out.append((bal_cur + bal_prev) / 2 / abs(flow) * period_days)
    return out


def _yoy_diff(series, lag=4):
    """series[i] - series[i+lag], series indexed newest-first. Output stays
    index-aligned with the input (None where not computable) — callers read
    index 0 as "latest quarter's YoY"; silently dropping unavailable entries
    would shift that read onto a stale, unlabeled older quarter instead."""
    out = []
    for i in range(len(series)):
        if i + lag >= len(series) or series[i] is None or series[i + lag] is None:
            out.append(None)
            continue
        out.append(series[i] - series[i + lag])
    return out


class ScoreCard:
    """Accumulates a pass/fail selection verdict + a weighted score. Any
    check whose inputs are unavailable is excluded from score/max_score
    (skip, don't fail the whole ruleset over missing data) — except
    selection-standard `require()` items, which conservatively count a
    None as not-met (don't admit a stock we can't actually verify)."""

    def __init__(self):
        self.criteria = []
        self.score = 0
        self.max_score = 0
        self.breakdown = []

    def require(self, label, condition):
        met = bool(condition)
        self.criteria.append(met)
        self.breakdown.append({'type': 'select', 'label': label, 'met': met})

    def award(self, label, condition, points, approx=False):
        if condition is None:
            self.breakdown.append({'type': 'score', 'label': label, 'met': None,
                                    'points': points, 'approx': approx})
            return
        met = bool(condition)
        self.max_score += points
        if met:
            self.score += points
        self.breakdown.append({'type': 'score', 'label': label, 'met': met,
                                'points': points, 'approx': approx})

    def award_count(self, label, achieved, max_occurrences, unit_points=1, approx=False):
        """Graduated scoring (e.g. '每次符合 +N 分，最高 M 次'): `achieved`
        occurrences, each worth `unit_points`, capped at `max_occurrences`.
        max_score is always the fixed max_occurrences*unit_points ceiling,
        not however many the stock happened to hit — unlike award(), where
        the caller supplies one fixed point value for a single yes/no check."""
        ceiling = max_occurrences * unit_points
        if achieved is None:
            self.breakdown.append({'type': 'score', 'label': label, 'met': None,
                                    'points': ceiling, 'approx': approx})
            return
        gained = max(0, min(achieved, max_occurrences)) * unit_points
        self.max_score += ceiling
        self.score += gained
        self.breakdown.append({'type': 'score', 'label': f'{label}（{achieved}次）', 'met': gained > 0,
                                'points': ceiling, 'gained': gained, 'approx': approx})

    def result(self):
        passed = bool(self.criteria) and all(self.criteria)
        return passed, self.score, self.max_score, self.breakdown


# ── context builder ──────────────────────────────────────────────────────────

def _build_context(db):
    """Bulk-fetch every table once and assemble a per-stock context dict.
    Returns {stock_code: ctx}."""
    today = datetime.now(_TZ).date()

    ctx = {
        code: {
            'code': code, 'name': name, 'market': market, 'industry': industry,
            'close': None, 'volume': None, 'avg_vol_10d': None,
            'per': None, 'pbr': None, 'pbr_avg_hist': None, 'dividend_yield': None,
            'revenue_yoy': None, 'rev_yoy_recent': [], 'rev3m_avg': None, 'rev12m_avg': None,
            'q': [], 'inst': [], 'hold': [], 'div_by_year': {}, 'div_fill_events': [],
            'tech': {},
        }
        for code, name, market, industry in
        db.query(Stock.code, Stock.name, Stock.market, Stock.industry).all()
    }

    for r in db.execute(text('''
        SELECT dp.stock_code, dp.close, dp.volume, dp.per, dp.pbr, dp.dividend_yield
        FROM daily_prices dp
        INNER JOIN (SELECT stock_code, MAX(date) AS max_date FROM daily_prices GROUP BY stock_code) lt
          ON dp.stock_code = lt.stock_code AND dp.date = lt.max_date
    ''')).mappings():
        c = ctx.get(r['stock_code'])
        if c:
            c['close'], c['volume'] = r['close'], r['volume']
            c['per'], c['pbr'], c['dividend_yield'] = r['per'], r['pbr'], r['dividend_yield']

    since_vol = (today - timedelta(days=20)).isoformat()
    for r in db.execute(text('''
        SELECT stock_code, AVG(volume) AS v FROM (
            SELECT stock_code, volume,
                   ROW_NUMBER() OVER (PARTITION BY stock_code ORDER BY date DESC) rn
            FROM daily_prices WHERE date >= :since
        ) WHERE rn <= 10 GROUP BY stock_code
    '''), {'since': since_vol}).mappings():
        c = ctx.get(r['stock_code'])
        if c:
            c['avg_vol_10d'] = r['v']

    for r in db.execute(text('''
        SELECT stock_code, AVG(pbr) AS v FROM daily_prices WHERE pbr IS NOT NULL GROUP BY stock_code
    ''')).mappings():
        c = ctx.get(r['stock_code'])
        if c:
            c['pbr_avg_hist'] = r['v']

    rev_rows = {}
    for r in db.execute(text('''
        SELECT stock_code, year, month, revenue, revenue_yoy FROM monthly_revenue
        ORDER BY stock_code, year DESC, month DESC
    ''')).mappings():
        rev_rows.setdefault(r['stock_code'], []).append(r)
    for code, rows in rev_rows.items():
        c = ctx.get(code)
        if not c:
            continue
        c['revenue_yoy'] = rows[0]['revenue_yoy']
        c['rev_yoy_recent'] = [x['revenue_yoy'] for x in rows[:2]]
        rev3 = [x['revenue'] for x in rows[:3] if x['revenue'] is not None]
        rev12 = [x['revenue'] for x in rows[:12] if x['revenue'] is not None]
        c['rev3m_avg'] = sum(rev3) / len(rev3) if rev3 else None
        c['rev12m_avg'] = sum(rev12) / len(rev12) if rev12 else None

    qf = {}
    for r in db.execute(text('''
        SELECT stock_code, year, quarter, revenue, operating_income, net_income, eps
        FROM quarterly_financials
    ''')).mappings():
        qf.setdefault(r['stock_code'], {})[(r['year'], r['quarter'])] = dict(r)
    fe = {}
    for r in db.execute(text('''
        SELECT stock_code, year, quarter, inventories, accounts_receivable, current_assets,
               current_liabilities, liabilities, equity, total_assets, long_term_borrowings,
               capital_stock, gross_profit, cost_of_goods_sold, pretax_income,
               operating_cash_flow, interest_expense, capex
        FROM financial_extra
    ''')).mappings():
        fe.setdefault(r['stock_code'], {})[(r['year'], r['quarter'])] = dict(r)

    for code, c in ctx.items():
        keys = sorted(set(qf.get(code, {})) | set(fe.get(code, {})), reverse=True)[:40]
        merged = []
        for key in keys:
            row = {'year': key[0], 'quarter': key[1]}
            row.update(qf.get(code, {}).get(key, {}))
            row.update({k: v for k, v in fe.get(code, {}).get(key, {}).items()
                        if k not in ('stock_code', 'year', 'quarter')})
            merged.append(row)
        c['q'] = merged

    since_inst = (today - timedelta(days=12)).isoformat()
    for r in db.execute(text('''
        SELECT stock_code, date, foreign_buy, foreign_sell, trust_buy, trust_sell,
               dealer_buy, dealer_sell FROM institutional_trades
        WHERE date >= :since ORDER BY stock_code, date DESC
    '''), {'since': since_inst}).mappings():
        c = ctx.get(r['stock_code'])
        if c and len(c['inst']) < 5:
            c['inst'].append({
                'foreign_net': (r['foreign_buy'] or 0) - (r['foreign_sell'] or 0),
                'trust_net': (r['trust_buy'] or 0) - (r['trust_sell'] or 0),
                'dealer_net': (r['dealer_buy'] or 0) - (r['dealer_sell'] or 0),
            })

    since_hold = (today - timedelta(days=45)).isoformat()
    for r in db.execute(text('''
        SELECT stock_code, date, pct_1000up, pct_800up, pct_600up, pct_400up,
               pct_200down, pct_100down FROM holding_concentration
        WHERE date >= :since ORDER BY stock_code, date DESC
    '''), {'since': since_hold}).mappings():
        c = ctx.get(r['stock_code'])
        if c and len(c['hold']) < 3:
            c['hold'].append({
                'big_pct': _mean([r['pct_1000up'], r['pct_800up'], r['pct_600up'], r['pct_400up']]),
                'small_pct': _mean([r['pct_200down'], r['pct_100down']]),
            })

    for r in db.execute(text('''
        SELECT stock_code, fiscal_year, SUM(cash_dividend) AS cash, SUM(stock_dividend) AS stock
        FROM dividend_policy GROUP BY stock_code, fiscal_year
    ''')).mappings():
        c = ctx.get(r['stock_code'])
        if c:
            c['div_by_year'][r['fiscal_year']] = {'cash': r['cash'] or 0.0, 'stock': r['stock'] or 0.0}

    since_div = (today - timedelta(days=5 * 365)).isoformat()
    for r in db.execute(text('''
        SELECT stock_code, ex_date, filled FROM dividend_fill_events WHERE ex_date >= :since
    '''), {'since': since_div}).mappings():
        c = ctx.get(r['stock_code'])
        if c:
            c['div_fill_events'].append({'filled': r['filled']})

    since_tech = (today - timedelta(days=760)).isoformat()
    price_hist = {}
    for r in db.execute(text('''
        SELECT stock_code, date, open, high, low, close FROM daily_prices
        WHERE date >= :since ORDER BY stock_code, date ASC
    '''), {'since': since_tech}).mappings():
        d = r['date']
        if isinstance(d, str):
            d = datetime.strptime(d, '%Y-%m-%d').date()
        price_hist.setdefault(r['stock_code'], []).append({
            'date': d, 'open': r['open'], 'high': r['high'], 'low': r['low'], 'close': r['close'],
        })
    for code, rows in price_hist.items():
        c = ctx.get(code)
        if c and len(rows) >= 30:
            try:
                c['tech'] = technical.snapshot(rows)
            except Exception:
                logger.exception('technical.snapshot failed for %s', code)

    return ctx


# ── 股泰 ─────────────────────────────────────────────────────────────────────

def _gutai(ctx, bull=True):
    q, t, inst, hold = ctx['q'], ctx['tech'], ctx['inst'], ctx['hold']
    s = ScoreCard()
    sign = 1 if bull else -1

    avg_eps = _mean([r.get('eps') for r in q[:8]])
    inv_days = _mean(_turnover_days(q[:9], 'inventories', 'cost_of_goods_sold'))
    ar_days = _mean(_turnover_days(q[:9], 'accounts_receivable', 'revenue'))
    op_margin = _mean(_ratio_series(q[:8], 'operating_income', 'revenue'))
    roe = _mean(_ratio_series(q[:8], 'net_income', 'equity', annualize=True))
    roa = _mean(_ratio_series(q[:8], 'net_income', 'total_assets', annualize=True))
    gross_margin = _mean(_ratio_series(q[:8], 'gross_profit', 'revenue'))
    current_ratio = _mean(_ratio_series(q[:8], 'current_assets', 'current_liabilities'))
    quick_ratio = _mean(_quick_ratio_series(q[:8]))
    debt_ratio = _mean(_ratio_series(q[:8], 'liabilities', 'total_assets'))

    foreign_pos_days = sum(1 for r in inst[:5] if r['foreign_net'] * sign > 0) if inst else None
    trust_pos_days = sum(1 for r in inst[:5] if r['trust_net'] * sign > 0) if inst else None
    big_up = hold[0]['big_pct'] * sign > hold[1]['big_pct'] * sign if len(hold) >= 2 and None not in (hold[0]['big_pct'], hold[1]['big_pct']) else None
    small_down = hold[0]['small_pct'] * sign < hold[1]['small_pct'] * sign if len(hold) >= 2 and None not in (hold[0]['small_pct'], hold[1]['small_pct']) else None

    if bull:
        s.require('近10天平均成交量>500張', ctx['avg_vol_10d'] and ctx['avg_vol_10d'] > 500_000)
        s.require('連續2年平均EPS>0', avg_eps is not None and avg_eps > 0)
        s.require('近2年平均存貨週轉天數<150天', inv_days is not None and inv_days < 150)
        s.require('近2年平均應收帳款周轉天數<150天', ar_days is not None and ar_days < 150)
        s.require('近2年平均營業利益率>0', op_margin is not None and op_margin > 0)
        s.require('近2年平均ROE>3%', roe is not None and roe > 3)
        s.require('近2年平均ROA>3%', roa is not None and roa > 3)
        s.require('近2年平均毛利率>3%', gross_margin is not None and gross_margin > 3)
        s.require('近2年平均流動比率>110%', current_ratio is not None and current_ratio > 110)
        s.require('近2年平均速動比率>50%', quick_ratio is not None and quick_ratio > 50)
        s.require('近5日至少2日外資買超>0', foreign_pos_days is not None and foreign_pos_days >= 2)
        s.require('近5日至少2日投信買超>0', trust_pos_days is not None and trust_pos_days >= 2)
        s.require('大戶持股比例本周>上周', big_up)
        s.require('散戶持股比例本周<上周', small_down)
    else:
        s.require('近10天平均成交量>500張', ctx['avg_vol_10d'] and ctx['avg_vol_10d'] > 500_000)
        s.require('近5日至少2日外資賣超', foreign_pos_days is not None and foreign_pos_days >= 2)
        s.require('近5日至少2日投信賣超', trust_pos_days is not None and trust_pos_days >= 2)
        s.require('大戶持股比例本周<上周', big_up)
        s.require('散戶持股比例本周>上周', small_down)

    k_w_ge_d = (t.get('k_weekly') is not None and t.get('d_weekly') is not None
                and (t['k_weekly'] >= t['d_weekly']) == bull)
    k_m_ge_d = (t.get('k_monthly') is not None and t.get('d_monthly') is not None
                and (t['k_monthly'] >= t['d_monthly']) == bull)
    macd_hist, macd_prev = t.get('macd_hist'), t.get('macd_hist_prev')
    macd_daily_trend = (macd_hist is not None and macd_prev is not None
                         and (macd_hist > macd_prev and macd_hist * sign > 0))
    macd_daily_sign = macd_hist * sign > 0 if macd_hist is not None else None
    macd_weekly_sign = t['macd_hist_weekly'] * sign > 0 if t.get('macd_hist_weekly') is not None else None
    rsi_d_ok = (t['rsi_daily'] >= 50) == bull if t.get('rsi_daily') is not None else None
    rsi_w_ok = (t['rsi_weekly'] >= 50) == bull if t.get('rsi_weekly') is not None else None
    ema_align_ok = (t.get('ema_alignment') == ('bull' if bull else 'bear'))
    support_ok = t.get('ema13_support') if bull else (not t['ema13_support'] if t.get('ema13_support') is not None else None)
    week_break = (ctx['close'] > t['week_low_4']) == bull if (ctx['close'] and t.get('week_low_4') is not None) else None
    month_break = (ctx['close'] > t['month_low_20']) == bull if (ctx['close'] and t.get('month_low_20') is not None) else None

    s.award('週K/週D同向', k_w_ge_d, 3)
    s.award('月K/月D同向', k_m_ge_d if t.get('k_monthly') is not None else None, 3)
    s.award('日MACD柱狀同向增強', macd_daily_trend, 3)
    s.award('日MACD柱狀方向一致', macd_daily_sign, 3)
    s.award('週MACD柱狀方向一致', macd_weekly_sign, 3)
    s.award('日RSI方向一致(50)', rsi_d_ok, 3)
    s.award('週RSI方向一致(50)', rsi_w_ok, 3)
    s.award('近似：站穩/跌破EMA13（原TU/TM/TD相關6項）', support_ok, 18, approx=True)
    s.award('近似：站上/跌破近4週低點（原週守/上週守）', week_break, 6, approx=True)
    s.award('近似：站上/跌破近20日低點（原月守/上月守）', month_break, 6, approx=True)
    s.award('EMA(3,5,8,13)排列方向一致', ema_align_ok if t.get('ema_alignment') else None, 3)
    s.award('近似：突破/跌破均線糾結', ema_align_ok if t.get('ema_alignment') else None, 3, approx=True)
    s.award('今日外資買超同向', (inst[0]['foreign_net'] * sign > 0) if inst else None, 3)
    s.award('今日投信買超同向', (inst[0]['trust_net'] * sign > 0) if inst else None, 3)
    s.award('今日自營商買超同向', (inst[0]['dealer_net'] * sign > 0) if inst else None, 3)
    hold_prev_up = (hold[1]['big_pct'] * sign > hold[2]['big_pct'] * sign
                    if len(hold) >= 3 and None not in (hold[1]['big_pct'], hold[2]['big_pct']) else None)
    hold_prev_down = (hold[1]['small_pct'] * sign < hold[2]['small_pct'] * sign
                       if len(hold) >= 3 and None not in (hold[1]['small_pct'], hold[2]['small_pct']) else None)
    s.award('大戶持股上周同向(前週)', hold_prev_up, 3)
    s.award('散戶持股上周同向(前週)', hold_prev_down, 3)
    eps_2q = (q[0].get('eps') is not None and q[1].get('eps') is not None
              and (q[0]['eps'] * sign > 0) and (q[1]['eps'] * sign > 0)) if len(q) >= 2 else None
    s.award('連續2季EPS方向一致', eps_2q, 7)
    s.award('近2年平均負債比率方向一致(80%)', (debt_ratio < 80) == bull if debt_ratio is not None else None, 7)
    yoy2 = ctx['rev_yoy_recent']
    rev_2m = (len(yoy2) >= 2 and yoy2[0] is not None and yoy2[1] is not None
              and (yoy2[0] * sign > 0) and (yoy2[1] * sign > 0))
    s.award('連續2月營收年增率方向一致', rev_2m if yoy2 else None, 7)
    s.award('近似：連續2月累計營收年增率方向一致（用單月年增代替）', rev_2m if yoy2 else None, 7, approx=True)

    return s.result()


def score_gutai_bull(ctx):
    return _gutai(ctx, bull=True)


def score_gutai_bear(ctx):
    return _gutai(ctx, bull=False)


# ── 888 / 巔峰 ───────────────────────────────────────────────────────────────

def score_flag888_1(ctx):
    q = ctx['q']
    s = ScoreCard()
    ocf3 = [r.get('operating_cash_flow') for r in q[:3]]
    s.require('近3季營業現金流入>0', len(q) >= 3 and all(v is not None and v > 0 for v in ocf3))
    pbr_ok = (ctx['pbr'] is not None and ctx['pbr_avg_hist'] is not None and ctx['pbr'] < ctx['pbr_avg_hist'])
    s.require('淨值比<歷史平均(近似10年平均)', pbr_ok)

    gm = _ratio_series(q[:8], 'gross_profit', 'revenue')
    roe = _ratio_series(q[:8], 'net_income', 'equity', annualize=True)
    inv_days = _turnover_days(q[:9], 'inventories', 'cost_of_goods_sold')

    s.award('月營收年增率>0', ctx['revenue_yoy'] > 0 if ctx['revenue_yoy'] is not None else None, 10)
    gm_yoy = _yoy_diff(gm)
    s.award('毛利率年增率>0', gm_yoy[0] > 0 if gm_yoy and gm_yoy[0] is not None else None, 30)
    inv_yoy = _yoy_diff([-d if d is not None else None for d in inv_days])  # turnover rate ~ -days
    s.award('存貨週轉率年增率>0', inv_yoy[0] > 0 if inv_yoy and inv_yoy[0] is not None else None, 30)
    roe_yoy = _yoy_diff(roe)
    s.award('ROE年增率>0', roe_yoy[0] > 0 if roe_yoy and roe_yoy[0] is not None else None, 20)
    inst_today = ctx['inst'][0] if ctx['inst'] else None
    s.award('法人買超>0', (inst_today['foreign_net'] + inst_today['trust_net'] + inst_today['dealer_net'] > 0)
            if inst_today else None, 10)
    return s.result()


def score_flag888_2(ctx):
    q = ctx['q']
    s = ScoreCard()
    years = sorted(ctx['div_by_year'], reverse=True)[:10]
    div_totals = [ctx['div_by_year'][y]['cash'] + ctx['div_by_year'][y]['stock'] for y in years]
    s.require('近10年股息>0', bool(years) and all(v > 0 for v in div_totals))

    latest_year = years[0] if years else None
    annual_eps = None
    if latest_year:
        annual_eps = _annual_eps_sum(q, latest_year)
    div_this_year = div_totals[0] if div_totals else None
    payout = (div_this_year / annual_eps * 100) if (div_this_year is not None and annual_eps) else None
    debt_ratio = _mean(_ratio_series(q[:8], 'liabilities', 'total_assets'))
    roe_5y = _mean(_ratio_series(q[:20], 'net_income', 'equity', annualize=True))
    roe = _ratio_series(q[:8], 'net_income', 'equity', annualize=True)
    roe_yoy = _yoy_diff(roe)

    s.award('股息配發率>70%', payout > 70 if payout is not None else None, 20)
    s.award('董監持股>12%（無公開資料來源，未列入評分）', None, 20)
    s.award('負債比<50%', debt_ratio < 50 if debt_ratio is not None else None, 20)
    s.award('近5年平均ROE>15%', roe_5y > 15 if roe_5y is not None else None, 10)
    s.award('近4季ROE年增率>0', roe_yoy[0] > 0 if roe_yoy and roe_yoy[0] is not None else None, 10)
    s.award('本益比<=15倍', ctx['per'] <= 15 if ctx['per'] is not None else None, 10)
    s.award('現金殖利率>6.67%', ctx['dividend_yield'] > 6.67 if ctx['dividend_yield'] is not None else None, 10)
    return s.result()


def score_flag888_3(ctx):
    q = ctx['q']
    s = ScoreCard()
    s.require('殖利率>0%', ctx['dividend_yield'] is not None and ctx['dividend_yield'] > 0)
    s.require('股價<=20', ctx['close'] is not None and ctx['close'] <= 20)

    current_ratio = _ratio_series(q[:8], 'current_assets', 'current_liabilities')
    gm = _ratio_series(q[:8], 'gross_profit', 'revenue')
    asset_turnover = _ratio_series(q[:8], 'revenue', 'total_assets')
    roa = _ratio_series(q[:8], 'net_income', 'total_assets', annualize=True)
    cap_stock_yoy = _yoy_diff([r.get('capital_stock') for r in q])

    inst_today = ctx['inst'][0] if ctx['inst'] else None
    ocf0 = q[0].get('operating_cash_flow') if q else None
    ni0 = q[0].get('net_income') if q else None
    ltb0 = q[0].get('long_term_borrowings') if q else None

    roa_yoy = _yoy_diff(roa)
    current_ratio_yoy = _yoy_diff(current_ratio)
    gm_yoy = _yoy_diff(gm)
    asset_turnover_yoy = _yoy_diff(asset_turnover)

    s.award('季EPS>0', q[0]['eps'] > 0 if q and q[0].get('eps') is not None else None, 10)
    s.award('ROA年增率>0', roa_yoy[0] > 0 if roa_yoy and roa_yoy[0] is not None else None, 10)
    s.award('季營業現金流入>0', ocf0 > 0 if ocf0 is not None else None, 10)
    s.award('營業現金流>淨利', (ocf0 > ni0) if (ocf0 is not None and ni0 is not None) else None, 10)
    s.award('長期借款<=0', ltb0 <= 0 if ltb0 is not None else None, 10)
    s.award('流動比年增率>0', current_ratio_yoy[0] > 0 if current_ratio_yoy and current_ratio_yoy[0] is not None else None, 10)
    s.award('股本年增率<=0', cap_stock_yoy[0] <= 0 if cap_stock_yoy and cap_stock_yoy[0] is not None else None, 10)
    s.award('毛利率年增率>0', gm_yoy[0] > 0 if gm_yoy and gm_yoy[0] is not None else None, 10)
    s.award('資產週轉率年增率>0', asset_turnover_yoy[0] > 0 if asset_turnover_yoy and asset_turnover_yoy[0] is not None else None, 10)
    s.award('法人買超>0', (inst_today['foreign_net'] + inst_today['trust_net'] + inst_today['dealer_net'] > 0)
            if inst_today else None, 10)
    return s.result()


def score_flag888_4(ctx):
    q = ctx['q']
    s = ScoreCard()
    years = sorted(ctx['div_by_year'], reverse=True)[:10]
    div_totals = [ctx['div_by_year'][y]['cash'] + ctx['div_by_year'][y]['stock'] for y in years]
    s.require('近10年股息>0', bool(years) and all(v > 0 for v in div_totals))

    # ctx['div_fill_events'] is already limited to the last 5 years (see _build_context)
    known = [e['filled'] for e in ctx['div_fill_events'] if e['filled'] is not None]
    fill_rate = (sum(known) / len(known) * 100) if known else None

    gm = _ratio_series(q[:8], 'gross_profit', 'revenue')
    op_margin = _ratio_series(q[:8], 'operating_income', 'revenue')
    op_income_ttm_yoy = None
    ttm = _mean([r.get('operating_income') for r in q[:4]])
    ttm_prev = _mean([r.get('operating_income') for r in q[4:8]])
    if ttm is not None and ttm_prev is not None:
        op_income_ttm_yoy = ttm - ttm_prev
    div_yoy = (div_totals[0] - div_totals[1]) if len(div_totals) >= 2 else None

    gm_yoy = _yoy_diff(gm)
    op_margin_yoy = _yoy_diff(op_margin)
    s.award('近5年填息機率>80%', fill_rate > 80 if fill_rate is not None else None, 70)
    s.award('毛利率年增率>0', gm_yoy[0] > 0 if gm_yoy and gm_yoy[0] is not None else None, 5)
    s.award('營業利益率年增率>0', op_margin_yoy[0] > 0 if op_margin_yoy and op_margin_yoy[0] is not None else None, 5)
    s.award('近3月平均營收>近12月平均', (ctx['rev3m_avg'] > ctx['rev12m_avg'])
            if (ctx['rev3m_avg'] is not None and ctx['rev12m_avg'] is not None) else None, 5)
    s.award('近4季營業利益合計年增率>0', op_income_ttm_yoy > 0 if op_income_ttm_yoy is not None else None, 5)
    s.award('股息年增率>0', div_yoy > 0 if div_yoy is not None else None, 10)
    return s.result()


def _annual_eps_sum(q, year):
    """Sum of a stock's quarterly EPS for one calendar year — an approximation
    of annual EPS (undercounts if a quarter hasn't been disclosed yet)."""
    vals = [r.get('eps') for r in q if r.get('year') == year and r.get('eps') is not None]
    return sum(vals) if vals else None


# ── 股魚 ─────────────────────────────────────────────────────────────────────

def score_guyu(ctx):
    q = ctx['q']
    s = ScoreCard()
    op_yoy_pct = []
    for i in range(len(q)):
        if i + 4 < len(q) and q[i].get('operating_income') is not None and q[i + 4].get('operating_income'):
            op_yoy_pct.append((q[i]['operating_income'] - q[i + 4]['operating_income']) / abs(q[i + 4]['operating_income']) * 100)
        else:
            op_yoy_pct.append(None)
    roe = _ratio_series(q[:8], 'net_income', 'equity', annualize=True)
    ocf4 = [r.get('operating_cash_flow') for r in q[:4]]

    s.require('近4季現金流>0', len(q) >= 4 and all(v is not None and v > 0 for v in ocf4))
    s.require('ROE>=8%', roe[0] >= 8 if roe and roe[0] is not None else False)
    op_yoy_2y = [v for v in op_yoy_pct[:8] if v is not None]
    s.require('近2年平均營業利益年增率<=100', bool(op_yoy_2y) and _mean(op_yoy_2y) <= 100)

    op_margin4 = _ratio_series(q[:4], 'operating_income', 'revenue')
    interest_cov = []
    for r in q[:4]:
        oi, ie = r.get('operating_income'), r.get('interest_expense')
        interest_cov.append(oi / abs(ie) if (oi is not None and ie) else None)
    main_biz_ratio = []
    for r in q[:4]:
        oi, pretax = r.get('operating_income'), r.get('pretax_income')
        main_biz_ratio.append(oi / pretax * 100 if (oi is not None and pretax) else None)

    yoy0 = op_yoy_pct[0] if op_yoy_pct else None
    s.award('近4季營業利益比去年大', yoy0 > 0 if yoy0 is not None else None, 10)
    s.award('近4季營業利益比去年大10%', yoy0 > 10 if yoy0 is not None else None, 2)
    s.award('近4季營業利益比去年大20%', yoy0 > 20 if yoy0 is not None else None, 3)
    s.award('近4季營業利益率皆>0', all(v > 0 for v in op_margin4) if all(v is not None for v in op_margin4) and op_margin4 else None, 8)
    s.award('ROE>8%', roe[0] > 8 if roe and roe[0] is not None else None, 20)
    s.award('ROE>15%', roe[0] > 15 if roe and roe[0] is not None else None, 4)
    s.award('ROE>20%', roe[0] > 20 if roe and roe[0] is not None else None, 8)
    s.award('近4季營業現金流>0', all(v is not None and v > 0 for v in ocf4) if ocf4 else None, 10)
    ic0 = interest_cov[0] if interest_cov else None
    s.award('利息保障倍數>40', ic0 > 40 if ic0 is not None else None, 6)
    s.award('利息保障倍數>80', ic0 > 80 if ic0 is not None else None, 3)
    s.award('利息保障倍數>100', ic0 > 100 if ic0 is not None else None, 3)
    mb0 = main_biz_ratio[0] if main_biz_ratio else None
    s.award('本業收入比介於80~120', (80 <= mb0 <= 120) if mb0 is not None else None, 15)
    s.award('本業收入比介於90~120', (90 <= mb0 <= 120) if mb0 is not None else None, 3)
    years = sorted(ctx['div_by_year'], reverse=True)[:1]
    latest_eps_annual = _annual_eps_sum(q, years[0]) if years else None
    div0 = (ctx['div_by_year'][years[0]]['cash'] + ctx['div_by_year'][years[0]]['stock']) if years else None
    payout0 = (div0 / latest_eps_annual * 100) if (div0 is not None and latest_eps_annual) else None
    s.award('股息發放率>=50%', payout0 >= 50 if payout0 is not None else None, 5)
    return s.result()


# ── 股海老牛 ─────────────────────────────────────────────────────────────────

def score_laoniu(ctx):
    q = ctx['q']
    s = ScoreCard()
    years = sorted(ctx['div_by_year'], reverse=True)
    div5 = years[:5]
    div_totals_5 = [ctx['div_by_year'][y]['cash'] + ctx['div_by_year'][y]['stock'] for y in div5]
    eps3 = [r.get('eps') for r in q[:12] if r.get('eps') is not None][:12]

    s.require('成交量>100張', ctx['volume'] is not None and ctx['volume'] > 100_000)
    s.require('近5年股利>0', len(div5) >= 1 and all(v > 0 for v in div_totals_5))
    s.require('近3年EPS>0', bool(eps3) and all(v > 0 for v in eps3))
    s.require('營收年增率>0', ctx['revenue_yoy'] is not None and ctx['revenue_yoy'] > 0)

    ni_yoy = _yoy_diff([r.get('net_income') for r in q])
    gm = _ratio_series(q[:8], 'gross_profit', 'revenue')
    op_margin = _ratio_series(q[:8], 'operating_income', 'revenue')
    debt_ratio = _mean(_ratio_series(q[:8], 'liabilities', 'total_assets'))
    ocf7 = [r.get('operating_cash_flow') for r in q[:28]]
    capex7 = [r.get('capex') for r in q[:28]]
    fcf7 = [(o + c) if (o is not None and c is not None) else None for o, c in zip(ocf7, capex7)]

    ni_yoy_ok = ni_yoy[0] > 0 if ni_yoy and ni_yoy[0] is not None else None
    s.award('淨利年增率>0', ni_yoy_ok, 6)
    s.award('近似：累積淨利年增率>0（用單季年增代替）', ni_yoy_ok, 6, approx=True)
    oi0, pretax0 = (q[0].get('operating_income'), q[0].get('pretax_income')) if q else (None, None)
    non_op = (pretax0 - oi0) if (oi0 is not None and pretax0 is not None) else None
    s.award('近4季營業利益>業外損益', (oi0 > non_op) if (oi0 is not None and non_op is not None) else None, 3)
    gm_yoy = _yoy_diff(gm)
    op_margin_yoy = _yoy_diff(op_margin)
    s.award('季毛利率年增率>0', gm_yoy[0] > 0 if gm_yoy and gm_yoy[0] is not None else None, 4)
    s.award('季營益率年增率>0', op_margin_yoy[0] > 0 if op_margin_yoy and op_margin_yoy[0] is not None else None, 10)
    ocf_pos = sum(1 for v in ocf7 if v is not None and v > 0)
    s.award_count('近似：營業現金流入次數（用近7季代替近7年）', ocf_pos if ocf7 else None, 7, approx=True)
    fcf_pos = sum(1 for v in fcf7 if v is not None and v > 0)
    s.award_count('近似：自由現金流入次數（用近7季代替近7年）', fcf_pos if fcf7 else None, 7, approx=True)
    s.award('負債比率<50%', debt_ratio < 50 if debt_ratio is not None else None, 3)

    eps4 = [r.get('eps') for r in q[:4]]
    ttm_eps_sum = sum(v for v in eps4 if v is not None) if any(v is not None for v in eps4) else None
    payout2 = []
    for i, y in enumerate(div5[:2]):
        annual_eps = _annual_eps_sum(q, y)
        payout2.append(div_totals_5[i] / annual_eps * 100 if annual_eps else None)
    payout2_valid = [p for p in payout2 if p is not None]
    avg_payout2 = _mean(payout2_valid) if payout2_valid else None
    yield_metric = (ttm_eps_sum * (avg_payout2 / 100) / ctx['close'] * 100
                     if (ttm_eps_sum is not None and avg_payout2 is not None and ctx['close']) else None)
    s.award('近4季EPS×近2年股息配發率/股價>5%', yield_metric > 5 if yield_metric is not None else None, 20)
    s.award('近2年平均股息配發率介於70~100%', (70 <= avg_payout2 <= 100) if avg_payout2 is not None else None, 10)
    div_ge_last = (div_totals_5[0] >= div_totals_5[1]) if len(div_totals_5) >= 2 else None
    s.award('股息>=去年', div_ge_last, 5)

    inst = ctx['inst']
    foreign_pos = sum(1 for r in inst if r['foreign_net'] >= 0) if inst else None
    trust_pos = sum(1 for r in inst if r['trust_net'] >= 0) if inst else None
    s.award_count('近5日外資買超次數', foreign_pos if inst else None, 5, unit_points=2)
    s.award_count('近5日投信買超次數', trust_pos if inst else None, 5, unit_points=1)
    return s.result()


SCORERS = {
    'gutai_bull': score_gutai_bull,
    'gutai_bear': score_gutai_bear,
    'flag888_1': score_flag888_1,
    'flag888_2': score_flag888_2,
    'flag888_3': score_flag888_3,
    'flag888_4': score_flag888_4,
    'guyu': score_guyu,
    'laoniu': score_laoniu,
}


def compute_expert_scores():
    """Runs all 8 rulesets over every tracked stock and overwrites
    expert_scores (one row per stock per ruleset, latest snapshot only —
    same "overwrite, no history" pattern as stock_ai_analysis)."""
    import json as _json
    db = SessionLocal()
    try:
        ctx_map = _build_context(db)
        records = []
        for code, ctx in ctx_map.items():
            for key, fn in SCORERS.items():
                try:
                    passed, score, max_score, breakdown = fn(ctx)
                except Exception:
                    logger.exception('Scoring %s failed for %s', key, code)
                    continue
                records.append({
                    'stock_code': code, 'expert_key': key, 'expert_label': EXPERT_LABELS[key],
                    'passed': int(passed), 'score': score, 'max_score': max_score,
                    'breakdown_json': _json.dumps(breakdown, ensure_ascii=False),
                    'computed_at': datetime.now(_TZ),
                })
        if records:
            db.execute(ExpertScore.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        logger.info('compute_expert_scores: %d rows written', len(records))
        return len(records)
    except Exception:
        db.rollback()
        logger.exception('compute_expert_scores failed')
        raise
    finally:
        db.close()
