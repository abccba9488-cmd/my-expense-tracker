"""CNN Fear & Greed Index 爬蟲。資料來源非官方 API
https://production.dataviz.cnn.io/index/fearandgreed/graphdata/<date>——CNN
官網前端本身打的端點，無公開文件，社群逆向工程確認可用（多個開源專案都
用同一支，例如 github.com/whit3rabbit/fear-greed-data）。

跟本專案其他爬蟲來源（TWSE/TPEX/MOPS/FinMind）不同，是唯一一個非台股
資料源——放在期權籌碼分析（taifex）功能底下純粹是使用情境相近（跟台指
期貨收盤價一起畫圖當國際市場情緒參考），不是真的跟台指期交所有關，所以
獨立成這個檔案而不塞進 crawler_taifex.py。

這支 API 沒有「查某一天」的概念：路徑上的日期參數只決定回傳歷史陣列的
起點，一次呼叫就能拿到從那天到「現在」的全部逐日資料（實測 2020-08-01
到今天約1500+筆，一次呼叫就緒），不像 FinMind 系列 dataset 要逐日迴圈。
因此不需要另外的 backfill 迴圈——`crawl_fear_greed_index()` 本身每次呼叫
就是「同步全部歷史＋順便更新最新一天」，重複呼叫是 idempotent
（INSERT OR REPLACE 靠 UniqueConstraint('date') 去重）。"""
import logging
from datetime import datetime, timezone

from database import SessionLocal, CnnFearGreedIndex
from crawler import _get, _log

logger = logging.getLogger(__name__)

# 固定抓 CNN 資料起點（2020-08-03）當天，讓 API 把能給的歷史全部吐出來
# ——不用逐年/逐日迴圈。**注意：日期參數若早於這個起點，CNN 的 API 會直接
# 回傳 500（已用真實請求驗證：2020-08-01 可以、2020-07-01 會500），不是本站
# 網路問題，別誤改成更早的日期。**
_URL = 'https://production.dataviz.cnn.io/index/fearandgreed/graphdata/2020-08-03'


def crawl_fear_greed_index(date_str: str):
    """CNN Fear & Greed Index。date_str: YYYYMMDD，只用來記錄 log，這支 API
    本身跟查詢日期無關（見上方模組說明）。"""
    task = 'cnn_fear_greed'
    _log(task, 'running', date_str)
    db = SessionLocal()
    try:
        resp = _get(_URL, headers={'Accept': 'application/json'})
        resp.raise_for_status()
        data = resp.json()
        points = data.get('fear_and_greed_historical', {}).get('data', [])

        # 同一天可能有多筆盤中快照，每個日期只留時間戳記最新的一筆。
        latest_by_date = {}
        for p in points:
            ts, score, rating = p.get('x'), p.get('y'), p.get('rating')
            if ts is None or score is None:
                continue
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
            if d not in latest_by_date or ts > latest_by_date[d][0]:
                latest_by_date[d] = (ts, score, rating)

        records = [{'date': d, 'score': score, 'rating': rating}
                   for d, (ts, score, rating) in latest_by_date.items()]
        if records:
            db.execute(CnnFearGreedIndex.__table__.insert().prefix_with('OR REPLACE'), records)
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
