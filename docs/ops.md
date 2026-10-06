# 維運：部署歷史、backfill、輔助腳本、資料驗證

> 從 `CLAUDE.md` 拆出（2026-10-06，內容未改寫）。文中「見○○章節」若不在本檔，用 `grep -n '^## ' CLAUDE.md docs/*.md` 找。

## 部署（Zeabur，⚠ 2026-07-22 起已停用）

目前一律本機開發/驗證，不部署。程式碼仍保留雲端相容邏輯，改動時別弄壞：
- `init_db()`、`sched.start()`、自動爬蟲偵測放在 `app.py` **模組層級**（gunicorn 不執行 `__main__` 區塊）
- `Procfile` 用 `--workers 1`：多 worker 會各自啟動一份 APScheduler，重複觸發排程爬蟲
- `database.py` 的 SQLite PRAGMA 依 `sys.platform` 分流（見「效能快取」章節）
- `fetch_db.py`（從 GitHub Release `db-v1` 下載 800MB DB 快照）、`trim_db.py`（刪 5 年前 `daily_prices` + `VACUUM`）是當時給雲端用的工具腳本

細節（Persistent Volume、`--bind 0.0.0.0:$PORT`、記憶體限制等）見 git log，重新啟用雲端時再查。

## 歷史資料補齊（backfill）

`backfill.py` 是獨立腳本，不需要 Flask 執行中。已有資料的日期/月份/季度自動跳過，可中斷後續跑。

```bat
backfill.bat   ← 雙擊，補齊 2011 年至今全部三類資料（約 31 小時）
```

或分開執行：
```
python backfill.py --prices              # 每日股價，~1.5 小時
python backfill.py --revenue             # 月營收，~21 小時
python backfill.py --quarterly           # 季財報，~8.5 小時
python backfill.py --from-year 2020 --prices   # 指定起始年
```

雲端環境建議用 `nohup python backfill.py --prices > /app/backfill.log 2>&1 &` 背景執行，避免終端機斷線中止。

**MOPS IFRS 資料可靠起點**：季財報從 2013 年起穩定；更早年份查詢可能回傳空值（自動略過）。

**TWSE Big5 編碼**：2015 年以前的 TWSE 資料欄位名稱為 Big5 編碼，crawler 解析到 0 筆但 tables 有資料時，會自動以 Big5 重新解碼後再解析。

**達人選股（FinMind）資料回填**：`backfill_finmind.py`，風格與 `backfill.py` 一致（已有資料自動跳過、可中斷續跑），需要 `FINMIND_TOKEN` 環境變數。

```bat
backfill_finmind.bat   ← 雙擊，回填三大法人/股權分散/財報/股利/PER-PBR
```

或分開執行：
```
python backfill_finmind.py --institutional --holding --financials --dividend --valuation
python backfill_finmind.py --financials --from-year 2013   # financial_extra 只有 2013 年後 IFRS 資料可靠
```

**在 Zeabur 正式站上用 `zeabur service exec` 補資料時的安全注意事項**（曾經真的把 production 容器搞當機過）：
- `service exec` 開的是一次性連線，連線一結束，裡面所有子行程（包含 `nohup`/`setsid` detach 過的）都會被砍掉——**不能**指望背景程序撐過單次 exec 呼叫。真的需要背景長跑，要改成對本機（`http://localhost:8080`，會被 `is_local` 判定放行）打 `POST /api/crawler/run/<task>`，讓它以 `_run_bg()` 的執行緒身分活在 gunicorn worker 裡，才不受 exec 連線生死影響。
- **絕對不要繞過 `backfill_finmind.py` 原本設計的節流（每次呼叫間 `time.sleep(0.3)`）自己寫緊湊迴圈直接呼叫 `crawler.crawl_finmind_*`**——沒有節流的連續高頻請求曾經把正式站容器整個壓垮（Zeabur 回收成 `REMOVED`，網站 502），而且跟原本的部署危機是兩回事、事後才發現的新問題。用單行 list comprehension 搭配 `(fn(), time.sleep(0.3))` 這種 tuple trick 可以在不换行的情況下維持節流（`service exec` 對多行 `python -c` 字串的 shell 轉譯不可靠，只能寫單行）。
- 大範圍回填要**分段執行（例如一季一段）並且每段後主動 curl 網站首頁確認還活著**，一旦不健康就先停手排查，不要盲目繼續下一段。
- 個別呼叫偶爾會撞到 `sqlite3.OperationalError: database is locked`（跟正式站當下的即時流量搶鎖）——`crawl_finmind_*` 都是 `INSERT OR REPLACE`/`OR IGNORE`，重跑整段是安全、冪等的，遇到就重試即可。

## 個人輔助腳本（未納入 git，橋接到外部專案，非核心架構）

以下腳本讀寫本專案的 `data/stocks.db`，但輸出目標是使用者另一個獨立專案「Soaring Stocks」（`C:\Users\user\Documents\claude\Soaring Stocks\`）或個人 `Downloads` 資料夾，**刻意不納入 git 版控**（路徑寫死、只在使用者本機有意義）：

- **`backfill_announcements.py`**：一次性回填 2023-01-01～2025-12-31 的自結公告（重用 `crawler.crawl_announcements()`），完成後把 `announcements` 全表匯出成 CSV 給 Soaring Stocks 專案使用。日期區間寫死在檔案頂部，已完成的日期會自動略過、可中斷續跑。
- **`generate_announcement_excel.py`** + **`generate_excel_run.bat`**：爬當天（或指定日期）MOPS 公告存入 DB 後，即時產生一份「注意交易公告」Excel，額外合併 Soaring Stocks 專案的 `dashboard_*.xlsx`（題材/主要產品標籤）與本站最新股價/近90天公告次數，輸出到 `Downloads\announcements_attention_notice_<timestamp>.xlsx`。雙擊 `.bat` 即可執行（可帶 `YYYYMMDD` 參數指定日期）。

## 分析與驗證

| 服務 | ID / 設定 | 位置 |
|------|----------|------|
| Google Analytics GA4 | `G-27TS0SERCE` | `<head>` 最前方 gtag.js |
| Google Search Console | `ZtZskJdvuKex_hNe5sE4xQIpCNHKOn-0OPXDKTtizRs` | `<meta name="google-site-verification">` |

## DB 欄位：前端未呈現但已存入

| 欄位 | 所在表 | 說明 |
|------|--------|------|
| `operating_income` | `quarterly_financials` | 營業利益（千元） |
| `net_income` | `quarterly_financials` | 本期淨利（千元） |
| `revenue_mom` | `monthly_revenue` | 月增率 % |
| `open / high / low / volume` | `daily_prices` | 完整 OHLCV |

新增分析功能時可直接從這些欄位取值，不需重新爬蟲。
