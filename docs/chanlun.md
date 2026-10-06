# 纏論與力道K線

> 從 `CLAUDE.md` 拆出（2026-10-06，內容未改寫）。文中「見○○章節」若不在本檔，用 `grep -n '^## ' CLAUDE.md docs/*.md` 找。

## 纏論（`chanlun.py`，2026-08-22 新增）

跟籌碼峰同一種「純運算、不落地存表、`daily_prices` 即時算」的設計哲學（`GET /api/stocks/<code>/chanlun?lookback=250`），K線合併→分型→筆→中樞→MACD背馳→買賣點，全部近似版。

**開工前先做過可行性評估**（跟 project owner 討論過才動手，不是憑空硬做）：纏論原著對「線段」（特徵序列分型判斷）的定義本來就沒有統一共識，不同實作對邊界案例常有分歧；本站也沒有分鐘級資料，做不到「大級別定方向、小級別找買賣點」的真正多級別聯動。基於這兩點，範圍刻意縮小成：**跳過線段層級，買賣點直接用「筆」推導**；**只做日線單一級別**（如果之後要延伸，日/週/月三種聚合天期是可行方向，但比原本的日內多級別粗得多）。這些限制都寫進 `compute_chanlun()` 回傳的 `limitations` 陣列，前端 ⓘ 說明框也同步列出，不是實作完才想起來要揭露。

**演算法**（都在 `chanlun.py`，純 Python 無 pandas/numpy）：
- `_merge_k_lines()`：K線包含關係處理，依當下趨勢方向合併（漲勢：高低點都取高的；跌勢：都取低的），方向不分陰陽線
- `_find_fractals()`：合併後K線上的頂/底分型（中間那根的高低點都比左右兩根極端）
- `_build_strokes()`：分型交替連接成筆，相鄰分型間至少間隔4根合併K線（獨立性規則的簡化版），同類型分型連續出現時只留最極端的一個
- `_find_centers()`：連續3筆的第1筆與第3筆重疊區間（ZG/ZD），中樞延伸判斷**用後續筆的「終點」而非整筆範圍**——這是刻意的：真正的突破筆本來就是「起點在區間內、終點衝出區間外」，若要求整筆都在區間外才算離開，會把突破筆本身誤判成還在中樞裡整理（開發時第一版就是這樣寫錯，用真實股價資料測試才發現中樞邊界異常寬，修正後中樞數量從4個變成7個，且不再吞掉明顯的突破走勢）
- `_find_divergences_and_signals()`：背馳用 MACD 同向柱狀圖面積比較（`_stroke_macd_area()`，重用 `technical.py` 的 `macd_series()`）——中樞前一筆（進入）跟離開中樞創新高/新低的那一筆，同方向且力度變弱才判定背馳，標記第一類買賣點；緊接著找「第一筆完全留在區間外」的回抽筆標記第三類；**第一類點之後、跟第一類點同方向的下一次測試（`exit_idx+2`）沒有創破價位則標記第二類**。

**Bug 修復（2026-08-22，同日發現+修正）**：第二類點原本寫的是 `strokes[exit_idx+1]`——但那一筆是離開中樞後**第一次反彈**，方向剛好跟第一類點相反，它的端點幾乎必然不會超過第一類點的極值（本來就是從那個極值反彈回來的），導致「有沒有創破」這個判斷式形同虛設、永遠成立，等於每個第一類點後面無條件多蓋一個第二類點（不是真的在檢查任何東西）。使用者要求「檢查纏論有沒有bug」，用真實資料驗證（把每個二類點跟對應一類點的 stroke 索引差列出來）發現全部樣本都剛好差1筆，證實判斷式是假的，改成 `exit_idx+2`（跟第一類點**同方向**的再次測試）才是纏論定義裡真正的二次確認。修復後全市場60檔測試樣本裡二類訊號占比從「幾乎等於一類訊號數」降到約23%（148筆訊號中47筆），分布明顯更合理；修復後記得重跑 `experts.compute_expert_scores()` 讓 `expert_scores` 表的 `chanlun_buy`/`chanlun_sell` 快取更新，光改程式碼不會自動生效（下次排程17:00才會自然跑到）。

**Bug 修復2（同日，使用者回報「同一天出現買點又出現賣點」）**：第三類點判斷式 `if s_hi < zd or s_lo > zg` 沒有分突破方向，只要「隨便哪一側」脫離區間就觸發，不管這個中樞當初是往上突破（該檢查站穩在 ZG 之上）還是往下突破（該檢查守住在 ZD 之下）。用真實股票（2905三商）追出根本原因：某中樞往下跌破後產生 1b/2b（買點情境），照理要檢查「有沒有守住跌破、不回補到 ZD 之上」，但後續走勢反轉大漲、衝出 ZG 之上，被沒分方向的判斷式誤判成「站穩確認」蓋出一個 3b，剛好跟**另一個中樞**（真正往上突破、正確產生 3s 賣點）同一天。修法：`outside = (s_lo > zg) if is_up else (s_hi < zd)`，只認跟突破同一側的脫離。修復後全市場抽測300檔、204筆訊號，同一天買賣點衝突從有到 0。這個 bug 從第一次寫出來就存在，不是被前一個 bug 修復引入的新問題——兩次修復都是同一輪「檢查有沒有bug」的過程中，靠對照真實個股資料逐步定位出來的，純靠讀程式碼看不太出來，這種「方向性判斷少了一個 if 分支」的錯誤要跑真實資料才容易現形。

**驗證方式**：沒有現成的纏論標準答案可以核對，用真實股票資料（2330/2317/2454/3008，近800個交易日）交叉檢查——確認股價/日期數字合理、背馳訊號只在真正的趨勢反轉附近出現（純延續的強勢上漲期間不會誤發訊號）、且用不同股票測試避免只在單一個案上調參數調到「看起來對」。

**前端**（`renderPriceChart()`／`renderChanlun()`，跟籌碼峰共用同一張股價圖與同一顆 ⓘ 說明圖示，不是獨立卡片）：
- `#chanlun-stats` 數值方塊：最新訊號（一/二/三買/賣＋日期價格，正負色）＋ 筆／中樞數量
- 疊圖：橘色鋸齒線＝筆（`_chanlunPointSeries()` 把筆的轉折點對應到股價圖完整日期軸，`spanGaps:true` 讓 Chart.js 直接連成直線）；灰色色塊＝中樞（`_chanlunCenterSeries()`，跟 VAH/VAL 一樣的 `stepped+fill:'+1'` 手法，中樞之間留 null 讓填色自然斷開）；綠/紅三角＝買/賣點（`_chanlunSignalSeries()`，`showLine:false` 純散點，1/2/3類的區分只在 tooltip callback 裡查表顯示，圖上不擠文字）
- 纏論固定用自己的250天分析窗，跟籌碼峰一樣獨立於 days-selector；選較短天數時落在可視範圍外的筆/中樞/訊號自然對不到 labels、不會出現，不用額外裁切
- **`.chanlun-guide` 常駐（非 hover）使用說明，預設收合**（`#chanlun-guide-body.hidden` + `toggleChanlunGuide()`，2026-08-22 新增）：使用者先要求「詳細的使用說明放在功能上面」（一開始是常駐展開的靜態區塊），隨後追加「做成可收合」才改成預設收合＋展開/收合按鈕，比照 `#taifex-detail-wrap`/`toggleTaifexDetail()` 同一種 `.hidden` 切換模式。跟其他地方用 hover 的 `.help-popover` 刻意不同——這裡是「一段解釋纏論術語的長文字」，不是「滑鼠移過去看一下」的簡短提示，常駐（可收合）比 hover 更適合這種閱讀量。

**纏論買點／纏論賣點併入達人選股（2026-08-22 新增，第10/11套規則，實驗性）**：使用者要求「在選股達人裡面直接選出有信號的」，比照 `momentum_guard` 的模式加進 `experts.py` 的 `SCORERS`/`EXPERT_LABELS`/`EXPERIMENTAL_EXPERTS`——因為 `/api/experts`／`/api/experts/<key>`／前端 `renderExpertTabs()` 全部是照著這三個字典/集合泛型驅動，**新增這兩個 key 之後分頁自動出現、完全不用改前端程式碼**，這是這個架構刻意保留的擴充性，這次真的用上了。
- **`_score_chanlun(ctx, want_buy)`**：不是對每檔股票重新跑一次纏論演算法，是重用 `_build_context()` 裡技術指標already抓好的同一份 OHLC（見下方）。只看「最新一個訊號」是否近期（`_CHANLUN_RECENT_DAYS=30`曆日內，約20個交易日，取整數曆日是刻意的簡化，不追求精確對齊交易日曆）且方向對得上（`chanlun_buy` 要買點、`chanlun_sell` 要賣點）。`ScoreCard` 只有一個 `require()`（近期有沒有對的訊號）+ 一個 100 分的 `award()`（把訊號類型/價位/日期/幾天前塞進 label），是二元通過/沒通過，沒有漸進式評分（跟其他10套「累積多項指標」的計分邏輯性質不同）。
- **`_build_context()` 的 `_flush_tech()` 順便算纏論**：這個函式本來就逐股流式處理 760天 OHLC 算 `technical.snapshot()`（避免全市場同時塞進記憶體 OOM，見上方「達人選股」章節），纏論需要的資料形狀完全一樣，直接在同一個 flush 點多呼叫一次 `chanlun.compute_chanlun(rows)` 存進 `c['chanlun']`，**不需要再對 daily_prices 多打一次查詢**。實測 `compute_expert_scores()` 全市場（1990檔×11套規則）跑一次約83秒，多這兩套規則沒有造成需要優化的效能問題。
- entered_at 沿用既有的通用延續機制（狀態沒變就延續進榜日期），但**不加入 transition（空轉多/多轉空）追蹤**——那個機制目前寫死只認 `_GUTAI_OPPOSITE`（gutai_bull/gutai_bear 這一對），chanlun_buy/sell 雖然結構上也是一對相反訊號，但刻意不擴充這個機制，維持「只有 gutai 那對有 transition」的既有行為不變（範圍最小化，這次沒被要求要做這個）。
- **列表頁「評分」欄對纏論兩個分頁沒有意義（2026-08-22 發現+修正）**：因為 `_score_chanlun` 是二元通過/不通過、`award()` 固定給 100 分，通過的列全部都是「100/100」，沒有區分度，使用者看了截圖問「這是第幾買還是第幾賣」。修法：`renderExpertTable()` 新增 `_isChanlunKey(key)` 判斷，是纏論分頁時把「評分」欄標題換成「訊號類型」、內容換成 `_chanlunSignalLabel(s)`——從該筆 `breakdown` 裡找 `type==='score'` 那一項的 `label`（後端刻意固定成 `「一/二/三買(賣) @ 價格（日期，N天前）」` 這個格式，見 `_score_chanlun`），取空白字元前那一段就是「一買」/「二買」/「三買」/「一賣」/「二賣」/「三賣」，不用另外加後端欄位或 API 呼叫。**排序（2026-08-22 同日補上）**：一開始沒改排序邏輯（`data-sort="score"` 還是按數值 `score` 排序，對纏論分頁沒有實質效果，因為全部都是100），使用者實測後回報「沒辦法排序」才補上——`_EXPERT_SORT_GETTERS.score` 改成 `_isChanlunKey(_expertKey)` 為真時改用 `_chanlunSignalRank(s)`（把 `_chanlunSignalLabel()` 取到的中文類型透過 `_CHANLUN_TYPE_RANK` 對照表換算成 1/2/3，一買/一賣=1…三買/三賣=3），非纏論分頁完全不受影響繼續用原本的 `s.score`。沒有做同分時的次要排序鍵（例如同樣是「二買」的股票之間再依日期排），並列的維持原本相對順序（JS `Array.sort` 穩定排序），這是可接受的簡化。
- **訊號「時有時無」是已知特性、不是bug（2026-08-24 使用者回報+查明）**：使用者發現 4729熒茂 達人選股「入榜日期」顯示 8/24，但訊號本身標示的三買日期是 8/10，兩者對不上。追查方式：用同一份 `daily_prices` 資料，把日期依序裁到不同截止日重跑 `compute_chanlun()`，結果 8/19 算得出「三買@8/10」，8/20～8/23 這個訊號**消失**（`latest_signal` 變 `None`），8/24 又重新出現——因為 `compute_chanlun()` 每次都是拿當下全部歷史K棒從頭重算（沒有任何鎖定/增量狀態，見上方設計哲學），筆／中樞的判定會隨後續新進K棒改變，同一個歷史訊號因此可能隨時間「抖動」。`entered_at`（入榜日期）記錄的是 `compute_expert_scores()` 連續判定「通過」狀態的起始日（`passed` 從 False 翻成 True 那天，見 `compute_expert_scores()` 的 `is_new_streak` 邏輯）——8/20～8/23 那幾天 `passed` 曾經翻回 False，所以 8/24 訊號重新出現時 `entered_at` 也跟著重置，不會等於訊號自己標示的 8/10。這不是資料錯誤，是「近似版」纏論演算法固有的限制（沒有線段層級、沒有多級別確認、每天全量重算不鎖定），已在前端補上明確提示：`#chanlun-guide-body`（個股詳情頁纏論走勢圖說明框）「重要限制」新增一條、`#expert-chanlun-note`（達人選股頁，僅 `chanlun_buy`/`chanlun_sell` 分頁顯示，`renderExpertTable()` 用 `_isChanlunKey()` 切換 `.hidden`）。

**`chanlun_star`（纏論買點+營收飆股，2026-08-30 新增，第13套規則，實驗性且 admin-only）**：使用者拿 `backtest_chanlun_chippeak.py` 的 `chanlun_star` tier 回測驗證過（全市場5年、766筆交易、勝率37.3%、平均報酬0.99%，四種濾網組合裡表現最好，見「纏論買點×籌碼峰回測」章節），確認後要求做成獨立的選股規則，訊號出現時把股票抓進清單，並且明確要求**只有自己（管理員）看得到**。
- **`score_chanlun_star(ctx)`**：兩個 `require()` 用 AND 合併（`ScoreCard.result()` 是 `all(self.criteria)`，不用另外寫布林運算）——近30曆日內有纏論買點（跟 `_score_chanlun` 同一套新鮮度判斷）、且符合營收飆股條件。營收飆股 ratio 公式**刻意跟 `app.js` `calcEst()`/`_getStarBase()` 完全一致**（`est = 最新月營收/最新季營收 × 最新季EPS × 240`，`ratio = est/收盤`，要求 `ratio>=1.5` 且 `月營收年增>=20%`），不是另外發明一套算法——`ctx['q'][0]` 已經是依 `(year,quarter)` 降冪排序的最新一季（`_build_context()` 既有邏輯），`revenue`（最新一個月原始營收）原本沒有存進 ctx（月營收查詢那段只存了 `revenue_yoy`/`rev_yoy_recent`/`rev3m_avg`/`rev12m_avg` 這些衍生值），這次補上 `c['revenue'] = rows[0]['revenue']`。
- **`ADMIN_ONLY_EXPERTS = {'chanlun_star'}`（2026-08-30 新增的存取控制機制，第一次用到）**：跟 `EXPERIMENTAL_EXPERTS`（純粹加「NEW」徽章，前端一樣公開）是不同層級的東西——這是真正擋存取，`app.py` 的 `/api/experts`（列表，過濾掉非管理員不該看到的 key）跟 `/api/experts/<key>`（明細，非管理員直接打 API 也回403）都要擋，不是只藏前端分頁，比照 taifex/AI分析等既有的 admin-only 慣例（「不是只藏畫面，直接打 API 也進不去」）。前端完全不用改——`renderExpertTabs()` 本來就是讀 `/api/experts` 回傳的列表渲染分頁，非管理員的回應裡本來就不會有這個 key，分頁自然不會出現。
- **範圍刻意最小化**：沒有比照 `_isChanlunKey()`/`_chanlunSignalLabel()` 幫這個新規則也做「訊號類型」欄位換算（`chanlun_buy`/`chanlun_sell` 當初是因為使用者具體抱怨「評分沒意義」才加的），`chanlun_star` 通過的列一樣顯示 100/100，沒有另外解析 `breakdown` 顯示第幾買——這次沒有被要求要做這個，之後如果使用者反應同樣的問題再比照辦理即可。
- **歷史進出場訊號 UI（2026-08-30 同日追加，使用者要求「把過去的進出場信號都表示出來」）**：`backtest_chanlun_chippeak.py` 新增 `run_single_stock(db, code, years=5, tier='chanlun_star')`——單股、同步、on-demand 版本，比照 `backtest_sweet_spot.run_backtest(db, code, years)` 的既有模式（`app.py` 直接 import 整個模組呼叫），完全重用 `simulate_stock()`/`_build_disclosure_series()`，不是另外寫一套簡化邏輯，所以個股詳情頁看到的歷史訊號**跟批次回測腳本、跟即時的 `chanlun_star` 選股規則，三處用的是同一套訊號/出場定義**，不會互相矛盾。`simulate_stock()` 的交易紀錄 dict 順便補上 `entry_price`/`exit_price`（原本只有 `return_pct`，UI 要顯示實際價格）。新端點 `GET /api/stocks/<code>/backtest/chanlun-star`（admin-only，跟 `chanlun_star` 本身的 `ADMIN_ONLY_EXPERTS` 存取限制一致，不是只藏前端）。**兩個顯示位置共用同一組 HTML-builder**（`_chanlunStarSummaryHtml()`/`_chanlunStarTradesHtml()`，`app.js`）：①個股詳情頁新增一張 `admin-only` 卡片（比照 🧪甜蜜點訊號回測卡片的按鈕觸發模式）；②達人選股 `chanlun_star` 分頁每一列在評分欄位旁加一顆「📊歷史」按鈕，點擊開 `#chanlun-star-history-modal`（重用 `.modal-box.taifex-chart-modal-box` 的寬版 modal 樣式，不用另外寫 CSS），不用先跳去個股詳情頁才能看歷史——使用者原話「兩個都要」，兩處都做。

**股價圖疊加歷史進出場標記（2026-08-30 再追加，使用者原本以為表格沒有出場記錄，實際上是表格排序讓「持有中」那筆排最上面容易誤解，追查後使用者改口要求「在圖表中顯示出來」）**：`renderPriceChart()`（`app.js`）新增 `state.chanlunStarTrades` 疊圖區塊——藍色星形＝進場（`_chanlunStarEntrySeries()`）、橘色星形＝出場（`_chanlunStarExitSeries()`，**只畫真的觸發停損/停利的出場**，「持有中」的部位沒有真正出場事件、不畫在圖上，避免暗示一個沒發生過的事件）。**故意用星形而非三角形**：策略進場點本質上是纏論買點的子集（多了營收飆股條件），同一天常常會跟既有的綠色買點三角形疊在同一個位置，形狀不同才分得出「這是原始纏論訊號」還是「通過完整策略條件的進場點」。`opts.animation = false` 這次也要設（沿用同一個 sparse-scatter 動畫 bug 的既有教訓，見上方「纏論」章節的 gotcha 記錄）。**tooltip callback 重構成合併版**——原本纏論買賣點三角形的 tooltip callback 是直接整組覆寫 `opts.plugins.tooltip.callbacks`，這次兩個疊圖區塊都需要自訂 tooltip，改成兩個區塊各自只收集自己的 `Map`（`signalByDate`／`entryByDate`+`exitByDate`），迴圈結束後才統一組一個 callback（用 `ctx.dataset.label` 判斷要查哪個 Map），避免後加的區塊蓋掉先加的。**觸發時機**：`runStockChanlunStarBacktest()`（卡片版按鈕，不是 modal 版）成功拿到資料後寫入 `state.chanlunStarTrades` 並呼叫 `renderPriceChart(state.prices)` 重繪；`state.prices` 是新增的欄位（原本 `prices` 只是 `loadStockDetail()`/天數切換按鈕內的區域變數，沒有存到 `state`，這次為了讓回測完成後能重繪同一份股價資料才補上）。切換股票時 `state.chanlunStarTrades` 要在 `loadStockDetail()` 最前面就重置成 `null`（在呼叫 `renderPriceChart(prices)` 之前），不然會把上一支股票的疊圖誤套到新股票的股價圖上。

**重要 gotcha：Chart.js 進場動畫會讓稀疏散點 dataset 的所有點位塌陷到 y 軸 baseline（2026-08-22 除錯半天才定位）**——買/賣點 dataset（`showLine:false`，大多數索引是 `null`，只有訊號那幾個日期有值）建立時，Chart.js 的預設進場動畫會讓這些點卡在動畫起始位置（baseline），完全不會過渡到實際數值對應的高度，不管資料/dataset 順序/canvas 是否重用怎麼調都一樣。**排查過程**：一開始誤判是 `chartOptions()` 的 `interaction.mode:'index'` 造成的（改成 `'nearest'` 沒用）、也懷疑過 Filler plugin／canvas 重用／dataset object 被 Chart.js 內部快取污染（用 `{...d}` 淺拷貝丟進全新 canvas 意外「修好」了，一度誤導方向）——最後用真實資料+真實 `chartOptions()` 逐一拔掉 `opts` 的欄位二分排查，鎖定就是 `animation` 本身：只要 `animation:false`，稀疏散點 dataset 就會正確定位；`animation` 預設開啟（或設成 `{}`）就會壞，跟 `interaction`/`fill`/dataset 順序都無關。修法：`renderPriceChart()` 偵測到有纏論資料時，直接在該次渲染的 `opts.animation = false`（這張圖表本來就是資料變動就整個重繪，進場動畫沒有實質意義，不影響其他圖表）。**未來任何要在這個專案的 Chart.js 圖表上疊加 `showLine:false` 的稀疏散點 dataset，都要記得順便關閉 animation**，不然會踩到同一個坑。

## 力道K線（`force_kline.py`/`backtest_force_kline.py`，2026-08-24 新增，**Phase A：只有計算+回測，尚未接前端，計畫是 admin-only**）

使用者分享一段 ChatGPT 對話設計了一套原創「台股力道K線指標」（坊間同名產品沒有公開統一公式），分完整版「1.0」（5因子加權+A/S/SS三級買點+紅綠K線視覺化）跟「實戰版」（ZScore標準化後加權，方便回測）兩種。比照這個專案一貫的開發哲學（`chip_peak.py`/`chanlun.py` 都是「先驗證訊號有沒有用，再決定要不要投入視覺化」）：**先只做實戰版算分模組＋回測腳本，驗證有效後才規劃 Phase B（買賣點分級/K線視覺化/前端整合/admin-only 存取限制）**。

**`force_kline.py` `compute_force_score(rows, inst_rows, zscore_window=60)`**：五因子（Momentum 30%／Volume 25%／Candle 20%／Trend 15%／MoneyFlow 10%）各自做滾動 ZScore 標準化後加權合成 `Force`，`PowerK = EMA(Force, 3)`。跟原始 ChatGPT 設計唯一的實質差異是 **MoneyFlow 因子升級成用真實三大法人買賣超**（`institutional_trades` 表，達人選股評分已在用同一份資料）取代原始的簡化版 `CLV×RVOL`——這是使用者明確要求的升級，缺資料的日子仍照原始設計退回 `CLV×RVOL`（比照 `chip_peak.py` `_quality_weight()` 的「缺值→中性」精神）。回傳 `dates`/`force_score`/`power_k` 三個跟輸入 `rows` 一一對齊（等長同索引，缺值處 `None`）的陣列，不是像 `chanlun.py` 那樣只回傳訊號摘要——這是刻意的，方便 `backtest_force_kline.py` 直接 zip 逐日比對，不用另外做索引映射。EMA 平滑**沒有**重用 `technical.ema_series`（開發時才發現：那個函式假設輸入從頭到尾連續無缺、用前N筆SMA當種子，遇到序列中段的 `None` 缺口會直接拋 `TypeError`——力道K線的五個因子任一個當天缺資料就會讓 `force[i]` 是 `None`，這種缺口是常態不是例外），改成自己寫一個 gap-aware 的遞迴 EMA（遇到 `None` 就重置狀態，下一個非 `None` 值重新當種子）。

**`backtest_force_kline.py`**：獨立唯讀 CLI 腳本，不寫任何DB表。基礎訊號定義＝`PowerK` 由 `<=0` 翻到 `>0`（比照 ChatGPT 對話裡「第一根翻紅」的精神，實戰版還沒有分級所以先測最基本的翻正訊號）。**故意不比照 `backtest_gutai.py` 的 point-in-time 揭露日期gating／weekly resample**：股泰依賴季報/月營收這種有法定揭露截止日的基本面資料，力道K線只用價格/成交量/三大法人日資料，三者都是「當天即為已知」沒有揭露時間差問題，可以省掉這兩層複雜度直接逐日回測。市場基準沿用 `backtest_gutai.py` 的定義（`_mean(all_fwd)`：同一天全市場 horizon 日後報酬率的平均），「獲勝」＝訊號報酬率 > 當天市場基準，不是單純為正就算贏。資料讀取比照 `experts.py` `_build_context()` 的 bulk-query 串流分組模式（`ORDER BY stock_code, date` 一次撈完，不逐股票查詢），避免 N+1。

六種訊號 tier（`find_signals(stock_data, horizon, bench_by_date, tier=...)`／`_is_signal()`；籌碼峰疊加系列共用 `apply_chip_peak_filter(stock_data, signals, level, comparison)`，`_DERIVED_TIERS` 這個 dict 定義每個衍生 tier 來源base tier+籌碼峰level('val'/'vah')+比較方向('below'/'above')三個參數）：
- `base`：PowerK翻正，無確認條件
- `s`：加 ChatGPT「1.0完整版」S級確認條件（ForceScore>+30、收盤>MA20、MA20上彎、RVOL>1.3）——`compute_force_score()` 為此回傳新增了 `ma20`/`rvol` 兩個中間值陣列（本來只在函式內部用，2026-08-24 補上）
- `base_val`／`s_val`：在 `base`／`s` 之上，**再要求訊號當天收盤價 <= 該股票當時的籌碼峰VAL**（便宜區買）——使用者在 base/s 兩輪都沒優勢後，想測試「力道翻正+價格在籌碼峰便宜區」的組合
- `base_vah`／`s_vah`：在 `base`／`s` 之上，**再要求訊號當天收盤價 >= 該股票當時的籌碼峰VAH**（突破貴的一端才買）——使用者接著想比較相反方向的組合，用 `apply_chip_peak_filter()` 的同一套機制傳 `level='vah', comparison='above'` 即可，不用另外寫一個函式

以上四個籌碼峰疊加 tier 都重用既有的 `chip_peak.compute_chip_peak()`，同樣的 point-in-time 原則，只傳 `rows[:i+1]`（訊號當天及之前的資料，不會看到未來的成交量分佈）。

**效能：`_load_stock_series()` 用 `multiprocessing.Pool` 平行算每支股票的 `compute_force_score()`（2026-08-24 使用者要求「跑回測時把電腦效能開到最大」新增）**——DB bulk query 仍在主行程一次做完（SQLite 不適合多行程同時打），平行的只有純CPU、不碰DB的算分本身，這是實際瓶頸所在（近2000檔股票單執行緒要跑5分半，44核平行後降到17秒）。`--workers` 預設吃滿 `os.cpu_count()` 全部核心，**刻意不跟 `backtest_gutai.py` 一樣預設「核心數-2」**——這支腳本是使用者明確要求全速跑的一次性研究腳本，不是常駐服務，不需要為其他工作留餘裕。平行化前後跑同一份資料，結果數字完全一致（已驗證），純粹是加速、不影響正確性。

**Phase A 回測結果（2026-08-24，全市場1963檔、近5年、10日horizon）**：

| 訊號 | 訊號數 | avg_fwd_return | median_fwd_return | win_rate_vs_benchmark | positive_return_rate | avg_max_drawdown |
|---|---|---|---|---|---|---|
| `base`（PowerK翻正，無確認條件） | 181,175 | 0.49% | -0.33% | 41.0% | 45.7% | -3.76% |
| `s`（+S級確認條件） | 29,105 | 0.95% | -0.49% | 41.6% | 45.3% | -4.34% |
| `base_val`（+收盤<=籌碼峰VAL，便宜區買） | 40,747 | 0.52% | 0.00% | 40.1% | 47.8% | -3.26% |
| `s_val`（S級+收盤<=籌碼峰VAL） | 1,257 | 0.08% | -0.67% | 36.6% | 42.6% | -3.77% |
| `base_vah`（+收盤>=籌碼峰VAH，突破貴的一端買） | 25,473 | 0.84% | -0.24% | 43.8% | 46.7% | -4.17% |
| `s_vah`（S級+收盤>=籌碼峰VAH） | 11,996 | **1.13%** | -0.29% | **43.9%** | 47.2% | -4.56% |

**結論：六種訊號定義，沒有一種展現出站得住腳的統計優勢（勝率全部低於50%），但方向上籌碼峰VAH突破組合明顯優於VAL便宜區組合**。`base_val`/`s_val`（便宜區買）對市場基準的勝率是六組裡最低的兩組（40.1%/36.6%，`s_val` 甚至是所有指標最差的一組）；`base_vah`/`s_vah`（突破買）則是六組裡表現最好的（勝率43.8%/43.9%、`s_vah` 平均報酬1.13%也是六組最高），推測原因：這套指標的核心邏輯是動能/趨勢追蹤（Momentum/Trend/Volume三個因子權重合計70%），「便宜區買」（價格在成交量密集區之下）在邏輯上更接近「逆勢承接」，跟動能指標的方向性假設互相矛盾；「突破貴的一端買」（價格站上成交量密集區之上）才是跟動能邏輯一致的「順勢突破」，即使還沒展現出統計顯著的優勢，方向至少是對的。即使是表現最好的 `s_vah`，43.9% 的勝率仍然低於50%，還不到能實際使用的門檻。截至目前**尚未進入 Phase B**（買賣點分級/K線視覺化/前端整合）。這個結論性段落之後如有更新，應該回來更新這裡，不要留著過時的狀態當作長期結論。
