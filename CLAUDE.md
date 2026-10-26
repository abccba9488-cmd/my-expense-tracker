# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概述

**飆股網** — 台灣上市櫃股票分析網站，本機執行，單一 Python Flask 後端 + 單頁 HTML 前端。無建置工具，無測試框架。

**功能細節都拆到 `docs/`**：本檔只放每次都需要的內容（指令、架構、schema、排程、跨功能 gotcha）。**改某個功能前，先讀對應的 docs 檔**——裡面記錄了設計理由、回測結果與踩過的雷，跳過很容易重犯。文中「見○○章節」若本檔找不到，用 `grep -n '^## ' CLAUDE.md docs/*.md` 定位。

| 檔案 | 內容 |
|------|------|
| `docs/experts.md` | 達人選股（`experts.py` 全部規則、計分引擎、前端）、持股健康檢查、投資組合壓力測試、股泰/甜蜜點回測、主力吸貨、投信買點 |
| `docs/known-issues.md` | 資料品質已知問題（`financial_extra` 千元單位、Q4 存成全年累計值、股本變動造成 Q4 EPS 失真、FinMind 資產負債表缺季）——**動到財報欄位計算前必讀** |
| `docs/chanlun.md` | 纏論（`chanlun.py`）、力道K線（`force_kline.py`） |
| `docs/chips.md` | 券商分點進出、籌碼峰（`chip_peak.py`） |
| `docs/backtests.md` | 纏論買點×籌碼峰、達人選股訊號組合（`flag888_guyu`/`wl823_pullback` 也在這）、籌碼峰匯聚訊號回測 |
| `docs/taifex.md` | 期權籌碼分析（`crawler_taifex.py`/`taifex_analysis.py`/Fear & Greed） |
| `docs/announcements-ai.md` | 自結公告（爬蟲＋解析＋評級）、AI 個股分析（OpenRouter 付費）、個股筆記 |
| `docs/frontend.md` | PWA、八個視圖、主表格欄位、手機版、通知、抽屜、K線圖、上下一檔導覽、總體分析、甜蜜點/虧轉盈/殖利率欄位細節、前端 gotcha 完整說明 |
| `docs/api.md` | REST API 端點清單、`/admin` 後台 |
| `docs/ops.md` | Zeabur 部署（已停用）、backfill 歷史補齊、個人輔助腳本、分析與驗證、已存未呈現的 DB 欄位、**事故紀錄**（token 斷線、watchdog、running 卡住、threaded、冷查詢） |

**雲端已停用**：2026-07-22 起不再部署 Zeabur，本機是唯一執行環境。程式與文件中殘留的 Zeabur／gunicorn／`_IS_CLOUD` 分支都是歷史遺跡，不必為它們做驗證。

## 環境變數

| 變數 | 必要性 | 用途 |
|------|--------|------|
| `FINMIND_TOKEN` | 達人選股/三大法人/期權籌碼等 FinMind 相關功能必要 | 沒設定會靜默失敗，見下方「排程」章節 |
| `OPENROUTER_API_KEY` | 「AI 個股分析」功能才需要 | 見 `docs/announcements-ai.md`「AI 個股分析」，未設定則該功能無法使用（其餘功能不受影響） |
| `OPENROUTER_MODEL` | 選用，預設 `perplexity/sonar` | 同上，覆寫 AI 個股分析用的模型 |

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

**沒有測試框架／linter**，改完後的最低限度檢查：
```
node --check static/js/app.js                         # JS 語法
C:\Users\user\anaconda3\python.exe -m py_compile app.py crawler.py   # Python 語法（換成實際改到的檔案）
```
其餘驗證是實際跑本機伺服器／爬蟲／回測腳本看結果。

**Windows 主控台中文亂碼**：直接用 python 印出 DB 內的中文（股票名稱等）會變成 `���s` 之類的亂碼（主控台 cp950）。一次性腳本／`-c` 指令加 `-X utf8`（或設 `PYTHONIOENCODING=utf-8`）再看輸出。

**改 `templates/*.html` 必須重啟伺服器才生效**（`debug=False`，Jinja 快取模板）；改 `static/js`／`static/css` 不用重啟（mtime 版號自動換網址）。詳見「籌碼峰」章節同名 gotcha。

**Claude Code 內重啟伺服器**一律用 PowerShell 工具（不要用 Bash 跑 `start.bat`／`_server.bat`——`start` 分離出去的子行程會在工具呼叫結束時被回收），且同一個指令內先載入 `FINMIND_TOKEN`（原因見下方「排程」章節踩雷）：
```powershell
$old = Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique
foreach ($p in $old) { Stop-Process -Id $p -Force -Confirm:$false -ErrorAction Stop }
Start-Sleep -Seconds 2
$env:FINMIND_TOKEN = [Environment]::GetEnvironmentVariable("FINMIND_TOKEN","User")
Start-Process -FilePath "C:\Users\user\anaconda3\python.exe" -ArgumentList "app.py" -WorkingDirectory "C:\Users\user\Documents\claude\VSCode\stock-analysis" -WindowStyle Minimized
$new = $null
for ($i = 0; $i -lt 15 -and -not $new; $i++) { Start-Sleep -Seconds 2; $new = (Get-NetTCPConnection -LocalPort 5000 -State Listen -ErrorAction SilentlyContinue).OwningProcess }
"old=$old new=$new"
```
重啟前確認沒有正在跑、會呼叫本站 API 的背景工作（例如 `/rate-announcements`）。**最後印出的 new PID 必須存在且跟 old 不同**——2026-10-25 曾發生舊版指令的 `Stop-Process`（沒加 `-ErrorAction Stop`）沒殺掉舊行程又沒報錯，新行程照樣啟動但 5000 port 仍由舊行程服務、API 回的是舊程式碼結果；現在改成殺不掉就直接報錯，並輪詢等新行程真的開始監聽（冷啟動常超過 8 秒）才印 PID。

**改了 `experts.py`／`chanlun.py` 等計分邏輯，一定要重啟伺服器**：在跑的 Flask 行程還是舊程式碼，`POST /api/crawler/run/expert_scores` 會用舊邏輯重算，而且每天 17:00 排程也會用舊邏輯把結果蓋回去。想不經伺服器直接重算（約 2 分鐘）：`C:\Users\user\anaconda3\python.exe -c "import experts; experts.compute_expert_scores()"`。

**本機開機自動啟動**：`autostart_server.bat`（`start.bat` 的背景版，不開瀏覽器）由 `shell:startup` 資料夾的捷徑在登入時啟動；捷徑不在 git 內，為何不用工作排程器見 `docs/ops.md`。

**`.bat` 檔案裡不要放中文註解**：`cmd.exe` 對多位元組字元處理不可靠，中文 `rem` 可能被錯誤斷行、後半段被當指令執行（踩過）。需要說明就用英文。

## 外部連線（ngrok）

`ngrok.exe` 放在專案根目錄，authtoken 已設定於 `%APPDATA%\Local\ngrok\ngrok.yml`。

雙擊 `ngrok.bat` 即可取得公開 `https://xxxx.ngrok-free.app` 網址，免費版每次重啟網址會變。

`cloudflared.bat` 是替代方案（Cloudflare Quick Tunnel，`cloudflared.exe`，同樣不在版控內、`.gitignore` 排除），免帳號、無流量限制，網址 `https://xxxx.trycloudflare.com` 同樣每次重啟會變；兩者擇一即可，不需要同時開。

**安全限制**：`POST /api/crawler/run/<task>` 僅允許 `127.0.0.1` / `::1` 呼叫，或是已用管理員帳號登入的 session；其他外部連線會收到 403。

## 架構

```
app.py          Flask app + REST API endpoints + 初始化入口
crawler.py      所有爬蟲函式（TWSE / TPEX / MOPS / FinMind / 董監持股 OpenAPI / 集保 OpenData＋MoneyDJ ETF成份股）
finmind_client.py  FinMind API 薄封裝（crawler.py 的 crawl_finmind_* 函式呼叫）
technical.py    純 Python 技術指標（EMA/MACD/RSI/KD），達人選股股泰規則用
chip_peak.py    純 Python 籌碼峰計算（POC/VAH/VAL，時間衰減＋法人品質加權），見「籌碼峰」章節
chanlun.py      純 Python 纏論計算（K線合併/分型/筆/中樞/背馳/買賣點），見「纏論」章節
experts.py      達人選股計分引擎（`SCORERS` 共 19 套：9 套公開規則 + 10 套實驗規則，其中 5 套 admin-only），見「達人選股」章節
crawler_taifex.py    台指期貨/選擇權籌碼爬蟲（FinMind），見「期權籌碼分析」章節
taifex_analysis.py   期權籌碼衍生計算（PC Ratio/支撐壓力/大戶多空比），見「期權籌碼分析」章節
crawler_fear_greed.py  CNN Fear & Greed Index 爬蟲（唯一非台股/非FinMind資料源），見「期權籌碼分析」章節
force_kline.py           純 Python 力道K線分數計算（個人專屬實驗功能，尚未接前端），見「力道K線」章節
backtest_force_kline.py  力道K線訊號回測腳本（僅本機CLI，不寫DB），見「力道K線」章節
backtest_chanlun_chippeak.py  纏論買點+籌碼峰VAL/VAH+營收飆股進出場模擬（僅本機CLI，不寫DB），見「纏論買點×籌碼峰回測」章節
portfolio_risk.py  投資組合壓力測試（自選股整包風險分析），見「投資組合壓力測試」章節
backtest_*.py   各策略回測，全部是獨立唯讀 CLI（不寫 DB、不經 Flask），結果輸出同名 *_result.json / *.log（不進 git）
scheduler.py    APScheduler 排程（BackgroundScheduler，Asia/Taipei）
database.py     SQLAlchemy models + SQLite 設定 + migration
templates/index.html   單頁前端（DataTables + Chart.js，CDN）
templates/admin.html + static/js/admin.js  後台管理頁（/admin），見「後台管理頁面」章節
.claude/skills/        專案內 Claude Code skills：analyze-stock（單檔估值報告）、rate-announcements（自結公告用 ChatGPT 網頁版人工評級，需 claude-in-chrome）
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

- **Server side**：`_summary_cache`（`app.py`）快取 JSON 字串，TTL 30 分鐘（2026-07-16 從 5 分鐘拉長，因為冷查詢很貴，見下方）。`_run_bg()` 在背景任務完成後自動呼叫 `_invalidate_summary_cache()`。
- **Gzip**：`compress_response` after_request handler 自動壓縮 JSON / HTML / CSS / JS 回應。
- **Client side**：`app.js` 以 `localStorage`（key `bao_sum_v1`，TTL 5 分鐘）做 stale-while-revalidate；頁面重載時先渲染快取，背景靜默更新。

**`_SUMMARY_SQL` 冷查詢約 25 秒**（四條均線是每檔各自的關聯子查詢）：已用 `_summary_rebuild_lock` 做 single-flight，`database.py` 在 Windows 用大 SQLite 快取。每次重啟伺服器都會清空 `_summary_cache`，改非 DB 相關程式碼時能不重啟就不重啟。完整經過見 `docs/ops.md`「事故紀錄」。

## 資料庫 Schema

| 表 | 主鍵 | 單位備註 |
|----|------|---------|
| `stocks` | `code` | market: TWSE \| TPEX |
| `daily_prices` | `(stock_code, date)` | volume 單位：股；`per`/`pbr`/`dividend_yield` 來自 FinMind，達人選股用 |
| `monthly_revenue` | `(stock_code, year, month)` | revenue 千元；`start_price` = 首次寫入當天收盤價，月份切換時才更新；`turnaround_signal` = 潛在虧轉盈候選旗標，每次爬蟲都重算（見下方說明） |
| `quarterly_financials` | `(stock_code, year, quarter)` | revenue/income 千元；eps 元/股；**各季獨立值**（Q4 已非累計） |
| `institutional_trades` | `(stock_code, date)` | 三大法人買賣超（股），FinMind，達人選股用 |
| `margin_trades` | `(stock_code, date)` | 融資融券餘額（張），FinMind bulk，2016-01 起，主力吸貨規則用，見「主力吸貨」章節 |
| `holding_concentration` | `(stock_code, date)` | 股權分散表（週資料），FinMind，達人選股用 |
| `financial_extra` | `(stock_code, year, quarter)` | 資產負債表/現金流量表/毛利項目（千元），FinMind，獨立於 MOPS 來源的 `quarterly_financials` |
| `dividend_policy` | `(stock_code, event_date)` | 逐筆股利分派事件（非年度加總），FinMind。個股詳情頁「股利政策」區塊有「回補最新資料」按鈕（2026-08-23 新增，任何登入使用者可用）——`crawler.backfill_dividend_policy(code)` 對這一支股票用 `data_id` 查全部歷史（已用真實 API 驗證單一股票、寬日期範圍一次查詢可靠，不像下方兩個 crawler 函式要逐日查詢），修過去 `FINMIND_TOKEN` 未設定期間造成的資料缺口，也能單純確認某股票近期真的沒有新股利事件 |
| `dividend_fill_events` | `(stock_code, ex_date)` | 除權息事件 + 填息判斷，FinMind |
| `director_holdings` | `(stock_code, year_month)` | 董監持股比例，TWSE/TPEX OpenAPI（非 FinMind） |
| `etf_holdings` | `(etf_code, stock_code)` | 受益人數前5大ETF成份股（集保 OpenData 排名＋MoneyDJ 月底持股），每次整表覆蓋，`etf_down_stroke_hold` 用，見 `docs/chanlun.md` |
| `broker_trades` | `(stock_code, date, broker_id)` | 券商分點單日買賣超（股），FinMind `TaiwanStockTradingDailyReport`，見 `docs/chips.md`「券商分點進出」 |
| `expert_scores` | `(stock_code, expert_key)` | 達人選股每套規則最新一次計分快取；`entered_at`/`transition` 是唯二跨執行延續（不覆寫）的欄位，見 `docs/experts.md`「達人選股」 |
| `users` | `id` | 會員帳號，`password_hash` 用 werkzeug |
| `watchlists` | `id` | 屬於某 `user_id`，可多個 |
| `watchlist_stocks` | `(watchlist_id, stock_code)` | 自選股關聯表 |
| `messages` | `id` | 全站留言板；`user_id`/`username`/`content`/`created_at` |
| `crawler_logs` | `id` | status: running \| success \| failed |
| `announcements` | `id` | UniqueConstraint(`stock_code`, `seq_no`)；自結公告爬蟲結果，見 `docs/announcements-ai.md`「自結公告」 |
| `stock_ai_analysis` | `stock_code` | 單檔股票最新一次 AI 估值分析快取，見 `docs/announcements-ai.md`「AI 個股分析」 |
| `stock_notes` | `stock_code` | AI分析筆記（自由文字，不綁定任何 AI 呼叫）——所有使用者可讀，僅管理員可寫，見 `docs/announcements-ai.md`「個股筆記」 |
| `taifex_futures_daily` | `(contract_date, date)` | 台指期貨(TX)每日行情，只存日盤，FinMind，見 `docs/taifex.md`「期權籌碼分析」 |
| `taifex_option_daily` | `(contract_date, date, strike_price, call_put)` | 台指選擇權(TXO)每日行情，逐履約價，資料量大（180天約41萬列） |
| `taifex_futures_institutional` | `(date, institutional_investors)` | 三大法人期貨(TX)買賣，含成交淨口數與未平倉餘額兩組欄位——後者才是「淨部位」對應的量級，見章節內的踩雷記錄 |
| `taifex_option_institutional` | `(date, call_put, institutional_investors)` | 三大法人選擇權(TXO)買賣，含契約金額 |
| `taifex_futures_institutional_mini` | `(date, futures_id, institutional_investors)` | 三大法人小台(MTX)/微台(TMF)期貨買賣，跟大台合併算「約當大台」用 |
| `taifex_futures_large_traders` | `(date, contract_type)` | 十大交易人期貨(TX)未沖銷部位 |
| `taifex_option_large_traders` | `(date, call_put, contract_type)` | 十大交易人選擇權(TXO)未沖銷部位，無契約金額欄位 |
| `taifex_option_vix` | `date` | 臺指選擇權波動率指數(TW VIX)，每天只存收盤那一筆（原始為盤中逐筆），FinMind 只回溯到2026-03-02 |
| `cnn_fear_greed_index` | `date` | CNN Fear & Greed Index，來源非官方API（非FinMind），美股行事曆日期，見「期權籌碼分析」章節 |
| `market_index` | `date` | TAIEX 加權指數每日開高低收＋成交量值（TWSE `exchangeReport/FMTQIK` 收盤/漲跌/量值 ＋ `indicesReport/MI_5MINS_HIST` 開高低，都是一次一整月、按日期合併），股票總覽頂端 K 線＋纏論圖用（`GET /api/market/index`、`/api/market/index/chanlun`）；`change_pct` 由爬蟲推算 |
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

- `dividend_yield`：用 `ldy` CTE 取「最近一筆有值的日期」，不是嚴格最新日期（估值爬蟲常落後股價爬蟲）。
- `ma20`/`ma60`/`ma120`/`ma240`：前端「甜蜜點」欄（`sweetSpotCell()`）。20/60/120 是對稱 ±3%，240 刻意不對稱（在線下或剛站上 3% 內才算）。
- `turnaround_signal`：由 `crawl_monthly_revenue()` 寫入，不是即時計算（最新季 EPS<0 且營收年增 ≥20%）。飆股清單這欄永遠是「—」是預期行為。

完整規則與沿革見 `docs/frontend.md`「表格欄位細節與前端 gotcha」。

## 爬蟲資料來源

| 資料 | 端點 | 格式 |
|------|------|------|
| 股票清單 | `isin.twse.com.tw/isin/C_public.jsp?strMode=2/4` | Big5 HTML，只取 `^\d{4}$` 代號 |
| TWSE 每日股價 | `twse.com.tw/exchangeReport/MI_INDEX?type=ALL` | JSON `tables[]`（2025+ 新格式）；漲跌方向為 HTML `color:red/green` |
| TPEX 每日股價 | `tpex.org.tw/.../stk_wn1430_result.php?se=AL` | JSON `tables[0].data`（2025+ 新格式）；volume 單位為股 |
| 月營收 | `mops.twse.com.tw/mops/api/t05st10_ifrs` | POST JSON；per-company；`data[0][1]`=當月營收，`data[3][1]`=年增率 |
| 季財報 EPS | `mops.twse.com.tw/mops/api/t164sb04` | POST JSON；`reportList` 陣列，關鍵字比對列標籤取值 |
| 自結公告 | `mopsov.twse.com.tw/mops/web/ajax_t05st02` | POST form（TYPEK=all, year/month/day ROC）→ HTML；單次回應即含當天全部公告的完整主旨/說明（藏在隱藏 `<input>` 欄位裡），**不需要、也不要額外發詳情頁請求**（細節見「自結公告」章節） |
| ETF 受益人數排名 | `opendata.tdcc.com.tw/getOD.ashx?id=1-5` | 集保股權分散表 CSV（全市場、每週），持股分級 17＝合計人數；證券代號右側補空白要 strip |
| ETF 成份股 | `moneydj.com/ETF/X/Basic/Basic0007a.xdjhtm?etfid=<code>.TW` | HTML 全持股表 `id=Repeater1`（月底資料，只有名稱沒代號）；回應無 charset，要手動 `resp.encoding='utf-8'` |

**SSL 注意**：TWSE/TPEX/MOPS 憑證有問題，`crawler.py` 用 `_session.verify = False` 統一處理。所有請求必須走 `_get()` / `_post()` 包裝函式，不可直接呼叫 `_session.get/post` 或裸的 `requests`。

**防爬蟲機制**（`crawler.py` 頂部）：
- `_get()` / `_post()`：統一入口，每次請求隨機挑選 UA、帶完整瀏覽器 headers（含 `Sec-Fetch-*`、`Origin`、`Cache-Control`）、429/5xx 與連線層級例外自動重試最多 3 次
- UA 池含 Chrome 136、Firefox 138、Safari 17、Edge 136 共 9 組，定期輪替
- `_jitter(base)`：`time.sleep(base × random(0.7, 1.6))`，消除固定間隔特徵
- 每 80 次請求清除一次 session cookie
- **`Accept-Encoding` 不可加 `br`**：執行環境不保證有裝 `brotli`（當初在已停用的 Zeabur 容器踩到），若伺服器回傳 Brotli 壓縮內容會導致 `resp.json()` 解析失敗，整批資料變成 0 筆但 task 仍顯示 success。只用 `gzip, deflate`
- TWSE/TPEX JSON 解析失敗或 `stat != 'OK'` 時會記錄 `logger.warning`（含狀態碼與回應大小），看本機伺服器 log 排查

月營收爬蟲在新增記錄時，會查 `daily_prices` 最新收盤價寫入 `start_price`；更新既有記錄時不修改 `start_price`。`turnaround_signal`（虧轉盈候選旗標）則相反，新增與更新都會重算覆寫，見上方「_SUMMARY_SQL 欄位索引」說明。

季財報爬蟲：抓到 Q4 時，從 DB 取出同年 Q1/Q2/Q3 相減後再存入，確保存的是個別季數值。

## 排程（APScheduler）

| 任務 | 觸發時間（Asia/Taipei） |
|------|---------|
| 股票清單 | 每週日 01:00 |
| 每日股價 | 週一〜五 14:00 與 15:00（各跑一次，避免單次失敗漏抓） |
| 每日股價（watchdog） | 週一〜五 14:00–17:00 每 30 分鐘檢查一次，若當天還沒有成功的 `daily_price` log 就補爬一次（`_daily_price_watchdog`，啟動時也會立即跑一次） |
| 加權指數（2026-10-21 新增） | 週一〜五 14:40、15:40，啟動時也跑一次；表內少於 250 筆（纏論分析窗）或有缺開高低的列時自動回補 15 個月（`_market_index_job`） |
| 月營收 | 每天 23:00（爬上個月；部分公司公布較晚，每天重抓直到有資料） |
| 月營收（watchdog，2026-08-05 新增） | 每天 23:00–06:00 每 30 分鐘檢查一次，若今天還沒有成功的 `monthly_revenue` log、且沒有一個「40 分鐘內剛啟動」的 `running` log 就補跑（`_monthly_revenue_watchdog`，啟動時也會立即跑一次），見下方排程踩雷重點 |
| 自結公告 | 週一〜五 05:00（非尖峰，爬前一交易日的 MOPS 重大公告） |
| 自結公告（測試用，**暫時性**） | 每 30 分鐘重爬「今天」的公告（`_announcements_test_job`），讓當日新公告不用等隔天 05:00。改成從清單頁直接解析（無需逐筆詳情頁請求）後單次執行只需數秒，不再有效能負擔；要調整頻率或正式移除這個 job 前先問使用者 |
| Q1 | 5 月每天 23:00（公告期限 5/15） |
| Q2 | 8 月每天 23:00（公告期限 8/14） |
| Q3 | 11 月每天 23:00（公告期限 11/14） |
| Q4 | 隔年 3 月每天 23:00（公告期限 3/31） |
| 季財報（watchdog，2026-08-05 新增） | 公告月份內每天 23:00–06:00 每 30 分鐘檢查一次，若今天還沒有成功的 `quarterly` log 就補跑（`_quarterly_watchdog`），跟月營收 watchdog 同一套邏輯 |
| 達人選股（FinMind 增量 + 重算分數） | 每天 17:00（股價爬蟲與 watchdog 之後），見「達人選股」章節 |
| 達人選股（FinMind，watchdog，2026-07-16 新增） | 週一〜五 17:00 起每 30 分鐘檢查一次，若當天還沒有成功的 `finmind_institutional` log 就補跑整個 `_finmind_job()`（`_finmind_watchdog`，啟動時也會立即跑一次），跟 `_daily_price_watchdog` 同一個模式，補的是「程序在 17:00 當下沒在跑」這種情況 |
| 達人選股（financial_extra） | 與官方季報同月份、每天 23:30（比官方季報 job 晚 30 分） |
| 券商分點進出（2026-07-22 新增） | 週一〜五 17:30（達人選股 FinMind job 之後），只對目前有人自選的股票抓當天，見「券商分點進出」章節 |
| 期權籌碼（期貨/選擇權每日行情＋十大交易人，2026-08-20 新增） | 週一〜五 17:00（`_taifex_job`，跟達人選股 FinMind 同批次，資料約16:30更新），見「期權籌碼分析」章節 |
| 期權籌碼（三大法人期貨/選擇權買賣，含小台/微台） | 週一〜五 18:30（`_taifex_institutional_job`，獨立排程晚一點跑，因為這幾個 FinMind dataset 約16:00/18:00才更新） |

**注意**：APScheduler 的「下次執行時間」在 `sched.start()` 當下計算，若當天排程時間已過（例如伺服器在 14:00 後重啟），當天的每日股價排程會被跳過、不會補跑。`app.py` 模組層級已加入**啟動時自動補跑**機制：若當天（平日且時間 ≥14:00）尚無成功的 `daily_price` log，啟動時自動觸發一次 `crawler.crawl_daily_prices`。

**排程相關踩雷重點**（完整經過見 `docs/ops.md`「事故紀錄」）：
- **`FINMIND_TOKEN` 沒設定會靜默失敗**：`finmind_*` 每天照樣觸發，但每次都馬上 failed，`director_holdings` 不需金鑰，仍然照常成功，容易誤判「排程正常」。啟動時若沒設定，會寫入一筆 `finmind_token_check` failed log。修法：`setx` 之後**重啟**伺服器，已開著的 session 拿不到新值。
- **`crawl_finmind_*` 不會回填缺口**：每次只抓單日。watchdog 只會補「今天」，多日斷線要手動逐日呼叫回填。
- **watchdog 只看有沒有 success log，不看資料量**：資料殘缺時，要用個股頁的 `POST /api/stocks/<code>/institutional-trades/refresh` 手動補。
- **月營收／季報可能卡在 `running`**（例如電腦睡眠凍結執行緒）：由 `_monthly_revenue_watchdog`/`_quarterly_watchdog` 處理，條件是今天沒有 success，且沒有 40 分鐘內啟動的 running。
- **`app.run(..., threaded=True)` 不可拿掉**：單執行緒時，背景跑重任務會讓網站看起來當機。

手動觸發：`POST /api/crawler/run/<task>`（僅限 localhost 或 admin 登入）；task 值：`stock_list` / `daily_price` / `monthly_revenue` / `quarterly` / `announcements` / `init` / `finmind_data` / `broker_trades` / `director_holdings` / `etf_holdings` / `expert_scores` / `taifex_data`（等同依序呼叫 `_taifex_job()` + `_taifex_institutional_job()`）/ `market_index`。季報觸發自動判斷「最近已公告季度」，可用 `?year=&quarter=` 覆蓋；公告可用 `?date=YYYYMMDD` 覆蓋日期，`?limit=N` 只處理清單前 N 筆做小規模測試（**測試專用，正式排程不要帶這個參數**，否則當天只會處理一部分公告；現在整個流程只需一次 HTTP 請求，正常情況下不需要這個參數來省時間，純粹是想看少量範例輸出時用）。`finmind_data` 等同 `_finmind_job()`（5 個 FinMind 增量函式 + 董監持股 + 前5大ETF成份股 + 重算 `expert_scores`）；`expert_scores` 只重算分數不重新爬資料，改規則邏輯後想立即看結果時用。

**新增一個爬蟲任務要同步的地方**：①`crawler.py`（或 `crawler_*.py`）的 `_log('<task>', ...)` task 名稱 → ②`app.py` `_HEALTH_TASKS`（同名，後台健康面板才看得到） → ③`app.py` `POST /api/crawler/run/<task>` 加分支 → ④`scheduler.py` job + `start()` 註冊 → ⑤本檔排程表與上方手動觸發 task 清單。新資料表另外要在 `database.py` 加 model（`init_db()` 的 `create_all` 會自動建表，之後加欄位才需要 ALTER migration）。

## 前端重要 gotcha（其餘前端細節見 `docs/frontend.md`）

- **jQuery `.data('code')` 會把 `"1218"` 轉成 number**：`loadStockDetail()` 入口已用 `String()` 正規化。其他地方用 `.data()` 取 code 再 `find` 時，要自己轉型。
- **`.DataTable()` 表格要橫向捲動，請用 `scrollX: true`**：不要用 `.table-scroll` 包 wrapper div，DataTables 會把它丟掉，整頁就會被撐爆。
- **不要重用 `.filter-btn` class**：`app.js` 市場篩選會對所有 `.filter-btn` 掛全域 handler。分頁按鈕外觀請用 `.bt-tab`。
