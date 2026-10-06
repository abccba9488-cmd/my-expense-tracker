# 自結公告、AI 個股分析、個股筆記

> 從 `CLAUDE.md` 拆出（2026-10-06，內容未改寫）。文中「見○○章節」若不在本檔，用 `grep -n '^## ' CLAUDE.md docs/*.md` 找。

## 自結公告（爬蟲 + 決定性解析 + AI 評級，2026-08-16 評級改為人工免費版）

`crawl_announcements(date_str=None, limit=None)` 於 `crawler.py`，預設爬取前一個交易日的 MOPS 重大訊息；`limit` 只在手動測試時用於只處理清單前 N 筆。所有**數字**欄位（單月EPS、去年同月EPS、年增率、預估全年EPS、預估本益比）都是決定性解析/計算出來的，**AI 從不自己生數字**——這部分邏輯對齊一個已驗證可用的參考實作（n8n 工作流程：先用關鍵字+正則決定性算好數字，再把這些「系統預算值」交給 AI 評級）。**評級/分析文字本身已改為人工流程**（見下方第7點與「AI評級欄」說明），不再由爬蟲自動呼叫付費 API。

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
7. **`ai_rating`/`ai_analysis` 一律留 NULL**（2026-08-16 前會在這裡呼叫 OpenRouter 付費 API 自動評級，`_analyze_with_ai()`／`_AI_SYSTEM_PROMPT` 已整個移除，改成免費的人工流程，見下方「AI評級欄」說明）——不再拖慢 `crawl_announcements()` 的執行時間，也不再產生 OpenRouter 費用
8. 用 `Announcement.__table__.insert().prefix_with('OR IGNORE')` 以 `seq_no`（合成鍵 `{date8}_{time6}_{code}`，MOPS 清單頁本身不提供全域唯一序號）去重——同一筆公告重複抓到時會被直接忽略，**不會產生重複列**，但既有列也不會因此被更新（`price_at_announce` 靠下方第9點的補值邏輯另外處理；`ai_rating` 走的是獨立的 `PUT /api/announcements/<id>/rating` 直接 UPDATE，不受此限制）
9. `_backfill_announcement_prices()`：每次 `crawl_announcements()` 跑完都會執行一次，掃描全表 `price_at_announce IS NULL` 的舊列重新查 `daily_prices`、補上股價與 `estimated_pe`。原因：`INSERT OR IGNORE` 對已存在的列完全不會更新，若公告當天的收盤價在第一次抓取時還沒寫入 `daily_prices`（常見於盤後立刻發布的公告），那一列的股價欄位會永久留空，除非有這段補值邏輯主動重算
10. `_log('announcements', 'success', ...)` 訊息格式為 `"{saved} saved / {backfilled} backfilled / {total} rows parsed"`

**重要踩雷：Format A/B 誤把「提到每股盈餘的一般敘述句」當成表格列，算出離譜的預估全年EPS（2026-08-23 發現+修復）**：使用者回報「預估全年EPS不太可能超過1000，也不太可能超過-100」，實測抓出 101 筆超出這個範圍的公告，最誇張一筆 `estimated_annual_eps` 高達 271,571,088。根因跟第4點描述的「唯一篩選依據是有沒有解析出 `monthly_eps`」是同一類問題的延伸——原本的 Format A/B 判斷「這行是不是EPS列」只看這行**有沒有出現 EPS 關鍵字 + 有沒有湊到足夠數量的數字**，完全沒管這些數字是不是真的來自同一張表格。結果好幾種完全不同性質的公告都會誤觸發：T-IFRSs/IFRSs 會計原則差異公告的附註句（"...業主淨利為新台幣36,958百萬元，基本每股盈餘為新台幣4.76元，民國112年12月31日..."——把獲利金額百萬元當成EPS、外加日期數字湊滿5個）、限制員工權利新股/認股權證的「每股盈餘稀釋情形」揭露句、股份買回公告、媒體澄清稿、敘述性新聞稿風格的自結公告（"...歸屬於母公司業主之淨利為新臺幣9.00億元，每股盈餘為新臺幣0.28元。"，把9.00億元的獲利當成EPS）。修法：新增 `_is_plausible_eps_row()`，核心判斷是「真正的表格列＝標籤＋純數字欄位，沒有其他文字」——檢查 EPS 關鍵字前後的文字，扣掉數字/括號內容（含全形（）；括號內可能是註腳「(註4)」或會計負數記法「(1.04)」，內容本身不重要）/已知的定性年增%替代詞（`_QUALITATIVE_EPS_YOY_RE`：由虧轉盈/由盈轉虧/由平轉盈/轉虧為盈/盈轉虧/虧轉盈/持續虧損/虧損減少/虧損增加，這幾個詞是用全語料庫掃描 EPS 列殘餘文字找出來的，比原本只認「虧轉盈/持續虧損」更完整）之後，**只要還剩下任何中文字就判定是敘述句，不是表格列**，整行跳過（Format A/B 兩個迴圈都從「找到關鍵字就處理/break」改成「找到關鍵字但不像表格列就 continue 找下一行」）。同時要求 EPS 關鍵字前面（`_ROW_PREFIX_OK_RE`）也只能是空白/數字/括號/中文數字編號，因為 MOPS 固定寬度換行有時會把敘述句的年份數字剛好排到關鍵字前面同一行（"第二年度(2026年度)、第三年度(2027年度)...對公司每股盈餘"，2026/2027 被誤判成EPS欄位）。**驗證方法**：寫一次性腳本用修好的 `_parse_disclosure()` 重新解析全庫 2526 筆公告的原始 `content`，逐筆比對新舊 `monthly_eps`——2235 筆不變、8 筆從明顯錯誤的數字修正成合理數字（都人工核對過原文，確認新值才是原文真正陳述的EPS）、283 筆從有 `monthly_eps`（其實是誤判）變成沒有（人工抽樣25筆核對，確認全部都是敘述句/揭露句類型的公告，本來就不該被當成自結公告）。修復後對正式 DB 執行同一份重算腳本（跑之前先 `cp data/stocks.db data/stocks.db.bak-<timestamp>` 備份），283 筆誤判的列直接刪除（不屬於這個功能的收錄範圍，見第4點的篩選原則）、8 筆更新成正確數字，`estimated_annual_eps` 超出 [-100,1000] 範圍的列數從 101 降到 0。

**除錯注意**：在 Zeabur 終端機貼含中文字的程式碼/heredoc 時，**終端機本身會在中文字之間插入空格**，不只是顯示問題，連貼上去的程式碼內容都會被改掉（例如 `re.compile('主旨')` 會變成 `re.compile('主 旨 ')` 導致比對失效）。之後要請使用者在終端機跑診斷用的 Python 腳本時，**程式碼裡絕對不要放新的中文字面值**，只能重用 `crawler.py` 裡已經部署好的常數/regex（如 `crawler._EPS_LABEL_RE`），或單純印出結構（不靠中文比對）讓人眼判讀。同樣道理適用於任何要在 Zeabur 上寫入中文資料的修正——`fix_stock_names.py`（一次性修正 13 檔被舊版 `crawl_stock_list()` big5 codec 弄壞的股票名稱）就是靠 `git pull` 後在雲端執行整個檔案，而不是把 UPDATE 語句貼進終端機，來避開這個問題。

**`GET /api/announcements/today` 原本寫死只回傳近7天，2026-08-17 移除**：`app.py` 的 `since = datetime.now(_TZ).date() - timedelta(days=7)` 把資料庫裡實際存在的歷史資料（`backfill_announcements.py` 一次性回填的 2023 年資料 + 每日排程持續累積，2026-08-17 當下共 2,513 筆、涵蓋 2023-01-06～）全部擋在畫面外，只是 API 篩選問題，資料庫本身完整無缺。使用者發現「只看得到當月資料」才抓到這個問題。移除篩選後改成回傳全部歷史，實測 2,513 筆一次回傳僅需 0.4 秒、後端本身無效能疑慮。

**前端加了簡易分頁（2026-08-17），不是效能問題、是「一次看2500列太多」的體驗問題**：`_annData` 仍然是抓回來的全部歷史資料（排序`sortAnnTable()`照樣對整包陣列操作），只是 `renderAnnTable()` 改成只把目前頁次的切片（`_ANN_PAGE_SIZE = 50` 筆）塞進 `#ann-tbody`，`#ann-pager` 顯示「‹上頁／頁碼／下頁›」（`_annPagerHtml()`，超出目前頁±2的頁碼用省略號縮起來，頭尾頁固定顯示）。**關鍵細節**：切片後每一列的 `data-idx` 仍然是該筆資料在完整 `_annData` 陣列裡的**全域索引**（`pageRows.map((a, localIdx) => renderAnnRow(a, start + localIdx))`），不是分頁內的區域索引——這樣複製提示詞/AI評級下拉選單/加入自選等既有按鈕邏輯（都是用 `_annData[i]` 取資料）完全不用改，換頁後每個按鈕還是準確對應到正確的那一筆。切換排序欄位或重新載入資料都會把 `_annPage` 重置回 1。

**前端（`#ann-view`）：** 純表格（不用 DataTables），15 欄：公告日期／**近90天公告**／代號／名稱／公告主旨／公告時股價／單月EPS／去年同月EPS／月EPS年增率／轉虧為盈／預估全年EPS／預估本益比／**AI評級**／AI分析／**自選股**。轉虧為盈欄位為真時顯示 🔥；預估本益比 `<= 0` 時前端顯示「—」（負本益比無意義，但後端仍照算存入 DB，不隱藏原始資料）。

- **公告日期欄**：顯示 `announce_date` + `announce_time`（取 `HH:MM`，捨去秒數），也就是 MOPS 網站上的「發言日期」+「發言時間」，不是爬蟲抓取/寫入的時間。API 排序為 `ORDER BY announce_date DESC, announce_time DESC`。
- **近90天公告欄（2026-08-21 新增，同日從60天調整為90天）**：同一檔股票可能因為大幅漲跌，在一兩個月內被公告不只一次，但這種重複往往被日期排序隔開的大量其他股票公告蓋過，肉眼很難發現。**沒有改變排序**（會犧牲「看今天新公告」的直覺，故意保留 date-first）——改成後端 `_compute_announcement_repeat_counts()`（`app.py`）逐股票分組、依日期排序後用雙指標滑動視窗，算出「以這筆公告日期為終點、往前推 `_ANNOUNCEMENT_REPEAT_WINDOW_DAYS`（90天，`app.py` 模組層級常數，改天數只需要改這一個值）的區間內，同一檔股票（含這一筆自己）總共公告了幾次」，回傳 `{'count': N, 'dates': [...]}`，JSON 裡拆成 `repeat_count`／`repeat_dates`（區間內全部公告日期，由舊到新，最後一個即自己這筆）兩個欄位。前端 `repeat_count === 1` 顯示「首次」，`>= 2` 顯示可點擊的「🔁 第N次」徽章（`_annRepeatCellHtml()`），欄位本身也可排序（`_ANN_SORT_GETTERS.repeat`）方便把重複公告最多的股票排到最前面看。**徽章點擊展開歷次日期（同日追加，比照 ℹ 說明框的 tap-to-toggle 模式）**：使用者原本要求「排列在一起讓我知道第1次第2次是什麼時候」——沒有改成依股票分組排序（會犧牲日期優先瀏覽），而是點徽章就地彈出 `.ann-repeat-popover`（CSS 定位在徽章下方），列出「第1次：日期／第2次：日期／…」，當前這筆用主色高亮。點擊事件是全域委派監聽（跟 `.help-icon` 那個一樣的模式），不是每列各自綁定。
- **公告主旨**：表格內只顯示前 10 字（`_annTruncate()`），點擊開 `#ann-modal`（同頁彈出視窗，不開新分頁/新頁面）顯示完整主旨與內容（`a.content`，無內容時顯示「（無詳細內容）」）。全部公告資料先一次性存進 `_annData`（模組層級陣列），modal/AI按鈕都用 `data-idx` 對應陣列索引去查，不用再打 API。
- **AI評級欄（2026-08-16 改為人工免費版，取代原本自動呼叫 OpenRouter 的付費流程）**：每列一個 📋 按鈕 + 一個下拉選單。點 📋 呼叫 `copyAnnRatingPrompt()`——組出跟原本 `_AI_SYSTEM_PROMPT`（已從 `crawler.py` 刪除）同一套評級標準的提示詞（已知數據直接取 `_annData` 裡已經算好的單月EPS/去年同月EPS/年增率/是否轉盈/預估全年EPS/預估本益比 + 公告全文），複製到剪貼簿並開新分頁到 ChatGPT（`chat.openai.com`），使用者自行貼上、看完回覆後回來在下拉選單（`.ann-rating-select`，選項：🔴強烈買進／🟠建議買進／🟡一般觀望／🟢需要小心／—未評級）手動選一個，`change` 事件呼叫 `setAnnRating()` → `PUT /api/announcements/<id>/rating`（`app.py`，僅限管理員 `_is_admin()`）直接 UPDATE 該筆 `ai_rating`。`ai_analysis` 欄位不再由這個流程寫入，維持 NULL（`#ann-modal` 裡原本顯示 `ai_analysis` 全文的區塊仍保留程式碼，只是現在通常不會有內容可顯示）。`_annRatingDot()` 這個依字串內容轉 emoji 的函式還留著，`#ann-modal` 開啟時仍用它顯示已選定的評級。
- **AI分析欄**（跟上面的 AI評級欄是兩個獨立功能，刻意並存）：`<a class="btn btn-sm ann-ai-link" href="https://gemini.google.com" target="_blank">`，點擊時 `copyAnnForAI()` 複製一段完整的估值分析提示詞到剪貼簿，同時連結本身會在新分頁開啟 Gemini（Gemini 網頁版不支援 URL 帶入提示詞，使用者需自行貼上），與既有 `copyStarForAI()`/`copyWlForAI()` 的「複製給AI」模式一致。提示詞包含：固定的分析師人設與分析步驟（同業本益比錨點、外資EPS預估、便宜/合理/昂貴價定價）+ 動態插入的股票代碼/名稱 + **目前股價**（從 `state.allData` 依 `stock_code` 查找，即本站資料庫的最新收盤價與資料日期，不是公告當時的價格，也不靠 AI 自己搜尋）+ 公告全文（無全文則用主旨）。要改提示詞文字本身，直接編輯 `copyAnnForAI()` 裡的模板字串。
- **自選股欄**：`addAnnToWatchlist()`。未登入或尚未建立任何自選股清單時顯示對應 Toast，不送出 API 請求。**若使用者只有 1 個清單，直接加入該清單**；**若有 2 個以上清單，彈出 `#wl-pick-modal`（重用 `templates/index.html` 既有的通用 `.modal-overlay`/`.modal-box` 樣式，跟 `#auth-modal`/`#about-modal` 同款，不是 `.ann-modal-*` 那套）讓使用者點選要加入哪一個**，清單項目旁標示目前支數或「已在清單中」。實際寫入邏輯抽成 `_wlAddStockTo(wl, code)`（接受任意指定的 `wl` 物件，不限定 `wlActive()`），原本的 `wlAddStock(code)`（只加到目前作用中清單，給 `#watchlist-view` 自己的搜尋框用）改為呼叫這個共用函式，行為不變。
- **今日更新面板**：`/api/updates/today` 多了 `ann_count`／`ann_last_checked`，今日有新公告就顯示「今日新增 N 筆」，否則顯示最後檢查時間。
- **表格排序**：純前端排序，不靠 DataTables（這個表格本身就不用 DataTables）。可排序欄位的 `<th>` 帶 `class="ann-sortable" data-sort="<field>"` + 一個 `<span class="ann-sort-arrow">` 佔位符；點擊呼叫 `sortAnnTable(field)`，直接對 `_annData` 原地排序（`_ANN_SORT_GETTERS` 定義各欄位的取值函式）後呼叫 `renderAnnTable()` 重繪整個 tbody。`renderAnnTable()` 是從 `loadAnnouncements()` 抽出來的共用渲染+事件綁定邏輯，排序後重新呼叫它才能讓 `data-idx`（對應 `_annData` 索引）保持與排序後的新順序一致，否則點擊主旨/評級/AI分析/自選股會對到錯的資料。空值一律排到最後（不論升降冪），不會因為遞減排序就跑到最前面。**公告主旨／AI分析／自選股三欄故意不可排序**（文字截斷後排序意義不大；後兩者是操作按鈕，不是資料）。
- **網站說明（About modal）**：新增「📰 自結公告爬蟲」段落，說明 30 分鐘排程與 AI分析按鈕用法（`templates/index.html` 的 `#about-modal`）。

## AI 個股分析（admin-only，按需觸發，**會產生 OpenRouter 費用**）

`crawler.analyze_stock_with_ai(code)`：跟自結公告的 AI 評級是不同的功能，**完全手動觸發、不自動、不批次**——每次呼叫都是真實付費的 OpenRouter API request，所以刻意設計成只有管理員點按鈕才會執行，沒有排程、沒有限流（使用者選擇不加限流，靠管理員自律控制費用）。

- **資料來源**：直接從本站 DB 查詢（`Stock`/`DailyPrice`/`MonthlyRevenue`/`QuarterlyFinancial`），不透過 HTTP 呼叫自己的 API。本益比公式**完全對齊** `_SUMMARY_SQL`（股價÷近四季EPS合計）；另外算出「營收預估股價」，公式**對齊** `static/js/app.js` 的 `calcEst()`（`(月營收/季營收)×EPS×240`）。這些數字都當作「已知，AI 不要重算」放進 prompt。
- **AI 只補資料庫沒有的東西**：同業本益比、外資EPS預估、近期新聞、法人籌碼——用 `_STOCK_AI_SYSTEM_PROMPT`，模型預設 `perplexity/sonar`（同自結公告，OpenRouter 上會自動觸發即時搜尋），輸出 `ai_rating`／`target_cheap`／`target_fair`／`target_expensive`／`ai_analysis`。
- **快取表 `stock_ai_analysis`**：`stock_code` 為主鍵，**每檔股票只存最新一次結果**（無歷史），`POST` 觸發新分析時直接覆寫。即使 AI 呼叫失敗也會覆寫一筆（`ai_rating` 等留 NULL），這樣才能正確反映「最近一次嘗試失敗」而不是顯示舊的過期結果。
- **同步執行**：`POST /api/stocks/<code>/ai-analysis` 不像 `crawl_*` 系列用 `_run_bg()` 背景執行，是直接 block 住等 OpenRouter 回應（最長 90 秒逾時）——因為這是管理員主動點擊、正在等結果的單次操作，不是排程批次任務，不需要輪詢機制。
- **前端**：`#detail-view` 最上方有個 `.admin-only.hidden` 卡片（非管理員完全看不到，連 `GET` 都不會打，省一次無意義的請求），`loadStockDetail()` 載入時若 `state.user.is_admin` 才順便抓快取結果；點「重新分析」呼叫 `runStockAiAnalysis()`，按鈕 disable 防止重複點擊（同一檔股票短時間連點兩次會疊加成兩筆 OpenRouter 費用）。評級 dot 重用 `_annRatingDot()`，跟自結公告同一套 🔴🟠🟡🟢 視覺語言。

## 個股筆記／AI分析筆記（2026-08-16 新增，2026-08-21 開放所有人讀取，純文字、不呼叫任何 AI）

跟上面「AI 個股分析」是兩個刻意分開的獨立功能：後者是**自動付費**、內容格式固定（評級+定價+分析文字）、任何人都能讀的快取；這個是**管理員自己貼上去的自由文字**（來源不限——ChatGPT、Claude、自己手寫都可以），純儲存不觸發任何 AI 呼叫。**2026-08-21 起 GET 對所有使用者開放**（原本跟 PUT 一樣只有管理員能看，因為這是私人研究筆記；後來決定內容公開、只鎖編輯權），`PUT` 仍然只有管理員（`_is_admin()`）。

- **DB**：`stock_notes`（`database.py` 的 `StockNote`），`stock_code` 為主鍵，一檔股票只留最新一份，沒有歷史版本，跟 `stock_ai_analysis` 同樣的「覆寫快取」模式。
- **API**：`GET`（所有人）／`PUT`（僅管理員）`/api/stocks/<code>/note`，`PUT` 的 body 是 `{content: "..."}`。
- **前端**：`#stock-note-card`（`.card.hidden`，不再是 `.admin-only`，緊接在 AI 個股分析卡片下方），標題「📝 AI分析筆記」。`loadStockDetail()` 一律呼叫 `loadStockNote(code)`（不分身分）；管理員看到可編輯的 `<textarea class="form-input stock-note-textarea">` + 「儲存筆記」按鈕（按鈕仍是 `.admin-only`），一般使用者看到唯讀的 `#stock-note-display`——`_formatNoteForDisplay()` 把內容裡每個中文句號「。」後面自動插入 `<br>` 換行，因為貼上來的原始文字常常整段沒有斷句，純文字塊很難讀；admin 編輯用的 textarea 不做這個轉換，存檔內容維持原樣。`saveStockNote()` 呼叫 `PUT`，按鈕 disable 防止重複送出。
