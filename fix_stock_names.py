"""
One-time fix for 13 stock names corrupted by crawl_stock_list()'s old
'big5' codec (see crawler.py's cp950 fix). Run this via `git pull` +
`python fix_stock_names.py` rather than pasting the UPDATE statements
directly into a terminal — Zeabur's web Command terminal is known to
insert spaces between pasted CJK characters (see CLAUDE.md), which
would silently corrupt the names being written, not just the display.
Getting this file via git sidesteps that entirely. Values below were
copied directly from the already-verified-correct local database, not
retyped, to rule out a second transcription error.

Usage:
    python fix_stock_names.py
"""
from database import SessionLocal, Stock

FIXES = {
    '2353': '宏碁',
    '3046': '建碁',
    '6285': '啟碁',
    '6776': '展碁國際',
    '2432': '倚天酷碁-創',
    '6908': '宏碁遊戲-創',
    '6174': '安碁',
    '6690': '安碁資訊',
    '6811': '宏碁資訊',
    '7794': '宏碁智新',
    '8077': '洛碁',
    '8111': '立碁',
    '8349': '恒耀國際',
}

if __name__ == '__main__':
    db = SessionLocal()
    try:
        updated = 0
        for code, name in FIXES.items():
            row = db.query(Stock).filter_by(code=code).first()
            if row:
                row.name = name
                updated += 1
        db.commit()
        print(f'Updated {updated}/{len(FIXES)} rows')
        for code in FIXES:
            row = db.query(Stock).filter_by(code=code).first()
            print(code, repr(row.name) if row else 'NOT FOUND')
    finally:
        db.close()
