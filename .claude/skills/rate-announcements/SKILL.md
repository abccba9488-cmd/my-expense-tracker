---
name: rate-announcements
description: 把「自結公告」頁面裡尚未評級的公告，逐筆用 ChatGPT 網頁版分析、寫回 AI評級，並把回覆存進對應股票詳情頁的「AI分析筆記」。使用者說「幫我跑自結公告評級」「今天的自結公告評完了嗎」「/rate-announcements」等請求時使用。需要瀏覽器自動化（claude-in-chrome）。
---

# 自結公告 AI評級批次處理

把「自結公告」（`#ann-view`）頁面裡從最新一筆往回數、尚未評級的公告，逐筆送到 ChatGPT 網頁版分析，讀取回覆判斷評級，寫回 `ai_rating`，並把 ChatGPT 完整回覆存進該股票個股詳情頁的「AI分析筆記」（`stock_notes`）。這是 2026-09-07 從一次手動操作提煉出的流程，所有細節（含剪貼簿在自動化環境下不可靠、要改用 JS 直接操作）都是實測驗證過的，照著做即可，不用重新摸索。

## 0. 前置需求（缺一項就先請使用者處理，不要自己想辦法繞過）

- 本機 Flask server 要在跑：`GET http://127.0.0.1:5000/api/crawler/status` 要連得到。連不到就用 `Start-Process` 起服務（見專案 CLAUDE.md「本機重啟 FINMIND_TOKEN」章節的正確重啟方式），不要用 `start.bat`/Bash 工具重啟。
- 需要一個 Chrome 分頁已經用**管理員帳號**登入 `http://127.0.0.1:5000/`——AI評級／AI分析筆記的寫入 API 都要求管理員。**不能自己輸入密碼**，缺這個要請使用者自己登入後再繼續。
- 需要 Chrome 能連上 `https://chatgpt.com` 且已登入——**同樣不能自己登入**，缺的話請使用者確認。
- claude-in-chrome 瀏覽器工具若還是 deferred 狀態，先用 `ToolSearch` 一次載入常用的一批（`tabs_context_mcp`/`navigate`/`computer`/`read_page`/`tabs_create_mcp`/`tabs_close_mcp`/`find`/`get_page_text`/`javascript_tool`）。

## 1. 決定要處理的批次

呼叫 `GET /api/announcements/today`（不需要登入也能讀，直接用 `javascript_tool` 對已登入那個分頁 `fetch` 最簡單）。回傳依 `announce_date DESC, announce_time DESC` 排序（最新在最前面）。**從 index 0 開始往後找，遇到第一筆 `ai_rating` 不是空字串的項目就停止**——它之前（不含它自己）的所有項目就是這次要處理的批次。這是使用者明確定的規則，不看日期、只看有沒有評級。

若批次是空的（第0筆本身就已經有評級），直接跟使用者說「目前沒有未評級的公告」，結束，不用往下跑。

## 2. 建議：把整批處理工作交給一個 fork 背景子程序

逐筆都要開新分頁、等 ChatGPT 回覆（常要等 10-30 秒）、確認送出成功、讀取內容、寫回兩個地方再驗證——單筆就要不少輪次，10 筆左右容易撞到 200 輪次上限。**用 Agent 工具 spawn 一個 `fork`**（不是全新 agent；fork 才會繼承目前已登入的分頁 tabId），把下面「逐筆處理流程」完整交給它執行。若它回報卡在 200 輪次上限（`status: completed` 但 summary 提到 turn limit），用 `SendMessage` 請它從中斷處繼續，重複直到它給出「全部處理完畢」的結束報告為止——不要重頭來過。

## 3. 逐筆處理流程（fork 執行，或沒有 spawn 時自己執行）

對批次裡每一筆，依原本順序（從最新的第一筆開始往下）：

**Step 0 — 判斷這筆要不要存筆記**：維護一個「本次批次已處理過的 `stock_code` 集合」（從空集合開始）。若這筆的 `stock_code` **已經在集合裡**（代表批次裡更新的一筆已經處理過同一支股票），這一筆**不要存 AI分析筆記**，只做評級；若**不在集合裡**，才要存筆記，並把 `stock_code` 加進集合。因為批次本身就是新到舊排序，「第一次出現」永遠是該股票在批次裡最新的一筆——這就是使用者要求的「同一檔股票出現兩次，筆記以公告時間最新那一筆為主」，每一筆的評級仍然各自獨立照做。

**Step 1 — 取得提示詞**：主站分頁上用 `javascript_tool` 直接呼叫頁面既有的 `copyAnnRatingPrompt`/`_annData` 邏輯取得這筆的完整提示詞文字（不用真的點 📋 觸發它的複製到剪貼簿+開分頁副作用，直接讀 `_annData[idx]` 組出的內容即可，跟按鈕組出來的是同一份）。

**Step 2 — 開 ChatGPT 分頁並貼上**：用 `tabs_create_mcp` 開新分頁到 `https://chatgpt.com`。**剪貼簿貼上在這個瀏覽器自動化環境裡實測不可靠**（貼上內容經常是錯誤/舊內容），改用穩定作法：`javascript_tool` 對輸入框先 focus，再 `document.execCommand('insertText', false, promptText)` 直接寫入文字，然後送出（Enter 或點送出鍵）。**送出鍵有時第一次點擊不會真的送出**，送出後檢查網址是否從首頁變成帶對話ID的網址，沒變就重試一次或改用 `el.click()`。

**Step 3 — 等待回覆**：分段 `wait`（例如先等 10 秒），用截圖或 `get_page_text` 確認送出鍵/停止鍵狀態是否還在變化，必要時再多等幾輪，最多等到約 90 秒。逾時仍無回覆、或出現額度/錯誤訊息，記錄這筆失敗（略過評級與筆記），跳到下一筆。

**Step 4 — 讀取回覆並判斷評級**：`javascript_tool` 讀出 ChatGPT 最後一則回覆訊息的 `innerText` 全文（不用點 ChatGPT 自己的複製按鈕，同樣是剪貼簿不可靠的緣故）。判斷屬於 🔴強烈買進／🟠建議買進／🟡一般觀望／🟢需要小心 四選一。若回覆含糊、對不到明確選項，記錄失敗，跳過評級與筆記，繼續下一筆。

**Step 5 — 關閉分頁、切回主站**：`tabs_close_mcp` 關掉 ChatGPT 分頁，切回主站分頁。

**Step 6 — 寫入 AI評級**：`javascript_tool` 直接呼叫：
```js
fetch('/api/announcements/' + id + '/rating', {
  method: 'PUT',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({rating: '🔴 強烈買進'})  // 四選一，逐字元比對，emoji後面有一個空格
})
```
合法值務必逐字元比對（`app.py` 定義）：`'🔴 強烈買進'` / `'🟠 建議買進'` / `'🟡 一般觀望'` / `'🟢 需要小心'`。**不要透過畫面點下拉選單**——實測過前端依賴的 `state.user.is_admin` 有時沒正確帶入，會讓下拉選單顯示唯讀，直接呼叫 API 更可靠。確認回傳 `{ok:true}`。

**Step 7 — 依 Step 0 的判斷存筆記**：若這筆該存筆記：
```js
fetch('/api/stocks/' + stock_code + '/note', {
  method: 'PUT',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({content: chatgptReplyText})
})
```
確認回傳 `{ok:true}`。同一支股票的筆記是覆蓋式儲存（`stock_notes` 以 `stock_code` 為主鍵，只留最新一份），這是預期行為。

**Step 8 — 低本益比自動加自選（2026-09-07 新增規則）**：檢查這筆的 `estimated_pe` 欄位（`_annData[idx].estimated_pe`，公告解析時算好的預估本益比）——**若 `estimated_pe` 不是 null 且 `0 < estimated_pe < 20`**（正值代表本益比有意義；負值是虧損股算出來的無意義負數，不算），就把這支股票加進名為「自結eps<20」的自選股清單：
```js
// 先確認清單存在，取得它的 id（清單已存在，通常是 id 4，但每次還是用名稱查、不要寫死id）
const wl = await (await fetch('/api/watchlists')).json();
let target = wl.find(w => w.name === '自結eps<20');
if (!target) {
  // 理論上不會發生（清單已存在），只是防禦性寫法：真的沒有才建立
  target = await (await fetch('/api/watchlists', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name: '自結eps<20'})
  })).json();
}
await fetch(`/api/watchlists/${target.id}/stocks`, {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({code: stock_code})
});
```
股票已經在清單裡時後端本來就是安全的重複加入（不會報錯/不會產生重複），不用先檢查是否已存在。這一步跟「AI評級」「AI分析筆記」是三件獨立的事，`estimated_pe` 不滿足門檻就單純跳過，不影響前面兩步的結果。

**Step 9 — 驗證**：重新 `fetch('/api/announcements/today')`，確認這筆 `id` 的 `ai_rating` 已等於剛設定的值；該存筆記的那幾筆額外 `fetch('/api/stocks/<code>/note')` 確認 `content` 等於剛存的內容；有觸發 Step 8 的那幾筆額外 `fetch('/api/watchlists')` 確認該股票代號已經在「自結eps<20」清單的 `codes` 裡。沒通過就重試一次對應步驟；還是不行才記錄失敗原因，繼續下一筆。

**Step 10**：處理下一筆，直到批次跑完。

## 4. 完成後回報

給使用者：批次共幾筆、每一筆的代號/名稱/公告日期時間/最終評級/是否存了筆記；失敗或跳過的筆數與原因（額度限制、回覆看不懂、驗證失敗等）。
