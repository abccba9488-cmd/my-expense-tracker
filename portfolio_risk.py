"""投資組合壓力測試 — 本站自製、實驗性功能（NEW）。

跟 experts.py 的 9 套「找買點」規則、compute_holding_health() 的「單股該不
該出場」用途都不同：這個是對使用者一份自選股清單「整包」做風險分析。

**沒有持股權重資料**（watchlist_stocks 只存代號，不存股數/金額），一律假設
等權重計算——這是明確標註的簡化，不是真正反映使用者實際部位大小的風險
模型，只能看出「持股組合本身」在各面向的風險輪廓。

四個分析面向：
1. **歷史情境回放**：不是假設性的總體經濟因子模型（本站沒有 beta/因子曝險
   資料可以做那種模型），而是直接回放台股史上幾段真實的系統性下跌期間，
   用清單裡每一檔股票「當時真實的股價走勢」算出報酬率/最大回檔，等權重
   平均成「這份清單如果在那段期間持有會發生什麼事」。清單中上市時間晚於
   某個情境起始日的股票，該情境會自動跳過該股（記錄涵蓋家數，不是硬湊）。
2. **產業集中度 HHI**：依 `stocks.industry` 分類、等權重計算 Herfindahl-
   Hirschman 指數（0–10000 標準尺度，>2500 高度集中／1500–2500 中度／<1500
   分散）。
3. **相關性**：近一年（400 個日曆天）逐日報酬率的兩兩 Pearson 相關係數，
   回傳平均值與相關性最高的一對，太少共同交易日（<30 天）的配對跳過不計。
4. **歷史模擬法 VaR（95%/99%）**：把每日「清單等權重平均報酬率」由小到大
   排序取分位數，不是常態分布假設的參數法 VaR。
"""
from datetime import date, timedelta

from sqlalchemy import text

STRESS_SCENARIOS = [
    {'key': 'gfc2011',      'label': '2011 歐債危機',      'start': '2011-07-01', 'end': '2011-10-04'},
    {'key': 'china2015',    'label': '2015 中國股災',      'start': '2015-06-01', 'end': '2015-08-25'},
    {'key': 'tradewar2018', 'label': '2018 中美貿易戰',    'start': '2018-06-01', 'end': '2018-12-25'},
    {'key': 'covid2020',    'label': '2020 COVID崩盤',     'start': '2020-01-20', 'end': '2020-03-19'},
    {'key': 'hike2022',     'label': '2022 全球升息熊市',  'start': '2022-01-05', 'end': '2022-10-25'},
]


def _scenario_impact(db, code, start, end):
    rows = db.execute(text('''
        SELECT close FROM daily_prices
        WHERE stock_code = :code AND date BETWEEN :start AND :end
        ORDER BY date ASC
    '''), {'code': code, 'start': start, 'end': end}).mappings().all()
    closes = [r['close'] for r in rows if r['close'] is not None]
    if len(closes) < 5:
        return None
    start_price, end_price, min_price = closes[0], closes[-1], min(closes)
    if not start_price:
        return None
    return {
        'return_pct': (end_price - start_price) / start_price * 100,
        'max_drawdown_pct': (min_price - start_price) / start_price * 100,
    }


def _pearson(a, b):
    common = sorted(set(a) & set(b))
    if len(common) < 30:
        return None
    xa, xb = [a[d] for d in common], [b[d] for d in common]
    ma, mb = sum(xa) / len(xa), sum(xb) / len(xb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(xa, xb))
    va = sum((x - ma) ** 2 for x in xa)
    vb = sum((y - mb) ** 2 for y in xb)
    if va == 0 or vb == 0:
        return None
    return cov / (va ** 0.5 * vb ** 0.5)


def _percentile(sorted_list, pct):
    if not sorted_list:
        return None
    idx = max(0, min(len(sorted_list) - 1, int(len(sorted_list) * pct)))
    return sorted_list[idx]


def run_stress_test(db, codes):
    """codes: 一份自選股清單的股票代號列表（會自動去重、保留原順序）。"""
    codes = list(dict.fromkeys(codes))
    n = len(codes)
    if n == 0:
        return {'holdings_count': 0, 'scenarios': [], 'industry_concentration': None,
                'correlation': None, 'var': None}

    names, industries_by_code = {}, {}
    for code in codes:
        row = db.execute(text('SELECT name, industry FROM stocks WHERE code = :c'), {'c': code}).mappings().first()
        names[code] = row['name'] if row else code
        industries_by_code[code] = (row['industry'] if row else None) or '未分類'

    # 1) 歷史情境回放
    scenarios_out = []
    for sc in STRESS_SCENARIOS:
        impacts = [_scenario_impact(db, code, sc['start'], sc['end']) for code in codes]
        impacts = [i for i in impacts if i]
        avg_return = sum(i['return_pct'] for i in impacts) / len(impacts) if impacts else None
        avg_dd = sum(i['max_drawdown_pct'] for i in impacts) / len(impacts) if impacts else None
        scenarios_out.append({
            'key': sc['key'], 'label': sc['label'], 'start': sc['start'], 'end': sc['end'],
            'portfolio_return_pct': round(avg_return, 2) if avg_return is not None else None,
            'portfolio_max_drawdown_pct': round(avg_dd, 2) if avg_dd is not None else None,
            'covered': len(impacts), 'total': n,
        })

    # 2) 產業集中度 HHI（等權重、依檔數，不是依市值/金額）
    industries = {}
    for ind in industries_by_code.values():
        industries[ind] = industries.get(ind, 0) + 1
    hhi = sum((cnt / n) ** 2 for cnt in industries.values()) * 10000
    hhi_level = '高度集中' if hhi > 2500 else ('中度集中' if hhi > 1500 else '分散')
    industry_breakdown = sorted(
        [{'industry': k, 'count': v, 'pct': round(v / n * 100, 1)} for k, v in industries.items()],
        key=lambda x: -x['count']
    )

    # 3) 相關性 + 4) 歷史模擬法 VaR（近400個日曆天≈近1年交易日）
    since = (date.today() - timedelta(days=400)).isoformat()
    returns_by_code = {}
    for code in codes:
        rows = db.execute(text('''
            SELECT date, close FROM daily_prices WHERE stock_code = :c AND date >= :since ORDER BY date ASC
        '''), {'c': code, 'since': since}).mappings().all()
        closes_by_date = [(r['date'], r['close']) for r in rows if r['close'] is not None]
        rets = {}
        for i in range(1, len(closes_by_date)):
            prev_d, prev_c = closes_by_date[i - 1]
            cur_d, cur_c = closes_by_date[i]
            if prev_c:
                rets[cur_d] = (cur_c - prev_c) / prev_c
        returns_by_code[code] = rets

    pairs = []
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            c = _pearson(returns_by_code[codes[i]], returns_by_code[codes[j]])
            if c is not None:
                pairs.append((codes[i], codes[j], c))
    avg_corr = sum(p[2] for p in pairs) / len(pairs) if pairs else None
    max_pair = max(pairs, key=lambda p: p[2]) if pairs else None

    all_dates = sorted(set().union(*[set(r.keys()) for r in returns_by_code.values()]))
    portfolio_returns = []
    for d in all_dates:
        day_rets = [returns_by_code[code][d] for code in codes if d in returns_by_code[code]]
        if day_rets:
            portfolio_returns.append(sum(day_rets) / len(day_rets))
    portfolio_returns.sort()
    var95 = _percentile(portfolio_returns, 0.05)
    var99 = _percentile(portfolio_returns, 0.01)

    return {
        'holdings_count': n,
        'scenarios': scenarios_out,
        'industry_concentration': {
            'hhi': round(hhi, 0), 'level': hhi_level, 'breakdown': industry_breakdown,
        },
        'correlation': {
            'avg_pairwise': round(avg_corr, 3) if avg_corr is not None else None,
            'pairs_computed': len(pairs),
            'most_correlated_pair': (
                {'a': max_pair[0], 'a_name': names.get(max_pair[0], max_pair[0]),
                 'b': max_pair[1], 'b_name': names.get(max_pair[1], max_pair[1]),
                 'corr': round(max_pair[2], 3)}
                if max_pair else None
            ),
        },
        'var': {
            'var_95_pct': round(var95 * 100, 2) if var95 is not None else None,
            'var_99_pct': round(var99 * 100, 2) if var99 is not None else None,
            'days_used': len(portfolio_returns),
        },
    }
