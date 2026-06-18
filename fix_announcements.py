"""
One-off repair script for existing `announcements` rows.

Background: crawl_announcements() used to fall back to a DB-derived fake
content (latest quarterly EPS mislabeled as monthly EPS) whenever the MOPS
detail page couldn't be reached. After fixing crawler.py to fetch the real
detail page and parse it deterministically, run this once to re-process
already-stored rows with the same logic.

Usage (run on the Zeabur terminal):
    python fix_announcements.py                    # all rows
    python fix_announcements.py --limit 50          # only the first 50
    python fix_announcements.py --since 2026-06-01  # only announce_date >= this
"""

import argparse
import logging
import time

import requests
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def main(limit=None, since=None):
    import crawler
    from database import SessionLocal

    db = SessionLocal()
    query = (
        "SELECT a.id, a.stock_code, a.seq_no, a.subject, s.name "
        "FROM announcements a LEFT JOIN stocks s ON s.code = a.stock_code"
    )
    params = {}
    if since:
        query += " WHERE a.announce_date >= :since"
        params['since'] = since
    query += " ORDER BY a.id"
    if limit:
        query += f" LIMIT {int(limit)}"

    rows = db.execute(text(query), params).fetchall()
    logger.info('Repairing %d announcement rows', len(rows))

    sess = crawler.new_mops_session()
    fixed = failed = 0

    for n, row in enumerate(rows, 1):
        ann_id, code, seq_no, old_subject, name = row
        try:
            typek, i, co_id, _date = seq_no.split('_')
        except ValueError:
            logger.warning('Skip id=%d — unparseable seq_no %s', ann_id, seq_no)
            failed += 1
            continue

        try:
            subject, content, parsed = crawler.fetch_announcement_detail(sess, typek, i, co_id)
        except requests.exceptions.RequestException as e:
            logger.warning('id=%d %s seq=%s detail fetch failed: %s', ann_id, code, seq_no, e)
            failed += 1
            crawler._jitter(3)
            continue

        if not content:
            logger.info('id=%d %s seq=%s — no content parsed, skip', ann_id, code, seq_no)
            failed += 1
            continue

        qf = db.execute(text(
            "SELECT eps FROM quarterly_financials WHERE stock_code=:c"
            " ORDER BY year DESC, quarter DESC LIMIT 1"), {'c': code}).first()
        dp = db.execute(text(
            "SELECT close FROM daily_prices WHERE stock_code=:c"
            " ORDER BY date DESC LIMIT 1"), {'c': code}).first()

        if 'quarterly_eps' not in parsed and qf:
            parsed['quarterly_eps'] = qf[0]

        monthly_eps       = parsed.get('monthly_eps')
        eps_yoy           = parsed.get('eps_yoy')
        quarterly_eps     = parsed.get('quarterly_eps')
        quarterly_eps_yoy = parsed.get('quarterly_eps_yoy')
        turnaround        = parsed.get('turnaround')

        eps_for_pe = (monthly_eps * 12) if monthly_eps else (
            quarterly_eps * 4 if quarterly_eps else None)
        estimated_pe = (round(dp[0] / eps_for_pe, 1)
                         if eps_for_pe and dp and eps_for_pe != 0 else None)

        ai_context = content
        if monthly_eps is not None or quarterly_eps is not None:
            ai_context += (
                f"\n[結構化數據] 月EPS={monthly_eps}（年增{eps_yoy}%）"
                f" 季EPS={quarterly_eps}（年增{quarterly_eps_yoy}%）"
                f" 由虧轉盈={'是' if turnaround else '否'}"
            )

        time.sleep(20)  # OpenRouter free-tier rate limit
        ai_rating, ai_analysis = crawler._analyze_with_ai(
            code, name or code, subject or old_subject, ai_context)

        db.execute(text(
            "UPDATE announcements SET subject=:subj, content=:cnt, ai_rating=:r, ai_analysis=:a,"
            " monthly_eps=:me, eps_yoy=:ey, estimated_pe=:ep,"
            " quarterly_eps=:qe, quarterly_eps_yoy=:qey, turnaround=:ta"
            " WHERE id=:id"
        ), {
            'subj': (subject or old_subject)[:500], 'cnt': content[:5000],
            'r': ai_rating, 'a': ai_analysis,
            'me': monthly_eps, 'ey': eps_yoy, 'ep': estimated_pe,
            'qe': quarterly_eps, 'qey': quarterly_eps_yoy, 'ta': turnaround,
            'id': ann_id,
        })
        db.commit()
        fixed += 1
        logger.info('Fixed id=%d %s monthly_eps=%s quarterly_eps=%s turnaround=%s (%d/%d)',
                     ann_id, code, monthly_eps, quarterly_eps, turnaround, n, len(rows))

        crawler._jitter(2)

    db.close()
    logger.info('Done: fixed=%d failed=%d', fixed, failed)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Repair existing announcements with real MOPS detail content')
    parser.add_argument('--limit', type=int, default=None, help='Only process the first N rows')
    parser.add_argument('--since', type=str, default=None, help='Only rows with announce_date >= YYYY-MM-DD')
    args = parser.parse_args()
    main(limit=args.limit, since=args.since)
