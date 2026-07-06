import os
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import (
    create_engine, event, Column, String, Integer, Float, BigInteger,
    Date, DateTime, Text, UniqueConstraint, Index, text
)
from sqlalchemy.orm import declarative_base, sessionmaker

_TZ = ZoneInfo('Asia/Taipei')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'stocks.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(
    f'sqlite:///{DB_PATH}',
    connect_args={'check_same_thread': False},
    pool_size=2,
    max_overflow=2,
)


@event.listens_for(engine, 'connect')
def _set_sqlite_pragma(dbapi_conn, _):
    """Cap SQLite's per-connection memory use (mmap/page cache) so large
    queries against the multi-year DB don't blow past the container's
    memory limit."""
    cur = dbapi_conn.cursor()
    cur.execute('PRAGMA mmap_size=0')
    cur.execute('PRAGMA cache_size=-2000')   # ~2MB page cache
    cur.execute('PRAGMA temp_store=FILE')
    # WAL: readers no longer block a writer's commit (the default rollback
    # journal does — a long-running read like compute_expert_scores() could
    # stall a concurrent backfill/crawler write past any busy_timeout).
    # journal_mode is stored in the db file itself, but PRAGMA is idempotent
    # so setting it on every connect is harmless and self-healing if the
    # file is ever replaced/restored without WAL.
    # BUT: WAL needs shared-memory (mmap) support from the filesystem the db
    # file lives on, and fails outright on some network-attached volumes
    # (exactly the kind a PaaS persistent volume often is) — wrapped so a
    # deployment whose volume doesn't support it still boots on the plain
    # rollback journal instead of crashing every worker at connect time,
    # before any log line can even be written.
    try:
        cur.execute('PRAGMA journal_mode=WAL')
    except Exception:
        pass
    # Belt-and-suspenders: wait instead of immediately raising "database is
    # locked" for the cases WAL doesn't fully cover (two simultaneous writers).
    cur.execute('PRAGMA busy_timeout=30000')
    cur.close()


SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Stock(Base):
    __tablename__ = 'stocks'
    code     = Column(String(10), primary_key=True)
    name     = Column(String(50), nullable=False)
    market   = Column(String(10), nullable=False)  # TWSE | TPEX
    industry = Column(String(50))
    updated_at = Column(DateTime, default=lambda: datetime.now(_TZ), onupdate=lambda: datetime.now(_TZ))


class DailyPrice(Base):
    __tablename__ = 'daily_prices'
    __table_args__ = (
        UniqueConstraint('stock_code', 'date'),
        Index('ix_dp_code_date', 'stock_code', 'date'),
    )
    id         = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(10), nullable=False)
    date       = Column(Date, nullable=False)
    open       = Column(Float)
    high       = Column(Float)
    low        = Column(Float)
    close      = Column(Float)
    volume     = Column(BigInteger)
    change     = Column(Float)
    change_pct = Column(Float)
    per             = Column(Float)   # 本益比（FinMind TaiwanStockPER）
    pbr             = Column(Float)   # 股價淨值比
    dividend_yield  = Column(Float)   # 殖利率 %


class MonthlyRevenue(Base):
    __tablename__ = 'monthly_revenue'
    __table_args__ = (
        UniqueConstraint('stock_code', 'year', 'month'),
        Index('ix_mr_code', 'stock_code'),
    )
    id          = Column(Integer, primary_key=True, autoincrement=True)
    stock_code  = Column(String(10), nullable=False)
    year        = Column(Integer, nullable=False)
    month       = Column(Integer, nullable=False)
    revenue     = Column(BigInteger)   # 單位：千元
    revenue_yoy = Column(Float)        # 年增率 %
    revenue_mom = Column(Float)        # 月增率 %
    start_price = Column(Float)        # 首次寫入當天收盤價
    turnaround_signal = Column(Integer)  # 1=潛在虧轉盈候選（最新一季EPS<0 且本月營收年增>=20%）
    updated_at  = Column(DateTime, default=lambda: datetime.now(_TZ), onupdate=lambda: datetime.now(_TZ))


class QuarterlyFinancial(Base):
    __tablename__ = 'quarterly_financials'
    __table_args__ = (
        UniqueConstraint('stock_code', 'year', 'quarter'),
        Index('ix_qf_code', 'stock_code'),
    )
    id               = Column(Integer, primary_key=True, autoincrement=True)
    stock_code       = Column(String(10), nullable=False)
    year             = Column(Integer, nullable=False)
    quarter          = Column(Integer, nullable=False)  # 1–4
    revenue          = Column(BigInteger)   # 千元
    operating_income = Column(BigInteger)   # 千元
    net_income       = Column(BigInteger)   # 千元
    eps              = Column(Float)        # 元/股
    updated_at       = Column(DateTime, default=lambda: datetime.now(_TZ), onupdate=lambda: datetime.now(_TZ))


class InstitutionalTrade(Base):
    """三大法人買賣超（日資料），來源 FinMind TaiwanStockInstitutionalInvestorsBuySell。
    單位：股。dealer_buy/sell 已把 Dealer_self + Dealer_Hedging 加總。"""
    __tablename__ = 'institutional_trades'
    __table_args__ = (
        UniqueConstraint('stock_code', 'date'),
        Index('ix_it_code_date', 'stock_code', 'date'),
    )
    id          = Column(Integer, primary_key=True, autoincrement=True)
    stock_code  = Column(String(10), nullable=False)
    date        = Column(Date, nullable=False)
    foreign_buy  = Column(BigInteger)
    foreign_sell = Column(BigInteger)
    trust_buy    = Column(BigInteger)
    trust_sell   = Column(BigInteger)
    dealer_buy   = Column(BigInteger)
    dealer_sell  = Column(BigInteger)


class HoldingConcentration(Base):
    """股權分散表（週資料），來源 FinMind TaiwanStockHoldingSharesPer。
    pct_* 為該張數門檻(含)以上／以下各級距 percent 加總，門檻對應
    FinMind HoldingSharesLevel 的股數分界（400,001/600,001/800,001/1,000,001 股）。"""
    __tablename__ = 'holding_concentration'
    __table_args__ = (
        UniqueConstraint('stock_code', 'date'),
        Index('ix_hc_code_date', 'stock_code', 'date'),
    )
    id          = Column(Integer, primary_key=True, autoincrement=True)
    stock_code  = Column(String(10), nullable=False)
    date        = Column(Date, nullable=False)
    pct_1000up  = Column(Float)   # >=1,000,001股 (1000張以上)
    pct_800up   = Column(Float)   # >=800,001股
    pct_600up   = Column(Float)   # >=600,001股
    pct_400up   = Column(Float)   # >=400,001股
    pct_200down = Column(Float)   # <=200,000股 (200張以下)
    pct_100down = Column(Float)   # <=100,000股


class FinancialExtra(Base):
    """季資料，補足 quarterly_financials 沒有的資產負債表/現金流量表/毛利項目。
    來源 FinMind TaiwanStockBalanceSheet + TaiwanStockFinancialStatements +
    TaiwanStockCashFlowsStatement。獨立於 quarterly_financials（MOPS 來源），
    用 (stock_code, year, quarter) 對齊但不混用兩個資料來源。單位：千元。"""
    __tablename__ = 'financial_extra'
    __table_args__ = (
        UniqueConstraint('stock_code', 'year', 'quarter'),
        Index('ix_fe_code', 'stock_code'),
    )
    id                    = Column(Integer, primary_key=True, autoincrement=True)
    stock_code            = Column(String(10), nullable=False)
    year                  = Column(Integer, nullable=False)
    quarter               = Column(Integer, nullable=False)
    inventories           = Column(BigInteger)
    accounts_receivable   = Column(BigInteger)
    current_assets        = Column(BigInteger)
    current_liabilities   = Column(BigInteger)
    liabilities           = Column(BigInteger)
    equity                = Column(BigInteger)
    total_assets          = Column(BigInteger)
    long_term_borrowings  = Column(BigInteger)
    capital_stock         = Column(BigInteger)
    gross_profit          = Column(BigInteger)
    cost_of_goods_sold    = Column(BigInteger)
    pretax_income         = Column(BigInteger)
    operating_cash_flow   = Column(BigInteger)
    interest_expense      = Column(BigInteger)
    capex                 = Column(BigInteger)
    updated_at            = Column(DateTime, default=lambda: datetime.now(_TZ), onupdate=lambda: datetime.now(_TZ))


class DividendPolicy(Base):
    """個別股利分派事件，來源 FinMind TaiwanStockDividend。單位：元/股。一家
    公司一年可能配息多次（如台積電改季配息後一年4次）——刻意存成逐筆事件而
    不在爬蟲階段就加總成年度總額：FinMind 這個 dataset 的 bulk 查詢只認「單一
    日期精準命中」，range 查詢會漏資料（已用真實 API 呼叫驗證過），爬蟲只能
    逐日呼叫、逐筆累積事件；若爬蟲階段就先加總，重跑/增量爬取的時間窗選擇會
    決定加總範圍，容易重複計算或漏算。年度加總改在 experts.py 用 SQL
    GROUP BY fiscal_year 即時算。fiscal_year 由 FinMind year 欄位（民國年
    字串，如「114年第1次」）反推的西元年。payout_ratio 不在此存（會隨當年度
    EPS 陸續公布而過時），改由 experts.py 對當年度 quarterly_financials EPS
    加總即時計算。"""
    __tablename__ = 'dividend_policy'
    __table_args__ = (UniqueConstraint('stock_code', 'event_date'),)
    id             = Column(Integer, primary_key=True, autoincrement=True)
    stock_code     = Column(String(10), nullable=False)
    event_date     = Column(Date, nullable=False)
    fiscal_year    = Column(Integer, nullable=False)
    cash_dividend  = Column(Float)
    stock_dividend = Column(Float)


class DividendFillEvent(Base):
    """除權息事件，來源 FinMind TaiwanStockDividendResult。before_price 是除權
    息前一交易日收盤價（填息比較基準）；filled 由爬蟲每次執行時用目前已有的
    daily_prices 資料重新判斷（除息日之後最高收盤價 >= before_price 即算填息），
    尚未填息的舊事件會隨新股價資料進來持續重新檢查，不是一次性判斷。"""
    __tablename__ = 'dividend_fill_events'
    __table_args__ = (UniqueConstraint('stock_code', 'ex_date'),)
    id           = Column(Integer, primary_key=True, autoincrement=True)
    stock_code   = Column(String(10), nullable=False)
    ex_date      = Column(Date, nullable=False)
    before_price = Column(Float)
    filled       = Column(Integer)   # 1 = 有填息, 0/NULL = 尚未（可能之後補上）


class ExpertScore(Base):
    """達人選股：每檔股票在每套規則下的最新一次計分結果（比照 stock_ai_analysis
    只存最新快照，每次 compute_expert_scores() 覆寫）。"""
    __tablename__ = 'expert_scores'
    __table_args__ = (UniqueConstraint('stock_code', 'expert_key'),)
    id            = Column(Integer, primary_key=True, autoincrement=True)
    stock_code    = Column(String(10), nullable=False)
    expert_key    = Column(String(30), nullable=False)
    expert_label  = Column(String(50))
    passed        = Column(Integer)     # 1 = 通過選股標準
    score         = Column(Integer)
    max_score     = Column(Integer)
    breakdown_json = Column(Text)
    computed_at   = Column(DateTime, default=lambda: datetime.now(_TZ), onupdate=lambda: datetime.now(_TZ))


class DirectorHolding(Base):
    """董監持股比例，來源 TWSE OpenAPI（opendata/t187ap11_L，上市）+ TPEX
    OpenAPI（mopsfin_t187ap11_O，上櫃）。兩個都是免金鑰的官方公開資料，每次
    呼叫回傳當下最新一期全市場快照（月更新），不能用日期查歷史。
    director_shares 只加總「職稱」為董事長/副董事長/常務董事/董事/獨立董事/
    監察人「本人」的列（排除法人代表列——股份已算在法人本身那筆、本人這裡是
    0，也排除總經理/副總經理/財會主管等經理人列、以及非董監的大股東本人列）。
    shares_outstanding 用 financial_extra.capital_stock 反推（股本÷面額10元，
    絕大多數台股適用，面額非10元的少數股票會不準）。"""
    __tablename__ = 'director_holdings'
    __table_args__ = (UniqueConstraint('stock_code', 'year_month'),)
    id                 = Column(Integer, primary_key=True, autoincrement=True)
    stock_code         = Column(String(10), nullable=False)
    year_month         = Column(String(6), nullable=False)   # 民國年月，如 "11505"，比照來源本身格式
    director_shares    = Column(BigInteger)
    shares_outstanding = Column(BigInteger)
    holding_pct        = Column(Float)
    updated_at         = Column(DateTime, default=lambda: datetime.now(_TZ), onupdate=lambda: datetime.now(_TZ))


class User(Base):
    __tablename__ = 'users'
    id            = Column(Integer, primary_key=True, autoincrement=True)
    username      = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    created_at    = Column(DateTime, default=lambda: datetime.now(_TZ))


class Watchlist(Base):
    __tablename__ = 'watchlists'
    __table_args__ = (Index('ix_wl_user', 'user_id'),)
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, nullable=False)
    name       = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(_TZ))


class WatchlistStock(Base):
    __tablename__ = 'watchlist_stocks'
    __table_args__ = (
        UniqueConstraint('watchlist_id', 'stock_code'),
        Index('ix_wls_wl', 'watchlist_id'),
    )
    id           = Column(Integer, primary_key=True, autoincrement=True)
    watchlist_id = Column(Integer, nullable=False)
    stock_code   = Column(String(10), nullable=False)


class Message(Base):
    __tablename__ = 'messages'
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, nullable=False)
    username   = Column(String(50), nullable=False)
    content    = Column(Text, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(_TZ))


class CrawlerLog(Base):
    __tablename__ = 'crawler_logs'
    id         = Column(Integer, primary_key=True, autoincrement=True)
    task       = Column(String(50), nullable=False)
    status     = Column(String(20), nullable=False)  # running | success | failed
    message    = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(_TZ))


class Announcement(Base):
    __tablename__ = 'announcements'
    __table_args__ = (UniqueConstraint('stock_code', 'seq_no'),)
    id            = Column(Integer, primary_key=True, autoincrement=True)
    stock_code    = Column(String(10), nullable=False)
    seq_no        = Column(String(20), nullable=False)   # MOPS SEQ_NO for dedup
    announce_date = Column(Date, nullable=False)
    announce_time = Column(String(10))
    subject       = Column(Text)
    content       = Column(Text)
    price_at_announce    = Column(Float)   # 公告日（或往前最近交易日）收盤價
    monthly_eps          = Column(Float)   # 單月EPS
    prior_year_eps       = Column(Float)   # 去年同月EPS
    eps_yoy              = Column(Float)   # 月EPS年增率 %
    turnaround           = Column(Integer) # 1 = 公告內容含「由虧轉盈/轉虧為盈」
    estimated_annual_eps = Column(Float)   # monthly_eps × 12
    estimated_pe         = Column(Float)   # price_at_announce / estimated_annual_eps，取小數點後1位
    ai_rating     = Column(String(30))   # 🔴🟠🟡🟢 + label
    ai_analysis   = Column(Text)
    created_at    = Column(DateTime, default=lambda: datetime.now(_TZ))


class StockAiAnalysis(Base):
    """On-demand AI valuation analysis for a single stock, admin-triggered
    only (never run automatically/in bulk — each row is an OpenRouter call
    the admin explicitly paid for). One row per stock_code, overwritten on
    each re-analysis — no history kept, this is a "latest snapshot" cache
    so repeat page views don't re-trigger a paid AI call."""
    __tablename__ = 'stock_ai_analysis'
    stock_code       = Column(String(10), primary_key=True)
    ai_rating        = Column(String(30))   # 🔴🟠🟡🟢 + label
    target_cheap     = Column(Float)        # 便宜價
    target_fair      = Column(Float)        # 合理價
    target_expensive = Column(Float)        # 昂貴價
    ai_analysis      = Column(Text)
    updated_at       = Column(DateTime, default=lambda: datetime.now(_TZ), onupdate=lambda: datetime.now(_TZ))


def _migrate_q4_to_individual(conn):
    """Convert Q4 annual cumulative data to individual Q4 by subtracting Q1+Q2+Q3."""
    for field in ('eps', 'revenue', 'operating_income', 'net_income'):
        conn.execute(text(f'''
            UPDATE quarterly_financials
            SET {field} = {field}
                - (SELECT q.{field} FROM quarterly_financials q
                   WHERE q.stock_code = quarterly_financials.stock_code
                     AND q.year = quarterly_financials.year AND q.quarter = 1)
                - (SELECT q.{field} FROM quarterly_financials q
                   WHERE q.stock_code = quarterly_financials.stock_code
                     AND q.year = quarterly_financials.year AND q.quarter = 2)
                - (SELECT q.{field} FROM quarterly_financials q
                   WHERE q.stock_code = quarterly_financials.stock_code
                     AND q.year = quarterly_financials.year AND q.quarter = 3)
            WHERE quarter = 4
              AND {field} IS NOT NULL
              AND (SELECT q.{field} FROM quarterly_financials q
                   WHERE q.stock_code = quarterly_financials.stock_code
                     AND q.year = quarterly_financials.year AND q.quarter = 1) IS NOT NULL
              AND (SELECT q.{field} FROM quarterly_financials q
                   WHERE q.stock_code = quarterly_financials.stock_code
                     AND q.year = quarterly_financials.year AND q.quarter = 2) IS NOT NULL
              AND (SELECT q.{field} FROM quarterly_financials q
                   WHERE q.stock_code = quarterly_financials.stock_code
                     AND q.year = quarterly_financials.year AND q.quarter = 3) IS NOT NULL
        '''))
    # Round eps to 2 decimal places after subtraction
    conn.execute(text('''
        UPDATE quarterly_financials SET eps = ROUND(eps, 2) WHERE quarter = 4 AND eps IS NOT NULL
    '''))


def init_db():
    Base.metadata.create_all(engine)

    # Schema migrations tracking table
    with engine.connect() as conn:
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        '''))
        conn.commit()

    # Migration: add start_price column if not present
    with engine.connect() as conn:
        try:
            conn.execute(text('ALTER TABLE monthly_revenue ADD COLUMN start_price REAL'))
            conn.commit()
        except Exception:
            pass  # Column already exists

    # Migration: add updated_at columns if not present
    with engine.connect() as conn:
        try:
            conn.execute(text('ALTER TABLE monthly_revenue ADD COLUMN updated_at DATETIME'))
            conn.commit()
        except Exception:
            pass  # Column already exists
    with engine.connect() as conn:
        try:
            conn.execute(text('ALTER TABLE quarterly_financials ADD COLUMN updated_at DATETIME'))
            conn.commit()
        except Exception:
            pass  # Column already exists

    # Migration: add turnaround_signal column if not present
    with engine.connect() as conn:
        try:
            conn.execute(text('ALTER TABLE monthly_revenue ADD COLUMN turnaround_signal INTEGER'))
            conn.commit()
        except Exception:
            pass  # Column already exists

    # Backfill: fill start_price for existing rows that have revenue but no start_price
    with engine.connect() as conn:
        conn.execute(text('''
            UPDATE monthly_revenue
            SET start_price = (
                SELECT dp.close FROM daily_prices dp
                WHERE dp.stock_code = monthly_revenue.stock_code
                ORDER BY dp.date DESC LIMIT 1
            )
            WHERE start_price IS NULL
              AND revenue IS NOT NULL
        '''))
        conn.commit()

    # Backfill: compute turnaround_signal for each stock's latest monthly_revenue
    # row using data already in the DB. Needed once after the column was added —
    # existing rows start out NULL and only get recomputed the next time
    # crawl_monthly_revenue() actually runs for that stock, which could be a
    # full day away. Only touches each stock's latest row since that's the only
    # one _SUMMARY_SQL ever reads; safe to rerun (no-op once NULL rows are gone).
    with engine.connect() as conn:
        conn.execute(text('''
            UPDATE monthly_revenue
            SET turnaround_signal = CASE
                WHEN revenue_yoy >= 20 AND (
                    SELECT q.eps FROM quarterly_financials q
                    WHERE q.stock_code = monthly_revenue.stock_code AND q.eps IS NOT NULL
                    ORDER BY q.year DESC, q.quarter DESC LIMIT 1
                ) < 0
                THEN 1 ELSE 0
            END
            WHERE turnaround_signal IS NULL
              AND (year * 100 + month) = (
                  SELECT MAX(year * 100 + month) FROM monthly_revenue mr2
                  WHERE mr2.stock_code = monthly_revenue.stock_code
              )
        '''))
        conn.commit()

    # Migration: fix Q4 annual EPS/revenue → individual Q4 (subtract Q1+Q2+Q3)
    with engine.connect() as conn:
        done = conn.execute(text(
            "SELECT COUNT(*) FROM schema_migrations WHERE name='q4_annual_to_individual'"
        )).scalar()
        if not done:
            _migrate_q4_to_individual(conn)
            conn.execute(text("INSERT INTO schema_migrations(name) VALUES('q4_annual_to_individual')"))
            conn.commit()

    # Migration: add new announcements columns (redesigned, AI-free crawler)
    with engine.connect() as conn:
        for col, coltype in (('price_at_announce', 'REAL'), ('prior_year_eps', 'REAL'),
                              ('estimated_annual_eps', 'REAL')):
            try:
                conn.execute(text(f'ALTER TABLE announcements ADD COLUMN {col} {coltype}'))
                conn.commit()
            except Exception:
                pass  # Column already exists

    # Migration: add back ai_rating / ai_analysis (AI re-introduced,
    # this time enriching deterministically-parsed rows instead of
    # inventing the numbers itself)
    with engine.connect() as conn:
        for col, coltype in (('ai_rating', 'VARCHAR(30)'), ('ai_analysis', 'TEXT')):
            try:
                conn.execute(text(f'ALTER TABLE announcements ADD COLUMN {col} {coltype}'))
                conn.commit()
            except Exception:
                pass  # Column already exists

    # Migration: add per/pbr/dividend_yield columns to daily_prices (達人選股,
    # FinMind TaiwanStockPER)
    with engine.connect() as conn:
        for col in ('per', 'pbr', 'dividend_yield'):
            try:
                conn.execute(text(f'ALTER TABLE daily_prices ADD COLUMN {col} REAL'))
                conn.commit()
            except Exception:
                pass  # Column already exists

    # Migration: one-time wipe of announcements rows written by the old
    # AI-rating design (different schema semantics, e.g. estimated_pe was
    # computed from today's price instead of the announce-date price).
    with engine.connect() as conn:
        done = conn.execute(text(
            "SELECT COUNT(*) FROM schema_migrations WHERE name='clear_old_announcements'"
        )).scalar()
        if not done:
            conn.execute(text('DELETE FROM announcements'))
            conn.execute(text("INSERT INTO schema_migrations(name) VALUES('clear_old_announcements')"))
            conn.commit()
