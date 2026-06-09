import os
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Integer, Float, BigInteger,
    Date, DateTime, Text, UniqueConstraint, Index, text
)
from sqlalchemy.orm import declarative_base, sessionmaker

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'data', 'stocks.db')
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(
    f'sqlite:///{DB_PATH}',
    connect_args={'check_same_thread': False},
    pool_size=5,
    max_overflow=10,
)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Stock(Base):
    __tablename__ = 'stocks'
    code     = Column(String(10), primary_key=True)
    name     = Column(String(50), nullable=False)
    market   = Column(String(10), nullable=False)  # TWSE | TPEX
    industry = Column(String(50))
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


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


class User(Base):
    __tablename__ = 'users'
    id            = Column(Integer, primary_key=True, autoincrement=True)
    username      = Column(String(50), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    created_at    = Column(DateTime, default=datetime.now)


class Watchlist(Base):
    __tablename__ = 'watchlists'
    __table_args__ = (Index('ix_wl_user', 'user_id'),)
    id         = Column(Integer, primary_key=True, autoincrement=True)
    user_id    = Column(Integer, nullable=False)
    name       = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.now)


class WatchlistStock(Base):
    __tablename__ = 'watchlist_stocks'
    __table_args__ = (
        UniqueConstraint('watchlist_id', 'stock_code'),
        Index('ix_wls_wl', 'watchlist_id'),
    )
    id           = Column(Integer, primary_key=True, autoincrement=True)
    watchlist_id = Column(Integer, nullable=False)
    stock_code   = Column(String(10), nullable=False)


class CrawlerLog(Base):
    __tablename__ = 'crawler_logs'
    id         = Column(Integer, primary_key=True, autoincrement=True)
    task       = Column(String(50), nullable=False)
    status     = Column(String(20), nullable=False)  # running | success | failed
    message    = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


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

    # Migration: fix Q4 annual EPS/revenue → individual Q4 (subtract Q1+Q2+Q3)
    with engine.connect() as conn:
        done = conn.execute(text(
            "SELECT COUNT(*) FROM schema_migrations WHERE name='q4_annual_to_individual'"
        )).scalar()
        if not done:
            _migrate_q4_to_individual(conn)
            conn.execute(text("INSERT INTO schema_migrations(name) VALUES('q4_annual_to_individual')"))
            conn.commit()
