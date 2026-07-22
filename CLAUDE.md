# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概述

**飆股網** — 台灣上市櫃股票分析網站，本機執行，單一 Python Flask 後端 + 單頁 HTML 前端。無建置工具，無測試框架。

## 啟動與停止

```bat
start.bat    ← 雙擊，自動開啟瀏覽器 http://localhost:5000（已在跑則直接開瀏覽器）
stop.bat     ← 雙擊停止伺服器與 ngrok
ngrok.bat    ← 雙擊啟動 Flask + ngrok 公開 tunnel（外部連線用）
```

`start.bat` 內部透過 `_server.bat` 啟動 Flask，使用 `python.exe`（有 cmd 視窗，可看 log）。
`stop.bat` 同時 kill Flask（port 5000）與 `ngrok.exe`。

直接執行（可看 log）：
```
C:\Users\user\anaconda3\python.exe app.py
```

Python 直譯器固定為 `C:\Users\user\anaconda3\python.exe`（Python 3.13），不使用虛擬環境。

安裝依賴：
```
C:\Users\user\anaconda3\Scripts\pip.exe install -r requirements.txt
```

**本機開機自動啟動**：`autostart_server.bat` 是 `start.bat` 的背景版——只確保 Flask（含內建 APScheduler 排程）在跑，不會跳出瀏覽器分頁。透過在 Windows「啟動」資料夾（`shell:startup`，使用者層級、不需要系統管理員權限）放一個指向它的捷徑，登入時自動背景啟動；這個捷徑本身不在 git 版控內，換一台機器要重設的話再重建一次即可。原本想用 `schtasks`/`Register-ScheduledTask` 註冊工作排程器，但兩者都需要系統管理員權限，改用啟動資料夾捷徑這個免提權做法。

**`.bat` 檔案裡不要放中文註解**：Windows `cmd.exe` 解析批次檔時對多位元組字元（中文）處理不可靠，中文 `rem` 註解可能被錯誤斷行、導致後面的文字被當成指令執行、噴出「找不到指令」的錯誤（親身踩過一次）。這個專案既有的 `.bat` 檔案本來就沒有中文註解，新增 `.bat` 檔案時延續這個慣例，需要說明就用英文或直接寫在 CLAUDE.md 裡。

## 外部連線（ngrok）

`ngrok.exe` 放在專案根目錄，authtoken 已設定於 `%APPDATA%\Local\ngrok\ngrok.yml`。

雙擊 `ngrok.bat` 即可取得公開 `https://xxxx.ngrok-free.app` 網址，免費版每次重啟網址會變。

**安全限制**：`POST /api/crawler/run/<task>` 僅允許 `127.0.0.1` / `::1` 呼叫，或是已用管理員帳號登入的 session；其他外部連線會收到 403。

## 架構

```
app.py          Flask app + REST API endpoints + 初始化入口
crawler.py      所有爬蟲函式（TWSE / TPEX / MOPS / FinMind / 董監持股 OpenAPI）
finmind_client.py  FinMind API 薄封裝（crawler.py 的 crawl_finmind_* 函式呼叫）
technical.py    純 Python 技術指標（EMA/MACD/RSI/KD），達人選股股泰規則用
experts.py      達人選股計分引擎（8 套規則），見「達人選股」章節
scheduler.py    APScheduler 排程（BackgroundScheduler，Asia/Taipei）
database.py     SQLAlchemy models + SQLite 設定 + migration
templates/index.html   單頁前端（DataTables + Chart.js，CDN）
static/css/style.css   CSS 設計代幣（dark/light theme）
static/js/app.js       前端邏輯（vanilla JS）
static/manifest.json   PWA manifest
static/sw.js           PWA service worker
static/img/icons/      PWA 圖示（generate_pwa_icons.py 產生）
data/stocks.db         SQLite 資料庫（自動建立）
```

### 資料流

1. `app.py` 啟動時呼叫 `init_db()` 建表及 migration、啟動排程器、若 DB 空則觸發 `_initial_crawl()`
2. 爬蟲函式全部在 `crawler.py`，透過 `_session`（`requests.Session(verify=False)`）發出請求
3. 前端呼叫 `/api/market/summary` 取得所有股票的最新快照（JOIN 四張表），資料已在瀏覽器端，飆股篩選與預估計算在 JS 完成

### 共用 SQL 查詢

`_SUMMARY_SQL`（`app.py`）是核心查詢，JOIN `stocks` + 最新 `daily_prices` + 最新 `monthly_revenue` + 最新 `quarterly_financials`，包含 `yeps` CTE（加總同年四季 EPS）用於 Q4 本益比計算。欄位順序固定，`_row_to_dict()` 依索引轉換。

**新增欄位時需同步修改六處**：`_SUMMARY_SQL` → `_row_to_dict` → CSV 端點 → `index.html <th>` → `app.js rows` 陣列 → `app.js columnDefs`。若欄位可由現有欄位計算（如 `price_diff`），可跳過 SQL 修改，只在 `_row_to_dict` 計算後加入 dict。

### 效能快取

- **Server side**：`_summary_cache`（`app.py`）快取 JSON 字串，TTL 30 分鐘（2026-07-16 從 5 分鐘拉長，原因見下方）。`_run_bg()` 在背景任務完成後自動呼叫 `_invalidate_summary_cache()`。
- **Gzip**：`compress_response` after_request handler 自動壓縮 JSON / HTML / CSS / JS 回應。
- **Client side**：`app.js` 以 `localStorage`（key `bao_sum_v1`，TTL 5 分鐘）做 stale-while-revalidate；頁面重載時先渲染快取，背景靜默更新。

**`_SUMMARY_SQL` 冷查詢在大型 DB 上可能耗時數分鐘、甚至逾時（2026-07-16 發現並修復）**：`daily_prices` 成長到 622 萬筆後，`ma20`/`ma60`/`ma120`/`ma240` 這四個「每檔股票各自關聯子查詢」的寫法（見下方欄位索引說明）等於每次冷查詢要跑約 8,000 次獨立查詢；問題在本機被 `database.py` 原本統一套用的極小 SQLite `cache_size`（2MB）+ `temp_store=FILE` 放大到誇張的程度（temp b-tree 落地磁碟，大量小額 I/O）——同一份查詢在本機曾實測「15 分鐘沒跑完直接被中止」，改成本機用大快取後降到約 25 秒。兩處修復：
1. **`app.py`**：`_summary_rebuild_lock`（`threading.Lock`）包住快取重建區塊，做成 single-flight——快取過期時若同時有多個請求進來，只有一個真的觸發昂貴查詢，其餘等鎖釋放後直接吃剛寫好的快取，不會每個請求各自重跑一次（cache stampede）。
2. **`database.py`**：SQLite PRAGMA 依 `sys.platform` 分流（`_IS_CLOUD = sys.platform != 'win32'`）——雲端（Zeabur/Linux）維持原本 `mmap_size=0`／`cache_size=-2000`／`temp_store=FILE` 不變（避免容器 OOM，這組設定本來就是為了解決那個問題）；本機（Windows）改用 `cache_size=-131072`（~128MB）+ `temp_store=MEMORY`。

**重點**：頻繁重啟本機伺服器（改 code 後重啟）每次都會清空記憶體內的 `_summary_cache`，下一個請求就會撞到冷查詢——這是這個問題在本機格外容易被踩到的原因，日常開發改動非資料庫相關程式碼時，能不重啟就不重啟。

## 資料庫 Schema

| 表 | 主鍵 | 單位備註 |
|----|------|---------|
| `stocks` | `code` | market: TWSE \| TPEX |
| `daily_prices` | `(stock_code, date)` | volume 單位：股；`per`/`pbr`/`dividend_yield` 來自 FinMind，達人選股用 |
| `monthly_revenue` | `(stock_code, year, month)` | revenue 千元；`start_price` = 首次寫入當天收盤價，月份切換時才更新；`turnaround_signal` = 潛在虧轉盈候選旗標，每次爬蟲都重算（見下方說明） |
| `quarterly_financials` | `(stock_code, year, quarter)` | revenue/income 千元；eps 元/股；**各季獨立值**（Q4 已非累計） |
| `institutional_trades` | `(stock_code, date)` | 三大法人買賣超（股），FinMind，達人選股用 |
| `holding_concentration` | `(stock_code, date)` | 股權分散表（週資料），FinMind，達人選股用 |
| `financial_extra` | `(stock_code, year, quarter)` | 資產負債表/現金流量表/毛利項目（千元），FinMind，獨立於 MOPS 來源的 `quarterly_financials` |
| `dividend_policy` | `(stock_code, event_date)` | 逐筆股利分派事件（非年度加總），FinMind |
| `dividend_fill_events` | `(stock_code, ex_date)` | 除權息事件 + 填息判斷，FinMind |
| `director_holdings` | `(stock_code, year_month)` | 董監持股比例，TWSE/TPEX OpenAPI（非 FinMind） |
| `broker_trades` | `(stock_code, date, broker_id)` | 券商分點單日買賣超（股），FinMind `TaiwanStockTradingDailyReport`，見下方「券商分點進出」章節 |
| `expert_scores` | `(stock_code, expert_key)` | 達人選股每套規則最新一次計分快取；`entered_at`/`transition` 是唯二跨執行延續（不覆寫）的欄位，見下方「達人選股」章節 |
| `users` | `id` | 會員帳號，`password_hash` 用 werkzeug |
| `watchlists` | `id` | 屬於某 `user_id`，可多個 |
| `watchlist_stocks` | `(watchlist_id, stock_code)` | 自選股關聯表 |
| `messages` | `id` | 全站留言板；`user_id`/`username`/`content`/`created_at` |
| `crawler_logs` | `id` | status: running \| success \| failed |
| `announcements` | `id` | UniqueConstraint(`stock_code`, `seq_no`)；自結公告爬蟲結果，見下方「自結公告」章節 |
| `stock_ai_analysis` | `stock_code` | 單檔股票最新一次 AI 估值分析快取，見下方「AI 個股分析」章節 |
| `schema_migrations` | `name` | 記錄已執行的 migration，防止重複執行 |

`monthly_revenue` / `quarterly_financials` 皆有 `updated_at`（`onupdate=datetime.now`），爬蟲在資料**實際變動**時才手動更新此欄位（用於 `/api/updates/today` 判斷「今日更新」清單）。

`init_db()` 目前執行的 migrations（均用 `schema_migrations` 防重複，或用 try/except ALTER 防重複）：
1. `ALTER TABLE monthly_revenue ADD COLUMN start_price REAL`
2. `ALTER TABLE monthly_revenue / quarterly_financials ADD COLUMN updated_at DATETIME`
3. 補填歷史 `start_price` 空值
4. `q4_annual_to_individual`：將 Q4 從年累計值減去 Q1+Q2+Q3，還原為個別季數值
5. `ALTER TABLE announcements ADD COLUMN price_at_announce / prior_year_eps / estimated_annual_eps REAL`
6. `ALTER TABLE announcements ADD COLUMN ai_rating VARCHAR(30) / ai_analysis TEXT`
7. `clear_old_announcements`：一次性清空舊版 AI 評級設計留下的 `announcements` 資料（schema 語意不同，只清資料不動欄位）
8. `ALTER TABLE monthly_revenue ADD COLUMN turnaround_signal INTEGER`
9. 回填現有每檔股票**最新一筆** `monthly_revenue` 的 `turnaround_signal`（用既有 `quarterly_financials` 資料算，不用重新爬）——新增欄位時舊資料全是 NULL，要等下次爬蟲跑才會重算，這個一次性回填讓欄位上線當下就有正確值，不用等
10. `ALTER TABLE daily_prices ADD COLUMN per / pbr / dividend_yield REAL`（達人選股，FinMind `TaiwanStockPER`）
11. `ALTER TABLE expert_scores ADD COLUMN entered_at DATE / transition VARCHAR(10)`（股泰多方/空方訊號的入榜日期＋翻轉標記，見「達人選股」章節）
12. `backfill_price_change`：一次性回填 `daily_prices.change`/`change_pct`（某段期間曾因（已修復的）程式問題留空，用該股前一交易日收盤價回推補上，OHLCV 本身沒問題）
13. `fix_finmind_decumulate`：修復 `financial_extra` 被舊版 `crawl_finmind_financials()` 錯誤處理的 Q4/累計值問題（詳見 `database.py` 的 `_fix_finmind_decumulate()` docstring）——損益表三欄（`gross_profit`/`cost_of_goods_sold`/`pretax_income`）FinMind 每季給的本來就是單季值，舊版誤當成「Q4=年度累計」多扣一次 Q1+Q2+Q3，修復前全庫 86% 的 Q4 毛利率是負的；現金流量表三欄（`operating_cash_flow`/`interest_expense`/`capex`）依台灣官方揭露慣例才是「年初至今累計」，舊版從未處理 Q2/Q3、Q4 又用錯減項。修復後兩組欄位的處理邏輯完全對調（前者不調整、後者逐季減去前一季），`crawl_finmind_financials()` 已同步修正，這個 migration 只補救歷史資料

## _SUMMARY_SQL 欄位索引（r[0]–r[22]）

```
0=code, 1=name, 2=market, 3=industry,
4=close, 5=change_pct, 6=price_date,
7=revenue, 8=revenue_yoy, 9=rev_year, 10=rev_month,
11=eps, 12=eps_year, 13=eps_quarter,
14=qf_revenue, 15=pe_ratio, 16=start_price, 17=ma20, 18=turnaround_signal,
19=ma60, 20=ma120, 21=ma240, 22=dividend_yield
```

`dividend_yield`（2026-07-16 新增）：跟 `per`/`pbr` 一樣來自 FinMind、存在 `daily_prices`，用「抓最近一筆這個欄位實際有值的日期」而非嚴格最新日期 JOIN（`ldy` CTE），避開估值爬蟲落後股價爬蟲時的空值問題（同 `experts.py` `_build_context()` 既有的處理方式）。前端只在達人選股 7 個基本面規則分頁（`flag888_1`–`4`／`guyu`／`laoniu`／`momentum_guard`）顯示這個欄位，`gutai_bull`/`gutai_bear`（技術面訊號、跟股利無關）不顯示。

`ma20`/`ma60`/`ma120`/`ma240`：各自以相關子查詢取該股最近 20／60／120／240 筆 `daily_prices.close`（`ORDER BY date DESC LIMIT N`，吃 `ix_dp_code_date` 索引，不用整表掃描）算出的簡單移動平均。前端四個表格（主表格／飆股清單／自選股／達人選股列表）最後一欄「**甜蜜點**」皆呼叫 `app.js` 的 `sweetSpotCell(s)` 顯示此值——**紅＝接近 `ma20`、黃＝接近 `ma60`、綠＝接近 `ma120`**：股價距離這三條均線正負 3% 內時儲存格變色＋🔔 圖示提示（`_SWEET_SPOT_TIERS` 陣列依序 20→60→120 檢查，同時接近多條時優先顯示天期較短的那條），這三條是「支撐/壓力測試」語意。**紫＝ `ma240`，判定規則刻意不對稱**（不是 ±3%）：`(close - ma240) / ma240 <= 0.03`，股價只要在 `ma240` 之下（不論低多少都算）、或高於 `ma240` 但漲幅不超過 3%，就顯示紫色；漲超過 3% 以上就不顯示——把 240 日均線當成「長期價值區」而非單純的支撐/壓力測試，只有「跌破或剛站上」才算，漲多了就不算。四條均線都不符合但至少有 `ma20` 時顯示樸素數值，完全沒有均線資料才顯示「—」。這欄原本只看 `ma20`（單色黃底），2026-07-12 改版加入 `ma60`/`ma120` 並更名「甜蜜點」，`ma20Cell()` 已重新命名為 `sweetSpotCell()`；2026-07-13 加入 `ma240` 並確立「20/60/120 對稱±3%、240 不對稱」這個最終版本（中途曾短暫改成四條都用不對稱規則，隨即依需求改回）。

`turnaround_signal`：**不是即時計算，是 `crawl_monthly_revenue()`（`crawler.py`）每次爬到新月營收時直接算好存進 `monthly_revenue` 表的**。邏輯：該股最新一季 `quarterly_financials.eps < 0`（還在虧損）**且**本月 `revenue_yoy >= 20`（跟營收飆股用同一個門檻）→ 寫入 1，否則 0；每次爬蟲都重算覆寫（不像 `start_price` 只在新增時寫一次）。前端 `app.js` 的 `turnaroundCell(s)` 為真時顯示 🔥 圖示、假則顯示「—」，**純圖示不塗滿底色**（跟 `sweetSpotCell` 的變色不同）。四個表格（主表格／飆股清單／自選股／達人選股列表）都有這欄；**飆股清單表格（`#star-table`）這欄會永遠顯示「—」**——`calcEst()` 要求 `eps > 0` 才會回傳值，飆股清單本身的篩選邏輯已排除所有虧損股，使用者要求三表一致才加上，不是邏輯漏洞。內容只有「—」/🔥 太窄，三個表格（主/飆股/自選）的這一欄都用 `columnDefs` 的 `width: '64px'` 固定寬度，避免 DataTables 自動欄寬把標題擠出欄位（曾經發生過對不齊的問題）。

## 爬蟲資料來源

| 資料 | 端點 | 格式 |
|------|------|------|
| 股票清單 | `isin.twse.com.tw/isin/C_public.jsp?strMode=2/4` | Big5 HTML，只取 `^\d{4}$` 代號 |
| TWSE 每日股價 | `twse.com.tw/exchangeReport/MI_INDEX?type=ALL` | JSON `tables[]`（2025+ 新格式）；漲跌方向為 HTML `color:red/green` |
| TPEX 每日股價 | `tpex.org.tw/.../stk_wn1430_result.php?se=AL` | JSON `tables[0].data`（2025+ 新格式）；volume 單位為股 |
| 月營收 | `mops.twse.com.tw/mops/api/t05st10_ifrs` | POST JSON；per-company；`data[0][1]`=當月營收，`data[3][1]`=年增率 |
| 季財報 EPS | `mops.twse.com.tw/mops/api/t164sb04` | POST JSON；`reportList` 陣列，關鍵字比對列標籤取值 |
| 自結公告 | `mopsov.twse.com.tw/mops/web/ajax_t05st02` | POST form（TYPEK=all, year/month/day ROC）→ HTML；單次回應即含當天全部公告的完整主旨/說明（藏在隱藏 `<input>` 欄位裡），**不需要、也不要額外發詳情頁請求**（細節見「自結公告」章節） |

**SSL 注意**：TWSE/TPEX/MOPS 憑證有問題，`crawler.py` 用 `_session.verify = False` 統一處理。所有請求必須走 `_get()` / `_post()` 包裝函式，不可直接呼叫 `_session.get/post` 或裸的 `requests`。

**防爬蟲機制**（`crawler.py` 頂部）：
- `_get()` / `_post()`：統一入口，每次請求隨機挑選 UA、帶完整瀏覽器 headers（含 `Sec-Fetch-*`、`Origin`、`Cache-Control`）、429/5xx 與連線層級例外自動重試最多 3 次
- UA 池含 Chrome 136、Firefox 138、Safari 17、Edge 136 共 9 組，定期輪替
- `_jitter(base)`：`time.sleep(base × random(0.7, 1.6))`，消除固定間隔特徵
- 每 80 次請求清除一次 session cookie
- **`Accept-Encoding` 不可加 `br`**：Zeabur 容器未安裝 `brotli`，若伺服器回傳 Brotli 壓縮內容會導致 `resp.json()` 解析失敗，整批資料變成 0 筆但 task 仍顯示 success。只用 `gzip, deflate`
- TWSE/TPEX JSON 解析失敗或 `stat != 'OK'` 時會記錄 `logger.warning`（含狀態碼與回應大小），方便從 Zeabur Runtime Logs 排查

月營收爬蟲在新增記錄時，會查 `daily_prices` 最新收盤價寫入 `start_price`；更新既有記錄時不修改 `start_price`。`turnaround_signal`（虧轉盈候選旗標）則相反，新增與更新都會重算覆寫，見上方「_SUMMARY_SQL 欄位索引」說明。

季財報爬蟲：抓到 Q4 時，從 DB 取出同年 Q1/Q2/Q3 相減後再存入，確保存的是個別季數值。

## 排程（APScheduler）

| 任務 | 觸發時間（Asia/Taipei） |
|------|---------|
| 股票清單 | 每週日 01:00 |
| 每日股價 | 週一〜五 14:00 與 15:00（各跑一次，避免單次失敗漏抓） |
| 每日股價（watchdog） | 週一〜五 14:00–17:00 每 30 分鐘檢查一次，若當天還沒有成功的 `daily_price` log 就補爬一次（`_daily_price_watchdog`，啟動時也會立即跑一次） |
| 月營收 | 每天 23:00（爬上個月；部分公司公布較晚，每天重抓直到有資料） |
| 自結公告 | 週一〜五 05:00（非尖峰，爬前一交易日的 MOPS 重大公告） |
| 自結公告（測試用，**暫時性**） | 每 30 分鐘重爬「今天」的公告（`_announcements_test_job`），讓當日新公告不用等隔天 05:00。改成從清單頁直接解析（無需逐筆詳情頁請求）後單次執行只需數秒，不再有效能負擔；要調整頻率或正式移除這個 job 前先問使用者 |
| Q1 | 5 月每天 23:00（公告期限 5/15） |
| Q2 | 8 月每天 23:00（公告期限 8/14） |
| Q3 | 11 月每天 23:00（公告期限 11/14） |
| Q4 | 隔年 3 月每天 23:00（公告期限 3/31） |
| 達人選股（FinMind 增量 + 重算分數） | 每天 17:00（股價爬蟲與 watchdog 之後），見「達人選股」章節 |
| 達人選股（FinMind，watchdog，2026-07-16 新增） | 週一〜五 17:00 起每 30 分鐘檢查一次，若當天還沒有成功的 `finmind_institutional` log 就補跑整個 `_finmind_job()`（`_finmind_watchdog`，啟動時也會立即跑一次），跟 `_daily_price_watchdog` 同一個模式，補的是「程序在 17:00 當下沒在跑」這種情況 |
| 達人選股（financial_extra） | 與官方季報同月份、每天 23:30（比官方季報 job 晚 30 分） |
| 券商分點進出（2026-07-22 新增） | 週一〜五 17:30（達人選股 FinMind job 之後），只對目前有人自選的股票抓當天，見「券商分點進出」章節 |

**注意**：APScheduler 的「下次執行時間」在 `sched.start()` 當下計算，若當天排程時間已過（例如 worker 因重新部署在 14:00 後重啟），當天的每日股價排程會被跳過、不會補跑。`app.py` 模組層級已加入**啟動時自動補跑**機制：若當天（平日且時間 ≥14:00）尚無成功的 `daily_price` log，啟動時自動觸發一次 `crawler.crawl_daily_prices`。

**`FINMIND_TOKEN` 沒設定是一個容易忽略的靜默失敗陷阱**：2026-07-16 發生過本機常駐服務（`autostart_server.bat`）從某次重啟後就沒帶到這個環境變數，導致 5 個 `finmind_*` 任務**每天** 17:00 都準時觸發、但每次都馬上失敗（`crawler_logs` 裡訊息一模一樣：`FINMIND_TOKEN environment variable not set`），`institutional_trades`/`holding_concentration` 因此在使用者沒發現的情況下卡在舊資料整整 11 天——`director_holdings`（TWSE/TPEX OpenAPI，不需要金鑰）仍然每天成功，容易誤以為「排程本身有在跑就是正常」而沒注意到部分子任務其實都在失敗。修法：`app.py` 模組層級啟動時檢查 `os.environ.get('FINMIND_TOKEN')`，沒設定就寫一筆 `finmind_token_check` / `failed` 的 `crawler_logs`，讓 ⚙ 爬蟲狀態面板一開就看得到，不用等到手動比對各表 `MAX(date)` 才發現。本機修復方式：`setx FINMIND_TOKEN "your-token"`（永久使用者環境變數，`set` 只在當次終端機有效）之後**重啟**本機服務（環境變數只在程序啟動當下被讀取，已經在跑的舊程序不會生效；已經開著的終端機/PowerShell session 也不會自動拿到新值，要嘛開一個全新的 session，要嘛在該 session 內手動 `$env:FINMIND_TOKEN = [Environment]::GetEnvironmentVariable("FINMIND_TOKEN","User")` 後再啟動）。

**`crawl_finmind_institutional()`（以及其餘 4 個 `crawl_finmind_*`）沒有回填缺口的機制，`_finmind_watchdog` 也補不了**：這幾個函式每次只抓「傳入的那一天」（`start_date=end_date=iso`，見上方「Bulk 模式」說明），`_finmind_watchdog` 的邏輯是「今天還沒成功就補跑今天」，並不會回頭補「過去缺的那幾天」。2026-07-16 那次 token 斷線 11 天的事故修好 token 後，`institutional_trades` 仍然停在斷線前最後一天，因為 watchdog 觸發的補跑只補了「當天」（且股價公布通常有 T+1 delay，當天往往還是 0 筆）——實際造成 `gutai_bull`/`gutai_bear` 全市場 0 檔通過（近5日窗口只剩1筆舊資料，天生不可能滿足「至少2日」門檻）。當時是手動逐日呼叫 `crawler.crawl_finmind_institutional(date_str)` 補齊缺的 7 個交易日才修復。若未來又發生多日斷線，同樣需要手動回填，不會自動復原。

手動觸發：`POST /api/crawler/run/<task>`（僅限 localhost 或 admin 登入）；task 值：`stock_list` / `daily_price` / `monthly_revenue` / `quarterly` / `announcements` / `init` / `finmind_data` / `broker_trades` / `director_holdings` / `expert_scores`。季報觸發自動判斷「最近已公告季度」，可用 `?year=&quarter=` 覆蓋；公告可用 `?date=YYYYMMDD` 覆蓋日期，`?limit=N` 只處理清單前 N 筆做小規模測試（**測試專用，正式排程不要帶這個參數**，否則當天只會處理一部分公告；現在整個流程只需一次 HTTP 請求，正常情況下不需要這個參數來省時間，純粹是想看少量範例輸出時用）。`finmind_data` 等同 `_finmind_job()`（5 個 FinMind 增量函式 + 董監持股 + 重算 `expert_scores`）；`expert_scores` 只重算分數不重新爬資料，改規則邏輯後想立即看結果時用。

## REST API

```
GET  /api/market/summary           所有股票快照（_SUMMARY_SQL，有 5 分鐘 server cache）
GET  /api/market/summary.csv       CSV 下載（UTF-8 BOM）
GET  /api/stocks/<code>           股票基本資料（代號/名稱/市場/產業）
GET  /api/stocks/<code>/prices     個股歷史股價（?days=90）
GET  /api/stocks/<code>/revenue    個股月營收
GET  /api/stocks/<code>/financials 個股季財報
GET  /api/stocks/<code>/ai-analysis  讀取該股快取的 AI 分析結果（任何人可讀，不會觸發新分析）
POST /api/stocks/<code>/ai-analysis  觸發一次全新 AI 分析（僅管理員；同步執行，會產生 OpenRouter 費用）
GET  /api/stocks/<code>/expert-scores  該股在 8 套達人選股規則下的最新分數/明細
GET  /api/stocks/<code>/broker-trades  券商分點單日買賣超近N天（?days=30），只有被自選過的股票才有資料，見「券商分點進出」章節
GET  /api/experts                  8 套達人選股規則清單（標籤、通過檔數/總檔數）
GET  /api/experts/<key>            該規則下依分數排序的完整清單（含每檔 breakdown）
GET  /api/stats                    DB 統計（stocks/prices/revenues/quarterly 筆數）
GET  /api/crawler/status           最近 30 筆爬蟲 log
POST /api/crawler/run/<task>       手動觸發爬蟲（僅限 localhost 或 admin 登入）
GET  /api/updates/today            今日更新摘要（股價日期 + 月營收/季財報清單 + 自結公告今日新增筆數）

GET  /api/auth/me                  取得目前登入使用者
POST /api/auth/register            註冊（自動通過，建立 session）
POST /api/auth/login               登入
POST /api/auth/logout              登出

GET  /api/watchlists               取得目前使用者所有自選股清單（含 codes）
POST /api/watchlists               新增清單
PUT  /api/watchlists/<id>          重新命名
DELETE /api/watchlists/<id>        刪除
POST /api/watchlists/<id>/stocks   加入股票 {code}
DELETE /api/watchlists/<id>/stocks/<code>  移除股票

GET  /api/messages                 留言板列表（最新 100 筆，含 can_delete 旗標）
POST /api/messages                 發表留言（需登入，內容上限 500 字）
DELETE /api/messages/<id>          刪除留言（本人或 ADMIN_USERNAME）

GET  /api/announcements/today      自結公告清單（近 7 天，依日期/時間降序，含 content 全文供前端 modal 使用）

GET  /api/admin/users              會員列表 + 各自自選股清單數（僅 ADMIN_USERNAME）
DELETE /api/admin/users/<id>       刪除會員（連同其自選股清單與留言；無法刪除管理員自己）
```

`ADMIN_USERNAME`（`app.py`）為留言板管理員帳號，可刪除任何人的留言。

## 部署（Zeabur）

正式網址：`https://stock-market.zeabur.app/`

**關鍵設定：**
- Zeabur 使用 gunicorn 啟動，`__main__` 區塊不執行。`init_db()`、`sched.start()`、自動爬蟲偵測已移至模組層級，匯入時即執行。
- 需在 Zeabur 控制台設定 **Persistent Volume** 掛載至 `/app/data`，否則重啟後 SQLite 資料消失。
- `SESSION_COOKIE_SAMESITE='Lax'` 確保 HTTPS 環境下 session cookie 正常運作。
- `Procfile`：`gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 300`
  - **必須帶 `--bind 0.0.0.0:$PORT`**，否則 gunicorn 預設綁定 `127.0.0.1:8000`，Zeabur 偵測不到 port 會回 502
  - **單一 worker**：避免每個 worker 各自啟動一份 APScheduler（重複觸發排程爬蟲）與 SQLite 連線池
  - `--timeout 300`：`/api/market/summary` 在大型 DB 上冷啟動查詢耗時較長，避免被 gunicorn 逾時 SIGKILL
- `database.py` 連線時設定 SQLite PRAGMA（`mmap_size=0`、`cache_size=-2000`、`temp_store=FILE`），避免大型 DB 的 mmap 把記憶體推爆容器限制——**這組設定只套用在雲端（`sys.platform != 'win32'`）**，本機用較大的 cache/記憶體 temp_store，詳見上方「效能快取」章節
- 服務記憶體限制設定在 Zeabur「設定 → 資源限制」（Mi），與帳號方案的總額度是兩回事

**雲端初始化 DB（跑 backfill 的替代方案）：**
`fetch_db.py` 從 GitHub Release `db-v1` 下載 DB 快照（800 MB，含 15 年資料）：
```bash
python fetch_db.py   # 在 Zeabur 終端機執行
```
快照來源：`github.com/abccba9488-cmd/my-expense-tracker/releases/tag/db-v1`

**縮減 DB 大小（解決大型 DB 在低記憶體環境的 OOM/逾時）：**
`trim_db.py` 刪除 `daily_prices` 中 5 年前的資料並執行 `VACUUM` 壓縮檔案：
```bash
python trim_db.py   # 在 Zeabur 終端機執行；VACUUM 需要獨佔存取，必要時先暫停服務
```

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

## PWA

- `/manifest.json`、`/sw.js`：`app.py` 透過 `send_from_directory('static', ...)` 從根路徑提供（scope 需為 `/`，放在 `/static/` 下無法註冊根 scope 的 service worker）。
- `static/sw.js`：只快取 `/static/` 下的資源（cache-first），`/api/` 一律直連網路，確保資料新鮮度。
- **`app.js`/`style.css` 版本不一致問題（已徹底解決，不需手動操作）**：早期作法是改 JS/CSS 後手動把 `CACHE_NAME` 版本號往上加一，但這個步驟很容易忘記（已經發生過兩次：使用者改完功能後看到 DataTables「Requested unknown parameter」這類欄位數不一致的錯誤，因為 SW 還在用快取住的舊版 JS 配上剛部署的新版 `index.html`）。現在改成根本解法：`app.py` 的 `inject_asset_version()`（一個 `@app.context_processor`）用 `os.path.getmtime()` 讀取 `static/js/app.js` / `static/css/style.css` 的檔案修改時間當版本號，`templates/index.html` 的 `<script>`/`<link>` 網址帶上 `?v={{ asset_version(...) }}`。**只要檔案內容變了，網址就自動變了**，SW 的快取永遠對不到舊網址、一定會發新的 network fetch，不再需要記得手動升版號。`static/sw.js` 的 `SHELL_ASSETS` 因此**故意不放** `app.js`/`style.css`（放了也沒用，precache 的是沒帶版本號的舊網址，瀏覽器實際要的是有版本號的新網址，兩者對不上）。`CACHE_NAME` 仍保留，給沒走版本化網址的資源（目前只有 icons）用，這些檔案改變頻率低，真的要改的話還是手動升版號。
- 圖示（`static/img/icons/`）由 `generate_pwa_icons.py` 產生（K 線圖樣式），含 `icon-192/512`、`maskable-512`、`apple-touch-icon`、`favicon.ico`；改設計後重新執行該腳本即可。
- `display: standalone`（manifest.json）：安裝後無網址列，但保留手機狀態列。
- 安裝按鈕：`#install-app-btn`（list view，CSV 下載旁），`app.js` 的 `initInstallButton()` / `installApp()`：
  - Chrome/Android：監聽 `beforeinstallprompt`，點擊觸發原生安裝對話框
  - iOS Safari：無原生 API，顯示 toast 提示「加入主畫面」

## 前端架構

`state.allData` 存放 `/api/market/summary` 的完整資料，篩選（上市/上櫃、飆股）皆在前端計算，不重新呼叫 API。

### 六個視圖

| 視圖 | 說明 |
|------|------|
| `#list-view` | 完整股票列表（DataTables，預設代號升冪） |
| `#star-view` | 營收飆股：`_ratio >= 1.5` **且** `revenue_yoy >= 20%`，依預估倍數降冪 |
| `#watchlist-view` | 自選股清單（需登入）；未登入顯示 `#wl-auth-prompt` |
| `#ann-view` | 自結公告：純表格（不用 DataTables），見下方「自結公告」章節 |
| `#expert-view` | 達人選股：8 套規則切換分頁，見下方「達人選股」章節 |
| `#detail-view` | 個股詳情（股價圖、月營收圖、季財報表、達人選股評分卡、上一/下一檔導覽） |

分頁列（`#page-tabs-bar`）在 detail view 時隱藏；`showListView()` 的 viewMap：`{ star: 'star-view', watchlist: 'watchlist-view', ann: 'ann-view', expert: 'expert-view' }`。

### 主表格欄位（18 欄，index 0–17）

代號 → 名稱 → 產業 → **起始股價** → 收盤價 → **價差%** → 漲跌幅% → **營收預估股價** → 營收月份 → 月營收 → 月營收年增% → 季營收 → 最新EPS → 本益比 → EPS期別 → 資料日期 → **甜蜜點** → **虧轉盈**

**價差%**：`(close - start_price) / start_price × 100`，在 `_row_to_dict()` 計算（非來自 SQL），`price_diff` 欄位直接放入 JSON 回傳。正值綠色，負值紅色。

**營收預估股價**公式：`(月營收 / 季營收) × EPS × 240`；紅色格 = 現價 2x+，黃色格 = 1.5x+。

**本益比**計算：Q1–Q3 用 `close / (eps / quarter × 4)`（年化）；Q4 用 `close / year_eps`（`yeps` CTE 全年加總）。

自選股表格（`#wl-table`）欄位與主表格一致（含**起始股價**、**價差%**、**甜蜜點**、**虧轉盈**），但無「季營收」欄；`renderWlTable()` 的 `columnDefs` 索引需與欄位順序同步。飆股清單（`#star-table`）欄位則無**季營收**、**EPS期別**、**資料日期**，最後兩欄是**甜蜜點**、**虧轉盈**（後者在此表必為「—」，見上方 `turnaround_signal` 說明）。

### 重要 gotcha：jQuery `.data()` 型別轉換

jQuery 的 `.data('code')` 會把純數字字串（如 `"1218"`）自動轉為 `number`（`1218`），導致 `state.allData.find(s => s.code === code)` 嚴格比對失敗（API 回傳的 `code` 是 `string`）。`loadStockDetail(code)` 入口第一行已做 `code = String(code)` 正規化，**所有呼叫 `loadStockDetail` 的地方不必再轉型**，但若未來新增其他用 `.data()` 取得 code 再做 find 的邏輯，需注意同樣問題。

### 重要 gotcha：DataTables 的橫向捲動不能用自訂 wrapper div

用 `<div class="table-scroll">`（`overflow-x:auto`）包住 `<table>` 對**一般 HTML 表格**（`#ann-table`、`#expert-table`，見上方各自章節的 `.ann-table-wrap`）有效，但對**用 `.DataTable()` 初始化的表格**（`#stocks-table`/`#star-table`/`#wl-table`/detail view 的三個表）完全沒用——DataTables 初始化時會把這個自訂 wrapper div 整個丟棄、換成它自己的 `.dataTables_wrapper`，導致寬表格直接撐爆 `.card`／`.container`，讓整個頁面（而不是表格本身）橫向捲動，在手機上尤其明顯。正確做法是在 `.DataTable({...})` 的初始化參數加上 **`scrollX: true`**，DataTables 會自動處理表頭/表身同步捲動且欄位對齊正確。所有用 DataTables 的表格都已加上這個選項；新增任何用 `.DataTable()` 初始化的新表格時要記得比照辦理，不要再用 `.table-scroll` 這種 wrapper div 的做法。

### 手機版注意事項

`#nav` 的 `.nav-right`（右側圖示列）、`.page-tabs-bar` 的 `.page-tabs`、以及 `.filter-group`（達人選股的 8 個規則分頁、飆股/自選股的市場篩選鈕都共用這個 class）在內容超過螢幕寬度時，統一用「`overflow-x:auto` + 子項 `flex-shrink:0` + 隱藏捲軸」讓它變成可橫向滑動的一排，而不是讓文字換行撐爆版面。新增任何會塞進這幾個容器的按鈕/圖示，不需要額外處理，會自動吃到這個捲動行為。

### 飆股 / 自選股附加功能

- `downloadStarCsv()`：下載飆股清單為 CSV
- `copyStarForAI()` / `copyWlForAI()`：將清單連同分析 prompt 複製到剪貼簿，旁邊有 Gemini 連結（新分頁開啟）

### 通知系統

每 15 秒 poll `/api/crawler/status`，偵測到新 `success` log 時用 `Notification` API 送桌面通知；若權限被拒則改用 Toast。

### 固定面板 / 抽屜

- `#status-panel`（⚙ 爬蟲狀態）、`#today-panel`（🆕 今日更新，呼叫 `/api/updates/today`）：右下／左下角浮動面板，`.hidden` 切換顯示。
- `#msg-panel`（💬 留言板）：右側滑出抽屜（`.msg-drawer.open` 切換），`loadMessages()` 載入、`sendMessage()` 發表、依 `can_delete` 顯示刪除按鈕。

### 個股詳情頁：上一檔 / 下一檔導覽

`#detail-nav-btns`（`prev-stock-btn`/`next-stock-btn`）在詳情頁沿用「使用者是從哪個列表點進來的」那份清單與目前排序/篩選，而不是固定用代號順序：呼叫進入前的表格用 `setDetailNavContext(codes, currentCode)` 記下 `state.detailNavList`/`state.detailNavIndex`；DataTables 表格用 `_dtOrderedCodes(dt, codeColIndex)`（`dt.rows({order:'applied', search:'applied'}).data()`）取出「目前排序+篩選後」的完整代號順序，不只是當前頁面那幾筆。`goToAdjacentStock(delta)` 直接呼叫 `loadStockDetail(list[newIdx])` 切換，按鈕在清單頭尾自動 disable。

**重要 gotcha：每一個會呼叫 `loadStockDetail(code)` 的進入點都必須自己呼叫 `setDetailNavContext()`**，這個機制沒有集中在 `loadStockDetail()` 內部統一處理，而是散落在 6 個呼叫點各自負責（主表格、飆股清單、自選股、自結公告、達人選股列表、今日更新面板）。曾經有一個進入點（🆕 今日更新面板點股票晶片）漏掉這一步，導致從那裡點進詳情頁後，上一檔/下一檔會沿用不相關的舊清單。新增任何新的「點股票進詳情頁」進入點時，務必記得同時呼叫 `setDetailNavContext(codes, code)`，否則會重蹈這個 bug。

**返回列表時定位到原本的列**（2026-07-15）：`showListView()` 呼叫前先記下 `state.currentCode`，切回列表視圖後呼叫 `_scrollToStockRow(code)` 把畫面捲動到該股所在列並短暫加上 `.row-flash` 樣式提示（不是每次都固定回到頁面最上方）。DataTables 表格（主表格/飆股清單/自選股）分頁只會把「目前頁」的列渲染進 DOM，所以 `_scrollToStockRow()` 若直接用 `querySelector('[data-code]')` 找不到，會用 `_VIEW_DT_INFO[state.activeTab]` 找出對應的 DataTables 實例，**重新**用 `_dtOrderedCodes(dt, col)` 算出該股在目前排序/篩選結果裡的位置、換算頁碼、`dt.page(n).draw('page')` 翻頁後再捲動——**刻意不直接借用 `state.detailNavIndex`**，因為使用者可能是從「今日更新面板」等其他進入點點進詳情頁的，那個 index 對應的是那個進入點自己的清單，不一定等於目前 `activeTab` 這個表格的順序，借用會翻錯頁。自結公告／達人選股列表因為所有列一次全部渲染進 DOM（不分頁），第一次 `querySelector` 就會成功，不會走到 DataTables 分支。詳情頁底部（`.detail-nav-bar-bottom`）也有一個「← 返回列表」按鈕，跟頂部那個呼叫同一個 `showListView()`，避免看到頁面最下方還要滑回最上方才能離開。

## 自結公告（爬蟲 + 決定性解析 + AI 評級）

`crawl_announcements(date_str=None, limit=None)` 於 `crawler.py`，預設爬取前一個交易日的 MOPS 重大訊息；`limit` 只在手動測試時用於只處理清單前 N 筆。所有**數字**欄位（單月EPS、去年同月EPS、年增率、預估全年EPS、預估本益比）都是決定性解析/計算出來的，**AI 從不自己生數字**；AI 只負責根據這些已知數字 + 即時搜尋到的新聞給出評級與分析文字，邏輯對齊一個已驗證可用的參考實作（n8n 工作流程：先用關鍵字+正則決定性算好數字，再把這些「系統預算值」連同公告內容交給 AI，AI 只負責評級+寫分析，不重算數字）。

**爬蟲流程（無詳情頁請求，單一 POST 取得當天全部資料）：**
1. POST `ajax_t05st02`（帶 ROC 年月日）取得當天公告清單的完整 HTML 回應
2. **`_parse_announcement_rows()` 直接從這份清單 HTML 的隱藏 `<input>` 欄位解析出每一筆的完整主旨與說明全文**——MOPS 在清單頁裡，每筆公告會內嵌一組 `h{base+0}`...`h{base+8}`（`base = 該筆序號 × 10`）的隱藏欄位：`+0`公司名稱、`+1`公司代號、`+2`發言日期（YYYYMMDD西元）、`+3`發言時間（HHMMSS）、`+4`主旨、`+8`說明全文。**完全不需要對單筆公告額外發 GET 請求**，所以也沒有「詳情頁被雲端 IP 擋」這個問題（這是這個功能第三次重寫才找到的根本解法；前兩版分別試過 `t05sr01_1?TYPEK&i&co_id` 和 `ajax_t05sr01_1?SEQ_NO&...`，都需要逐筆詳情頁請求，已被棄用，詳見 git log）。比對依據：一個已驗證可長期穩定運作的參考實作（n8n 工作流程）採用同樣的隱藏欄位解析法
3. `_parse_disclosure()` 解析說明全文裡的自結合併財務資訊表格，只取**單月**資料（不再解析季/累計）：
   - A）TWSE「sii」單一表格，EPS 列 5 個數字都在**同一行**（月值/月年增%/季值/季年增%/累計值），只用前兩個；**去年同月EPS 沒有直接給，用 `monthly_eps / (1 + eps_yoy/100)` 反推**（`eps_yoy == -100` 時無法反推，留空）。不會去看下一行湊數字——曾經有這個 fallback，但會不小心把下一段標題行裡的年份/季數字（如「115年第1季」含的「115」「1」）也算進去，湊出假的5個數字，已移除
   - B）TPEX「otc」多段式（單月／單季／累計），EPS 列通常 3 個數字（本期/去年同期/年增%，年增%有時是「虧轉盈」「持續虧損」這種文字而非數字，這時只取前兩個），**去年同月EPS 是表格直接給的，不用反推**。**區段標題的編號寫法每家公司都不一樣**：阿拉伯數字「(1)單月」、中文數字「(一)單月」、英文字母「A.單月」、甚至完全不編號只接註腳「單月(註1)」——因此判斷區段邊界**不靠編號樣式，只認「單月」「單季」這兩個關鍵字本身**：第一次出現「單月」即進入單月區段，之後只要看到「單季」（或「四季累計」）就結束，中途若儲存格說明文字裡又出現一次「單月」（例如欄位名稱「最近一月單月」）不會被誤判成新區段重新開始
   - 兩種版面都會偵測「由虧轉盈/轉虧為盈」字樣 → `turnaround=1`
   - **`_extract_numbers()` 會把會計慣例的括號負數轉成負號**：`(25.23)` → `-25.23`（純文字括號如「(元)」「(由虧轉盈)」會先被去掉，不會被誤判成數字）。這個轉換漏掉的話，虧損/衰退的公司會被算成正數
4. **無主旨 pre-filter，唯一的篩選依據是「有沒有解析出 `monthly_eps`」**——原本對齊參考實作（n8n）用的是單純字串比對「說明欄是否含『每股盈餘』」，後來放寬到也保留「注意交易資訊」類公告（即使沒有財務表格），但這兩種寬鬆條件都製造過誤判：「限制員工權利新股」「庫藏股」「可轉債」等公告依法要寫「對公司每股盈餘稀釋情形」揭露文字，會被誤判成自結公告；可轉換公司債價格變動的注意公告（跟公司本身的 EPS 完全無關）也會被誤判成相關。現在改成：先呼叫 `_parse_disclosure()`，**只有真的解析出 `monthly_eps` 才存**，其餘一律跳過不存
5. `_price_at_or_before()` 查 `daily_prices` 取得「公告日期當天，若非交易日則往前找最近一個交易日」的收盤價 → `price_at_announce`
6. `estimated_annual_eps = monthly_eps × 12`；`estimated_pe = round(price_at_announce / estimated_annual_eps, 1)`（任一缺值則留 None，`estimated_annual_eps <= 0` 也不計算）
7. **若有設定 `OPENROUTER_API_KEY`**，對每一筆通過 `monthly_eps` 篩選的公告呼叫 `_analyze_with_ai()`：把已經算好的數字（單月EPS/去年同月EPS/年增率/是否由虧轉盈/預估全年EPS/預估本益比）連同公告全文交給 AI，prompt 明確要求「直接採用這些數值，不要自己重新計算」，AI 只回傳 `ai_rating`（🔴🟠🟡🟢 四級）與 `ai_analysis`（4段分析文字）。`model` 預設 `perplexity/sonar`（OpenRouter 上會自動觸發即時網路搜尋的模型，對齊 n8n 預設選擇），可用 `OPENROUTER_MODEL` 覆蓋；**沒設定 `OPENROUTER_API_KEY` 則整段跳過**，`ai_rating`/`ai_analysis` 留 NULL，不影響其他欄位。每筆呼叫前 `time.sleep(20)`——這是跟爬蟲合一執行的代價，**有 AI 候選筆數多的那天，整個 `crawl_announcements()` 執行時間會從原本的幾秒鐘明顯拉長**，屬正常現象，務必背景執行（`nohup ... &`）
8. 用 `Announcement.__table__.insert().prefix_with('OR IGNORE')` 以 `seq_no`（合成鍵 `{date8}_{time6}_{code}`，MOPS 清單頁本身不提供全域唯一序號）去重——同一筆公告重複抓到時會被直接忽略，**不會產生重複列**，但既有列也不會因此被更新（包含 AI 評級：若某筆當次 AI 呼叫失敗，之後重跑也不會自動補上，目前沒有像股價那樣的補值機制）
9. `_backfill_announcement_prices()`：每次 `crawl_announcements()` 跑完都會執行一次，掃描全表 `price_at_announce IS NULL` 的舊列重新查 `daily_prices`、補上股價與 `estimated_pe`。原因：`INSERT OR IGNORE` 對已存在的列完全不會更新，若公告當天的收盤價在第一次抓取時還沒寫入 `daily_prices`（常見於盤後立刻發布的公告），那一列的股價欄位會永久留空，除非有這段補值邏輯主動重算
10. `_log('announcements', 'success', ...)` 訊息格式為 `"{saved} saved / {backfilled} backfilled / {total} rows parsed"`

**除錯注意**：在 Zeabur 終端機貼含中文字的程式碼/heredoc 時，**終端機本身會在中文字之間插入空格**，不只是顯示問題，連貼上去的程式碼內容都會被改掉（例如 `re.compile('主旨')` 會變成 `re.compile('主 旨 ')` 導致比對失效）。之後要請使用者在終端機跑診斷用的 Python 腳本時，**程式碼裡絕對不要放新的中文字面值**，只能重用 `crawler.py` 裡已經部署好的常數/regex（如 `crawler._EPS_LABEL_RE`），或單純印出結構（不靠中文比對）讓人眼判讀。同樣道理適用於任何要在 Zeabur 上寫入中文資料的修正——`fix_stock_names.py`（一次性修正 13 檔被舊版 `crawl_stock_list()` big5 codec 弄壞的股票名稱）就是靠 `git pull` 後在雲端執行整個檔案，而不是把 UPDATE 語句貼進終端機，來避開這個問題。

**前端（`#ann-view`）：** 純表格（不用 DataTables），14 欄：公告日期／代號／名稱／公告主旨／公告時股價／單月EPS／去年同月EPS／月EPS年增率／轉虧為盈／預估全年EPS／預估本益比／**AI評級**／AI分析／**自選股**。轉虧為盈欄位為真時顯示 🔥；預估本益比 `<= 0` 時前端顯示「—」（負本益比無意義，但後端仍照算存入 DB，不隱藏原始資料）。

- **公告日期欄**：顯示 `announce_date` + `announce_time`（取 `HH:MM`，捨去秒數），也就是 MOPS 網站上的「發言日期」+「發言時間」，不是爬蟲抓取/寫入的時間。API 排序為 `ORDER BY announce_date DESC, announce_time DESC`。
- **公告主旨**：表格內只顯示前 10 字（`_annTruncate()`），點擊開 `#ann-modal`（同頁彈出視窗，不開新分頁/新頁面）顯示完整主旨與內容（`a.content`，無內容時顯示「（無詳細內容）」）。全部公告資料先一次性存進 `_annData`（模組層級陣列），modal/AI按鈕都用 `data-idx` 對應陣列索引去查，不用再打 API。
- **AI評級欄**：`_annRatingDot()` 依 `ai_rating` 字串內容（含「強烈」「建議」「一般」其餘視為需要小心）顯示 🔴🟠🟡🟢 emoji，沒有評級顯示「—」。點擊（`.ann-rating-link`）開 `#ann-modal`，跟點主旨開的是同一個 modal，內容會多顯示評級與 `ai_analysis` 全文（`.ann-modal-rating`/`.ann-modal-analysis`）。這欄是後端 `crawl_announcements()` 自動產生的，使用者不能手動觸發單筆重新評級。
- **AI分析欄**（跟上面的 AI評級欄是兩個獨立功能，刻意並存）：`<a class="btn btn-sm ann-ai-link" href="https://gemini.google.com" target="_blank">`，點擊時 `copyAnnForAI()` 複製一段完整的估值分析提示詞到剪貼簿，同時連結本身會在新分頁開啟 Gemini（Gemini 網頁版不支援 URL 帶入提示詞，使用者需自行貼上），與既有 `copyStarForAI()`/`copyWlForAI()` 的「複製給AI」模式一致。提示詞包含：固定的分析師人設與分析步驟（同業本益比錨點、外資EPS預估、便宜/合理/昂貴價定價）+ 動態插入的股票代碼/名稱 + **目前股價**（從 `state.allData` 依 `stock_code` 查找，即本站資料庫的最新收盤價與資料日期，不是公告當時的價格，也不靠 AI 自己搜尋）+ 公告全文（無全文則用主旨）。要改提示詞文字本身，直接編輯 `copyAnnForAI()` 裡的模板字串。
- **自選股欄**：`addAnnToWatchlist()`。未登入或尚未建立任何自選股清單時顯示對應 Toast，不送出 API 請求。**若使用者只有 1 個清單，直接加入該清單**；**若有 2 個以上清單，彈出 `#wl-pick-modal`（重用 `templates/index.html` 既有的通用 `.modal-overlay`/`.modal-box` 樣式，跟 `#auth-modal`/`#about-modal` 同款，不是 `.ann-modal-*` 那套）讓使用者點選要加入哪一個**，清單項目旁標示目前支數或「已在清單中」。實際寫入邏輯抽成 `_wlAddStockTo(wl, code)`（接受任意指定的 `wl` 物件，不限定 `wlActive()`），原本的 `wlAddStock(code)`（只加到目前作用中清單，給 `#watchlist-view` 自己的搜尋框用）改為呼叫這個共用函式，行為不變。
- **今日更新面板**：`/api/updates/today` 多了 `ann_count`／`ann_last_checked`，今日有新公告就顯示「今日新增 N 筆」，否則顯示最後檢查時間。
- **表格排序**：純前端排序，不靠 DataTables（這個表格本身就不用 DataTables）。可排序欄位的 `<th>` 帶 `class="ann-sortable" data-sort="<field>"` + 一個 `<span class="ann-sort-arrow">` 佔位符；點擊呼叫 `sortAnnTable(field)`，直接對 `_annData` 原地排序（`_ANN_SORT_GETTERS` 定義各欄位的取值函式）後呼叫 `renderAnnTable()` 重繪整個 tbody。`renderAnnTable()` 是從 `loadAnnouncements()` 抽出來的共用渲染+事件綁定邏輯，排序後重新呼叫它才能讓 `data-idx`（對應 `_annData` 索引）保持與排序後的新順序一致，否則點擊主旨/評級/AI分析/自選股會對到錯的資料。空值一律排到最後（不論升降冪），不會因為遞減排序就跑到最前面。**公告主旨／AI分析／自選股三欄故意不可排序**（文字截斷後排序意義不大；後兩者是操作按鈕，不是資料）。
- **網站說明（About modal）**：新增「📰 自結公告爬蟲」段落，說明 30 分鐘排程與 AI分析按鈕用法（`templates/index.html` 的 `#about-modal`）。

## AI 個股分析（admin-only，按需觸發，**會產生 OpenRouter 費用**）

`crawler.analyze_stock_with_ai(code)`：跟自結公告的 AI 評級是不同的功能，**完全手動觸發、不自動、不批次**——每次呼叫都是真實付費的 OpenRouter API request，所以刻意設計成只有管理員點按鈕才會執行，沒有排程、沒有限流（使用者選擇不加限流，靠管理員自律控制費用）。

- **資料來源**：直接從本站 DB 查詢（`Stock`/`DailyPrice`/`MonthlyRevenue`/`QuarterlyFinancial`），不透過 HTTP 呼叫自己的 API。本益比公式**完全對齊** `_SUMMARY_SQL`（Q4 用該年四季EPS加總，Q1–Q3 年化單季EPS）；另外算出「營收預估股價」，公式**對齊** `static/js/app.js` 的 `calcEst()`（`(月營收/季營收)×EPS×240`）。這些數字都當作「已知，AI 不要重算」放進 prompt。
- **AI 只補資料庫沒有的東西**：同業本益比、外資EPS預估、近期新聞、法人籌碼——用 `_STOCK_AI_SYSTEM_PROMPT`，模型預設 `perplexity/sonar`（同自結公告，OpenRouter 上會自動觸發即時搜尋），輸出 `ai_rating`／`target_cheap`／`target_fair`／`target_expensive`／`ai_analysis`。
- **快取表 `stock_ai_analysis`**：`stock_code` 為主鍵，**每檔股票只存最新一次結果**（無歷史），`POST` 觸發新分析時直接覆寫。即使 AI 呼叫失敗也會覆寫一筆（`ai_rating` 等留 NULL），這樣才能正確反映「最近一次嘗試失敗」而不是顯示舊的過期結果。
- **同步執行**：`POST /api/stocks/<code>/ai-analysis` 不像 `crawl_*` 系列用 `_run_bg()` 背景執行，是直接 block 住等 OpenRouter 回應（最長 90 秒逾時）——因為這是管理員主動點擊、正在等結果的單次操作，不是排程批次任務，不需要輪詢機制。
- **前端**：`#detail-view` 最上方有個 `.admin-only.hidden` 卡片（非管理員完全看不到，連 `GET` 都不會打，省一次無意義的請求），`loadStockDetail()` 載入時若 `state.user.is_admin` 才順便抓快取結果；點「重新分析」呼叫 `runStockAiAnalysis()`，按鈕 disable 防止重複點擊（同一檔股票短時間連點兩次會疊加成兩筆 OpenRouter 費用）。評級 dot 重用 `_annRatingDot()`，跟自結公告同一套 🔴🟠🟡🟢 視覺語言。

## 達人選股（8 套規則計分引擎 + 1 套本站自製實驗規則）

編碼 4 位台股達人（股泰多方/空方、888/巔峰標準1–4、股魚、股海老牛）公開的選股/評分邏輯，對本站 DB + FinMind 補充資料即時計分，`expert_key`：`gutai_bull`/`gutai_bear`/`flag888_1`–`flag888_4`/`guyu`/`laoniu`，中文標籤見 `experts.py` 的 `EXPERT_LABELS`。

**`momentum_guard`（動能防雷，2026-07-16 新增）是第 9 套，跟前 8 套性質不同**：不是抄錄自某個公開達人的選股法，是本站自製的實驗性規則，記在 `EXPERIMENTAL_EXPERTS` 集合裡，`app.py` 的 `/api/experts`、`/api/experts/<key>`、`/api/stocks/<code>/expert-scores` 三個端點都會多回傳一個 `is_experimental` 布林欄位，前端 `renderExpertTabs()`/`loadStockExpertScores()` 據此在分頁按鈕加掛 `<span class="new-badge">NEW</span>`。設計目的是避開「統計上便宜、但基本面正在惡化」的價值陷阱（起因：2496 卓越在 2026-07 遇到的狀況——6月營收年增在連續5個月遞減後首度轉負，但PBR/PE看起來還是便宜）：選股門檻 `require()` 直接排除「近月營收年增率剛由正轉負」的股票，加分項則看毛利率/營業利益率/ROE/單季EPS 的年增率是否轉強（`_yoy_diff` 算出的變化量，不是絕對水準）。

### 資料來源

- **FinMind**（`finmind_client.py`，付費 999 方案 6,000 次/小時）：三大法人買賣超、股權分散表、資產負債表/現金流量表/財報毛利項目、股利政策、除權息填息、PER/PBR。**Bulk 模式（不帶 `data_id`）一次回傳全市場當天資料，但 `end_date` 對大多數 dataset 不是真正的 range 篩選**——只有精準等於 `start_date` 才有效，寬範圍查詢會直接塌縮成只回傳 `start_date` 當天的資料（已用真實 API 呼叫驗證過）。因此 `crawler.py` 所有 `crawl_finmind_*` 一律逐日呼叫（`_finmind_daily_rows()` 共用這個逐日迴圈），不依賴 range 查詢一次拿多天資料。
- **TWSE/TPEX OpenAPI**（`crawl_director_holdings()`，非 FinMind、免金鑰）：董監持股比例。兩個端點都只回傳「目前最新一期」全市場快照（月更新），**無法查歷史日期**，跟其他 FinMind 函式的 `date_str` 參數模式不同。
- **技術指標**（`technical.py`）：不靠 FinMind，純 Python 用 `daily_prices` 的 OHLC 自算 EMA(3/5/8/13)/MACD/RSI/KD（含週K、月K），只有股泰規則會用到。

### 已知的簡化/近似（都跟 project owner 討論過，不是漏洞）

- 股泰的 TU/TM/TD 價位與 週守/月守 支撐是其軟體專屬公式，未公開——用 `technical.py` 的 `ema13_support`/`week_low_4`/`month_low_20` 近似替代，`breakdown` 裡都標成 `approx: true`，不冒充原始公式。
- 888 標準2「董監持股」用 `financial_extra.capital_stock ÷ 面額10元` 反推已發行股數，面額非 10 元的少數股票會不準（`crawl_director_holdings()` 對算出 >100% 的異常值直接捨棄不存）。
- 「累計月營收年增率」（股泰）、「累積淨利年增率」（老牛）都用單期 YoY 代替真正的累計值——本站 schema 沒有累計營收/淨利欄位。
- 每一項「近N年平均」比率（ROE/ROA/毛利率/流動比率/周轉天數）都用「近 N×4 季」的算術平均近似，不是嚴格的曆年桶。

### 計分引擎（`experts.py`）

- `ScoreCard`：`require(label, cond)` 是選股門檻（`passed` = 全部 `require` 皆真；輸入為 `None` 一律視為不通過，不放行無法驗證的股票）；`award(label, cond, points)` 是配分項，`cond=None` 時整項跳過不計入 `max_score`（資料不足不扣分，也不算分）；`award_count(label, achieved, max_occurrences, unit_points)` 是「每命中一次 +N 分，封頂 M 次」的漸進計分（如「近5日外資買超次數」）。
- `_build_context(db)`：一次 bulk 查完全部表，組成 `{stock_code: ctx}`。**技術指標快照是逐股流式計算**（`daily_prices` 查詢本身已 `ORDER BY stock_code, date`，累積到換股票就 flush 該股的 `technical.snapshot()` 後捨棄），而不是先把全市場~2 年 OHLC 全部塞進記憶體再統一算——後者在 Zeabur 容器上會直接 OOM（~1,982 檔 × ~500 筆同時在記憶體是實測會爆的規模）。**`per`/`pbr`/`dividend_yield` 刻意不跟 `close`/`volume` 綁同一個「最新一天」查詢**，而是另外抓「最近一筆這三欄實際有值」的資料列：`crawl_finmind_valuation` 排在股價爬蟲之後跑，且容許落後 1–3 天回補（見下方爬蟲章節），若跟 `close` 一樣強制要求同一天，只要當天估值資料還沒進來，888標準1（淨值比）/888標準3（殖利率）會瞬間全數判定不通過（曾經在這個確切原因下發生過 0/1982、1/1982 的假性全滅，已修復）。
- `compute_expert_scores()`：對每檔股票跑全部 8 套規則，寫入 `expert_scores`（`INSERT OR REPLACE`，跟 `stock_ai_analysis` 同樣的「只存最新一筆快照，不留歷史」模式，**唯二例外是 `entered_at`/`transition`**）。單一規則對單一股票算分丟例外時只記 log 跳過，不影響其他規則/股票。
- **`entered_at`/`transition`（僅股泰多方/空方訊號有意義）**：每次執行都先讀出覆寫前的舊列（`old_rows`），`passed` 狀態沒變就延續舊的 `entered_at`（入榜日期）；狀態改變（或該列第一次寫入/剛加欄位的 bootstrap）才把 `entered_at` 更新成當天。`transition` 只在 `gutai_bull`/`gutai_bear` 這對互斥規則、且是「真正的狀態翻轉」時才計算：進榜當下若「舊快照」發現該股正好在對面那個訊號上榜，記錄 `空轉多`/`多轉空`；bootstrap（欄位剛加入，`old.entered_at is None`）或非翻轉的正常首次進榜一律是 `None`，不會亂猜。

### API / 排程

`GET /api/experts`、`GET /api/experts/<key>`、`GET /api/stocks/<code>/expert-scores`（見上方 REST API 清單）。排程：`_finmind_job()` 每天 17:00 依序跑 5 個 FinMind 增量函式 + `crawl_director_holdings()` + `compute_expert_scores()`；`_finmind_financials_job(quarter)` 跟官方季報同月份、每天 23:30 補 `financial_extra`（`financial_extra` MOPS IFRS 資料可靠起點同官方季報一樣是 2013 年）。手動觸發任務見上方「排程」章節。

### 前端

- **`#expert-view`**（列表）：`#expert-tabs` 8 個規則切換鈕（`loadExperts()`/`renderExpertTabs()`），下方純表格 13 欄：排名/代號/名稱/產業/營收月份/起始股價/收盤價/價差%/漲跌幅%/評分/預估倍數/資料日期/甜蜜點。**`gutai_bull`/`gutai_bear` 這兩個分頁額外多兩欄**（入榜日期/轉換，來自 `expert_scores.entered_at`/`transition`）：`renderExpertTable()` 用 `_isGutaiKey(_expertKey)` 判斷，動態 toggle 這兩個 `<th>`（`#expert-th-entered`/`#expert-th-transition`，預設 `.hidden`）並在列資料多帶兩個 `<td>`，其他 6 套規則不顯示。「預估倍數」欄（2026-07-22 從「評分明細」按鈕改版）沿用 `#star-view`（營收飆股清單）既有的 `calcEst(s)` 公式即時算：`(revenue / qf_revenue) × eps × 240`，再除以 `close`，跟 `#star-view`/主表格用同一套邏輯與顯示格式（`x.xx` + `x` 字尾），純前端算、不需要後端額外欄位。原本點按鈕開 `#expert-modal` 彈窗看評分明細長條圖的機制已整個移除（`openExpertModal`/`closeExpertModal`/`renderModalExpertChart` 連同 HTML 一併刪除，不留死代碼）——同樣的評分明細長條圖在詳情頁的「達人選股評分」卡（`#stock-expert-card`，`renderStockExpertChart()`）仍看得到，資訊沒有真的消失，只是列表頁不再重複提供彈窗入口。
- **`#detail-view` 達人選股評分卡**（`#stock-expert-card`）：`loadStockExpertScores(code)` 抓 `/api/stocks/<code>/expert-scores`，`#stock-expert-tabs` 列出該股所有已算出分數的規則，`renderStockExpertDetail()` 一樣先畫「總分 X/Y 分」大字標頭，再選股標準清單，再呼叫 `renderStockExpertChart()`。**圖表繪製邏輯抽成共用的 `_renderExpertChart(scoreItems, canvasId, wrapId, chartKey)`**，`renderStockExpertChart`/`renderModalExpertChart` 只是帶入各自的 canvas/state key 呼叫它——確保列表 modal 跟詳情頁兩處的視覺化永遠同步；`state.stockExpertChart`/`state.modalExpertChart` 各自持有 Chart.js 實例，切換分頁/關閉彈窗時 `.destroy()` 再建新的，避免 canvas 重用衝突。
- **重要 gotcha：`_stockExpertKey`（目前選中的達人分頁）刻意跨股票延續，不是每次都重置**——`loadStockExpertScores()` 只有在 `_stockExpertKey` 對新股票不存在（`!scored.some(s => s.expert_key === _stockExpertKey)`，理論上不會發生，因為每檔股票都算好全部 8 套規則）時才 fallback 到「第一個通過的規則」。曾經每次都重置成「這檔股票自己第一個通過的規則」，導致用上一檔/下一檔導覽瀏覽時，選中的達人分頁會隨機跳來跳去（每檔股票通過的規則不同）。另外，從 `#expert-view` 列表點股票進入詳情頁時，`renderExpertTable()` 的點擊事件必須在呼叫 `loadStockDetail()` 之前手動把 `_stockExpertKey` 設成該列表目前的 `_expertKey`，否則會沿用使用者上次在別處瀏覽時殘留的分頁，而不是使用者點擊當下所在的那個達人榜單。
- **總分一定要清楚顯示**：詳情頁的評分卡（`#stock-expert-card`，唯一還會畫評分明細長條圖的地方，見上方「列表彈窗已移除」說明），`.stock-expert-total`（大字、`--primary` 顏色數字）都放在選股標準清單「之前」，不是只靠分頁按鈕上的小字 `(X/Y)` 讓使用者自己找。
- **`#expert-table` 表格排序**（2026-07-13 新增，2026-07-22 補上「預估倍數」欄可排序）：跟自結公告表格（`#ann-table`）同一套純前端排序機制（`class="ann-sortable" data-sort="<field>"` + `.ann-sort-arrow`，兩個表格都是 `class="ann-table"` 所以共用同一份 CSS），但獨立實作一份 `_EXPERT_SORT_GETTERS`/`sortExpertTable()`/`_applyExpertSort()`，**沒有**跟 `sortAnnTable()` 共用程式碼——刻意保持兩份獨立，因為欄位取值邏輯不同：達人選股表格排序用到的價格類欄位（起始股價/收盤價/價差%/漲跌幅%/預估倍數/資料日期/甜蜜點）並不在 `_expertData` 本身上，而是要透過 `_expertP(code)`（`state.allData.find(...)`）另外查表算，`_ANN_SORT_GETTERS` 沒有這個需求。**排序偏好跨切換達人分頁（`_expertKey`）延續**：`loadExpertDetail()` fetch 到新規則的資料後，若 `_expertSortField` 已設定就呼叫 `_applyExpertSort()` 套用同一個排序，不會因為換分頁就悄悄變回 API 預設順序、卻讓表頭箭頭誤導使用者以為還在排序中。「排名」（純序號）一欄不可排序，理由同自結公告表格的主旨/AI分析/自選股欄。

### 持股健康檢查（`compute_holding_health`，本站自製、實驗性，2026-07-16 新增）

跟上面 9 套「找買點」的達人選股規則用途不同——這個是給**已經持有**的自選股看要不要注意出場的三階段預警：`正常`／`早期警告`／`注意`／`撤退`。**不是批次跑全市場**，而是 `GET /api/stocks/<code>/health` 單股即時查詢時才計算（技術指標只抓該股近 400 天 OHLC 算 `technical.snapshot()`，比 `_build_context()` 的全市場批次快很多，適合自選股清單這種小數量、即時查詢的場景）。

- **技術面異常**（0–4 項）：跌破 20 日均線、跌破 60 日均線、日 MACD 柱狀由正轉負、較 60 日高點回落逾 15%。
- **基本面異常**（0–3 項）：最新月營收年增率 <0、單季EPS年增率 <0、毛利率年增率 <0（惡化）。
- **升級邏輯**：技術≥2 且基本面≥2 → `撤退`；技術≥1 且基本面≥1 → `注意`；只有其中一邊 ≥1 → `早期警告`；都沒有 → `正常`。門檻是本站自訂的近似值，沒有對外公開的原始出處可以核對。
- 前端只用在 `#watchlist-view` 的 `#wl-table`（最後一欄，`healthBadgeCell()`，紅=撤退/黃=注意/灰=早期警告/綠=正常，`title` 顯示觸發幾項技術面/基本面異常）：`renderWlTable()` 改成 `async`，先用 `Promise.all` 平行抓自選股清單裡每一支股票的健康度、組成 `healthByCode` 對照表，才建立 rows 陣列——不是每支股票各自觸發一次表格重繪。主表格／飆股清單／達人選股列表**沒有**這一欄，只有自選股清單有（用途上只對「已持有」有意義）。

### 投資組合壓力測試（`portfolio_risk.py`，本站自製、實驗性，2026-07-16 新增）

獨立模組，不在 `experts.py` 裡（性質上是「整包清單」風險分析，跟單股計分是不同的關注層級）。`GET /api/watchlists/<wl_id>/stress-test`（需登入且是清單擁有者，沿用 `_wl_rows(db, wl_id)` 取代號清單）呼叫 `portfolio_risk.run_stress_test(db, codes)`，前端 `runWlStressTest()`（`#watchlist-view` 工具列的「🧪 壓力測試」按鈕）觸發、結果渲染進 `#wl-stress-panel`。

**核心限制、務必先知道**：`watchlist_stocks` 只存代號，不存股數/金額，所以整個模組**一律假設等權重**——這不是真實持股的風險模型，只能看出「這份清單本身」在各面向的風險輪廓，前端面板文字有明講這個限制。

四個分析面向：
1. **歷史情境回放**（`STRESS_SCENARIOS`）：不是假設性的總經因子模型（本站沒有 beta/因子曝險資料能做那種模型），是直接回放 5 段台股史上真實的系統性下跌期間（2011歐債危機/2015中國股災/2018中美貿易戰/2020 COVID崩盤/2022全球升息熊市），用清單裡每一檔股票「當時真實的股價走勢」算出期間報酬率與最大回檔，等權重平均。某檔股票若在情境起始日之前還沒有價格資料（例如當時尚未上市），該情境會自動跳過該股不硬湊，`covered`/`total` 兩個欄位讓前端可以誠實標示涵蓋家數。
2. **產業集中度 HHI**：依 `stocks.industry`、等權重（依檔數，不是依市值/金額）算標準 0–10000 尺度 HHI，>2500 高度集中／1500–2500 中度／<1500 分散。
3. **相關性**：近 400 個日曆天（≈近1年交易日）逐日報酬率兩兩 Pearson 相關係數，共同交易日 <30 天的配對直接跳過（`_pearson()`），回傳平均值 + 相關性最高的一對（含股票名稱，方便前端顯示）。
4. **歷史模擬法 VaR（95%/99%）**：把清單「等權重平均每日報酬率」的完整序列由小到大排序取 5%/1% 分位數（`_percentile()`）——是用實際歷史分布抓尾部風險，不是常態分布假設的參數法 VaR，這是刻意的方法選擇（不需要額外估計波動率/相關矩陣，用同一份日報酬率資料就能算，跟情境回放同樣「直接用歷史資料說話」的精神一致）。

本機測試過一組台積電/鴻海/聯發科/聯電/葡萄王的清單，結果合理：HHI 4400（高度集中，3檔半導體業佔60%）、2022升息熊市衝擊最大（期間報酬-32.32%、最大回檔-34.6%）、台積電與鴻海相關性最高（0.843）。

### 已知未修復問題（8 角度 code review 找到，優先度較低，故意先擱置）

- **`financial_extra` 千元轉換沒有自動化 migration 防護**：`_to_thousands()`（`crawler.py`）修復千元單位問題那次是手動整表重新回填，`database.py` 的 `init_db()` migration 清單裡沒有對應的自動修復項目。若之後 DB 從 `fetch_db.py` 的舊快照（早於那次修復）還原，或有任何殘留的舊格式資料被 Q4 反算引用，會算出離譜的比率，且無任何錯誤訊息。
- **`crawl_director_holdings()` 的 `year_month` 只取 TWSE/TPEX 兩個來源中先回應的那個，套用到全部股票**：兩個 OpenAPI 各自獨立按月更新，若剛好其中一邊已進新月份、另一邊還沒，較晚更新的市場那批股票會被貼上錯誤的月份標籤。
- **`static/js/app.js` 的 `_stockExpertKey` 全域可變狀態設計脆弱**：這個 session 已經因為這個模式修過兩次分頁跳來跳去的 bug（`c8aecf5`、`76cb5ed`），任何新增的「進入股票詳情頁」入口如果忘記手動同步這個變數，會再犯同樣的錯，且沒有機制強制檢查。
- **`database.py` 的 `PRAGMA journal_mode=WAL` 用不分類的 `except Exception: pass`**：本意是容忍「檔案系統不支援 WAL」，但也會靜默吞掉其他真正的資料庫錯誤。
- **本機與雲端資料庫的 FinMind 歷史深度不完全一致**：雲端 `dividend_policy` 在 2014–2019 部分年份仍偏稀疏（回填時多次遇到 Zeabur 容器在高負載下當機，過程詳見 git log 附近的操作紀錄），導致同一套規則在本機/雲端算出的通過檔數不同。不是程式錯誤，純粹是資料完整度差異；`888標準2`/`888標準4`/`股海老牛` 這幾套依賴長期股利歷史的規則受影響最大。
- **`financial_extra` 有 15 個季度的資產負債表欄位（`current_assets`/`current_liabilities`/`liabilities`/`equity`/`total_assets`/`inventories`/`accounts_receivable`/`long_term_borrowings`/`capital_stock`）全庫幾乎 0% 有值**：2013Q1、2013Q2、2016Q4、2017Q3、2017Q4、2018Q1、2018Q2、2018Q3、2019Q1、2019Q2、2022Q4、2023Q3、2023Q4、2024Q1、2024Q2。**已確認是 FinMind 上游資料源缺口，不是本專案的爬蟲/解析 bug，無法透過重跑修復**——用 `diagnose_balance_sheet.py`/`diagnose_balance_sheet2.py`（唯讀診斷腳本，已保留在 repo）對 `TaiwanStockBalanceSheet` 直接查詢單一股票（2330 台積電，資料覆蓋率最好的股票）+ 寬日期範圍驗證過，這幾個季度 FinMind 自己的資料庫就是空的，連續重跑 `crawl_finmind_financials()`/`backfill_missing_financial_extra.py` 拿到的還是同一個空回應。缺口的季度分布有週期性（2016Q4–2019Q2 跟 2022Q4–2024Q2 兩段的缺口型態幾乎一模一樣，像是 FinMind 自己資料回補進度的某種週期性缺口），但成因在 FinMind 那端，不是我們能控制的。**影響範圍**：這幾季衍生出的 ROE/ROA/流動比率/速動比率/週轉天數（`experts.py` 的 `_ratio_series`/`_quick_ratio_series`/`_turnover_days`，以及 `get_stock_fundamentals()` 的逐季圖表）在這些季度會是 `None`；因為現有邏輯本來就是「有幾季用幾季平均」而非強制連續 N 季，這不會讓程式出錯或算出離譜數字，只是那幾個時間窗的平均值精準度會因為少了一季而略打折扣。損益表欄位（`gross_profit`/`cost_of_goods_sold`/`pretax_income`，來自 `TaiwanStockFinancialStatements`）與現金流欄位（來自 `TaiwanStockCashFlowsStatement`）不受影響，這兩個資料集在同樣的季度都正常。

### 回測（backtest_gutai.py，僅本機、獨立於正式排程）

`backtest_gutai.py` 回測「股泰多方/空方訊號」歷史上是否真的有效——**刻意獨立於 `experts.py` 的 `_build_context()`**（正式排程每天呼叫的那個），只讀取 DB、從不寫入 `expert_scores`，避免任何回測邏輯有機會影響正式評分。

**方法**：每週取一個歷史樣本日，用「只包含當時已知資料」重建 context（月營收用「次月10日後才算已知」、季報用專案既有的公告期限規則 5/15、8/14、11/14、隔年3/31 判斷，避免用到未來資料作弊），直接呼叫 `experts.py` 原封不動的 `score_gutai_bull`/`score_gutai_bear`。當天分數 `passed=True` 且 `score>=--min-score` 才算「發出訊號」，記錄該股之後 `--horizon` 個交易日的報酬，跟當天全市場平均報酬（基準）比較，並依分數級距（60-79/80-89/90+）分組統計。

**已知限制**：`stocks` 表只有目前追蹤中的股票，沒有回測當時的完整名單，下市股票的失敗案例看不到（倖存者偏差）；沒有計入交易成本/滑價；股泰的 TU/TM/TD 真實公式未公開，回測沿用跟正式評分一樣的 `technical.py` 近似值。

**效能規範（往後任何回測腳本都要遵守）：一律用 `multiprocessing` 平行運算，盡量榨乾多核心 CPU，不要寫成單執行緒**。技術指標（EMA/MACD/RSI/KD）對每檔股票只算一次、快取成陣列，用 `bisect` 依日期索引取值，不要每個取樣日都重新計算一次（那樣等於重跑一次完整的 `compute_expert_scores()`，慢上百倍）；平行化的軸是「每個取樣日彼此獨立」，用 `multiprocessing.Pool(initializer=...)` 把大型唯讀資料（技術指標陣列、法人、持股、季報）在每個 worker process 只序列化一次（用 initializer 塞進 worker 自己的全域變數），不要每個 task 都重新 pickle 一次。Windows 用 `spawn` 模式啟動子行程，進入點一定要包在 `if __name__ == '__main__':` 裡。

## 券商分點進出（自選股專屬，2026-07-22 新增）

只記錄「有人自選過」的股票，不是全市場資料——來源 FinMind `TaiwanStockTradingDailyReport` 是逐股票呼叫，一檔一天就近千到數千列（2330 實測近 5800 列），全市場每天跑不現實也沒必要。**曾誤以為要用 `TaiwanStockTradingDailyReportSecIdAgg`（需 FinMind 贊助方案）**，實測發現這個 dataset 名稱已不存在（`422` 列出的合法 enum 沒有它），改用 `TaiwanStockTradingDailyReport`——這個現有 `FINMIND_TOKEN` 就能直接呼叫，不需要升級付費方案。

**原始資料是逐價位明細**：同一券商同一天在不同成交價各有一列，`crawler.py` 的 `crawl_broker_trades(date_str, stock_code)` 依 `securities_trader_id` 加總 `buy`/`sell` 股數才存進 `broker_trades`（`buy_price`/`sell_price` 是用金額加權平均出來的，不是原始值直接搬）。這個 dataset 不分上市/上櫃，同一支函式即可，不用像 `daily_prices` 那樣分 TWSE/TPEX。

**觸發機制（兩條路徑，共用 `crawler.backfill_broker_trades(code, days=30)`）**：
1. **首次自選回補 30 天**：`POST /api/watchlists/<id>/stocks` 新增成功後，若 `broker_trades` 裡該股票尚無任何資料（代表全站第一次有人自選），用既有 `_run_bg()` 背景執行緒觸發回補。
2. **每日增量 + 舊自選股補漏**：`scheduler.py` 的 `_broker_trades_job()`，週一〜五 17:30（`_finmind_job` 之後），對 `crawler.watchlisted_stock_codes(db)`（`SELECT DISTINCT stock_code FROM watchlist_stocks`）逐檔檢查：已有資料的只抓當天增量，**完全沒資料的直接補 30 天**——這條分支是 2026-07-22 補上的，原因是「這個功能上線前就已經在自選清單裡」的股票只靠路徑1（新增當下才觸發）永遠等不到資料，本機驗證時實測踩到（既有 29 檔自選股全部卡在 0 筆）。

移除自選不特別處理——`watchlisted_stock_codes()` 重新查詢就會自然排除，舊資料留著（資料量小，不做清理）。

**踩雷**：本機用工具（PowerShell）重啟 `python app.py` 時，子行程繼承的是那個 shell session 當下的環境變數，不是登錄檔即時值——`setx FINMIND_TOKEN` 寫進去之後，沒重新載入 `$env:FINMIND_TOKEN` 就直接 `Start-Process` 會導致爬蟲全部靜默失敗（只在 `crawler_logs` 留一筆 `finmind_token_check`/`failed`，畫面上完全看不出來）。

**券商名稱偶爾亂碼，是 FinMind 上游資料本身的問題，不是我們解碼錯**：實測對比同一 `(stock_id, date, securities_trader_id)` 直接呼叫即時 API 也拿到亂碼（`resp.encoding='utf-8'` 強制設定無效），確認是 FinMind 資料源間歇性把某些券商名稱的字元換成字面上的問號（如 broker_id `6010`/`6012`/`601d`「奔亞」系列，同一 broker_id 在不同日期時而正常時而變成「?亞」）。修法見 `database.py` 的 `_fix_garbled_broker_names()`（每次啟動都跑，用同一 `broker_id` 底下最常見的乾淨名稱覆蓋掉開頭是「?」的列）+ `crawler.py` 的 `crawl_broker_trades()` 寫入前防護（爬到可疑名稱且資料庫已有乾淨名稱時直接用乾淨的）。

**API**：`GET /api/stocks/<code>/broker-trades?days=30` 回傳近 N 天已聚合的原始列（股數，不分日期排序好，單位換算留給前端）。前端（`app.js` 的 `renderBrokerTrades()`）算出「整個時間窗累計買超/賣超前10大券商」決定矩陣的欄位，再攤開成**日期 × 券商矩陣表格**（每一列一天、每一欄一個券商、最後一列合計），比照 `/api/market/summary` 前端算飆股的既有慣例，數字統一 `_brokerLots()` 除以1000四捨五入成張數顯示（2026-07-22 從「選日期看當日前10」改版，使用者要求「不要一天一天查」）。前端只在 `#stock-broker-card`（詳情頁）顯示，且只在使用者自選清單裡有這檔股票時才顯示（`state.watchlists` 找不到就整卡隱藏）。手動測試：`POST /api/crawler/run/broker_trades`。
