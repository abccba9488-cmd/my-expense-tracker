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

## 外部連線（ngrok）

`ngrok.exe` 放在專案根目錄，authtoken 已設定於 `%APPDATA%\Local\ngrok\ngrok.yml`。

雙擊 `ngrok.bat` 即可取得公開 `https://xxxx.ngrok-free.app` 網址，免費版每次重啟網址會變。

**安全限制**：`POST /api/crawler/run/<task>` 僅允許 `127.0.0.1` / `::1` 呼叫，或是已用管理員帳號登入的 session；其他外部連線會收到 403。

## 架構

```
app.py          Flask app + REST API endpoints + 初始化入口
crawler.py      所有爬蟲函式（TWSE / TPEX / MOPS）
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

- **Server side**：`_summary_cache`（`app.py`）快取 JSON 字串，TTL 5 分鐘。`_run_bg()` 在背景任務完成後自動呼叫 `_invalidate_summary_cache()`。
- **Gzip**：`compress_response` after_request handler 自動壓縮 JSON / HTML / CSS / JS 回應。
- **Client side**：`app.js` 以 `localStorage`（key `bao_sum_v1`，TTL 5 分鐘）做 stale-while-revalidate；頁面重載時先渲染快取，背景靜默更新。

## 資料庫 Schema

| 表 | 主鍵 | 單位備註 |
|----|------|---------|
| `stocks` | `code` | market: TWSE \| TPEX |
| `daily_prices` | `(stock_code, date)` | volume 單位：股 |
| `monthly_revenue` | `(stock_code, year, month)` | revenue 千元；`start_price` = 首次寫入當天收盤價，月份切換時才更新；`turnaround_signal` = 潛在虧轉盈候選旗標，每次爬蟲都重算（見下方說明） |
| `quarterly_financials` | `(stock_code, year, quarter)` | revenue/income 千元；eps 元/股；**各季獨立值**（Q4 已非累計） |
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

## _SUMMARY_SQL 欄位索引（r[0]–r[18]）

```
0=code, 1=name, 2=market, 3=industry,
4=close, 5=change_pct, 6=price_date,
7=revenue, 8=revenue_yoy, 9=rev_year, 10=rev_month,
11=eps, 12=eps_year, 13=eps_quarter,
14=qf_revenue, 15=pe_ratio, 16=start_price, 17=ma20, 18=turnaround_signal
```

`ma20`：以相關子查詢取該股最近 20 筆 `daily_prices.close`（`ORDER BY date DESC LIMIT 20`，吃 `ix_dp_code_date` 索引，不用整表掃描）算出的簡單移動平均。前端三個表格（主表格／飆股清單／自選股）最後一欄「20日均」皆呼叫 `app.js` 的 `ma20Cell(s)` 顯示此值，股價落在 `ma20` 上下 3% 內時儲存格變色＋🔔 圖示提示。

`turnaround_signal`：**不是即時計算，是 `crawl_monthly_revenue()`（`crawler.py`）每次爬到新月營收時直接算好存進 `monthly_revenue` 表的**。邏輯：該股最新一季 `quarterly_financials.eps < 0`（還在虧損）**且**本月 `revenue_yoy >= 20`（跟營收飆股用同一個門檻）→ 寫入 1，否則 0；每次爬蟲都重算覆寫（不像 `start_price` 只在新增時寫一次）。前端 `app.js` 的 `turnaroundCell(s)` 為真時顯示 🔥 圖示、假則顯示「—」，**純圖示不塗滿底色**（跟 `ma20Cell` 的變色不同）。三個表格（主表格／飆股清單／自選股）都有這欄；**飆股清單表格（`#star-table`）這欄會永遠顯示「—」**——`calcEst()` 要求 `eps > 0` 才會回傳值，飆股清單本身的篩選邏輯已排除所有虧損股，使用者要求三表一致才加上，不是邏輯漏洞。內容只有「—」/🔥 太窄，三個表格的這一欄都用 `columnDefs` 的 `width: '64px'` 固定寬度，避免 DataTables 自動欄寬把標題擠出欄位（曾經發生過對不齊的問題）。

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

**注意**：APScheduler 的「下次執行時間」在 `sched.start()` 當下計算，若當天排程時間已過（例如 worker 因重新部署在 14:00 後重啟），當天的每日股價排程會被跳過、不會補跑。`app.py` 模組層級已加入**啟動時自動補跑**機制：若當天（平日且時間 ≥14:00）尚無成功的 `daily_price` log，啟動時自動觸發一次 `crawler.crawl_daily_prices`。

手動觸發：`POST /api/crawler/run/<task>`（僅限 localhost 或 admin 登入）；task 值：`stock_list` / `daily_price` / `monthly_revenue` / `quarterly` / `announcements` / `init`。季報觸發自動判斷「最近已公告季度」，可用 `?year=&quarter=` 覆蓋；公告可用 `?date=YYYYMMDD` 覆蓋日期，`?limit=N` 只處理清單前 N 筆做小規模測試（**測試專用，正式排程不要帶這個參數**，否則當天只會處理一部分公告；現在整個流程只需一次 HTTP 請求，正常情況下不需要這個參數來省時間，純粹是想看少量範例輸出時用）。

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
- `database.py` 連線時設定 SQLite PRAGMA（`mmap_size=0`、`cache_size=-2000`、`temp_store=FILE`），避免大型 DB 的 mmap 把記憶體推爆容器限制
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

### 五個視圖

| 視圖 | 說明 |
|------|------|
| `#list-view` | 完整股票列表（DataTables，預設代號升冪） |
| `#star-view` | 營收飆股：`_ratio >= 1.5` **且** `revenue_yoy >= 20%`，依預估倍數降冪 |
| `#watchlist-view` | 自選股清單（需登入）；未登入顯示 `#wl-auth-prompt` |
| `#ann-view` | 自結公告：純表格（不用 DataTables），見下方「自結公告」章節 |
| `#detail-view` | 個股詳情（股價圖、月營收圖、季財報表） |

分頁列（`#page-tabs-bar`）在 detail view 時隱藏；`showListView()` 的 viewMap：`{ star: 'star-view', watchlist: 'watchlist-view', ann: 'ann-view' }`。

### 主表格欄位（18 欄，index 0–17）

代號 → 名稱 → 產業 → **起始股價** → 收盤價 → **價差%** → 漲跌幅% → **營收預估股價** → 營收月份 → 月營收 → 月營收年增% → 季營收 → 最新EPS → 本益比 → EPS期別 → 資料日期 → **20日均** → **虧轉盈**

**價差%**：`(close - start_price) / start_price × 100`，在 `_row_to_dict()` 計算（非來自 SQL），`price_diff` 欄位直接放入 JSON 回傳。正值綠色，負值紅色。

**營收預估股價**公式：`(月營收 / 季營收) × EPS × 240`；紅色格 = 現價 2x+，黃色格 = 1.5x+。

**本益比**計算：Q1–Q3 用 `close / (eps / quarter × 4)`（年化）；Q4 用 `close / year_eps`（`yeps` CTE 全年加總）。

自選股表格（`#wl-table`）欄位與主表格一致（含**起始股價**、**價差%**、**20日均**、**虧轉盈**），但無「季營收」欄；`renderWlTable()` 的 `columnDefs` 索引需與欄位順序同步。飆股清單（`#star-table`）欄位則無**季營收**、**EPS期別**、**資料日期**，最後兩欄是**20日均**、**虧轉盈**（後者在此表必為「—」，見上方 `turnaround_signal` 說明）。

### 重要 gotcha：jQuery `.data()` 型別轉換

jQuery 的 `.data('code')` 會把純數字字串（如 `"1218"`）自動轉為 `number`（`1218`），導致 `state.allData.find(s => s.code === code)` 嚴格比對失敗（API 回傳的 `code` 是 `string`）。`loadStockDetail(code)` 入口第一行已做 `code = String(code)` 正規化，**所有呼叫 `loadStockDetail` 的地方不必再轉型**，但若未來新增其他用 `.data()` 取得 code 再做 find 的邏輯，需注意同樣問題。

### 飆股 / 自選股附加功能

- `downloadStarCsv()`：下載飆股清單為 CSV
- `copyStarForAI()` / `copyWlForAI()`：將清單連同分析 prompt 複製到剪貼簿，旁邊有 Gemini 連結（新分頁開啟）

### 通知系統

每 15 秒 poll `/api/crawler/status`，偵測到新 `success` log 時用 `Notification` API 送桌面通知；若權限被拒則改用 Toast。

### 固定面板 / 抽屜

- `#status-panel`（⚙ 爬蟲狀態）、`#today-panel`（🆕 今日更新，呼叫 `/api/updates/today`）：右下／左下角浮動面板，`.hidden` 切換顯示。
- `#msg-panel`（💬 留言板）：右側滑出抽屜（`.msg-drawer.open` 切換），`loadMessages()` 載入、`sendMessage()` 發表、依 `can_delete` 顯示刪除按鈕。

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

**除錯注意**：在 Zeabur 終端機貼含中文字的程式碼/heredoc 時，**終端機本身會在中文字之間插入空格**，不只是顯示問題，連貼上去的程式碼內容都會被改掉（例如 `re.compile('主旨')` 會變成 `re.compile('主 旨 ')` 導致比對失效）。之後要請使用者在終端機跑診斷用的 Python 腳本時，**程式碼裡絕對不要放新的中文字面值**，只能重用 `crawler.py` 裡已經部署好的常數/regex（如 `crawler._EPS_LABEL_RE`），或單純印出結構（不靠中文比對）讓人眼判讀。

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
