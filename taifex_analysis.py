"""台指期權籌碼衍生計算（Put/Call Ratio、到期別分類、支撐/壓力、大戶多空比）。
純 Python，不做 DB 查詢／不落地存表——跟 chip_peak.py 同一種設計哲學：呼叫端
（app.py）先把需要的列查出來（dict 或 ORM row 皆可，這裡只用屬性存取），
這裡只負責計算。

已知限制：「大戶多空比」原站確切算法未公開，這裡用近似公式（十大交易人
淨部位 / 全市場未沖銷部位），標記為 approx。"""
import re

_MONTH_RE = re.compile(r'^\d{6}$')
_WED_RE = re.compile(r'^(\d{6})W\d+$')
_FRI_RE = re.compile(r'^(\d{6})F\d+$')


def _get(row, key):
    """Accept either a dict or an ORM row/object for `row`."""
    return row[key] if isinstance(row, dict) else getattr(row, key)


def compute_pc_ratio(option_rows):
    """option_rows: 某一天全部履約價×全部到期月份的 TaifexOptionDaily 列
    （不分到期別，比照期交所官方 Put/Call Ratio 全市場口徑）。回傳成交量/
    未平倉量的買權賣權比率（%），習慣上 PC Ratio = Put / Call。資料不足回傳
    {}。"""
    call_volume = put_volume = call_oi = put_oi = 0
    for r in option_rows:
        cp = _get(r, 'call_put')
        volume = _get(r, 'volume') or 0
        oi = _get(r, 'open_interest') or 0
        if cp == 'call':
            call_volume += volume
            call_oi += oi
        elif cp == 'put':
            put_volume += volume
            put_oi += oi
    if not call_volume and not put_volume:
        return {}
    return {
        'call_volume': call_volume,
        'put_volume': put_volume,
        'volume_ratio_pct': round(put_volume / call_volume * 100, 2) if call_volume else None,
        'call_oi': call_oi,
        'put_oi': put_oi,
        'oi_ratio_pct': round(put_oi / call_oi * 100, 2) if call_oi else None,
    }


def classify_expiry_contracts(distinct_contract_dates):
    """distinct_contract_dates: 某一天 TaifexOptionDaily 出現過的 contract_date
    去重集合。依命名規則分四類：純 YYYYMM=月契約，YYYYMMWn=週三到期週契約，
    YYYYMMFn=週五到期週契約。FinMind 當天回傳的都是「尚未到期、目前掛牌中」
    的契約，所以字串由小到大排序後取第一筆即為「最近一個尚未到期」的那個
    ——不需要另外查到期日曆。回傳 {'week3', 'week5', 'month', 'next_month'}，
    查不到的類別值為 None。"""
    months = sorted(cd for cd in distinct_contract_dates if _MONTH_RE.match(cd))
    weds = sorted(cd for cd in distinct_contract_dates if _WED_RE.match(cd))
    fris = sorted(cd for cd in distinct_contract_dates if _FRI_RE.match(cd))
    return {
        'week3': weds[0] if weds else None,
        'week5': fris[0] if fris else None,
        'month': months[0] if months else None,
        'next_month': months[1] if len(months) > 1 else None,
    }


def compute_support_resistance(option_rows):
    """option_rows: 某一天、單一 contract_date 的 TaifexOptionDaily 列（所有
    履約價）。壓力 = 買權未平倉量最大的履約價（賣方是主要壓力來源）；
    支撐 = 賣權未平倉量最大的履約價。回傳 {} 若無資料。"""
    if not option_rows:
        return {}
    best_call = best_put = None
    best_call_oi = best_put_oi = -1
    for r in option_rows:
        oi = _get(r, 'open_interest') or 0
        cp = _get(r, 'call_put')
        if cp == 'call' and oi > best_call_oi:
            best_call, best_call_oi = r, oi
        elif cp == 'put' and oi > best_put_oi:
            best_put, best_put_oi = r, oi
    return {
        'resistance': _get(best_call, 'strike_price') if best_call else None,
        'resistance_oi': _get(best_call, 'open_interest') if best_call else None,
        'support': _get(best_put, 'strike_price') if best_put else None,
        'support_oi': _get(best_put, 'open_interest') if best_put else None,
    }


def compute_bull_bear_ratio(large_trader_row):
    """large_trader_row: TaifexFuturesLargeTraders 的 contract_type='all' 那一
    列。近似公式（approx，原站確切算法未公開）：十大交易人淨部位（買方-賣方）
    / 全市場未沖銷部位 × 100%。回傳 None 若 market_open_interest 為 0/缺值。"""
    if large_trader_row is None:
        return None
    market_oi = _get(large_trader_row, 'market_open_interest')
    if not market_oi:
        return None
    net = (_get(large_trader_row, 'buy_top10_trader_open_interest') or 0) \
        - (_get(large_trader_row, 'sell_top10_trader_open_interest') or 0)
    return round(net / market_oi * 100, 1)


# Contract-point-value ratios for 約當大台 (big-contract-equivalent) conversion:
# 臺股期貨(TX) 200元/點、小型臺指期貨(MTX) 50元/點、微型臺指期貨(TMF) 10元/點
# → 1 TX = 4 MTX = 20 TMF. 十大交易人沒有這個換算（TAIFEX 官方的大額交易人
# 未沖銷部位統計本來就只公布大台，見 crawler_taifex.py 的
# crawl_finmind_taifex_futures_institutional_mini docstring）。
CONTRACT_DIVISOR = {'TX': 1, 'MTX': 4, 'TMF': 20}


def compute_contract_equivalent(net_by_futures_id):
    """net_by_futures_id: {'TX': net_TX_or_None, 'MTX': net_MTX_or_None,
    'TMF': net_TMF_or_None}（net = long - short，同一個機構同一天）。回傳約當
    大台淨部位（四捨五入到整數口）。任一天沒有的商品當 0 處理，不是整段回傳
    None——三大法人每天一定有 TX 資料，MTX/TMF 缺值多半是當天很淡本來就沒
    人動用小台/微台，不是抓取失敗。"""
    total = 0.0
    for futures_id, divisor in CONTRACT_DIVISOR.items():
        net = net_by_futures_id.get(futures_id)
        if net:
            total += net / divisor
    return round(total)
