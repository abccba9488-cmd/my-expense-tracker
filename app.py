import gzip as _gzip
import json as _json
import logging
import os
import re
import threading
import time as _time
from datetime import date, timedelta, datetime
from zoneinfo import ZoneInfo

import csv
import io
from flask import Flask, jsonify, render_template, request, Response, session, send_from_directory, redirect
from sqlalchemy import desc, text, func as sa_func
from werkzeug.security import generate_password_hash, check_password_hash

import backtest_sweet_spot
import chip_peak
import crawler
import experts
import portfolio_risk
import scheduler as sched
from database import (
    SessionLocal, Stock, DailyPrice, MonthlyRevenue,
    QuarterlyFinancial, CrawlerLog, User, Watchlist, WatchlistStock, Message,
    Announcement, StockAiAnalysis, StockNote, ExpertScore, BrokerTrade, InstitutionalTrade,
    VisitLog, init_db
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


def _client_ip():
    # Behind ngrok the TCP peer is always the local ngrok agent (127.0.0.1) —
    # the real visitor IP only shows up in X-Forwarded-For.
    xff = request.headers.get('X-Forwarded-For')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr


@app.before_request
def _log_visit():
    """Log homepage loads for the admin's visitor dashboard (see
    /api/admin/visits). Deliberately scoped to GET "/" only — that's the
    SPA's single entry point, so one row per real visit rather than one per
    API/asset request. Never let a logging failure break the actual page."""
    if request.method != 'GET' or request.path != '/':
        return
    db = SessionLocal()
    try:
        db.add(VisitLog(
            ip=_client_ip(),
            path=request.path,
            referrer=(request.headers.get('Referer') or '')[:500],
            user_agent=(request.headers.get('User-Agent') or '')[:255],
            username=session.get('username'),
        ))
        db.commit()
    except Exception:
        logger.exception('Failed to log visit')
    finally:
        db.close()

# ── summary cache ─────────────────────────────────────────────────────────────

_SUMMARY_CACHE_TTL = 1800  # seconds — MA lines are smoothed indicators, staleness
                            # is a non-issue; longer TTL mainly to reduce how often
                            # anyone pays for the expensive rebuild (see lock below)
_summary_cache: dict = {'body': None, 'ts': 0.0}
# daily_prices has grown to millions of rows — a cold rebuild of the market
# summary (ma20/60/120/240 per stock) can take well over a minute. Without
# this lock, every concurrent request that lands during a cache miss would
# each kick off its own copy of that expensive query (cache stampede),
# compounding the slowdown instead of just paying it once.
_summary_rebuild_lock = threading.Lock()


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

@app.context_processor
def inject_asset_version():
    """Cache-busting query param for static/js/app.js & static/css/style.css,
    derived from each file's mtime. Without this, the service worker's
    cache-first strategy can serve a stale cached JS/CSS alongside the
    freshly-fetched index.html right after a deploy (column-count mismatch,
    DataTables "unknown parameter" warnings etc.) until the browser happens
    to reload again. A versioned URL makes that mismatch impossible — the
    new HTML always points at a URL the old cache has never seen, so it's
    always a fresh network fetch, no manual CACHE_NAME bump needed."""
    def asset_version(filename):
        path = os.path.join(app.static_folder, filename)
        try:
            return int(os.path.getmtime(path))
        except OSError:
            return 0
    return dict(asset_version=asset_version)


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/admin')
def admin_page():
    if not _is_admin():
        return redirect('/')
    return render_template('admin.html')


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

        def _last_checked(task):
            """Most recent crawler_logs entry for this task, regardless of
            status/date — lets the UI show "last checked" when there's
            nothing new today instead of looking like it never ran."""
            log = (
                db.query(CrawlerLog)
                .filter(CrawlerLog.task == task)
                .order_by(desc(CrawlerLog.created_at))
                .first()
            )
            return str(log.created_at) if log else None

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
            'price_last_checked':     _last_checked('daily_price'),
            'monthly_revenue': [{'code': c, 'name': n} for c, n in revenue_rows],
            'revenue_last_checked':   _last_checked('monthly_revenue'),
            'quarterly':       [{'code': c, 'name': n} for c, n in quarterly_rows],
            'quarterly_last_checked': _last_checked('quarterly'),
            'ann_count':              ann_count,
            'ann_last_checked':       _last_checked('announcements'),
        })
    finally:
        db.close()


# ── Shared summary SQL (col order: 0=code,1=name,2=market,3=industry,
#    4=close,5=change_pct,6=price_date,7=revenue,8=revenue_yoy,
#    9=rev_year,10=rev_month,11=eps,12=eps_year,13=eps_quarter,
#    14=qf_revenue,15=pe_ratio,16=start_price,17=ma20,18=turnaround_signal,
#    19=ma60,20=ma120,21=ma240,22=dividend_yield)
_SUMMARY_SQL = '''
    WITH lp AS (
        SELECT stock_code, MAX(date) AS max_date
        FROM daily_prices GROUP BY stock_code
    ),
    ldy AS (
        SELECT stock_code, MAX(date) AS max_date
        FROM daily_prices WHERE dividend_yield IS NOT NULL GROUP BY stock_code
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
        mr.start_price,
        (
            SELECT AVG(close) FROM (
                SELECT close FROM daily_prices d20
                WHERE d20.stock_code = s.code
                ORDER BY d20.date DESC LIMIT 20
            )
        ) AS ma20,
        mr.turnaround_signal,
        (
            SELECT AVG(close) FROM (
                SELECT close FROM daily_prices d60
                WHERE d60.stock_code = s.code
                ORDER BY d60.date DESC LIMIT 60
            )
        ) AS ma60,
        (
            SELECT AVG(close) FROM (
                SELECT close FROM daily_prices d120
                WHERE d120.stock_code = s.code
                ORDER BY d120.date DESC LIMIT 120
            )
        ) AS ma120,
        (
            SELECT AVG(close) FROM (
                SELECT close FROM daily_prices d240
                WHERE d240.stock_code = s.code
                ORDER BY d240.date DESC LIMIT 240
            )
        ) AS ma240,
        dy.dividend_yield
    FROM stocks s
    LEFT JOIN lp ON s.code = lp.stock_code
    LEFT JOIN daily_prices dp
        ON dp.stock_code = lp.stock_code AND dp.date = lp.max_date
    LEFT JOIN ldy ON s.code = ldy.stock_code
    LEFT JOIN daily_prices dy
        ON dy.stock_code = ldy.stock_code AND dy.date = ldy.max_date
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
        'ma20':         round(r[17], 2) if r[17] is not None else None,
        'turnaround_signal': bool(r[18]) if r[18] is not None else False,
        'ma60':         round(r[19], 2) if r[19] is not None else None,
        'ma120':        round(r[20], 2) if r[20] is not None else None,
        'ma240':        round(r[21], 2) if r[21] is not None else None,
        'dividend_yield': round(r[22], 2) if r[22] is not None else None,
    }


# ── API: market summary ───────────────────────────────────────────────────────

@app.route('/api/market/summary')
def api_market_summary():
    now = _time.time()
    if _summary_cache['body'] and (now - _summary_cache['ts']) < _SUMMARY_CACHE_TTL:
        return Response(_summary_cache['body'], mimetype='application/json')
    with _summary_rebuild_lock:
        # Another thread may have just rebuilt it while we were waiting on the lock.
        now = _time.time()
        if _summary_cache['body'] and (now - _summary_cache['ts']) < _SUMMARY_CACHE_TTL:
            return Response(_summary_cache['body'], mimetype='application/json')
        db = SessionLocal()
        try:
            rows = db.execute(text(_SUMMARY_SQL)).fetchall()
            body = _json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)
            _summary_cache['body'] = body
            _summary_cache['ts'] = _time.time()
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
                    '資料日期', '本益比', '20日均', '虧轉盈訊號', '60日均', '120日均', '240日均'])
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
                round(r[17], 2) if r[17] is not None else '',
                '是' if r[18] else '',
                round(r[19], 2) if r[19] is not None else '',
                round(r[20], 2) if r[20] is not None else '',
                round(r[21], 2) if r[21] is not None else '',
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


@app.route('/api/stocks/<code>/chip-peak')
def api_chip_peak(code):
    """籌碼峰：純運算，不落地存表（見 CLAUDE.md「籌碼峰」章節）。取
    daily_prices 全部歷史（chip_peak.compute_chip_peak 自己按 lookback 取
    窗），並用同一股票的 institutional_trades 算出每日「法人淨買超 / 當日
    成交量」比例，交給 compute_chip_peak 做 Phase 2 品質加權（該股沒有法人
    資料的日子一律視為中性權重，不處理成缺值懲罰）。資料不足 30 筆時回傳
    空物件。額外附上 poc_history（compute_chip_peak_series，2026-08-18
    新增）供前端畫出 POC 隨時間移動的軌跡線，不只是目前這一個數字。"""
    db = SessionLocal()
    try:
        lookback   = int(request.args.get('lookback', 120))
        half_life  = int(request.args.get('half_life', 30))
        rows = (
            db.query(DailyPrice)
            .filter(DailyPrice.stock_code == code)
            .order_by(DailyPrice.date)
            .all()
        )
        inst_rows = (
            db.query(InstitutionalTrade)
            .filter(InstitutionalTrade.stock_code == code)
            .all()
        )
        inst_net_by_date = {
            r.date: (r.foreign_buy or 0) - (r.foreign_sell or 0)
                  + (r.trust_buy or 0) - (r.trust_sell or 0)
                  + (r.dealer_buy or 0) - (r.dealer_sell or 0)
            for r in inst_rows
        }
        row_dicts = []
        for r in rows:
            d = {'date': r.date, 'close': r.close, 'volume': r.volume}
            inst_net = inst_net_by_date.get(r.date)
            if inst_net is not None and r.volume:
                d['inst_net_ratio'] = inst_net / r.volume
            row_dicts.append(d)
        result = chip_peak.compute_chip_peak(row_dicts, lookback=lookback, half_life=half_life)
        if result:
            result['poc_history'] = chip_peak.compute_chip_peak_series(
                row_dicts, lookback=lookback, half_life=half_life)
        return jsonify(result)
    finally:
        db.close()


@app.route('/api/stocks/<code>/institutional-trades')
def api_institutional_trades(code):
    """三大法人（外資/投信/自營商）每日買賣超，近 N 天（預設90）。資料已由
    排程每天 17:00 抓好（見 CLAUDE.md「達人選股（FinMind）」），這裡純讀取，
    平常不需要按需觸發爬蟲；若排程當下 FinMind 資料不完整導致缺最新一兩天，
    見下方 institutional-trades/refresh 端點手動補齊。累計持股增減交給前端算
    （同一份視窗內的買賣超逐日加總，不是絕對持股張數）。"""
    db = SessionLocal()
    try:
        days = int(request.args.get('days', 90))
        cutoff = datetime.now(_TZ).date() - timedelta(days=days)
        rows = (
            db.query(InstitutionalTrade)
            .filter(InstitutionalTrade.stock_code == code, InstitutionalTrade.date >= cutoff)
            .order_by(InstitutionalTrade.date)
            .all()
        )
        return jsonify([{
            'date':        str(r.date),
            'foreign_buy':  r.foreign_buy,
            'foreign_sell': r.foreign_sell,
            'trust_buy':    r.trust_buy,
            'trust_sell':   r.trust_sell,
            'dealer_buy':   r.dealer_buy,
            'dealer_sell':  r.dealer_sell,
        } for r in rows])
    finally:
        db.close()


@app.route('/api/stocks/<code>/institutional-trades/refresh', methods=['POST'])
def api_institutional_trades_refresh(code):
    """個股詳情頁「補齊近5日資料」按鈕：任何登入使用者可觸發。重新爬近5個
    曆日的三大法人資料——這是全市場一次性 bulk 抓取（見 crawler.py
    crawl_finmind_institutional），code 參數不影響爬取範圍，只是沿用本站
    「詳情頁按鈕觸發」的網址慣例。用途是修復已知缺口：finmind_institutional
    排程若在 FinMind 當天資料尚未補齊時執行，會回報成功但只抓到部分股票，
    watchdog 只檢查「今天有沒有成功 log」、不檢查資料完不完整，不會自動抓到
    這種情況（2026-08-15 實際發生過一次，見 CLAUDE.md）。crawl_finmind_institutional
    用 INSERT OR REPLACE，重跑安全冪等；非交易日呼叫會安全拿到 0 筆，無副作用。"""
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    try:
        today = datetime.now(_TZ).date()
        for i in range(5):
            d = today - timedelta(days=i)
            crawler.crawl_finmind_institutional(d.strftime('%Y%m%d'))
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


@app.route('/api/stocks/<code>/broker-trades')
def api_broker_trades(code):
    """券商分點單日買賣超，近 N 天（預設90）。資料來源是自選股回補或使用者
    在個股詳情頁按「查詢」觸發（見 api_broker_trades_fetch）——沒資料回傳空
    陣列，由前端決定顯示空狀態還是矩陣。日/90日累計前十五大買超/賣超排序交給
    前端算，比照 /api/market/summary 的既有慣例。"""
    db = SessionLocal()
    try:
        days = int(request.args.get('days', 90))
        cutoff = datetime.now(_TZ).date() - timedelta(days=days)
        rows = (
            db.query(BrokerTrade)
            .filter(BrokerTrade.stock_code == code, BrokerTrade.date >= cutoff)
            .order_by(BrokerTrade.date)
            .all()
        )
        return jsonify([{
            'date':        str(r.date),
            'broker_id':   r.broker_id,
            'broker_name': r.broker_name,
            'buy_volume':  r.buy_volume,
            'sell_volume': r.sell_volume,
        } for r in rows])
    finally:
        db.close()


@app.route('/api/stocks/<code>/broker-trades/fetch', methods=['POST'])
def api_broker_trades_fetch(code):
    """個股詳情頁「查詢」按鈕：任何登入使用者可主動觸發，不再限制自選股。
    同步執行（比照 AI 個股分析的作法），已有資料只補當天增量，完全沒資料
    才回補90天，邏輯與 scheduler._broker_trades_job() 一致。"""
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    db = SessionLocal()
    try:
        has_data = db.query(BrokerTrade).filter_by(stock_code=code).first() is not None
    finally:
        db.close()
    try:
        if has_data:
            today = datetime.now(_TZ).strftime('%Y%m%d')
            crawler.crawl_broker_trades(today, code)
        else:
            crawler.backfill_broker_trades(code)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'ok': True})


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


@app.route('/api/stocks/<code>/fundamentals')
def api_stock_fundamentals(code):
    db = SessionLocal()
    try:
        return jsonify(experts.get_stock_fundamentals(db, code))
    finally:
        db.close()


@app.route('/api/stocks/<code>/health')
def api_stock_health(code):
    db = SessionLocal()
    try:
        return jsonify(experts.compute_holding_health(db, code))
    finally:
        db.close()


@app.route('/api/stocks/<code>/expert-scores')
def api_stock_expert_scores(code):
    db = SessionLocal()
    try:
        rows = db.query(ExpertScore).filter_by(stock_code=code).all()
        by_key = {e.expert_key: e for e in rows}
        return jsonify([{
            'expert_key': key,
            'expert_label': label,
            'passed': bool(by_key[key].passed) if key in by_key else None,
            'score': by_key[key].score if key in by_key else None,
            'max_score': by_key[key].max_score if key in by_key else None,
            'breakdown': _json.loads(by_key[key].breakdown_json) if key in by_key and by_key[key].breakdown_json else [],
            'entered_at': str(by_key[key].entered_at) if key in by_key and by_key[key].entered_at else None,
            'transition': by_key[key].transition if key in by_key else None,
            'computed_at': str(by_key[key].computed_at) if key in by_key else None,
            'is_experimental': key in experts.EXPERIMENTAL_EXPERTS,
        } for key, label in experts.EXPERT_LABELS.items()])
    finally:
        db.close()


@app.route('/api/stocks/<code>/backtest/sweet-spot')
def api_stock_backtest_sweet_spot(code):
    """On-demand, single-stock backtest of all 4 甜蜜點 tiers（🔴20日／
    🟡60日／🟢120日 對稱±3%；🟣240日 收盤價在均線之下或高於但漲幅<=3%）
    各自搭配獲利目標出場（+10/15/20/25/30% by default）、不加碼（持有期間忽略
    新訊號，出場後才能再進場）——見 backtest_sweet_spot.py 模組 docstring
    完整方法論。純本機運算（不打外部付費 API），跟 AI 分析不同，不限管理員；
    單一股票近5年、4種天期×5種目標共20輪模擬，約數秒內完成。"""
    years = request.args.get('years', 5, type=int)
    db = SessionLocal()
    try:
        result = backtest_sweet_spot.run_backtest(db, code, years=years)
        if result is None:
            return jsonify({'error': '股價資料不足，無法回測（至少需要約60個交易日的歷史資料）'}), 400
        return jsonify(result)
    except Exception as e:
        logger.exception('Backtest (sweet spot) failed for %s', code)
        return jsonify({'error': str(e)}), 500
    finally:
        db.close()


@app.route('/api/stocks/<code>/ai-analysis')
def api_stock_ai_analysis_get(code):
    """Return the cached latest AI analysis for this stock, if any —
    never triggers a new (paid) AI call. Visible to anyone, same as the
    rest of the read-only stock data; only *triggering* a new analysis
    is admin-only (see the POST route below)."""
    db = SessionLocal()
    try:
        a = db.query(StockAiAnalysis).filter_by(stock_code=code).first()
        if not a:
            return jsonify(None)
        return jsonify({
            'stock_code':       a.stock_code,
            'ai_rating':        a.ai_rating or '',
            'ai_analysis':      a.ai_analysis or '',
            'target_cheap':     a.target_cheap,
            'target_fair':      a.target_fair,
            'target_expensive': a.target_expensive,
            'updated_at':       str(a.updated_at) if a.updated_at else None,
        })
    finally:
        db.close()


@app.route('/api/stocks/<code>/ai-analysis', methods=['POST'])
def api_stock_ai_analysis_run(code):
    """Admin-only: trigger a fresh (paid OpenRouter) AI analysis for this
    stock. Synchronous — the admin is actively waiting on this one click,
    unlike the batch crawler tasks which run in the background."""
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    try:
        result = crawler.analyze_stock_with_ai(code)
        return jsonify(result)
    except (ValueError, RuntimeError) as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        logger.exception('Stock AI analysis request failed for %s', code)
        return jsonify({'error': str(e)}), 500


@app.route('/api/stocks/<code>/note')
def api_stock_note_get(code):
    """管理員個人分析筆記（自由文字，貼上哪裡來的分析都可以，不觸發任何 AI
    呼叫，見 CLAUDE.md「個股筆記」章節）。僅管理員可讀——這是私人研究筆記，
    不是給一般訪客看的公開內容，跟上面 ai-analysis（公開可讀的付費分析快取）
    刻意分開兩張表、兩套權限。"""
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    db = SessionLocal()
    try:
        n = db.query(StockNote).filter_by(stock_code=code).first()
        return jsonify({
            'content':    n.content if n else '',
            'updated_at': str(n.updated_at) if n and n.updated_at else None,
        })
    finally:
        db.close()


@app.route('/api/stocks/<code>/note', methods=['PUT'])
def api_stock_note_save(code):
    """管理員個人分析筆記：儲存（INSERT OR UPDATE，一檔股票只留最新一份，
    不留歷史版本）。"""
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    content = (request.json or {}).get('content', '')
    db = SessionLocal()
    try:
        n = db.query(StockNote).filter_by(stock_code=code).first()
        if n:
            n.content = content
        else:
            n = StockNote(stock_code=code, content=content)
            db.add(n)
        db.commit()
        return jsonify({'ok': True, 'updated_at': str(n.updated_at) if n.updated_at else None})
    finally:
        db.close()


# ── API: announcements ───────────────────────────────────────────────────────

@app.route('/api/announcements/today')
def api_announcements_today():
    """路由名稱沿用歷史命名（原本設計只給「今日」用），實際回傳全部歷史公告
    （2026-08-17 移除原本寫死的近7天篩選）——資料庫裡從 backfill_announcements.py
    一次性回填的 2023 年資料到每日排程持續累積的資料都在，只是先前這裡的
    `since` 篩選把 7 天前的全部擋掉，不是資料庫沒有資料。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(Announcement, Stock.name)
            .outerjoin(Stock, Stock.code == Announcement.stock_code)
            .order_by(desc(Announcement.announce_date), desc(Announcement.announce_time))
            .all()
        )
        return jsonify([{
            'id':                   a.id,
            'stock_code':           a.stock_code,
            'name':                 name or '',
            'announce_date':        str(a.announce_date),
            'announce_time':        a.announce_time or '',
            'subject':              a.subject or '',
            'content':              a.content or '',
            'price_at_announce':    a.price_at_announce,
            'monthly_eps':          a.monthly_eps,
            'prior_year_eps':       a.prior_year_eps,
            'eps_yoy':              a.eps_yoy,
            'turnaround':           bool(a.turnaround),
            'estimated_annual_eps': a.estimated_annual_eps,
            'estimated_pe':         a.estimated_pe,
            'ai_rating':            a.ai_rating or '',
            'ai_analysis':          a.ai_analysis or '',
        } for a, name in rows])
    finally:
        db.close()


@app.route('/api/announcements/<int:ann_id>/rating', methods=['PUT'])
def api_announcement_set_rating(ann_id):
    """人工評級（取代原本自動呼叫 OpenRouter 的付費 AI 評級，見 CLAUDE.md「自結公告」
    章節）：前端複製一段提示詞讓使用者自行貼到 ChatGPT 等免費工具，看完回覆後在下拉選單
    手動選一個評級，呼叫這支端點寫回 ai_rating。僅限管理員（跟站上其他公開頁面的編輯型
    動作一致）。"""
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    rating = (request.json or {}).get('rating') or None
    valid = {'🔴 強烈買進', '🟠 建議買進', '🟡 一般觀望', '🟢 需要小心', None}
    if rating not in valid:
        return jsonify({'error': 'invalid rating'}), 400
    db = SessionLocal()
    try:
        a = db.query(Announcement).get(ann_id)
        if not a:
            return jsonify({'error': 'not found'}), 404
        a.ai_rating = rating
        db.commit()
        return jsonify({'ok': True, 'ai_rating': rating or ''})
    finally:
        db.close()


# ── API: 達人選股 ───────────────────────────────────────────────────────────────

@app.route('/api/experts')
def api_experts_list():
    db = SessionLocal()
    try:
        rows = (
            db.query(ExpertScore.expert_key, ExpertScore.expert_label,
                      sa_func.count().label('total'),
                      sa_func.sum(ExpertScore.passed).label('passed_count'),
                      sa_func.max(ExpertScore.computed_at).label('computed_at'))
            .group_by(ExpertScore.expert_key)
            .all()
        )
        by_key = {r.expert_key: r for r in rows}
        return jsonify([{
            'expert_key': key,
            'expert_label': label,
            'passed_count': (by_key[key].passed_count or 0) if key in by_key else 0,
            'total': by_key[key].total if key in by_key else 0,
            'computed_at': str(by_key[key].computed_at) if key in by_key else None,
            'is_experimental': key in experts.EXPERIMENTAL_EXPERTS,
        } for key, label in experts.EXPERT_LABELS.items()])
    finally:
        db.close()


@app.route('/api/experts/<key>')
def api_experts_detail(key):
    if key not in experts.EXPERT_LABELS:
        return jsonify({'error': 'Unknown expert_key'}), 404
    db = SessionLocal()
    try:
        rows = (
            db.query(ExpertScore, Stock.name, Stock.market, Stock.industry)
            .outerjoin(Stock, Stock.code == ExpertScore.stock_code)
            .filter(ExpertScore.expert_key == key)
            .order_by(desc(ExpertScore.passed), desc(ExpertScore.score))
            .all()
        )
        return jsonify([{
            'code': e.stock_code,
            'name': name or '',
            'market': market or '',
            'industry': industry or '',
            'passed': bool(e.passed),
            'score': e.score,
            'max_score': e.max_score,
            'breakdown': _json.loads(e.breakdown_json) if e.breakdown_json else [],
            'entered_at': str(e.entered_at) if e.entered_at else None,
            'transition': e.transition,
            'computed_at': str(e.computed_at),
        } for e, name, market, industry in rows])
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
        ann_limit = request.args.get('limit', type=int)  # optional, testing only
        _run_bg(crawler.crawl_announcements, ann_date, ann_limit)

    elif task == 'finmind_data':
        _run_bg(sched._finmind_job)

    elif task == 'broker_trades':
        _run_bg(sched._broker_trades_job)

    elif task == 'director_holdings':
        _run_bg(crawler.crawl_director_holdings)

    elif task == 'expert_scores':
        _run_bg(experts.compute_expert_scores)

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
            # First time anyone watchlists this stock → backfill 30 days of
            # broker-branch trades in the background instead of waiting for
            # the daily job to accumulate history one day at a time.
            if not db.query(BrokerTrade).filter_by(stock_code=code).first():
                _run_bg(crawler.backfill_broker_trades, code)
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


@app.route('/api/watchlists/<int:wl_id>/stress-test')
def api_wl_stress_test(wl_id):
    if 'user_id' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    db = SessionLocal()
    try:
        if not db.query(Watchlist).filter_by(id=wl_id, user_id=session['user_id']).first():
            return jsonify({'error': 'not found'}), 404
        codes = _wl_rows(db, wl_id)
        return jsonify(portfolio_risk.run_stress_test(db, codes))
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


# task -> Chinese label for the health dashboard; kept in sync with the
# `_log('<task>', ...)` calls scattered across crawler.py.
_HEALTH_TASKS = [
    ('stock_list',              '股票清單'),
    ('daily_price',              '每日股價'),
    ('monthly_revenue',          '月營收'),
    ('quarterly',                '季財報'),
    ('announcements',            '自結公告'),
    ('finmind_institutional',    '三大法人買賣超'),
    ('finmind_holding',          '股權分散表'),
    ('finmind_valuation',        'PER/PBR/殖利率'),
    ('finmind_dividend',         '股利政策'),
    ('finmind_dividend_result',  '除權息填息'),
    ('finmind_financials',       '財報補充(資產負債/現金流)'),
    ('director_holdings',        '董監持股'),
    ('broker_trades',            '券商分點進出'),
    ('stock_ai_analysis',        'AI個股分析'),
]


@app.route('/api/admin/health')
def api_admin_health():
    """System health snapshot for the admin dashboard: DB row counts,
    FINMIND_TOKEN presence, and per-task last-run status + success rate
    over the most recent 20 attempts (mirrors the silent-failure trap
    described in CLAUDE.md — this makes it visible without manually
    diffing crawler_logs)."""
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    db = SessionLocal()
    try:
        db_stats = {
            'stocks':    db.query(Stock).count(),
            'prices':    db.query(DailyPrice).count(),
            'revenues':  db.query(MonthlyRevenue).count(),
            'quarterly': db.query(QuarterlyFinancial).count(),
            'users':     db.query(User).count(),
            'messages':  db.query(Message).count(),
            'last_price_date': str(
                db.execute(text('SELECT MAX(date) FROM daily_prices')).scalar() or ''
            ),
        }
        tasks = []
        for task, label in _HEALTH_TASKS:
            recent = (
                db.query(CrawlerLog)
                .filter(CrawlerLog.task == task)
                .order_by(desc(CrawlerLog.created_at))
                .limit(20)
                .all()
            )
            last = recent[0] if recent else None
            success_n = sum(1 for l in recent if l.status == 'success')
            tasks.append({
                'task':          task,
                'label':         label,
                'last_status':   last.status if last else None,
                'last_message':  last.message if last else None,
                'last_run':      str(last.created_at) if last else None,
                'success_rate':  f'{success_n}/{len(recent)}' if recent else '—',
            })
        return jsonify({
            'db': db_stats,
            'finmind_token_set': bool(os.environ.get('FINMIND_TOKEN')),
            'tasks': tasks,
        })
    finally:
        db.close()


@app.route('/api/admin/visits')
def api_admin_visits():
    """Recent homepage visits (see the before_request hook _log_visit) +
    today's unique-IP / visit counts, for the admin dashboard's "who's
    coming to my site" panel."""
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    db = SessionLocal()
    try:
        today_str = datetime.now(_TZ).date().isoformat()
        total_visits = db.query(VisitLog).count()
        today_visits = (
            db.query(VisitLog)
            .filter(text("date(created_at) = :today")).params(today=today_str)
            .count()
        )
        today_unique_ips = (
            db.query(sa_func.count(sa_func.distinct(VisitLog.ip)))
            .filter(text("date(created_at) = :today")).params(today=today_str)
            .scalar()
        )
        recent = (
            db.query(VisitLog)
            .order_by(desc(VisitLog.created_at))
            .limit(200)
            .all()
        )
        return jsonify({
            'total_visits':     total_visits,
            'today_visits':     today_visits,
            'today_unique_ips': today_unique_ips or 0,
            'visits': [{
                'created_at':  str(v.created_at),
                'ip':          v.ip or '',
                'referrer':    v.referrer or '',
                'user_agent':  v.user_agent or '',
                'username':    v.username or '',
            } for v in recent],
        })
    finally:
        db.close()


@app.route('/api/admin/messages')
def api_admin_messages():
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    db = SessionLocal()
    try:
        msgs = db.query(Message).order_by(Message.id.desc()).limit(500).all()
        per_user = {}
        for m in msgs:
            per_user[m.username] = per_user.get(m.username, 0) + 1
        return jsonify({
            'total': db.query(Message).count(),
            'per_user': sorted(per_user.items(), key=lambda kv: -kv[1]),
            'messages': [{
                'id':         m.id,
                'user_id':    m.user_id,
                'username':   m.username,
                'content':    m.content,
                'created_at': m.created_at.strftime('%Y-%m-%d %H:%M'),
            } for m in msgs],
        })
    finally:
        db.close()


@app.route('/api/admin/messages/bulk-delete', methods=['POST'])
def api_admin_messages_bulk_delete():
    if not _is_admin():
        return jsonify({'error': 'unauthorized'}), 403
    ids = (request.json or {}).get('ids') or []
    if not isinstance(ids, list) or not ids:
        return jsonify({'error': 'no ids given'}), 400
    db = SessionLocal()
    try:
        n = db.query(Message).filter(Message.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
        return jsonify({'ok': True, 'deleted': n})
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

# FINMIND_TOKEN missing is a silent-failure trap: every finmind_* crawler
# task fails with the same message every single day (crawler_logs shows it,
# but nobody checks that panel proactively), so institutional_trades /
# holding_concentration / PER-PBR / dividend data quietly goes stale for
# weeks without anything loud enough to notice — this is exactly what
# happened locally on 2026-07-16 (11 days stale, only found by manually
# diffing table max(date) values). Surface it immediately at startup
# instead of waiting to be rediscovered the same way again.
if not os.environ.get('FINMIND_TOKEN'):
    logger.warning('FINMIND_TOKEN is not set — every finmind_* crawl task will fail until it is')
    crawler._log('finmind_token_check', 'failed',
                  'FINMIND_TOKEN environment variable not set — 達人選股相關資料（法人買賣超/股權分散表/'
                  'PER-PBR/股利政策）將無法更新。設定方式：setx FINMIND_TOKEN "your-token"，然後重啟本機服務。')


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    # threaded=True: without it, Werkzeug's dev server handles one request at
    # a time, so a heavy background crawl (quarterly/finmind_data) blocks
    # every page load until it finishes — looks like the site crashed.
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
