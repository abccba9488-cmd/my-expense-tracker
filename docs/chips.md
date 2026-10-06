# 籌碼：券商分點、籌碼峰

> 從 `CLAUDE.md` 拆出（2026-10-06，內容未改寫）。文中「見○○章節」若不在本檔，用 `grep -n '^## ' CLAUDE.md docs/*.md` 找。

## 券商分點進出（2026-07-22 新增，2026-07-22 開放詳情頁按需查詢）

只記錄「有人自選過」的股票，不是全市場資料——來源 FinMind `TaiwanStockTradingDailyReport` 是逐股票呼叫，一檔一天就近千到數千列（2330 實測近 5800 列），全市場每天跑不現實也沒必要。**曾誤以為要用 `TaiwanStockTradingDailyReportSecIdAgg`（需 FinMind 贊助方案）**，實測發現這個 dataset 名稱已不存在（`422` 列出的合法 enum 沒有它），改用 `TaiwanStockTradingDailyReport`——這個現有 `FINMIND_TOKEN` 就能直接呼叫，不需要升級付費方案。

**原始資料是逐價位明細**：同一券商同一天在不同成交價各有一列，`crawler.py` 的 `crawl_broker_trades(date_str, stock_code)` 依 `securities_trader_id` 加總 `buy`/`sell` 股數才存進 `broker_trades`（`buy_price`/`sell_price` 是用金額加權平均出來的，不是原始值直接搬）。這個 dataset 不分上市/上櫃，同一支函式即可，不用像 `daily_prices` 那樣分 TWSE/TPEX。

**觸發機制（兩條路徑，共用 `crawler.backfill_broker_trades(code, days=90)`，2026-08-11 從 30 天延長為 90 天）**：
1. **首次自選回補 90 天**：`POST /api/watchlists/<id>/stocks` 新增成功後，若 `broker_trades` 裡該股票尚無任何資料（代表全站第一次有人自選），用既有 `_run_bg()` 背景執行緒觸發回補。
2. **每日增量 + 舊自選股補漏**：`scheduler.py` 的 `_broker_trades_job()`，週一〜五 17:30（`_finmind_job` 之後），對 `crawler.watchlisted_stock_codes(db)`（`SELECT DISTINCT stock_code FROM watchlist_stocks`）逐檔檢查：已有資料的只抓當天增量，**完全沒資料的直接補 90 天**——這條分支是 2026-07-22 補上的，原因是「這個功能上線前就已經在自選清單裡」的股票只靠路徑1（新增當下才觸發）永遠等不到資料，本機驗證時實測踩到（既有 29 檔自選股全部卡在 0 筆）。

移除自選不特別處理——`watchlisted_stock_codes()` 重新查詢就會自然排除，舊資料留著（資料量小，不做清理）。

**踩雷**：本機用工具（PowerShell）重啟 `python app.py` 時，子行程繼承的是那個 shell session 當下的環境變數，不是登錄檔即時值——`setx FINMIND_TOKEN` 寫進去之後，沒重新載入 `$env:FINMIND_TOKEN` 就直接 `Start-Process` 會導致爬蟲全部靜默失敗（只在 `crawler_logs` 留一筆 `finmind_token_check`/`failed`，畫面上完全看不出來）。

**券商名稱偶爾亂碼，是 FinMind 上游資料本身的問題，不是我們解碼錯**：實測對比同一 `(stock_id, date, securities_trader_id)` 直接呼叫即時 API 也拿到亂碼（`resp.encoding='utf-8'` 強制設定無效），確認是 FinMind 資料源間歇性把某些券商名稱的字元換成字面上的問號（如 broker_id `6010`/`6012`/`601d`「奔亞」系列，同一 broker_id 在不同日期時而正常時而變成「?亞」）。修法見 `database.py` 的 `_fix_garbled_broker_names()`（每次啟動都跑，用同一 `broker_id` 底下最常見的乾淨名稱覆蓋掉開頭是「?」的列）+ `crawler.py` 的 `crawl_broker_trades()` 寫入前防護（爬到可疑名稱且資料庫已有乾淨名稱時直接用乾淨的）。

**API**：`GET /api/stocks/<code>/broker-trades?days=90`（2026-08-11 從 30 天延長為 90 天）回傳近 N 天已聚合的原始列（股數，不分日期排序好，單位換算留給前端）。前端（`app.js` 的 `renderBrokerTrades()`）算出「整個時間窗累計買超/賣超前15大券商」（2026-08-11 從前10大擴增為前15大）決定矩陣的欄位，再攤開成**日期 × 券商矩陣表格**（每一列一天、每一欄一個券商、最後一列合計），比照 `/api/market/summary` 前端算飆股的既有慣例，數字統一 `_brokerLots()` 除以1000四捨五入成張數顯示（2026-07-22 從「選日期看當日前10」改版，使用者要求「不要一天一天查」）。手動測試：`POST /api/crawler/run/broker_trades`。

**詳情頁按需查詢（2026-07-22 新增，取代原本「僅自選股才顯示」的限制）**：`#stock-broker-card` 現在對任何股票的詳情頁都會顯示，卡片內一律有「查詢近90天券商分點」按鈕。原因是使用者反應這個功能不該綁死一定要先加自選——加自選只是「之後每天自動增量更新」的觸發器，跟「現在我想手動看一次」是兩件事，不該互相綁定。行為：
1. 進入詳情頁時 `loadStockBrokerTrades()` 只做 `GET .../broker-trades`（不觸發爬蟲）——有資料就照舊畫矩陣，沒資料就顯示空狀態文字＋按鈕，不會每次瀏覽個股都自動打 FinMind。
2. 按下按鈕呼叫 `fetchStockBrokerTrades()` → `POST /api/stocks/<code>/broker-trades/fetch`（`app.py` 的 `api_broker_trades_fetch`，需登入，未登入回 401、前端攔截顯示 toast）。這支**同步執行**（跟「AI 個股分析」的 `runStockAiAnalysis()` 走同一種模式：按鈕 disable＋文字改成「查詢中」、request 直接 block 到爬完才回應），已有資料只補當天（`crawler.crawl_broker_trades`），完全沒資料才回補90天（`crawler.backfill_broker_trades`，90 天 × 0.3s 節流，實測約數十秒到一分鐘），邏輯與 `scheduler._broker_trades_job()` 的「已有資料只補當天/沒資料回補90天」判斷一致。完成後前端重新 `GET` 一次並重繪矩陣。
3. 排程（`_broker_trades_job`，週一〜五 17:30）與「首次自選回補 90 天」（`POST /api/watchlists/<id>/stocks`）這兩條既有的自動觸發路徑**維持不變**，仍然只對自選清單裡的股票跑——按需查詢只是額外開放的第三條「使用者手動、任何股票、當下觸發」路徑，不影響前兩條的排程/自動化範圍（避免把每日排程也擴大成全市場爬取）。

## 籌碼峰（chip_peak.py，2026-08-13 新增，Phase 1+2）

純運算功能，不落地存表——`GET /api/stocks/<code>/chip-peak` 每次請求即時算，跟 `technical.py`（同樣純 Python、不用 pandas/numpy）走同一種設計哲學。目標是先驗證「時間衰減加權籌碼峰」這個訊號有沒有參考價值，刻意先做最簡化的版本，不追求機構級精確度。

**Phase 1（`compute_chip_peak()`）**：把 `daily_prices` 近 `lookback`（預設120）個交易日的收盤價依 `bin_pct`（預設1%，`bin_size = max(latest_close × bin_pct, 0.01)`）分箱，每天的當日全部成交量記在收盤價那個箱子（**已知簡化**：不像逐筆成交/OHLC日內分布那樣細分，且用未還原除權息的原始收盤價，跟站內其他股價功能一致），乘上指數半衰期權重 `e^(-ln2/half_life × days_ago)`（`days_ago` 以交易日索引距離計算，不是曆日）累加。**POC** = 加權量最大的價格箱；**VAH/VAL** = 從 POC 往兩側擴散、每次併入權重較大的鄰箱，直到涵蓋 70% 總權重為止（標準 Value Area 算法）。資料不足 30 筆（新股剛上市/剛恢復交易）回傳空物件 `{}`。

**Phase 2（法人品質加權）**：`app.py` 的 `api_chip_peak()` 額外查 `institutional_trades`，算出每日「三大法人淨買超 / 當日成交量」比例（`inst_net_ratio`），餵給 `compute_chip_peak()` 做 `_quality_weight()`：正值放大當天權重、負值縮小，`quality_cap`（預設0.3）避免單日極端值主導。**沒有法人資料的日子一律視為中性權重 1.0，不當成缺值懲罰**（`institutional_trades` 的資料起點晚於 `daily_prices`，若當缺值懲罰會系統性低估較久遠的日子）。回傳多帶 `quality_weighted`/`quality_coverage` 兩個欄位，前端可用來判斷這次計算有沒有實際套用到法人加權。

**Phase 3（分點集中度，目前只是資訊顯示，未併入 POC 計算）**：`app.js` 的 `renderBrokerConcentration()` 沿用既有 `broker_trades` 資料（見上方「券商分點進出」章節，只有查過的股票才有資料），算出「近期成交量最大的前5家券商分點合計占比」顯示在券商分點卡片裡。沒有做成 POC 加權的第三層，因為 `broker_trades` 是「買超+賣超」的日彙總數字，全市場淨額恆等於0（每筆交易一買一賣，分點視角天生互相抵銷），不像三大法人買賣超那樣有可以直接當方向性訊號用的量——HHI式的集中度只能講「交易有沒有集中在少數分點」，講不出「集中的是買方還是賣方」，所以刻意只當輔助資訊，不冒充成加權因子。

**Phase 4（POC/VAH/VAL 遷移軌跡，2026-08-18 新增，`compute_chip_peak_series()`）**：使用者發現圖上的 POC 只是一條不會動的水平線，追問後才意識到原本的設計就是「只算今天這一刻的一個數字」，沒有時間軸概念——對應報告裡提到的「Chip Peak Migration」完全沒做。`compute_chip_peak_series(rows, step=5, num_points=20)` 從最新一天往回，每隔 `step`（預設5）個交易日取一個樣本點，每個樣本點都**只用當時已知的資料**重新呼叫一次 `compute_chip_peak()`（`rows[:idx+1]`，嚴禁用到未來資料），存成 `{date, poc, vah, val}`，預設抓 20 個點。**一開始刻意只做 POC**（避免圖表過亂），使用者接著追問「VAH/VAL 是不是也該是一條直線」，確認後補上——現在三個價位都是時間序列。效能：20點 × 120日窗口 ≈ 2,400 次加權運算，純 Python 也是毫秒級，不需要快取。`app.py` 的 `api_chip_peak()` 在 `compute_chip_peak()` 有結果時才附帶算 `poc_history` 塞進同一個回應（不加新端點，欄位名稱沿用舊名 `poc_history` 沒改，即使現在裡面也帶 vah/val）。

**前端**（個股詳情頁「股價走勢」卡片）：
- 4 個數值方塊（`renderChipPeak()`）：POC／VAL–VAH／現價距POC（正負變色）／主峰集中度，各自用原生 `title` 屬性做 hover 說明（不用額外的 tooltip 元件庫，滑鼠移過去瀏覽器原生顯示，維持畫面簡潔）
- 股價圖（`renderPriceChart()`）疊加 POC/VAH/VAL 三條參考線，用 `state.chipPeak` 暫存，天數選擇器（30/90/120/180天...）切換不影響這三條線的計算窗口（籌碼峰固定用自己的 `lookback`，跟圖表顯示天數是兩件事）。**三條線都是會動的階梯線**：`_chipPeakFieldSeries(labels, poc_history, field)`（`field` 傳 `'poc'`/`'vah'`/`'val'`）把較稀疏的取樣點（每5個交易日一筆）對應到股價圖完整的日期軸上，兩個取樣點之間用「最近一次算出的值」往後補滿（`stepped: true`，不是內插——重算瞬間才跳一階），取樣範圍以前的日期留 null（`spanGaps: false`，不畫假線）。VAH 用 `fill: '+1'` 往下一個 dataset（VAL）填半透明色，變成一個會隨時間變寬/變窄的「價值區間帶」，而不是兩條容易看混的交叉線；VAH/VAL 顏色統一用 `--text2` 疊透明度、跟 POC 的實心橘線區分主副。若 API 沒帶 `poc_history`（理論上不會發生，防禦性寫法）三條線都退回舊版單一水平線畫法。
- 「股價走勢」標題旁的 ⓘ 圖示（`.help-icon`）放完整使用說明（定義＋實戰判讀＋限制）。**2026-08-20 起改用自訂 `.help-popover`**，不再是原生 `title` 屬性——原生 tooltip 字體大小無法用 CSS 控制（瀏覽器/作業系統自己畫的），使用者反應看不清楚後改成 CSS 自訂彈出框（14px、hover 顯示，`static/css/style.css` 的 `.help-popover`），並在 `app.js` 加了一個全域 click 監聽器讓觸控裝置也能點擊 ⓘ 開關（桌面版仍可 hover）。之後新增任何 ⓘ 說明都應該比照這個新寫法，不要再用 `title` 屬性

**重要 gotcha：Flask `debug=off` 時 Jinja 不會自動重新讀取模板檔**——改 `templates/index.html` 後若只是重新整理瀏覽器不會看到變化，必須重啟 `python app.py`；改 `static/js/app.js`/`static/css/style.css` 則不需要重啟，`inject_asset_version()` 的 mtime 版號機制（見上方「PWA」章節）會自動讓瀏覽器抓到新版本。這是本次開發過程中反覆踩到的點，特別記錄。

**天數選擇器新增 120天**（`templates/index.html` 的 `.days-selector`）：原本只有 30/90/180天/1年/3年/5年/全部，因為籌碼峰預設 `lookback` 是 120 日，使用者需要對應天數的股價圖選項才能肉眼比對，故插在 90天/180天之間。
