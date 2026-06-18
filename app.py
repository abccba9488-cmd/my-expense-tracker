import gzip as _gzip
import json as _json
import logging
import re
import threading
import time as _time
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

import csv
import io
from flask import Flask, jsonify, render_template, request, Response, session, send_from_directory
from sqlalchemy import desc, text, func as sa_func
from werkzeug.security import generate_password_hash, check_password_hash

import crawler
import scheduler as sched
from database import (
    SessionLocal, Stock, DailyPrice, MonthlyRevenue,
    QuarterlyFinancial, CrawlerLog, User, Watchlist, WatchlistStock, Message,
    Announcement, init_db
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)
_TZ = ZoneInfo('Asia/Taipei')

app = Flask(__name__)
app.secret_key = 'tw-stock-local-2025'
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
)

ADMIN_USERNAME = 'tom6855'


def _is_admin():
    return session.get('username') == ADMIN_USERNAME

# ── summary cache ─────────────────────────────────────────────────────────────

_SUMMARY_CACHE_TTL = 300  # seconds
_summary_cache: dict = {'body': None, 'ts': 0.0}


def _invalidate_summary_cache():
    _summary_cache['ts'] = 0.0


# ── helpers ───────────────────────────────────────────────────────────────────

def _run_bg(fn, *args):
    def _wrapper():
        try:
            fn(*args)
        finally:
            _invalidate_summary_cache()
    t = threading.Thread(target=_wrapper, daemon=True)
    t.start()


def _initial_crawl():
    """Background task: populate DB on first run."""
    logger.info('Starting initial data crawl...')
    try:
        crawler.crawl_stock_list()
    except Exception as e:
        logger.error('Initial stock list failed: %s', e)
        return

    for d in crawler.get_recent_trading_days(30):
        try:
            crawler.crawl_daily_prices(d)
        except Exception as e:
            logger.warning('Price skip %s: %s', d, e)

    today = datetime.now(_TZ)
    for delta in range(3, 0, -1):
        m = today.month - delta
        y = today.year
        if m <= 0:
            m += 12
            y -= 1
        try:
            crawler.crawl_monthly_revenue(y, m)
        except Exception as e:
            logger.warning('Revenue skip %d/%d: %s', y, m, e)

    logger.info('Initial data crawl complete.')


# ── security & SEO headers ────────────────────────────────────────────────────

@app.after_request
def add_security_headers(response):
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
    return response


@app.after_request
def compress_response(response):
    if (response.status_code < 200 or response.status_code >= 300
            or 'Content-Encoding' in response.headers
            or response.direct_passthrough):
        return response
    if 'gzip' not in request.accept_encodings:
        return response
    ct = response.content_type or ''
    if not any(t in ct for t in ('json', 'javascript', 'html', 'css', 'text')):
        return response
    data = response.get_data()
    if len(data) < 500:
        return response
    compressed = _gzip.compress(data, compresslevel=6)
    if len(compressed) >= len(data):
        return response
    response.set_data(compressed)
    response.headers['Content-Encoding'] = 'gzip'
    response.headers['Vary'] = 'Accept-Encoding'
    return response


# ── views ─────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/robots.txt')
def robots_txt():
    content = (
        'User-agent: *\n'
        'Allow: /\n'
        f'Sitemap: {request.url_root}sitemap.xml\n'
    )
    return Response(content, mimetype='text/plain')


@app.route('/manifest.json')
def manifest_json():
    return send_from_directory('static', 'manifest.json')


@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js')


@app.route('/sitemap.xml')
def sitemap_xml():
    today = datetime.now(_TZ).date().isoformat()
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        '  <url>\n'
        f'    <loc>{request.url_root}</loc>\n'
        f'    <lastmod>{today}</lastmod>\n'
        '    <changefreq>daily</changefreq>\n'
        '    <priority>1.0</priority>\n'
        '  </url>\n'
        '</urlset>'
    )
    return Response(xml, mimetype='application/xml')


# ── API: stats ────────────────────────────────────────────────────────────────

@app.route('/api/stats')
def api_stats():
    db = SessionLocal()
    try:
        return jsonify({
            'stocks':    db.query(Stock).count(),
            'prices':    db.query(DailyPrice).count(),
            'revenues':  db.query(MonthlyRevenue).count(),
            'quarterly': db.query(QuarterlyFinancial).count(),
            'last_price_date': str(
                db.execute(text('SELECT MAX(date) FROM daily_prices')).scalar() or ''
            ),
        })
    finally:
        db.close()


@app.route('/api/updates/today')
def api_updates_today():
    db = SessionLocal()
    try:
        today_str = datetime.now(_TZ).date().isoformat()

        # Latest successful daily_price log today → extract trade date from message
        price_log = (
            db.query(CrawlerLog)
            .filter(CrawlerLog.task == 'daily_price', CrawlerLog.status == 'success')
            .filter(text("date(created_at) = :today")).params(today=today_str)
            .order_by(desc(CrawlerLog.created_at))
            .first()
        )
        price_date = None
        if price_log and price_log.message:
            m = re.match(r'^(\d{4})(\d{2})(\d{2})', price_log.message)
            if m:
                price_date = f'{m.group(1)}-{m.group(2)}-{m.group(3)}'

        revenue_rows = (
            db.query(MonthlyRevenue.stock_code, Stock.name)
            .join(Stock, Stock.code == MonthlyRevenue.stock_code)
            .filter(text("date(monthly_revenue.updated_at) = :today")).params(today=today_str)
            .all()
        )

        quarterly_rows = (
            db.query(QuarterlyFinancial.stock_code, Stock.name)
            .join(Stock, Stock.code == QuarterlyFinancial.stock_code)
            .filter(text("date(quarterly_financials.updated_at) = :today")).params(today=today_str)
            .all()
        )

        ann_count = (
            db.query(Announcement)
            .filter(text("date(created_at) = :today")).params(today=today_str)
            .count()
        )

        return jsonify({
            'price_date': price_date,
            'monthly_revenue': [{'code': c, 'name': n} for c, n in revenue_rows],
            'quarterly':       [{'code': c, 'name': n} for c, n in quarterly_rows],
            'ann_count': ann_count,
        })
    finally:
        db.close()


# ── Shared summary SQL (col order: 0=code,1=name,2=market,3=industry,
#    4=close,5=change_pct,6=price_date,7=revenue,8=revenue_yoy,
#    9=rev_year,10=rev_month,11=eps,12=eps_year,13=eps_quarter,
#    14=qf_revenue,15=pe_ratio,16=start_price)
_SUMMARY_SQL = '''
    WITH lp AS (
        SELECT stock_code, MAX(date) AS max_date
        FROM daily_prices GROUP BY stock_code
    ),
    lr AS (
        SELECT stock_code, MAX(year * 100 + month) AS max_ym
        FROM monthly_revenue GROUP BY stock_code
    ),
    lq AS (
        SELECT stock_code, MAX(year * 10 + quarter) AS max_yq
        FROM quarterly_financials GROUP BY stock_code
    ),
    yeps AS (
        SELECT stock_code, year, SUM(eps) AS year_eps
        FROM quarterly_financials
        WHERE eps IS NOT NULL
        GROUP BY stock_code, year
    )
    SELECT
        s.code, s.name, s.market, s.industry,
        dp.close, dp.change_pct, dp.date,
        mr.revenue, mr.revenue_yoy, mr.year, mr.month,
        qf.eps, qf.year, qf.quarter, qf.revenue,
        CASE
            WHEN dp.close IS NOT NULL AND qf.quarter = 4 AND ye.year_eps > 0
                THEN ROUND(dp.close / ye.year_eps, 1)
            WHEN dp.close IS NOT NULL AND qf.eps > 0 AND qf.quarter BETWEEN 1 AND 3
                THEN ROUND(dp.close / (qf.eps / qf.quarter * 4.0), 1)
            ELSE NULL
        END AS pe_ratio,
        mr.start_price
    FROM stocks s
    LEFT JOIN lp ON s.code = lp.stock_code
    LEFT JOIN daily_prices dp
        ON dp.stock_code = lp.stock_code AND dp.date = lp.max_date
    LEFT JOIN lr ON s.code = lr.stock_code
    LEFT JOIN monthly_revenue mr
        ON mr.stock_code = lr.stock_code
        AND (mr.year * 100 + mr.month) = lr.max_ym
    LEFT JOIN lq ON s.code = lq.stock_code
    LEFT JOIN quarterly_financials qf
        ON qf.stock_code = lq.stock_code
        AND (qf.year * 10 + qf.quarter) = lq.max_yq
    LEFT JOIN yeps ye
        ON ye.stock_code = qf.stock_code AND ye.year = qf.year
    ORDER BY CAST(s.code AS INTEGER)
'''


def _row_to_dict(r):
    return {
        'code':        r[0],
        'name':        r[1],
        'market':      r[2],
        'industry':    r[3],
        'close':       r[4],
        'change_pct':  r[5],
        'price_date':  str(r[6]) if r[6] else None,
        'revenue':     r[7],
        'revenue_yoy': r[8],
        'rev_year':    r[9],
        'rev_month':   r[10],
        'eps':         r[11],
        'eps_year':    r[12],
        'eps_quarter': r[13],
        'qf_revenue':   r[14],
        'pe_ratio':     r[15],
        'start_price':  r[16],
        'price_diff':   round((r[4] - r[16]) / r[16] * 100, 2) if r[4] is not None and r[16] and r[16] > 0 else None,
    }


# ── API: market summary ───────────────────────────────────────────────────────

@app.route('/api/market/summary')
def api_market_summary():
    now = _time.time()
    if _summary_cache['body'] and (now - _summary_cache['ts']) < _SUMMARY_CACHE_TTL:
        return Response(_summary_cache['body'], mimetype='application/json')
    db = SessionLocal()
    try:
        rows = db.execute(text(_SUMMARY_SQL)).fetchall()
        body = _json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)
        _summary_cache['body'] = body
        _summary_cache['ts'] = now
        return Response(body, mimetype='application/json')
    finally:
        db.close()


@app.route('/api/market/summary.csv')
def api_market_summary_csv():
    db = SessionLocal()
    try:
        rows = db.execute(text(_SUMMARY_SQL)).fetchall()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['股票代號', '名稱', '市場', '產業',
                    '起始股價', '收盤價', '價差%', '漲跌幅%',
                    '月營收(千元)', '月營收年增%', '月營收期別',
                    '季營收(千元)',
                    '最新EPS', 'EPS期別',
                    '資料日期', '本益比'])
        for r in rows:
            eps_period = f"{r[12]}Q{r[13]}" if r[12] else ''
            rev_period = f"{r[9]}/{r[10]:02d}" if r[9] else ''
            w.writerow([
                r[0], r[1], r[2], r[3],
                r[16] if r[16] is not None else '',
                r[4] if r[4] is not None else '',
                round((r[4] - r[16]) / r[16] * 100, 2) if r[4] is not None and r[16] and r[16] > 0 else '',
                r[5] if r[5] is not None else '',
                r[7] if r[7] is not None else '',
                r[8] if r[8] is not None else '',
                rev_period,
                r[14] if r[14] is not None else '',
                r[11] if r[11] is not None else '',
                eps_period,
                str(r[6]) if r[6] else '',
                r[15] if r[15] is not None else '',
            ])
        # utf-8-sig adds BOM so Excel opens Chinese correctly
        csv_bytes = buf.getvalue().encode('utf-8-sig')
        return Response(
            csv_bytes,
            mimetype='text/csv; charset=utf-8',
            headers={'Content-Disposition': 'attachment; filename=taiwan_stocks.csv'},
        )
    finally:
        db.close()


# ── API: individual stock ─────────────────────────────────────────────────────

@app.route('/api/stocks/<code>')
def api_stock_info(code):
    db = SessionLocal()
    try:
        s = db.query(Stock).filter_by(code=code).first()
        if not s:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({
            'code': s.code, 'name': s.name,
            'market': s.market, 'industry': s.industry,
        })
    finally:
        db.close()


@app.route('/api/stocks/<code>/prices')
def api_prices(code):
    db = SessionLocal()
    try:
        days   = int(request.args.get('days', 90))
        cutoff = datetime.now(_TZ).date() - timedelta(days=days)
        prices = (
            db.query(DailyPrice)
            .filter(DailyPrice.stock_code == code, DailyPrice.date >= cutoff)
            .order_by(DailyPrice.date)
            .all()
        )
        return jsonify([{
            'date':       str(p.date),
            'open':       p.open,
            'high':       p.high,
            'low':        p.low,
            'close':      p.close,
            'volume':     p.volume,
            'change':     p.change,
            'change_pct': p.change_pct,
        } for p in prices])
    finally:
        db.close()


@app.route('/api/stocks/<code>/revenue')
def api_revenue(code):
    db = SessionLocal()
    try:
        rows = (
            db.query(MonthlyRevenue)
            .filter_by(stock_code=code)
            .order_by(desc(MonthlyRevenue.year), desc(MonthlyRevenue.month))
            .all()
        )
        return jsonify([{
            'year':        r.year,
            'month':       r.month,
            'revenue':     r.revenue,
            'revenue_yoy': r.revenue_yoy,
            'revenue_mom': r.revenue_mom,
        } for r in rows])
    finally:
        db.close()


@app.route('/api/stocks/<code>/financials')
def api_financials(code):
    db = SessionLocal()
    try:
        rows = (
            db.query(QuarterlyFinancial)
            .filter_by(stock_code=code)
            .order_by(desc(QuarterlyFinancial.year), desc(QuarterlyFinancial.quarter))
            .all()
        )
        return jsonify([{
            'year':             f.year,
            'quarter':          f.quarter,
            'revenue':          f.revenue,
            'operating_income': f.operating_income,
            'net_income':       f.net_income,
            'eps':              f.eps,
        } for f in rows])
    finally:
        db.close()


# ── API: debug ───────────────────────────────────────────────────────────────

@app.route('/api/test/ai')
def api_test_ai():
    """Debug: verify OpenRouter API key and connectivity."""
    import os, requests as _req
    api_key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    if not api_key:
        return jsonify({'ok': False, 'error': 'OPENROUTER_API_KEY not set'}), 500
    model = os.environ.get('OPENROUTER_MODEL', 'google/gemini-3.1-flash-lite-preview')
    try:
        resp = _req.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': model, 'messages': [{'role': 'user', 'content': '回覆 OK'}]},
            timeout=30,
        )
        resp.raise_for_status()
        reply = resp.json()['choices'][0]['message']['content']
        return jsonify({'ok': True, 'model': model, 'reply': reply})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/test/crawl')
def api_test_crawl():
    """Debug: test MOPS crawl for a given date (default: last business day)."""
    from datetime import timedelta
    import re
    from bs4 import BeautifulSoup
    date_str = request.args.get('date')
    if date_str:
        from datetime import datetime as _dt
        dt = _dt.strptime(date_str, '%Y%m%d').date()
    else:
        dt = datetime.now(_TZ).date() - timedelta(days=1)
        while dt.weekday() >= 5:
            dt -= timedelta(days=1)
    roc_year  = str(dt.year - 1911)
    month_str = f'{dt.month:02d}'
    day_str   = f'{dt.day:02d}'
    try:
        crawler._get(f'{crawler._ANN_BASE}/t05sr01_1', timeout=20)
        resp = crawler._post_form(
            f'{crawler._ANN_BASE}/ajax_t05st02',
            data={'firstin': 'true', 'off': '1', 'step': '1', 'step00': '0',
                  'TYPEK': 'all', 'year': roc_year, 'month': month_str, 'day': day_str},
            timeout=30,
        )
        resp.encoding = 'utf-8'
        soup = BeautifulSoup(resp.text, 'lxml')
        onclick_re = re.compile(
            r'\.TYPEK\.value="([^"]+)".*?\.i\.value="([^"]+)".*?\.co_id\.value="([^"]+)"',
            re.DOTALL)
        links = [m.groups() for tag in soup.find_all(onclick=True)
                 for m in [onclick_re.search(tag['onclick'])] if m]
        # Collect sample onclick strings for debugging
        all_onclicks = [(tag['onclick'][:150]) for tag in soup.find_all(onclick=True)][:5]
        html_snippet = resp.text[:800]
        return jsonify({
            'date': str(dt),
            'total_links': len(links),
            'total_onclick_tags': len(all_onclicks),
            'sample_onclicks': all_onclicks,
            'html_preview': html_snippet,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ── API: announcements ───────────────────────────────────────────────────────

@app.route('/api/announcements/today')
def api_announcements_today():
    db = SessionLocal()
    try:
        from datetime import timedelta
        since = datetime.now(_TZ).date() - timedelta(days=7)
        rows = (
            db.query(Announcement, Stock.name)
            .outerjoin(Stock, Stock.code == Announcement.stock_code)
            .filter(Announcement.announce_date >= since)
            .order_by(desc(Announcement.announce_date), desc(Announcement.announce_time))
            .all()
        )
        return jsonify([{
            'stock_code':   a.stock_code,
            'name':         name or '',
            'announce_date': str(a.announce_date),
            'announce_time': a.announce_time or '',
            'subject':      a.subject or '',
            'content':      a.content or '',
            'ai_rating':    a.ai_rating or '',
            'ai_analysis':  a.ai_analysis or '',
            'monthly_eps':  a.monthly_eps,
            'eps_yoy':      a.eps_yoy,
            'estimated_pe': a.estimated_pe,
            'quarterly_eps':     a.quarterly_eps,
            'quarterly_eps_yoy': a.quarterly_eps_yoy,
            'turnaround':        bool(a.turnaround),
        } for a, name in rows])
    finally:
        db.close()


@app.route('/api/announcements/<code>')
def api_announcements_stock(code):
    db = SessionLocal()
    try:
        rows = (
            db.query(Announcement)
            .filter(Announcement.stock_code == code)
            .order_by(desc(Announcement.announce_date), desc(Announcement.announce_time))
            .limit(20)
            .all()
        )
        return jsonify([{
            'announce_date': str(a.announce_date),
            'announce_time': a.announce_time or '',
            'subject':      a.subject or '',
            'ai_rating':    a.ai_rating or '',
            'ai_analysis':  a.ai_analysis or '',
            'monthly_eps':  a.monthly_eps,
            'eps_yoy':      a.eps_yoy,
            'estimated_pe': a.estimated_pe,
            'quarterly_eps':     a.quarterly_eps,
            'quarterly_eps_yoy': a.quarterly_eps_yoy,
            'turnaround':        bool(a.turnaround),
        } for a in rows])
    finally:
        db.close()


# ── API: crawler ──────────────────────────────────────────────────────────────

@app.route('/api/crawler/status')
def api_crawler_status():
    db = SessionLocal()
    try:
        logs = (
            db.query(CrawlerLog)
            .order_by(desc(CrawlerLog.created_at))
            .limit(30)
            .all()
        )
        return jsonify([{
            'task':       l.task,
            'status':     l.status,
            'message':    l.message,
            'created_at': str(l.created_at),
        } for l in logs])
    finally:
        db.close()


@app.route('/api/crawler/run/<task>', methods=['POST'])
def api_run_crawler(task):
    is_local = request.remote_addr in ('127.0.0.1', '::1')
    is_admin = session.get('username') == ADMIN_USERNAME
    if not (is_local or is_admin):
        return jsonify({'error': 'forbidden'}), 403
    today = datetime.now(_TZ)
    date_str = today.strftime('%Y%m%d')

    if task == 'stock_list':
        _run_bg(crawler.crawl_stock_list)

    elif task == 'daily_price':
        _run_bg(crawler.crawl_daily_prices, date_str)

    elif task == 'monthly_revenue':
        # Previous month (companies publish by the 10th of the current month)
        m = today.month - 1 or 12
        y = today.year if today.month > 1 else today.year - 1
        _run_bg(crawler.crawl_monthly_revenue, y, m)

    elif task == 'quarterly':
        # Determine the most recently DISCLOSED quarter based on publication deadlines:
        # Q1 (Jan-Mar): disclosed by May 15
        # Q2 (Apr-Jun): disclosed by Aug 14
        # Q3 (Jul-Sep): disclosed by Nov 14
        # Q4 (Oct-Dec): disclosed by Mar 31 of the following year
        m = today.month
        if m >= 11:             # Nov 14+ → Q3 of current year
            y, q = today.year, 3
        elif m >= 8:            # Aug 14+ → Q2 of current year
            y, q = today.year, 2
        elif m >= 5:            # May 15+ → Q1 of current year
            y, q = today.year, 1
        else:                   # Jan–Apr → Q4 of previous year (deadline Mar 31)
            y, q = today.year - 1, 4
        # Allow override via ?year=YYYY&quarter=Q
        y = int(request.args.get('year',  y))
        q = int(request.args.get('quarter', q))
        _run_bg(crawler.crawl_quarterly_financials, y, q)

    elif task == 'announcements':
        ann_date = request.args.get('date')  # optional YYYYMMDD override
        _run_bg(crawler.crawl_announcements, ann_date)

    elif task == 'init':
        _run_bg(_initial_crawl)
    else:
        return jsonify({'error': 'Unknown task'}), 400

    return jsonify({'status': 'started', 'task': task,
                    'detail': f'quarterly {locals().get("y","")}/Q{locals().get("q","")}' if task == 'quarterly' else ''})


# ── auth ─────────────────────────────────────────────────────────────────────

@app.route('/api/auth/me')
def api_auth_me():
    if 'user_id' not in session:
        return jsonify({'user': None})
    return jsonify({'user': {
        'id': session['user_id'],
        'username': session['username'],
        'is_admin': session['username'] == ADMIN_USERNAME,
    }})


@app.route('/api/auth/register', methods=['POST'])
def api_auth_register():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    if not username or not password:
        return jsonify({'error': '帳號與密碼不得為空'}), 400
    if len(username) < 2:
        return jsonify({'error': '帳號至少 2 個字元'}), 400
    if len(password) < 6:
        return jsonify({'error': '密碼至少 6 個字元'}), 400
    db = SessionLocal()
    try:
        if db.query(User).filter_by(username=username).first():
            return jsonify({'error': '此帳號已被使用'}), 409
        user = User(username=username, password_hash=generate_password_hash(password))
        db.add(user)
        db.commit()
        session['user_id'] = user.id
        session['username'] = user.username
        return jsonify({'ok': True, 'username': user.username})
    finally:
        db.close()


@app.route('/api/auth/login', methods=['POST'])
def api_auth_login():
    data = request.json or {}
    username = data.get('username', '').strip()
    password = data.get('password', '')
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(username=username).first()
        if not user or not check_password_hash(user.password_hash, password):
            return jsonify({'error': '帳號或密碼錯誤'}), 401
        session['user_id'] = user.id
        session['username'] = user.username
        return jsonify({'ok': True, 'username': user.username})
    finally:
        db.close()


@app.route('/api/auth/logout', methods=['POST'])
def api_auth_logout():
    session.clear()
    return jsonify({'ok': True})


# ── message board ─────────────────────────────────────────────────────────────

def _msg_to_dict(m):
    is_owner = 'user_id' in session and m.user_id == session['user_id']
    is_admin = session.get('username') == ADMIN_USERNAME
    return {
        'id': m.id, 'username': m.username, 'content': m.content,
        'created_at': m.created_at.strftime('%Y-%m-%d %H:%M'),
        'can_delete': is_owner or is_admin,
    }


@app.route('/api/messages')
def api_messages_get():
    db = SessionLocal()
    try:
        msgs = db.query(Message).order_by(Message.id.desc()).limit(100).all()
        return jsonify([_msg_to_dict(m) for m in reversed(msgs)])
    finally:
        db.close()


@app.route('/api/messages', methods=['POST'])
def api_messages_post():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    content = (request.json or {}).get('content', '').strip()
    if not content:
        return jsonify({'error': '留言不得為空'}), 400
    if len(content) > 500:
        return jsonify({'error': '留言過長（上限 500 字）'}), 400
    db = SessionLocal()
    try:
        msg = Message(user_id=session['user_id'], username=session['username'], content=content)
        db.add(msg)
        db.commit()
        return jsonify(_msg_to_dict(msg))
    finally:
        db.close()


@app.route('/api/messages/<int:msg_id>', methods=['DELETE'])
def api_messages_delete(msg_id):
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    db = SessionLocal()
    try:
        msg = db.query(Message).filter_by(id=msg_id).first()
        if not msg:
            return jsonify({'error': 'not found'}), 404
        is_owner = msg.user_id == session['user_id']
        is_admin = session.get('username') == ADMIN_USERNAME
        if not (is_owner or is_admin):
            return jsonify({'error': 'unauthorized'}), 403
        db.delete(msg)
        db.commit()
        return jsonify({'ok': True})
    finally:
        db.close()


# ── user watchlists ───────────────────────────────────────────────────────────

def _wl_rows(db, wl_id):
    return [s.stock_code for s in db.query(WatchlistStock).filter_by(watchlist_id=wl_id).all()]


@app.route('/api/watchlists')
def api_watchlists_get():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    db = SessionLocal()
    try:
        wls = db.query(Watchlist).filter_by(user_id=session['user_id']).order_by(Watchlist.id).all()
        return jsonify([{'id': w.id, 'name': w.name, 'codes': _wl_rows(db, w.id)} for w in wls])
    finally:
        db.close()


@app.route('/api/watchlists', methods=['POST'])
def api_watchlists_create():
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    name = (request.json or {}).get('name', '').strip()
    if not name:
        return jsonify({'error': '名稱不得為空'}), 400
    db = SessionLocal()
    try:
        wl = Watchlist(user_id=session['user_id'], name=name)
        db.add(wl)
        db.commit()
        return jsonify({'id': wl.id, 'name': wl.name, 'codes': []})
    finally:
        db.close()


@app.route('/api/watchlists/<int:wl_id>', methods=['PUT'])
def api_watchlists_rename(wl_id):
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    name = (request.json or {}).get('name', '').strip()
    db = SessionLocal()
    try:
        wl = db.query(Watchlist).filter_by(id=wl_id, user_id=session['user_id']).first()
        if not wl:
            return jsonify({'error': 'not found'}), 404
        wl.name = name
        db.commit()
        return jsonify({'ok': True})
    finally:
        db.close()


@app.route('/api/watchlists/<int:wl_id>', methods=['DELETE'])
def api_watchlists_delete(wl_id):
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    db = SessionLocal()
    try:
        wl = db.query(Watchlist).filter_by(id=wl_id, user_id=session['user_id']).first()
        if not wl:
            return jsonify({'error': 'not found'}), 404
        db.query(WatchlistStock).filter_by(watchlist_id=wl_id).delete()
        db.delete(wl)
        db.commit()
        return jsonify({'ok': True})
    finally:
        db.close()


@app.route('/api/watchlists/<int:wl_id>/stocks', methods=['POST'])
def api_wl_add_stock(wl_id):
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    code = (request.json or {}).get('code', '').strip()
    db = SessionLocal()
    try:
        if not db.query(Watchlist).filter_by(id=wl_id, user_id=session['user_id']).first():
            return jsonify({'error': 'not found'}), 404
        if not db.query(WatchlistStock).filter_by(watchlist_id=wl_id, stock_code=code).first():
            db.add(WatchlistStock(watchlist_id=wl_id, stock_code=code))
            db.commit()
        return jsonify({'ok': True})
    finally:
        db.close()


@app.route('/api/watchlists/<int:wl_id>/stocks/<code>', methods=['DELETE'])
def api_wl_remove_stock(wl_id, code):
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    db = SessionLocal()
    try:
        if not db.query(Watchlist).filter_by(id=wl_id, user_id=session['user_id']).first():
            return jsonify({'error': 'not found'}), 404
        db.query(WatchlistStock).filter_by(watchlist_id=wl_id, stock_code=code).delete()
        db.commit()
        return jsonify({'ok': True})
    finally:
        db.close()


# ── admin ──────────────────────────────────────────────────────────────────────

@app.route('/api/admin/users')
def api_admin_users():
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.id).all()
        wl_counts = dict(
            db.query(Watchlist.user_id, sa_func.count(Watchlist.id))
            .group_by(Watchlist.user_id).all()
        )
        return jsonify({
            'total': len(users),
            'users': [{
                'id': u.id,
                'username': u.username,
                'created_at': u.created_at.strftime('%Y-%m-%d %H:%M'),
                'watchlist_count': wl_counts.get(u.id, 0),
            } for u in users],
        })
    finally:
        db.close()


@app.route('/api/admin/users/<int:user_id>', methods=['DELETE'])
def api_admin_delete_user(user_id):
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    db = SessionLocal()
    try:
        user = db.query(User).filter_by(id=user_id).first()
        if not user:
            return jsonify({'error': 'not found'}), 404
        if user.username == ADMIN_USERNAME:
            return jsonify({'error': '無法刪除管理員帳號'}), 400
        wl_ids = [w.id for w in db.query(Watchlist).filter_by(user_id=user_id).all()]
        if wl_ids:
            db.query(WatchlistStock).filter(WatchlistStock.watchlist_id.in_(wl_ids)).delete(synchronize_session=False)
            db.query(Watchlist).filter_by(user_id=user_id).delete()
        db.query(Message).filter_by(user_id=user_id).delete()
        db.delete(user)
        db.commit()
        return jsonify({'ok': True})
    finally:
        db.close()


# ── startup (runs under both `python app.py` and gunicorn) ───────────────────

init_db()
sched.start()

_db = SessionLocal()
_needs_init = _db.query(Stock).count() == 0
_db.close()
if _needs_init:
    logger.info('Empty database detected — starting initial crawl in background')
    _run_bg(_initial_crawl)
else:
    # Catch-up: APScheduler computes "next fire time" at sched.start(). If the
    # worker restarts (e.g. redeploy) after today's 14:00/15:00 cron time,
    # today's daily_price run is skipped entirely (not deferred). Detect this
    # and trigger it once at startup.
    _now_tw = datetime.now(_TZ)
    if _now_tw.weekday() < 5 and _now_tw.hour >= 14:
        _today_str = _now_tw.strftime('%Y%m%d')
        _db = SessionLocal()
        try:
            _done = _db.query(CrawlerLog).filter(
                CrawlerLog.task == 'daily_price',
                CrawlerLog.status == 'success',
                CrawlerLog.message.like(f'{_today_str}:%'),
            ).first()
        finally:
            _db.close()
        if not _done:
            logger.info('Daily price not yet run today (%s) — triggering catch-up crawl', _today_str)
            _run_bg(crawler.crawl_daily_prices, _today_str)


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)
