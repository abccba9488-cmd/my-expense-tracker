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

## 事故紀錄（2026-10-26 從 CLAUDE.md 移入）

CLAUDE.md 只留一行重點，完整經過與修法在這裡。

### `_SUMMARY_SQL` 冷查詢在大型 DB 上可能耗時數分鐘、甚至逾時（2026-07-16 發現並修復）

`daily_prices` 成長到 622 萬筆後，`ma20`/`ma60`/`ma120`/`ma240` 這四個「每檔股票各自關聯子查詢」的寫法等於每次冷查詢要跑約 8,000 次獨立查詢；問題在本機被 `database.py` 原本統一套用的極小 SQLite `cache_size`（2MB）+ `temp_store=FILE` 放大到誇張的程度（temp b-tree 落地磁碟，大量小額 I/O）——同一份查詢在本機曾實測「15 分鐘沒跑完直接被中止」，改成本機用大快取後降到約 25 秒。兩處修復：
1. **`app.py`**：`_summary_rebuild_lock`（`threading.Lock`）包住快取重建區塊，做成 single-flight——快取過期時若同時有多個請求進來，只有一個真的觸發昂貴查詢，其餘等鎖釋放後直接吃剛寫好的快取（避免 cache stampede）。
2. **`database.py`**：SQLite PRAGMA 依 `sys.platform` 分流（`_IS_CLOUD = sys.platform != 'win32'`）——非 Windows 分支（原 Zeabur 容器用，雲端已停用）維持 `mmap_size=0`／`cache_size=-2000`／`temp_store=FILE`（避免容器 OOM）；本機（Windows）用 `cache_size=-131072`（~128MB）+ `temp_store=MEMORY`。

頻繁重啟本機伺服器每次都會清空記憶體內的 `_summary_cache`，下一個請求就會撞到冷查詢——日常開發改動非資料庫相關程式碼時，能不重啟就不重啟。

### `FINMIND_TOKEN` 沒設定的靜默失敗（2026-07-16）

本機常駐服務（`autostart_server.bat`）從某次重啟後就沒帶到這個環境變數，導致 5 個 `finmind_*` 任務**每天** 17:00 都準時觸發、但每次都馬上失敗（`crawler_logs` 訊息一模一樣：`FINMIND_TOKEN environment variable not set`），`institutional_trades`/`holding_concentration` 因此在沒人發現的情況下卡在舊資料整整 11 天——`director_holdings`（TWSE/TPEX OpenAPI，不需要金鑰）仍然每天成功，容易誤以為「排程有在跑就是正常」。

修法：`app.py` 模組層級啟動時檢查 `os.environ.get('FINMIND_TOKEN')`，沒設定就寫一筆 `finmind_token_check` / `failed` 的 `crawler_logs`，讓 ⚙ 爬蟲狀態面板一開就看得到。本機修復：`setx FINMIND_TOKEN "your-token"`（永久使用者環境變數，`set` 只在當次終端機有效）之後**重啟**本機服務——環境變數只在程序啟動當下被讀取；已經開著的終端機/PowerShell session 也不會自動拿到新值，要嘛開新 session，要嘛在該 session 內 `$env:FINMIND_TOKEN = [Environment]::GetEnvironmentVariable("FINMIND_TOKEN","User")` 後再啟動。

### `crawl_finmind_*` 沒有回填缺口的機制，`_finmind_watchdog` 也補不了

這幾個函式每次只抓「傳入的那一天」（`start_date=end_date=iso`），`_finmind_watchdog` 的邏輯是「今天還沒成功就補跑今天」，不會回頭補過去缺的日子。上面那次 token 斷線 11 天修好後，`institutional_trades` 仍停在斷線前最後一天（watchdog 只補當天，且當天常因 T+1 delay 還是 0 筆）——實際造成 `gutai_bull`/`gutai_bear` 全市場 0 檔通過（近5日窗口只剩1筆舊資料，不可能滿足「至少2日」門檻）。當時手動逐日呼叫 `crawler.crawl_finmind_institutional(date_str)` 補齊 7 個交易日才修復。再發生多日斷線一樣要手動回填。

### watchdog 只檢查「有沒有成功 log」，不檢查資料完不完整（2026-08-15）

08-14 17:00 的 `finmind_institutional` 準時觸發且回報 `success`，但當下 FinMind 資料還沒補齊，bulk 回應只有 784 檔（正常約 1870~1890 檔），watchdog 看到 success 就不再補跑。修法：`POST /api/stocks/<code>/institutional-trades/refresh`（任何登入使用者可觸發，個股詳情頁「三大法人進出與持股」卡片右上角按鈕），重新呼叫近 5 個曆日的 `crawl_finmind_institutional()`——`INSERT OR REPLACE` 冪等，非交易日也安全拿到 0 筆。這是手動補救，不是自動化機制。

### `crawl_monthly_revenue()`/`crawl_quarterly_financials()` 卡在 `running` 永遠不結束（2026-08-05 診斷＋修復）

這兩個任務逐檔迴圈跑約 2000 檔、正常約 10 分鐘。中途若 process 被凍結（實例：電腦半夜睡眠，Flask 背景執行緒整個凍結；醒來卡在爛掉的 socket 上，`_post()` 的 `timeout=15` 沒觸發，因為計時器也一起停了），迴圈就停在某一檔不動、不丟例外——`crawler_logs` 只留一筆 `running`，直到重啟伺服器才清掉。曾連續三晚（08/01–08/03）卡住，月營收 3 天沒更新才被發現。修法：`_monthly_revenue_watchdog`/`_quarterly_watchdog`——「今天沒有 success」且「沒有 40 分鐘內剛啟動的 running」兩者都成立才補跑（避免誤殺真的還在跑的那 10 分鐘）。

### 本機 `app.run()` 沒開 `threaded=True` 讓網站看起來當機（2026-08-05 修復）

Werkzeug 開發伺服器預設單執行緒。背景同時跑 `quarterly`（逐檔迴圈）+ `finmind_data`（含吃 CPU 的 `compute_expert_scores()`）時，首頁／`/api/market/summary` 直接卡到逾時，看起來像當機，其實 process 沒死、只是排不到。已在 `app.run(...)` 加 `threaded=True`。（原 Zeabur 的 gunicorn `--workers 1 --threads 4` 沒這問題；雲端已停用。）

### 本機開機自動啟動的做法

`autostart_server.bat` 是 `start.bat` 的背景版（只確保 Flask＋APScheduler 在跑，不開瀏覽器），透過 Windows「啟動」資料夾（`shell:startup`）的捷徑在登入時啟動；捷徑不在 git 內，換機器要重建。原本想用 `schtasks`/`Register-ScheduledTask`，但兩者都要系統管理員權限，改用免提權的啟動資料夾捷徑。
