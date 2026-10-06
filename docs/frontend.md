# 前端細節

> 從 `CLAUDE.md` 拆出（2026-10-06，內容未改寫）。文中「見○○章節」若不在本檔，用 `grep -n '^## ' CLAUDE.md docs/*.md` 找。

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

### 八個視圖

| 視圖 | 說明 |
|------|------|
| `#list-view` | 完整股票列表（DataTables，預設代號升冪） |
| `#star-view` | 營收飆股：`_ratio >= 1.5` **且** `revenue_yoy >= 20%`，依預估倍數降冪 |
| `#watchlist-view` | 自選股清單（需登入）；未登入顯示 `#wl-auth-prompt` |
| `#ann-view` | 自結公告：純表格（不用 DataTables），見下方「自結公告」章節 |
| `#expert-view` | 達人選股：16 套規則切換分頁（9套公開+7套實驗性（含 2026-10-05 的主力吸貨），其中 `chanlun_star`/`flag888_guyu`/`wl823_pullback` 3套 admin-only、非管理員看不到這幾個分頁），見下方「達人選股」章節 |
| `#detail-view` | 個股詳情（股價圖、月營收圖、季財報表、達人選股評分卡、上一/下一檔導覽） |
| `#macro-view` | 總體分析：機構級研究備忘錄提示詞產生器（純前端，不呼叫任何 AI API），見下方「總體分析」章節 |
| `#taifex-view` | 期權籌碼分析（**需登入，不限管理員**，nav 分頁與整個 view 都掛 `login-only hidden`），見下方「期權籌碼分析」章節 |

分頁列（`#page-tabs-bar`）在 detail view 時隱藏；`showListView()` 的 viewMap：`{ star: 'star-view', watchlist: 'watchlist-view', ann: 'ann-view', expert: 'expert-view', macro: 'macro-view', taifex: 'taifex-view' }`。

### 主表格欄位（18 欄，index 0–17）

代號 → 名稱 → 產業 → **起始股價** → 收盤價 → **價差%** → 漲跌幅% → **營收預估股價** → 營收月份 → 月營收 → 月營收年增% → 季營收 → 最新EPS → 本益比 → EPS期別 → 資料日期 → **甜蜜點** → **虧轉盈**

**價差%**：`(close - start_price) / start_price × 100`，在 `_row_to_dict()` 計算（非來自 SQL），`price_diff` 欄位直接放入 JSON 回傳。正值綠色，負值紅色。

**營收預估股價**公式：`(月營收 / 季營收) × EPS × 240`；紅色格 = 現價 2x+，黃色格 = 1.5x+。

**本益比**計算（2026-09-30 修正）：`close / 近四季EPS合計`（`_SUMMARY_SQL` 的 `ttm` CTE，須連續 4 季都有 EPS，否則 NULL），對齊證交所／FinMind 官方 PER（抽樣 300 檔中位數比值 1.006）。舊公式 Q1–Q3 用 `close / (eps / quarter × 4)`，但 `quarterly_financials.eps` 是**單季值**不是累計值，導致 Q2 高估約 2 倍、Q3 約 3 倍——別改回去。**同一公式另有一份 Python 版在 `crawler.py`（AI 個股分析組 prompt 處），改動要兩邊同步**。

自選股表格（`#wl-table`）欄位與主表格一致（含**起始股價**、**價差%**、**纏論買點**、**甜蜜點**、**虧轉盈**），但無「季營收」欄；`renderWlTable()` 的 `columnDefs` 索引需與欄位順序同步。飆股清單（`#star-table`）欄位則無**季營收**、**EPS期別**、**資料日期**，最後三欄依序是**纏論買點**、**甜蜜點**、**虧轉盈**（虧轉盈在此表必為「—」，見上方 `turnaround_signal` 說明）。

纏論買點欄資料不在 `state.allData`（`/api/market/summary`）裡，前端另外呼叫公開端點 `/api/experts/chanlun_buy`（`app.js` `_loadStarChanlunMap()`，快取成 `code → 一/二/三買` 的 map，只收 `passed=true` 的列，`#star-table`／`#wl-table` 共用同一份快取），首次渲染時非同步抓取、抓到後再各自觸發一次 `renderStarTable()`／`renderWlTable()`（後者僅在 `wlDt` 已存在時才觸發，避免自選股頁面尚未開過就被硬拉起）補上欄位值；欄位用 `[sortValue, displayHTML]` pair（`_starChanlunCell()`）比照 `sweetSpotCell()` 的 render 慣例做排序（三買=3、二買=2、一買=1、無訊號=0）。

### 手機版注意事項

`#nav` 的 `.nav-right`（右側圖示列）、`.page-tabs-bar` 的 `.page-tabs`、以及 `.filter-group`（達人選股的 8 個規則分頁、飆股/自選股的市場篩選鈕都共用這個 class）在內容超過螢幕寬度時，統一用「`overflow-x:auto` + 子項 `flex-shrink:0` + 隱藏捲軸」讓它變成可橫向滑動的一排，而不是讓文字換行撐爆版面。新增任何會塞進這幾個容器的按鈕/圖示，不需要額外處理，會自動吃到這個捲動行為。

### 飆股 / 自選股附加功能

- `downloadStarCsv()`：下載飆股清單為 CSV
- `copyStarForAI()` / `copyWlForAI()`：將清單連同分析 prompt 複製到剪貼簿，旁邊有 Gemini 連結（新分頁開啟）

**「自結eps<20」清單的入榜日期**（2026-10-06）：`watchlist_stocks.added_at`（DATE，新增時由 `api_wl_add_stock` 寫入台北日期；`/api/watchlists` 每個清單多回傳 `added_at: {code: 日期}`，`codes` 陣列格式不變，`/rate-announcements` skill 照舊可用）。`renderWlTable()` 只在清單名稱為 `_WL_EPS20_NAME`（'自結eps<20'）時，把第 15 欄「資料日期」換成「入榜日期」（表頭用 `wlDt.column(15).header()` 動態改字）。欄位上線前已在清單裡的股票由 `init_db()` 一次性 migration `backfill_wl_eps20_added_at` 回推：2026-09-07（自動加入規則開始日）之後第一筆預估本益比 0~20 的自結公告日，沒有則取最近一筆符合的；兩者都沒有（手動加入的，如 5475/5608）維持 NULL 顯示「—」。其他清單的 `added_at` 舊資料都是 NULL，目前也不顯示。

**「自結eps<20」回測（2026-10-06）**：`backtest_eps20.py`（僅本機 CLI，不寫 DB）用 `announcements` 全部歷史（2023 起約 2,100 筆）重現入榜條件——公告日下一個交易日開盤買進、停損/移動停利出場（`--stop/--trail`）、不加碼，比較預估本益比 0~20／>=20／虧損／全部公告／流動股每20日定期進場對照組。結果：0~20 組在 10%/15%/20% 下每筆平均 +2.21%/+2.50%/+4.84%，三種設定都優於其他公告組與對照組（+0.99%/+0.80%/+2.19%）。使用者後來表示只需看清單本身（近一個月入榜的少數幾檔，樣本太小只能當觀察），另外比較過「入榜後等收盤回到前5日均線才買」：會錯過不回檔的強勢股（如柏騰），整體沒有比隔日開盤買進好。

### 通知系統

每 15 秒 poll `/api/crawler/status`，偵測到新 `success` log 時用 `Notification` API 送桌面通知；若權限被拒則改用 Toast。

### 固定面板 / 抽屜

- `#status-panel`（⚙ 爬蟲狀態）、`#today-panel`（🆕 今日更新，呼叫 `/api/updates/today`）：右下／左下角浮動面板，`.hidden` 切換顯示。
- `#msg-panel`（💬 留言板）：右側滑出抽屜（`.msg-drawer.open` 切換），`loadMessages()` 載入、`sendMessage()` 發表、依 `can_delete` 顯示刪除按鈕。

### 個股詳情頁：股價走勢 K 線圖（2026-09-30 從收盤價折線改成 K 線＋5/20/60日線）

- `renderPriceChart()` 仍是 Chart.js `type:'line'` 的 category 軸圖表，K 線用兩個重疊的 floating bar dataset 畫（`_wick`＝`[low, high]` 1px 細條、`K線`＝`[open, close]` 實體，`grouped:false`），**不引入 chartjs-chart-financial 外掛**——那個外掛要 time scale，會打壞既有籌碼峰/纏論/回測進出場這些以 `labels` 日期字串對齊的疊圖。台股慣例紅漲綠跌（跟站內 `--pos` 綠色＝正值的慣例相反，刻意的）。`_wick` 用 legend/tooltip `filter` 藏起來，K線的 tooltip 顯示開高低收。
- **成交量窗格**（2026-09-30）：同一張 canvas 用兩個 `stack:'price'` 的垂直堆疊 y 軸（`yVol`:`y` = 1:4），量柱單位換成張（`volume/1000`）、顏色跟 K 棒同色半透明。`yVol` 必須定義在 `y` 之前才會排在下方窗格——但這樣沒指定軸的 dataset 會預設綁到第一個 y 軸（`yVol`），所以建圖前統一補 `yAxisID ??= 'y'`，之後新增疊圖不用自己記得指定。圖高由 `.price-chart-wrap`（560px／手機 420px）控制。
- **投信成本線**（2026-10-01）：`_trustCostSeries()` 純前端計算，資料來自既有 `/api/stocks/<code>/institutional-trades`（`state.instTrades`，跟股價同步抓、同樣多抓暖機）。每天往回重播近 `_TRUST_COST_WINDOW`（120）個交易日的投信**淨**買賣超，從零持股開始用**移動平均成本法**：淨買超以 (高+低+收)/3 加權進成本、淨賣超只扣持股不動成本、持股歸零即重設，持股不足 1 張回 null（斷線）。**刻意不用「Σ(張數×價)/Σ張數」把賣超也乘賣價扣掉**——那是網路上常見的錯誤公式，投信獲利賣出會把「成本」拉低（例：80 買 100 張、120 賣 50 張會算出 40）。用 `--text` 色虛線，ⓘ 說明在 `index.html` 的 help-popover。另有：①`_renderTrustCostTile()` 在籌碼峰數值列（`#chip-peak-stats`）末端追加「現價距投信成本」方塊（`renderChipPeak()` 先重建該列、`renderPriceChart()` 再追加，所以順序不能反）；②收盤價相對成本線的突破／跌破點（前一日在線下/上、當日收在線上/下），用紅／綠圓點標在成本線上，圓點是其他疊圖沒用過的形狀。
- **功能群組切換列**（2026-10-01）：Chart.js 內建圖例關掉（`legend.display=false`），改由 `_renderPriceChartGroups()` 在圖表上方動態插入 `#price-chart-groups`（JS 建立，不在 `index.html`），每個功能一顆膠囊按鈕（K線／成交量／均線／投信成本／籌碼峰／纏論／回測進出場），一次顯示/隱藏該功能的所有 dataset；隱藏狀態存 `localStorage`（`price_chart_hidden_groups`），切換股票／天數重繪後保留。**dataset 依 label 對應群組（`_priceChartGroupOf()`）——新增疊圖要記得加進去，否則會落到「其他」群組**。
- **暖機資料**：`_fetchPriceSeries()`／`_fetchInstTradeSeries()` 會多抓 `_CHART_WARMUP_DAYS`（200 曆日，涵蓋 120 交易日）讓 60 日線與投信成本線在圖表最左邊就有值；`state.prices` 存的是含暖機的完整序列，`_visiblePrices()` 依 `state.priceDays` 裁回使用者選的區間，圖表 x 軸與下方股價明細表都只吃裁過的資料。「全部」（9999）不額外多抓。

### 個股詳情頁：上一檔 / 下一檔導覽

`.detail-nav-btns`（`.nav-prev-btn`/`.nav-next-btn`）在詳情頁沿用「使用者是從哪個列表點進來的」那份清單與目前排序/篩選，而不是固定用代號順序：呼叫進入前的表格用 `setDetailNavContext(codes, currentCode)` 記下 `state.detailNavList`/`state.detailNavIndex`；DataTables 表格用 `_dtOrderedCodes(dt, codeColIndex)`（`dt.rows({order:'applied', search:'applied'}).data()`）取出「目前排序+篩選後」的完整代號順序，不只是當前頁面那幾筆。`goToAdjacentStock(delta)` 直接呼叫 `loadStockDetail(list[newIdx])` 切換，按鈕在清單頭尾自動 disable。

**三組按鈕（頂部／中間／底部），2026-08-22 新增**：使用者反應「想看上一檔/下一檔每次都要拉回最上方」，在頂部（`.detail-nav-bar`）之外，股價圖卡片後（`.detail-nav-bar-mid`，置中）跟頁面最底（`.detail-nav-bar-bottom`，跟「← 返回列表」同一列）各加一組一模一樣的按鈕。三組共用 CSS class（`.detail-nav-btns`/`.nav-prev-btn`/`.nav-next-btn`）而不是各自的 `id`，`updateDetailNavButtons()` 因此改成 `querySelectorAll('.detail-nav-btns')` 逐一更新每一組的 hidden/disabled 狀態，不用三份幾乎一樣的程式碼；`goToAdjacentStock()`/`onclick` 邏輯完全不用改，三組按鈕呼叫的是同一個函式。

**重要 gotcha：每一個會呼叫 `loadStockDetail(code)` 的進入點都必須自己呼叫 `setDetailNavContext()`**，這個機制沒有集中在 `loadStockDetail()` 內部統一處理，而是散落在 6 個呼叫點各自負責（主表格、飆股清單、自選股、自結公告、達人選股列表、今日更新面板）。曾經有一個進入點（🆕 今日更新面板點股票晶片）漏掉這一步，導致從那裡點進詳情頁後，上一檔/下一檔會沿用不相關的舊清單。新增任何新的「點股票進詳情頁」進入點時，務必記得同時呼叫 `setDetailNavContext(codes, code)`，否則會重蹈這個 bug。

**返回列表時定位到原本的列**（2026-07-15）：`showListView()` 呼叫前先記下 `state.currentCode`，切回列表視圖後呼叫 `_scrollToStockRow(code)` 把畫面捲動到該股所在列並短暫加上 `.row-flash` 樣式提示（不是每次都固定回到頁面最上方）。DataTables 表格（主表格/飆股清單/自選股）分頁只會把「目前頁」的列渲染進 DOM，所以 `_scrollToStockRow()` 若直接用 `querySelector('[data-code]')` 找不到，會用 `_VIEW_DT_INFO[state.activeTab]` 找出對應的 DataTables 實例，**重新**用 `_dtOrderedCodes(dt, col)` 算出該股在目前排序/篩選結果裡的位置、換算頁碼、`dt.page(n).draw('page')` 翻頁後再捲動——**刻意不直接借用 `state.detailNavIndex`**，因為使用者可能是從「今日更新面板」等其他進入點點進詳情頁的，那個 index 對應的是那個進入點自己的清單，不一定等於目前 `activeTab` 這個表格的順序，借用會翻錯頁。自結公告／達人選股列表因為所有列一次全部渲染進 DOM（不分頁），第一次 `querySelector` 就會成功，不會走到 DataTables 分支。詳情頁底部（`.detail-nav-bar-bottom`）也有一個「← 返回列表」按鈕，跟頂部那個呼叫同一個 `showListView()`，避免看到頁面最下方還要滑回最上方才能離開。

## 總體分析（`#macro-view`，2026-09-19 新增，純前端提示詞產生器，不呼叫任何 AI API）

分頁本身不打任何後端分析 API，只是把使用者填的表單＋本站既有資料組成一份完整提示詞，複製到剪貼簿並開新分頁到 Gemini/ChatGPT/Perplexity，使用者自行貼上分析——跟既有 `copyStarForAI()`/`copyWlForAI()`/`copyAnnForAI()` 屬於同一種「本站不呼叫付費 AI API、只組提示詞」的模式，只是這次是完整的機構研究備忘錄模板（供需矩陣、多空情境、交易防守邏輯）而非簡短提示。

- **股票搜尋**沿用自選股搜尋框（`#wl-search`）的既有模式：`state.allData` 前端過濾＋下拉選單，Enter 選第一筆。選定股票後（`selectMacroStock()`）自動帶入目前股價（`state.allData` 裡的 `close`），並額外呼叫 `/api/stocks/<code>/financials` + `/api/stocks/<code>/revenue` + `/api/stocks/<code>/prices?days=182` + `/api/stocks/<code>/broker-trades?days=90`（後兩者 2026-09-23 新增）組成「附加資訊」欄位（開頭註明未還原股價／千元／單季EPS，成交量換算成張；提示詞並要求 AI 與官方來源不一致時以官方為準並列出差異；近 8 季財報、近 12 個月營收年增率、近半年日線 OHLC＋漲跌%＋量、近90交易日主力分點買超/賣超前十大＋前5大分點集中度），2026-10-01 再加 `/api/stocks/<code>/institutional-trades`：三大法人近5/20/60日買賣超合計＋連續買/賣超天數＋近半年逐日外資/投信/自營商買賣超（張），以及 `_trustCostSeries()` 算的投信推估成本與現價乖離（股價/法人多抓 `_CHART_WARMUP_DAYS` 暖機，只印近半年）；同日再補齊資料庫其餘可用資料：`/api/stocks/<code>/fundamentals`（近8季毛利率/營益率/ROE/ROA/流動/速動/負債比/週轉天數、近5年股利與配發率、PBR、填息機率）、新端點 `/api/stocks/<code>/macro-extra`（`financial_extra` 近8季現金流＋資產負債表原始值〔現金流已去累計為單季、capex 為負值＝流出，FCF＝OCF＋capex〕、股權分散近12週、董監持股近6個月、近5年除權息填息事件）、`/chip-peak`（POC/VAL/VAH/集中度）、`/chanlun`（最新訊號＋最近中樞）。整份補充資料約 1.5 萬字，使用者可自行編輯或補充 K 線重點。分點彙整重用 `renderBrokerTrades()`／`_brokerLots()` 同一套「依 broker_id 加總 net/activity、取前N大」邏輯，未曾查詢過分點資料的股票會回傳空陣列、直接略過這段。
- `buildMacroPrompt()`（`app.js`）把固定的高盛研究員角色提示詞模板＋使用者填的表單欄位（目前股價／持股成本／持有數量／現金股利／股票股利／預計投資週期／最大可承受虧損比例；持股成本（元／股，平均買進價）為數字輸入（成本與目前股價相差超過3倍時，提示詞會加「⚠️ 成本檢查」要求 AI 先提醒可能輸入錯誤並並列兩種情境——2026-10-04 因使用者把 92.54 打成 9.254 算出 +898.8% 報酬而加）、持有數量是「數字＋張/股下拉選單」，數字留空＝尚未持有；「現金股利（元，累計已領總額）」`#macro-cash-dividend`、「股票股利（元／股）」`#macro-stock-dividend`（2026-10-04 新增；股票股利照公告慣例填元數，提示詞以面額10元換算配股股數＝持股股數×元÷10，1.0元＝每張配100股，假設持有數量為配股前股數），任一有填時提示詞會多一行要求 AI 計算含權息報酬與平均成本，留空＝無；2026-10-01 起移除美股市場選單與提示詞中所有美股字樣，本站只針對台股（產業龍頭對照仍要求台股＋國際龍頭，國際同業比較是分析需要，不算支援美股））＋上述自動帶入的附加資訊組成完整提示詞，模板內含「產業龍頭股對照」（第六層競爭優勢之後，2026-09-26 新增：要求 AI 聯網搜尋台股＋美股／國際龍頭並比較營收、毛利率、估值、股價連動），結尾固定加一段「輸出檔案格式要求」，要求 AI 額外把報告整理成可下載的獨立 HTML 檔（供列印或另存 PDF）。
- 三個 AI 連結按鈕（`openMacroAi(target)`）呼叫 `navigator.clipboard.writeText()` 但不 `await` 它、緊接著同步呼叫 `window.open()`——沿用既有 `copyAnnRatingPrompt()` 的既有寫法，讓 `window.open()` 留在使用者點擊的呼叫堆疊內，不要包進 `.then()` 回呼裡。
