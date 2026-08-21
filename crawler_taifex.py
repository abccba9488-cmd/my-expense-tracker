"""台指期貨/選擇權籌碼資料爬蟲（FinMind），獨立於 crawler.py（該檔案的資料
都以 stock_code 為鍵，這裡的資料是市場層級，不綁股票代號）。

涵蓋：期貨/選擇權每日行情、三大法人期貨/選擇權買賣、十大交易人期貨/選擇權
未沖銷部位。衍生計算（Put/Call Ratio、支撐/壓力、大戶多空比）見
taifex_analysis.py，這裡只負責把 FinMind 原始資料原樣存進 DB。

寫法比照 crawler.py 的 crawl_finmind_institutional：_log() 記錄 running/
success/failed、INSERT OR REPLACE 靠 UniqueConstraint 去重、例外一律
rollback → _log(failed) → logger.exception → raise。"""
import logging

from database import (
    SessionLocal, TaifexFuturesDaily, TaifexOptionDaily,
    TaifexFuturesInstitutional, TaifexOptionInstitutional,
    TaifexFuturesLargeTraders, TaifexOptionLargeTraders,
    TaifexFuturesInstitutionalMini,
)
import finmind_client
from crawler import _log, _parse_iso_date

logger = logging.getLogger(__name__)

# TaiwanOptionInstitutionalInvestors uses Chinese 買權/賣權; TaiwanOptionDaily
# and the *OpenInterestLargeTraders datasets already use English call/put —
# normalize everything to English so the DB (and downstream charting code)
# only ever sees one convention.
_CALL_PUT_MAP = {'買權': 'call', '賣權': 'put', 'call': 'call', 'put': 'put'}


def _norm_call_put(v):
    return _CALL_PUT_MAP.get(v, v)


# TaifexFuturesLargeTraders / TaifexOptionLargeTraders share this exact set
# of 18 fields (FinMind returns them with identical names for both datasets).
_LARGE_TRADER_FIELDS = (
    'buy_top5_trader_open_interest', 'buy_top5_trader_open_interest_per',
    'buy_top10_trader_open_interest', 'buy_top10_trader_open_interest_per',
    'sell_top5_trader_open_interest', 'sell_top5_trader_open_interest_per',
    'sell_top10_trader_open_interest', 'sell_top10_trader_open_interest_per',
    'market_open_interest',
    'buy_top5_specific_open_interest', 'buy_top5_specific_open_interest_per',
    'buy_top10_specific_open_interest', 'buy_top10_specific_open_interest_per',
    'sell_top5_specific_open_interest', 'sell_top5_specific_open_interest_per',
    'sell_top10_specific_open_interest', 'sell_top10_specific_open_interest_per',
)

# TaifexFuturesInstitutional / TaifexOptionInstitutional share this set too.
_INSTITUTIONAL_FIELDS = (
    'long_deal_volume', 'long_deal_amount', 'short_deal_volume', 'short_deal_amount',
    'long_open_interest_balance_volume', 'long_open_interest_balance_amount',
    'short_open_interest_balance_volume', 'short_open_interest_balance_amount',
)


def crawl_finmind_taifex_futures_daily(date_str: str):
    """台指期貨（TX）每日行情。date_str: YYYYMMDD。只存日盤（position），跳過
    跨月價差契約（contract_date 含 '/' 的列，如 '202608/202609'）。"""
    iso = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    task = 'finmind_taifex_futures_daily'
    _log(task, 'running', date_str)
    db = SessionLocal()
    try:
        rows = finmind_client.fetch('TaiwanFuturesDaily', start_date=iso, end_date=iso, data_id='TX')
        records = []
        for r in rows:
            if r.get('trading_session') != 'position':
                continue
            cd = r.get('contract_date') or ''
            if '/' in cd:
                continue
            records.append({
                'contract_date': cd,
                'date': _parse_iso_date(r['date']),
                'open': r.get('open'), 'high': r.get('max'), 'low': r.get('min'),
                'close': r.get('close'), 'spread': r.get('spread'), 'spread_per': r.get('spread_per'),
                'volume': r.get('volume'), 'settlement_price': r.get('settlement_price'),
                'open_interest': r.get('open_interest'),
            })
        if records:
            db.execute(TaifexFuturesDaily.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log(task, 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log(task, 'failed', f'{date_str}: {e}')
        logger.exception('%s failed for %s', task, date_str)
        raise
    finally:
        db.close()


def crawl_finmind_taifex_option_daily(date_str: str):
    """台指選擇權（TXO）每日行情，逐履約價。date_str: YYYYMMDD。只存日盤。"""
    iso = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    task = 'finmind_taifex_option_daily'
    _log(task, 'running', date_str)
    db = SessionLocal()
    try:
        rows = finmind_client.fetch('TaiwanOptionDaily', start_date=iso, end_date=iso, data_id='TXO')
        records = []
        for r in rows:
            if r.get('trading_session') != 'position':
                continue
            records.append({
                'contract_date': r.get('contract_date'),
                'date': _parse_iso_date(r['date']),
                'strike_price': r.get('strike_price'),
                'call_put': _norm_call_put(r.get('call_put')),
                'open': r.get('open'), 'high': r.get('max'), 'low': r.get('min'),
                'close': r.get('close'), 'volume': r.get('volume'),
                'settlement_price': r.get('settlement_price'),
                'open_interest': r.get('open_interest'),
            })
        if records:
            db.execute(TaifexOptionDaily.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log(task, 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log(task, 'failed', f'{date_str}: {e}')
        logger.exception('%s failed for %s', task, date_str)
        raise
    finally:
        db.close()


def crawl_finmind_taifex_futures_institutional(date_str: str):
    """三大法人期貨買賣（台指期貨TX，自營商/投信/外資）。date_str: YYYYMMDD。"""
    iso = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    task = 'finmind_taifex_futures_institutional'
    _log(task, 'running', date_str)
    db = SessionLocal()
    try:
        rows = finmind_client.fetch('TaiwanFuturesInstitutionalInvestors',
                                     start_date=iso, end_date=iso, data_id='TX')
        records = []
        for r in rows:
            rec = {f: r.get(f) for f in _INSTITUTIONAL_FIELDS}
            rec['date'] = _parse_iso_date(r['date'])
            rec['institutional_investors'] = r.get('institutional_investors')
            records.append(rec)
        if records:
            db.execute(TaifexFuturesInstitutional.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log(task, 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log(task, 'failed', f'{date_str}: {e}')
        logger.exception('%s failed for %s', task, date_str)
        raise
    finally:
        db.close()


def crawl_finmind_taifex_option_institutional(date_str: str):
    """三大法人選擇權買賣（台指選擇權TXO，買權/賣權分開）。date_str: YYYYMMDD。"""
    iso = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    task = 'finmind_taifex_option_institutional'
    _log(task, 'running', date_str)
    db = SessionLocal()
    try:
        rows = finmind_client.fetch('TaiwanOptionInstitutionalInvestors',
                                     start_date=iso, end_date=iso, data_id='TXO')
        records = []
        for r in rows:
            rec = {f: r.get(f) for f in _INSTITUTIONAL_FIELDS}
            rec['date'] = _parse_iso_date(r['date'])
            rec['call_put'] = _norm_call_put(r.get('call_put'))
            rec['institutional_investors'] = r.get('institutional_investors')
            records.append(rec)
        if records:
            db.execute(TaifexOptionInstitutional.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log(task, 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log(task, 'failed', f'{date_str}: {e}')
        logger.exception('%s failed for %s', task, date_str)
        raise
    finally:
        db.close()


def crawl_finmind_taifex_futures_large_traders(date_str: str):
    """十大交易人期貨未沖銷部位（台指期貨TX）。date_str: YYYYMMDD。
    contract_type: 'week'（近週）/YYYYMM（近月）/'all'（所有契約月份合計）。"""
    iso = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    task = 'finmind_taifex_futures_large_traders'
    _log(task, 'running', date_str)
    db = SessionLocal()
    try:
        rows = finmind_client.fetch('TaiwanFuturesOpenInterestLargeTraders',
                                     start_date=iso, end_date=iso, data_id='TX')
        records = []
        for r in rows:
            rec = {f: r.get(f) for f in _LARGE_TRADER_FIELDS}
            rec['date'] = _parse_iso_date(r['date'])
            rec['contract_type'] = r.get('contract_type')
            records.append(rec)
        if records:
            db.execute(TaifexFuturesLargeTraders.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log(task, 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log(task, 'failed', f'{date_str}: {e}')
        logger.exception('%s failed for %s', task, date_str)
        raise
    finally:
        db.close()


def crawl_finmind_taifex_option_large_traders(date_str: str):
    """十大交易人選擇權未沖銷部位（台指選擇權TXO，買權/賣權分開）。
    date_str: YYYYMMDD。原始欄位名是 put_call（非 call_put），這裡正規化。"""
    iso = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    task = 'finmind_taifex_option_large_traders'
    _log(task, 'running', date_str)
    db = SessionLocal()
    try:
        rows = finmind_client.fetch('TaiwanOptionOpenInterestLargeTraders',
                                     start_date=iso, end_date=iso, data_id='TXO')
        records = []
        for r in rows:
            rec = {f: r.get(f) for f in _LARGE_TRADER_FIELDS}
            rec['date'] = _parse_iso_date(r['date'])
            rec['call_put'] = _norm_call_put(r.get('put_call'))
            rec['contract_type'] = r.get('contract_type')
            records.append(rec)
        if records:
            db.execute(TaifexOptionLargeTraders.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log(task, 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log(task, 'failed', f'{date_str}: {e}')
        logger.exception('%s failed for %s', task, date_str)
        raise
    finally:
        db.close()


def crawl_finmind_taifex_futures_institutional_mini(date_str: str):
    """三大法人小台(MTX)/微台(TMF)期貨買賣，用來跟大台(TX，見
    crawl_finmind_taifex_futures_institutional)合併算「約當大台」。
    date_str: YYYYMMDD。逐一對 MTX/TMF 兩個 data_id 各呼叫一次 FinMind
    （這個 dataset 不支援一次查多個商品）。"""
    iso = f'{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}'
    task = 'finmind_taifex_futures_institutional_mini'
    _log(task, 'running', date_str)
    db = SessionLocal()
    try:
        records = []
        for futures_id in ('MTX', 'TMF'):
            rows = finmind_client.fetch('TaiwanFuturesInstitutionalInvestors',
                                         start_date=iso, end_date=iso, data_id=futures_id)
            for r in rows:
                rec = {f: r.get(f) for f in _INSTITUTIONAL_FIELDS}
                rec['date'] = _parse_iso_date(r['date'])
                rec['futures_id'] = futures_id
                rec['institutional_investors'] = r.get('institutional_investors')
                records.append(rec)
        if records:
            db.execute(TaifexFuturesInstitutionalMini.__table__.insert().prefix_with('OR REPLACE'), records)
            db.commit()
        _log(task, 'success', f'{date_str}: {len(records)} records')
        return len(records)
    except Exception as e:
        db.rollback()
        _log(task, 'failed', f'{date_str}: {e}')
        logger.exception('%s failed for %s', task, date_str)
        raise
    finally:
        db.close()
