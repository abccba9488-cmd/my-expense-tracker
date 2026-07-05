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
    QuarterlyFinancial, CrawlerLog, Announcement, StockAiAnalysis,
    InstitutionalTrade, HoldingConcentration, FinancialExtra,
    DividendPolicy, DividendFillEvent,
)
import finmind_client

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

                # Turnaround signal: company is still loss-making as of its latest
                # reported quarter, but this month's revenue YoY hit the same 20%
                # bar used for 營收飆股 — a candidate worth watching, not a computed
                # EPS/PE estimate. Recomputed every crawl since revenue_yoy changes
                # monthly and the "latest quarter" EPS it's checked against can too.
                turnaround_signal = 0
                if revenue_yoy is not None and revenue_yoy >= 20:
                    latest_q = db.query(QuarterlyFinancial).filter_by(
                        stock_code=stock.code
                    ).order_by(QuarterlyFinancial.year.desc(), QuarterlyFinancial.quarter.desc()).first()
                    if latest_q and latest_q.eps is not None and latest_q.eps < 0:
                        turnaround_signal = 1

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
                    existing.turnaround_signal = turnaround_signal
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
                        turnaround_signal=turnaround_signal,
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


_AI_SYSTEM_PROMPT = """你是一位專業的台灣股票分析師，請根據提供的資料與你取得的最新網路資訊，進行簡潔明確的投資評分與風險提示。

【即時搜尋要求】
1. 你必須搜尋並引用該公司與其所屬產業的最新新聞與產業動態，不得只依賴使用者提供的公告內容。
2. 若找不到相關新聞，必須在分析中明確說明「未能取得最新新聞，以下評估僅根據現有財務與公告資料」。
3. 當預估本益比 > 20 時，必須特別搜尋產業與個股熱度，由你自行判斷是否屬於當前市場熱門題材。

【已知數據 — 直接採用，不要自己重新計算】
使用者輸入中的「系統已計算數據」區塊（單月EPS、去年同月EPS、EPS年增率、是否由虧轉盈、預估全年EPS、預估本益比）皆為系統已從公告原文解析計算完成，請直接採用這些數值進行評級判斷，不要自行重算或質疑其正確性。

【評級標準 — 依優先順序綜合判斷，請嚴格執行】
🔴 強烈買進（路徑A或路徑B任一即可）：
  路徑A：預估本益比 ≤ 20 + EPS年增 > 0%（含由虧轉盈）+ 營收不衰退
  路徑B：預估本益比 > 20 + 有熱門題材支撐 + （EPS年增 > 30% 或 EPS成長率大幅優於營收成長率）
🟠 建議買進：EPS或營收正成長 + 預估本益比 ≤ 30，不需要強烈題材支撐
🟡 一般觀望：成長有限（年增 < 10%）或本益比 > 30 缺題材，或虧損但收窄中
🟢 需要小心：營收或EPS年減、財務惡化、由盈轉虧或衰退 > 30%

【輸出格式 — 嚴格遵守】
只輸出一個純 JSON 物件，不要任何 JSON 以外的文字，不要加 markdown 標記或反引號。
{
  "ai_rating": "🔴 強烈買進",
  "ai_analysis": "4段分析文字：評級理由+數據、成長動能分析、產業熱度與風險、結論，段落間用\\n分隔"
}

規則：
- ai_rating 只能是 🔴 強烈買進／🟠 建議買進／🟡 一般觀望／🟢 需要小心 四種之一
- 禁用 [1][2][3] 等引用標記，不可出現「根據來源」等字樣
- 全部使用繁體中文，專有名詞可保留英文縮寫
- ai_analysis 為純文字，段落間用\\n分隔，不使用任何 HTML 標籤"""


def _analyze_with_ai(stock_code, stock_name, subject, content, parsed,
                      estimated_annual_eps, estimated_pe):
    """Call OpenRouter to rate + write an analysis for a self-disclosure
    announcement. All numeric fields (monthly_eps etc.) are already
    deterministically parsed by _parse_disclosure()/crawl_announcements()
    — AI is explicitly told to use them as given, not recompute, mirroring
    the reference n8n workflow's "系統預算值" instruction. Returns
    (ai_rating, ai_analysis), both None if no API key is configured or
    the call fails for any reason (never raises — a failed analysis
    should not block saving the deterministic numeric fields)."""
    api_key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not api_key:
        return None, None
    model = os.environ.get('OPENROUTER_MODEL', 'perplexity/sonar')

    known = (
        f"單月EPS：{parsed.get('monthly_eps')}\n"
        f"去年同月EPS：{parsed.get('prior_year_eps')}\n"
        f"EPS年增率：{parsed.get('eps_yoy')}%\n"
        f"是否由虧轉盈：{'是' if parsed.get('turnaround') else '否'}\n"
        f"預估全年EPS：{estimated_annual_eps}\n"
        f"預估本益比：{estimated_pe}"
    )
    user_msg = (
        f"股票：{stock_name}（{stock_code}）\n主旨：{subject}\n\n"
        f"【系統已計算數據】\n{known}\n\n公告說明：\n{content[:3000]}"
    )
    try:
        resp = requests.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'HTTP-Referer': 'https://stock-market.zeabur.app',
            },
            json={
                'model': model,
                'messages': [
                    {'role': 'system', 'content': _AI_SYSTEM_PROMPT},
                    {'role': 'user',   'content': user_msg},
                ],
                'response_format': {'type': 'json_object'},
            },
            timeout=90,
        )
        if resp.status_code == 429:
            logger.warning('AI 429 rate limit for %s — skipping', stock_code)
            return None, None
        resp.raise_for_status()
        raw = resp.json()['choices'][0]['message']['content']
        m = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', raw)
        text_to_parse = m.group(1) if m else raw
        if not text_to_parse.strip().startswith('{'):
            bm = re.search(r'\{[\s\S]+\}', text_to_parse)
            if bm:
                text_to_parse = bm.group(0)
        try:
            j = _json.loads(text_to_parse)
        except _json.JSONDecodeError:
            logger.warning('AI JSON parse failed for %s — raw: %s', stock_code, raw[:500])
            return None, None
        return j.get('ai_rating'), j.get('ai_analysis')
    except Exception as e:
        logger.warning('AI analysis failed for %s: %s', stock_code, e)
        return None, None


def crawl_announcements(date_str=None, limit=None):
    """Crawl MOPS重大訊息 for self-disclosure (自結) EPS announcements.

    The full 主旨/說明 for every announcement of the day comes straight out
    of the single list-page response's hidden form fields (see
    _parse_announcement_rows) — no per-item detail-page fetch at all, so
    none of the old anti-bot/endpoint problems apply. `limit` caps how
    many parsed rows get processed (testing only; leave None in
    production).

    All numeric fields are computed deterministically, never by AI. If
    OPENROUTER_API_KEY is set, each row that passes the monthly_eps filter
    also gets an AI rating + analysis (_analyze_with_ai) — inline, one
    call per row, mirroring the reference n8n workflow's logic and
    pacing. This means a day with many candidates can take a while; if
    no API key is set, AI is skipped entirely and this stays as fast as
    before.
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
        # An earlier version also kept any 注意交易資訊-flagged announcement
        # even without EPS, but that let irrelevant noise through too (e.g.
        # a convertible-bond price-trigger notice that happens to use the
        # same phrase but has nothing to do with the company's own EPS).
        # Now the sole inclusion criterion is an actual extracted
        # monthly_eps — that only succeeds against the real tabular
        # self-disclosure formats handled by _parse_disclosure().
        try:
            for row in rows:
                code = row['code']
                subject = row['subject']
                content = row['content']

                parsed = _parse_disclosure(content) if content else {}
                if 'monthly_eps' not in parsed:
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

                    ai_rating, ai_analysis = None, None
                    if os.environ.get('OPENROUTER_API_KEY', '').strip():
                        # Inline, one call per saved candidate — matches the
                        # reference n8n workflow's pacing (it paces every AI
                        # call too, to stay under its model's rate limit).
                        # This means a day with many candidates can take much
                        # longer than the few seconds the rest of the crawl
                        # takes; that's expected, not a bug.
                        time.sleep(20)
                        ai_rating, ai_analysis = _analyze_with_ai(
                            code, row['name'], subject, content, parsed,
                            estimated_annual_eps, estimated_pe)

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
                            'ai_rating':            ai_rating,
                            'ai_analysis':          ai_analysis,
                            'created_at':           datetime.now(_TZ),
                        }],
                    )
                    db.commit()
                    saved += 1
                    logger.info('Saved announcement %s %s monthly_eps=%s pe=%s rating=%s',
                                code, subject[:40], monthly_eps, estimated_pe, ai_rating)

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


# ── on-demand AI stock analysis (admin-only, never automatic/bulk) ─────────────

_STOCK_AI_SYSTEM_PROMPT = """你是一位擁有20年經驗、精通估值法的基金經理人。你的看法專業、深入、有獨特見解。

【任務】根據提供的個股財務數據（已從本站資料庫算好，直接採用不要重算）與你即時搜尋到的市場資訊，評估這檔股票是否適合中長期投資，並給出目標價區間。

【即時搜尋要求】
1. 搜尋並列出同業競爭者目前的「預估本益比」。
2. 搜尋各大外資對這檔股票今年全年EPS的預估值。
3. 搜尋近1-3個月是否有重大新聞影響股價（訂單、產能、價格、客戶集中度等）。
4. 搜尋近3個月法人（外資/投信/自營商）籌碼是否在累積或出脫。
5. 若以上搜尋不到，於分析中明確說明「未搜尋到相關資訊」，不可編造。

【定價計算】設定20%折價作為安全邊際：
便宜價 = min(本站EPS估值, 外資EPS估值) × 同業平均本益比 × (1-20%)
合理價 = EPS共識 × 同業平均本益比
昂貴價 = 樂觀EPS情境 × 同業本益比上緣
若EPS為負或本益比無法計算，三個目標價請回傳 null，改用文字描述成長性與轉機題材。

【輸出格式】只輸出一個純JSON物件，不要任何JSON以外文字、不要markdown標記：
{
  "ai_rating": "🔴 強烈買進",
  "target_cheap": 100.0,
  "target_fair": 120.0,
  "target_expensive": 140.0,
  "ai_analysis": "4段分析文字：評級理由+數據、成長動能、產業熱度與風險、結論，段落間用\\n分隔"
}

規則：
- ai_rating 只能是「🔴 強烈買進」「🟠 建議買進」「🟡 一般觀望」「🟢 需要小心」四種之一
- 禁用 [1][2][3] 引用標記，不可出現「根據來源」字樣
- 全部使用繁體中文
- ai_analysis 為純文字，段落間用\\n分隔，不使用任何HTML標籤"""


def analyze_stock_with_ai(code):
    """On-demand, admin-triggered AI valuation analysis for one stock —
    never called automatically or in bulk (each call is a paid OpenRouter
    request the admin explicitly asked for). Pulls objective numbers
    straight from this site's own DB (current price, PE — same formula as
    _SUMMARY_SQL, revenue/EPS trend, the site's own 營收預估股價 momentum
    metric — same formula as static/js/app.js's calcEst()), feeds them to
    AI as "already computed, use as given", and lets AI fill in only what
    the DB can't: peer PE, analyst EPS estimates, news, institutional flow.

    Always upserts a stock_ai_analysis row, even if the AI call itself
    fails (ai_rating left NULL in that case, logged as 'failed') — only
    raises for a genuinely bad input (unknown code / no price data at
    all), since those mean there's nothing sensible to analyze."""
    api_key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('OPENROUTER_API_KEY not configured')

    db = SessionLocal()
    try:
        stock = db.query(Stock).filter_by(code=code).first()
        if not stock:
            raise ValueError(f'Unknown stock code {code}')

        price = (db.query(DailyPrice).filter_by(stock_code=code)
                  .order_by(DailyPrice.date.desc()).first())
        if not price:
            raise ValueError(f'{code} has no price data')

        revenues = (db.query(MonthlyRevenue).filter_by(stock_code=code)
                     .order_by(MonthlyRevenue.year.desc(), MonthlyRevenue.month.desc())
                     .limit(6).all())
        quarters = (db.query(QuarterlyFinancial).filter_by(stock_code=code)
                     .order_by(QuarterlyFinancial.year.desc(), QuarterlyFinancial.quarter.desc())
                     .limit(8).all())
        latest_q = quarters[0] if quarters else None
        latest_r = revenues[0] if revenues else None

        # PE — same formula as _SUMMARY_SQL: Q4 uses the year's summed
        # EPS, Q1-Q3 annualizes the single quarter.
        pe = None
        if latest_q and latest_q.eps is not None:
            if latest_q.quarter == 4:
                year_eps = sum(q.eps for q in quarters
                               if q.year == latest_q.year and q.eps is not None)
                if year_eps > 0:
                    pe = round(price.close / year_eps, 1)
            elif latest_q.eps > 0:
                pe = round(price.close / (latest_q.eps / latest_q.quarter * 4.0), 1)

        # 營收預估股價 — same formula as calcEst() in static/js/app.js
        est_price = None
        if (latest_r and latest_r.revenue and latest_q and latest_q.revenue
                and latest_q.revenue > 0 and latest_q.eps and latest_q.eps > 0):
            est_price = round((latest_r.revenue / latest_q.revenue) * latest_q.eps * 240, 1)

        revenue_trend = '；'.join(
            f"{r.year}/{r.month:02d} YoY {r.revenue_yoy:+.1f}%" if r.revenue_yoy is not None
            else f"{r.year}/{r.month:02d} 無資料"
            for r in revenues
        ) or '無月營收資料'
        eps_trend = '；'.join(
            f"{q.year}Q{q.quarter} EPS {q.eps}" if q.eps is not None
            else f"{q.year}Q{q.quarter} 無資料"
            for q in quarters
        ) or '無季財報資料'

        known = (
            f"目前股價：{price.close} 元（{price.date}）\n"
            f"本站本益比：{pe if pe is not None else '無法計算'}\n"
            f"最新季EPS：{latest_q.eps if latest_q else '無資料'}\n"
            f"近期月營收年增率走勢：{revenue_trend}\n"
            f"近期季EPS走勢：{eps_trend}\n"
            f"營收預估股價（本站動能指標）：{est_price if est_price is not None else '無法計算'}"
        )
        user_msg = (
            f"股票：{stock.name}（{code}，{stock.market}，{stock.industry or ''}）\n\n"
            f"【本站已計算數據】\n{known}"
        )

        model = os.environ.get('OPENROUTER_MODEL', 'perplexity/sonar')
        ai_rating = ai_analysis = target_cheap = target_fair = target_expensive = None
        try:
            resp = requests.post(
                'https://openrouter.ai/api/v1/chat/completions',
                headers={
                    'Authorization': f'Bearer {api_key}',
                    'Content-Type': 'application/json',
                    'HTTP-Referer': 'https://stock-market.zeabur.app',
                },
                json={
                    'model': model,
                    'messages': [
                        {'role': 'system', 'content': _STOCK_AI_SYSTEM_PROMPT},
                        {'role': 'user',   'content': user_msg},
                    ],
                    'response_format': {'type': 'json_object'},
                },
                timeout=90,
            )
            resp.raise_for_status()
            raw = resp.json()['choices'][0]['message']['content']
            m = re.search(r'```(?:json)?\s*([\s\S]+?)\s*```', raw)
            text_to_parse = m.group(1) if m else raw
            if not text_to_parse.strip().startswith('{'):
                bm = re.search(r'\{[\s\S]+\}', text_to_parse)
                if bm:
                    text_to_parse = bm.group(0)
            j = _json.loads(text_to_parse)
            ai_rating        = j.get('ai_rating')
            ai_analysis      = j.get('ai_analysis')
            target_cheap     = _parse_num(str(j.get('target_cheap', '') or ''))
            target_fair      = _parse_num(str(j.get('target_fair', '') or ''))
            target_expensive = _parse_num(str(j.get('target_expensive', '') or ''))
        except Exception as e:
            logger.warning('Stock AI analysis failed for %s: %s', code, e)

        existing = db.query(StockAiAnalysis).filter_by(stock_code=code).first()
        if existing:
            existing.ai_rating        = ai_rating
            existing.target_cheap     = target_cheap
            existing.target_fair      = target_fair
            existing.target_expensive = target_expensive
            existing.ai_analysis      = ai_analysis
        else:
            db.add(StockAiAnalysis(
                stock_code=code, ai_rating=ai_rating,
                target_cheap=target_cheap, target_fair=target_fair,
                target_expensive=target_expensive, ai_analysis=ai_analysis,
            ))
        db.commit()
        _log('stock_ai_analysis', 'success' if ai_rating else 'failed',
             f'{code}: rating={ai_rating}')

        return {
            'stock_code': code, 'ai_rating': ai_rating, 'ai_analysis': ai_analysis,
            'target_cheap': target_cheap, 'target_fair': target_fair,
            'target_expensive': target_expensive,
        }
    finally:
        db.close()


# ── FinMind (達人選股資料來源) ─────────────────────────────────────────────────
# Every FinMind dataset used here supports "bulk mode" — omit data_id and pass
# start_date/end_date, and it returns ALL stocks for that date/period in one
# call (verified: TaiwanStockInstitutionalInvestorsBuySell for one day returns
# ~19k rows across the whole market). So unlike the per-stock MOPS crawlers
# above, these fetch once per date/quarter and filter down to our own
# tracked universe (`Stock` table) rather than looping per stock_code.

def _to_int(v):
    return int(v) if v is not None else None


def _parse_iso_date(s):
    return datetime.strptime(s, '%Y-%m-%d').date()


def _finmind_valid_codes(db):
    return {c for (c,) in db.query(Stock.code).all()}


def _finmind_daily_rows(dataset, end_date, lookback_days, valid_codes):
    """Yield rows from `dataset` across [end_date-lookback_days, end_date],
    one FinMind call per day and filtered to valid_codes. FinMind's bulk mode
    (no data_id) only honors start_date, not end_date — a wider range
    silently collapses to just start_date's rows, verified against the live
    API (see finmind_client.fetch's docstring). Looping day by day is the
    only reliable way to cover a multi-day window."""
    for delta in range(lookback_days + 1):
        d_iso = (end_date - timedelta(days=delta)).isoformat()
        for r in finmind_client.fetch(dataset, start_date=d_iso, end_date=d_iso):
            if r.get('stock_id') in valid_codes:
                yield r


def crawl_finmind_institutional(date_str: str):
    """三大法人買賣超（日資料）。date_str: YYYYMMDD。單位：股。"""
    iso = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    _log('finmind_institutional', 'running', date_str)
    db = SessionLocal()
    try:
        valid = _finmind_valid_codes(db)
        rows = finmind_client.fetch('TaiwanStockInstitutionalInvestorsBuySell',
                                     start_date=iso, end_date=iso)
        agg = {}
        for r in rows:
            code = r['stock_id']
            if code not in valid:
                continue
            key = (code, _parse_iso_date(r['date']))
            a = agg.setdefault(key, {'foreign_buy': 0, 'foreign_sell': 0,
                                      'trust_buy': 0, 'trust_sell': 0,
                                      'dealer_buy': 0, 'dealer_sell': 0})
            name = r.get('name')
            buy, sell = r.get('buy') or 0, r.get('sell') or 0
            if name in ('Foreign_Investor', 'Foreign_Dealer_Self'):
                a['foreign_buy'] += buy
                a['foreign_sell'] += sell
            elif name == 'Investment_Trust':
                a['trust_buy'] += buy
                a['trust_sell'] += sell
            elif name in ('Dealer_self', 'Dealer_Hedging'):
                a['dealer_buy'] += buy
                a['dealer_sell'] += sell

        records = [{'stock_code': code, 'date': d, **vals} for (code, d), vals in agg.items()]
        if records:
            db.execute(InstitutionalTrade.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log('finmind_institutional', 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log('finmind_institutional', 'failed', f'{date_str}: {e}')
        logger.exception('crawl_finmind_institutional failed for %s', date_str)
        raise
    finally:
        db.close()


# HoldingSharesLevel bucket thresholds match 股泰's own definitions exactly:
# "大戶" = 1000/800/600/400張以上 (>=1,000,001/800,001/600,001/400,001 股),
# "散戶" = 200/100張以下 (<=200,000/100,000 股). Levels between 200,001 and
# 400,000 shares count toward neither bucket (matches the source definitions).
_HOLDING_LEVEL_BUCKETS = {
    'more than 1,000,001': ('pct_1000up', 'pct_800up', 'pct_600up', 'pct_400up'),
    '800,001-1,000,000':   ('pct_800up', 'pct_600up', 'pct_400up'),
    '600,001-800,000':     ('pct_600up', 'pct_400up'),
    '400,001-600,000':     ('pct_400up',),
    '100,001-200,000':     ('pct_200down',),
    '50,001-100,000':      ('pct_200down', 'pct_100down'),
    '40,001-50,000':       ('pct_200down', 'pct_100down'),
    '30,001-40,000':       ('pct_200down', 'pct_100down'),
    '20,001-30,000':       ('pct_200down', 'pct_100down'),
    '15,001-20,000':       ('pct_200down', 'pct_100down'),
    '10,001-15,000':       ('pct_200down', 'pct_100down'),
    '5,001-10,000':        ('pct_200down', 'pct_100down'),
    '1,000-5,000':         ('pct_200down', 'pct_100down'),
    '1-999':                ('pct_200down', 'pct_100down'),
}


def crawl_finmind_holding(date_str: str, lookback_days: int = 10):
    """股權分散表（週資料，TDCC 每週特定日公布，通常週五但假日會調整）。
    這個 dataset 的 bulk 模式一次只認一個確切的公布日（start_date 必須精準命中
    當週公布日，否則回傳 0 筆，不會像其他 dataset 那樣把整個 date range 內的
    資料都吐出來——已用真實 API 呼叫驗證過），所以逐日嘗試 lookback_days 天，
    公布日以外的日子回傳 0 筆直接跳過即可，成本仍然很低（一次最多
    lookback_days+1 次呼叫）。date_str: YYYYMMDD。"""
    end = datetime.strptime(date_str, '%Y%m%d').date()
    _log('finmind_holding', 'running', date_str)
    db = SessionLocal()
    try:
        valid = _finmind_valid_codes(db)
        agg = {}
        for r in _finmind_daily_rows('TaiwanStockHoldingSharesPer', end, lookback_days, valid):
            buckets = _HOLDING_LEVEL_BUCKETS.get(r.get('HoldingSharesLevel'))
            if not buckets:
                continue
            key = (r['stock_id'], _parse_iso_date(r['date']))
            a = agg.setdefault(key, {'pct_1000up': 0.0, 'pct_800up': 0.0, 'pct_600up': 0.0,
                                      'pct_400up': 0.0, 'pct_200down': 0.0, 'pct_100down': 0.0})
            pct = r.get('percent') or 0.0
            for b in buckets:
                a[b] += pct

        records = [{'stock_code': code, 'date': d, **vals} for (code, d), vals in agg.items()]
        if records:
            db.execute(HoldingConcentration.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log('finmind_holding', 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log('finmind_holding', 'failed', f'{date_str}: {e}')
        logger.exception('crawl_finmind_holding failed for %s', date_str)
        raise
    finally:
        db.close()


_BS_KEYS = ('Inventories', 'AccountsReceivableNet', 'CurrentAssets', 'CurrentLiabilities',
            'Liabilities', 'Equity', 'TotalAssets', 'LongtermBorrowings', 'CapitalStock')
_FS_KEYS = ('GrossProfit', 'CostOfGoodsSold', 'PreTaxIncome')
_CF_KEYS = ('CashFlowsFromOperatingActivities', 'NetCashInflowFromOperatingActivities',
            'InterestExpense', 'PropertyAndPlantAndEquipment')
_QUARTER_END = {1: '03-31', 2: '06-30', 3: '09-30', 4: '12-31'}


def crawl_finmind_financials(year: int, quarter: int):
    """資產負債表 + 財報毛利項目 + 現金流量表（季資料，補 quarterly_financials
    沒有的欄位；quarterly_financials 本身來自 MOPS，這裡刻意存成獨立表不混用
    兩個資料來源）。Q4 的損益/現金流項目是年度累計值，比照 quarterly_financials
    的作法減去 Q1+Q2+Q3 還原為單季值；資產負債表項目本身是期末時點值，
    不需要調整。單位：千元。"""
    period_end = f'{year}-{_QUARTER_END[quarter]}'
    _log('finmind_financials', 'running', f'{year}Q{quarter}')
    db = SessionLocal()
    try:
        valid = _finmind_valid_codes(db)

        def _pivot(dataset, keys):
            rows = finmind_client.fetch(dataset, start_date=period_end, end_date=period_end)
            out = {}
            for r in rows:
                code = r['stock_id']
                if code not in valid or r.get('type') not in keys:
                    continue
                out.setdefault(code, {})[r['type']] = r.get('value')
            return out

        bs = _pivot('TaiwanStockBalanceSheet', _BS_KEYS)
        fs = _pivot('TaiwanStockFinancialStatements', _FS_KEYS)
        cf = _pivot('TaiwanStockCashFlowsStatement', _CF_KEYS)

        def _de_cumulate(val, existing, field):
            """Q4 fields from FinMind are annual cumulative; subtract Q1-Q3
            (already stored in financial_extra) to get the individual quarter,
            same convention as quarterly_financials' own Q4 handling."""
            if val is None or existing is None:
                return val
            parts = [getattr(e, field) for e in existing]
            if any(p is None for p in parts):
                return val
            return val - sum(parts)

        records = []
        for code in set(bs) | set(fs) | set(cf):
            b, f, c = bs.get(code, {}), fs.get(code, {}), cf.get(code, {})
            ocf = c.get('CashFlowsFromOperatingActivities')
            if ocf is None:
                ocf = c.get('NetCashInflowFromOperatingActivities')
            capex = c.get('PropertyAndPlantAndEquipment')
            gross_profit, cogs, pretax = f.get('GrossProfit'), f.get('CostOfGoodsSold'), f.get('PreTaxIncome')
            interest_expense = c.get('InterestExpense')

            if quarter == 4:
                q123 = [db.query(FinancialExtra).filter_by(stock_code=code, year=year, quarter=q).first()
                        for q in (1, 2, 3)]
                if all(q123):
                    gross_profit     = _de_cumulate(gross_profit, q123, 'gross_profit')
                    cogs              = _de_cumulate(cogs, q123, 'cost_of_goods_sold')
                    pretax            = _de_cumulate(pretax, q123, 'pretax_income')
                    ocf               = _de_cumulate(ocf, q123, 'operating_cash_flow')
                    interest_expense  = _de_cumulate(interest_expense, q123, 'interest_expense')
                    capex             = _de_cumulate(capex, q123, 'capex')

            records.append({
                'stock_code': code, 'year': year, 'quarter': quarter,
                'inventories': _to_int(b.get('Inventories')),
                'accounts_receivable': _to_int(b.get('AccountsReceivableNet')),
                'current_assets': _to_int(b.get('CurrentAssets')),
                'current_liabilities': _to_int(b.get('CurrentLiabilities')),
                'liabilities': _to_int(b.get('Liabilities')),
                'equity': _to_int(b.get('Equity')),
                'total_assets': _to_int(b.get('TotalAssets')),
                'long_term_borrowings': _to_int(b.get('LongtermBorrowings')),
                'capital_stock': _to_int(b.get('CapitalStock')),
                'gross_profit': _to_int(gross_profit),
                'cost_of_goods_sold': _to_int(cogs),
                'pretax_income': _to_int(pretax),
                'operating_cash_flow': _to_int(ocf),
                'interest_expense': _to_int(interest_expense),
                'capex': _to_int(capex),
                'updated_at': datetime.now(_TZ),
            })

        if records:
            db.execute(FinancialExtra.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log('finmind_financials', 'success', f'{year}Q{quarter}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log('finmind_financials', 'failed', f'{year}Q{quarter}: {e}')
        logger.exception('crawl_finmind_financials failed %dQ%d', year, quarter)
        raise
    finally:
        db.close()


def crawl_finmind_dividend(date_str: str, lookback_days: int = 14):
    """個別股利分派事件，來源 TaiwanStockDividend。這個 dataset 跟股權分散表
    一樣，bulk 模式的 range 查詢對「各公司自己時程」的事件型資料不可靠（已用
    真實 API 呼叫驗證：整年 range 幾乎查不到資料，但單日查詢正常回傳當天全部
    公司的事件），所以逐日呼叫 lookback_days 天。逐筆存事件（不在此加總成年度
    總額，見 DividendPolicy model 說明），OR REPLACE 天然 idempotent，同一事件
    重複爬到也不會重複計算。date_str: YYYYMMDD。"""
    end = datetime.strptime(date_str, '%Y%m%d').date()
    _log('finmind_dividend', 'running', date_str)
    db = SessionLocal()
    try:
        valid = _finmind_valid_codes(db)
        records = []
        for r in _finmind_daily_rows('TaiwanStockDividend', end, lookback_days, valid):
            m = re.match(r'(\d+)', str(r.get('year', '')))
            if not m:
                continue
            records.append({
                'stock_code': r['stock_id'],
                'event_date': _parse_iso_date(r['date']),
                'fiscal_year': int(m.group(1)) + 1911,
                'cash_dividend': r.get('CashEarningsDistribution') or 0.0,
                'stock_dividend': r.get('StockEarningsDistribution') or 0.0,
            })

        if records:
            db.execute(DividendPolicy.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log('finmind_dividend', 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log('finmind_dividend', 'failed', f'{date_str}: {e}')
        logger.exception('crawl_finmind_dividend failed for %s', date_str)
        raise
    finally:
        db.close()


def crawl_finmind_dividend_result(date_str: str, lookback_days: int = 14):
    """除權息事件 + 填息判斷，來源 TaiwanStockDividendResult。跟
    crawl_finmind_dividend 同樣的 dataset 限制，逐日呼叫。填息與否用目前已有
    的 daily_prices 重新檢查所有尚未填息的舊事件（除息日之後最高收盤價 >=
    before_price），隨股價資料增加持續自我修正，不是一次性判斷。
    date_str: YYYYMMDD。"""
    end = datetime.strptime(date_str, '%Y%m%d').date()
    _log('finmind_dividend_result', 'running', date_str)
    db = SessionLocal()
    try:
        valid = _finmind_valid_codes(db)
        records = [
            {'stock_code': r['stock_id'], 'ex_date': _parse_iso_date(r['date']),
             'before_price': r.get('before_price'), 'filled': None}
            for r in _finmind_daily_rows('TaiwanStockDividendResult', end, lookback_days, valid)
        ]
        if records:
            # OR IGNORE: keep whatever `filled` an existing event already has,
            # only insert events we haven't seen before.
            db.execute(DividendFillEvent.__table__.insert().prefix_with('OR IGNORE'), records)
            db.commit()

        db.execute(text('''
            UPDATE dividend_fill_events
            SET filled = 1
            WHERE (filled IS NULL OR filled = 0)
              AND before_price IS NOT NULL
              AND EXISTS (
                  SELECT 1 FROM daily_prices dp
                  WHERE dp.stock_code = dividend_fill_events.stock_code
                    AND dp.date > dividend_fill_events.ex_date
                    AND dp.close >= dividend_fill_events.before_price
              )
        '''))
        db.commit()
        _log('finmind_dividend_result', 'success', f'{date_str}: {len(records)} events')
        return len(records)
    except Exception as e:
        db.rollback()
        _log('finmind_dividend_result', 'failed', f'{date_str}: {e}')
        logger.exception('crawl_finmind_dividend_result failed for %s', date_str)
        raise
    finally:
        db.close()


def crawl_finmind_valuation(date_str: str, lookback_days: int = 3):
    """PER/PBR/殖利率寫回 daily_prices。用 UPDATE-only（不是 OR REPLACE），
    避免覆蓋掉當天已寫入的 OHLCV 欄位。UPDATE 對還沒有當天 daily_prices 列的
    股票會靜默影響 0 筆（例如當天股價爬蟲還沒跑完就先跑到這裡）——預設回看
    3 天，讓前幾天漏掉的 PER/PBR 有機會在後續幾次執行中自動補上，不需要
    額外的一次性補值 job。date_str: YYYYMMDD。"""
    end = datetime.strptime(date_str, '%Y%m%d').date()
    _log('finmind_valuation', 'running', date_str)
    db = SessionLocal()
    try:
        valid = _finmind_valid_codes(db)
        records = [
            {'stock_code': r['stock_id'], 'date': r['date'],
             'per': r.get('PER'), 'pbr': r.get('PBR'), 'dividend_yield': r.get('dividend_yield')}
            for r in _finmind_daily_rows('TaiwanStockPER', end, lookback_days, valid)
        ]
        if records:
            db.execute(text('''
                UPDATE daily_prices SET per=:per, pbr=:pbr, dividend_yield=:dividend_yield
                WHERE stock_code=:stock_code AND date=:date
            '''), records)
            db.commit()
        _log('finmind_valuation', 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log('finmind_valuation', 'failed', f'{date_str}: {e}')
        logger.exception('crawl_finmind_valuation failed for %s', date_str)
        raise
    finally:
        db.close()


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
