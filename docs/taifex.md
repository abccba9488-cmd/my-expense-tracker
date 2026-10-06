# 期權籌碼分析

> 從 `CLAUDE.md` 拆出（2026-10-06，內容未改寫）。文中「見○○章節」若不在本檔，用 `grep -n '^## ' CLAUDE.md docs/*.md` 找。

## 期權籌碼分析（`crawler_taifex.py`/`taifex_analysis.py`，2026-08-20 新增，**需登入可見**）

台指期貨/選擇權籌碼分析，仿照 `aistock.brain168.com/taifex` 這個第三方分析網站的內容重建（外資/自營商/十大交易人部位、PC Ratio、支撐壓力、大戶多空比、TW VIX、CNN Fear & Greed）——實驗性功能，公式/資料完整度都還在驗證階段，因此不對匿名訪客開放，但**不限管理員**：前端 nav 分頁與整個 `#taifex-view` 都掛 `login-only hidden`（`updateAuthUI()` 依 `!!state.user` 切換，跟只給管理員看的 `admin-only` 是獨立的一組 class），後端 `/api/taifex/*` 全部 10 個 GET 端點也各自擋 `_is_logged_in()` 回 403（不是只藏畫面，直接打 API 也進不去；2026-08-23 從 `_is_admin()` 放寬成 `_is_logged_in()`，任何已登入帳號皆可用）。

**資料來源決定用 FinMind、不爬期交所官網**：期交所有免金鑰的 OpenAPI，但只回傳「最新一天」、沒有日期參數，回補歷史還要處理另一套 HTML 表單+jQuery 日期選擇器的 POST 邏輯。FinMind 的對應 dataset（`TaiwanFuturesDaily`/`TaiwanOptionDaily`/`TaiwanFuturesInstitutionalInvestors`/`TaiwanOptionInstitutionalInvestors`/`TaiwanFuturesOpenInterestLargeTraders`/`TaiwanOptionOpenInterestLargeTraders`）能沿用專案既有的 `crawl_finmind_*` 寫法慣例，歷史也回得更早（三大法人/十大交易人 2018-06-05 起，期貨/選擇權行情最早到 1998/2001）。

**約當大台**（外資/自營商/投信 三大法人專屬，十大交易人沒有）：台指期貨(TX)之外，另抓小台(MTX)/微台(TMF)，存進獨立的 `taifex_futures_institutional_mini` 表（`crawl_finmind_taifex_futures_institutional_mini()`）。換算比例來自契約規格（TX 200元/點、MTX 50元/點、TMF 10元/點 → 1 TX = 4 MTX = 20 TMF），`taifex_analysis.compute_contract_equivalent()` 合併三者的未平倉淨額算出。**十大交易人沒有這個換算**——不是本站沒接，是 TAIFEX 官方的大額交易人未沖銷部位統計本來就只公布大台，FinMind `TaiwanFuturesOpenInterestLargeTraders` 對 `data_id=MTX/TMF` 已實測回傳 0 筆確認過。

**重要踩雷：「淨部位」要用未平倉餘額，不是今日成交淨口數**（2026-08-20 發現+修復）：一開始 `foreign_net` 等欄位都用 `long_deal_volume - short_deal_volume`（當天的交易流量），數字很小（個位數百）；比對原網站「外資期貨」欄位（-82,693 這種量級）才發現對不上，改用 `long_open_interest_balance_volume - short_open_interest_balance_volume`（未平倉部位淨額，即目前持有的部位總量）之後，數字幾乎跟原站精確吻合（實測 2026-08-20：本站外資期貨淨部位 -82,423 / 約當大台 -82,694，對照原站外資期貨 -82,693 / 外資約當大台 -82,665）。這兩組欄位（deal_volume=流量 vs open_interest_balance=存量）語意完全不同，`TaifexFuturesInstitutional`/`TaifexFuturesInstitutionalMini` 兩張表都同時存了，取用時務必確認拿對的那組——`api_taifex_summary()`/`api_taifex_futures_institutional()`/`api_taifex_daily_detail()` 三處都已修正為未平倉餘額版本。

**大戶多空比**（`taifex_analysis.compute_bull_bear_ratio()`）：近似公式，原站確切算法未公開——十大交易人期貨淨部位（買方-賣方未沖銷）／全市場未沖銷部位 × 100%。**Gauge 的座標軸刻意不是原站的 ±70%**：查過本站實際歷史（152個交易日，2026-01-02~08-20）範圍只有 -11.9%~+9.0%，用 ±70% 會把整個波動範圍壓縮成表尺的一小塊；改用 ±15%（留一點headroom，且是整數），同一個數字時間序列版的「收盤 & 大戶多空比」圖表也用同一組資料（不是另外用選擇權算的獨立指標——這點原本判斷錯過一次，`大戶「期權」多空比` 這個命名一度誤導成要另外拿選擇權十大交易人資料湊一個新公式，使用者發現形狀不對後改回沿用期貨版同一個 `bull_bear_ratio_pct`）。

**大戶多空比 gauge 5色分區+外圈刻度（2026-08-21，比照 TW VIX/Fear&Greed 同一套設計）**：原本是單純的正/負兩色填充環（`--pos`/`--neg`），改成跟 VIX 一樣的 5 個等角度色塊＋指針。色塊邊界 `_BULL_BEAR_ZONES`（`app.js`）：`-15/-6/-2/2/6/15`（反指標看多／偏多／中性／偏空／過熱），比例對齊參考站 `-70/-30/-10/10/30/70` 的相對寬度（7:3:1）等比例縮放到本站 ±15 量級，四捨五入成好記整數，顏色深綠→淺綠→黃→橘→紅。**這個 gauge 有兩份完全獨立的 DOM/canvas**：最上方縮小版三合一列（`#taifex-gauge-mini-chart`）跟下面「今日摘要」卡片裡的詳細版（`#taifex-gauge-chart`）共用同一份繪圖邏輯 `_renderBullBearGauge(pct, canvasId, valueId, labelId, stateKey)`，`renderTaifexGauge()`/`renderTaifexGaugeMini()` 只是帶入各自的 element id 和 `state` chart key 呼叫它——不是同一個 chart 重複渲染兩次，`renderTaifexSummary()` 每次都各自呼叫一次。使用者一開始要求把三個量表縮小並排在最上方，中途追加「原本的大戶多空比+今日摘要區塊要獨立保留」，才變成這個雙實例的結構。

**外圈刻度數字（`_gaugeTicksPlugin`，2026-08-21，三個 gauge 共用）**：使用者看到參考站三張截圖的弧線外圈都有邊界數字（大戶多空比 `-70/-30/-10/10/30/70`、VIX `0/10/15/20/30/50`、Fear&Greed `0/25/45/55/75/100`），要求本站也比照加上。`afterDatasetsDraw` 直接在 canvas 外圈（`outerRadius + 12`）畫文字，位置公式跟指針共用同一套角度換算（`_zoneFraction()`，見下方 VIX 段落）。**踩雷**：窄卡片（大戶多空比縮小版只有 280px 寬）的最左/右刻度文字（`-15`/`15`）一開始被卡片邊緣裁掉一半，因為 `layout.padding.left/right` 只留了 6px；改成 20px 才完整顯示，VIX/Fear&Greed 卡片較寬（360px）但也一併加大 padding 以策安全。

**效能踩雷：支撐壓力 API 一度讓整頁卡死**（2026-08-20）：`taifex_option_daily` 資料量大（180天約41萬列），`/api/taifex/support-resistance` 原本用 `?type=` 參數讓前端 4 種到期別（週三/週五/月/下月）各自呼叫一次，等於同一份資料被完整掃描 4 遍，SQLAlchemy ORM 物件化的開銷在這個量級下慢到讓 `Promise.all` 遲遲不 resolve、整頁沒有任何資料渲染。修法：改成一次查詢、用 Core 風格的欄位 tuple（不建構完整 ORM 物件）回傳全部 4 種到期別，前端也從 4 次 fetch 合併成 1 次。

**TW VIX 量表（2026-08-21 新增）**：來源 FinMind `TaiwanOptionVix`（臺指選擇權波動率指數），原始資料是盤中逐筆報價（09:00~13:45，每天約1100+筆逐15秒一筆），`crawl_finmind_taifex_option_vix()` 只取當天最後一筆（13:45收盤值）存進 `taifex_option_vix`，不留逐筆。**這個 dataset 只回溯到 2026-03-02**，實測更早的 `start_date` 一律回傳空陣列——推測是即時類資料集本身的保留窗口限制，`backfill_taifex_option_vix()` 的 `min_year` 夾到 2026。前端是仿照參考站的半圓量表：5個固定寬度色塊（低波動0-10/偏低10-15/正常15-20/偏高20-30/高波動30-50，每塊固定佔36°、跟色塊對應的數值寬度無關）+ 指針，指針角度由通用版 `_zoneFraction(v, zones, min)`（`app.js`，跟大戶多空比 gauge 共用同一套「等角度分區」計算，不管每區實際數值寬度多寬，N 個區永遠平分180°半圓）算出「落在第幾區塊+區塊內的相對位置」再換算成角度，用自訂 Chart.js plugin（`_gaugeNeedlePlugin`，`afterDatasetsDraw` 直接在 canvas 上畫指針線+圓點；`id: 'gaugeNeedle'`）疊在 `doughnut` 色塊圖上——Chart.js 本身不支援指針，這是社群常見的自製 gauge-needle 寫法。卡片右上角「了解更多→」開 `#taifex-vix-modal`，疊圖顯示 TW VIX 歷史線（面積填色，紅）+期貨收盤價線雙軸圖（藍，2026-08-21 從深紅改成藍色——兩條都是紅色系放大後太難分辨），區間可切 30/90/180/365天（`GET /api/taifex/vix-history?days=N`），下方顯示當前/區間平均/最高/最低。

**CNN Fear & Greed Index 量表（2026-08-21 新增，跟 TW VIX 同一批新增）**：唯一一個**非 FinMind、非台股**的資料源——`crawler_fear_greed.py` 打 CNN 自家網頁前端用的非官方 JSON 端點 `production.dataviz.cnn.io/index/fearandgreed/graphdata/<date>`（無公開文件，社群逆向工程確認可用）。**這支 API 沒有「查某一天」的概念**：路徑上的日期參數只決定回傳陣列的起點，一次呼叫就能拿到從那天到「現在」的全部逐日資料（實測 2020-08-03 至今約1500+筆一次到位），因此不像 FinMind 系列需要逐日迴圈或獨立 backfill 函式，`crawl_fear_greed_index()` 本身重複呼叫就是全量同步（idempotent，INSERT OR REPLACE）。**踩雷**：日期參數若早於 CNN 資料起點（2020-08-03）會直接觸發對方 500（已用真實請求驗證 2020-08-01 可以、2020-07-01 會500），寫死用 `2020-08-03` 當錨點日期。前端量表用線性 0-100 尺度（跟 TW VIX 的等角度分區不同，這是 CNN 自己的標準設計，色塊寬度依真實數值區間 0/25/45/55/75/100，肉眼可見中間黃色「中性」區塊比左右紅綠區塊窄），中心文字的標籤/顏色直接用 CNN 回傳的 `rating` 字串（'extreme fear'/'fear'/'neutral'/'greed'/'extreme greed'）對應，不是自己重算門檻。跟台指期貨收盤價一起畫圖時，兩邊日期是不同行事曆（CNN 用美股/UTC 日期，期貨用台灣日期）用日期字串直接對齊，會有約1天時區落差，比照參考站本身的簡化對齊方式。指針/色塊 gauge 沿用同一個 `_gaugeNeedlePlugin`（前身是只給 VIX 用的 `_vixNeedlePlugin`，改名成通用版本讓兩個量表共用）。

**前端**（`static/js/app.js` 的 `期權籌碼分析` 區塊）：
- Gauge：Chart.js `doughnut` 做半圓表（`circumference:180, rotation:-90`），中間文字用 HTML 疊層（Chart.js 原生不支援置中文字）。三個量表（大戶多空比／TW VIX／Fear&Greed）都是 5 色區塊＋指針＋外圈刻度數字，色塊/刻度細節見上面各自的段落
- **外資／自營商／十大交易人 三分頁架構**（`#taifex-entity-tabs`）比照原站，每個分頁下重繪同一組 canvas（期貨部位/選擇權買方口數/賣方口數/買方契約金額/賣方契約金額），不是每個身份各自一整組固定 DOM——十大交易人分頁沒有契約金額資料時顯示說明文字而非硬湊假圖
- **`_taifexLoadSeq` 序號防護競態**：`loadTaifexView()` 每次呼叫遞增序號，await 完成後比對序號是否還是最新——使用者快速切換掉這個分頁又切回來時，兩次重疊的載入不會互相覆蓋共用的模組變數（`_taifexFuturesInst`/`_taifexOptionInst` 等），只有最新一次呼叫允許渲染
- **圖表點擊放大**（`#taifex-chart-modal`，比照原站行為）：點任一張圖（`.taifex-chart-clickable` 底下的 canvas，gauge 除外）用事件委派抓 `#taifex-view` 上的 click，讀原圖表的 `chart.config.{type,data,options}` 在放大版 canvas 重建一個新 Chart 實例。**Modal 剛從 `hidden` 移除的當下，容器還沒排版完成**，Chart.js 若立即量測容器尺寸會抓到塌陷的舊值，圖表會縮得很小——用 `requestAnimationFrame` 延後一個 frame 再建立圖表解決。放大版寬度用 `.modal-box.taifex-chart-modal-box`（需要兩個 class 一起選才能贏過 `.modal-box` 本身較晚定義的 `width:360px`，單獨 `.taifex-chart-modal-box` 選擇器特異度打平、CSS cascade 順序會輸）
- 期貨收盤價的右側 Y 軸統一 `stepSize: 5000`，比照原站刻度（PC Ratio / 收盤&大戶多空比 / 身份別期貨部位 三張有價格疊圖的圖表都套用）

**已知限制**（比照專案一貫的誠實揭露慣例）：
- 「Call約當」「Put約當」（原站每日明細表格的欄位）需要原站未公開的專屬公式，本站沒有對應資料，不收錄
- 大戶多空比、支撐/壓力皆為本站自建的近似邏輯，不是官方指標
- 十大交易人系列 dataset 需要 FinMind Sponsor 等級（999元/月方案），本站既有訂閱已涵蓋

歷史回補：`python backfill_finmind.py --taifex --from-year 2018`（三大法人/十大交易人系列會自動把 `from_year` 夾到 2018，TW VIX 夾到 2026，因為 FinMind 更早沒有資料；期貨/選擇權每日行情可以填更早的年份；CNN Fear & Greed 不吃 `from_year`，一次呼叫就是全量同步，見上方說明）。
