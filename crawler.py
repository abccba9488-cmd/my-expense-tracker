import re
import time
import random
import logging
from datetime import datetime, date, timedelta

import requests
import urllib3
from bs4 import BeautifulSoup

# Taiwan financial sites (TWSE, TPEX, MOPS) use non-standard SSL certs
# that fail Python's strict verification. Disable it for this trusted domain set.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_session = requests.Session()
_session.verify = False

from database import (
    SessionLocal, Stock, DailyPrice, MonthlyRevenue,
    QuarterlyFinancial, CrawlerLog
)

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15',
]

# Request counter for periodic session refresh
_req_count = 0
_SESSION_REFRESH_EVERY = 80


def _get_ua():
    return random.choice(_USER_AGENTS)


def _jitter(base: float, lo: float = 0.7, hi: float = 1.6) -> None:
    """Sleep for base * random factor to avoid fixed-interval fingerprinting."""
    time.sleep(base * random.uniform(lo, hi))


def _get(url, *, headers=None, timeout=60, retries=3):
    """GET with UA rotation, jitter retry on 429/5xx."""
    global _req_count
    h = {
        'User-Agent': _get_ua(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
    }
    if headers:
        h.update(headers)

    _req_count += 1
    if _req_count % _SESSION_REFRESH_EVERY == 0:
        _session.cookies.clear()

    for attempt in range(retries):
        resp = _session.get(url, headers=h, timeout=timeout)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = (attempt + 1) * random.uniform(8, 15)
            logger.warning('HTTP %d on %s — retry in %.1fs', resp.status_code, url, wait)
            time.sleep(wait)
            continue
        return resp
    return resp


def _post(url, *, json=None, headers=None, timeout=15, retries=3):
    """POST with UA rotation, jitter retry on 429/5xx."""
    global _req_count
    h = {
        'User-Agent': _get_ua(),
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
    }
    if headers:
        h.update(headers)

    _req_count += 1
    if _req_count % _SESSION_REFRESH_EVERY == 0:
        _session.cookies.clear()

    for attempt in range(retries):
        resp = _session.post(url, json=json, headers=h, timeout=timeout)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = (attempt + 1) * random.uniform(8, 15)
            logger.warning('HTTP %d on POST %s — retry in %.1fs', resp.status_code, url, wait)
            time.sleep(wait)
            continue
        return resp
    return resp


# Keep for backward compatibility (used nowhere externally, but just in case)
HEADERS = {
    'User-Agent': _USER_AGENTS[0],
    'Accept-Language': 'zh-TW,zh;q=0.9,en;q=0.8',
}
MOPS_API_HEADERS = {
    'User-Agent': _USER_AGENTS[0],
    'Content-Type': 'application/json',
    'Accept': 'application/json',
    'Referer': 'https://mops.twse.com.tw/mops/',
    'Origin': 'https://mops.twse.com.tw',
}


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_num(s):
    if not s:
        return None
    s = str(s).strip().replace(',', '').replace(' ', '')
    if s in ('--', '-', '', 'N/A', '除權息', '停牌'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _log(task, status, message=''):
    db = SessionLocal()
    try:
        db.add(CrawlerLog(task=task, status=status, message=str(message)[:2000]))
        db.commit()
    except Exception:
        pass
    finally:
        db.close()


def _upsert_prices(db, records):
    from sqlalchemy.dialects.sqlite import insert
    if not records:
        return 0
    db.execute(
        DailyPrice.__table__.insert().prefix_with('OR REPLACE'),
        records,
    )
    db.commit()
    return len(records)


# ── stock list ────────────────────────────────────────────────────────────────

def crawl_stock_list():
    """Crawl TWSE + TPEX stock list from TWSE ISIN."""
    _log('stock_list', 'running')
    db = SessionLocal()
    stocks = []
    try:
        for mode, market in [('2', 'TWSE'), ('4', 'TPEX')]:
            url = f'https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}'
            resp = _get(url, timeout=30)
            resp.encoding = 'big5'
            soup = BeautifulSoup(resp.text, 'lxml')

            table = soup.find('table', class_='h4')
            if not table:
                logger.warning('Stock list table not found for market=%s', market)
                continue

            current_industry = ''
            for row in table.find_all('tr')[1:]:
                cells = row.find_all('td')
                if not cells:
                    continue

                # Industry header rows have only 1 cell or a specific bgcolor
                first_text = cells[0].get_text(strip=True)

                # Detect industry header (no separating whitespace / no digit prefix)
                if len(cells) == 1:
                    current_industry = first_text
                    continue

                # Stock row: "DDDD　name" or "DDDD  name"
                m = re.match(r'^(\d{4,6})\s*[　\s]+(.+)$', first_text)
                if not m:
                    # Sometimes the industry name appears in a row with bgcolor
                    if row.get('bgcolor') in ('#c0c0c0', '#a0a0a0'):
                        current_industry = first_text
                    continue

                code = m.group(1).strip()
                name = m.group(2).strip()

                # Only 4-digit numeric codes (regular stocks + ETFs)
                if not re.match(r'^\d{4}$', code):
                    continue

                industry = cells[4].get_text(strip=True) if len(cells) > 4 else current_industry

                stocks.append({
                    'code': code,
                    'name': name,
                    'market': market,
                    'industry': industry or current_industry,
                    'updated_at': datetime.now(),
                })

            _jitter(1.0)

        # Upsert
        for s in stocks:
            existing = db.query(Stock).filter_by(code=s['code']).first()
            if existing:
                existing.name = s['name']
                existing.market = s['market']
                existing.industry = s['industry']
                existing.updated_at = datetime.now()
            else:
                db.add(Stock(
                    code=s['code'], name=s['name'],
                    market=s['market'], industry=s['industry'],
                ))
        db.commit()
        _log('stock_list', 'success', f'{len(stocks)} stocks')
        logger.info('Stock list updated: %d stocks', len(stocks))
        return len(stocks)

    except Exception as e:
        db.rollback()
        _log('stock_list', 'failed', str(e))
        logger.exception('crawl_stock_list failed')
        raise
    finally:
        db.close()


# ── daily prices ─────────────────────────────────────────────────────────────

def _fi(fields, keyword, exclude=None):
    """Find field index by keyword; optionally exclude fields containing 'exclude'."""
    for i, f in enumerate(fields):
        if keyword in f and (exclude is None or exclude not in f):
            return i
    return -1


def _parse_twse_tables(tables, trade_date):
    """Parse TWSE MI_INDEX new 'tables' format (2025+)."""
    records = []
    for tbl in tables:
        fields = tbl.get('fields', [])
        rows   = tbl.get('data', [])
        if not fields or not rows:
            continue

        i_code  = _fi(fields, '代號')
        i_close = _fi(fields, '收盤')
        if i_code < 0 or i_close < 0:
            continue  # not a price table

        i_open = _fi(fields, '開盤')
        i_high = _fi(fields, '最高')
        i_low  = _fi(fields, '最低')
        i_vol  = _fi(fields, '成交股數')
        i_dir  = _fi(fields, '漲跌(+/-)')
        # '漲跌差' or '漲跌價差', but NOT '漲跌(+/-)'
        i_chg  = _fi(fields, '漲跌', exclude='(+/-)')

        for row in rows:
            if not row or len(row) <= i_code:
                continue
            code = str(row[i_code]).strip()
            if not re.match(r'^\d{4}$', code):
                continue

            close = _parse_num(row[i_close]) if i_close < len(row) else None
            if close is None:
                continue

            # Direction encoded as HTML: color:red = up(+), color:green = down(-)
            dir_html   = row[i_dir] if i_dir >= 0 and i_dir < len(row) else ''
            chg_amount = _parse_num(row[i_chg]) if i_chg >= 0 and i_chg < len(row) else None
            if chg_amount is not None:
                if 'red' in dir_html:
                    change = chg_amount
                elif 'green' in dir_html:
                    change = -chg_amount
                else:
                    change = 0.0
            else:
                change = None

            prev    = (close - change) if change is not None else None
            chg_pct = (change / prev * 100) if prev and prev != 0 else None
            vol     = _parse_num(row[i_vol]) if i_vol >= 0 and i_vol < len(row) else None

            records.append({
                'stock_code': code,
                'date':       trade_date,
                'open':       _parse_num(row[i_open]) if i_open >= 0 and i_open < len(row) else None,
                'high':       _parse_num(row[i_high]) if i_high >= 0 and i_high < len(row) else None,
                'low':        _parse_num(row[i_low])  if i_low  >= 0 and i_low  < len(row) else None,
                'close':      close,
                'volume':     int(vol) if vol is not None else None,
                'change':     change,
                'change_pct': round(chg_pct, 2) if chg_pct is not None else None,
            })
    return records


def _parse_twse_mi_index(json_data, trade_date):
    """Parse TWSE MI_INDEX legacy format (numbered fields1/data1...)."""
    records = []
    for i in range(1, 25):
        fields = json_data.get(f'fields{i}')
        rows   = json_data.get(f'data{i}')
        if not fields or not rows:
            continue
        if '收盤價' not in fields or '證券代號' not in fields:
            continue

        i_code  = fields.index('證券代號')
        i_open  = fields.index('開盤價')  if '開盤價'  in fields else -1
        i_high  = fields.index('最高價')  if '最高價'  in fields else -1
        i_low   = fields.index('最低價')  if '最低價'  in fields else -1
        i_close = fields.index('收盤價')
        i_vol   = fields.index('成交股數') if '成交股數' in fields else -1
        i_dir   = fields.index('漲跌(+/-)') if '漲跌(+/-)' in fields else -1
        i_chg   = fields.index('漲跌價差')  if '漲跌價差'  in fields else -1

        for row in rows:
            code  = row[i_code].strip()
            if not re.match(r'^\d{4}$', code):
                continue
            close = _parse_num(row[i_close])
            if close is None:
                continue
            direction  = row[i_dir].strip() if i_dir >= 0 else ''
            chg_amount = _parse_num(row[i_chg]) if i_chg >= 0 else None
            if chg_amount is not None:
                change = chg_amount if '+' in direction else (-chg_amount if '-' in direction else 0.0)
            else:
                change = None
            prev    = (close - change) if change is not None else None
            chg_pct = (change / prev * 100) if prev and prev != 0 else None
            vol     = _parse_num(row[i_vol]) if i_vol >= 0 else None
            records.append({
                'stock_code': code, 'date': trade_date,
                'open':       _parse_num(row[i_open]) if i_open >= 0 else None,
                'high':       _parse_num(row[i_high]) if i_high >= 0 else None,
                'low':        _parse_num(row[i_low])  if i_low  >= 0 else None,
                'close':      close,
                'volume':     int(vol) if vol is not None else None,
                'change':     change,
                'change_pct': round(chg_pct, 2) if chg_pct is not None else None,
            })
    return records


def _crawl_twse_prices(date_str, trade_date):
    url = (
        'https://www.twse.com.tw/exchangeReport/MI_INDEX'
        f'?response=json&date={date_str}&type=ALL'
    )
    resp = _get(url, headers={'Referer': 'https://www.twse.com.tw/'}, timeout=60)
    data = resp.json()
    if data.get('stat') != 'OK':
        return []
    # 2025+ new format uses 'tables'; legacy uses numbered fields/data
    if 'tables' in data:
        return _parse_twse_tables(data['tables'], trade_date)
    return _parse_twse_mi_index(data, trade_date)


def _crawl_tpex_prices(date_str, trade_date):
    dt = datetime.strptime(date_str, '%Y%m%d')
    roc = f'{dt.year - 1911}/{dt.month:02d}/{dt.day:02d}'
    url = (
        'https://www.tpex.org.tw/web/stock/aftertrading/'
        f'otc_quotes_no1430/stk_wn1430_result.php'
        f'?l=zh-tw&d={roc}&se=AL&_={int(time.time()*1000)}'
    )
    resp = _get(url, headers={'Referer': 'https://www.tpex.org.tw/'}, timeout=60)
    data = resp.json()

    # 2025+ new format: {"tables":[{"fields":[...],"data":[...]}]}
    # Legacy format:    {"aaData":[...]}
    if 'tables' in data:
        tables = data.get('tables') or []
        if not tables:
            return []
        tbl    = tables[0]
        fields = tbl.get('fields', [])
        rows   = tbl.get('data', [])

        def fi(keyword):
            for i, f in enumerate(fields):
                if keyword in f:
                    return i
            return -1

        i_code   = fi('代號')
        i_close  = fi('收盤')
        i_change = fi('漲跌')
        i_open   = fi('開盤')
        i_high   = fi('最高')
        i_low    = fi('最低')
        # New format: 成交股數(股) — already in shares, not thousands
        i_vol    = fi('成交股數')
        vol_multiplier = 1
    else:
        # Legacy aaData format — volume in thousands
        rows = data.get('aaData') or []
        i_code, i_close, i_change = 0, 2, 3
        i_open, i_high, i_low, i_vol = 4, 5, 6, 8
        vol_multiplier = 1000

    records = []
    for row in rows:
        if not row or i_code < 0:
            continue
        code = str(row[i_code]).strip()
        if not re.match(r'^\d{4}$', code):
            continue

        close  = _parse_num(row[i_close])  if i_close  >= 0 and i_close  < len(row) else None
        change = _parse_num(row[i_change]) if i_change >= 0 and i_change < len(row) else None
        if close is None:
            continue

        prev    = (close - change) if change is not None else None
        chg_pct = (change / prev * 100) if prev and prev != 0 else None
        vol_raw = _parse_num(row[i_vol]) if i_vol >= 0 and i_vol < len(row) else None

        records.append({
            'stock_code': code,
            'date':       trade_date,
            'open':       _parse_num(row[i_open]) if i_open >= 0 and i_open < len(row) else None,
            'high':       _parse_num(row[i_high]) if i_high >= 0 and i_high < len(row) else None,
            'low':        _parse_num(row[i_low])  if i_low  >= 0 and i_low  < len(row) else None,
            'close':      close,
            'volume':     int(vol_raw * vol_multiplier) if vol_raw is not None else None,
            'change':     change,
            'change_pct': round(chg_pct, 2) if chg_pct is not None else None,
        })
    return records


def crawl_daily_prices(date_str: str):
    """Crawl TWSE + TPEX daily prices for the given date (YYYYMMDD)."""
    _log('daily_price', 'running', date_str)
    db = SessionLocal()
    try:
        trade_date = datetime.strptime(date_str, '%Y%m%d').date()
        records = []

        twse = _crawl_twse_prices(date_str, trade_date)
        records.extend(twse)
        logger.info('TWSE %s: %d records', date_str, len(twse))

        _jitter(0.5)

        tpex = _crawl_tpex_prices(date_str, trade_date)
        records.extend(tpex)
        logger.info('TPEX %s: %d records', date_str, len(tpex))

        if not records:
            _log('daily_price', 'success', f'{date_str}: no data (holiday?)')
            return 0

        # Bulk upsert using INSERT OR REPLACE
        db.execute(
            DailyPrice.__table__.insert().prefix_with('OR REPLACE'),
            records,
        )
        db.commit()
        _log('daily_price', 'success', f'{date_str}: {len(records)} records')
        return len(records)

    except Exception as e:
        db.rollback()
        _log('daily_price', 'failed', f'{date_str}: {e}')
        logger.exception('crawl_daily_prices failed for %s', date_str)
        raise
    finally:
        db.close()


# ── monthly revenue ───────────────────────────────────────────────────────────
# MOPS new SPA API (discovered 2026-06):
#   POST https://mops.twse.com.tw/mops/api/t05st10_ifrs
#   Body: {"companyId":"2330","dataType":"2","year":"115","month":"4",...}
#   Response data[0][1] = current month revenue (千元)
#             data[3][1] = year-over-year change %

def _mops_revenue_one(code, roc_year, month):
    """Call MOPS JSON API for one company's monthly revenue. Returns (revenue, yoy) or (None,None)."""
    resp = _post(
        'https://mops.twse.com.tw/mops/api/t05st10_ifrs',
        json={
            'companyId':          code,
            'dataType':           '2',
            'year':               str(roc_year),
            'month':              str(month),
            'subsidiaryCompanyId': '',
        },
        headers={'Referer': 'https://mops.twse.com.tw/mops/', 'Origin': 'https://mops.twse.com.tw'},
        timeout=15,
    )
    if resp.status_code != 200:
        return None, None
    body = resp.json()
    if body.get('code') != 200:
        return None, None
    rows = body.get('result', {}).get('data', [])
    if not rows:
        return None, None
    revenue     = _parse_num(rows[0][1]) if len(rows) > 0 else None
    revenue_yoy = _parse_num(rows[3][1]) if len(rows) > 3 else None
    if revenue is None:
        return None, None
    return int(revenue), revenue_yoy


def crawl_monthly_revenue(year: int, month: int):
    """Crawl all listed companies' monthly revenue from MOPS JSON API."""
    _log('monthly_revenue', 'running', f'{year}/{month:02d}')
    db = SessionLocal()
    roc_year = year - 1911
    stocks = db.query(Stock).all()
    total = 0

    try:
        for i, stock in enumerate(stocks):
            try:
                revenue, revenue_yoy = _mops_revenue_one(stock.code, roc_year, month)
                if revenue is None:
                    _jitter(0.2)
                    continue

                # MoM: calculate from previous month stored in DB
                pm, py = (month - 1, year) if month > 1 else (12, year - 1)
                prev = db.query(MonthlyRevenue).filter_by(
                    stock_code=stock.code, year=py, month=pm
                ).first()
                revenue_mom = None
                if prev and prev.revenue:
                    revenue_mom = round((revenue - prev.revenue) / prev.revenue * 100, 2)

                existing = db.query(MonthlyRevenue).filter_by(
                    stock_code=stock.code, year=year, month=month
                ).first()
                if existing:
                    existing.revenue     = revenue
                    existing.revenue_yoy = revenue_yoy
                    existing.revenue_mom = revenue_mom
                    # start_price intentionally unchanged (keep first-crawl price)
                else:
                    latest_dp = db.query(DailyPrice.close).filter_by(
                        stock_code=stock.code
                    ).order_by(DailyPrice.date.desc()).first()
                    db.add(MonthlyRevenue(
                        stock_code=stock.code, year=year, month=month,
                        revenue=revenue, revenue_yoy=revenue_yoy, revenue_mom=revenue_mom,
                        start_price=latest_dp[0] if latest_dp else None,
                    ))
                total += 1

                if total % 50 == 0:
                    db.commit()
                    logger.info('Monthly revenue %d/%d: %d saved', year, month, total)

            except Exception as e:
                logger.warning('Revenue skip %s: %s', stock.code, e)

            _jitter(0.25)

        db.commit()
        _log('monthly_revenue', 'success', f'{year}/{month:02d}: {total} records')
        return total

    except Exception as e:
        db.rollback()
        _log('monthly_revenue', 'failed', str(e))
        logger.exception('crawl_monthly_revenue failed %d/%d', year, month)
        raise
    finally:
        db.close()


# ── quarterly financials ──────────────────────────────────────────────────────
# MOPS new SPA API:
#   POST https://mops.twse.com.tw/mops/api/t164sb04
#   Body: {"companyId":"2330","dataType":"2","year":"114","season":"4",...}
#   result.reportList = list of rows [label, cur_val, cur_pct, prior_val, prior_pct]
#   Values are in 千元; EPS is in NTD/share.
#   Note: season=4 gives cumulative (full-year) figures.

def _find_in_reportlist(rows, *keywords):
    """Return the current-period numeric value of the first row whose label matches any keyword."""
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        label = row[0]
        if any(kw in label for kw in keywords):
            val = _parse_num(row[1])
            if val is not None:
                return val
    return None


def _mops_quarterly_one(code, roc_year, quarter):
    """Call MOPS JSON API for one company's quarterly income statement."""
    resp = _post(
        'https://mops.twse.com.tw/mops/api/t164sb04',
        json={
            'companyId':          code,
            'dataType':           '2',
            'year':               str(roc_year),
            'season':             str(quarter),
            'subsidiaryCompanyId': '',
        },
        headers={'Referer': 'https://mops.twse.com.tw/mops/', 'Origin': 'https://mops.twse.com.tw'},
        timeout=15,
    )
    if resp.status_code != 200:
        return None, None, None, None
    body = resp.json()
    if body.get('code') != 200:
        return None, None, None, None
    rows = body.get('result', {}).get('reportList', [])
    if not rows:
        return None, None, None, None

    revenue   = _find_in_reportlist(rows, '營業收入合計', '收入合計')
    op_income = _find_in_reportlist(rows, '營業利益', '營業損失')
    net_inc   = _find_in_reportlist(rows, '本期淨利', '本期損益', '繼續營業本期淨利')
    eps       = _find_in_reportlist(rows, '基本每股盈餘', '基本每股')

    # Revenue and income are in 千元; convert to 元 for storage (keep as 千元 per schema)
    to_int = lambda v: int(v) if v is not None else None
    return to_int(revenue), to_int(op_income), to_int(net_inc), eps


def crawl_quarterly_financials(year: int, quarter: int):
    """Crawl quarterly EPS and financials for all stocks from MOPS JSON API."""
    _log('quarterly', 'running', f'{year}Q{quarter}')
    db = SessionLocal()
    stocks = db.query(Stock).all()
    total = 0
    roc_year = year - 1911

    try:
        for stock in stocks:
            try:
                revenue, op_income, net_income, eps = _mops_quarterly_one(
                    stock.code, roc_year, quarter
                )
                if eps is None and revenue is None:
                    _jitter(0.25)
                    continue

                # Q4 from MOPS is the annual report; subtract Q1+Q2+Q3 to get individual Q4
                if quarter == 4:
                    q1 = db.query(QuarterlyFinancial).filter_by(stock_code=stock.code, year=year, quarter=1).first()
                    q2 = db.query(QuarterlyFinancial).filter_by(stock_code=stock.code, year=year, quarter=2).first()
                    q3 = db.query(QuarterlyFinancial).filter_by(stock_code=stock.code, year=year, quarter=3).first()
                    if q1 and q2 and q3:
                        if revenue is not None and q1.revenue and q2.revenue and q3.revenue:
                            revenue = revenue - q1.revenue - q2.revenue - q3.revenue
                        if op_income is not None and q1.operating_income and q2.operating_income and q3.operating_income:
                            op_income = op_income - q1.operating_income - q2.operating_income - q3.operating_income
                        if net_income is not None and q1.net_income and q2.net_income and q3.net_income:
                            net_income = net_income - q1.net_income - q2.net_income - q3.net_income
                        if eps is not None and q1.eps and q2.eps and q3.eps:
                            eps = round(eps - q1.eps - q2.eps - q3.eps, 2)
                    else:
                        logger.warning('Q4 adjustment skipped for %s %d: Q1/Q2/Q3 missing', stock.code, year)

                existing = db.query(QuarterlyFinancial).filter_by(
                    stock_code=stock.code, year=year, quarter=quarter
                ).first()
                if existing:
                    existing.revenue          = revenue
                    existing.operating_income = op_income
                    existing.net_income       = net_income
                    existing.eps              = eps
                else:
                    db.add(QuarterlyFinancial(
                        stock_code=stock.code, year=year, quarter=quarter,
                        revenue=revenue, operating_income=op_income,
                        net_income=net_income, eps=eps,
                    ))
                total += 1
                if total % 50 == 0:
                    db.commit()
                    logger.info('Quarterly %dQ%d: %d saved', year, quarter, total)

            except Exception as e:
                logger.warning('Quarterly skip %s: %s', stock.code, e)

            _jitter(0.3)

        db.commit()
        _log('quarterly', 'success', f'{year}Q{quarter}: {total} records')
        return total

    except Exception as e:
        db.rollback()
        _log('quarterly', 'failed', str(e))
        logger.exception('crawl_quarterly_financials failed %dQ%d', year, quarter)
        raise
    finally:
        db.close()


# ── convenience ───────────────────────────────────────────────────────────────

def get_recent_trading_days(n=10):
    """Return last n calendar weekdays as YYYYMMDD strings (oldest first)."""
    days = []
    d = date.today()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime('%Y%m%d'))
        d -= timedelta(days=1)
    return list(reversed(days))
