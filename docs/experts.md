# 達人選股（experts.py）

> 從 `CLAUDE.md` 拆出（2026-10-06，內容未改寫）。文中「見○○章節」若不在本檔，用 `grep -n '^## ' CLAUDE.md docs/*.md` 找。

## 達人選股（`experts.py`）

編碼 4 位台股達人（股泰多方/空方、888/巔峰標準1–4、股魚、股海老牛）公開的選股/評分邏輯，加上 1 套來源未附具名作者的「好公司7大財務指標」checklist，對本站 DB + FinMind 補充資料即時計分，`expert_key`：`gutai_bull`/`gutai_bear`/`flag888_1`–`flag888_4`/`guyu`/`laoniu`/`haogongsi`（以上9套皆非實驗性），中文標籤見 `experts.py` 的 `EXPERT_LABELS`。另有3套本站自製的實驗性規則（`EXPERIMENTAL_EXPERTS`，前端加掛 NEW 徽章）：`momentum_guard`（動能防雷）、`chanlun_buy`/`chanlun_sell`（纏論買/賣點，見下方「纏論買點／纏論賣點併入達人選股」）。

**`haogongsi`（好公司7指標，2026-07-23 新增）**：來源是 project owner 提供的截圖表格（未附書名/作者），7 項指標——實質盈餘、實質ROE、資盈率、獲利含金量、配息率、利潤率（毛利率＋實質盈餘利益率）、董監持股比率。本站 schema 沒有「非經常性利益」「折舊攤銷」欄位：實質盈餘一律用稅後淨利近似（不扣非經常性利益），資盈率＝資本支出÷稅後淨利、獲利含金量＝營運現金流÷稅後淨利（分母都少加回折舊攤銷，`approx: true`）。原表格「近8年趨勢平穩或成長／穩定向上」是軟性描述、非量化數字門檻，改成 `award()` 用「近2年均 vs 前2年均」比較近似；只有表格明確寫出的數字門檻（實質ROE近5年均>10%、資盈率近2年均<=70%、獲利含金量近2年均>=70%、配息率近5年均>=40%、毛利率近2年均>=20%、實質盈餘利益率近2年均>=6%、董監持股>=10%）才做成 `require()`。配息率沿用 flag888_2/guyu 既有的「現金股利(單股)÷EPS」算法，跟「現金股利總額÷歸屬母公司淨利總額」數學等價。

**`momentum_guard`（動能防雷，2026-07-16 新增）是第一個實驗性規則，跟前 9 套公開達人選股法性質不同**：不是抄錄自某個公開達人的選股法，是本站自製的實驗性規則，記在 `EXPERIMENTAL_EXPERTS` 集合裡（2026-08-22 起這個集合又多了 `chanlun_buy`/`chanlun_sell`，見「纏論買點／纏論賣點併入達人選股」），`app.py` 的 `/api/experts`、`/api/experts/<key>`、`/api/stocks/<code>/expert-scores` 三個端點都會多回傳一個 `is_experimental` 布林欄位，前端 `renderExpertTabs()`/`loadStockExpertScores()` 據此在分頁按鈕加掛 `<span class="new-badge">NEW</span>`。設計目的是避開「統計上便宜、但基本面正在惡化」的價值陷阱（起因：2496 卓越在 2026-07 遇到的狀況——6月營收年增在連續5個月遞減後首度轉負，但PBR/PE看起來還是便宜）：選股門檻 `require()` 直接排除「近月營收年增率剛由正轉負」的股票，加分項則看毛利率/營業利益率/ROE/單季EPS 的年增率是否轉強（`_yoy_diff` 算出的變化量，不是絕對水準）。

### 資料來源

- **FinMind**（`finmind_client.py`，付費 999 方案 6,000 次/小時）：三大法人買賣超、股權分散表、資產負債表/現金流量表/財報毛利項目、股利政策、除權息填息、PER/PBR。**Bulk 模式（不帶 `data_id`）一次回傳全市場當天資料，但 `end_date` 對大多數 dataset 不是真正的 range 篩選**——只有精準等於 `start_date` 才有效，寬範圍查詢會直接塌縮成只回傳 `start_date` 當天的資料（已用真實 API 呼叫驗證過）。因此 `crawler.py` 所有 `crawl_finmind_*` 一律逐日呼叫（`_finmind_daily_rows()` 共用這個逐日迴圈），不依賴 range 查詢一次拿多天資料。
- **TWSE/TPEX OpenAPI**（`crawl_director_holdings()`，非 FinMind、免金鑰）：董監持股比例。兩個端點都只回傳「目前最新一期」全市場快照（月更新），**無法查歷史日期**，跟其他 FinMind 函式的 `date_str` 參數模式不同。
- **技術指標**（`technical.py`）：不靠 FinMind，純 Python 用 `daily_prices` 的 OHLC 自算 EMA(3/5/8/13)/MACD/RSI/KD（含週K、月K），只有股泰規則會用到。

### 已知的簡化/近似（都跟 project owner 討論過，不是漏洞）

- 股泰的 TU/TM/TD 價位與 週守/月守 支撐是其軟體專屬公式，未公開——用 `technical.py` 的 `ema13_support`/`week_low_4`/`month_low_20` 近似替代，`breakdown` 裡都標成 `approx: true`，不冒充原始公式。
- 888 標準2「董監持股」用 `financial_extra.capital_stock ÷ 面額10元` 反推已發行股數，面額非 10 元的少數股票會不準（`crawl_director_holdings()` 對算出 >100% 的異常值直接捨棄不存）。
- 「累計月營收年增率」（股泰）、「累積淨利年增率」（老牛）都用單期 YoY 代替真正的累計值——本站 schema 沒有累計營收/淨利欄位。
- 每一項「近N年平均」比率（ROE/ROA/毛利率/流動比率/周轉天數）都用「近 N×4 季」的算術平均近似，不是嚴格的曆年桶。
- `haogongsi`「實質盈餘」用稅後淨利近似（不扣非經常性利益）；「資盈率」「獲利含金量」分母都少加回折舊攤銷——本站 schema 沒有這兩欄。

### 計分引擎（`experts.py`）

- `ScoreCard`：`require(label, cond)` 是選股門檻（`passed` = 全部 `require` 皆真；輸入為 `None` 一律視為不通過，不放行無法驗證的股票）；`award(label, cond, points)` 是配分項，`cond=None` 時整項跳過不計入 `max_score`（資料不足不扣分，也不算分）；`award_count(label, achieved, max_occurrences, unit_points)` 是「每命中一次 +N 分，封頂 M 次」的漸進計分（如「近5日外資買超次數」）。
- `_build_context(db)`：一次 bulk 查完全部表，組成 `{stock_code: ctx}`。**技術指標快照是逐股流式計算**（`daily_prices` 查詢本身已 `ORDER BY stock_code, date`，累積到換股票就 flush 該股的 `technical.snapshot()` 後捨棄），而不是先把全市場~2 年 OHLC 全部塞進記憶體再統一算——後者在 Zeabur 容器上會直接 OOM（~1,982 檔 × ~500 筆同時在記憶體是實測會爆的規模）。**`per`/`pbr`/`dividend_yield` 刻意不跟 `close`/`volume` 綁同一個「最新一天」查詢**，而是另外抓「最近一筆這三欄實際有值」的資料列：`crawl_finmind_valuation` 排在股價爬蟲之後跑，且容許落後 1–3 天回補（見下方爬蟲章節），若跟 `close` 一樣強制要求同一天，只要當天估值資料還沒進來，888標準1（淨值比）/888標準3（殖利率）會瞬間全數判定不通過（曾經在這個確切原因下發生過 0/1982、1/1982 的假性全滅，已修復）。
- `compute_expert_scores()`：對每檔股票跑 `SCORERS` 裡的全部規則（字典驅動，目前 17 套），寫入 `expert_scores`（`INSERT OR REPLACE`，跟 `stock_ai_analysis` 同樣的「只存最新一筆快照，不留歷史」模式，**唯二例外是 `entered_at`/`transition`**）。單一規則對單一股票算分丟例外時只記 log 跳過，不影響其他規則/股票。
- **`entered_at`/`transition`（僅股泰多方/空方訊號有意義）**：每次執行都先讀出覆寫前的舊列（`old_rows`），`passed` 狀態沒變就延續舊的 `entered_at`（入榜日期）；狀態改變（或該列第一次寫入/剛加欄位的 bootstrap）才把 `entered_at` 更新成當天。`transition` 只在 `gutai_bull`/`gutai_bear` 這對互斥規則、且是「真正的狀態翻轉」時才計算：進榜當下若「舊快照」發現該股正好在對面那個訊號上榜，記錄 `空轉多`/`多轉空`；bootstrap（欄位剛加入，`old.entered_at is None`）或非翻轉的正常首次進榜一律是 `None`，不會亂猜。

### API / 排程

`GET /api/experts`、`GET /api/experts/<key>`、`GET /api/stocks/<code>/expert-scores`（見上方 REST API 清單）。排程：`_finmind_job()` 每天 17:00 依序跑 5 個 FinMind 增量函式 + `crawl_director_holdings()` + `compute_expert_scores()`；`_finmind_financials_job(quarter)` 跟官方季報同月份、每天 23:30 補 `financial_extra`（`financial_extra` MOPS IFRS 資料可靠起點同官方季報一樣是 2013 年）。手動觸發任務見上方「排程」章節。

### 前端

- **`#expert-view`**（列表）：`#expert-tabs` 規則切換鈕（依 `/api/experts` 回傳動態產生）（`loadExperts()`/`renderExpertTabs()`），下方純表格 13 欄：排名/代號/名稱/產業/營收月份/起始股價/收盤價/價差%/漲跌幅%/評分/預估倍數/資料日期/甜蜜點。**`gutai_bull`/`gutai_bear` 這兩個分頁額外多兩欄**（入榜日期/轉換，來自 `expert_scores.entered_at`/`transition`）：`renderExpertTable()` 用 `_isGutaiKey(_expertKey)` 判斷，動態 toggle 這兩個 `<th>`（`#expert-th-entered`/`#expert-th-transition`，預設 `.hidden`）並在列資料多帶兩個 `<td>`，其他 6 套規則不顯示。「預估倍數」欄（2026-07-22 從「評分明細」按鈕改版）沿用 `#star-view`（營收飆股清單）既有的 `calcEst(s)` 公式即時算：`(revenue / qf_revenue) × eps × 240`，再除以 `close`，跟 `#star-view`/主表格用同一套邏輯與顯示格式（`x.xx` + `x` 字尾），純前端算、不需要後端額外欄位。原本點按鈕開 `#expert-modal` 彈窗看評分明細長條圖的機制已整個移除（`openExpertModal`/`closeExpertModal`/`renderModalExpertChart` 連同 HTML 一併刪除，不留死代碼）——同樣的評分明細長條圖在詳情頁的「達人選股評分」卡（`#stock-expert-card`，`renderStockExpertChart()`）仍看得到，資訊沒有真的消失，只是列表頁不再重複提供彈窗入口。
- **`#detail-view` 達人選股評分卡**（`#stock-expert-card`）：`loadStockExpertScores(code)` 抓 `/api/stocks/<code>/expert-scores`，`#stock-expert-tabs` 列出該股所有已算出分數的規則，`renderStockExpertDetail()` 一樣先畫「總分 X/Y 分」大字標頭，再選股標準清單，再呼叫 `renderStockExpertChart()`。**圖表繪製邏輯抽成共用的 `_renderExpertChart(scoreItems, canvasId, wrapId, chartKey)`**，`renderStockExpertChart`/`renderModalExpertChart` 只是帶入各自的 canvas/state key 呼叫它——確保列表 modal 跟詳情頁兩處的視覺化永遠同步；`state.stockExpertChart`/`state.modalExpertChart` 各自持有 Chart.js 實例，切換分頁/關閉彈窗時 `.destroy()` 再建新的，避免 canvas 重用衝突。
- **重要 gotcha：`_stockExpertKey`（目前選中的達人分頁）刻意跨股票延續，不是每次都重置**——`loadStockExpertScores()` 只有在 `_stockExpertKey` 對新股票不存在（`!scored.some(s => s.expert_key === _stockExpertKey)`，理論上不會發生，因為每檔股票都算好 `SCORERS` 全部規則）時才 fallback 到「第一個通過的規則」。曾經每次都重置成「這檔股票自己第一個通過的規則」，導致用上一檔/下一檔導覽瀏覽時，選中的達人分頁會隨機跳來跳去（每檔股票通過的規則不同）。另外，從 `#expert-view` 列表點股票進入詳情頁時，`renderExpertTable()` 的點擊事件必須在呼叫 `loadStockDetail()` 之前手動把 `_stockExpertKey` 設成該列表目前的 `_expertKey`，否則會沿用使用者上次在別處瀏覽時殘留的分頁，而不是使用者點擊當下所在的那個達人榜單。
- **總分一定要清楚顯示**：詳情頁的評分卡（`#stock-expert-card`，唯一還會畫評分明細長條圖的地方，見上方「列表彈窗已移除」說明），`.stock-expert-total`（大字、`--primary` 顏色數字）都放在選股標準清單「之前」，不是只靠分頁按鈕上的小字 `(X/Y)` 讓使用者自己找。
- **`#expert-table` 表格排序**（2026-07-13 新增，2026-07-22 補上「預估倍數」欄可排序）：跟自結公告表格（`#ann-table`）同一套純前端排序機制（`class="ann-sortable" data-sort="<field>"` + `.ann-sort-arrow`，兩個表格都是 `class="ann-table"` 所以共用同一份 CSS），但獨立實作一份 `_EXPERT_SORT_GETTERS`/`sortExpertTable()`/`_applyExpertSort()`，**沒有**跟 `sortAnnTable()` 共用程式碼——刻意保持兩份獨立，因為欄位取值邏輯不同：達人選股表格排序用到的價格類欄位（起始股價/收盤價/價差%/漲跌幅%/預估倍數/資料日期/甜蜜點）並不在 `_expertData` 本身上，而是要透過 `_expertP(code)`（`state.allData.find(...)`）另外查表算，`_ANN_SORT_GETTERS` 沒有這個需求。**排序偏好跨切換達人分頁（`_expertKey`）延續**：`loadExpertDetail()` fetch 到新規則的資料後，若 `_expertSortField` 已設定就呼叫 `_applyExpertSort()` 套用同一個排序，不會因為換分頁就悄悄變回 API 預設順序、卻讓表頭箭頭誤導使用者以為還在排序中。「排名」（純序號）一欄不可排序，理由同自結公告表格的主旨/AI分析/自選股欄。

### 持股健康檢查（`compute_holding_health`，本站自製、實驗性，2026-07-16 新增）

跟上面「找買點」的達人選股規則（見「達人選股」章節）用途不同——這個是給**已經持有**的自選股看要不要注意出場的三階段預警：`正常`／`早期警告`／`注意`／`撤退`。**不是批次跑全市場**，而是 `GET /api/stocks/<code>/health` 單股即時查詢時才計算（技術指標只抓該股近 400 天 OHLC 算 `technical.snapshot()`，比 `_build_context()` 的全市場批次快很多，適合自選股清單這種小數量、即時查詢的場景）。

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

### 回測（backtest_gutai.py，僅本機、獨立於正式排程）

`backtest_gutai.py` 回測「股泰多方/空方訊號」歷史上是否真的有效——**刻意獨立於 `experts.py` 的 `_build_context()`**（正式排程每天呼叫的那個），只讀取 DB、從不寫入 `expert_scores`，避免任何回測邏輯有機會影響正式評分。

**方法**：每週取一個歷史樣本日，用「只包含當時已知資料」重建 context（月營收用「次月10日後才算已知」、季報用專案既有的公告期限規則 5/15、8/14、11/14、隔年3/31 判斷，避免用到未來資料作弊），直接呼叫 `experts.py` 原封不動的 `score_gutai_bull`/`score_gutai_bear`。當天分數 `passed=True` 且 `score>=--min-score` 才算「發出訊號」，記錄該股之後 `--horizon` 個交易日的報酬，跟當天全市場平均報酬（基準）比較，並依分數級距（60-79/80-89/90+）分組統計。

**已知限制**：`stocks` 表只有目前追蹤中的股票，沒有回測當時的完整名單，下市股票的失敗案例看不到（倖存者偏差）；沒有計入交易成本/滑價；股泰的 TU/TM/TD 真實公式未公開，回測沿用跟正式評分一樣的 `technical.py` 近似值。

**效能規範（往後任何全市場回測腳本都要遵守）：一律用 `multiprocessing` 平行運算，盡量榨乾多核心 CPU，不要寫成單執行緒**。技術指標（EMA/MACD/RSI/KD）對每檔股票只算一次、快取成陣列，用 `bisect` 依日期索引取值，不要每個取樣日都重新計算一次（那樣等於重跑一次完整的 `compute_expert_scores()`，慢上百倍）；平行化的軸是「每個取樣日彼此獨立」，用 `multiprocessing.Pool(initializer=...)` 把大型唯讀資料（技術指標陣列、法人、持股、季報）在每個 worker process 只序列化一次（用 initializer 塞進 worker 自己的全域變數），不要每個 task 都重新 pickle 一次。Windows 用 `spawn` 模式啟動子行程，進入點一定要包在 `if __name__ == '__main__':` 裡。**這條規範是給「全市場 × 多年」規模的回測用的**——下面「甜蜜點訊號回測」是單一股票規模，不受此規範限制，見該章節說明為何用單執行緒同步跑就夠了。

### 甜蜜點訊號回測（backtest_sweet_spot.py，2026-07-28 新增，個股詳情頁按需查詢）

跟 `backtest_gutai.py`（開發者本機跑的獨立腳本，全市場×多年×需要 multiprocessing）性質不同：這是**單一股票**規模的回測，直接掛在個股詳情頁（券商分點卡片跟股價圖之間）當一個「開始回測」按鈕觸發的功能，`GET /api/stocks/<code>/backtest/sweet-spot?years=5`（`app.py`）同步執行——單一股票近5年、4種天期×5種目標（2026-08-12 新增 +10%，原本只有+15/20/25/30%）共20輪模擬，約數秒內完成，不需要 `backtest_gutai.py` 那套技術指標預算/`multiprocessing` 機制，所以**不适用**上面「全市場回測效能規範」，故意寫成單執行緒同步 Flask request。

**沿革**（同一天 2026-07-28 內連續三次修改，先讀最後結果即可，這段純記錄演進過程）：
1. 最初版本（模組名 `backtest_flag888_4.py`）進場條件是「達人選股 `flag888_4`（888/巔峰 標準4 填息穩定）passed 訊號」+「甜蜜點紫色（ma240）」兩項同時成立
2. 依 project owner 要求「取消第1個條件」，拿掉 flag888_4 這項，只留下甜蜜點紫色；連帶把 `experts.py` `score_flag888_4()`/`_consecutive_dividend_years()` 當時為了配合這個回測新增的 `as_of` 參數也還原拿掉（新增後從未被其他地方用到，拿掉後兩個函式都恢復成原本的簽名，避免留著沒人呼叫的參數）
3. 依 project owner 要求「另外三個甜蜜點，也依照這個邏輯做回測」，把單一 ma240 條件推廣成 `sweetSpotCell()` 的全部 4 個顏色（🔴ma20／🟡ma60／🟢ma120／🟣ma240），模組改名 `backtest_sweet_spot.py`、路由改名 `/api/stocks/<code>/backtest/sweet-spot`（舊路由 `flag888-4-fill` 已停用，前端也同步改了）

**方法**：`TIER_INFO`（`backtest_sweet_spot.py`）定義 4 個天期各自的均線視窗與判定方式，`_is_signal()` 完全對齊 `app.js` `sweetSpotCell()` 的公式——
- 🔴ma20／🟡ma60／🟢ma120（對稱±3%，支撐/壓力測試）：`abs(close - ma) / ma <= 0.03`
- 🟣ma240（不對稱，長期價值區測試）：`(close - ma) / ma <= 0.03`，股價在均線之下（不論低多少）或高於但漲幅不超過3%才算

均線本身用「T 當下往前最近（最多 N 筆）收盤價」現算（`_ma_asof()`），不借用 `_SUMMARY_SQL` 那個「用今天」算出來的值，避免回測用到未來資料。四個天期各自獨立回測（互不影響），前端 `#stock-backtest-tier-tabs` 4 個分頁可切換查看。

**不加碼**：4 個天期 × 5 個獲利目標（+10%／+15%／+20%／+25%／+30%，2026-08-12 新增 +10%）等於 **20 輪各自獨立的模擬**，因為出場時間點不同、導致「目前有沒有持股」這件事在每一輪之間都不一樣。每一輪模擬用 `next_allowed_idx` 這個游標卡住：持有部位期間（尚未達到那一輪自己的目標價），即使某一週又出現訊號也直接跳過、完全不評估，等這一輪的部位真的出場（達標賣出）之後，才會開始偵測下一次進場訊號；如果一直抱到資料末端都沒達標，這一輪往後就再也不會有新的進場（`next_allowed_idx` 設成資料長度，永久卡住），對應「這筆單還沒賣，不可能再買第二張」的直覺。

**已知簡化**：使用未還原除權息的原始收盤價（跟站內其他股價功能一致，`daily_prices.close` 本來就沒有做股利/分割還原），除息後的價格「自然下跌」會反映在報酬率計算裡；沒有交易成本/滑價。

## 主力吸貨（`experts.score_accumulation`，2026-10-05 新增，公開、實驗性）

改編自使用者提供的 ChatGPT／Gemini「大戶偷偷吃貨」討論。達人選股第 16 套規則（`accumulation`，`EXPERIMENTAL_EXPERTS`，非 admin-only），沿用每日 17:00 `compute_expert_scores()`。

- **資料**：`_build_context()` 新增 `inst20`（三大法人近20交易日淨買超合計，窗內至少15天有資料才算）、`hold_weekly`（`pct_400up`/`pct_100down` 近5週，算4週變化）、`margin`（融資餘額近21筆）、`pv`（`_price_volume_snapshot()`：MA20/60、近20日上漲日/下跌日均量、近5日下跌日均量；價格查詢因此多抓 `volume`）。
- **融資融券**：新表 `margin_trades`（張），`crawler.crawl_finmind_margin()`（FinMind `TaiwanStockMarginPurchaseShortSale`，bulk 一天一次呼叫全市場），排在 `_finmind_job` 第二步；`backfill_finmind.py --margin`。2016-01 起已回補（約 410 萬筆，回補約 75 分鐘、未撞到 FinMind 額度）。
- **配分**：ChatGPT 原表加總其實是 95，「下跌縮量」由 5 調成 10 湊滿 100。
- **現行門檻＝N2 組（見本章最後一點）**；以下為最初選 H 組的過程。**門檻來自 `backtest_accumulation.py`**（前瞻報酬法：訊號日收盤進場，量測 20/60 交易日報酬 vs 全市場全日無條件平均，同股 20 日內重複訊號只算一次）。2016～2026 全市場 10 組比較：原始版（任何變動都給分）60 日超額只有 +0.32%；選定的 H 組（大戶/散戶變動 ≥1 個百分點才給分＋得分率≥85%＋距MA60<15%硬門檻＋外資投信20日皆買超）60 日超額 +1.26%、勝率 49.2%、約 4,900 次事件（每年約 460）；再加嚴（距MA60<10%、得分率90%）反而變差。上線當天全市場 12 檔入榜（原始版 244 檔）。**勝率仍低於 50%，超額報酬來自右尾，是弱訊號**——對使用者說明時別誇大。
- **2026-10-06 補充回測（`backtest_accumulation.py` 同時輸出兩種結算；`--stop/--trail` 調停損停利，非預設值時結果檔加後綴）**：
  - **停損＋移動停利出場（10%/15%/20%）下，H 組每筆平均 +0.93%/+1.53%/+2.00%，全部輸給對照組「流動股每20日定期進場」+1.30%/+2.31%/+3.92%**，停利越寬差距越大。固定持有的超額報酬則對流動股對照組也成立（對照組60日超額 −0.18%），所以優勢是籌碼訊號本身、但屬「慢慢發酵」型，被停損停利洗掉。網站說明因此定位為「中長期觀察名單，不適合短線」。
  - **帶量突破**（收盤創前20日新高＋量 ≥ 前20日均量1.5倍）：當天同時成立（K）或吸貨後20日內等突破進場（L）都沒改善；只看突破（M）無優勢。**反而「H 且近20日都沒帶量突破」（N2）是所有版本最好**：60日超額 +1.83%、勝率 51.4%、約1,080次事件（每年約100）。**同日使用者同意改用 N2**：`_price_volume_snapshot()` 新增 `recent_breakout`（近20個交易日內任一天符合帶量突破，定義與 `backtest_accumulation._is_breakout` 相同），`score_accumulation()` 多一個 `require('近20日未出現帶量突破')`；當天入榜由 12 檔降為 5 檔。

## 投信買點（`experts.score_trust_buy`，2026-10-06 新增，公開、實驗性）

達人選股第 17 套規則（`trust_buy`，`EXPERIMENTAL_EXPERTS`，非 admin-only），沿用每日 17:00 `compute_expert_scores()`。

- **定義**：投信成本線＝近60交易日投信「買超日」以買超股數加權的均價（均價用 (H+L+C)/3 近似）。入榜＝投信60日淨買超＞0、近5日淨額≥0、近20日收盤曾高於成本線≥10%、今天收盤在成本線上方0～3%、10日均量>500張。
- **資料**：`_build_context()` 載入近150天投信每日淨額 `trust_daily`，在 `_flush_tech()` 用同一份 OHLC 呼叫 `_trust_cost_snapshot()` → `ctx['trust_cost']`（不多打 daily_prices 查詢）。
- **回測 `backtest_trust.py`**（前瞻報酬法同 `backtest_accumulation.py`，`--from/--until` 可切期間）：使用者原本要「投信認養初期」（A 系列：前60日買超≤3天、近5日≥3天）＋「由下往上突破成本線」（B 系列），**兩者都沒贏過「流動股每20日定期進場」對照組**（B 每段都更差；A7 順勢版全期小贏、但 2021-2026 輸）；投信買超佔成交量比例門檻越高越差。唯一勝出的是**拉回成本線 E1**：全期60日平均 +5.18% vs 對照組 +3.43%（4,156 事件、勝率50.4%），但 **2016-2020 跟對照組持平（3.96% vs 4.00%），優勢只在 2021-2026（4.92% vs 4.04%）**；停損/移動停利出場不優於對照組。使用者選擇只上線 E1。上線當天（2026-10-06）入榜 3 檔。
- 跟回測核對過：`experts` 入榜名單與 `backtest_trust._pullback` 對最後一天的判定一致。
- **前端說明**：`#expert-view` 表格上方 `#expert-trust-note`（比照 `#expert-chanlun-note`，`renderExpertTable()` 只在 `_expertKey === 'trust_buy'` 時顯示）；另有說明頁 `<h4>🏦 投信買點</h4>` 段落。說明文字刻意寫「出榜不等於賣出訊號」——沒回測過以成本線當停損。
