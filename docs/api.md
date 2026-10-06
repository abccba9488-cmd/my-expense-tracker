# REST API 與後台管理頁

> 從 `CLAUDE.md` 拆出（2026-10-06，內容未改寫）。文中「見○○章節」若不在本檔，用 `grep -n '^## ' CLAUDE.md docs/*.md` 找。

## REST API

```
GET  /api/market/summary           所有股票快照（_SUMMARY_SQL，有 5 分鐘 server cache）
GET  /api/market/summary.csv       CSV 下載（UTF-8 BOM）
GET  /api/stocks/<code>           股票基本資料（代號/名稱/市場/產業）
GET  /api/stocks/<code>/prices     個股歷史股價（?days=90）
GET  /api/stocks/<code>/revenue    個股月營收
GET  /api/stocks/<code>/financials 個股季財報
GET  /api/stocks/<code>/fundamentals  逐季獲利/財務健康比率＋股利歷史＋最新估值快照（`experts.get_stock_fundamentals()`，跟達人選股計分引擎共用同一套比率公式，但保留逐季明細供詳情頁畫趨勢圖，而非計分引擎用的「近N季平均」）
GET  /api/stocks/<code>/macro-extra  總體分析補充資料專用：financial_extra 近8季現金流/資產負債表原始值、股權分散近12週、董監持股近6個月、近5年填息事件（見「總體分析」章節）
GET  /api/stocks/<code>/health      持股健康檢查（正常/早期警告/注意/撤退），見「持股健康檢查」章節
GET  /api/stocks/<code>/ai-analysis  讀取該股快取的 AI 分析結果（任何人可讀，不會觸發新分析）
GET  /api/stocks/<code>/note        讀取AI分析筆記（所有人可讀），見「個股筆記」章節
PUT  /api/stocks/<code>/note        儲存AI分析筆記（僅管理員）
POST /api/stocks/<code>/ai-analysis  觸發一次全新 AI 分析（僅管理員；同步執行，會產生 OpenRouter 費用）
GET  /api/stocks/<code>/expert-scores  該股在全部達人選股規則下的最新分數/明細
GET  /api/stocks/<code>/backtest/sweet-spot  單一股票「甜蜜點」4種天期(20/60/120/240日)訊號回測、不加碼（?years=5），見「甜蜜點訊號回測」章節（獲利目標 +10/15/20/25/30%）
GET  /api/stocks/<code>/institutional-trades  三大法人（外資/投信/自營商）每日買賣超近N天（?days=90），資料已由排程抓好，純讀取
POST /api/stocks/<code>/institutional-trades/refresh  手動補齊近5個曆日三大法人資料（需登入），修復排程「跑了但資料殘缺」的已知缺口，見「達人選股」章節
GET  /api/stocks/<code>/chip-peak  籌碼峰 POC/VAH/VAL + poc_history（POC遷移軌跡）（?lookback=120&half_life=30），純運算不落地存表，見「籌碼峰」章節
GET  /api/stocks/<code>/chanlun   纏論：筆/中樞/背馳/買賣點（?lookback=250），純運算不落地存表，近似版（跳過線段層級），見「纏論」章節
GET  /api/stocks/<code>/broker-trades  券商分點單日買賣超近N天（?days=90），見「券商分點進出」章節
POST /api/stocks/<code>/broker-trades/fetch  個股詳情頁「查詢」按鈕觸發（需登入），同步執行，見「券商分點進出」章節
POST /api/stocks/<code>/dividend/fetch  個股詳情頁「股利政策」區塊「回補最新資料」按鈕觸發（需登入），同步執行 `crawler.backfill_dividend_policy(code)`

以下 10 個皆需登入才可用、不限管理員（`_is_logged_in()` 擋 403，非只是前端隱藏），見「期權籌碼分析」章節：
GET  /api/taifex/summary                    今日摘要（期貨收盤/漲跌、PC Ratio、三大法人期貨淨部位+約當大台、十大交易人淨部位、大戶多空比、TW VIX、CNN Fear & Greed）
GET  /api/taifex/futures-institutional      三大法人期貨買賣近N天（?days=90），依機構別分開，含約當大台
GET  /api/taifex/option-institutional       三大法人選擇權買賣近N天，依買權/賣權+機構別分開，含契約金額
GET  /api/taifex/pc-ratio                   Put/Call Ratio 時間序列，附期貨收盤價供疊圖
GET  /api/taifex/large-traders              十大交易人期貨未沖銷部位近N天（?contract_type=all預設）
GET  /api/taifex/option-large-traders       十大交易人選擇權未沖銷部位近N天，依買權/賣權分開
GET  /api/taifex/support-resistance         選擇權賣方支撐/壓力，一次回傳全部4種到期別（週三/週五/月/下月）
GET  /api/taifex/daily-detail               上述資料合併成逐日一列的明細表格
GET  /api/taifex/vix-history                TW VIX 歷史趨勢近N天（?days=90），附期貨收盤價供雙軸疊圖
GET  /api/taifex/fear-greed-history         CNN Fear & Greed Index 歷史趨勢近N天，附期貨收盤價供雙軸疊圖
GET  /api/experts                  全部達人選股規則清單（標籤、通過檔數/總檔數，泛型驅動於 experts.EXPERT_LABELS，新增規則不用改這個端點）
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
GET  /api/watchlists/<id>/stress-test  該清單投資組合壓力測試（需登入且是清單擁有者），見「投資組合壓力測試」章節

GET  /api/messages                 留言板列表（最新 100 筆，含 can_delete 旗標）
POST /api/messages                 發表留言（需登入，內容上限 500 字）
DELETE /api/messages/<id>          刪除留言（本人或 ADMIN_USERNAME）

GET  /api/announcements/today      自結公告清單（**2026-08-17 起回傳全部歷史**，原本寫死近7天已移除，依日期/時間降序，含 content 全文供前端 modal 使用）
PUT  /api/announcements/<id>/rating  手動設定該筆公告的 AI 評級（僅限管理員），見「自結公告」章節

GET  /api/admin/users              會員列表 + 各自自選股清單數（僅 ADMIN_USERNAME）
DELETE /api/admin/users/<id>       刪除會員（連同其自選股清單與留言；無法刪除管理員自己）
GET  /api/admin/health             系統健康快照：DB 筆數統計、FINMIND_TOKEN 是否設定、各爬蟲任務最後執行時間/狀態/近20次成功率（僅 ADMIN_USERNAME），見「後台管理頁面」章節
GET  /api/admin/messages           留言板全部留言（上限500則）+ 總數 + 依使用者留言數排行（僅 ADMIN_USERNAME）
POST /api/admin/messages/bulk-delete  批次刪除留言，body `{ids: [...]}`（僅 ADMIN_USERNAME）
GET  /api/admin/visits             訪客紀錄：今日不重複IP數/今日進站次數/累計進站次數 + 最近200筆明細（僅 ADMIN_USERNAME）
```

`ADMIN_USERNAME`（`app.py`）為留言板管理員帳號，可刪除任何人的留言。

## 後台管理頁面（/admin，2026-07-27 新增）

獨立頁面（`templates/admin.html` + `static/js/admin.js`），取代原本散落在首頁浮動面板裡的管理功能（👥 會員管理浮動面板、⚙ 爬蟲狀態面板裡的一排 admin-only 按鈕），整合成一個頁面方便之後擴充。`GET /admin`（`app.py`）伺服器端用 `_is_admin()` 檔非管理員（`redirect('/')`），`admin.js` 的 `initAuth()` 再做一次前端檢查（處理頁面已載入後 session 過期的情況）。首頁 nav 的 👥 按鈕已移除，改成 🛠 連結（`.admin-only.hidden`，指向 `/admin`）；首頁 ⚙ 爬蟲狀態浮動面板只保留公開的「更新股票清單」按鈕與一個「更多爬蟲控制 →」連結，其餘 admin-only 爬蟲按鈕全部移到這個新頁面。

四個區塊（各自一個 `.card`）：
1. **系統健康**：`/api/admin/health` 回傳的 DB 筆數統計（股票/股價/月營收/季財報/會員/留言）+ 最新股價日期，以及 `FINMIND_TOKEN` 是否設定的醒目 badge（對齊 CLAUDE.md「`FINMIND_TOKEN` 沒設定是一個容易忽略的靜默失敗陷阱」那段描述的問題，讓管理員不用等到手動比對各表 `MAX(date)` 才發現）。
2. **爬蟲控制與排程狀態**：全部 9 個手動觸發按鈕（沿用既有 `runCrawler()`/`/api/crawler/run/<task>`）+ 一張「任務健康表」（`_HEALTH_TASKS` 常數，`app.py`，對齊 `crawler.py` 裡實際出現的 `_log()` task 名稱）顯示每個任務最後執行時間/狀態/近20次成功率/最後訊息，下方是既有的「最近30筆執行紀錄」（沿用 `/api/crawler/status`）。
3. **會員管理**：沿用原本浮動面板的邏輯與 CSS 類別（`.admin-user-list`/`.admin-user-item` 等），只是不再用 `position:fixed` 的面板，改放在一般 `.card` 裡。
4. **留言板管理**：`/api/admin/messages` 一次取全部（上限500則，本站留言量還小，暫不做分頁）+ 依使用者留言數排行；表格支援複選（`.admin-chk`）+ 全選 + 批次刪除（`/api/admin/messages/bulk-delete`），也保留單則刪除（沿用既有 `DELETE /api/messages/<id>`，該端點本來就允許本人或 ADMIN_USERNAME 刪除）。
5. **訪客紀錄（2026-07-27 新增）**：`app.py` 的 `_log_visit()`（`@app.before_request`）只記錄 `GET /`（SPA 唯一入口頁，不含 API/靜態資源請求），寫入新表 `visit_logs`（`database.py`，`ip`/`path`/`referrer`/`user_agent`/`username`[登入才有值]/`created_at`）。`_client_ip()` 優先讀 `X-Forwarded-For`（首個值）、沒有才退回 `request.remote_addr`——本機直接連線時兩者一樣，但透過 `ngrok.bat` 對外開放時，TCP peer 永遠是本機 ngrok agent（127.0.0.1），真實訪客 IP 只會出現在 `X-Forwarded-For`，沒有這個判斷會讓所有外部訪客都顯示成本機位址。`GET /api/admin/visits` 回傳今日不重複IP數/今日進站次數/累計進站次數 + 最近200筆明細，前端表格用 `_visitReferrer()` 把來源網址簡化成 hostname（無 referrer 顯示「直接造訪」）。記錄失敗（例如 DB 忙碌）只記 log、不影響首頁本身的回應。`visit_logs` 目前沒有裁剪機制，量小暫不處理，之後成長明顯的話可仿照 `trim_db.py` 的做法定期清舊資料。

`admin.html` 自帶一份簡化版 nav（回首頁連結 + 主題切換 + 登出），CSS 完全重用 `static/css/style.css`（`.card`/`.ann-table`/`.status-logs`/`.admin-user-*` 等既有類別），只新增少量必要的 class（`.health-grid`/`.health-tile`/`.token-badge`/`.admin-bulk-bar`/`.admin-chk`）。`admin.js` 是獨立檔案（不 import `app.js`），複製了少數共用小函式（`showToast`/`_escapeHtml`/`runCrawler`/`loadCrawlerStatus`）以維持頁面獨立、不用擔心 `app.js` 那份針對首頁 DOM 結構寫的邏輯誤觸發。
