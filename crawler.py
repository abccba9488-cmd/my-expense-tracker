import json as _json
import os
import re
import time
import random
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import requests
import urllib3
from bs4 import BeautifulSoup

# Taiwan financial sites (TWSE, TPEX, MOPS) use non-standard SSL certs
# that fail Python's strict verification. Disable it for this trusted domain set.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_session = requests.Session()
_session.verify = False

from sqlalchemy import text
from database import (
    SessionLocal, Stock, DailyPrice, MonthlyRevenue,
    QuarterlyFinancial, CrawlerLog, Announcement
)

logger = logging.getLogger(__name__)
_TZ = ZoneInfo('Asia/Taipei')

_USER_AGENTS = [
    # Chrome Windows
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    # Chrome macOS
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36',
    # Firefox
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:138.0) Gecko/20100101 Firefox/138.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:138.0) Gecko/20100101 Firefox/138.0',
    # Safari
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15',
    # Edge
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0',
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
        'Accept-Encoding': 'gzip, deflate',
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
        try:
            resp = _session.get(url, headers=h, timeout=timeout)
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise
            wait = (attempt + 1) * random.uniform(8, 15)
            logger.warning('Connection error on %s: %s — retry in %.1fs', url, e, wait)
            time.sleep(wait)
            continue
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
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
    }
    if headers:
        h.update(headers)

    _req_count += 1
    if _req_count % _SESSION_REFRESH_EVERY == 0:
        _session.cookies.clear()

    for attempt in range(retries):
        try:
            resp = _session.post(url, json=json, headers=h, timeout=timeout)
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise
            wait = (attempt + 1) * random.uniform(8, 15)
            logger.warning('Connection error on POST %s: %s — retry in %.1fs', url, e, wait)
            time.sleep(wait)
            continue
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
                    'updated_at': datetime.now(_TZ),
                })

            _jitter(1.0)

        # Upsert
        for s in stocks:
            existing = db.query(Stock).filter_by(code=s['code']).first()
            if existing:
                existing.name = s['name']
                existing.market = s['market']
                existing.industry = s['industry']
                existing.updated_at = datetime.now(_TZ)
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
    try:
        data = resp.json()
    except Exception as e:
        logger.warning('TWSE %s: failed to parse JSON (status=%d, %d bytes): %s',
                        date_str, resp.status_code, len(resp.content), e)
        return []
    if data.get('stat') != 'OK':
        logger.warning('TWSE %s: stat=%r', date_str, data.get('stat'))
        return []
    # 2025+ new format uses 'tables'; legacy uses numbered fields/data
    if 'tables' in data:
        records = _parse_twse_tables(data['tables'], trade_date)
        # Old data (pre-2015) has Big5-encoded field names; if 0 records but rows exist,
        # re-decode the raw bytes as Big5 so field lookup works correctly.
        if not records:
            total_rows = sum(len(t.get('data', [])) for t in data['tables'])
            if total_rows > 0:
                try:
                    data2 = _json.loads(resp.content.decode('big5', errors='ignore'))
                    if data2.get('stat') == 'OK' and 'tables' in data2:
                        records = _parse_twse_tables(data2['tables'], trade_date)
                except Exception:
                    pass
        return records
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
    try:
        data = resp.json()
    except Exception as e:
        logger.warning('TPEX %s: failed to parse JSON (status=%d, %d bytes): %s',
                        date_str, resp.status_code, len(resp.content), e)
        return []

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
                    changed = (existing.revenue != revenue
                               or existing.revenue_yoy != revenue_yoy
                               or existing.revenue_mom != revenue_mom)
                    existing.revenue     = revenue
                    existing.revenue_yoy = revenue_yoy
                    existing.revenue_mom = revenue_mom
                    # start_price intentionally unchanged (keep first-crawl price)
                    if changed:
                        existing.updated_at = datetime.now(_TZ)
                else:
                    latest_dp = db.query(DailyPrice.close).filter_by(
                        stock_code=stock.code
                    ).order_by(DailyPrice.date.desc()).first()
                    db.add(MonthlyRevenue(
                        stock_code=stock.code, year=year, month=month,
                        revenue=revenue, revenue_yoy=revenue_yoy, revenue_mom=revenue_mom,
                        start_price=latest_dp[0] if latest_dp else None,
                        updated_at=datetime.now(_TZ),
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
                    changed = (existing.revenue != revenue
                               or existing.operating_income != op_income
                               or existing.net_income != net_income
                               or existing.eps != eps)
                    existing.revenue          = revenue
                    existing.operating_income = op_income
                    existing.net_income       = net_income
                    existing.eps              = eps
                    if changed:
                        existing.updated_at = datetime.now(_TZ)
                else:
                    db.add(QuarterlyFinancial(
                        stock_code=stock.code, year=year, quarter=quarter,
                        revenue=revenue, operating_income=op_income,
                        net_income=net_income, eps=eps,
                        updated_at=datetime.now(_TZ),
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


# ── announcements ─────────────────────────────────────────────────────────────

_ANN_BASE = 'https://mopsov.twse.com.tw/mops/web'
_TURNAROUND_RE = re.compile(r'由虧轉盈|轉虧為盈|虧轉盈')
_EPS_LABEL_RE  = re.compile(r'每股盈餘|每股稅後盈餘|每股稅前盈餘|基本每股盈餘')


def _strip_cjk_spaces(s):
    """MOPS inserts a space between every CJK character in <td>/<th> text
    (both list and detail pages). Strip it — leaves ASCII (numbers, units,
    English) spacing untouched since the lookaround requires non-ASCII on
    both sides."""
    return re.sub(r'(?<=[^\x00-\x7F]) (?=[^\x00-\x7F])', '', s)


def _extract_numbers(line):
    """Pull numeric tokens from a table row. Strip purely-textual
    parenthetical annotations first (units like '(元)', turnaround notes
    like '(由虧轉盈)' — anything with no digits inside), then convert
    MOPS' accounting-style negative parens around a plain number — e.g.
    '(25.23)' meaning -25.23 — into an explicit minus sign before
    extracting (otherwise a loss/decline row reads as a positive)."""
    cleaned = re.sub(r'\([^0-9\-]*\)', '', line)
    cleaned = re.sub(r'\(\s*(\d[\d,]*\.?\d*)\s*\)', r'-\1', cleaned)
    return [float(n.replace(',', '')) for n in re.findall(r'-?\d[\d,]*\.?\d*', cleaned)]


def _parse_disclosure(body):
    """Parse a MOPS 自結合併財務資訊 announcement body for the monthly EPS
    figure, its year-ago comparison, and YoY%. Two known layouts:
      A) TWSE 'sii' — single table, EPS row has 5 numbers (月值, 月年增%,
         季值, 季年增%, 累計值). Only the first two matter; the prior-year
         value isn't given directly so it's reverse-derived from the YoY%.
      B) TPEX 'otc' — sections 單月/單季/(最近四季)累計, each with 2-3
         numbers (本期值, 去年同期值, [年增%]). Only the 單月 section
         matters — it gives the prior-year value directly, no need to
         derive it. Every company seems to write its own numbering
         convention around these section headers — (1)/(2)/(3),
         (一)/(二)/(三), A./B., or no prefix at all (just "單月(註1)") —
         so section boundaries are detected on the bare keywords "單月"/
         "單季" themselves rather than any particular numbering style.
    Returns a dict with whatever fields were found (missing keys omitted)."""
    result = {}
    if _TURNAROUND_RE.search(body):
        result['turnaround'] = 1

    lines = [l for l in body.splitlines() if l.strip()]

    # Format A — all 5 numbers must be on the EPS row's own line. (A
    # look-ahead-to-next-line fallback used to live here for rows that
    # might wrap, but it could accidentally pull in unrelated numbers
    # from the next section's header line — e.g. "115年第1季" contributes
    # stray digits — turning a Format B/C row into a false Format A
    # match. No confirmed real case needs the fallback, so it's gone.)
    for line in lines:
        if _EPS_LABEL_RE.search(line):
            nums = _extract_numbers(line)
            if len(nums) >= 5:
                monthly_eps, yoy = nums[0], nums[1]
                result['monthly_eps'] = monthly_eps
                result['eps_yoy'] = yoy
                denom = 1 + yoy / 100
                if abs(denom) > 1e-9:
                    result['prior_year_eps'] = round(monthly_eps / denom, 4)
                return result

    # Format B — keyword-based section detection (see docstring for why
    # numbering-style matching was abandoned). Only look for the "單月"
    # trigger ONCE, before entering the section — a company's column
    # sub-header right under "單月" often repeats the word (e.g. "最近
    # 一月單月"), which would otherwise be mistaken for hitting a second
    # section and end capture immediately. Once inside, only "單季"
    # (the next section) is treated as the end boundary.
    monthly_lines, started = [], False
    for line in lines:
        if not started:
            if '單月' in line:
                started = True
            continue
        if '單季' in line or '四季累計' in line:
            break
        monthly_lines.append(line)
    for line in monthly_lines:
        if _EPS_LABEL_RE.search(line):
            nums = _extract_numbers(line)
            # Usually 3 numbers (本期/去年同期/年增%), but the YoY column
            # is sometimes qualitative text instead of a number (e.g.
            # "虧轉盈", "持續虧損") — still take the two real values then.
            if len(nums) >= 2:
                result['monthly_eps']    = nums[0]
                result['prior_year_eps'] = nums[1]
                if len(nums) >= 3:
                    result['eps_yoy'] = nums[2]
            break

    return result


def _ann_headers():
    return {
        'User-Agent': _get_ua(),
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7',
        # NOTE: do NOT add 'br' — Zeabur's container has no brotli decoder,
        # so a brotli response would silently fail to parse (same gotcha as
        # the TWSE/TPEX JSON endpoints, see CLAUDE.md).
        'Accept-Encoding': 'gzip, deflate',
        'Referer': f'{_ANN_BASE}/t05sr01_1',
        'Sec-Ch-Ua': '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'same-origin',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
        'Cache-Control': 'max-age=0',
        'Connection': 'keep-alive',
    }


_HIDDEN_FIELD_RE = re.compile(
    r"<input[^>]*type=['\"]hidden['\"][^>]*name=['\"]h(\d+)['\"][^>]*value=['\"]([^'\"]*)['\"]",
    re.IGNORECASE,
)


def _parse_announcement_rows(html):
    """Extract every row's full 主旨/說明 directly from the list response's
    hidden <input> fields — no separate detail-page fetch needed at all.

    The list page (ajax_t05st02) embeds one row's full data per group of
    hidden inputs named h{base+0}..h{base+9} (base = row_index*10):
    +0 company name, +1 company code, +2 announce date (YYYYMMDD, AD),
    +3 announce time (HHMMSS), +4 subject, +6 clause no., +7 fact date,
    +8 full description. Confirmed against a known-working n8n reference
    that parses MOPS announcements the same way, and verified directly
    against this project's own list response.

    Returns a list of dicts: code, name, date8, time6, subject, content.
    """
    fields = {}
    for numstr, value in _HIDDEN_FIELD_RE.findall(html):
        fields[int(numstr)] = value

    rows = []
    for base in sorted(n for n in fields if n % 10 == 0):
        code = (fields.get(base + 1) or '').strip()
        if not code:
            continue
        rows.append({
            'code':    code,
            'name':    _strip_cjk_spaces(fields.get(base, '') or ''),
            'date8':   fields.get(base + 2, '') or '',
            'time6':   (fields.get(base + 3, '') or '').zfill(6),
            'subject': _strip_cjk_spaces(fields.get(base + 4, '') or ''),
            'content': _strip_cjk_spaces(fields.get(base + 8, '') or ''),
        })
    return rows


def _price_at_or_before(db, stock_code, ann_date):
    """Latest daily_prices.close on or before ann_date (handles non-trading days)."""
    row = db.execute(text(
        "SELECT close FROM daily_prices WHERE stock_code=:c AND date<=:d"
        " ORDER BY date DESC LIMIT 1"
    ), {'c': stock_code, 'd': ann_date}).first()
    return row[0] if row else None


def _backfill_announcement_prices(db):
    """Re-attempt price_at_announce for rows saved before daily_prices had
    a close price for that date yet. INSERT OR IGNORE means a re-crawl of
    the same announcement never updates an already-saved row, so without
    this, a row that missed its price once stays NULL forever even after
    the price data catches up. Recomputes estimated_pe alongside when
    possible. Safe to call every run — only touches rows still missing
    price_at_announce."""
    rows = db.execute(text(
        "SELECT id, stock_code, announce_date, estimated_annual_eps"
        " FROM announcements WHERE price_at_announce IS NULL"
    )).fetchall()
    updated = 0
    for ann_id, code, announce_date, estimated_annual_eps in rows:
        price = _price_at_or_before(db, code, announce_date)
        if price is None:
            continue
        estimated_pe = (round(price / estimated_annual_eps, 1)
                         if estimated_annual_eps else None)
        db.execute(text(
            "UPDATE announcements SET price_at_announce=:p, estimated_pe=:pe WHERE id=:id"
        ), {'p': price, 'pe': estimated_pe, 'id': ann_id})
        updated += 1
    if updated:
        db.commit()
        logger.info('Backfilled price_at_announce for %d announcements', updated)
    return updated


def crawl_announcements(date_str=None, limit=None):
    """Crawl MOPS重大訊息 for self-disclosure (自結) EPS announcements.
    Pure crawl + deterministic parsing — no AI involved.

    The full 主旨/說明 for every announcement of the day comes straight out
    of the single list-page response's hidden form fields (see
    _parse_announcement_rows) — no per-item detail-page fetch at all, so
    none of the old anti-bot/endpoint problems apply. `limit` caps how
    many parsed rows get processed (testing only; leave None in
    production).
    """
    if date_str is None:
        d = datetime.now(_TZ).date() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        date_str = d.strftime('%Y%m%d')

    dt = datetime.strptime(date_str, '%Y%m%d').date()
    roc_year  = str(dt.year - 1911)
    month_str = f'{dt.month:02d}'
    day_str   = f'{dt.day:02d}'

    _log('announcements', 'running', date_str)
    logger.info('Crawling announcements for %s', date_str)

    ann_sess = requests.Session()
    ann_sess.verify = False

    try:
        ann_sess.get(f'{_ANN_BASE}/t05sr01_1', headers=_ann_headers(), timeout=20)
        _jitter(1)

        list_resp = ann_sess.post(
            f'{_ANN_BASE}/ajax_t05st02',
            data={'firstin': 'true', 'off': '1', 'step': '1', 'step00': '0',
                  'TYPEK': 'all', 'year': roc_year, 'month': month_str, 'day': day_str},
            headers={**_ann_headers(), 'Content-Type': 'application/x-www-form-urlencoded',
                     'Origin': 'https://mopsov.twse.com.tw'},
            timeout=30,
        )
        list_resp.encoding = 'utf-8'

        rows = _parse_announcement_rows(list_resp.text)
        if rows:
            logger.info('Row sample [0]: code=%s name=%s subj=%s',
                        rows[0]['code'], rows[0]['name'], rows[0]['subject'][:80])

        if not rows:
            _log('announcements', 'success', f'{date_str}: no announcements found')
            return 0

        logger.info('Found %d announcement rows for %s', len(rows), date_str)
        if limit is not None:
            rows = rows[:limit]
            logger.info('limit=%d — only processing the first %d rows', limit, len(rows))

        db = SessionLocal()
        saved = 0

        # No detail fetch needed — filter directly on the row's own
        # content. Originally this matched the reference n8n workflow's
        # criterion exactly (説明 contains 每股盈餘), but that's a naive
        # substring check: many unrelated announcement types (e.g. 限制
        # 員工權利新股/庫藏股/可轉債) are legally required to include an
        # "對公司每股盈餘稀釋情形" dilution disclosure, which contains the
        # same 4 characters without being a genuine 自結 financial table.
        # Parse first and require an actual extracted monthly_eps instead
        # — that only succeeds against the real tabular formats handled
        # by _parse_disclosure(), not prose mentioning EPS in passing.
        # 注意交易資訊 announcements are still kept even without EPS (per
        # their own keyword check) since they're a separate, deliberately
        # kept category that often lacks a financial table altogether.
        try:
            for row in rows:
                code = row['code']
                subject = row['subject']
                content = row['content']

                parsed = _parse_disclosure(content) if content else {}
                is_relevant = ('monthly_eps' in parsed
                               or '注意交易資訊' in subject
                               or '注意交易資訊' in content)
                if not is_relevant:
                    continue

                time6 = row['time6']
                announce_time = (f'{time6[0:2]}:{time6[2:4]}:{time6[4:6]}'
                                  if len(time6) == 6 else '')
                seq_no = f'{row["date8"] or date_str}_{time6}_{code}'

                try:
                    logger.info('Row parsed %s %s: %s', code, seq_no, parsed)

                    monthly_eps    = parsed.get('monthly_eps')
                    prior_year_eps = parsed.get('prior_year_eps')
                    eps_yoy        = parsed.get('eps_yoy')
                    turnaround     = parsed.get('turnaround')

                    estimated_annual_eps = (round(monthly_eps * 12, 2)
                                             if monthly_eps is not None else None)
                    price_at_announce = _price_at_or_before(db, code, dt)
                    estimated_pe = (round(price_at_announce / estimated_annual_eps, 1)
                                    if price_at_announce is not None and estimated_annual_eps
                                    else None)

                    db.execute(
                        Announcement.__table__.insert().prefix_with('OR IGNORE'),
                        [{
                            'stock_code':           code,
                            'seq_no':               seq_no,
                            'announce_date':        dt,
                            'announce_time':        announce_time,
                            'subject':              subject[:500],
                            'content':              content[:5000],
                            'price_at_announce':    price_at_announce,
                            'monthly_eps':          monthly_eps,
                            'prior_year_eps':       prior_year_eps,
                            'eps_yoy':              eps_yoy,
                            'turnaround':           turnaround,
                            'estimated_annual_eps': estimated_annual_eps,
                            'estimated_pe':         estimated_pe,
                            'created_at':           datetime.now(_TZ),
                        }],
                    )
                    db.commit()
                    saved += 1
                    logger.info('Saved announcement %s %s monthly_eps=%s pe=%s',
                                code, subject[:40], monthly_eps, estimated_pe)

                except Exception as e:
                    db.rollback()
                    logger.warning('Processing failed for %s seq %s: %s', code, seq_no, e)

            backfilled = _backfill_announcement_prices(db)

        finally:
            db.close()

        _log('announcements', 'success',
             f'{date_str}: {saved} saved / {backfilled} backfilled / {len(rows)} rows parsed')
        return saved

    except Exception as e:
        _log('announcements', 'failed', str(e))
        logger.exception('crawl_announcements failed for %s', date_str)
        raise


# ── convenience ───────────────────────────────────────────────────────────────

def get_recent_trading_days(n=10):
    """Return last n calendar weekdays as YYYYMMDD strings (oldest first)."""
    days = []
    d = datetime.now(_TZ).date()
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d.strftime('%Y%m%d'))
        d -= timedelta(days=1)
    return list(reversed(days))
