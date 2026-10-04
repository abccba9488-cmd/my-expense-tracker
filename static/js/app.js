/* ── State ── */
const state = {
  allData:         [],
  currentMarket:   'all',
  currentIndustry: 'all',
  currentCode:     null,
  activeTab:       'list',
  activeWlId:      null,
  user:            null,
  watchlists:      [],
  priceChart:      null,
  revenueChart:    null,
  epsChart:        null,
  priceDt:         null,
  revenueDt:       null,
  quarterlyDt:     null,
  institutionalDt: null,
  priceDays:       90,
  fundamentals:      null,
  showFundamentals:  false,
  fundProfitChart:   null,
  fundHealthChart:   null,
  fundTurnoverChart: null,
  fundDividendChart: null,
  fundDividendDt:    null,
  chipPeak:          null,
};

/* ── Theme ── */
function initTheme() {
  const saved = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
  document.getElementById('theme-btn').textContent = saved === 'dark' ? '☀' : '🌙';
}
document.getElementById('theme-btn').addEventListener('click', () => {
  const cur = document.documentElement.getAttribute('data-theme');
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('theme', next);
  document.getElementById('theme-btn').textContent = next === 'dark' ? '☀' : '🌙';
  redrawCharts();
});

/* ── Helpers ── */
const fmt = {
  num:    v => v == null ? '—' : Number(v).toLocaleString(),
  price:  v => v == null ? '—' : Number(v).toFixed(2),
  pct:    v => v == null ? '—' : `${v > 0 ? '+' : ''}${Number(v).toFixed(2)}%`,
  rev:    v => v == null ? '—' : Number(v).toLocaleString(),
  eps:    v => v == null ? '—' : Number(v).toFixed(2),
};

function pctClass(v) {
  if (v == null) return 'neutral';
  return v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral';
}

// "甜蜜點"：MA20/MA60/MA120 are support/resistance tests — highlighted
// whenever price is within ±3% either side (red/yellow/green). MA240 is
// different on purpose — treated as a long-term value line rather than a
// support/resistance test: ANY price below it counts (however far below),
// but above it only counts up to +3% (deep-overbought territory above the
// 240-day average isn't a "sweet spot").
// Returns [sortVal, displayHtml] so DataTables can sort by colour block:
//   0 = near MA20 (red)          → sorts first
//   1 = near MA60 (yellow)
//   2 = near MA120 (green)
//   3 = at/below MA240, or up to 3% above it (purple)
//   4 = has MA data but not near/qualifying for any of the four
//   5 = no MA data at all        → sorts last
const _SWEET_SPOT_TIERS = [
  ['ma20',  '#ef4444', '#fff'],
  ['ma60',  '#eab308', '#000'],
  ['ma120', '#22c55e', '#fff'],
];
function sweetSpotCell(s) {
  const fill = 'display:block;margin:-9px -12px;padding:9px 12px;font-weight:600;';
  if (s.close != null) {
    for (let i = 0; i < _SWEET_SPOT_TIERS.length; i++) {
      const [key, bg, fg] = _SWEET_SPOT_TIERS[i];
      const ma = s[key];
      if (ma == null) continue;
      if (Math.abs(s.close - ma) / ma <= 0.03) {
        return [i, `<span style="${fill}background:${bg};color:${fg}">🔔 ${fmt.price(ma)}</span>`];
      }
    }
    if (s.ma240 != null && (s.close - s.ma240) / s.ma240 <= 0.03) {
      return [3, `<span style="${fill}background:#a855f7;color:#fff">🔔 ${fmt.price(s.ma240)}</span>`];
    }
  }
  if (s.ma20 != null) return [4, fmt.price(s.ma20)];
  return [5, '—'];
}

// Icon-only signal (no cell fill): set by the monthly-revenue crawler when
// the stock is still loss-making but this month's revenue YoY hit the same
// 20% bar used for 營收飆股 — a turnaround candidate worth watching.
function turnaroundCell(s) {
  return s.turnaround_signal ? '🔥' : '—';
}

function showToast(msg, ms = 2500) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function getCssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* ── DB stats ── */
async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();
    const el = document.getElementById('db-stats');
    if (d.stocks > 0) {
      el.textContent = `${d.stocks.toLocaleString()} 支 ｜ 最新 ${d.last_price_date || '—'}`;
      document.getElementById('init-banner').classList.add('hidden');
    } else {
      el.textContent = '尚無資料';
      document.getElementById('init-banner').classList.remove('hidden');
    }
  } catch (_) {}
}

function _statsFromData(data) {
  const el = document.getElementById('db-stats');
  if (!data.length) {
    el.textContent = '尚無資料';
    document.getElementById('init-banner').classList.remove('hidden');
    return;
  }
  const maxDate = data.reduce((mx, s) => (s.price_date || '') > mx ? s.price_date : mx, '');
  el.textContent = `${data.length.toLocaleString()} 支 ｜ 最新 ${maxDate || '—'}`;
  document.getElementById('init-banner').classList.add('hidden');
}

/* ── Stock list ── */
const _SUMMARY_CACHE_KEY = 'bao_sum_v1';
const _SUMMARY_TTL = 300_000; // 5 min
let mainDt = null;

async function loadMarketSummary() {
  // Serve cached data immediately if fresh (avoids blank screen while fetching)
  try {
    const raw = localStorage.getItem(_SUMMARY_CACHE_KEY);
    if (raw) {
      const { ts, data } = JSON.parse(raw);
      if (Date.now() - ts < _SUMMARY_TTL) {
        state.allData = data;
        populateIndustryFilter();
        renderStockTable();
        _statsFromData(data);
      }
    }
  } catch (_) {}

  // Always fetch fresh data in background
  try {
    const r = await fetch('/api/market/summary');
    const data = await r.json();
    state.allData = data;
    try {
      localStorage.setItem(_SUMMARY_CACHE_KEY, JSON.stringify({ ts: Date.now(), data }));
    } catch (_) {}
    populateIndustryFilter();
    renderStockTable();
    _statsFromData(data);
  } catch (e) {
    if (!state.allData.length) showToast('載入股票清單失敗');
  }
}

function populateIndustryFilter() {
  const sel = document.getElementById('industry-filter');
  const industries = [...new Set(state.allData.map(s => s.industry).filter(Boolean))].sort();
  sel.innerHTML = '<option value="all">所有產業</option>' +
    industries.map(i => `<option value="${i}">${i}</option>`).join('');
  sel.value = state.currentIndustry;
}

function renderStockTable() {
  let data = state.currentMarket === 'all'
    ? state.allData
    : state.allData.filter(s => s.market === state.currentMarket);
  if (state.currentIndustry !== 'all')
    data = data.filter(s => s.industry === state.currentIndustry);

  const rows = data.map(s => [
    `<span class="stock-link" data-code="${s.code}">${s.code}</span>`,
    `<span class="stock-link" data-code="${s.code}">${s.name}</span>`,
    s.industry || '—',
    s.start_price != null ? fmt.price(s.start_price) : '—',
    s.close != null ? fmt.price(s.close) : '—',
    s.price_diff != null ? `<span class="${pctClass(s.price_diff)}">${fmt.pct(s.price_diff)}</span>` : '—',
    s.change_pct != null
      ? `<span class="${pctClass(s.change_pct)}">${fmt.pct(s.change_pct)}</span>`
      : '—',
    (() => {
      if (s.revenue == null || s.qf_revenue == null || s.qf_revenue <= 0 || s.eps == null || s.eps <= 0) return '—';
      const est = (s.revenue / s.qf_revenue) * s.eps * 240;
      const val = fmt.price(est);
      const fill = 'display:block;margin:-9px -12px;padding:9px 12px;font-weight:600;';
      if (s.close != null && est >= s.close * 2)
        return `<span style="${fill}background:#ef4444;color:#fff">${val}</span>`;
      if (s.close != null && est >= s.close * 1.5)
        return `<span style="${fill}background:#eab308;color:#000">${val}</span>`;
      return val;
    })(),
    (s.rev_year && s.rev_month) ? `${s.rev_year}/${String(s.rev_month).padStart(2,'0')}` : '—',
    s.revenue != null ? fmt.rev(s.revenue) : '—',
    s.revenue_yoy != null
      ? `<span class="${pctClass(s.revenue_yoy)}">${fmt.pct(s.revenue_yoy)}</span>`
      : '—',
    s.qf_revenue != null ? fmt.rev(s.qf_revenue) : '—',
    s.eps != null
      ? `<span class="${pctClass(s.eps)}">${fmt.eps(s.eps)}</span>`
      : '—',
    s.pe_ratio != null ? Number(s.pe_ratio).toFixed(1) + 'x' : '—',
    (s.eps_year && s.eps_quarter) ? `${s.eps_year}Q${s.eps_quarter}` : '—',
    s.price_date || '—',
    sweetSpotCell(s),
    turnaroundCell(s),
  ]);

  if (mainDt) {
    mainDt.clear().rows.add(rows).draw();
  } else {
    mainDt = $('#stocks-table').DataTable({
      data: rows,
      deferRender: true,
      pageLength: 25,
      order:      [[0, 'asc']],
      language:   dtLang(),
      scrollX:    true,
      columnDefs: [
        { targets: [3, 4, 5, 6, 7, 9, 10, 11, 12, 13], className: 'dt-right', type: 'num-cell' },
        { targets: [0, 1, 2, 8, 14, 15], className: 'dt-left' },
        { targets: 16, className: 'dt-right', render: { _: 0, display: 1 } },
        // Fixed width — content is just '—' or '🔥', too narrow for
        // DataTables' auto column-width to size against its own header text.
        { targets: 17, className: 'dt-left', width: '64px' },
      ],
    });
    // Row click
    $('#stocks-table tbody').on('click', 'td', function() {
      const $link = $(this).find('[data-code]');
      const code = $link.data('code') || $(this).closest('tr').find('[data-code]').data('code');
      if (code) {
        setDetailNavContext(_dtOrderedCodes(mainDt, 0), String(code));
        loadStockDetail(code);
      }
    });
  }
}

// Custom numeric sort: strips HTML tags and formatting (%, x, commas) from
// rendered cell content; '—' / empty sorts as -Infinity (always last when sorting desc).
$.fn.dataTable.ext.type.order['num-cell-pre'] = function(data) {
  const text = String(data).replace(/<[^>]*>/g, '').trim();
  if (text === '' || text === '—') return -Infinity;
  const num = parseFloat(text.replace(/[^0-9.\-]/g, ''));
  return isNaN(num) ? -Infinity : num;
};

function dtLang() {
  return {
    search: '搜尋：',
    lengthMenu: '顯示 _MENU_ 筆',
    info: '第 _START_–_END_ 筆，共 _TOTAL_ 筆',
    paginate: { previous: '上頁', next: '下頁' },
    zeroRecords: '無資料',
  };
}

/* ── Market filter ── */
document.querySelectorAll('.filter-btn').forEach(btn => {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    state.currentMarket = this.dataset.market;
    renderStockTable();
  });
});

/* ── Industry filter ── */
document.getElementById('industry-filter').addEventListener('change', function() {
  state.currentIndustry = this.value;
  renderStockTable();
});

/* ── Stock detail ── */
/* ── Prev/next stock navigation within whichever list was clicked from ── */
function _codeFromCell(html) {
  const m = /data-code="([^"]+)"/.exec(html || '');
  return m ? m[1] : null;
}

// DataTables' current filter+sort order (not just the current page), so
// prev/next still makes sense after sorting a column or paginating.
function _dtOrderedCodes(dt, codeColIndex) {
  if (!dt) return [];
  return dt.rows({ order: 'applied', search: 'applied' }).data().toArray()
    .map(row => _codeFromCell(row[codeColIndex])).filter(Boolean);
}

function setDetailNavContext(codes, currentCode) {
  state.detailNavList = codes;
  state.detailNavIndex = codes.indexOf(currentCode);
  updateDetailNavButtons();
}

function updateDetailNavButtons() {
  // 上一檔/下一檔按鈕現在有3組（頂部/中間/底部，方便讀完一段內容不用拉
  // 回最上方），共用 class 不用 id，這裡用 querySelectorAll 統一更新，
  // 不用三份幾乎一樣的程式碼。
  const wraps = document.querySelectorAll('.detail-nav-btns');
  const list = state.detailNavList || [];
  const idx = state.detailNavIndex;
  const show = list.length > 0 && idx >= 0;
  wraps.forEach(wrap => {
    wrap.classList.toggle('hidden', !show);
    if (!show) return;
    wrap.querySelectorAll('.nav-prev-btn').forEach(b => { b.disabled = idx <= 0; });
    wrap.querySelectorAll('.nav-next-btn').forEach(b => { b.disabled = idx >= list.length - 1; });
  });
}

function goToAdjacentStock(delta) {
  const list = state.detailNavList || [];
  const newIdx = (state.detailNavIndex ?? -1) + delta;
  if (newIdx < 0 || newIdx >= list.length) return;
  state.detailNavIndex = newIdx;
  loadStockDetail(list[newIdx]);
  updateDetailNavButtons();
}

async function loadStockDetail(code) {
  code = String(code);
  state.currentCode = code;
  state.chanlunStarTrades = null;  // 上一支股票的歷史訊號疊圖不能沿用到新股票，見 renderPriceChart()
  state.flag888GuyuTrades = null;  // 同上，888標準3+股魚 疊圖
  showDetailView();

  // Stock info
  const info = state.allData.find(s => s.code === code) || {};
  document.getElementById('d-name').textContent     = info.name || code;
  document.getElementById('d-code').textContent     = code + (info.name ? ` ${info.name}` : '');
  document.getElementById('d-market').textContent   = info.market || '';
  document.getElementById('d-industry').textContent = info.industry || '';

  // Sync days-selector buttons with the persisted period (kept across stock switches)
  document.querySelectorAll('.days-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.days === String(state.priceDays));
  });

  // Load data in parallel
  const [prices, instTrades, revenues, financials, fundamentals, chipPeak, chanlunRes] = await Promise.all([
    _fetchPriceSeries(code, state.priceDays),
    _fetchInstTradeSeries(code, state.priceDays),
    fetch(`/api/stocks/${code}/revenue`).then(r => r.json()).catch(() => []),
    fetch(`/api/stocks/${code}/financials`).then(r => r.json()).catch(() => []),
    fetch(`/api/stocks/${code}/fundamentals`).then(r => r.json()).catch(() => null),
    fetch(`/api/stocks/${code}/chip-peak`).then(r => r.json()).catch(() => null),
    fetch(`/api/stocks/${code}/chanlun`).then(r => r.json()).catch(() => null),
  ]);

  state.chipPeak = (chipPeak && chipPeak.poc != null) ? chipPeak : null;
  renderChipPeak(state.chipPeak);
  state.chanlun = (chanlunRes && chanlunRes.strokes) ? chanlunRes : null;
  renderChanlun(state.chanlun);
  state.prices = prices;
  state.instTrades = instTrades;
  renderPriceChart(prices);
  renderPriceTable(_visiblePrices(prices));
  renderRevenueChart(revenues);
  renderRevenueTable(revenues);
  renderEpsChart(financials);
  renderQuarterlyTable(financials);
  renderFundamentalsPanel(fundamentals);
  loadStockExpertScores(code);
  loadStockInstitutionalTrades(code);
  loadStockBrokerTrades(code);
  resetStockBacktestCard();
  resetStockChanlunStarCard();
  resetStockFlag888GuyuCard();

  if (state.user && state.user.is_admin) {
    loadStockAiAnalysis(code);
  }
  loadStockNote(code);
}

/* ── AI 個股分析（admin only） ── */
function renderStockAiAnalysis(a) {
  const body = document.getElementById('stock-ai-body');
  if (!a || !a.ai_rating) {
    body.innerHTML = '尚無分析，點擊「重新分析」開始';
    return;
  }
  const targets = [
    a.target_cheap     != null ? `便宜價 ${a.target_cheap}` : null,
    a.target_fair       != null ? `合理價 ${a.target_fair}` : null,
    a.target_expensive  != null ? `昂貴價 ${a.target_expensive}` : null,
  ].filter(Boolean).join(' ｜ ');
  body.innerHTML = `
    <div class="ann-modal-rating">${_annRatingDot(a.ai_rating)} ${a.ai_rating}</div>
    ${targets ? `<div class="stock-ai-targets">${targets}</div>` : ''}
    <div class="ann-modal-analysis">${a.ai_analysis || ''}</div>
    <div class="stock-ai-updated">最後分析時間：${a.updated_at ? a.updated_at.slice(0, 16) : '—'}</div>
  `;
}

async function loadStockAiAnalysis(code) {
  const btn = document.getElementById('stock-ai-btn');
  if (btn) btn.disabled = false;
  try {
    const a = await fetch(`/api/stocks/${code}/ai-analysis`).then(r => r.json());
    renderStockAiAnalysis(a);
  } catch (_) {
    document.getElementById('stock-ai-body').innerHTML = '載入失敗';
  }
}

async function runStockAiAnalysis() {
  if (!state.currentCode) return;
  const btn = document.getElementById('stock-ai-btn');
  const body = document.getElementById('stock-ai-body');
  btn.disabled = true;
  body.innerHTML = '分析中，請稍候（即時搜尋通常需要數十秒）…';
  try {
    const resp = await fetch(`/api/stocks/${state.currentCode}/ai-analysis`, {method: 'POST'});
    const data = await resp.json();
    if (!resp.ok) {
      body.innerHTML = `分析失敗：${data.error || '未知錯誤'}`;
    } else if (!data.ai_rating) {
      body.innerHTML = 'AI 分析失敗（可能是 API 限速或暫時無法連線），請稍後再試一次';
    } else {
      renderStockAiAnalysis(data);
      showToast('分析完成');
    }
  } catch (_) {
    body.innerHTML = '分析失敗，請稍後再試';
  } finally {
    btn.disabled = false;
  }
}

/* ── AI分析筆記（所有人可讀，僅管理員可編輯，自由文字，不呼叫任何 AI） ── */
function _formatNoteForDisplay(text) {
  // 讀者端把每個句號後面自動換行，原始貼上的文字常常整段沒有斷句，
  // 純文字塊很難讀；admin 編輯用的 textarea 不做這個轉換，存檔內容維持原樣。
  const esc = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc.replace(/。/g, '。<br>').replace(/(<br>)+$/, '');
}

async function loadStockNote(code) {
  document.getElementById('stock-note-card').classList.remove('hidden');
  const ta = document.getElementById('stock-note-textarea');
  const display = document.getElementById('stock-note-display');
  const updated = document.getElementById('stock-note-updated');
  const isAdmin = !!(state.user && state.user.is_admin);
  ta.classList.toggle('hidden', !isAdmin);
  display.classList.toggle('hidden', isAdmin);
  ta.value = '';
  display.innerHTML = '';
  updated.textContent = '';
  try {
    const n = await fetch(`/api/stocks/${code}/note`).then(r => r.json());
    const content = n.content || '';
    if (isAdmin) {
      ta.value = content;
    } else {
      display.innerHTML = _formatNoteForDisplay(content);
    }
    updated.textContent = n.updated_at ? `最後更新：${n.updated_at.slice(0, 16)}` : '';
  } catch (_) {
    // 靜默失敗即可，筆記空白讓使用者重新輸入
  }
}

async function saveStockNote() {
  if (!state.currentCode) return;
  const ta = document.getElementById('stock-note-textarea');
  const btn = document.getElementById('stock-note-save-btn');
  const updated = document.getElementById('stock-note-updated');
  btn.disabled = true;
  btn.textContent = '儲存中…';
  try {
    const resp = await fetch(`/api/stocks/${state.currentCode}/note`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: ta.value}),
    });
    const data = await resp.json();
    if (!resp.ok) {
      showToast(`儲存失敗：${data.error || '未知錯誤'}`);
    } else {
      updated.textContent = data.updated_at ? `最後更新：${data.updated_at.slice(0, 16)}` : '';
      showToast('筆記已儲存');
    }
  } catch (_) {
    showToast('儲存失敗，請稍後再試');
  } finally {
    btn.disabled = false;
    btn.textContent = '儲存筆記';
  }
}

/* ── Days selector ── */
document.querySelectorAll('.days-btn').forEach(btn => {
  btn.addEventListener('click', async function() {
    if (!state.currentCode) return;
    document.querySelectorAll('.days-btn').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    const days = this.dataset.days;
    state.priceDays = Number(days);
    const [prices, instTrades] = await Promise.all([
      _fetchPriceSeries(state.currentCode, state.priceDays),
      _fetchInstTradeSeries(state.currentCode, state.priceDays),
    ]);
    state.prices = prices;
    state.instTrades = instTrades;
    renderPriceChart(prices);
    renderPriceTable(_visiblePrices(prices));
  });
});

/* Price / institutional series are fetched with extra warm-up history so the
   60-day MA and the 120-trading-day trust cost line are already valid at the
   left edge of the chart; state.prices holds the full (warm-up included)
   series, _visiblePrices() trims it to the selected period for the chart
   x-axis and the price table. */
const _CHART_WARMUP_DAYS = 200;  // calendar days, covers 120 trading days incl. long holidays
const _TRUST_COST_WINDOW = 120;  // trading days, same lookback as chip peak

function _warmupDays(days) { return days >= 9999 ? days : days + _CHART_WARMUP_DAYS; }

function _fetchPriceSeries(code, days) {
  return fetch(`/api/stocks/${code}/prices?days=${_warmupDays(days)}`).then(r => r.json()).catch(() => []);
}

function _fetchInstTradeSeries(code, days) {
  return fetch(`/api/stocks/${code}/institutional-trades?days=${_warmupDays(days)}`).then(r => r.json()).catch(() => []);
}

/* 投信推估成本線：for each day, replay the trailing `window` trading days of
   投信 net buy/sell from zero holdings using the moving-average cost method —
   a net buy re-weights the average cost at that day's typical price
   (H+L+C)/3, a net sell only reduces holdings (average cost unchanged; using
   the sell price would let profitable selling drag the "cost" down), and
   holdings falling to zero reset the position (null → line breaks instead
   of dividing by ~0). It's an estimate from daily net flows, not 投信's real
   book cost: positions held before the window and intraday fills are unknown. */
function _trustCostSeries(allPrices, instRows, window = _TRUST_COST_WINDOW) {
  const netByDate = new Map(instRows.map(r => [r.date, (r.trust_buy || 0) - (r.trust_sell || 0)]));
  const tp = allPrices.map(p => (p.high != null && p.low != null && p.close != null)
    ? (p.high + p.low + p.close) / 3 : p.close);
  return allPrices.map((_, i) => {
    if (i < window - 1) return null;
    let hold = 0, cost = 0;
    for (let j = i - window + 1; j <= i; j++) {
      const net = netByDate.get(allPrices[j].date) || 0;
      if (net > 0 && tp[j] != null) {
        cost = (cost * hold + net * tp[j]) / (hold + net);
        hold += net;
      } else if (net < 0) {
        hold += net;
        if (hold <= 0) { hold = 0; cost = 0; }
      }
    }
    return hold >= 1000 ? +cost.toFixed(2) : null;  // need at least 1 張 held
  });
}

function _priceCutoffDate() {
  const d = new Date();
  d.setDate(d.getDate() - state.priceDays);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

function _visiblePrices(prices) {
  if (state.priceDays >= 9999) return prices;
  const cutoff = _priceCutoffDate();
  return prices.filter(p => p.date >= cutoff);
}

function _movingAverage(values, period) {
  let sum = 0, count = 0;
  const out = [];
  values.forEach((v, i) => {
    sum += v ?? 0; count += v != null ? 1 : 0;
    if (i >= period) { const old = values[i - period]; sum -= old ?? 0; count -= old != null ? 1 : 0; }
    out.push(i >= period - 1 && count === period ? +(sum / period).toFixed(2) : null);
  });
  return out;
}

/* ── Price chart ── */
/* ── Chip peak (Phase 1: pure computation, see chip_peak.py) ── */
function renderChipPeak(chipPeak) {
  const box = document.getElementById('chip-peak-stats');
  if (!chipPeak) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  const distPct = chipPeak.price_to_poc * 100;
  const pocTip = chipPeak.quality_weighted
    ? `近${chipPeak.lookback_days}個交易日，經時間衰減＋三大法人買賣超品質加權後成交量最集中的價位（窗口內${(chipPeak.quality_coverage * 100).toFixed(0)}%交易日有法人資料），可視為市場主要成本區。使用未還原除權息股價估算，僅供參考。`
    : `近${chipPeak.lookback_days}個交易日，經時間衰減加權後成交量最集中的價位，可視為市場主要成本區。此窗口內沒有法人買賣超資料可用於品質加權。使用未還原除權息股價估算，僅供參考。`;
  box.innerHTML = `
    <div class="fund-stat-tile" title="${pocTip}">
      <div class="fund-stat-label">籌碼峰 POC（${chipPeak.lookback_days}日）</div>
      <div class="fund-stat-value">${fmt.price(chipPeak.poc)}</div>
    </div>
    <div class="fund-stat-tile" title="涵蓋約70%加權成交量的價格區間，範圍外的價位近期交易相對稀少。">
      <div class="fund-stat-label">價值區間 VAL–VAH</div>
      <div class="fund-stat-value">${fmt.price(chipPeak.val)}–${fmt.price(chipPeak.vah)}</div>
    </div>
    <div class="fund-stat-tile" title="目前股價偏離主要成本區（POC）的百分比，正值代表現價高於POC。">
      <div class="fund-stat-label">現價距 POC</div>
      <div class="fund-stat-value ${pctClass(distPct)}">${fmt.pct(distPct)}</div>
    </div>
    <div class="fund-stat-tile" title="POC 那個價位的加權成交量，占整個窗口總量的比例。數字越高代表籌碼越集中在單一價位。">
      <div class="fund-stat-label">主峰集中度</div>
      <div class="fund-stat-value">${(chipPeak.peak_strength * 100).toFixed(1)}%</div>
    </div>
  `;
  box.classList.remove('hidden');
}

/* Appends/updates the 「現價距投信成本」 tile in the chip-peak stats row
   (renderChipPeak() rebuilds that row first, then renderPriceChart() calls
   this). Uses the full series' latest value, independent of the days selector. */
function _renderTrustCostTile(allPrices, trustCostAll) {
  const box = document.getElementById('chip-peak-stats');
  box.querySelector('#trust-cost-tile')?.remove();
  const cost = trustCostAll ? trustCostAll[trustCostAll.length - 1] : null;
  const close = allPrices.length ? allPrices[allPrices.length - 1].close : null;
  if (cost == null || close == null) {
    if (!box.children.length) box.classList.add('hidden');
    return;
  }
  const dev = (close - cost) / cost * 100;
  box.insertAdjacentHTML('beforeend', `
    <div class="fund-stat-tile" id="trust-cost-tile" title="（現價 − 投信推估成本）÷ 投信推估成本。投信成本＝近${_TRUST_COST_WINDOW}個交易日投信買賣超以移動平均成本法推估，非投信實際持股成本。">
      <div class="fund-stat-label">現價距投信成本（${fmt.price(cost)}）</div>
      <div class="fund-stat-value ${pctClass(dev)}">${fmt.pct(dev)}</div>
    </div>`);
  box.classList.remove('hidden');
}

const _CHANLUN_SIGNAL_LABELS = { '1b': '一買', '2b': '二買', '3b': '三買', '1s': '一賣', '2s': '二賣', '3s': '三賣' };

function renderChanlun(chanlun) {
  const box = document.getElementById('chanlun-stats');
  if (!chanlun) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  const latest = chanlun.latest_signal;
  const latestLabel = latest ? `${_CHANLUN_SIGNAL_LABELS[latest.type]} ${fmt.price(latest.price)}（${latest.date}）` : '近期無訊號';
  const latestClass = latest ? (latest.type.endsWith('b') ? 'pos' : 'neg') : '';
  box.innerHTML = `
    <div class="fund-stat-tile" title="依「筆＋中樞＋MACD背馳」推導的近似買賣點，跳過線段層級判斷，見上方 ⓘ 說明的重要限制。">
      <div class="fund-stat-label">纏論最新訊號</div>
      <div class="fund-stat-value ${latestClass}">${latestLabel}</div>
    </div>
    <div class="fund-stat-tile" title="近一年（250個交易日）內辨識出的筆＋中樞數量，數量越多代表這段期間走勢越震盪。">
      <div class="fund-stat-label">筆／中樞數</div>
      <div class="fund-stat-value">${chanlun.strokes.length} / ${chanlun.centers.length}</div>
    </div>
  `;
  box.classList.remove('hidden');
}

/* 把纏論的筆端點/中樞區間/買賣點（都是稀疏的特定日期）對應到股價圖完整
   的日期軸上，只在該日期有值、其餘留 null——纏論固定用自己的250天分析
   窗（跟籌碼峰一樣獨立於 days-selector），選較短天數時，落在可視範圍外
   的點自然對不到 labels、不會出現，不用額外裁切。 */
function _chanlunPointSeries(labels, points) {
  const byDate = new Map(points.map(p => [p.date, p.price]));
  return labels.map(d => byDate.has(d) ? byDate.get(d) : null);
}

function _chanlunCenterSeries(labels, centers, field) {
  return labels.map(d => {
    const c = centers.find(c => d >= c.start_date && d <= c.end_date);
    return c ? c[field] : null;
  });
}

function _chanlunSignalSeries(labels, signals, isBuy) {
  const byDate = new Map();
  signals.filter(s => s.type.endsWith(isBuy ? 'b' : 's')).forEach(s => byDate.set(s.date, s));
  return labels.map(d => byDate.has(d) ? byDate.get(d).price : null);
}

function _chanlunStarEntrySeries(labels, trades) {
  const byDate = new Map(trades.map(t => [t.entry_date, t.entry_price]));
  return labels.map(d => byDate.has(d) ? byDate.get(d) : null);
}

function _chanlunStarExitSeries(labels, trades) {
  const byDate = new Map();
  trades.filter(t => t.exit_date).forEach(t => byDate.set(t.exit_date, t.exit_price));
  return labels.map(d => byDate.has(d) ? byDate.get(d) : null);
}

// 888標準3+股魚 疊圖用，跟上面兩個函式邏輯完全一樣，只是分開命名對應
// state.flag888GuyuTrades（見 renderPriceChart()），保持跟 chanlun_star
// 疊圖同樣獨立、互不影響。
function _flag888GuyuEntrySeries(labels, trades) {
  const byDate = new Map(trades.map(t => [t.entry_date, t.entry_price]));
  return labels.map(d => byDate.has(d) ? byDate.get(d) : null);
}

function _flag888GuyuExitSeries(labels, trades) {
  const byDate = new Map();
  trades.filter(t => t.exit_date).forEach(t => byDate.set(t.exit_date, t.exit_price));
  return labels.map(d => byDate.has(d) ? byDate.get(d) : null);
}

/* 把 chip-peak 的 poc_history（較稀疏的取樣點，每筆帶 poc/vah/val，見
   chip_peak.py compute_chip_peak_series）對應到股價圖的完整日期軸上，取樣點
   之間用最近一次算出的值往後補滿（階梯狀，不是內插），取樣範圍以前的日期留
   null（沒有那麼久以前的資料，畫不出來就不畫，不瞎猜）。labels 與 history
   都假設是日期字串由舊到新排序，field 是 'poc'/'vah'/'val' 三選一。 */
function _chipPeakFieldSeries(labels, history, field) {
  let hi = 0, current = null;
  return labels.map(d => {
    while (hi < history.length && history[hi].date <= d) {
      current = history[hi][field];
      hi++;
    }
    return current;
  });
}

function renderPriceChart(allPrices) {
  const canvas = document.getElementById('price-chart');
  if (state.priceChart) { state.priceChart.destroy(); state.priceChart = null; }
  const prices = _visiblePrices(allPrices);
  if (!prices.length) { document.getElementById('price-chart-groups')?.replaceChildren(); return; }

  const labels = prices.map(p => p.date);
  const n = prices.length;
  const offset = allPrices.length - n;  // visible slice is the tail of allPrices

  // Determine x-axis tick density based on period length
  const maxTicks = n > 1000 ? 10 : n > 365 ? 12 : n > 90 ? 18 : 30;

  const opts = chartOptions('股價 (元)');
  opts.scales.x.ticks = { ...opts.scales.x.ticks, maxTicksLimit: maxTicks };
  if (n > 500) {
    opts.plugins.decimation = { enabled: true, algorithm: 'min-max' };
  }

  opts.scales.x.offset = true;          // keep first/last candle fully visible
  opts.scales.y.beginAtZero = false;
  // Price and volume share one canvas as two vertically stacked y scales
  // (same `stack`, stackWeight 4:1), so the volume pane lines up with the
  // candles on the same x axis. Volume is shown in 張 (daily_prices.volume is 股).
  // Within a stack Chart.js lays scales out top-to-bottom in reverse definition
  // order, so yVol must be defined before y to sit in the bottom pane.
  Object.assign(opts.scales.y, { stack: 'price', stackWeight: 4 });
  const { x: xScale, y: yScale } = opts.scales;
  opts.scales = { x: xScale, yVol: {
    stack: 'price', stackWeight: 1, position: 'left', offset: true, beginAtZero: true,
    grid: { color: getCssVar('--border') },
    ticks: { color: getCssVar('--text2'), maxTicksLimit: 3,
      callback: v => v >= 10000 ? `${+(v / 10000).toFixed(1)}萬` : v.toLocaleString() },
    title: { display: true, text: '成交量 (張)', color: getCssVar('--text2') },
  }, y: yScale };

  // Candlesticks drawn as two overlapping floating-bar datasets (wick = [low, high],
  // body = [open, close]) so no extra financial-chart plugin / time scale is
  // needed and every existing category-axis overlay below keeps working.
  // Taiwan convention: red = up, green = down. Higher `order` draws underneath,
  // so wick < body < MA/overlay lines (order 0).
  const UP = '#ef4444', DOWN = '#22c55e';
  const candleColor = prices.map(p => (p.close ?? 0) >= (p.open ?? p.close ?? 0) ? UP : DOWN);
  const ohlcOk = p => p.open != null && p.high != null && p.low != null && p.close != null;
  const datasets = [
    { type: 'bar', label: 'K線', order: 1, grouped: false,
      data: prices.map(p => ohlcOk(p) ? [p.open, p.close] : null),
      backgroundColor: candleColor, borderColor: candleColor, borderWidth: 0,
      minBarLength: 1, barPercentage: 0.8, categoryPercentage: 0.9 },
    { type: 'bar', label: '_wick', order: 2, grouped: false,
      data: prices.map(p => ohlcOk(p) ? [p.low, p.high] : null),
      backgroundColor: candleColor, borderWidth: 0,
      minBarLength: 1, barThickness: 1 },
    { type: 'bar', label: '成交量', order: 3, grouped: false, yAxisID: 'yVol',
      data: prices.map(p => p.volume != null ? Math.round(p.volume / 1000) : null),
      backgroundColor: candleColor.map(c => c + '99'), borderWidth: 0,
      barPercentage: 0.8, categoryPercentage: 0.9 },
  ];

  const closesAll = allPrices.map(p => p.close);
  [[5, '#f472b6'], [20, '#38bdf8'], [60, '#a3e635']].forEach(([period, color]) => {
    datasets.push({
      label: `${period}日線`, data: _movingAverage(closesAll, period).slice(offset),
      borderColor: color, backgroundColor: color, borderWidth: 1.5,
      pointRadius: 0, fill: false, tension: 0, spanGaps: true,
    });
  });

  let trustCrossByDate = null;
  const trustCostAll = (state.instTrades && state.instTrades.length)
    ? _trustCostSeries(allPrices, state.instTrades) : null;
  _renderTrustCostTile(allPrices, trustCostAll);
  if (trustCostAll) {
    const trustCost = trustCostAll.slice(offset);
    // Close crossing the cost line vs. the previous day (both days need a cost value)
    trustCrossByDate = new Map();
    const crossUp = labels.map(() => null), crossDown = labels.map(() => null);
    for (let i = Math.max(offset, 1); i < allPrices.length; i++) {
      const c0 = trustCostAll[i - 1], c1 = trustCostAll[i];
      const p0 = allPrices[i - 1].close, p1 = allPrices[i].close;
      if (c0 == null || c1 == null || p0 == null || p1 == null) continue;
      const k = i - offset;
      if (p0 <= c0 && p1 > c1) { crossUp[k] = c1; trustCrossByDate.set(labels[k], c1); }
      else if (p0 >= c0 && p1 < c1) { crossDown[k] = c1; trustCrossByDate.set(labels[k], c1); }
    }
    datasets.push(
      { label: '投信成本', data: trustCost,
        borderColor: getCssVar('--text'), backgroundColor: getCssVar('--text'),
        borderWidth: 2, borderDash: [8, 3], pointRadius: 0, fill: false, tension: 0, spanGaps: false },
      // Circles (not used by any other overlay); Taiwan colors: red = up, green = down
      { label: '突破投信成本', data: crossUp, showLine: false, fill: false,
        pointStyle: 'circle', pointRadius: 5, backgroundColor: '#ef4444', borderColor: '#ef4444',
        pointBackgroundColor: '#ef4444', pointBorderColor: '#ffffff', pointBorderWidth: 1 },
      { label: '跌破投信成本', data: crossDown, showLine: false, fill: false,
        pointStyle: 'circle', pointRadius: 5, backgroundColor: '#22c55e', borderColor: '#22c55e',
        pointBackgroundColor: '#22c55e', pointBorderColor: '#ffffff', pointBorderWidth: 1 },
    );
    opts.animation = false;  // sparse scatter points get stuck at baseline when animated (see 纏論 note below)
  }

  // Overlay chip-peak reference lines when available — independent of the
  // days-selector's own lookback window (chip peak always uses its own
  // fixed lookback, see CLAUDE.md「籌碼峰」). All three (POC/VAH/VAL) are
  // drawn as stepped lines that move over time (chip_peak.py's
  // compute_chip_peak_series — "chip peak migration"); VAH/VAL are filled
  // between them as a translucent band (fill:'+1' on VAH → next dataset,
  // VAL) so a widening/narrowing value area reads as a shape, not two
  // crossing lines. Falls back to flat lines at today's values if no
  // history was returned.
  const cp = state.chipPeak;
  if (cp) {
    const hasHistory = cp.poc_history && cp.poc_history.length;
    const seriesFor = (field, fallback) => hasHistory
      ? _chipPeakFieldSeries(labels, cp.poc_history, field)
      : labels.map(() => fallback);
    datasets.push(
      { label: `POC ${fmt.price(cp.poc)}`, data: seriesFor('poc', cp.poc),
        borderColor: '#f59e0b', borderWidth: 2, borderDash: [], pointRadius: 0, fill: false,
        tension: 0, stepped: true, spanGaps: false },
      { label: `VAH ${fmt.price(cp.vah)}`, data: seriesFor('vah', cp.vah),
        borderColor: getCssVar('--text2') + 'aa', borderWidth: 1, borderDash: [6, 4], pointRadius: 0,
        tension: 0, stepped: true, spanGaps: false, fill: '+1', backgroundColor: getCssVar('--text2') + '14' },
      { label: `VAL ${fmt.price(cp.val)}`, data: seriesFor('val', cp.val),
        borderColor: getCssVar('--text2') + 'aa', borderWidth: 1, borderDash: [6, 4], pointRadius: 0, fill: false,
        tension: 0, stepped: true, spanGaps: false },
    );
  }

  // Overlay 纏論（筆/中樞/買賣點）——固定用自己的250天分析窗，同樣獨立於
  // days-selector（見上方 chip-peak 的說明）。筆畫成鋸齒線（spanGaps 讓
  // Chart.js 把稀疏的轉折點直接連成直線）；中樞用跟 VAH/VAL 一樣的
  // stepped+fill 手法畫成色塊，中樞之間留 null 讓填色自然斷開，不會連成
  // 一整條；買賣點是稀疏的散點（showLine:false），顏色分買/賣，數字1/2/3
  // 只在 tooltip 裡顯示，圖上不擠文字。
  const cl = state.chanlun;
  let signalByDate = null;
  if (cl && cl.strokes.length) {
    // Chart.js 進場動畫對「showLine:false 的稀疏散點 dataset」（買賣點）有個
    // 踩過才知道的怪異行為：point 元素會整個卡在動畫起始位置（y 軸 baseline），
    // 完全不會過渡到實際數值對應的位置——不管資料/canvas/其他 dataset 設定
    // 怎麼調都一樣，逐一拔掉 chartOptions() 的欄位二分排查，最後鎖定就是
    // animation 本身（不是 interaction mode，一開始誤判過一次）。這張圖表
    // 本來就是資料變動就整個重繪，進場動畫沒有實質意義，直接關掉最單純。
    opts.animation = false;
    const strokePoints = [{ date: cl.strokes[0].start_date, price: cl.strokes[0].start_price },
      ...cl.strokes.map(s => ({ date: s.end_date, price: s.end_price }))];
    signalByDate = new Map(cl.signals.map(s => [s.date, s]));
    datasets.push(
      { label: '筆', data: _chanlunPointSeries(labels, strokePoints),
        borderColor: '#a78bfa', borderWidth: 1.5, pointRadius: 0, fill: false,
        tension: 0, spanGaps: true },
      { label: '中樞上緣', data: _chanlunCenterSeries(labels, cl.centers, 'zg'),
        borderColor: 'transparent', pointRadius: 0, stepped: true, spanGaps: false,
        fill: '+1', backgroundColor: getCssVar('--text2') + '22' },
      { label: '中樞下緣', data: _chanlunCenterSeries(labels, cl.centers, 'zd'),
        borderColor: 'transparent', pointRadius: 0, stepped: true, spanGaps: false, fill: false },
      { label: '買點', data: _chanlunSignalSeries(labels, cl.signals, true),
        showLine: false, fill: false, pointStyle: 'triangle', rotation: 0, pointRadius: 6,
        pointBackgroundColor: '#22c55e', borderColor: '#22c55e' },
      { label: '賣點', data: _chanlunSignalSeries(labels, cl.signals, false),
        showLine: false, fill: false, pointStyle: 'triangle', rotation: 180, pointRadius: 6,
        pointBackgroundColor: '#ef4444', borderColor: '#ef4444' },
    );
  }

  // Overlay 纏論買點+營收飆股 歷史進出場（🎯 卡片「開始回測」後才有資料，
  // 見 runStockChanlunStarBacktest()，2026-08-30 使用者要求「在圖表中顯示
  // 出來，包含過去的歷史記錄」新增）。跟上面原始纏論買/賣點三角形共用同一
  // 張圖，但故意用不同形狀+顏色（星形、藍/橘）區分，因為策略進場點本來就是
  // 纏論買點的子集（多了營收飆股條件），同一天很可能兩個 dataset 疊在同一個
  //位置——形狀不同才分得出「這是原始纏論訊號」還是「這是通過完整策略條件
  // 的進場點」。出場點只畫「真的觸發停損/停利」的那些，「持有中」的部位
  // 沒有真正出場事件，不畫在圖上（用最後一天股價算的未實現報酬只存在於
  // 下面的表格，不是圖表上的一個歷史事件）。
  const cst = state.chanlunStarTrades;
  let entryByDate = null, exitByDate = null;
  if (cst && cst.length) {
    opts.animation = false;
    entryByDate = new Map(cst.map(t => [t.entry_date, t]));
    exitByDate = new Map(cst.filter(t => t.exit_date).map(t => [t.exit_date, t]));
    datasets.push(
      { label: '策略進場', data: _chanlunStarEntrySeries(labels, cst),
        showLine: false, fill: false, pointStyle: 'star', pointRadius: 9,
        pointBackgroundColor: '#38bdf8', borderColor: '#38bdf8' },
      { label: '策略出場', data: _chanlunStarExitSeries(labels, cst),
        showLine: false, fill: false, pointStyle: 'star', pointRadius: 9,
        pointBackgroundColor: '#fb923c', borderColor: '#fb923c' },
    );
  }

  // Overlay 888標準3+股魚 歷史進出場（📐 卡片「開始回測」後才有資料，見
  // runStockFlag888GuyuBacktest()）。跟上面兩組疊圖共用同一張圖，故意用
  // 第三種形狀+顏色（菱形、黃/粉）區分，避免三組訊號視覺上混在一起分不清
  // 是哪一套規則的標記。
  const fgt = state.flag888GuyuTrades;
  let fgEntryByDate = null, fgExitByDate = null;
  if (fgt && fgt.length) {
    opts.animation = false;
    fgEntryByDate = new Map(fgt.map(t => [t.entry_date, t]));
    fgExitByDate = new Map(fgt.filter(t => t.exit_date).map(t => [t.exit_date, t]));
    datasets.push(
      { label: '組合進場', data: _flag888GuyuEntrySeries(labels, fgt),
        showLine: false, fill: false, pointStyle: 'rectRot', pointRadius: 8,
        pointBackgroundColor: '#facc15', borderColor: '#facc15' },
      { label: '組合出場', data: _flag888GuyuExitSeries(labels, fgt),
        showLine: false, fill: false, pointStyle: 'rectRot', pointRadius: 8,
        pointBackgroundColor: '#f472b6', borderColor: '#f472b6' },
    );
  }

  opts.plugins.legend.labels.filter = item => item.text !== '_wick';
  opts.plugins.tooltip.filter = item => item.dataset.label !== '_wick';
  opts.plugins.tooltip.callbacks = {
    label(ctx) {
      const label = ctx.dataset.label;
      if (label === 'K線') {
        const p = prices[ctx.dataIndex];
        return `開 ${fmt.price(p.open)}  高 ${fmt.price(p.high)}  低 ${fmt.price(p.low)}  收 ${fmt.price(p.close)}`;
      }
      if (signalByDate && (label === '買點' || label === '賣點')) {
        const s = signalByDate.get(labels[ctx.dataIndex]);
        return s ? `${_CHANLUN_SIGNAL_LABELS[s.type]} ${fmt.price(s.price)}` : label;
      }
      if (entryByDate && label === '策略進場') {
        const t = entryByDate.get(labels[ctx.dataIndex]);
        return t ? `進場 ${fmt.price(t.entry_price)}` : label;
      }
      if (exitByDate && label === '策略出場') {
        const t = exitByDate.get(labels[ctx.dataIndex]);
        return t
          ? `出場 ${fmt.price(t.exit_price)}（${_exitReasonLabel(t.exit_reason)}，${t.return_pct > 0 ? '+' : ''}${t.return_pct}%）`
          : label;
      }
      if (fgEntryByDate && label === '組合進場') {
        const t = fgEntryByDate.get(labels[ctx.dataIndex]);
        return t ? `進場 ${fmt.price(t.entry_price)}` : label;
      }
      if (fgExitByDate && label === '組合出場') {
        const t = fgExitByDate.get(labels[ctx.dataIndex]);
        return t
          ? `出場 ${fmt.price(t.exit_price)}（${_exitReasonLabel(t.exit_reason)}，${t.return_pct > 0 ? '+' : ''}${t.return_pct}%）`
          : label;
      }
      if (trustCrossByDate && (label === '突破投信成本' || label === '跌破投信成本')) {
        return `${label} ${fmt.price(trustCrossByDate.get(labels[ctx.dataIndex]))}`;
      }
      if (label === '成交量') return `成交量: ${ctx.raw.toLocaleString()} 張`;
      return `${label}: ${ctx.formattedValue}`;
    },
  };

  // Datasets without an explicit axis would bind to the first y scale (yVol)
  datasets.forEach(d => { d.yAxisID ??= 'y'; });

  // Feature groups replace the built-in per-dataset legend (see _PRICE_CHART_GROUPS)
  const hiddenGroups = _priceChartHiddenGroups();
  datasets.forEach(d => {
    d.group = _priceChartGroupOf(d.label);
    d.hidden = hiddenGroups.has(d.group);
  });
  opts.plugins.legend.display = false;

  state.priceChart = new Chart(canvas, {
    type: 'line',
    data: { labels, datasets },
    options: opts,
  });
  _renderPriceChartGroups(canvas, datasets, hiddenGroups);
}

/* ── Price chart feature groups ──
   The chart overlays several independent features (K線, 成交量, 均線, 投信成本,
   籌碼峰, 纏論, 回測進出場); instead of Chart.js's one-legend-item-per-dataset
   (15+ items), each feature is one toggle chip that shows/hides all of its
   datasets. Hidden groups persist per viewer in localStorage. Datasets are
   mapped to groups by label, so a new overlay must be added to
   _priceChartGroupOf() or it lands in 'other'. */
const _PRICE_CHART_GROUPS = [
  { key: 'kline',    name: 'K線',      colors: ['#ef4444', '#22c55e'] },
  { key: 'volume',   name: '成交量',   colors: ['#ef444499', '#22c55e99'] },
  { key: 'ma',       name: '均線',     colors: ['#f472b6', '#38bdf8', '#a3e635'] },
  { key: 'trust',    name: '投信成本', colors: ['--text', '#ef4444', '#22c55e'] },
  { key: 'chip',     name: '籌碼峰',   colors: ['#f59e0b', '--text2'] },
  { key: 'chanlun',  name: '纏論',     colors: ['#a78bfa', '#22c55e', '#ef4444'] },
  { key: 'backtest', name: '回測進出場', colors: ['#38bdf8', '#fb923c', '#facc15', '#f472b6'] },
  { key: 'other',    name: '其他',     colors: ['--text2'] },
];
const _PRICE_CHART_HIDDEN_KEY = 'price_chart_hidden_groups';

function _priceChartGroupOf(label) {
  if (label === 'K線' || label === '_wick') return 'kline';
  if (label === '成交量') return 'volume';
  if (/^\d+日線$/.test(label)) return 'ma';
  if (label.includes('投信成本')) return 'trust';
  if (/^(POC|VAH|VAL) /.test(label)) return 'chip';
  if (['筆', '中樞上緣', '中樞下緣', '買點', '賣點'].includes(label)) return 'chanlun';
  if (['策略進場', '策略出場', '組合進場', '組合出場'].includes(label)) return 'backtest';
  return 'other';
}

function _priceChartHiddenGroups() {
  try { return new Set(JSON.parse(localStorage.getItem(_PRICE_CHART_HIDDEN_KEY)) || []); }
  catch (_) { return new Set(); }
}

function _renderPriceChartGroups(canvas, datasets, hiddenGroups) {
  const wrap = canvas.parentElement;
  let bar = document.getElementById('price-chart-groups');
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'price-chart-groups';
    bar.className = 'chart-group-bar';
    wrap.insertAdjacentElement('beforebegin', bar);
    bar.addEventListener('click', e => {
      const btn = e.target.closest('.chart-group-btn');
      if (!btn || !state.priceChart) return;
      const key = btn.dataset.group;
      const hidden = _priceChartHiddenGroups();
      hidden.has(key) ? hidden.delete(key) : hidden.add(key);
      try { localStorage.setItem(_PRICE_CHART_HIDDEN_KEY, JSON.stringify([...hidden])); } catch (_) {}
      const visible = !hidden.has(key);
      state.priceChart.data.datasets.forEach((d, i) => {
        if (d.group === key) state.priceChart.setDatasetVisibility(i, visible);
      });
      state.priceChart.update();
      btn.classList.toggle('off', !visible);
    });
  }
  const present = new Set(datasets.map(d => d.group));
  const color = c => c.startsWith('--') ? getCssVar(c) : c;
  bar.innerHTML = _PRICE_CHART_GROUPS.filter(g => present.has(g.key)).map(g => `
    <button type="button" class="chart-group-btn${hiddenGroups.has(g.key) ? ' off' : ''}" data-group="${g.key}">
      <span class="chart-group-swatch">${g.colors.map(c => `<i style="background:${color(c)}"></i>`).join('')}</span>${g.name}
    </button>`).join('');
}

/* ── Price table ── */
function renderPriceTable(prices) {
  if (state.priceDt) { state.priceDt.destroy(); state.priceDt = null; }
  const rows = [...prices].reverse().map(p => [
    p.date,
    fmt.price(p.open),
    fmt.price(p.high),
    fmt.price(p.low),
    fmt.price(p.close),
    p.change != null
      ? `<span class="${pctClass(p.change)}">${p.change > 0 ? '+' : ''}${fmt.price(p.change)}</span>`
      : '—',
    p.change_pct != null
      ? `<span class="${pctClass(p.change_pct)}">${fmt.pct(p.change_pct)}</span>`
      : '—',
    p.volume != null ? Number(p.volume).toLocaleString() : '—',
  ]);
  state.priceDt = $('#price-table').DataTable({
    data: rows, pageLength: 10, order: [],
    language: dtLang(), destroy: true, scrollX: true,
  });
}

/* ── Revenue chart ── */
function renderRevenueChart(revenues) {
  const canvas = document.getElementById('revenue-chart');
  if (state.revenueChart) { state.revenueChart.destroy(); state.revenueChart = null; }
  if (!revenues.length) return;

  const sorted = [...revenues].reverse();
  const n      = sorted.length;
  const labels  = sorted.map(r => `${r.year}/${String(r.month).padStart(2, '0')}`);
  const values  = sorted.map(r => r.revenue);
  const yoys    = sorted.map(r => r.revenue_yoy);
  const maxTicks = n > 60 ? 12 : n > 24 ? 18 : n;

  state.revenueChart = new Chart(canvas, {
    data: {
      labels,
      datasets: [
        {
          type: 'bar',
          label: '月營收(千元)',
          data:  values,
          backgroundColor: getCssVar('--primary') + '99',
          borderColor:     getCssVar('--primary'),
          borderWidth: 1,
          yAxisID: 'y',
        },
        {
          type: 'line',
          label: '年增率%',
          data:   yoys,
          borderColor:  getCssVar('--pos'),
          borderWidth:  2,
          pointRadius:  n > 60 ? 0 : 3,
          tension:      0.3,
          yAxisID: 'y2',
        },
      ],
    },
    options: {
      ...chartOptions(),
      scales: {
        y:  { position: 'left',  grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
        y2: { position: 'right', grid: { drawOnChartArea: false },        ticks: { color: getCssVar('--pos'), callback: v => v + '%' } },
        x:  { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), maxRotation: 45, maxTicksLimit: maxTicks } },
      },
    },
  });
}

/* ── Revenue table ── */
function renderRevenueTable(revenues) {
  if (state.revenueDt) { state.revenueDt.destroy(); state.revenueDt = null; }
  const rows = revenues.map(r => [
    r.year, r.month, fmt.rev(r.revenue),
    r.revenue_mom != null
      ? `<span class="${pctClass(r.revenue_mom)}">${fmt.pct(r.revenue_mom)}</span>` : '—',
    r.revenue_yoy != null
      ? `<span class="${pctClass(r.revenue_yoy)}">${fmt.pct(r.revenue_yoy)}</span>` : '—',
  ]);
  state.revenueDt = $('#revenue-table').DataTable({
    data: rows, pageLength: 10, order: [],
    language: dtLang(), destroy: true, scrollX: true,
  });
}

/* ── EPS chart ── */
function renderEpsChart(financials) {
  const canvas = document.getElementById('eps-chart');
  if (state.epsChart) { state.epsChart.destroy(); state.epsChart = null; }
  if (!financials.length) return;

  const sorted = [...financials].reverse();
  const n      = sorted.length;
  const labels = sorted.map(f => `${f.year}/Q${f.quarter}`);
  const eps    = sorted.map(f => f.eps);
  const bgColors  = eps.map(v => v != null && v >= 0 ? getCssVar('--pos') + 'bb' : getCssVar('--neg') + 'bb');
  const bdrColors = eps.map(v => v != null && v >= 0 ? getCssVar('--pos') : getCssVar('--neg'));
  const maxTicks  = n > 30 ? 12 : n > 16 ? 16 : n;

  state.epsChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [{
        label: 'EPS (元)',
        data:            eps,
        backgroundColor: bgColors,
        borderColor:     bdrColors,
        borderWidth:     1,
      }],
    },
    options: {
      ...chartOptions('EPS (元)'),
      scales: {
        x: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), maxRotation: 45, maxTicksLimit: maxTicks } },
        y: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') }, title: { display: true, text: 'EPS (元)', color: getCssVar('--text2') } },
      },
    },
  });
}

/* ── Quarterly table ── */
function renderQuarterlyTable(financials) {
  if (state.quarterlyDt) { state.quarterlyDt.destroy(); state.quarterlyDt = null; }
  const rows = financials.map(f => [
    f.year, `Q${f.quarter}`,
    f.revenue  != null ? fmt.rev(f.revenue)  : '—',
    f.operating_income != null ? fmt.rev(f.operating_income) : '—',
    f.net_income != null ? fmt.rev(f.net_income) : '—',
    f.eps != null
      ? `<span class="${pctClass(f.eps)}">${fmt.eps(f.eps)}</span>` : '—',
  ]);
  state.quarterlyDt = $('#quarterly-table').DataTable({
    data: rows, pageLength: 10, order: [],
    language: dtLang(), destroy: true, scrollX: true,
  });
}

/* ── Fundamentals panel (達人選股用進階財報指標) ── */
function toggleFundamentals(checked) {
  state.showFundamentals = checked;
  document.getElementById('fund-body').classList.toggle('hidden', !checked);
  if (checked) renderFundCharts(state.fundamentals);
}

function renderFundamentalsPanel(data) {
  state.fundamentals = data;
  document.getElementById('fund-toggle-chk').checked = state.showFundamentals;
  document.getElementById('fund-body').classList.toggle('hidden', !state.showFundamentals);
  renderFundStats(data ? data.snapshot : null);
  if (state.showFundamentals) renderFundCharts(data);
}

function renderFundStats(snap) {
  const wrap = document.getElementById('fund-stats');
  if (!snap) { wrap.innerHTML = ''; return; }
  const tiles = [
    ['本益比',        snap.per                  != null ? snap.per.toFixed(2)                  : '—'],
    ['股價淨值比',    snap.pbr                  != null ? snap.pbr.toFixed(2)                  : '—'],
    ['殖利率',        snap.dividend_yield       != null ? `${snap.dividend_yield.toFixed(2)}%`  : '—'],
    ['董監持股比例',  snap.director_holding_pct != null ? `${snap.director_holding_pct.toFixed(2)}%` : '—'],
    ['近5年填息機率', snap.fill_rate_5y         != null ? `${snap.fill_rate_5y.toFixed(1)}%`     : '—'],
  ];
  wrap.innerHTML = tiles.map(([label, value]) => `
    <div class="fund-stat-tile">
      <div class="fund-stat-label">${label}</div>
      <div class="fund-stat-value">${value}</div>
    </div>
  `).join('');
}

function renderFundCharts(data) {
  const quarterly = (data && data.quarterly) || [];
  const dividends = (data && data.dividends) || [];
  renderFundProfitChart(quarterly);
  renderFundHealthChart(quarterly);
  renderFundTurnoverChart(quarterly);
  renderFundDividendChart(dividends);
  renderFundDividendTable(dividends);
}

function _fundGroupedBarOptions(n) {
  const maxTicks = n > 30 ? 12 : n > 16 ? 16 : n;
  return {
    ...chartOptions('%'),
    scales: {
      x: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), maxRotation: 45, maxTicksLimit: maxTicks } },
      y: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
    },
  };
}

function renderFundProfitChart(quarterly) {
  const canvas = document.getElementById('fund-profit-chart');
  if (state.fundProfitChart) { state.fundProfitChart.destroy(); state.fundProfitChart = null; }
  if (!quarterly.length) return;
  const sorted = [...quarterly].reverse();
  const labels = sorted.map(q => `${q.year}/Q${q.quarter}`);
  const mk = field => sorted.map(q => q[field] != null ? Number(q[field].toFixed(2)) : null);

  state.fundProfitChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: '毛利率%',     data: mk('gross_margin'),     backgroundColor: getCssVar('--primary') + 'bb' },
        { label: '營業利益率%', data: mk('operating_margin'), backgroundColor: getCssVar('--pos') + 'bb' },
        { label: 'ROE%',        data: mk('roe'),              backgroundColor: getCssVar('--neg') + 'bb' },
        { label: 'ROA%',        data: mk('roa'),              backgroundColor: getCssVar('--text2') + 'bb' },
      ],
    },
    options: _fundGroupedBarOptions(sorted.length),
  });
}

function renderFundHealthChart(quarterly) {
  const canvas = document.getElementById('fund-health-chart');
  if (state.fundHealthChart) { state.fundHealthChart.destroy(); state.fundHealthChart = null; }
  if (!quarterly.length) return;
  const sorted = [...quarterly].reverse();
  const labels = sorted.map(q => `${q.year}/Q${q.quarter}`);
  const mk = field => sorted.map(q => q[field] != null ? Number(q[field].toFixed(2)) : null);

  state.fundHealthChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: '流動比率%', data: mk('current_ratio'), backgroundColor: getCssVar('--primary') + 'bb' },
        { label: '速動比率%', data: mk('quick_ratio'),   backgroundColor: getCssVar('--pos') + 'bb' },
        { label: '負債比率%', data: mk('debt_ratio'),    backgroundColor: getCssVar('--neg') + 'bb' },
      ],
    },
    options: _fundGroupedBarOptions(sorted.length),
  });
}

function renderFundTurnoverChart(quarterly) {
  const canvas = document.getElementById('fund-turnover-chart');
  if (state.fundTurnoverChart) { state.fundTurnoverChart.destroy(); state.fundTurnoverChart = null; }
  if (!quarterly.length) return;
  const sorted = [...quarterly].reverse();
  const labels = sorted.map(q => `${q.year}/Q${q.quarter}`);
  const mk = field => sorted.map(q => q[field] != null ? Number(q[field].toFixed(1)) : null);

  state.fundTurnoverChart = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: '存貨週轉天數',     data: mk('inventory_turnover_days'), backgroundColor: getCssVar('--primary') + 'bb' },
        { label: '應收帳款週轉天數', data: mk('ar_turnover_days'),        backgroundColor: getCssVar('--pos') + 'bb' },
      ],
    },
    options: {
      ...chartOptions('天'),
      scales: {
        x: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), maxRotation: 45, maxTicksLimit: sorted.length > 30 ? 12 : sorted.length > 16 ? 16 : sorted.length } },
        y: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
      },
    },
  });
}

function renderFundDividendChart(dividends) {
  const canvas = document.getElementById('fund-dividend-chart');
  if (state.fundDividendChart) { state.fundDividendChart.destroy(); state.fundDividendChart = null; }
  if (!dividends.length) return;
  const sorted = [...dividends].reverse();
  const labels = sorted.map(d => d.fiscal_year);

  state.fundDividendChart = new Chart(canvas, {
    data: {
      labels,
      datasets: [
        {
          type: 'bar', label: '現金股利', stack: 'div', yAxisID: 'y',
          data: sorted.map(d => d.cash_dividend),
          backgroundColor: getCssVar('--primary') + '99', borderColor: getCssVar('--primary'), borderWidth: 1,
        },
        {
          type: 'bar', label: '股票股利', stack: 'div', yAxisID: 'y',
          data: sorted.map(d => d.stock_dividend),
          backgroundColor: getCssVar('--pos') + '99', borderColor: getCssVar('--pos'), borderWidth: 1,
        },
        {
          type: 'line', label: '配發率%', yAxisID: 'y2',
          data: sorted.map(d => d.payout_ratio),
          borderColor: getCssVar('--neg'), borderWidth: 2, pointRadius: 3, tension: 0.3,
        },
        {
          type: 'line', label: '殖利率%', yAxisID: 'y2',
          data: sorted.map(d => d.dividend_yield),
          borderColor: getCssVar('--text2'), borderWidth: 2, pointRadius: 3, tension: 0.3,
        },
      ],
    },
    options: {
      ...chartOptions(),
      scales: {
        y:  { position: 'left',  stacked: true, grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
        y2: { position: 'right', grid: { drawOnChartArea: false },        ticks: { color: getCssVar('--neg'), callback: v => v + '%' } },
        x:  { stacked: true, grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
      },
    },
  });
}

function renderFundDividendTable(dividends) {
  if (state.fundDividendDt) { state.fundDividendDt.destroy(); state.fundDividendDt = null; }
  const rows = dividends.map(d => [
    d.fiscal_year,
    d.cash_dividend  != null ? d.cash_dividend.toFixed(2)  : '—',
    d.stock_dividend != null ? d.stock_dividend.toFixed(2) : '—',
    d.total          != null ? d.total.toFixed(2)          : '—',
    d.payout_ratio   != null ? fmt.pct(d.payout_ratio)     : '—',
    d.dividend_yield != null ? fmt.pct(d.dividend_yield)   : '—',
  ]);
  state.fundDividendDt = $('#fund-dividend-table').DataTable({
    data: rows, pageLength: 10, order: [],
    language: dtLang(), destroy: true, scrollX: true,
  });
}

/* ── Chart options ── */
function chartOptions(yLabel = '') {
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { labels: { color: getCssVar('--text2'), boxWidth: 12 } },
      tooltip: { backgroundColor: getCssVar('--bg3'), titleColor: getCssVar('--text'), bodyColor: getCssVar('--text2'), borderColor: getCssVar('--border'), borderWidth: 1 },
    },
    scales: {
      x: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), maxRotation: 30 } },
      y: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') }, title: { display: !!yLabel, text: yLabel, color: getCssVar('--text2') } },
    },
  };
}

function redrawCharts() {
  if (state.currentCode) {
    loadStockDetail(state.currentCode);
  }
}

/* ── Page tabs ── */
document.querySelectorAll('.page-tab').forEach(btn => {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.page-tab').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    state.activeTab = this.dataset.tab;
    document.getElementById('list-view').classList.toggle('active', state.activeTab === 'list');
    document.getElementById('star-view').classList.toggle('active', state.activeTab === 'star');
    document.getElementById('watchlist-view').classList.toggle('active', state.activeTab === 'watchlist');
    document.getElementById('ann-view').classList.toggle('active', state.activeTab === 'ann');
    document.getElementById('expert-view').classList.toggle('active', state.activeTab === 'expert');
    document.getElementById('macro-view').classList.toggle('active', state.activeTab === 'macro');
    document.getElementById('taifex-view').classList.toggle('active', state.activeTab === 'taifex');
    if (state.activeTab === 'star') renderStarTable();
    if (state.activeTab === 'watchlist') renderWatchlistView();
    if (state.activeTab === 'ann') loadAnnouncements();
    if (state.activeTab === 'expert') loadExperts();
    if (state.activeTab === 'taifex') loadTaifexView();
  });
});

/* ── 營收飆股 ── */
let starDt = null;
let starMarket = 'all';
let starLatestMonthOnly = false;

document.querySelectorAll('[data-star-market]').forEach(btn => {
  btn.addEventListener('click', function() {
    document.querySelectorAll('[data-star-market]').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    starMarket = this.dataset.starMarket;
    renderStarTable();
  });
});

document.getElementById('star-latest-month').addEventListener('change', function() {
  starLatestMonthOnly = this.checked;
  renderStarTable();
});

/* 纏論買點訊號快取：code -> '一買'/'二買'/'三買'，只收 passed=true 的列——
   飆股清單／自選股資料源（state.allData，/api/market/summary）不含纏論欄位，
   需另外呼叫達人選股共用的 /api/experts/chanlun_buy 抓一次快取起來，兩個表格共用。 */
let _starChanlunMap = null;
async function _loadStarChanlunMap() {
  if (_starChanlunMap) return;
  _starChanlunMap = {};
  try {
    const rows = await fetch('/api/experts/chanlun_buy').then(r => r.json());
    rows.filter(r => r.passed).forEach(r => { _starChanlunMap[r.code] = _chanlunSignalLabel(r); });
  } catch (_) { /* keep empty map, column falls back to — */ }
  renderStarTable();
  if (wlDt) renderWlTable();
}
const _STAR_CHANLUN_RANK = { '一買': 1, '二買': 2, '三買': 3 };
function _starChanlunCell(code) {
  const label = _starChanlunMap && _starChanlunMap[code];
  return label ? [_STAR_CHANLUN_RANK[label] || 0, label] : [0, '—'];
}

function calcEst(s) {
  if (s.revenue == null || s.qf_revenue == null || s.qf_revenue <= 0 || s.eps == null || s.eps <= 0) return null;
  return (s.revenue / s.qf_revenue) * s.eps * 240;
}

function _getStarBase() {
  const src = starMarket === 'all' ? state.allData : state.allData.filter(s => s.market === starMarket);
  return src
    .map(s => ({ ...s, _est: calcEst(s), _ratio: s.close ? calcEst(s) / s.close : null }))
    .filter(s => s._ratio != null && s._ratio >= 1.5 && s.revenue_yoy != null && s.revenue_yoy >= 20)
    .sort((a, b) => b._ratio - a._ratio);
}

function getStarFiltered() {
  const all = _getStarBase();
  if (!starLatestMonthOnly) return all;
  const maxYm = all.reduce((mx, s) => {
    const ym = s.rev_year && s.rev_month ? s.rev_year * 100 + s.rev_month : 0;
    return Math.max(mx, ym);
  }, 0);
  return maxYm ? all.filter(s => !s.rev_year || !s.rev_month || s.rev_year * 100 + s.rev_month === maxYm) : all;
}

function downloadStarCsv() {
  const rows = getStarFiltered();
  const headers = ['代號','名稱','產業','起始股價','收盤價','價差%','漲跌幅%','營收預估股價','預估倍數',
                   '營收月份','月營收(千元)','月營收年增%','最新EPS','本益比'];
  const lines = [headers.join(',')];
  rows.forEach(s => {
    const revMonth = (s.rev_year && s.rev_month) ? `${s.rev_year}/${String(s.rev_month).padStart(2,'0')}` : '';
    lines.push([
      s.code, s.name, s.industry || '',
      s.start_price ?? '', s.close ?? '', s.price_diff ?? '', s.change_pct ?? '',
      s._est != null ? s._est.toFixed(2) : '',
      s._ratio != null ? s._ratio.toFixed(2) : '',
      revMonth,
      s.revenue ?? '', s.revenue_yoy ?? '',
      s.eps ?? '', s.pe_ratio ?? '',
    ].join(','));
  });
  const bom = '﻿';
  const blob = new Blob([bom + lines.join('\n')], { type: 'text/csv;charset=utf-8;' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'revenue_stars.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

function copyStarForAI() {
  const rows = getStarFiltered();
  if (!rows.length) { showToast('目前沒有飆股資料'); return; }

  const header = '代號 | 名稱 | 月營收年增%';
  const lines = rows.map(s => [
    s.code,
    s.name,
    s.revenue_yoy != null ? s.revenue_yoy.toFixed(1) + '%' : '—',
  ].join(' | '));

  const prompt = `你現在是一位資深的台股操盤手。我提供你一份股票清單，請幫我剔除「目前沒有市場話題、缺乏題材性、處於夕陽產業或冷門、沒有想像空間、股價在100元以上」的股票。

請根據以下「請以 2026 年當前最熱門的市場主線為準」來保留股票：

科技與未來趨勢（如：AI/伺服器、半導體高階製程、機器人、低軌衛星、WiFi 7）
政策與綠能（如：重電、生技、潔淨能源、碳權）
週期與消費受惠（如：降息受惠、奧運概念、記憶體復甦、網通、摺疊機）
其他近期在財經新聞上能見度高、有實質題材支撐的個股。

輸出格式要求：
【剔除清單】：請列出被剔除的股票，並簡述剔除原因（例如：傳統紡織無亮點、冷門傳產、缺乏催化劑）。
【保留清單】：請列出保留的股票，並註明它屬於什麼「熱門題材」以及「未來可能的催化劑（Catalyst）」。

---股票清單---
${header}
${lines.join('\n')}`;

  navigator.clipboard.writeText(prompt)
    .then(() => showToast(`已複製 ${rows.length} 支飆股，貼到 AI 即可分析`))
    .catch(() => showToast('複製失敗，請手動複製'));
}

function copyWlForAI() {
  const wl = wlActive();
  if (!wl || !wl.codes.length) { showToast('自選股清單是空的'); return; }

  const stocks = wl.codes
    .map(code => state.allData.find(d => d.code === code))
    .filter(Boolean);
  if (!stocks.length) { showToast('找不到股票資料，請先載入'); return; }

  const header = '代號 | 名稱 | 月營收年增% | 預估倍數';
  const lines = stocks.map(s => {
    const ratio = calcEst(s) && s.close ? (calcEst(s) / s.close).toFixed(2) + 'x' : '—';
    return [
      s.code,
      s.name,
      s.revenue_yoy != null ? s.revenue_yoy.toFixed(1) + '%' : '—',
      ratio,
    ].join(' | ');
  });

  const prompt = `你現在是一位資深的台股操盤手。我提供你一份自選股清單，請幫我分析每一支股票的現況與題材，並給出操作建議。

請根據「2026 年當前最熱門的市場主線」評估：

科技與未來趨勢（如：AI/伺服器、半導體高階製程、機器人、低軌衛星、WiFi 7）
政策與綠能（如：重電、生技、潔淨能源、碳權）
週期與消費受惠（如：降息受惠、奧運概念、記憶體復甦、網通、摺疊機）

輸出格式要求：
【留意】：近期有題材、值得追蹤，並說明催化劑。
【觀望】：短期無明顯催化劑，但基本面尚可。
【風險】：題材退潮、基本面轉弱或估值過高，說明理由。

---自選股清單---
${header}
${lines.join('\n')}`;

  navigator.clipboard.writeText(prompt)
    .then(() => showToast(`已複製 ${stocks.length} 支自選股，貼到 AI 即可分析`))
    .catch(() => showToast('複製失敗，請手動複製'));
}

function renderStarTable() {
  if (_starChanlunMap === null) _loadStarChanlunMap();
  const all = _getStarBase();

  // Show month-filter checkbox only when multiple revenue months exist in the data
  const yms = [...new Set(all.filter(s => s.rev_year && s.rev_month).map(s => s.rev_year * 100 + s.rev_month))];
  const hasMultiple = yms.length > 1;
  const maxYm = yms.length ? Math.max(...yms) : 0;
  document.getElementById('star-month-filter-wrap').classList.toggle('hidden', !hasMultiple);
  if (hasMultiple && maxYm) {
    document.getElementById('star-latest-month-label').textContent =
      `只顯示最新月份（${Math.floor(maxYm / 100)}/${String(maxYm % 100).padStart(2, '0')}）`;
  }

  const filtered = (starLatestMonthOnly && hasMultiple && maxYm)
    ? all.filter(s => !s.rev_year || !s.rev_month || s.rev_year * 100 + s.rev_month === maxYm)
    : all;

  document.getElementById('star-count').textContent = `共 ${filtered.length} 支`;

  const rows = filtered.map(s => {
    const est  = s._est;
    const fill = 'display:block;margin:-9px -12px;padding:9px 12px;font-weight:600;';
    const estCell = est >= s.close * 2
      ? `<span style="${fill}background:#ef4444;color:#fff">${fmt.price(est)}</span>`
      : `<span style="${fill}background:#eab308;color:#000">${fmt.price(est)}</span>`;

    return [
      `<span class="stock-link" data-code="${s.code}">${s.code}</span>`,
      `<span class="stock-link" data-code="${s.code}">${s.name}</span>`,
      s.industry || '—',
      s.start_price != null ? fmt.price(s.start_price) : '—',
      fmt.price(s.close),
      s.price_diff != null ? `<span class="${pctClass(s.price_diff)}">${fmt.pct(s.price_diff)}</span>` : '—',
      s.change_pct != null ? `<span class="${pctClass(s.change_pct)}">${fmt.pct(s.change_pct)}</span>` : '—',
      estCell,
      s._ratio.toFixed(2) + 'x',
      (s.rev_year && s.rev_month) ? `${s.rev_year}/${String(s.rev_month).padStart(2,'0')}` : '—',
      s.revenue != null ? fmt.rev(s.revenue) : '—',
      s.revenue_yoy != null ? `<span class="${pctClass(s.revenue_yoy)}">${fmt.pct(s.revenue_yoy)}</span>` : '—',
      s.eps != null ? `<span class="${pctClass(s.eps)}">${fmt.eps(s.eps)}</span>` : '—',
      s.pe_ratio != null ? Number(s.pe_ratio).toFixed(1) + 'x' : '—',
      _starChanlunCell(s.code),
      sweetSpotCell(s),
      turnaroundCell(s),
    ];
  });

  if (starDt) {
    starDt.clear().rows.add(rows).draw();
  } else {
    starDt = $('#star-table').DataTable({
      data: rows,
      deferRender: true,
      pageLength: 25,
      order: [[7, 'desc']],
      language: dtLang(),
      scrollX: true,
      columnDefs: [
        { targets: [3, 4, 5, 6, 7, 8, 10, 11, 12, 13], className: 'dt-right', type: 'num-cell' },
        { targets: [0, 1, 2, 9], className: 'dt-left' },
        { targets: 14, className: 'dt-center', render: { _: 0, display: 1 } },
        { targets: 15, className: 'dt-right', render: { _: 0, display: 1 } },
        { targets: 16, className: 'dt-left', width: '64px' },
      ],
    });
    $('#star-table tbody').on('click', 'td', function() {
      const code = $(this).find('[data-code]').data('code') ||
                   $(this).closest('tr').find('[data-code]').data('code');
      if (code) {
        setDetailNavContext(_dtOrderedCodes(starDt, 0), String(code));
        loadStockDetail(code);
      }
    });
  }
}

/* ── View switching ── */
function showDetailView() {
  document.getElementById('list-view').classList.remove('active');
  document.getElementById('star-view').classList.remove('active');
  document.getElementById('watchlist-view').classList.remove('active');
  document.getElementById('ann-view').classList.remove('active');
  document.getElementById('expert-view').classList.remove('active');
  document.getElementById('taifex-view').classList.remove('active');
  document.getElementById('detail-view').classList.add('active');
  document.getElementById('page-tabs-bar').classList.add('hidden');
  window.scrollTo(0, 0);
}

function showListView() {
  const returningCode = state.currentCode;
  document.getElementById('detail-view').classList.remove('active');
  document.getElementById('page-tabs-bar').classList.remove('hidden');
  const viewMap = { star: 'star-view', watchlist: 'watchlist-view', ann: 'ann-view', expert: 'expert-view', macro: 'macro-view', taifex: 'taifex-view' };
  document.getElementById(viewMap[state.activeTab] || 'list-view').classList.add('active');
  state.currentCode = null;
  if (returningCode) requestAnimationFrame(() => _scrollToStockRow(returningCode));
}

// DataTables-backed views (list/star/watchlist) only keep the *current
// page's* rows in the DOM, so the stock we're returning to may not be
// there yet — flip to whichever page it's actually on (recomputed from the
// table's live filter+sort order, not the possibly-unrelated
// detailNavList/-Index some other entry point set) before scrolling.
// ann-view/expert-view render every row unconditionally, so the direct
// lookup already succeeds and this never reaches the DataTables branch.
const _VIEW_DT_INFO = {
  list:      () => ({ dt: mainDt, col: 0 }),
  star:      () => ({ dt: starDt, col: 0 }),
  watchlist: () => ({ dt: wlDt,   col: 1 }),
};

function _scrollToStockRow(code) {
  const tryScroll = () => {
    const el = document.querySelector(`[data-code="${code}"]`);
    if (!el) return false;
    const row = el.closest('tr') || el;
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.add('row-flash');
    setTimeout(() => row.classList.remove('row-flash'), 1600);
    return true;
  };
  if (tryScroll()) return;

  const info = _VIEW_DT_INFO[state.activeTab];
  const dt = info && info().dt;
  if (!dt) return;
  const { col } = info();
  const idx = _dtOrderedCodes(dt, col).indexOf(String(code));
  if (idx === -1) return;
  const pageLen = dt.page.len();
  if (pageLen === -1) { requestAnimationFrame(tryScroll); return; }
  dt.one('draw', () => requestAnimationFrame(tryScroll));
  dt.page(Math.floor(idx / pageLen)).draw('page');
}

/* ── Watchlists ── */
let wlDt = null;

function wlActive() {
  return state.watchlists.find(w => w.id === state.activeWlId) || state.watchlists[0] || null;
}

// 投資組合壓力測試（本站自製、實驗性 NEW 功能）：對整份自選股清單做等權重
// 假設下的歷史情境回放/產業集中度HHI/相關性/歷史模擬法VaR，見
// portfolio_risk.py 的模組說明。跟 sweetSpotCell/healthBadgeCell 那種「單股
// 一格」的呈現方式不同，這是觸發後才載入的獨立面板。
async function runWlStressTest() {
  const wl = wlActive();
  if (!wl) return;
  const panel = document.getElementById('wl-stress-panel');
  const body = document.getElementById('wl-stress-body');
  panel.classList.remove('hidden');
  body.innerHTML = '<p style="color:var(--text2);">計算中…</p>';
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });

  let data;
  try {
    data = await fetch(`/api/watchlists/${wl.id}/stress-test`).then(r => r.json());
  } catch (_) {
    body.innerHTML = '<p style="color:var(--neg);">計算失敗，請稍後再試</p>';
    return;
  }
  if (!data.holdings_count) {
    body.innerHTML = '<p style="color:var(--text2);">這份清單目前沒有股票，先加幾檔再試試看。</p>';
    return;
  }

  const scenarioRows = data.scenarios.map(s => {
    const ret = s.portfolio_return_pct;
    const dd = s.portfolio_max_drawdown_pct;
    const retHtml = ret != null ? `<span class="${pctClass(ret)}">${fmt.pct(ret)}</span>` : '—';
    const ddHtml = dd != null ? `<span class="neg">${dd.toFixed(2)}%</span>` : '—';
    return `<tr>
      <td>${s.label}<div style="font-size:11px;color:var(--text2);">${s.start} ~ ${s.end}</div></td>
      <td class="num">${retHtml}</td>
      <td class="num">${ddHtml}</td>
      <td class="num" style="color:var(--text2);">${s.covered}/${s.total}</td>
    </tr>`;
  }).join('');

  const ic = data.industry_concentration;
  const icBreakdown = ic.breakdown.map(b =>
    `<span class="badge" style="margin-right:6px;">${b.industry} ${b.count}檔（${b.pct}%）</span>`).join('');
  const icColor = ic.level === '高度集中' ? 'var(--neg)' : (ic.level === '中度集中' ? '#eab308' : 'var(--pos)');

  const corr = data.correlation;
  const corrPairHtml = corr.most_correlated_pair
    ? `${corr.most_correlated_pair.a_name}(${corr.most_correlated_pair.a}) 與 ${corr.most_correlated_pair.b_name}(${corr.most_correlated_pair.b})：<b>${corr.most_correlated_pair.corr}</b>`
    : '資料不足';

  const v = data.var;

  body.innerHTML = `
    <p style="color:var(--text2);font-size:12.5px;margin-bottom:14px;">
      共 ${data.holdings_count} 檔持股，因自選股清單未記錄實際股數/金額，以下一律假設<b>等權重</b>計算，僅反映持股組合本身的風險輪廓，不是真實部位風險。
    </p>

    <h4 style="font-size:14px;margin-bottom:8px;">📉 歷史情境回放</h4>
    <div class="ann-table-wrap">
      <table class="ann-table" style="width:100%;font-size:13px;margin-bottom:16px;">
        <thead><tr><th>情境</th><th class="num">期間報酬</th><th class="num">最大回檔</th><th class="num">涵蓋家數</th></tr></thead>
        <tbody>${scenarioRows}</tbody>
      </table>
    </div>

    <h4 style="font-size:14px;margin-bottom:8px;">🏭 產業集中度</h4>
    <p style="margin-bottom:8px;">HHI = <b style="color:${icColor};">${ic.hhi}</b>（<span style="color:${icColor};">${ic.level}</span>，>2500高度集中／1500-2500中度／&lt;1500分散）</p>
    <p style="margin-bottom:16px;">${icBreakdown}</p>

    <h4 style="font-size:14px;margin-bottom:8px;">🔗 相關性（近1年逐日報酬）</h4>
    <p style="margin-bottom:16px;">平均兩兩相關係數：<b>${corr.avg_pairwise != null ? corr.avg_pairwise : '資料不足'}</b>（${corr.pairs_computed} 組配對）<br>相關性最高的一對：${corrPairHtml}</p>

    <h4 style="font-size:14px;margin-bottom:8px;">📊 歷史模擬法 VaR（單日）</h4>
    <p>95% VaR：<b class="neg">${v.var_95_pct != null ? v.var_95_pct + '%' : '資料不足'}</b>　99% VaR：<b class="neg">${v.var_99_pct != null ? v.var_99_pct + '%' : '資料不足'}</b>
      <span style="color:var(--text2);font-size:12px;">（用近 ${v.days_used} 個交易日的等權重每日報酬率分布估算，不是常態分布假設的參數法）</span>
    </p>
  `;
}

async function wlCreate(name) {
  if (!state.user) return;
  try {
    const wl = await fetch('/api/watchlists', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name}),
    }).then(r => r.json());
    state.watchlists.push({id: wl.id, name: wl.name, codes: []});
    state.activeWlId = wl.id;
    renderWatchlistView();
  } catch { showToast('建立失敗'); }
}

async function wlDelete(id) {
  if (!state.user) return;
  try {
    await fetch(`/api/watchlists/${id}`, {method: 'DELETE'});
    state.watchlists = state.watchlists.filter(w => w.id !== id);
    if (state.activeWlId === id) state.activeWlId = state.watchlists[0]?.id || null;
    if (wlDt) { wlDt.destroy(); wlDt = null; }
    renderWatchlistView();
  } catch { showToast('刪除失敗'); }
}

async function wlRename(id, name) {
  if (!state.user) return;
  const wl = state.watchlists.find(w => w.id === id);
  if (!wl) return;
  const prev = wl.name;
  wl.name = name;
  renderWatchlistView();
  try {
    await fetch(`/api/watchlists/${id}`, {
      method: 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name}),
    });
  } catch { wl.name = prev; renderWatchlistView(); showToast('更名失敗'); }
}

async function _wlAddStockTo(wl, code) {
  if (!wl || wl.codes.includes(code)) return false;
  wl.codes.push(code);
  if (wl.id === state.activeWlId) renderWlTable();
  try {
    await fetch(`/api/watchlists/${wl.id}/stocks`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code}),
    });
    return true;
  } catch {
    wl.codes = wl.codes.filter(c => c !== code);
    if (wl.id === state.activeWlId) renderWlTable();
    showToast('新增失敗');
    return false;
  }
}

async function wlAddStock(code) {
  if (!state.user) return;
  await _wlAddStockTo(wlActive(), code);
}

async function wlRemoveStock(code) {
  if (!state.user) return;
  const wl = wlActive();
  if (!wl) return;
  wl.codes = wl.codes.filter(c => c !== code);
  renderWlTable();
  try {
    await fetch(`/api/watchlists/${wl.id}/stocks/${code}`, {method: 'DELETE'});
  } catch { showToast('移除失敗'); }
}

function renderWatchlistView() {
  const authPrompt = document.getElementById('wl-auth-prompt');
  const content    = document.getElementById('wl-content');
  if (!state.user) {
    authPrompt.classList.remove('hidden');
    content.classList.add('hidden');
    return;
  }
  authPrompt.classList.add('hidden');
  content.classList.remove('hidden');

  const wls = state.watchlists;
  if (!wls.find(w => w.id === state.activeWlId))
    state.activeWlId = wls[0]?.id || null;

  document.getElementById('wl-tabs').innerHTML = wls.map(wl => `
    <div class="wl-tab${wl.id === state.activeWlId ? ' active' : ''}" data-wl-id="${wl.id}">
      <span class="wl-tab-name" data-wl-id="${wl.id}">${wl.name}</span>
      <button class="wl-tab-del" data-wl-del="${wl.id}" title="刪除清單">✕</button>
    </div>`).join('');

  document.getElementById('wl-empty').classList.toggle('hidden', wls.length > 0);
  document.getElementById('wl-table-wrap').classList.toggle('hidden', wls.length === 0);
  if (wls.length > 0) renderWlTable();
}

// 撤退=紅／注意=黃／早期警告=灰／正常=綠。這一整套「持股健康檢查」是本站
// 自製的實驗性功能（NEW），跟達人選股用途不同：那邊是找買點，這裡是給
// 已持有的自選股看要不要出場，見 experts.compute_holding_health()。
function healthBadgeCell(health) {
  if (!health) return '—';
  const styles = { retreat: ['#ef4444', '#fff'], caution: ['#eab308', '#000'],
                    early: ['#94a3b8', '#000'], normal: ['#22c55e', '#fff'] };
  const [bg, fg] = styles[health.tier] || ['#94a3b8', '#000'];
  const fill = 'display:block;margin:-9px -12px;padding:9px 12px;font-weight:600;';
  return `<span style="${fill}background:${bg};color:${fg}" title="技術面異常${health.tech_score}項／基本面異常${health.fund_score}項">${health.tier_label}</span>`;
}

async function renderWlTable() {
  if (_starChanlunMap === null) _loadStarChanlunMap();
  const wl = wlActive();
  if (!wl) return;
  document.getElementById('wl-count').textContent = `${wl.codes.length} 支`;

  const healthList = await Promise.all(wl.codes.map(code =>
    fetch(`/api/stocks/${code}/health`).then(r => r.json()).catch(() => null)
  ));
  const healthByCode = {};
  wl.codes.forEach((code, i) => { healthByCode[code] = healthList[i]; });

  const rows = wl.codes.map(code => {
    const s = state.allData.find(d => d.code === code);
    const rmBtn = `<button class="wl-remove-btn" data-rm-code="${code}" title="移除">✕</button>`;
    if (!s) return [rmBtn, code, '(未載入)', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—'];
    const est = calcEst(s);
    const ratio = (est != null && s.close) ? est / s.close : null;
    let estCell = '—';
    if (est != null) {
      const fill = 'display:block;margin:-9px -12px;padding:9px 12px;font-weight:600;';
      if (s.close != null && est >= s.close * 2)
        estCell = `<span style="${fill}background:#ef4444;color:#fff">${fmt.price(est)}</span>`;
      else if (s.close != null && est >= s.close * 1.5)
        estCell = `<span style="${fill}background:#eab308;color:#000">${fmt.price(est)}</span>`;
      else estCell = fmt.price(est);
    }
    return [
      rmBtn,
      `<span class="stock-link" data-code="${code}">${code}</span>`,
      `<span class="stock-link" data-code="${code}">${s.name}</span>`,
      s.industry || '—',
      s.start_price != null ? fmt.price(s.start_price) : '—',
      s.close != null ? fmt.price(s.close) : '—',
      s.price_diff != null ? `<span class="${pctClass(s.price_diff)}">${fmt.pct(s.price_diff)}</span>` : '—',
      s.change_pct != null ? `<span class="${pctClass(s.change_pct)}">${fmt.pct(s.change_pct)}</span>` : '—',
      estCell,
      ratio != null ? ratio.toFixed(2) + 'x' : '—',
      (s.rev_year && s.rev_month) ? `${s.rev_year}/${String(s.rev_month).padStart(2,'0')}` : '—',
      s.revenue != null ? fmt.rev(s.revenue) : '—',
      s.revenue_yoy != null ? `<span class="${pctClass(s.revenue_yoy)}">${fmt.pct(s.revenue_yoy)}</span>` : '—',
      s.eps != null ? `<span class="${pctClass(s.eps)}">${fmt.eps(s.eps)}</span>` : '—',
      s.pe_ratio != null ? Number(s.pe_ratio).toFixed(1) + 'x' : '—',
      s.price_date || '—',
      _starChanlunCell(code),
      sweetSpotCell(s),
      turnaroundCell(s),
      healthBadgeCell(healthByCode[code]),
    ];
  });

  if (wlDt) {
    wlDt.clear().rows.add(rows).draw();
  } else {
    wlDt = $('#wl-table').DataTable({
      data: rows, pageLength: 25, order: [], language: dtLang(), destroy: true, scrollX: true,
      columnDefs: [
        { targets: 0, orderable: false, className: 'dt-center', width: '32px' },
        { targets: [4,5,6,7,8,9,11,12,13,14], className: 'dt-right', type: 'num-cell' },
        { targets: [1,2,3,10,15],             className: 'dt-left' },
        { targets: 16, className: 'dt-center', render: { _: 0, display: 1 } },
        { targets: 17, className: 'dt-right', render: { _: 0, display: 1 } },
        { targets: 18, className: 'dt-left', width: '64px' },
        { targets: 19, className: 'dt-left', width: '76px' },
      ],
    });
    $('#wl-table tbody').on('click', '.wl-remove-btn', function(e) {
      e.stopImmediatePropagation();
      wlRemoveStock(this.dataset.rmCode);
    });
    $('#wl-table tbody').on('click', 'td', function() {
      const code = $(this).find('[data-code]').data('code') ||
                   $(this).closest('tr').find('[data-code]').data('code');
      if (code) {
        setDetailNavContext(_dtOrderedCodes(wlDt, 1), String(code));
        loadStockDetail(code);
      }
    });
  }
}

// Tab click (switch / delete)
document.getElementById('wl-tabs').addEventListener('click', function(e) {
  const rawDelId = e.target.dataset.wlDel;
  if (rawDelId) {
    const delId = parseInt(rawDelId);
    const wl = state.watchlists.find(w => w.id === delId);
    if (wl && confirm(`確定刪除「${wl.name}」？`)) wlDelete(delId);
    return;
  }
  const tab = e.target.closest('.wl-tab');
  if (tab) {
    const tabId = parseInt(tab.dataset.wlId);
    if (tabId !== state.activeWlId) {
      state.activeWlId = tabId;
      if (wlDt) { wlDt.destroy(); wlDt = null; }
      renderWatchlistView();
    }
  }
});

// Double-click tab name to rename
document.getElementById('wl-tabs').addEventListener('dblclick', function(e) {
  const nameEl = e.target.closest('.wl-tab-name');
  if (!nameEl) return;
  const wl = state.watchlists.find(w => w.id === parseInt(nameEl.dataset.wlId));
  if (!wl) return;
  const newName = prompt('請輸入新的清單名稱：', wl.name);
  if (newName && newName.trim()) wlRename(wl.id, newName.trim());
});

// New watchlist button
document.getElementById('wl-new-btn').addEventListener('click', function() {
  const name = prompt('請輸入自選股清單名稱：', `自選股 ${state.watchlists.length + 1}`);
  if (name && name.trim()) wlCreate(name.trim());
});

// Search input
const wlSearchInput = document.getElementById('wl-search');
const wlSearchDropdown = document.getElementById('wl-search-dropdown');

let _wlMatches = [];

wlSearchInput.addEventListener('input', function() {
  const q = this.value.trim().toLowerCase();
  if (!q) { wlSearchDropdown.classList.add('hidden'); _wlMatches = []; return; }
  _wlMatches = state.allData
    .filter(s => s.code.startsWith(q) || s.name.toLowerCase().includes(q))
    .slice(0, 10);
  if (!_wlMatches.length) { wlSearchDropdown.classList.add('hidden'); return; }
  wlSearchDropdown.innerHTML = _wlMatches.map(s =>
    `<div class="wl-search-item" data-code="${s.code}">
      <span class="wl-si-code">${s.code}</span>
      <span class="wl-si-name">${s.name}</span>
      <span class="wl-si-ind">${s.industry || ''}</span>
    </div>`).join('');
  wlSearchDropdown.classList.remove('hidden');
});

wlSearchInput.addEventListener('keydown', function(e) {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const top = _wlMatches[0];
  if (!top) return;
  wlAddStock(top.code);
  this.value = '';
  wlSearchDropdown.classList.add('hidden');
  _wlMatches = [];
  showToast(`已加入 ${top.code} ${top.name}`);
});

wlSearchDropdown.addEventListener('click', function(e) {
  const item = e.target.closest('.wl-search-item');
  if (!item) return;
  wlAddStock(item.dataset.code);
  wlSearchInput.value = '';
  wlSearchDropdown.classList.add('hidden');
  showToast(`已加入 ${item.dataset.code}`);
});

document.addEventListener('click', function(e) {
  if (!wlSearchInput.contains(e.target) && !wlSearchDropdown.contains(e.target))
    wlSearchDropdown.classList.add('hidden');
});

/* ── 總體分析：機構級研究備忘錄提示詞產生器 ── */
const macroSearchInput = document.getElementById('macro-search');
const macroSearchDropdown = document.getElementById('macro-search-dropdown');
let _macroMatches = [];
let _macroSelected = null;

macroSearchInput.addEventListener('input', function() {
  const q = this.value.trim().toLowerCase();
  if (!q) { macroSearchDropdown.classList.add('hidden'); _macroMatches = []; return; }
  _macroMatches = state.allData
    .filter(s => s.code.startsWith(q) || (s.name && s.name.toLowerCase().includes(q)))
    .slice(0, 10);
  if (!_macroMatches.length) { macroSearchDropdown.classList.add('hidden'); return; }
  macroSearchDropdown.innerHTML = _macroMatches.map(s =>
    `<div class="wl-search-item" data-code="${s.code}">
      <span class="wl-si-code">${s.code}</span>
      <span class="wl-si-name">${s.name}</span>
      <span class="wl-si-ind">${s.industry || ''}</span>
    </div>`).join('');
  macroSearchDropdown.classList.remove('hidden');
});

macroSearchInput.addEventListener('keydown', function(e) {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  const top = _macroMatches[0];
  if (!top) return;
  selectMacroStock(top.code);
});

macroSearchDropdown.addEventListener('click', function(e) {
  const item = e.target.closest('.wl-search-item');
  if (!item) return;
  selectMacroStock(item.dataset.code);
});

document.addEventListener('click', function(e) {
  if (!macroSearchInput.contains(e.target) && !macroSearchDropdown.contains(e.target))
    macroSearchDropdown.classList.add('hidden');
});

async function selectMacroStock(code) {
  const s = state.allData.find(d => d.code === String(code));
  if (!s) { showToast('找不到這檔股票'); return; }
  _macroSelected = s;
  macroSearchInput.value = '';
  macroSearchDropdown.classList.add('hidden');
  _macroMatches = [];

  const sel = document.getElementById('macro-selected');
  sel.textContent = `已選擇：${s.code} ${s.name}${s.industry ? `（${s.industry}）` : ''}`;
  sel.classList.remove('hidden');

  if (s.close != null) document.getElementById('macro-price').value = s.close;

  const extraEl = document.getElementById('macro-extra');
  extraEl.value = '載入本站資料中…';
  // Prices/institutional are fetched with warm-up so the trust cost line
  // (120 trading days) is valid; only the last ~half year is printed.
  const [financials, revenues, pricesAll, brokerTrades, instAll] = await Promise.all([
    fetch(`/api/stocks/${code}/financials`).then(r => r.json()).catch(() => []),
    fetch(`/api/stocks/${code}/revenue`).then(r => r.json()).catch(() => []),
    fetch(`/api/stocks/${code}/prices?days=${182 + _CHART_WARMUP_DAYS}`).then(r => r.json()).catch(() => []),
    fetch(`/api/stocks/${code}/broker-trades?days=90`).then(r => r.json()).catch(() => []),
    fetch(`/api/stocks/${code}/institutional-trades?days=${182 + _CHART_WARMUP_DAYS}`).then(r => r.json()).catch(() => []),
  ]);
  const [fundamentals, macroExtra, chipPeak, chanlunRes] = await Promise.all([
    fetch(`/api/stocks/${code}/fundamentals`).then(r => r.json()).catch(() => null),
    fetch(`/api/stocks/${code}/macro-extra`).then(r => r.ok ? r.json() : null).catch(() => null),
    fetch(`/api/stocks/${code}/chip-peak`).then(r => r.json()).catch(() => null),
    fetch(`/api/stocks/${code}/chanlun`).then(r => r.json()).catch(() => null),
  ]);
  const cutoff = new Date(Date.now() - 182 * 86400000).toISOString().slice(0, 10);
  const prices = pricesAll.filter(p => p.date >= cutoff);
  const inst = instAll.filter(r => r.date >= cutoff);
  const trustCost = instAll.length ? _trustCostSeries(pricesAll, instAll).at(-1) : null;
  extraEl.value = _buildMacroExtraText(s, financials, revenues, prices, brokerTrades, inst, trustCost,
    { fundamentals, macroExtra, chipPeak, chanlun: chanlunRes });
}

// Net buy/sell in 張 for one investor type ('foreign'/'trust'/'dealer')
function _instNetLots(r, who) {
  const b = r[`${who}_buy`], se = r[`${who}_sell`];
  return b == null && se == null ? null : Math.round(((b || 0) - (se || 0)) / 1000);
}

// Consecutive days (from the latest row backwards) with the same net sign
function _instStreak(rows, who) {
  let n = 0, sign = 0;
  for (let i = rows.length - 1; i >= 0; i--) {
    const v = _instNetLots(rows[i], who);
    const sg = v > 0 ? 1 : v < 0 ? -1 : 0;
    if (!sg) break;
    if (!sign) sign = sg;
    if (sg !== sign) break;
    n++;
  }
  return sign ? `連續${sign > 0 ? '買超' : '賣超'} ${n} 日` : '最新一日無買賣超';
}

function _buildMacroExtraText(s, financials, revenues, prices, brokerTrades, inst = [], trustCost = null, more = {}) {
  const { fundamentals, macroExtra, chipPeak, chanlun } = more;
  const n2 = v => v == null ? '—' : Number(v).toFixed(2);
  const n1 = v => v == null ? '—' : Number(v).toFixed(1);
  const k = v => v == null ? '—' : Math.round(v).toLocaleString();
  const lines = [];
  lines.push(`【本站資料庫基本面摘要｜資料日期 ${s.price_date || '—'}】`);
  lines.push('（股價皆為未還原除權息的原始價格；金額單位為新台幣千元；EPS 為單季值，單位元）');
  lines.push(`收盤價：${s.close ?? '—'}｜漲跌幅：${s.change_pct != null ? s.change_pct + '%' : '—'}`);
  lines.push(`均線：20MA ${s.ma20 ?? '—'}｜60MA ${s.ma60 ?? '—'}｜120MA ${s.ma120 ?? '—'}｜240MA ${s.ma240 ?? '—'}`);
  lines.push(`本益比（股價÷近四季EPS合計）：${s.pe_ratio ?? '—'}｜殖利率：${s.dividend_yield != null ? s.dividend_yield + '%' : '—'}`);
  lines.push(`最新月營收：${s.rev_year && s.rev_month ? `${s.rev_year}/${String(s.rev_month).padStart(2, '0')}` : '—'}，年增率 ${s.revenue_yoy != null ? s.revenue_yoy + '%' : '—'}`);
  lines.push(`最新單季EPS：${s.eps ?? '—'}（${s.eps_year ?? '—'}Q${s.eps_quarter ?? '—'}）`);

  const q = (financials || []).slice(0, 8);
  if (q.length) {
    lines.push('');
    lines.push('近8季單季財報（年季 | 營收(千元) | 營業利益(千元) | 淨利(千元) | 單季EPS(元)）：');
    q.forEach(f => lines.push(`${f.year}Q${f.quarter} | ${f.revenue ?? '—'} | ${f.operating_income ?? '—'} | ${f.net_income ?? '—'} | ${f.eps ?? '—'}`));
  }
  const r = (revenues || []).slice(0, 12);
  if (r.length) {
    lines.push('');
    lines.push('近12個月月營收年增率：');
    lines.push(r.map(x => `${x.year}/${String(x.month).padStart(2, '0')} ${x.revenue_yoy != null ? x.revenue_yoy.toFixed(1) + '%' : '—'}`).join('、'));
  }
  const fq = ((fundamentals && fundamentals.quarterly) || []).slice(0, 8);
  if (fq.some(q => q.gross_margin != null || q.debt_ratio != null)) {
    lines.push('');
    lines.push('近8季財務比率（年季 | 毛利率% | 營益率% | ROE%(年化) | ROA%(年化) | 流動比率% | 速動比率% | 負債比率% | 存貨週轉天數 | 應收帳款週轉天數）：');
    fq.forEach(q => lines.push(`${q.year}Q${q.quarter} | ${n1(q.gross_margin)} | ${n1(q.operating_margin)} | ${n1(q.roe)} | ${n1(q.roa)} | ${n1(q.current_ratio)} | ${n1(q.quick_ratio)} | ${n1(q.debt_ratio)} | ${n1(q.inventory_turnover_days)} | ${n1(q.ar_turnover_days)}`));
  }
  const fe = (macroExtra && macroExtra.financial_extra) || [];
  if (fe.length) {
    lines.push('');
    lines.push('近8季現金流量（千元，單季值；資本支出為負＝流出；自由現金流＝營業現金流＋資本支出）：');
    lines.push('年季 | 營業現金流 | 資本支出 | 自由現金流 | 利息費用 | 稅前淨利');
    fe.forEach(r => {
      const fcf = r.operating_cash_flow != null && r.capex != null ? r.operating_cash_flow + r.capex : null;
      lines.push(`${r.year}Q${r.quarter} | ${k(r.operating_cash_flow)} | ${k(r.capex)} | ${k(fcf)} | ${k(r.interest_expense)} | ${k(r.pretax_income)}`);
    });
    lines.push('近8季資產負債表（千元，季末餘額）：');
    lines.push('年季 | 總資產 | 總負債 | 股東權益 | 流動資產 | 流動負債 | 存貨 | 應收帳款 | 長期借款 | 股本');
    fe.forEach(r => lines.push(`${r.year}Q${r.quarter} | ${k(r.total_assets)} | ${k(r.liabilities)} | ${k(r.equity)} | ${k(r.current_assets)} | ${k(r.current_liabilities)} | ${k(r.inventories)} | ${k(r.accounts_receivable)} | ${k(r.long_term_borrowings)} | ${k(r.capital_stock)}`));
  }
  const divs = ((fundamentals && fundamentals.dividends) || []).slice(0, 5);
  const snap = (fundamentals && fundamentals.snapshot) || {};
  if (divs.length || snap.pbr != null) {
    lines.push('');
    lines.push(`估值與股利：股價淨值比 ${n2(snap.pbr)}｜近5年填息機率 ${snap.fill_rate_5y != null ? n1(snap.fill_rate_5y) + '%' : '—'}`);
    if (divs.length) {
      lines.push('近5個年度股利（所屬年度 | 現金股利(元) | 股票股利(元) | 合計 | 配發率% | 當年底殖利率%）：');
      divs.forEach(d => lines.push(`${d.fiscal_year} | ${n2(d.cash_dividend)} | ${n2(d.stock_dividend)} | ${n2(d.total)} | ${n1(d.payout_ratio)} | ${n2(d.dividend_yield)}`));
    }
    const fills = (macroExtra && macroExtra.fill_events) || [];
    if (fills.length) {
      lines.push(`近5年除權息事件（除息日 除息前收盤價 是否已填息）：${fills.map(f => `${f.ex_date} ${f.before_price ?? '—'} ${f.filled ? '已填息' : '未填息'}`).join('、')}`);
    }
  }
  const p = prices || [];
  if (p.length) {
    lines.push('');
    lines.push(`近半年日線資料（日期 | 開 | 高 | 低 | 收 | 漲跌% | 成交量(張)）共 ${p.length} 筆：`);
    p.forEach(x => lines.push(`${x.date} | ${x.open ?? '—'} | ${x.high ?? '—'} | ${x.low ?? '—'} | ${x.close ?? '—'} | ${x.change_pct != null ? x.change_pct + '%' : '—'} | ${x.volume != null ? Math.round(x.volume / 1000) : '—'}`));
  }
  if (inst.length) {
    const WHO = [['foreign', '外資'], ['trust', '投信'], ['dealer', '自營商']];
    const sumLast = (who, n) => inst.slice(-n).reduce((a, r) => a + (_instNetLots(r, who) || 0), 0);
    const sgn = v => (v > 0 ? '+' : '') + v.toLocaleString();
    lines.push('');
    lines.push(`三大法人買賣超彙總（單位：張，資料至 ${inst[inst.length - 1].date}）：`);
    WHO.forEach(([k, name]) => lines.push(
      `${name}：近5日 ${sgn(sumLast(k, 5))}｜近20日 ${sgn(sumLast(k, 20))}｜近60日 ${sgn(sumLast(k, 60))}｜${_instStreak(inst, k)}`));
    if (trustCost != null) {
      const last = prices.length ? prices[prices.length - 1].close : s.close;
      lines.push(`投信推估成本（近120交易日投信買賣超，移動平均成本法，非實際持股成本）：${trustCost}${last ? `，現價距成本 ${((last - trustCost) / trustCost * 100).toFixed(2)}%` : ''}`);
    }
    lines.push(`近半年三大法人逐日買賣超（日期 | 外資 | 投信 | 自營商，單位：張，正＝買超）共 ${inst.length} 筆：`);
    inst.forEach(r => lines.push(`${r.date} | ${WHO.map(([k]) => { const v = _instNetLots(r, k); return v == null ? '—' : sgn(v); }).join(' | ')}`));
  }
  const hold = (macroExtra && macroExtra.holding) || [];
  if (hold.length) {
    lines.push('');
    lines.push('股權分散（集保週資料，占集保總股數%｜日期 | 1000張以上大戶 | 400張以上 | 200張以下散戶）：');
    hold.forEach(h => lines.push(`${h.date} | ${n2(h.pct_1000up)} | ${n2(h.pct_400up)} | ${n2(h.pct_200down)}`));
  }
  const dir = (macroExtra && macroExtra.director) || [];
  if (dir.length) {
    lines.push(`董監持股比例（民國年月 比例%）：${dir.map(d => `${d.year_month} ${n2(d.holding_pct)}%`).join('、')}`);
  }
  if (chipPeak && chipPeak.poc != null) {
    lines.push('');
    lines.push(`籌碼峰（近${chipPeak.lookback_days}個交易日成交量經時間衰減＋法人品質加權的價量分布，未還原股價）：POC ${chipPeak.poc}｜價值區間 VAL ${chipPeak.val}～VAH ${chipPeak.vah}｜現價距POC ${(chipPeak.price_to_poc * 100).toFixed(2)}%｜主峰集中度 ${(chipPeak.peak_strength * 100).toFixed(1)}%`);
  }
  if (chanlun && chanlun.strokes && chanlun.strokes.length) {
    const sig = chanlun.latest_signal;
    const center = chanlun.centers && chanlun.centers[chanlun.centers.length - 1];
    lines.push(`纏論（近一年日線，近似版、未做線段層級）：最新訊號 ${sig ? `${_CHANLUN_SIGNAL_LABELS[sig.type] || sig.type} ${sig.price}（${sig.date}）` : '無'}｜最近中樞 ${center ? `${center.zd}～${center.zg}（${center.start_date}～${center.end_date}）` : '無'}｜筆數 ${chanlun.strokes.length}`);
  }
  const bt = brokerTrades || [];
  if (bt.length) {
    const dates = [...new Set(bt.map(r => r.date))];
    const cumMap = {};
    for (const r of bt) {
      const c = cumMap[r.broker_id] || (cumMap[r.broker_id] = {name: r.broker_name || r.broker_id, net: 0, activity: 0});
      c.net += (r.buy_volume || 0) - (r.sell_volume || 0);
      c.activity += (r.buy_volume || 0) + (r.sell_volume || 0);
    }
    const cum = Object.values(cumMap);
    const topBuy  = [...cum].sort((a, b) => b.net - a.net).slice(0, 10).filter(c => c.net > 0);
    const topSell = [...cum].sort((a, b) => a.net - b.net).slice(0, 10).filter(c => c.net < 0);
    const totalActivity = cum.reduce((s, c) => s + c.activity, 0);
    const top5Pct = totalActivity ? ([...cum].sort((a, b) => b.activity - a.activity).slice(0, 5)
      .reduce((s, c) => s + c.activity, 0) / totalActivity * 100).toFixed(1) : null;
    lines.push('');
    lines.push(`近${dates.length}個交易日主力分點進出（單位：張，${cum.length}家分點參與${top5Pct != null ? `，前5大分點占${top5Pct}%成交量` : ''}）：`);
    lines.push(`買超前十：${topBuy.length ? topBuy.map(c => `${c.name} +${_brokerLots(c.net).toLocaleString()}`).join('、') : '（無）'}`);
    lines.push(`賣超前十：${topSell.length ? topSell.map(c => `${c.name} ${_brokerLots(c.net).toLocaleString()}`).join('、') : '（無）'}`);
  }
  return lines.join('\n');
}

function _macroField(id) { return (document.getElementById(id).value || '').trim(); }

function buildMacroPrompt() {
  const code = _macroSelected ? _macroSelected.code : '';
  const name = _macroSelected ? _macroSelected.name : '';
  const searchVal = _macroField('macro-search');
  const target = code ? `${name}（${code}）` : (searchVal || '（未指定，請先在上方搜尋並選擇股票）');
  const price = _macroField('macro-price') || '（未提供）';
  const costVal = _macroField('macro-cost');
  const qtyVal = _macroField('macro-qty');
  const cost = costVal ? `${costVal} 元／股（平均買進價）` : '尚未持有';
  // Sanity check: a per-share cost >3x away from the current price is most
  // likely a typo (misplaced decimal, or total amount entered as per-share)
  const costRatio = costVal && Number(costVal) > 0 && Number(_macroField('macro-price')) > 0
    ? Number(_macroField('macro-price')) / Number(costVal) : null;
  const costCheck = costRatio != null && (costRatio > 3 || costRatio < 1 / 3)
    ? `
* **⚠️ 成本檢查：** 持股成本與目前股價相差約 ${costRatio >= 1 ? costRatio.toFixed(1) : (1 / costRatio).toFixed(1)} 倍，可能是輸入錯誤（小數點位置、或把總金額當成每股成本）。請在報告開頭先明確提醒使用者確認成本，並同時列出「依輸入值」與「若成本約等於現價量級」兩種情境的報酬計算，不要只給出依輸入值算出的極端報酬率。`
    : '';
  const qty = qtyVal ? `${qtyVal} ${_macroField('macro-qty-unit')}` : '尚未持有';
  // 股息 = cash dividends (元), 股利 = stock dividends received (股). Optional
  // chaining: the fields only exist once the server serves the updated index.html
  const cashDivVal = (document.getElementById('macro-cash-dividend')?.value || '').trim();
  const stockDivVal = (document.getElementById('macro-stock-dividend')?.value || '').trim();
  const cashDividend = cashDivVal
    ? `${Number(cashDivVal).toLocaleString()} 元（持有期間累計已領現金股利總額）`
    : '無／未提供';
  // 股票股利 is quoted in 元/股 at par 10 元: shares received = shares held × 元 ÷ 10
  // (1.0 元 → 100 股 per 張). Assumes 持有數量 is the pre-distribution holding.
  const heldShares = qtyVal ? Number(qtyVal) * (_macroField('macro-qty-unit') === '張' ? 1000 : 1) : null;
  const stockDividend = stockDivVal
    ? `${stockDivVal} 元／股（面額10元計，每張配 ${Math.round(Number(stockDivVal) * 100)} 股${heldShares ? `；以持有 ${heldShares.toLocaleString()} 股計，約配 ${Math.round(heldShares * Number(stockDivVal) / 10).toLocaleString()} 股` : ''}）`
    : '無／未提供';
  const dividendNote = (cashDivVal || stockDivVal)
    ? '\n* **含權息計算要求：** 請把現金股利與股票股利一併納入，計算含權息報酬率與含權息平均成本（現金股利直接抵減投入成本；股票股利依面額10元換算配股股數＝持股股數×股票股利÷10，增加持股、攤低每股成本）。上方配股股數假設「持有數量」為配股前股數，若實際已含配股請說明並調整；若公司面額非10元請改以實際面額換算。'
    : '';
  const horizonSel = document.getElementById('macro-horizon');
  const horizon = horizonSel.options[horizonSel.selectedIndex].textContent;
  const maxloss = _macroField('macro-maxloss');
  const maxlossText = maxloss ? `${maxloss}%` : '（未提供）';
  const extra = _macroField('macro-extra') || '（無補充資料）';
  const stockLine = code || searchVal || '（請填入股票代號）';

  return `# 機構級台股「基本面 × 供需 × 技術 × 籌碼 × 估值 × 風控」全方位研究 Prompt

## 一、角色設定

你是一名具備 20 年以上經驗的頂級投資銀行 Sell-side Research 資深股票研究員，同時具備：

* 台股基本面研究能力
* 半導體／硬體／AI／科技產業供需分析能力
* 財報與現金流分析能力
* 技術分析與 K 線結構判讀能力
* 法人／主力／籌碼流向分析能力
* 估值模型與情境分析能力
* 機構投資組合風險管理能力

你的研究對象為資產管理公司、基金經理人及專業投資人。

**核心任務：不是預測股價，而是找出「企業價值、產業供需與市場價格」三者之間的差異，並據此建立可驗證的投資與風險管理框架。**

---

# 二、分析標的

* **股票名稱／代號：** ${target}
* **市場：** 台股
* **目前股價：** ${price}
* **持股成本：** ${cost}${costCheck}
* **持有數量：** ${qty}
* **現金股利：** ${cashDividend}
* **股票股利：** ${stockDividend}${dividendNote}
* **預計投資週期：** ${horizon}
* **最大可承受虧損：** ${maxlossText}
* **K線圖：** （本站無法直接附加圖片，如有 K 線圖請自行提供給 AI，或參考下方補充資料的文字描述）
* **補充資料：**
${extra}

---

# 三、即時資料與事實查核規則

## 1. 必須進行最新網路搜尋

分析前必須搜尋：

1. 公司最新財報
2. 最新營收／月營收
3. 最新法說會簡報
4. 法說會 Q&A／文字稿
5. 最新重大訊息
6. CAPEX／擴產計畫
7. 公司最新產品與訂單資訊
8. 所屬產業最新供需狀況
9. 最新產品報價／ASP／利差
10. 主要競爭對手最新狀況
11. 外資／本土券商產業研究
12. TrendForce、IDC、Gartner、Counterpoint、Digitimes 等專業產業資料（若適用）
13. 最新市場與產業新聞
14. 所屬產業的龍頭股（台股與國際龍頭各至少 1～3 家）及其最新財報、營收、法說會展望、股價走勢與估值

**不能只依賴使用者提供的資料。**

「補充資料」來自使用者自建資料庫，可能有落差（例如尚未更新、Q4 由年度數字反推、股本變動造成 EPS 失真）。若與 Tier 1 官方來源（公開資訊觀測站、證交所、櫃買中心、公司財報）不一致，**以官方數字為準**，並在報告中列出差異項目與兩邊數值。

若找不到最新新聞或可靠資料，必須明確寫：

> 【資料不足，無法確認】

不得自行補猜。

---

# 四、資料來源分級

### Tier 1：官方第一手資料

最高權重：

* 公司財報
* 公司法說會
* 法說會簡報
* 法說會 Q&A
* 公司公告
* 重大訊息
* 月營收
* CAPEX
* 公司正式新聞稿

### Tier 2：專業第二手資料

* 外資券商
* 本土券商
* TrendForce
* Gartner
* IDC
* Counterpoint
* Digitimes
* 其他具可信度的產業研究機構

### Tier 3：市場資訊

* 財經媒體
* 產業新聞
* 市場訪查
* 法人公開資訊
* 社群／論壇

Tier 3 僅可作為輔助，不可單獨作為重大投資結論的核心依據。

---

# 五、第一層：投資摘要評等框

分析開頭先提供：

| 項目          | 評估         |
| ----------- | ---------- |
| 股票          |            |
| 基準日         | YYYY/MM/DD |
| 股價          |            |
| 基本面狀態       |            |
| 產業供需        |            |
| 技術趨勢        |            |
| 籌碼結構        |            |
| 估值          |            |
| 主要催化劑       |            |
| 最大風險        |            |
| 12個月多方情境目標價 |            |
| 12個月基準情境目標價 |            |
| 12個月空方情境目標價 |            |
| 投資策略        |            |
| 信心程度        | 高／中／低      |

**禁止用「最佳、最強、第一名」等排名式描述。**

---

# 六、第二層：商業模式與基本面

## 1. 商業模式拆解

用白話回答：

> 公司到底靠什麼賺錢？

說明：

* 核心產品
* 核心客戶
* 收入來源
* 成本結構
* 利潤來源
* 主要成長引擎
* 哪些因素會讓 EPS 上升
* 哪些因素會讓 EPS 下滑

---

## 2. 營收結構

整理：

| 事業／產品線 | 營收占比 | YoY | QoQ | 近3～5年趨勢 | 未來展望 |
| ------ | ---: | --: | --: | ------- | ---- |
|        |      |     |     |         |      |

分析：

* 哪個產品線正在成長？
* 哪個產品線正在衰退？
* 成長來自價格、數量還是產品組合？
* 成長是否具持續性？

---

# 七、第三層：獲利能力與財務品質

至少分析近 5 年：

| 指標    | FY-4 | FY-3 | FY-2 | FY-1 | 最新年度 |
| ----- | ---: | ---: | ---: | ---: | ---: |
| 營收    |      |      |      |      |      |
| 毛利率   |      |      |      |      |      |
| 營業利益率 |      |      |      |      |      |
| 淨利率   |      |      |      |      |      |
| EPS   |      |      |      |      |      |
| ROE   |      |      |      |      |      |
| ROIC  |      |      |      |      |      |

重點判斷：

1. 毛利率是否上升？
2. 營益率是否改善？
3. 是否存在營運槓桿？
4. ROE 是否由真正獲利改善推動？
5. EPS 成長是否主要靠業外收益？
6. 是否存在一次性收益？

---

# 八、第四層：資產負債表健康度

分析：

| 指標      | 最新數據 | 趨勢 | 判讀 |
| ------- | ---: | -- | -- |
| 負債權益比   |      |    |    |
| 流動比率    |      |    |    |
| 速動比率    |      |    |    |
| 現金及約當現金 |      |    |    |
| 總負債     |      |    |    |
| 現金／總負債  |      |    |    |
| 淨負債     |      |    |    |
| 利息保障倍數  |      |    |    |

特別判斷：

* 公司是否具有財務壓力？
* 是否需要大量借款擴產？
* 利率上升是否會造成風險？
* 現金是否足以支應 CAPEX？

---

# 九、第五層：自由現金流與資本配置

分析：

* CFO
* CAPEX
* FCF
* FCF Margin
* FCF Yield
* FCF YoY 成長率

公式：

> FCF = 營業現金流 − 資本支出

分析：

1. FCF 是否長期為正？
2. FCF 是否與淨利同步？
3. FCF 是否持續成長？
4. 資本支出是否過度？
5. 公司如何使用現金？

資本配置依序分析：

> CAPEX → 研發 → 股利 → 庫藏股 → M&A → 降低負債

判斷管理層資本配置是否與股東利益一致。

---

# 十、第六層：競爭優勢評分

對以下項目分別給 1～10 分：

| 競爭優勢  |  分數 | 證據 |
| ----- | --: | -- |
| 定價權   | /10 |    |
| 品牌    | /10 |    |
| 技術壁壘  | /10 |    |
| 轉換成本  | /10 |    |
| 網路效應  | /10 |    |
| 規模經濟  | /10 |    |
| 客戶黏著度 | /10 |    |
| 供應鏈地位 | /10 |    |

**分數必須有具體事實支持，不得憑主觀印象評分。**

## 產業龍頭股對照

必須先聯網搜尋並列出本公司所屬產業（及其上下游關鍵環節）的龍頭股，台股與國際龍頭都要涵蓋：

| 龍頭公司／代號 | 市場 | 產業地位／市占 | 最新營收 YoY | 毛利率 | 預估 P/E | 近3個月股價表現 | 最新法說會展望 |
| ------- | -- | ------- | -------: | --: | -----: | -------: | ------- |
| 本公司     |    |         |          |     |        |          |         |
| 龍頭A     |    |         |          |     |        |          |         |
| 龍頭B     |    |         |          |     |        |          |         |
| 龍頭C     |    |         |          |     |        |          |         |

分析：

* 本公司與龍頭的差距在哪裡（規模、技術、客戶、成本）？
* 龍頭最新的營收／展望／資本支出，對本公司是領先指標還是競爭威脅？
* 龍頭股價與本公司股價的連動性（是否跟漲不跟跌、落後補漲或領先轉弱）
* 本公司相對龍頭的估值是折價還是溢價？是否合理？

---

# 十一、第七層：管理層品質

分析：

### 資本配置紀錄

* 過去 5 年 CAPEX
* 股利
* 庫藏股
* M&A
* 負債管理

### 管理層與股東利益

分析：

* 董事／高階主管持股
* 內部人交易
* 股票薪酬
* 管理層薪酬制度
* EPS／ROIC／FCF 是否與獎酬掛鉤
* 是否存在股東稀釋

最後以：

> 管理層與股東利益一致程度：高／中／低

呈現。

---

# 十二、第八層：供需狀態矩陣

針對公司核心產品／產業建立「供需狀態矩陣」。

## 標準 8 欄

| 評估維度         | 現狀分析與量化數據（基準日：YYYY年MM月） |
| :----------- | :---------------------- |
| **目前供需**     |                         |
| **供需緊繃程度**   |                         |
| **報價／利差趨勢**  |                         |
| **未來1～2年趨勢** |                         |
| **轉折預估季度**   |                         |
| **供需驅動力**    |                         |
| **瓶頸持續性**    |                         |
| **數據可信度**    |                         |

### 供需燈號只能使用：

* 🔴 供不應求
* 🟡 供需平衡
* 🟢 供過於求
* ⚪ 緩步復甦

### 報價／利差：

* 強勢走揚
* 溫和走揚
* 持平
* 承壓

### 未來1～2年：

* 維持供不應求
* 供需逐步平衡
* 供過於求
* 緩步復甦

### 供需驅動力：

* 需求暴增
* 供給瓶頸
* 兩者兼具
* 需求放緩
* 成本推動

### 瓶頸持續性：

* 短期：1～4 季
* 中期：5～12 季
* 長期：13 季以上

格式：

> 短期（約 X 個季度）

### 數據可信度：

> 高／中／低

並列出 Tier 1／Tier 2 資料來源。

---

# 十三、第九層：供需與財務交叉驗證

不要只分析「需求很好」，必須確認：

**需求 → 出貨量 → ASP → 營收 → 毛利率 → 營益率 → EPS → FCF**

建立完整傳導鏈。

例如：

> AI需求增加
> ↓
> 出貨量增加
> ↓
> ASP 上升
> ↓
> 毛利率改善
> ↓
> 營業利益率提升
> ↓
> EPS 成長
> ↓
> FCF 增加

若其中任何一環沒有數據支持，明確標示：

> 【資料不足，無法確認】

---

# 十四、第十層：估值快照

至少分析：

* P/E
* P/S
* EV/EBITDA
* P/B
* FCF Yield

並比較：

### ① 公司自身歷史

* 目前估值
* 5 年平均
* 5 年高點
* 5 年低點

### ② 同業比較

至少選擇 3 家主要競爭者。

| 公司  | P/E | P/S | EV/EBITDA | FCF Yield |
| --- | --: | --: | --------: | --------: |
| 本公司 |     |     |           |           |
| 同業A |     |     |           |           |
| 同業B |     |     |           |           |
| 同業C |     |     |           |           |

若預估 P/E > 20 倍，必須額外分析：

* 產業是否處於高成長期
* 市場是否給予成長溢價
* 盈餘成長率是否足以支持估值
* 當前市場是否屬於熱門題材
* 估值壓縮風險

---

# 十五、第十一層：技術面「交易前八問法」

## 1. 當前市場在做什麼？

判斷：

* 多頭趨勢
* 空頭趨勢
* 盤整整理
* 反彈初期
* 拉回修正
* 高檔震盪
* 低檔打底

分析：

* 大盤
* 類股
* 個股
* 成交量
* K線
* 均線

---

## 2. 趨勢起點在哪裡？

找出：

* 起漲點
* 日期
* 起漲價
* 當時成交量
* 法人籌碼
* 產業／公司催化劑

計算：

> 起漲至今漲跌幅 =（目前股價／起漲價 − 1）×100%

---

## 3. 現在處於哪個階段？

從以下選擇：

* 初升段
* 主升段
* 加速段
* 高檔震盪
* 末升段
* 拉回整理
* 反彈逃命波
* 空頭下跌段

---

# 十六、第十二層：多週期技術分析

分別分析：

### 日線

* MA5
* MA10
* MA20
* MA60
* MA120
* 成交量
* RSI
* MACD
* KD

### 週線

### 月線

最後判斷：

> 日線 × 週線 × 月線是否一致？

分類：

### A：大多＋小多

趨勢一致

### B：大多＋小空

等待拉回止穩

### C：大空＋小多

反彈風險

### D：大空＋小空

觀望

---

# 十七、第十三層：籌碼面

分析：

* 外資
* 投信
* 自營商
* 三大法人
* 融資
* 融券
* 借券
* 大戶持股
* 主力分點
* 成交量
* 換手率
* 內外盤
* 法人連續買賣超

重點判斷：

> 股價上漲是否得到籌碼支持？

特別注意：

### 「價漲量增＋法人買超」

與

### 「價漲量縮＋法人賣超」

兩者必須區分。

---

# 十八、第十四層：心理偏誤診斷

客觀檢查交易者可能存在：

* FOMO
* 追高
* 攤平
* 凹單
* 沉沒成本
* 損失厭惡
* 過度相信利多
* 忽略估值
* 只看短線 K 線
* 只看基本面忽略市場價格

不得猜測使用者心理，必須使用「可能存在的交易偏誤」表述。

---

# 十九、第十五層：關鍵價位與風險控管

明確計算：

| 價位    | 價格 |
| ----- | -: |
| 第一支撐  |    |
| 第二支撐  |    |
| 關鍵停損  |    |
| 第一壓力  |    |
| 第二壓力  |    |
| 短線停利區 |    |
| 波段停利區 |    |

若持有成本已提供，計算：

### 停損損失

> （停損價－成本）／成本 × 100%

### 目標報酬

> （目標價－成本）／成本 × 100%

### 風報比

> 預期獲利 ÷ 預期虧損

---

# 二十、第十六層：12個月多空情境估值

建立三種情境。

## 情境A：多方

列出：

* 營收假設
* 毛利率
* EPS
* 合理 P/E 或 EV/EBITDA
* 12個月目標價

## 情境B：基準

同樣列出：

* 營收
* 毛利率
* EPS
* 合理估值
* 12個月目標價

## 情境C：空方

同樣列出：

* 營收
* 毛利率
* EPS
* 合理估值
* 12個月目標價

表格：

| 情境 | 核心假設 | EPS | 評價倍數 | 12個月目標價 |
| -- | ---- | --: | ---: | ------: |
| 多方 |      |     |      |         |
| 基準 |      |     |      |         |
| 空方 |      |     |      |         |

**所有目標價必須說明計算依據，不得只給數字。**

---

# 二十一、第十七層：催化劑與風險

## 正向催化劑

列出未來 3～12 個月：

1.
2.
3.
4.
5.

並標明：

> 事件 → 對營收／毛利／EPS 的影響

## 主要風險

至少分析：

1. 需求下滑
2. ASP 下跌
3. 毛利率下降
4. 競爭加劇
5. CAPEX 過大
6. 客戶集中
7. 庫存增加
8. 匯率
9. 地緣政治
10. 估值壓縮

---

# 二十二、第十八層：最終「投資 × 交易」整合表

| 評估項目      | 結論 |
| --------- | -- |
| 商業模式      |    |
| 基本面       |    |
| 營收成長      |    |
| 獲利能力      |    |
| 資產負債表     |    |
| FCF       |    |
| 競爭優勢      |    |
| 管理層       |    |
| 產業供需      |    |
| 報價趨勢      |    |
| 技術趨勢      |    |
| 籌碼        |    |
| 估值        |    |
| 最大催化劑     |    |
| 最大風險      |    |
| 12個月多方目標價 |    |
| 12個月基準目標價 |    |
| 12個月空方目標價 |    |

---

# 二十三、第十九層：交易決策表

| 評估項目      | 結論與建議     |
| --------- | --------- |
| 現在是否適合買進？ |           |
| 已持股是否續抱？  |           |
| 是否適合加碼？   |           |
| 是否適合減碼？   |           |
| 是否需要停損？   |           |
| 適合操作型態    | 短線／波段／中長期 |
| 最佳進場區間    |           |
| 最佳停利區間    |           |
| 必守停損價     |           |
| 操作風險      | 低／中／高     |

---

# 二十四、第二十層：三大情境交易策略

## 情境一：強勢突破

條件：

* 突破哪個價格？
* 需要多少成交量？
* 法人是否同步買超？
* 突破後第一目標？
* 第二目標？
* 假突破如何辨識？

說明：

> 若突破成立，交易計畫如何調整。

---

## 情境二：拉回整理

條件：

* 第一支撐
* 第二支撐
* 20MA
* 60MA
* 前高／前低
* 成交量是否縮減

建立：

> 第一批／第二批／第三批

分批進場規則。

---

## 情境三：跌破支撐

明確定義：

* 哪個價格屬於警戒？
* 哪個價格屬於確認跌破？
* 減碼比例
* 停損條件
* 是否重新評估基本面

**禁止因「公司很好」而取消技術停損。**

---

# 二十五、第二十一層：最終研究結論

最後用「機構研究備忘錄」格式回答：

### 1. 投資核心論點

用 3～5 點說明：

> 為什麼這家公司值得／不值得進一步研究？

### 2. 市場目前可能錯估的地方

分成：

* 市場過度樂觀
* 市場過度悲觀
* 尚無法確認

### 3. 最大驗證指標

列出未來最需要追蹤的 3～5 個指標：

例如：

* 月營收
* 毛利率
* CAPEX
* 訂單
* ASP
* 庫存
* 法人持股
* 產能利用率

### 4. 失效條件

明確列出：

> 哪些事件發生後，目前投資論點必須重新評估？

---

# 二十六、研究紀律

必須遵守：

1. 不得捏造數據。
2. 不得把新聞推測當成公司事實。
3. 不得把券商預估當成官方數據。
4. 所有重要數字標明資料期間。
5. 所有即時資訊標明日期。
6. 財務數據優先使用 Tier 1。
7. 供需數據使用 Tier 1＋Tier 2 交叉驗證。
8. 若資料不足，直接寫「【資料不足，無法確認】」。
9. 不得使用模糊詞語掩蓋資料不足。
10. 技術分析不能取代基本面分析。
11. 基本面良好不能直接等於股價短線會上漲。
12. 股價強勢也不能直接等於企業價值合理。
13. 明確區分「事實、推估、情境」。
14. 所有目標價都必須提供計算邏輯。
15. 不使用情緒化、煽動性或保證獲利的語言。
16. 不得因單一指標直接下結論。
17. 若不同週期訊號互相矛盾，必須明確揭露。
18. 最終結論必須同時考慮「企業價值＋產業供需＋市場價格＋風險報酬」。

---

# 二十七、最終輸出順序

請嚴格依照以下順序輸出：

**① 摘要評等框**

↓

**② 商業模式**

↓

**③ 營收結構**

↓

**④ 5年獲利能力**

↓

**⑤ 資產負債表**

↓

**⑥ FCF與資本配置**

↓

**⑦ 競爭優勢與產業龍頭股對照**

↓

**⑧ 管理層**

↓

**⑨ 供需狀態矩陣**

↓

**⑩ 供需 × 財務傳導驗證**

↓

**⑪ 估值快照**

↓

**⑫ 技術面交易前八問**

↓

**⑬ 日／週／月線**

↓

**⑭ 籌碼面**

↓

**⑮ 心理偏誤**

↓

**⑯ 支撐／壓力／停損／停利**

↓

**⑰ 12個月多空情境目標價**

↓

**⑱ 催化劑與風險**

↓

**⑲ 投資 × 交易整合表**

↓

**⑳ 三大交易情境**

↓

**㉑ 最終研究結論**

---

# 最後一句核心原則

**先判斷「公司值多少」，再判斷「產業供需往哪裡走」，最後才判斷「現在的價格是否值得交易」。**

不要因為技術面強勢就忽略估值，也不要因為基本面優秀就忽略股價風險。

股票：${stockLine}

---

## 📤 輸出檔案格式要求

完成以上完整報告後，請將整份報告內容另外整理成一份**可直接下載的獨立 HTML 檔案**（單一檔案、內嵌所有 CSS 樣式、不依賴外部資源），版面需清楚適合列印或另存為 PDF（A4尺寸、清楚的標題階層、表格加框線、重點數字適度加粗）。
若你的介面支援產生檔案／Canvas／Artifact 等下載功能，請直接以檔案形式輸出該 HTML（或 PDF）供我下載；若不支援檔案下載，請完整印出該 HTML 原始碼（用程式碼區塊包住），我會自行另存為 .html 檔案，並可透過瀏覽器的「列印→另存為 PDF」功能轉存成 PDF。`;
}

function copyMacroPrompt() {
  const prompt = buildMacroPrompt();
  navigator.clipboard.writeText(prompt)
    .then(() => showToast('已複製完整提示詞'))
    .catch(() => showToast('複製失敗，請手動複製'));
}

function openMacroAi(target) {
  const urls = {
    gemini: 'https://gemini.google.com',
    chatgpt: 'https://chat.openai.com',
    perplexity: 'https://www.perplexity.ai',
  };
  const prompt = buildMacroPrompt();
  navigator.clipboard.writeText(prompt)
    .then(() => showToast('已複製提示詞，貼到分析頁面即可開始'))
    .catch(() => showToast('複製失敗，請手動複製'));
  window.open(urls[target] || urls.gemini, '_blank', 'noopener');
}

/* ── Help icon popover (tap-to-toggle for touch devices; desktop uses :hover) ── */
document.addEventListener('click', function(e) {
  if (e.target.closest('.help-popover')) return;
  const icon = e.target.closest('.help-icon');
  document.querySelectorAll('.help-icon.show').forEach(el => {
    if (el !== icon) el.classList.remove('show');
  });
  if (icon) icon.classList.toggle('show');
});

/* ── Announcement repeat-count badge popover (same tap-to-toggle pattern) ── */
document.addEventListener('click', function(e) {
  if (e.target.closest('.ann-repeat-popover')) return;
  const badge = e.target.closest('.ann-repeat-badge');
  document.querySelectorAll('.ann-repeat-badge.show').forEach(el => {
    if (el !== badge) el.classList.remove('show');
  });
  if (badge) badge.classList.toggle('show');
});

/* ── Crawler control ── */
async function runCrawler(task) {
  showToast(`已觸發：${task}，請稍候…`);
  try {
    const resp = await fetch(`/api/crawler/run/${task}`, { method: 'POST' });
    const info = await resp.json();
    if (info.detail) showToast(`${task} → ${info.detail}`);
    setTimeout(loadCrawlerStatus, 1500);

    // Update quarterly button label with target quarter
    if (task === 'quarterly' && info.detail) {
      const btn = document.getElementById('quarterly-btn');
      if (btn) btn.textContent = `更新季財報 (${info.detail.replace('quarterly ','')})`;
    }

    if (task === 'init') {
      const poll = setInterval(async () => {
        await loadStats();
        const r = await fetch('/api/stats').then(r => r.json());
        if (r.stocks > 0) { clearInterval(poll); loadMarketSummary(); }
      }, 8000);
    }

    // After monthly or quarterly finishes, auto-reload summary every 30s until new data appears
    if (task === 'monthly_revenue' || task === 'quarterly') {
      const db0 = await fetch('/api/stats').then(r => r.json());
      const key  = task === 'monthly_revenue' ? 'revenues' : 'quarterly';
      const poll = setInterval(async () => {
        const db1 = await fetch('/api/stats').then(r => r.json());
        if (db1[key] > db0[key]) {
          clearInterval(poll);
          await loadMarketSummary();
          sendNotify(`✅ ${TASK_LABEL[task] || task} 完成`, `新增 ${db1[key] - db0[key]} 筆資料`);
        }
        loadCrawlerStatus();
      }, 20000);
    }
  } catch (_) {
    showToast('呼叫失敗');
  }
}

/* ── Announcement view ── */
let _annData = [];
let _annPage = 1;
const _ANN_PAGE_SIZE = 50;

function _annTruncate(s, n) {
  if (!s) return '—';
  return s.length > n ? s.slice(0, n) + '…' : s;
}

function _annRatingDot(rating) {
  if (!rating) return '<span class="ann-dot-empty">—</span>';
  const cls = rating.includes('強烈') ? 'red' : rating.includes('建議') ? 'orange'
            : rating.includes('一般') ? 'yellow' : 'green';
  const emoji = rating.includes('強烈') ? '🔴' : rating.includes('建議') ? '🟠'
              : rating.includes('一般') ? '🟡' : '🟢';
  return `<span class="ann-dot ann-dot-${cls}" title="${rating}">${emoji}</span>`;
}

const _ANN_RATING_OPTIONS = ['🔴 強烈買進', '🟠 建議買進', '🟡 一般觀望', '🟢 需要小心'];

function _annRatingSelectHtml(i, rating) {
  const opts = ['<option value="">— 未評級</option>']
    .concat(_ANN_RATING_OPTIONS.map(r => `<option value="${r}" ${r === rating ? 'selected' : ''}>${r}</option>`));
  return `<select class="ann-rating-select" data-idx="${i}">${opts.join('')}</select>`;
}

function _annRepeatCellHtml(a) {
  if (a.repeat_count <= 1) return '<td class="td-center">首次</td>';
  const dates = a.repeat_dates || [];
  const rows = dates.map((d, idx) => {
    const isCurrent = idx === dates.length - 1;
    return `<p class="${isCurrent ? 'ann-repeat-current' : ''}">第${idx + 1}次：${d}</p>`;
  }).join('');
  return `<td class="td-center">
    <span class="ann-repeat-badge" title="點擊展開近90天歷次公告日期">
      🔁 第${a.repeat_count}次
      <span class="ann-repeat-popover">
        <strong>${a.name || a.stock_code} 近90天公告紀錄</strong>
        ${rows}
      </span>
    </span>
  </td>`;
}

function renderAnnRow(a, i) {
  return `<tr>
    <td>${a.announce_date}${a.announce_time ? ' ' + a.announce_time.slice(0, 5) : ''}</td>
    ${_annRepeatCellHtml(a)}
    <td><span class="stock-link" data-code="${a.stock_code}">${a.stock_code}</span></td>
    <td><span class="stock-link" data-code="${a.stock_code}">${a.name || ''}</span></td>
    <td><span class="ann-subject-link" data-idx="${i}">${_annTruncate(a.subject, 10)}</span></td>
    <td class="num">${fmt.price(a.price_at_announce)}</td>
    <td class="num">${fmt.eps(a.monthly_eps)}</td>
    <td class="num">${fmt.eps(a.prior_year_eps)}</td>
    <td class="num ${pctClass(a.eps_yoy)}">${fmt.pct(a.eps_yoy)}</td>
    <td class="td-center">${a.turnaround ? '🔥' : '—'}</td>
    <td class="num">${fmt.eps(a.estimated_annual_eps)}</td>
    <td class="num">${a.estimated_pe != null && a.estimated_pe > 0 ? Number(a.estimated_pe).toFixed(1) : '—'}</td>
    <td class="td-center ann-rating-cell">
      <button class="btn btn-sm ann-rating-copy-btn" data-idx="${i}" title="複製評級提示詞，貼到 ChatGPT 免費分析">📋</button>
      ${_annRatingSelectHtml(i, a.ai_rating)}
    </td>
    <td class="td-center"><a class="btn btn-sm ann-ai-link" href="https://gemini.google.com" target="_blank" rel="noopener" data-idx="${i}">🤖 AI分析</a></td>
    <td class="td-center"><button class="btn btn-sm ann-wl-add-btn" data-idx="${i}">⭐ 加入自選</button></td>
  </tr>`;
}

async function loadAnnouncements() {
  const tbody = document.getElementById('ann-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="15" class="ann-empty">載入中…</td></tr>';
  try {
    _annData = await fetch('/api/announcements/today').then(r => r.json());
    _annPage = 1;
    renderAnnTable();
  } catch (_) {
    tbody.innerHTML = '<tr><td colspan="15" class="ann-empty">載入失敗</td></tr>';
  }
}

function _annPagerHtml(totalPages) {
  if (totalPages <= 1) return '';
  const btn = (label, page, disabled, active) =>
    `<button class="ann-pager-btn ${active ? 'active' : ''}" ${disabled ? 'disabled' : ''} data-page="${page}">${label}</button>`;
  const windowSize = 2;
  const pages = new Set([1, totalPages]);
  for (let p = _annPage - windowSize; p <= _annPage + windowSize; p++) {
    if (p >= 1 && p <= totalPages) pages.add(p);
  }
  const sorted = [...pages].sort((a, b) => a - b);
  let html = btn('‹ 上頁', _annPage - 1, _annPage <= 1, false);
  let prev = 0;
  for (const p of sorted) {
    if (p - prev > 1) html += `<span class="ann-pager-ellipsis">…</span>`;
    html += btn(p, p, false, p === _annPage);
    prev = p;
  }
  html += btn('下頁 ›', _annPage + 1, _annPage >= totalPages, false);
  return html;
}

function renderAnnTable() {
  const tbody = document.getElementById('ann-tbody');
  const pager = document.getElementById('ann-pager');
  const countEl = document.getElementById('ann-count');
  if (!tbody) return;

  const total = _annData.length;
  const totalPages = Math.max(1, Math.ceil(total / _ANN_PAGE_SIZE));
  if (_annPage > totalPages) _annPage = totalPages;
  if (_annPage < 1) _annPage = 1;
  const start = (_annPage - 1) * _ANN_PAGE_SIZE;
  const pageRows = _annData.slice(start, start + _ANN_PAGE_SIZE);

  if (countEl) {
    countEl.textContent = total
      ? `共 ${total} 筆（第 ${_annPage}/${totalPages} 頁，每頁 ${_ANN_PAGE_SIZE} 筆）`
      : '共 0 筆';
  }

  tbody.innerHTML = total
    ? pageRows.map((a, localIdx) => renderAnnRow(a, start + localIdx)).join('')
    : '<tr><td colspan="15" class="ann-empty">近期無公告</td></tr>';

  if (pager) {
    pager.innerHTML = _annPagerHtml(totalPages);
    pager.querySelectorAll('.ann-pager-btn:not([disabled])').forEach(el => {
      el.addEventListener('click', () => {
        _annPage = +el.dataset.page;
        renderAnnTable();
        document.getElementById('ann-table').scrollIntoView({block: 'start', behavior: 'smooth'});
      });
    });
  }

  tbody.querySelectorAll('[data-code]').forEach(el => {
    el.addEventListener('click', () => {
      setDetailNavContext([...new Set(_annData.map(a => a.stock_code))], el.dataset.code);
      loadStockDetail(el.dataset.code);
    });
  });
  tbody.querySelectorAll('.ann-subject-link').forEach(el => {
    el.addEventListener('click', () => openAnnModal(+el.dataset.idx));
  });
  tbody.querySelectorAll('.ann-ai-link').forEach(el => {
    el.addEventListener('click', () => copyAnnForAI(+el.dataset.idx));
  });
  tbody.querySelectorAll('.ann-rating-copy-btn').forEach(el => {
    el.addEventListener('click', () => copyAnnRatingPrompt(+el.dataset.idx));
  });
  tbody.querySelectorAll('.ann-rating-select').forEach(el => {
    el.addEventListener('change', () => setAnnRating(+el.dataset.idx, el.value));
  });
  tbody.querySelectorAll('.ann-wl-add-btn').forEach(el => {
    el.addEventListener('click', () => addAnnToWatchlist(+el.dataset.idx));
  });
  document.querySelectorAll('#ann-table .ann-sortable').forEach(th => {
    const arrow = th.querySelector('.ann-sort-arrow');
    if (arrow) arrow.textContent = th.dataset.sort === _annSortField ? (_annSortDir > 0 ? ' ▲' : ' ▼') : '';
  });
}

/* ── Announcement table sorting ── */
let _annSortField = null, _annSortDir = 1;

function _annRatingRank(r) {
  if (!r) return null;
  if (r.includes('強烈')) return 4;
  if (r.includes('建議')) return 3;
  if (r.includes('一般')) return 2;
  return 1;
}

const _ANN_SORT_GETTERS = {
  date:        a => `${a.announce_date || ''} ${a.announce_time || ''}`,
  repeat:      a => a.repeat_count || 0,
  code:        a => a.stock_code,
  name:        a => a.name || '',
  price:       a => a.price_at_announce,
  monthly_eps: a => a.monthly_eps,
  prior_eps:   a => a.prior_year_eps,
  yoy:         a => a.eps_yoy,
  turnaround:  a => a.turnaround ? 1 : 0,
  annual_eps:  a => a.estimated_annual_eps,
  pe:          a => (a.estimated_pe != null && a.estimated_pe > 0) ? a.estimated_pe : null,
  rating:      a => _annRatingRank(a.ai_rating),
};

function sortAnnTable(field) {
  const getter = _ANN_SORT_GETTERS[field];
  if (!getter) return;
  _annSortDir = (_annSortField === field) ? -_annSortDir : 1;
  _annSortField = field;
  _annData.sort((x, y) => {
    const vx = getter(x), vy = getter(y);
    const vxNull = vx == null || vx === '';
    const vyNull = vy == null || vy === '';
    if (vxNull || vyNull) return vxNull === vyNull ? 0 : (vxNull ? 1 : -1);
    if (typeof vx === 'string') return vx.localeCompare(vy) * _annSortDir;
    return (vx - vy) * _annSortDir;
  });
  _annPage = 1;
  renderAnnTable();
}

document.querySelectorAll('#ann-table thead .ann-sortable').forEach(th => {
  th.addEventListener('click', () => sortAnnTable(th.dataset.sort));
});

let _wlPickCode = null, _wlPickName = '';

function addAnnToWatchlist(i) {
  const a = _annData[i];
  if (!a) return;
  if (!state.user) { showToast('請先登入才能使用自選股'); return; }
  if (!state.watchlists.length) { showToast('請先建立一個自選股清單'); return; }
  if (state.watchlists.length === 1) {
    _addStockToWatchlist(state.watchlists[0], a.stock_code, a.name);
    return;
  }
  _wlPickCode = a.stock_code;
  _wlPickName = a.name || '';
  openWlPickModal();
}

async function _addStockToWatchlist(wl, code, name) {
  if (wl.codes.includes(code)) { showToast(`${code} 已在「${wl.name}」中`); return; }
  const ok = await _wlAddStockTo(wl, code);
  if (ok) showToast(`已加入 ${code} ${name || ''} 到「${wl.name}」`);
}

function openWlPickModal() {
  document.getElementById('wl-pick-list').innerHTML = state.watchlists.map(wl => `
    <div class="wl-pick-item" data-wl-id="${wl.id}">
      <span class="wl-pick-name">${wl.name}</span>
      <span class="wl-pick-meta">${wl.codes.includes(_wlPickCode) ? '已在清單中' : `${wl.codes.length} 支`}</span>
    </div>
  `).join('');
  document.getElementById('wl-pick-modal').classList.remove('hidden');
}

function closeWlPickModal() {
  document.getElementById('wl-pick-modal').classList.add('hidden');
}

document.getElementById('wl-pick-list').addEventListener('click', (e) => {
  const item = e.target.closest('.wl-pick-item');
  if (!item) return;
  const wl = state.watchlists.find(w => w.id === +item.dataset.wlId);
  if (!wl) return;
  _addStockToWatchlist(wl, _wlPickCode, _wlPickName);
  closeWlPickModal();
});

function openAnnModal(i) {
  const a = _annData[i];
  if (!a) return;
  document.getElementById('ann-modal-title').textContent = `${a.stock_code} ${a.name || ''}`;
  document.getElementById('ann-modal-body').innerHTML = `
    <div class="ann-modal-subject">${a.subject || ''}</div>
    <div class="ann-modal-date">${a.announce_date}</div>
    ${a.ai_rating ? `<div class="ann-modal-rating">${_annRatingDot(a.ai_rating)} ${a.ai_rating}</div>` : ''}
    ${a.ai_analysis ? `<div class="ann-modal-analysis">${a.ai_analysis}</div>` : ''}
    ${a.content
      ? `<hr class="ann-modal-divider"><pre class="ann-modal-content">${a.content}</pre>`
      : '<div class="ann-empty">（無詳細內容）</div>'}
  `;
  document.getElementById('ann-modal').classList.remove('hidden');
}

function closeAnnModal() {
  document.getElementById('ann-modal').classList.add('hidden');
}

function copyAnnForAI(i) {
  const a = _annData[i];
  if (!a) return;
  const stock = state.allData.find(s => s.code === a.stock_code);
  const priceLine = (stock && stock.close != null)
    ? `目前股價（資料庫最新收盤價，${stock.price_date || '—'}）：${stock.close} 元`
    : '目前股價：資料庫無此股票最新價格資料';
  const prompt = `你是一位擁有20年經驗，精通估值法的基金經理人。你的看法專業、深入、有獨特見解，你的專長是從海量且碎片化的資訊中，拼湊出供應鏈的真實、正確且有邏輯的樣貌。如果我給你股票代碼跟名稱以及重大公告資訊。重大公告資訊所代表的含義，並幫我分析這檔股票是否適合投資？今年目標價。
請執行以下自動化步驟:
步驟一:錨點搜尋(Auto-Anchor) 請聯網搜尋並列出同業競爭者目前的『預估本益比』。
步驟二:獲利探勘(EPS Mining) 請搜尋各大外資(如 Morgan Stanley) 對這檔股票今年全年的EPS預估值。
步驟三:定價計算 (The Pricing) 請設定20%的折價(Discount)作為安全邊際, 並依據公式算出:
便宜價(Burry 防線)
合理價(法人共識)
昂貴價(瘋狂價)
輸出要求:
請給我一個清晰的表格,標註目前的股價位於哪個區間
同時分析這檔股票近3個月的法人籌碼流向（外資與主力是否在暗中佈局），以及最近1個月是否有重大新聞影響其股價
由於我的投資是以中長期為主，請幫我用週線搭配日線的方式分析
分析他近期的營收是一次性獲利還是漲價或是缺貨或是其他原因
訂單量是否有實際增長
分析這檔股票有沒有與他相同產業或是與他相關的產業還沒上漲或是還有機會補漲的股票

股票代碼：${a.stock_code}
股票名稱：${a.name || ''}
${priceLine}

重大公告資訊：
${a.content || a.subject || ''}`;
  navigator.clipboard.writeText(prompt)
    .then(() => showToast('已複製提示詞，貼到 Gemini 即可分析'))
    .catch(() => showToast('複製失敗，請手動複製'));
}

/* ── 自結公告 AI 評級（免費版：複製提示詞給使用者自己貼到 ChatGPT，
   看完回覆後在下拉選單手動選評級，不再由爬蟲自動呼叫付費 API） ── */
function copyAnnRatingPrompt(i) {
  const a = _annData[i];
  if (!a) return;
  const known = `單月EPS：${a.monthly_eps ?? '無資料'}
去年同月EPS：${a.prior_year_eps ?? '無資料'}
EPS年增率：${a.eps_yoy ?? '無資料'}%
是否由虧轉盈：${a.turnaround ? '是' : '否'}
預估全年EPS：${a.estimated_annual_eps ?? '無資料'}
預估本益比：${a.estimated_pe ?? '無資料'}`;
  const prompt = `你是一位專業的台灣股票分析師，請根據提供的資料與你取得的最新網路資訊，進行簡潔明確的投資評分與風險提示。

【即時搜尋要求】
1. 你必須搜尋並引用該公司與其所屬產業的最新新聞與產業動態，不得只依賴我提供的公告內容。
2. 若找不到相關新聞，請明確說明「未能取得最新新聞，以下評估僅根據現有財務與公告資料」。
3. 當預估本益比 > 20 時，請特別搜尋產業與個股熱度，自行判斷是否屬於當前市場熱門題材。

【已知數據 — 直接採用，不要自己重新計算】
以下數據皆已從公告原文解析計算完成，請直接採用，不要自行重算或質疑正確性。
${known}

【評級標準 — 依優先順序綜合判斷】
🔴 強烈買進（路徑A或路徑B任一即可）：
  路徑A：預估本益比 ≤ 20 + EPS年增 > 0%（含由虧轉盈）+ 營收不衰退
  路徑B：預估本益比 > 20 + 有熱門題材支撐 + （EPS年增 > 30% 或 EPS成長率大幅優於營收成長率）
🟠 建議買進：EPS或營收正成長 + 預估本益比 ≤ 30，不需要強烈題材支撐
🟡 一般觀望：成長有限（年增 < 10%）或本益比 > 30 缺題材，或虧損但收窄中
🟢 需要小心：營收或EPS年減、財務惡化、由盈轉虧或衰退 > 30%

請給我：① 評級（🔴強烈買進／🟠建議買進／🟡一般觀望／🟢需要小心 四選一）② 4段分析文字（評級理由+數據、成長動能分析、產業熱度與風險、結論）。

股票：${a.name || ''}（${a.stock_code}）
主旨：${a.subject || ''}

公告說明：
${a.content || a.subject || ''}`;
  navigator.clipboard.writeText(prompt)
    .then(() => showToast('已複製評級提示詞，貼到 ChatGPT 看完回覆後回來選評級'))
    .catch(() => showToast('複製失敗，請手動複製'));
  window.open('https://chat.openai.com', '_blank', 'noopener');
}

async function setAnnRating(i, rating) {
  const a = _annData[i];
  if (!a) return;
  try {
    const resp = await fetch(`/api/announcements/${a.id}/rating`, {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rating: rating || null}),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      showToast(`更新失敗：${data.error || '未知錯誤'}`);
      renderAnnTable();
      return;
    }
    a.ai_rating = rating || '';
    showToast('評級已更新');
  } catch (_) {
    showToast('更新失敗，請稍後再試');
    renderAnnTable();
  }
}

/* ── 達人選股 ── */
let _expertList = [];
let _expertData = [];
let _expertKey = null;

async function loadExperts() {
  try {
    _expertList = await fetch('/api/experts').then(r => r.json());
  } catch (_) {
    document.getElementById('expert-tabs').innerHTML = '<span class="ann-empty">載入失敗</span>';
    return;
  }
  if (!_expertKey && _expertList.length) _expertKey = _expertList[0].expert_key;
  renderExpertTabs();
  if (_expertKey) loadExpertDetail(_expertKey);
}

function renderExpertTabs() {
  document.getElementById('expert-tabs').innerHTML = _expertList.map(e => `
    <button class="filter-btn ${e.expert_key === _expertKey ? 'active' : ''}" data-expert-key="${e.expert_key}">
      ${e.expert_label}（${e.passed_count}/${e.total}）${e.is_experimental ? '<span class="new-badge">NEW</span>' : ''}
    </button>
  `).join('');
  document.querySelectorAll('#expert-tabs [data-expert-key]').forEach(btn => {
    btn.addEventListener('click', () => {
      _expertKey = btn.dataset.expertKey;
      renderExpertTabs();
      loadExpertDetail(_expertKey);
    });
  });
}

function _isGutaiKey(key) {
  return key === 'gutai_bull' || key === 'gutai_bear';
}

function _isChanlunKey(key) {
  return key === 'chanlun_buy' || key === 'chanlun_sell';
}

/* 纏論買/賣點的「評分」永遠是 100/100（二元通過/不通過，見 experts.py
   _score_chanlun），數字本身沒有區分度——比起分數，使用者更想知道「這是
   第幾買/第幾賣」。breakdown 裡的 score 項目 label 是後端刻意固定的格式
   `「一/二/三買(賣) @ 價格（日期，N天前）」`（見 experts.py _score_chanlun），
   直接取空白前那一段就是類型，不用另外加後端欄位。*/
function _chanlunSignalLabel(s) {
  const item = (s.breakdown || []).find(b => b.type === 'score' && b.met);
  return item ? item.label.split(' ')[0] : '—';
}

// 「訊號類型」欄排序用：把一/二/三買(賣) 轉成 1/2/3 這樣的數字才排得動——
// 原本表頭沿用 data-sort="score"，但纏論的 score 固定都是100，排序等於
// 沒作用（使用者測試後回報「沒辦法排序」），改成纏論分頁時這個排序鍵
// 換算成訊號類型的序位（1買/1賣最早出現、3買/3賣確認度最高，見纏論說明）。
const _CHANLUN_TYPE_RANK = { '一買': 1, '二買': 2, '三買': 3, '一賣': 1, '二賣': 2, '三賣': 3 };
function _chanlunSignalRank(s) {
  const rank = _CHANLUN_TYPE_RANK[_chanlunSignalLabel(s)];
  return rank ?? null;
}

async function loadExpertDetail(key) {
  const tbody = document.getElementById('expert-tbody');
  const colspan = 15; // 14 shared cols + 1 (轉換 for gutai tabs, 殖利率 for the rest)
  tbody.innerHTML = `<tr><td colspan="${colspan}" class="ann-empty">載入中…</td></tr>`;
  try {
    _expertData = await fetch(`/api/experts/${key}`).then(r => r.json());
  } catch (_) {
    tbody.innerHTML = `<tr><td colspan="${colspan}" class="ann-empty">載入失敗</td></tr>`;
    return;
  }
  // Keep whatever sort the user had picked on a previous tab applied to the
  // freshly-fetched data for this ruleset, instead of silently reverting to
  // API order while the header arrow still claims a sort is active.
  if (_expertSortField) _applyExpertSort();
  const passedCount = _expertData.filter(s => s.passed).length;
  document.getElementById('expert-count').textContent = `符合選股標準 ${passedCount} / 共 ${_expertData.length} 支`;
  renderExpertTable();
}

/* ── Expert table sorting ── */
let _expertSortField = null, _expertSortDir = 1;

function _expertP(code) {
  return state.allData.find(d => d.code === code) || {};
}

const _EXPERT_SORT_GETTERS = {
  code:       s => s.code,
  name:       s => s.name || '',
  industry:   s => s.industry || '',
  rev:        s => { const p = _expertP(s.code); return (p.rev_year && p.rev_month) ? p.rev_year * 100 + p.rev_month : null; },
  start:      s => _expertP(s.code).start_price,
  close:      s => _expertP(s.code).close,
  diff:       s => _expertP(s.code).price_diff,
  chg:        s => _expertP(s.code).change_pct,
  score:      s => _isChanlunKey(_expertKey) ? _chanlunSignalRank(s) : s.score,
  est:        s => { const p = _expertP(s.code); const est = calcEst(p); return (est != null && p.close) ? est / p.close : null; },
  date:       s => _expertP(s.code).price_date,
  sweet:      s => sweetSpotCell(_expertP(s.code))[0],
  entered:    s => s.entered_at,
  transition: s => s.transition || '',
  yield:      s => _expertP(s.code).dividend_yield,
};

function _applyExpertSort() {
  const getter = _EXPERT_SORT_GETTERS[_expertSortField];
  if (!getter) return;
  _expertData.sort((x, y) => {
    const vx = getter(x), vy = getter(y);
    const vxNull = vx == null || vx === '';
    const vyNull = vy == null || vy === '';
    if (vxNull || vyNull) return vxNull === vyNull ? 0 : (vxNull ? 1 : -1);
    if (typeof vx === 'string') return vx.localeCompare(vy) * _expertSortDir;
    return (vx - vy) * _expertSortDir;
  });
}

function sortExpertTable(field) {
  if (!_EXPERT_SORT_GETTERS[field]) return;
  _expertSortDir = (_expertSortField === field) ? -_expertSortDir : 1;
  _expertSortField = field;
  _applyExpertSort();
  renderExpertTable();
}

document.querySelectorAll('#expert-table thead .ann-sortable').forEach(th => {
  th.addEventListener('click', () => sortExpertTable(th.dataset.sort));
});

function renderExpertTable() {
  const tbody = document.getElementById('expert-tbody');
  // 入榜日期對全部 8 套規則都有意義，一律顯示；轉換（空轉多/多轉空）只對
  // 股泰多方/空方訊號這一組有意義（見 experts.py ExpertScore 的
  // entered_at/transition 說明），其他規則不顯示這欄。
  const isGutai = _isGutaiKey(_expertKey);
  const isChanlun = _isChanlunKey(_expertKey);
  document.getElementById('expert-th-transition').classList.toggle('hidden', !isGutai);
  document.getElementById('expert-th-yield').classList.toggle('hidden', isGutai);
  document.getElementById('expert-th-score-label').textContent = isChanlun ? '訊號類型' : '評分';
  document.getElementById('expert-chanlun-note').classList.toggle('hidden', !isChanlun);
  const colspan = 15;

  if (!_expertData.length) {
    tbody.innerHTML = `<tr><td colspan="${colspan}" class="ann-empty">尚無資料，請先執行「達人選股資料」爬蟲</td></tr>`;
    return;
  }
  const rows = _expertData.filter(s => s.passed);
  tbody.innerHTML = rows.length
    ? rows.map((s, i) => {
        const p = state.allData.find(d => d.code === s.code) || {};
        const est = calcEst(p);
        const ratio = (est != null && p.close) ? est / p.close : null;
        return `
        <tr>
          <td>${i + 1}</td>
          <td><span class="stock-link" data-code="${s.code}">${s.code}</span></td>
          <td><span class="stock-link" data-code="${s.code}">${s.name}</span></td>
          <td>${s.industry || '—'}</td>
          <td>${(p.rev_year && p.rev_month) ? `${p.rev_year}/${String(p.rev_month).padStart(2,'0')}` : '—'}</td>
          <td class="num">${p.start_price != null ? fmt.price(p.start_price) : '—'}</td>
          <td class="num">${p.close != null ? fmt.price(p.close) : '—'}</td>
          <td class="num">${p.price_diff != null ? `<span class="${pctClass(p.price_diff)}">${fmt.pct(p.price_diff)}</span>` : '—'}</td>
          <td class="num">${p.change_pct != null ? `<span class="${pctClass(p.change_pct)}">${fmt.pct(p.change_pct)}</span>` : '—'}</td>
          <td class="num">${isChanlun ? `<b>${_chanlunSignalLabel(s)}</b>` : `${s.score} / ${s.max_score}`}${
            _expertKey === 'chanlun_star'
              ? ` <button class="btn btn-sm" onclick="openChanlunStarHistoryModal('${s.code}')">📊歷史</button>`
              : _expertKey === 'flag888_guyu'
                ? ` <button class="btn btn-sm" onclick="openFlag888GuyuHistoryModal('${s.code}')">📊歷史</button>`
                : ''
          }</td>
          <td class="num">${ratio != null ? ratio.toFixed(2) + 'x' : '—'}</td>
          <td>${p.price_date || '—'}</td>
          <td class="td-left">${sweetSpotCell(p)[1]}</td>
          <td>${s.entered_at || '—'}</td>
          ${isGutai
            ? `<td>${s.transition || '—'}</td>`
            : `<td class="num">${p.dividend_yield != null ? `${p.dividend_yield.toFixed(2)}%` : '—'}</td>`}
        </tr>
      `;
      }).join('')
    : `<tr><td colspan="${colspan}" class="ann-empty">目前沒有符合選股標準的個股</td></tr>`;
  tbody.querySelectorAll('[data-code]').forEach(el => {
    el.addEventListener('click', () => {
      // Land on the same ruleset tab the user was browsing from — without
      // this, loadStockExpertScores() would keep whatever tab was left
      // selected from some earlier, unrelated visit to the detail page.
      _stockExpertKey = _expertKey;
      setDetailNavContext(rows.map(s => s.code), el.dataset.code);
      loadStockDetail(el.dataset.code);
    });
  });
  document.querySelectorAll('#expert-table .ann-sortable').forEach(th => {
    const arrow = th.querySelector('.ann-sort-arrow');
    if (arrow) arrow.textContent = th.dataset.sort === _expertSortField ? (_expertSortDir > 0 ? ' ▲' : ' ▼') : '';
  });
}


/* ── 個股詳情頁：達人選股評分圖表 ── */
let _stockExpertData = [];
let _stockExpertKey = null;

/* ── 三大法人進出與持股（詳情頁，資料已由排程抓好，純讀取；可按需補齊） ── */
async function loadStockInstitutionalTrades(code) {
  document.getElementById('stock-institutional-card').classList.remove('hidden');
  const btn = document.getElementById('stock-institutional-refresh-btn');
  btn.disabled = false;
  btn.textContent = '補齊近5日資料';
  let rows = [];
  try {
    rows = await fetch(`/api/stocks/${code}/institutional-trades?days=90`).then(r => r.json());
  } catch (_) {
    rows = [];
  }
  renderInstitutionalTable(rows);
}

async function refreshStockInstitutionalTrades() {
  if (!state.currentCode) return;
  if (!state.user) { showToast('請先登入才能補齊三大法人資料'); return; }
  const btn = document.getElementById('stock-institutional-refresh-btn');
  btn.disabled = true;
  btn.textContent = '補齊中，請稍候…';
  try {
    const resp = await fetch(`/api/stocks/${state.currentCode}/institutional-trades/refresh`, {method: 'POST'});
    const data = await resp.json();
    if (!resp.ok) {
      showToast(`補齊失敗：${data.error || '未知錯誤'}`);
    } else {
      const rows = await fetch(`/api/stocks/${state.currentCode}/institutional-trades?days=90`).then(r => r.json());
      renderInstitutionalTable(rows);
      showToast('補齊完成');
    }
  } catch (_) {
    showToast('補齊失敗，請稍後再試');
  } finally {
    btn.disabled = false;
    btn.textContent = '補齊近5日資料';
  }
}

function _lots(shares) {
  // 股數 → 張（1張=1000股），四捨五入
  return Math.round((shares || 0) / 1000);
}

function _lotsCell(lots) {
  return `<span class="${pctClass(lots)}">${lots > 0 ? '+' : ''}${lots.toLocaleString()}</span>`;
}

let _institutionalTradeData = [];

function renderInstitutionalTable(rows) {
  _institutionalTradeData = rows;
  if (state.institutionalDt) { state.institutionalDt.destroy(); state.institutionalDt = null; }
  let cum = 0;
  const dtRows = rows.map(r => {
    const foreign = _lots((r.foreign_buy || 0) - (r.foreign_sell || 0));
    const trust   = _lots((r.trust_buy   || 0) - (r.trust_sell   || 0));
    const dealer  = _lots((r.dealer_buy  || 0) - (r.dealer_sell  || 0));
    const total   = foreign + trust + dealer;
    cum += total;
    return [r.date, foreign, trust, dealer, total, cum];
  }).reverse();
  const rowsHtml = dtRows.map(([date, foreign, trust, dealer, total, cum]) => [
    date, _lotsCell(foreign), _lotsCell(trust), _lotsCell(dealer), _lotsCell(total), _lotsCell(cum),
  ]);
  state.institutionalDt = $('#institutional-table').DataTable({
    data: rowsHtml, pageLength: 10, order: [], ordering: false,
    language: dtLang(), destroy: true, scrollX: true,
  });
}

/* ── 三大法人單一項目進出趨勢圖（點擊表格欄位標題觸發） ── */
const _INST_CHART_FIELDS = {
  foreign: { label: '外資買賣超', get: r => _lots((r.foreign_buy || 0) - (r.foreign_sell || 0)) },
  trust:   { label: '投信買賣超', get: r => _lots((r.trust_buy   || 0) - (r.trust_sell   || 0)) },
  dealer:  { label: '自營商買賣超', get: r => _lots((r.dealer_buy  || 0) - (r.dealer_sell  || 0)) },
  total:   { label: '三大法人合計買賣超', get: r =>
    _lots((r.foreign_buy || 0) - (r.foreign_sell || 0)) +
    _lots((r.trust_buy   || 0) - (r.trust_sell   || 0)) +
    _lots((r.dealer_buy  || 0) - (r.dealer_sell  || 0)) },
};

function openInstitutionalDetailChart(field) {
  if (!_institutionalTradeData.length) return;
  const rows = [..._institutionalTradeData].sort((a, b) => a.date < b.date ? -1 : a.date > b.date ? 1 : 0);
  const labels = rows.map(r => r.date);

  let title, datasets;
  if (field === 'cum') {
    // 累計持股增減本身就是跑動加總，直接畫這條線本身即可，不需要再疊一條「累計的累計」
    let running = 0;
    const cumData = rows.map(r => (running +=
      _INST_CHART_FIELDS.foreign.get(r) + _INST_CHART_FIELDS.trust.get(r) + _INST_CHART_FIELDS.dealer.get(r)));
    title = '🏛️ 累計持股增減 — 趨勢';
    datasets = [
      { type: 'line', label: '累計持股增減(張)', data: cumData, borderColor: getCssVar('--primary'),
        backgroundColor: getCssVar('--primary') + '22', fill: true, borderWidth: 2, pointRadius: 0, tension: 0.2, yAxisID: 'y' },
    ];
  } else {
    const cfg = _INST_CHART_FIELDS[field];
    if (!cfg) return;
    const net = rows.map(cfg.get);
    let running = 0;
    const cumulative = net.map(v => (running += v));
    const bgColors = net.map(v => v >= 0 ? getCssVar('--pos') + 'bb' : getCssVar('--neg') + 'bb');
    title = `🏛️ ${cfg.label} — 趨勢`;
    datasets = [
      { type: 'bar', label: `單日買賣超(張)`, data: net, backgroundColor: bgColors, yAxisID: 'y' },
      { type: 'line', label: '累計買賣超(張)', data: cumulative, borderColor: getCssVar('--primary'), borderWidth: 2, pointRadius: 0, tension: 0.2, yAxisID: 'y2' },
    ];
  }

  document.getElementById('institutional-detail-modal-title').textContent = title;
  document.getElementById('institutional-detail-modal').classList.remove('hidden');
  if (state.institutionalDetailChart) { state.institutionalDetailChart.destroy(); state.institutionalDetailChart = null; }
  requestAnimationFrame(() => {
    state.institutionalDetailChart = new Chart(document.getElementById('institutional-detail-chart-canvas'), {
      data: { labels, datasets },
      options: {
        ...chartOptions('張'),
        scales: field === 'cum' ? {
          x: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), maxRotation: 45, maxTicksLimit: labels.length > 30 ? 12 : labels.length } },
          y: { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
        } : {
          x:  { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), maxRotation: 45, maxTicksLimit: labels.length > 30 ? 12 : labels.length } },
          y:  { position: 'left',  grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
          y2: { position: 'right', grid: { drawOnChartArea: false },        ticks: { color: getCssVar('--primary') } },
        },
      },
    });
  });
}

function closeInstitutionalDetailModal() {
  document.getElementById('institutional-detail-modal').classList.add('hidden');
  if (state.institutionalDetailChart) { state.institutionalDetailChart.destroy(); state.institutionalDetailChart = null; }
}

document.getElementById('institutional-table').addEventListener('click', function (e) {
  const th = e.target.closest('th[data-field]');
  if (!th) return;
  openInstitutionalDetailChart(th.dataset.field);
});

/* ── 券商分點進出（詳情頁按需查詢，不再限制自選股） ── */
let _brokerTradeData = [];

async function loadStockBrokerTrades(code) {
  document.getElementById('stock-broker-card').classList.remove('hidden');
  const btn = document.getElementById('stock-broker-btn');
  btn.disabled = false;
  btn.textContent = '查詢近90天券商分點';
  try {
    _brokerTradeData = await fetch(`/api/stocks/${code}/broker-trades?days=90`).then(r => r.json());
  } catch (_) {
    _brokerTradeData = [];
  }
  _renderBrokerCardState();
}

function _renderBrokerCardState() {
  const empty = document.getElementById('stock-broker-empty');
  const wrap = document.getElementById('stock-broker-matrix-wrap');
  if (!Array.isArray(_brokerTradeData) || !_brokerTradeData.length) {
    empty.textContent = '尚無資料，點擊「查詢近90天券商分點」開始';
    empty.classList.remove('hidden');
    wrap.classList.add('hidden');
  } else {
    empty.classList.add('hidden');
    wrap.classList.remove('hidden');
    renderBrokerTrades();
  }
}

async function fetchStockBrokerTrades() {
  if (!state.currentCode) return;
  if (!state.user) { showToast('請先登入才能查詢券商分點'); return; }
  const btn = document.getElementById('stock-broker-btn');
  const empty = document.getElementById('stock-broker-empty');
  btn.disabled = true;
  btn.textContent = '查詢中，請稍候…';
  document.getElementById('stock-broker-matrix-wrap').classList.add('hidden');
  empty.textContent = '查詢中，請稍候（可能需要數十秒）…';
  empty.classList.remove('hidden');
  try {
    const resp = await fetch(`/api/stocks/${state.currentCode}/broker-trades/fetch`, {method: 'POST'});
    const data = await resp.json();
    if (!resp.ok) {
      empty.textContent = `查詢失敗：${data.error || '未知錯誤'}`;
    } else {
      _brokerTradeData = await fetch(`/api/stocks/${state.currentCode}/broker-trades?days=90`).then(r => r.json());
      _renderBrokerCardState();
      if (Array.isArray(_brokerTradeData) && _brokerTradeData.length) {
        showToast('查詢完成');
      } else {
        empty.textContent = '查無資料（可能該股票近期無成交，或 FinMind 尚未更新）';
      }
    }
  } catch (_) {
    empty.textContent = '查詢失敗，請稍後再試';
  } finally {
    btn.disabled = false;
    btn.textContent = '查詢近90天券商分點';
  }
}

/* ── 股利政策（詳情頁按需回補全部歷史） ── */
async function fetchStockDividend() {
  if (!state.currentCode) return;
  if (!state.user) { showToast('請先登入才能回補股利資料'); return; }
  const btn = document.getElementById('stock-dividend-btn');
  btn.disabled = true;
  btn.textContent = '回補中，請稍候…';
  try {
    const resp = await fetch(`/api/stocks/${state.currentCode}/dividend/fetch`, {method: 'POST'});
    const data = await resp.json();
    if (!resp.ok) {
      showToast(`回補失敗：${data.error || '未知錯誤'}`);
    } else {
      const fundamentals = await fetch(`/api/stocks/${state.currentCode}/fundamentals`).then(r => r.json()).catch(() => null);
      renderFundamentalsPanel(fundamentals);
      showToast('股利資料已更新至最新');
    }
  } catch (_) {
    showToast('回補失敗，請稍後再試');
  } finally {
    btn.disabled = false;
    btn.textContent = '回補最新資料';
  }
}

/* ── 甜蜜點訊號回測（詳情頁按需查詢） ── */
const _BACKTEST_TARGETS = [10, 15, 20, 25, 30];
const _BACKTEST_TIERS = ['ma20', 'ma60', 'ma120', 'ma240'];
let _backtestData = null;
let _backtestActiveTier = 'ma20';
let _backtestActiveTarget = 10;

function resetStockBacktestCard() {
  // Results are stock-specific and expensive to compute — don't auto-run on
  // every detail-page visit, but do clear out the *previous* stock's stale
  // results so switching stocks never shows the wrong one's numbers.
  _backtestData = null;
  _backtestActiveTier = 'ma20';
  _backtestActiveTarget = 10;
  document.getElementById('stock-backtest-body').classList.add('hidden');
  const empty = document.getElementById('stock-backtest-empty');
  empty.textContent = '點擊「開始回測」開始（單一股票計算，通常數秒內完成）';
  empty.classList.remove('hidden');
  document.querySelectorAll('#stock-backtest-tier-tabs .bt-tab').forEach(b =>
    b.classList.toggle('active', b.dataset.tier === 'ma20'));
  document.querySelectorAll('#stock-backtest-tabs .bt-tab').forEach(b =>
    b.classList.toggle('active', b.dataset.target === '10'));
  const btn = document.getElementById('stock-backtest-btn');
  btn.disabled = false;
  btn.textContent = '開始回測（近5年）';
}

function _renderBacktestSummary() {
  const summary = _backtestData.results[_backtestActiveTier].summary;
  document.getElementById('stock-backtest-summary').innerHTML = _BACKTEST_TARGETS.map(pct => {
    const s = summary[String(pct)];
    if (!s || !s.n_entries) return `
      <div class="health-tile"><div class="health-tile-label">+${pct}%</div><div class="health-tile-value">—</div></div>`;
    return `
      <div class="health-tile">
        <div class="health-tile-label">+${pct}% 目標（共${s.n_entries}次進場）</div>
        <div class="health-tile-value">${s.win_rate_pct}%</div>
        <div class="stock-ai-updated" style="margin-top:4px;">
          已出場（達標） ${s.n_hit} 次｜平均${s.avg_days_to_hit ?? '—'}天<br>
          未出場（持有中） ${s.n_open} 次${s.avg_open_return_pct != null ? `（現況 ${s.avg_open_return_pct > 0 ? '+' : ''}${s.avg_open_return_pct}%）` : ''}
        </div>
      </div>`;
  }).join('');
}

function _renderBacktestTable() {
  const tbody = document.getElementById('stock-backtest-tbody');
  const pct = _backtestActiveTarget;
  const trades = _backtestData.results[_backtestActiveTier].trades_by_target[String(pct)] || [];
  const tierLabel = _backtestData.tier_labels[_backtestActiveTier];
  tbody.innerHTML = trades.length ? trades.map(t => `
    <tr>
      <td>${t.date}</td>
      <td class="num">${t.price}</td>
      <td class="num">${t.ma ?? '—'}</td>
      <td>${t.hit ? t.exit_date : '持有中'}</td>
      <td class="num">${t.hit ? t.days + '天' : '—'}</td>
      <td class="num ${t.hit ? 'pos' : (t.open_return_pct > 0 ? 'pos' : t.open_return_pct < 0 ? 'neg' : '')}">${
        t.hit ? `+${pct}%` : (t.open_return_pct != null ? `${t.open_return_pct > 0 ? '+' : ''}${t.open_return_pct}%` : '—')
      }</td>
    </tr>
  `).join('') : `<tr><td colspan="6" class="ann-empty">近5年 +${pct}% 目標沒有出現${tierLabel}甜蜜點訊號</td></tr>`;
}

document.getElementById('stock-backtest-tier-tabs').addEventListener('click', function (e) {
  const btn = e.target.closest('.bt-tab');
  if (!btn || !_backtestData) return;
  _backtestActiveTier = btn.dataset.tier;
  this.querySelectorAll('.bt-tab').forEach(b => b.classList.toggle('active', b === btn));
  _renderBacktestSummary();
  _renderBacktestTable();
});

document.getElementById('stock-backtest-tabs').addEventListener('click', function (e) {
  const btn = e.target.closest('.bt-tab');
  if (!btn || !_backtestData) return;
  _backtestActiveTarget = Number(btn.dataset.target);
  this.querySelectorAll('.bt-tab').forEach(b => b.classList.toggle('active', b === btn));
  _renderBacktestTable();
});

async function runStockBacktest() {
  if (!state.currentCode) return;
  const btn = document.getElementById('stock-backtest-btn');
  const empty = document.getElementById('stock-backtest-empty');
  const body = document.getElementById('stock-backtest-body');
  btn.disabled = true;
  btn.textContent = '回測中，請稍候…';
  body.classList.add('hidden');
  empty.textContent = '回測中，請稍候（4種天期×5種目標，可能需要數秒）…';
  empty.classList.remove('hidden');
  try {
    const resp = await fetch(`/api/stocks/${state.currentCode}/backtest/sweet-spot?years=5`);
    const data = await resp.json();
    if (!resp.ok) {
      empty.textContent = `回測失敗：${data.error || '未知錯誤'}`;
    } else {
      empty.classList.add('hidden');
      body.classList.remove('hidden');
      _backtestData = data;
      _backtestActiveTier = 'ma20';
      _backtestActiveTarget = 10;
      document.querySelectorAll('#stock-backtest-tier-tabs .bt-tab').forEach(b =>
        b.classList.toggle('active', b.dataset.tier === 'ma20'));
      document.querySelectorAll('#stock-backtest-tabs .bt-tab').forEach(b =>
        b.classList.toggle('active', b.dataset.target === '10'));
      _renderBacktestSummary();
      _renderBacktestTable();
      const total = _BACKTEST_TIERS.reduce((sum, tier) => sum + _BACKTEST_TARGETS.reduce(
        (s2, pct) => s2 + (data.results[tier].summary[String(pct)]?.n_entries || 0), 0), 0);
      showToast(total ? '回測完成' : '回測完成，近5年四種甜蜜點都沒有出現訊號');
    }
  } catch (_) {
    empty.classList.remove('hidden');
    body.classList.add('hidden');
    empty.textContent = '回測失敗，請稍後再試';
  } finally {
    btn.disabled = false;
    btn.textContent = '開始回測（近5年）';
  }
}

/* ── 纏論買點+營收飆股 歷史訊號（詳情頁按需查詢，admin-only） ── */
function resetStockChanlunStarCard() {
  document.getElementById('stock-chanlun-star-body').classList.add('hidden');
  const empty = document.getElementById('stock-chanlun-star-empty');
  empty.textContent = '點擊「開始回測」開始（單一股票計算，因為要逐日重算纏論訊號，通常數秒內完成）';
  empty.classList.remove('hidden');
  const btn = document.getElementById('stock-chanlun-star-btn');
  btn.disabled = false;
  btn.textContent = '開始回測（近5年）';
}

function _exitReasonLabel(reason) {
  return { trailing: '移動停利', stop: '固定停損', open: '持有中' }[reason] || reason;
}

// 卡片版（個股詳情頁）跟 modal 版（達人選股 chanlun_star 分頁）共用這兩個
// HTML-builder，只是塞進去的容器不同——這樣兩處畫面永遠一致，不用維護兩份
//重複的樣板。
function _chanlunStarSummaryHtml(s) {
  if (!s.n_trades_total) {
    return `<div class="health-tile"><div class="health-tile-label">訊號次數</div><div class="health-tile-value">0</div></div>`;
  }
  return `
    <div class="health-tile">
      <div class="health-tile-label">已平倉 ${s.n_closed} 次${s.n_still_open ? `（另有 ${s.n_still_open} 次持有中）` : ''}</div>
      <div class="health-tile-value">${s.win_rate_pct ?? '—'}%</div>
      <div class="stock-ai-updated" style="margin-top:4px;">
        勝率（已實現報酬&gt;0 的比例）<br>
        移動停利出場 ${s.n_trailing_exit ?? 0} 次｜固定停損出場 ${s.n_stop_exit ?? 0} 次
      </div>
    </div>
    <div class="health-tile">
      <div class="health-tile-label">平均報酬 / 中位數</div>
      <div class="health-tile-value">${s.avg_return_pct != null ? `${s.avg_return_pct > 0 ? '+' : ''}${s.avg_return_pct}%` : '—'}</div>
      <div class="stock-ai-updated" style="margin-top:4px;">
        中位數 ${s.median_return_pct != null ? `${s.median_return_pct > 0 ? '+' : ''}${s.median_return_pct}%` : '—'}｜平均持有 ${s.avg_holding_days ?? '—'} 天
      </div>
    </div>`;
}

function _chanlunStarTradesHtml(trades, years, signalLabel) {
  signalLabel = signalLabel || '纏論買點+營收飆股';
  return trades.length ? [...trades].reverse().map(t => `
    <tr>
      <td>${t.entry_date}</td>
      <td class="num">${fmt.price(t.entry_price)}</td>
      <td>${t.exit_date || '持有中'}</td>
      <td class="num">${fmt.price(t.exit_price)}</td>
      <td>${_exitReasonLabel(t.exit_reason)}</td>
      <td class="num">${t.holding_days}天</td>
      <td class="num ${t.return_pct > 0 ? 'pos' : t.return_pct < 0 ? 'neg' : ''}">${
        t.return_pct != null ? `${t.return_pct > 0 ? '+' : ''}${t.return_pct}%` : '—'
      }</td>
    </tr>
  `).join('') : `<tr><td colspan="7" class="ann-empty">近${years}年沒有出現${signalLabel}訊號</td></tr>`;
}

function _renderChanlunStarResult(data) {
  document.getElementById('stock-chanlun-star-summary').innerHTML = _chanlunStarSummaryHtml(data.summary);
  document.getElementById('stock-chanlun-star-tbody').innerHTML = _chanlunStarTradesHtml(data.trades || [], data.years);
}

async function runStockChanlunStarBacktest() {
  if (!state.currentCode) return;
  const btn = document.getElementById('stock-chanlun-star-btn');
  const empty = document.getElementById('stock-chanlun-star-empty');
  const body = document.getElementById('stock-chanlun-star-body');
  btn.disabled = true;
  btn.textContent = '回測中，請稍候…';
  body.classList.add('hidden');
  empty.textContent = '回測中，請稍候（逐日重算纏論訊號，可能需要數秒）…';
  empty.classList.remove('hidden');
  try {
    const resp = await fetch(`/api/stocks/${state.currentCode}/backtest/chanlun-star?years=5`);
    const data = await resp.json();
    if (!resp.ok) {
      empty.textContent = `回測失敗：${data.error || '未知錯誤'}`;
    } else {
      empty.classList.add('hidden');
      body.classList.remove('hidden');
      _renderChanlunStarResult(data);
      // 使用者要求「在圖表中顯示出來，包含過去的歷史記錄」——存進 state 後
      // 重繪股價圖，renderPriceChart() 會偵測到 state.chanlunStarTrades 疊上
      // 藍色（進場）/橘色（出場）星形標記，見該函式內的說明。
      state.chanlunStarTrades = data.trades;
      if (state.prices) renderPriceChart(state.prices);
      showToast(data.trades.length ? '回測完成' : '回測完成，近5年沒有出現訊號');
    }
  } catch (_) {
    empty.classList.remove('hidden');
    body.classList.add('hidden');
    empty.textContent = '回測失敗，請稍後再試';
  } finally {
    btn.disabled = false;
    btn.textContent = '開始回測（近5年）';
  }
}

/* 達人選股 chanlun_star 分頁「📊歷史」按鈕 → modal（跟上面個股詳情頁卡片
   共用 _chanlunStarSummaryHtml()/_chanlunStarTradesHtml()，同一支 API，
   同一套訊號/出場定義，只是容器換成 modal，方便在清單頁直接看，不用先跳
   去個股詳情頁）。*/
async function openChanlunStarHistoryModal(code) {
  const modal = document.getElementById('chanlun-star-history-modal');
  const title = document.getElementById('chanlun-star-history-title');
  const summaryEl = document.getElementById('chanlun-star-history-summary');
  const tbody = document.getElementById('chanlun-star-history-tbody');
  const stock = state.allData.find(d => d.code === code);
  title.textContent = `${code} ${stock ? stock.name : ''} — 纏論買點+營收飆股 歷史訊號`;
  summaryEl.innerHTML = '';
  tbody.innerHTML = `<tr><td colspan="7" class="ann-empty">載入中…</td></tr>`;
  modal.classList.remove('hidden');
  try {
    const resp = await fetch(`/api/stocks/${code}/backtest/chanlun-star?years=5`);
    const data = await resp.json();
    if (!resp.ok) {
      tbody.innerHTML = `<tr><td colspan="7" class="ann-empty">載入失敗：${data.error || '未知錯誤'}</td></tr>`;
      return;
    }
    summaryEl.innerHTML = _chanlunStarSummaryHtml(data.summary);
    tbody.innerHTML = _chanlunStarTradesHtml(data.trades || [], data.years);
  } catch (_) {
    tbody.innerHTML = `<tr><td colspan="7" class="ann-empty">載入失敗，請稍後再試</td></tr>`;
  }
}

function closeChanlunStarHistoryModal() {
  document.getElementById('chanlun-star-history-modal').classList.add('hidden');
}

/* ── 888標準3+股魚 歷史訊號（詳情頁按需查詢，admin-only） ── */
// 直接重用 _chanlunStarSummaryHtml()/_chanlunStarTradesHtml()/_exitReasonLabel()
// ——summary/trades 的資料形狀跟 chanlun_star 完全一樣（見
// backtest_signal_combos.summarize()，兩邊共用同一個聚合函式），沒有理由
// 另外複製一份幾乎一樣的 HTML-builder。
function resetStockFlag888GuyuCard() {
  document.getElementById('stock-flag888-guyu-body').classList.add('hidden');
  const empty = document.getElementById('stock-flag888-guyu-empty');
  empty.textContent = '點擊「開始回測」開始';
  empty.classList.remove('hidden');
  const btn = document.getElementById('stock-flag888-guyu-btn');
  btn.disabled = false;
  btn.textContent = '開始回測（近5年）';
}

function _renderFlag888GuyuResult(data) {
  document.getElementById('stock-flag888-guyu-summary').innerHTML = _chanlunStarSummaryHtml(data.summary);
  document.getElementById('stock-flag888-guyu-tbody').innerHTML =
    _chanlunStarTradesHtml(data.trades || [], data.years, '888標準3+股魚');
}

async function runStockFlag888GuyuBacktest() {
  if (!state.currentCode) return;
  const btn = document.getElementById('stock-flag888-guyu-btn');
  const empty = document.getElementById('stock-flag888-guyu-empty');
  const body = document.getElementById('stock-flag888-guyu-body');
  btn.disabled = true;
  btn.textContent = '回測中，請稍候…';
  body.classList.add('hidden');
  empty.textContent = '回測中，請稍候…';
  empty.classList.remove('hidden');
  try {
    const resp = await fetch(`/api/stocks/${state.currentCode}/backtest/flag888-guyu?years=5`);
    const data = await resp.json();
    if (!resp.ok) {
      empty.textContent = `回測失敗：${data.error || '未知錯誤'}`;
    } else {
      empty.classList.add('hidden');
      body.classList.remove('hidden');
      _renderFlag888GuyuResult(data);
      state.flag888GuyuTrades = data.trades;
      if (state.prices) renderPriceChart(state.prices);
      showToast(data.trades.length ? '回測完成' : '回測完成，近5年沒有出現訊號');
    }
  } catch (_) {
    empty.classList.remove('hidden');
    body.classList.add('hidden');
    empty.textContent = '回測失敗，請稍後再試';
  } finally {
    btn.disabled = false;
    btn.textContent = '開始回測（近5年）';
  }
}

/* 達人選股 flag888_guyu 分頁「📊歷史」按鈕 → modal，跟 chanlun_star 那組
   同樣的容器切換模式，共用同一批 HTML-builder。*/
async function openFlag888GuyuHistoryModal(code) {
  const modal = document.getElementById('flag888-guyu-history-modal');
  const title = document.getElementById('flag888-guyu-history-title');
  const summaryEl = document.getElementById('flag888-guyu-history-summary');
  const tbody = document.getElementById('flag888-guyu-history-tbody');
  const stock = state.allData.find(d => d.code === code);
  title.textContent = `${code} ${stock ? stock.name : ''} — 888標準3+股魚 歷史訊號`;
  summaryEl.innerHTML = '';
  tbody.innerHTML = `<tr><td colspan="7" class="ann-empty">載入中…</td></tr>`;
  modal.classList.remove('hidden');
  try {
    const resp = await fetch(`/api/stocks/${code}/backtest/flag888-guyu?years=5`);
    const data = await resp.json();
    if (!resp.ok) {
      tbody.innerHTML = `<tr><td colspan="7" class="ann-empty">載入失敗：${data.error || '未知錯誤'}</td></tr>`;
      return;
    }
    summaryEl.innerHTML = _chanlunStarSummaryHtml(data.summary);
    tbody.innerHTML = _chanlunStarTradesHtml(data.trades || [], data.years, '888標準3+股魚');
  } catch (_) {
    tbody.innerHTML = `<tr><td colspan="7" class="ann-empty">載入失敗，請稍後再試</td></tr>`;
  }
}

function closeFlag888GuyuHistoryModal() {
  document.getElementById('flag888-guyu-history-modal').classList.add('hidden');
}

function _brokerLots(shares) {
  // 股數 → 張（1張=1000股），四捨五入
  return Math.round((shares || 0) / 1000);
}

function _brokerLotsCell(shares) {
  const lots = _brokerLots(shares);
  if (!lots) return '<td>—</td>';
  return `<td><span class="${pctClass(lots)}">${lots > 0 ? '+' : ''}${lots.toLocaleString()}</span></td>`;
}

function _brokerMatrixHtml(dates, brokers) {
  if (!brokers.length) return '<tr><td class="broker-trade-empty">無資料</td></tr>';
  const byBrokerDate = {};
  for (const r of _brokerTradeData) {
    (byBrokerDate[r.broker_id] || (byBrokerDate[r.broker_id] = {}))[r.date] =
      (r.buy_volume || 0) - (r.sell_volume || 0);
  }
  const head = `<thead><tr><th>日期</th>${brokers.map(b =>
    `<th class="broker-th-clickable" data-broker-id="${b.broker_id}" data-broker-name="${b.broker_name || b.broker_id}" title="點擊查看此分點進出趨勢圖">${b.broker_name || b.broker_id}</th>`
  ).join('')}</tr></thead>`;
  const rows = dates.map(d => {
    const cells = brokers.map(b => _brokerLotsCell(byBrokerDate[b.broker_id]?.[d])).join('');
    return `<tr><td>${d}</td>${cells}</tr>`;
  }).join('');
  const totalCells = brokers.map(b => _brokerLotsCell(b.net)).join('');
  const totalRow = `<tr class="broker-trade-total-row"><td>合計</td>${totalCells}</tr>`;
  return `${head}<tbody>${rows}${totalRow}</tbody>`;
}

function renderBrokerTrades() {
  const dates = [...new Set(_brokerTradeData.map(r => r.date))].sort().reverse();

  const cumMap = {};
  for (const r of _brokerTradeData) {
    const c = cumMap[r.broker_id] || (cumMap[r.broker_id] = {broker_id: r.broker_id, broker_name: r.broker_name, net: 0, activity: 0});
    c.net += (r.buy_volume || 0) - (r.sell_volume || 0);
    c.activity += (r.buy_volume || 0) + (r.sell_volume || 0);
  }
  const cum = Object.values(cumMap);
  const topBuy  = [...cum].sort((a, b) => b.net - a.net).slice(0, 15);
  const topSell = [...cum].sort((a, b) => a.net - b.net).slice(0, 15);

  document.getElementById('broker-buy-matrix-label').textContent  = `買超前15大券商（近${dates.length}日，單位：張）`;
  document.getElementById('broker-sell-matrix-label').textContent = `賣超前15大券商（近${dates.length}日，單位：張）`;
  document.getElementById('broker-buy-matrix').innerHTML  = _brokerMatrixHtml(dates, topBuy);
  document.getElementById('broker-sell-matrix').innerHTML = _brokerMatrixHtml(dates, topSell);
  renderBrokerConcentration(cum);
}

/* ── Broker concentration (Phase 3 chip-peak signal, display-only for now — see chip_peak.py roadmap) ── */
function renderBrokerConcentration(cum) {
  const el = document.getElementById('broker-concentration-summary');
  const totalActivity = cum.reduce((s, c) => s + c.activity, 0);
  if (!totalActivity) { el.innerHTML = ''; return; }
  const top5Activity = [...cum].sort((a, b) => b.activity - a.activity).slice(0, 5)
    .reduce((s, c) => s + c.activity, 0);
  const top5Pct = (top5Activity / totalActivity * 100).toFixed(1);
  el.innerHTML = `<span title="這段期間內，成交量（買超+賣超）最大的前5家券商分點，合計占所有參與分點總成交量的比例。比例越高代表交易集中在少數分點，可能有主力或大戶介入；比例低則接近分散的一般散戶交易。純資訊顯示，目前未併入籌碼峰 POC/VAH/VAL 的計算。">🔍 分點集中度：前5大分點合計占 <strong>${top5Pct}%</strong>（共 ${cum.length} 家分點參與）</span>`;
}

/* ── 單一分點進出趨勢圖（點擊矩陣表格欄位標題觸發） ── */
function openBrokerDetailChart(brokerId, brokerName) {
  const rows = _brokerTradeData
    .filter(r => r.broker_id === brokerId)
    .sort((a, b) => a.date < b.date ? -1 : a.date > b.date ? 1 : 0);
  if (!rows.length) return;

  document.getElementById('broker-detail-modal-title').textContent = `🏦 ${brokerName} — 買賣超趨勢`;
  document.getElementById('broker-detail-modal').classList.remove('hidden');

  const labels = rows.map(r => r.date);
  const net = rows.map(r => _brokerLots((r.buy_volume || 0) - (r.sell_volume || 0)));
  let running = 0;
  const cumulative = net.map(v => (running += v));
  const bgColors = net.map(v => v >= 0 ? getCssVar('--pos') + 'bb' : getCssVar('--neg') + 'bb');

  if (state.brokerDetailChart) { state.brokerDetailChart.destroy(); state.brokerDetailChart = null; }
  requestAnimationFrame(() => {
    state.brokerDetailChart = new Chart(document.getElementById('broker-detail-chart-canvas'), {
      data: {
        labels,
        datasets: [
          { type: 'bar', label: '單日買賣超(張)', data: net, backgroundColor: bgColors, yAxisID: 'y' },
          { type: 'line', label: '累計買賣超(張)', data: cumulative, borderColor: getCssVar('--primary'), borderWidth: 2, pointRadius: 0, tension: 0.2, yAxisID: 'y2' },
        ],
      },
      options: {
        ...chartOptions('張'),
        scales: {
          x:  { grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), maxRotation: 45, maxTicksLimit: labels.length > 30 ? 12 : labels.length } },
          y:  { position: 'left',  grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
          y2: { position: 'right', grid: { drawOnChartArea: false },        ticks: { color: getCssVar('--primary') } },
        },
      },
    });
  });
}

function closeBrokerDetailModal() {
  document.getElementById('broker-detail-modal').classList.add('hidden');
  if (state.brokerDetailChart) { state.brokerDetailChart.destroy(); state.brokerDetailChart = null; }
}

document.getElementById('stock-broker-matrix-wrap').addEventListener('click', function (e) {
  const th = e.target.closest('th[data-broker-id]');
  if (!th) return;
  openBrokerDetailChart(th.dataset.brokerId, th.dataset.brokerName);
});

async function loadStockExpertScores(code) {
  const card = document.getElementById('stock-expert-card');
  try {
    _stockExpertData = await fetch(`/api/stocks/${code}/expert-scores`).then(r => r.json());
  } catch (_) {
    card.classList.add('hidden');
    return;
  }
  const scored = _stockExpertData.filter(s => s.score != null);
  if (!scored.length) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');

  // Keep whichever ruleset tab the user already had selected (e.g. after
  // clicking prev/next to browse to a different stock) instead of resetting
  // it to this stock's own "first passed" ruleset on every navigation —
  // that reset made the selected tab appear to jump around at random as
  // you stepped through stocks. Only pick a fresh default when there's no
  // prior selection, or it doesn't apply to this stock.
  if (!scored.some(s => s.expert_key === _stockExpertKey)) {
    const preferred = scored.find(s => s.passed) || scored[0];
    _stockExpertKey = preferred.expert_key;
  }

  document.getElementById('stock-expert-tabs').innerHTML = scored.map(s => `
    <button class="filter-btn ${s.expert_key === _stockExpertKey ? 'active' : ''}" data-expert-key="${s.expert_key}">
      ${s.passed ? '✅ ' : ''}${s.expert_label}（${s.score}/${s.max_score}）${s.is_experimental ? '<span class="new-badge">NEW</span>' : ''}
    </button>
  `).join('');
  document.querySelectorAll('#stock-expert-tabs [data-expert-key]').forEach(btn => {
    btn.addEventListener('click', () => {
      _stockExpertKey = btn.dataset.expertKey;
      document.querySelectorAll('#stock-expert-tabs [data-expert-key]').forEach(b =>
        b.classList.toggle('active', b.dataset.expertKey === _stockExpertKey));
      renderStockExpertDetail();
    });
  });
  renderStockExpertDetail();
}

function renderStockExpertDetail() {
  const s = _stockExpertData.find(x => x.expert_key === _stockExpertKey);
  const critEl = document.getElementById('stock-expert-criteria');
  if (!s || !s.breakdown.length) {
    critEl.innerHTML = '<div class="ann-empty">尚無評分資料</div>';
    if (state.stockExpertChart) { state.stockExpertChart.destroy(); state.stockExpertChart = null; }
    return;
  }
  const metIcon = (met) => met === null ? '<span class="ann-dot-empty">—</span>' : (met ? '✅' : '❌');
  const selectItems = s.breakdown.filter(b => b.type === 'select');
  critEl.innerHTML = `
    <div class="stock-expert-total">${s.expert_label}　總分 <span class="stock-expert-total-num">${s.score} / ${s.max_score}</span> 分</div>
    <div class="ann-modal-subject">選股標準（${s.passed ? '✅ 全部符合' : '❌ 未全部符合'}）</div>
    <ul class="expert-criteria-list">
      ${selectItems.map(b => `<li>${metIcon(b.met)} ${b.label}</li>`).join('')}
    </ul>
    <div class="ann-modal-subject">評分明細（${s.score} / ${s.max_score} 分）</div>
  `;
  renderStockExpertChart(s.breakdown.filter(b => b.type === 'score'));
}

function _renderExpertChart(scoreItems, canvasId, wrapId, chartKey) {
  const canvas = document.getElementById(canvasId);
  if (state[chartKey]) { state[chartKey].destroy(); state[chartKey] = null; }
  if (!scoreItems.length) return;

  const labels   = scoreItems.map(b => b.label.replace(/（[^）]*次）/, ''));
  const gained   = scoreItems.map(b => b.met === null ? 0 : (b.gained != null ? b.gained : (b.met ? b.points : 0)));
  const missing  = scoreItems.map((b, i) => Math.max(0, b.points - gained[i]));
  const barColor = scoreItems.map(b => b.met === null ? getCssVar('--text2') : (b.met ? getCssVar('--pos') : getCssVar('--neg')));

  document.getElementById(wrapId).style.height = `${Math.max(240, scoreItems.length * 26)}px`;

  state[chartKey] = new Chart(canvas, {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { label: '取得分數', data: gained, backgroundColor: barColor, stack: 's' },
        { label: '未取得/上限', data: missing, backgroundColor: getCssVar('--border'), stack: 's' },
      ],
    },
    options: {
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { labels: { color: getCssVar('--text2'), boxWidth: 12 } },
        tooltip: { backgroundColor: getCssVar('--bg3'), titleColor: getCssVar('--text'), bodyColor: getCssVar('--text2'), borderColor: getCssVar('--border'), borderWidth: 1 },
      },
      scales: {
        x: { stacked: true, grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
        y: { stacked: true, grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), font: { size: 11 } } },
      },
    },
  });
}

function renderStockExpertChart(scoreItems) {
  _renderExpertChart(scoreItems, 'stock-expert-chart', 'stock-expert-chart-wrap', 'stockExpertChart');
}

/* ── Crawler status panel ── */
document.getElementById('status-btn').addEventListener('click', () => {
  const panel = document.getElementById('status-panel');
  panel.classList.toggle('hidden');
  if (!panel.classList.contains('hidden')) loadCrawlerStatus();
});

function closeStatus() {
  document.getElementById('status-panel').classList.add('hidden');
}

async function loadCrawlerStatus() {
  try {
    const r = await fetch('/api/crawler/status');
    const logs = await r.json();
    const el = document.getElementById('status-logs');
    el.innerHTML = logs.map(l => `
      <div class="log-item">
        <div class="log-dot ${l.status}"></div>
        <div>
          <div class="log-task">${l.task}</div>
          <div class="log-msg">${l.message || ''}</div>
        </div>
        <div class="log-time">${l.created_at.slice(0, 16)}</div>
      </div>
    `).join('');
  } catch (_) {}
}

/* ── Today updates panel ── */
document.getElementById('today-btn').addEventListener('click', () => {
  const panel = document.getElementById('today-panel');
  panel.classList.toggle('hidden');
  if (!panel.classList.contains('hidden')) loadTodayUpdates();
});

function closeTodayPanel() {
  document.getElementById('today-panel').classList.add('hidden');
}

function _todayChips(list, lastChecked) {
  if (!list.length) {
    return lastChecked
      ? `<div class="today-empty">最後檢查：${_fmtCheckedTime(lastChecked)}</div>`
      : '<div class="today-empty">無</div>';
  }
  return `<div class="today-stock-list">${list.map(s =>
    `<span class="today-stock-chip" data-code="${s.code}">${s.code} ${s.name}</span>`
  ).join('')}</div>`;
}

function _fmtCheckedTime(t) {
  return t ? t.slice(0, 16) : '';
}

async function loadTodayUpdates() {
  try {
    const r = await fetch('/api/updates/today');
    const data = await r.json();
    const el = document.getElementById('today-logs');
    el.innerHTML = `
      <div class="today-section">
        <div class="today-section-title">📈 股價</div>
        ${data.price_date ? `<div>${data.price_date} 股價已更新</div>`
          : data.price_last_checked ? `<div class="today-empty">最後檢查：${_fmtCheckedTime(data.price_last_checked)}</div>`
          : '<div class="today-empty">尚未更新</div>'}
      </div>
      <div class="today-section">
        <div class="today-section-title">💰 月營收（${data.monthly_revenue.length}）</div>
        ${_todayChips(data.monthly_revenue, data.revenue_last_checked)}
      </div>
      <div class="today-section">
        <div class="today-section-title">📊 季財報（${data.quarterly.length}）</div>
        ${_todayChips(data.quarterly, data.quarterly_last_checked)}
      </div>
      <div class="today-section">
        <div class="today-section-title">📰 自結公告</div>
        ${data.ann_count > 0 ? `<div>今日新增 ${data.ann_count} 筆</div>`
          : data.ann_last_checked ? `<div class="today-empty">最後檢查：${_fmtCheckedTime(data.ann_last_checked)}</div>`
          : '<div class="today-empty">尚未更新</div>'}
      </div>
    `;
    const chips = el.querySelectorAll('.today-stock-chip');
    const codes = Array.from(chips).map(c => c.dataset.code);
    chips.forEach(chip => {
      chip.addEventListener('click', () => {
        closeTodayPanel();
        setDetailNavContext(codes, chip.dataset.code);
        loadStockDetail(chip.dataset.code);
      });
    });
  } catch (_) {}
}

/* ── Notifications ── */
const TASK_LABEL = {
  stock_list:      '股票清單更新',
  daily_price:     '今日股價更新',
  monthly_revenue: '月營收更新',
  quarterly:       '季財報更新',
  announcements:   '自結公告更新',
  stock_ai_analysis: 'AI個股分析',
  init:            '初始化',
};

function initNotifications() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function sendNotify(title, body = '') {
  if ('Notification' in window && Notification.permission === 'granted') {
    const n = new Notification(title, { body, icon: '/static/favicon.ico' });
    setTimeout(() => n.close(), 6000);
  }
  showToast(body ? `${title}：${body}` : title, 4000);
}

// Background poller: detect any crawler "success" and notify
let _lastNotifiedLog = null;
async function pollCrawlerNotify() {
  try {
    const logs = await fetch('/api/crawler/status').then(r => r.json());
    if (!logs.length) return;
    const latest = logs[0];
    const key = `${latest.task}|${latest.created_at}`;
    if (latest.status === 'success' && key !== _lastNotifiedLog) {
      _lastNotifiedLog = key;
      const label = TASK_LABEL[latest.task] || latest.task;
      sendNotify(`✅ ${label} 完成`, latest.message || '');
    }
  } catch (_) {}
}

// Initialize baseline log before polling starts so existing logs don't trigger notifications
async function initNotifyPoller() {
  try {
    const logs = await fetch('/api/crawler/status').then(r => r.json());
    if (logs.length) {
      const latest = logs[0];
      _lastNotifiedLog = `${latest.task}|${latest.created_at}`;
    }
  } catch (_) {}
  setInterval(pollCrawlerNotify, 15000);
}

/* ── Auth ── */
async function initAuth() {
  try {
    const data = await fetch('/api/auth/me').then(r => r.json());
    state.user = data.user;
    if (state.user) {
      const wls = await fetch('/api/watchlists').then(r => r.json());
      state.watchlists = Array.isArray(wls) ? wls : [];
      state.activeWlId = state.watchlists[0]?.id || null;
    }
  } catch {}
  updateAuthUI();
}

function updateAuthUI() {
  const area = document.getElementById('auth-area');
  const isAdmin = !!(state.user && state.user.is_admin);
  document.querySelectorAll('.admin-only').forEach(el => el.classList.toggle('hidden', !isAdmin));
  document.querySelectorAll('.login-only').forEach(el => el.classList.toggle('hidden', !state.user));
  if (state.user) {
    area.innerHTML = `
      <span class="auth-user" title="${state.user.username}">${state.user.username}</span>
      <button class="auth-logout-btn" id="auth-logout-btn">登出</button>`;
    document.getElementById('auth-logout-btn').addEventListener('click', async () => {
      await fetch('/api/auth/logout', {method: 'POST'});
      state.user = null;
      state.watchlists = [];
      state.activeWlId = null;
      updateAuthUI();
      if (state.activeTab === 'watchlist') renderWatchlistView();
    });
  } else {
    area.innerHTML = `<button class="auth-login-btn" id="auth-login-btn">登入 / 註冊</button>`;
    document.getElementById('auth-login-btn').addEventListener('click', () => openAuthModal('login'));
  }
}

let _authMode = 'login';
function openAuthModal(mode) {
  _authMode = mode || 'login';
  document.querySelectorAll('.modal-tab').forEach(t =>
    t.classList.toggle('active', t.dataset.authTab === _authMode));
  document.getElementById('auth-submit').textContent = _authMode === 'login' ? '登入' : '註冊';
  document.getElementById('auth-username').value = '';
  document.getElementById('auth-password').value = '';
  const errEl = document.getElementById('auth-error');
  errEl.textContent = ''; errEl.classList.add('hidden');
  document.getElementById('auth-modal').classList.remove('hidden');
  setTimeout(() => document.getElementById('auth-username').focus(), 50);
}

document.getElementById('auth-modal-close').addEventListener('click', () =>
  document.getElementById('auth-modal').classList.add('hidden'));

/* ── About modal ── */
function initAboutDot() {
  if (!localStorage.getItem('about_seen')) {
    document.getElementById('about-dot').classList.remove('hidden');
  }
}
document.getElementById('about-btn').addEventListener('click', () => {
  document.getElementById('about-modal').classList.remove('hidden');
  localStorage.setItem('about_seen', '1');
  document.getElementById('about-dot').classList.add('hidden');
});
document.getElementById('about-modal-close').addEventListener('click', () =>
  document.getElementById('about-modal').classList.add('hidden'));
document.getElementById('about-modal').addEventListener('click', function(e) {
  if (e.target === this) this.classList.add('hidden');
});

document.getElementById('auth-modal').addEventListener('click', function(e) {
  if (e.target === this) this.classList.add('hidden');
});
document.querySelectorAll('#auth-modal .modal-tab').forEach(btn => {
  btn.addEventListener('click', function() {
    _authMode = this.dataset.authTab;
    document.querySelectorAll('#auth-modal .modal-tab').forEach(t => t.classList.toggle('active', t === this));
    document.getElementById('auth-submit').textContent = _authMode === 'login' ? '登入' : '註冊';
    document.getElementById('auth-error').classList.add('hidden');
  });
});

document.getElementById('auth-username').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('auth-password').focus();
});
document.getElementById('auth-password').addEventListener('keydown', e => {
  if (e.key === 'Enter') document.getElementById('auth-submit').click();
});

document.getElementById('auth-submit').addEventListener('click', async () => {
  const username = document.getElementById('auth-username').value.trim();
  const password = document.getElementById('auth-password').value;
  const errEl = document.getElementById('auth-error');
  errEl.classList.add('hidden');
  if (!username || !password) {
    errEl.textContent = '請填寫帳號與密碼'; errEl.classList.remove('hidden'); return;
  }
  try {
    const resp = await fetch(`/api/auth/${_authMode}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({username, password}),
    }).then(r => r.json());
    if (resp.error) { errEl.textContent = resp.error; errEl.classList.remove('hidden'); return; }
    state.user = {username: resp.username};
    const wls = await fetch('/api/watchlists').then(r => r.json());
    state.watchlists = Array.isArray(wls) ? wls : [];
    state.activeWlId = state.watchlists[0]?.id || null;
    document.getElementById('auth-modal').classList.add('hidden');
    updateAuthUI();
    if (state.activeTab === 'watchlist') renderWatchlistView();
  } catch { errEl.textContent = '連線失敗，請稍後再試'; errEl.classList.remove('hidden'); }
});

document.getElementById('wl-login-btn').addEventListener('click', () => openAuthModal('login'));

/* ── Message board ── */
function _escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function _renderMsgItem(m) {
  return `
    <div class="msg-item" data-id="${m.id}">
      <div class="msg-item-head">
        <span class="msg-item-user">${_escapeHtml(m.username)}</span>
        <span class="msg-item-time">${m.created_at}</span>
        ${m.can_delete ? '<button class="msg-item-del" title="刪除">✕</button>' : ''}
      </div>
      <div class="msg-item-content">${_escapeHtml(m.content)}</div>
    </div>`;
}

async function loadMessages() {
  const listEl = document.getElementById('msg-list');
  try {
    const msgs = await fetch('/api/messages').then(r => r.json());
    listEl.innerHTML = msgs.length
      ? msgs.map(_renderMsgItem).join('')
      : '<div class="msg-empty">還沒有留言，搶頭香吧！</div>';
    listEl.scrollTop = listEl.scrollHeight;
    _markMessagesSeen(msgs);
  } catch {
    listEl.innerHTML = '<div class="msg-empty">留言載入失敗</div>';
  }
}

function _markMessagesSeen(msgs) {
  if (msgs.length) localStorage.setItem('msg_last_seen_id', String(msgs[msgs.length - 1].id));
  document.getElementById('msg-dot').classList.add('hidden');
}

async function checkUnreadMessages() {
  try {
    const msgs = await fetch('/api/messages').then(r => r.json());
    if (!msgs.length) return;
    const lastSeen = Number(localStorage.getItem('msg_last_seen_id') || 0);
    const latest = msgs[msgs.length - 1].id;
    if (latest > lastSeen) document.getElementById('msg-dot').classList.remove('hidden');
  } catch {}
}

document.getElementById('msg-list').addEventListener('click', async function(e) {
  const btn = e.target.closest('.msg-item-del');
  if (!btn) return;
  const item = btn.closest('.msg-item');
  const id = item.dataset.id;
  try {
    const resp = await fetch(`/api/messages/${id}`, {method: 'DELETE'}).then(r => r.json());
    if (resp.ok) item.remove();
  } catch {}
});

document.getElementById('msg-btn').addEventListener('click', () => {
  document.getElementById('msg-panel').classList.toggle('open');
  document.getElementById('msg-input-wrap').classList.toggle('hidden', !state.user);
  document.getElementById('msg-login-prompt').classList.toggle('hidden', !!state.user);
  if (document.getElementById('msg-panel').classList.contains('open')) loadMessages();
});

document.getElementById('msg-panel-close').addEventListener('click', () =>
  document.getElementById('msg-panel').classList.remove('open'));

document.getElementById('msg-login-link').addEventListener('click', () => {
  document.getElementById('msg-panel').classList.remove('open');
  openAuthModal('login');
});

async function sendMessage() {
  const input = document.getElementById('msg-input');
  const content = input.value.trim();
  if (!content) return;
  const sendBtn = document.getElementById('msg-send');
  sendBtn.disabled = true;
  try {
    const resp = await fetch('/api/messages', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content}),
    }).then(r => r.json());
    if (resp.error) { showToast(resp.error); return; }
    const listEl = document.getElementById('msg-list');
    const empty = listEl.querySelector('.msg-empty');
    if (empty) empty.remove();
    listEl.insertAdjacentHTML('beforeend', _renderMsgItem(resp));
    listEl.scrollTop = listEl.scrollHeight;
    _markMessagesSeen([resp]);
    input.value = '';
  } catch {
    showToast('送出失敗，請稍後再試');
  } finally {
    sendBtn.disabled = false;
  }
}

document.getElementById('msg-send').addEventListener('click', sendMessage);
document.getElementById('msg-input').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

/* ── PWA: service worker ── */
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js');
  });
}

/* ── PWA: install button ── */
let deferredInstallPrompt = null;

function initInstallButton() {
  const btn = document.getElementById('install-app-btn');
  const isStandalone = window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone;
  if (isStandalone) return;
  if (/iphone|ipad|ipod/i.test(navigator.userAgent)) btn.classList.remove('hidden');
}

window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  deferredInstallPrompt = e;
  document.getElementById('install-app-btn').classList.remove('hidden');
});

window.addEventListener('appinstalled', () => {
  document.getElementById('install-app-btn').classList.add('hidden');
  deferredInstallPrompt = null;
});

function installApp() {
  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    deferredInstallPrompt.userChoice.then(() => { deferredInstallPrompt = null; });
    return;
  }
  if (/iphone|ipad|ipod/i.test(navigator.userAgent)) {
    showToast('請點擊 Safari 下方的分享按鈕，選擇「加入主畫面」', 4000);
  } else {
    showToast('此瀏覽器不支援安裝，請改用 Chrome', 3000);
  }
}

/* ── 期權籌碼分析 ── */
let _taifexFuturesInst = [];
let _taifexOptionInst = [];
let _taifexLargeTraders = [];
let _taifexOptionLargeTraders = [];
let _taifexSrByType = {};
let _taifexSrType = 'week3';
let _taifexEntity = '外資';
let _taifexCloseByDate = {};
let _taifexLoadSeq = 0;
let _taifexDailyDetail = [];
let _taifexDetailLoaded = false;

async function loadTaifexView() {
  // Guard against overlapping calls (e.g. user clicking away and back to
  // this tab before the previous load finished) clobbering each other's
  // shared module-level state — only the most recent call is allowed to render.
  const seq = ++_taifexLoadSeq;
  const days = 180;
  const [summary, futuresInst, optionInst, pcRatio, largeTraders, optionLargeTraders,
         srByType, dailyDetail] = await Promise.all([
    fetch('/api/taifex/summary').then(r => r.json()).catch(() => ({})),
    fetch(`/api/taifex/futures-institutional?days=${days}`).then(r => r.json()).catch(() => []),
    fetch(`/api/taifex/option-institutional?days=${days}`).then(r => r.json()).catch(() => []),
    fetch(`/api/taifex/pc-ratio?days=${days}`).then(r => r.json()).catch(() => []),
    fetch(`/api/taifex/large-traders?days=${days}`).then(r => r.json()).catch(() => []),
    fetch(`/api/taifex/option-large-traders?days=${days}`).then(r => r.json()).catch(() => []),
    fetch(`/api/taifex/support-resistance?days=${days}`).then(r => r.json())
      .catch(() => ({ week3: [], week5: [], month: [], next_month: [] })),
    fetch(`/api/taifex/daily-detail?days=${days}`).then(r => r.json()).catch(() => []),
  ]);
  if (seq !== _taifexLoadSeq) return;

  _taifexFuturesInst = futuresInst;
  _taifexOptionInst = optionInst;
  _taifexLargeTraders = largeTraders;
  _taifexOptionLargeTraders = optionLargeTraders;
  _taifexSrByType = srByType;
  _taifexDailyDetail = dailyDetail;
  _taifexDetailLoaded = false;
  _taifexCloseByDate = Object.fromEntries(dailyDetail.map(r => [r.date, r.close]));
  document.getElementById('taifex-detail-wrap').classList.add('hidden');
  document.getElementById('taifex-detail-tbody').innerHTML = '';

  renderTaifexSummary(summary);
  renderTaifexPcRatioChart(pcRatio);
  renderTaifexBullBearChart(_taifexLargeTraders);
  renderTaifexEntityFuturesChart(_taifexEntity);
  renderTaifexEntityOptionCharts(_taifexEntity);
  renderTaifexSrChart(_taifexSrByType[_taifexSrType]);
}

function renderTaifexSummary(s) {
  document.getElementById('taifex-date').textContent = s.date || '';
  const tableEl = document.getElementById('taifex-summary-table');

  if (!s.date) {
    renderTaifexGauge(null);
    document.getElementById('taifex-gauge-updated').textContent = '';
    renderTaifexGaugeMini(null);
    document.getElementById('taifex-gauge-mini-updated').textContent = '';
    tableEl.innerHTML = '<div class="stock-ai-body">尚無資料，請先在「⚙ 爬蟲狀態」或後台管理頁面觸發「期權籌碼」爬蟲</div>';
    renderTaifexVixGauge(null);
    document.getElementById('taifex-vix-updated').textContent = '';
    renderTaifexFearGreedGauge(null, null);
    document.getElementById('taifex-fg-updated').textContent = '';
    return;
  }

  const pc = s.pc_ratio || {};
  const fmt = v => v == null ? '—' : v.toLocaleString();
  const fmtSignedPct = v => v == null ? '—' : `${v >= 0 ? '+' : ''}${v}%`;
  const trend = s.bull_bear_ratio_pct == null ? '' : _bullBearZoneLabel(s.bull_bear_ratio_pct);

  // 兩份獨立 gauge：下面「今日摘要」的詳細版 + 最上方縮小三合一列的版本
  renderTaifexGauge(s.bull_bear_ratio_pct);
  document.getElementById('taifex-gauge-updated').textContent = `${s.date} 更新`;
  renderTaifexGaugeMini(s.bull_bear_ratio_pct);
  document.getElementById('taifex-gauge-mini-updated').textContent = `${s.date} 更新`;
  renderTaifexVixGauge(s.vix);
  document.getElementById('taifex-vix-updated').textContent = `${s.date} 更新`;
  renderTaifexFearGreedGauge(s.fear_greed_score, s.fear_greed_rating);
  document.getElementById('taifex-fg-updated').textContent = s.fear_greed_date ? `${s.fear_greed_date} 更新` : '';

  // Label/value table — 3 pairs per row, matching the source site's summary layout
  const rows = [
    ['期貨收盤', fmt(s.futures_close), '外資期貨淨部位', fmt(s.foreign_net), '十大淨部位(全部)', fmt(s.large_traders_net_top10)],
    ['漲跌', s.futures_spread != null ? `${s.futures_spread >= 0 ? '+' : ''}${s.futures_spread} (${s.futures_spread_per}%)` : '—',
      '自營商期貨淨部位', fmt(s.dealer_net), '十大淨部位(近月)', fmt(s.large_traders_net_top10_near_month)],
    ['PC Ratio(成交量)', pc.volume_ratio_pct != null ? pc.volume_ratio_pct + '%' : '—',
      '投信期貨淨部位', fmt(s.trust_net), '多空比(約)', fmtSignedPct(s.bull_bear_ratio_pct)],
    ['PC Ratio(未平倉)', pc.oi_ratio_pct != null ? pc.oi_ratio_pct + '%' : '—',
      '全市場未沖銷部位', fmt(s.market_open_interest), '趨勢', trend || '—'],
  ];
  tableEl.innerHTML = rows.map(cells => {
    let html = '';
    for (let i = 0; i < cells.length; i += 2) {
      html += `<div class="label">${cells[i]}</div><div class="value">${cells[i + 1]}</div>`;
    }
    return html;
  }).join('');
}

/* ── 大戶多空比 gauge：5 個等角度色塊＋指針＋外圈刻度數字，比照參考站
   「大戶期權多空比」的設計（-70~70、六個刻度 -70/-30/-10/10/30/70，分五個
   等寬色塊）。**刻度數字不是照抄參考站的 ±70**：那個尺度是配他們自己（未知
   演算法）的指標算出來的，跟本站 compute_bull_bear_ratio() 這個近似公式
   量級完全不同——查過本站實際歷史（152個交易日，2026-01-02~08-20）範圍只
   有 -11.9%~+9.0%，沿用 ±70 會把整個波動範圍壓縮成表尺的一小塊。改用 ±15
   （既有的 gauge 上限），刻度值等比例縮放參考站的 -70/-30/-10 比例
   （7:3:1）算出 -15/-6/-2，四捨五入成好記的整數。 ── */
const _BULL_BEAR_ZONES = [
  { max: -6, label: '反指標看多', color: '#2e7d32' },
  { max: -2, label: '偏多',       color: '#66bb6a' },
  { max: 2,  label: '中性',       color: '#f2c744' },
  { max: 6,  label: '偏空',       color: '#e8622c' },
  { max: 15, label: '過熱',       color: '#dc2626' },
];
const _BULL_BEAR_MIN = -15;

// Shared by both the gauge label and the "今日摘要" table's 趨勢 column — they
// used to disagree (table did a naive sign check while the gauge used the
// zone bands below), which read as a contradiction for values like +5.3%
// (sign check said 偏多, zone band said 偏空). Both now go through this.
function _bullBearZoneLabel(pct) {
  const { index } = _zoneFraction(pct, _BULL_BEAR_ZONES, _BULL_BEAR_MIN);
  return _BULL_BEAR_ZONES[index].label;
}

/* 大戶多空比 gauge 有兩份獨立 DOM/canvas（頂部縮小三合一列 + 下面「今日
   摘要」的詳細版），共用同一份繪圖邏輯，只是目標 element id 和
   state chart key 不同——不是同一個 chart 重複渲染兩次。*/
function _renderBullBearGauge(pct, canvasId, valueId, labelId, stateKey) {
  const canvas = document.getElementById(canvasId);
  const valueEl = document.getElementById(valueId);
  const labelEl = document.getElementById(labelId);
  if (state[stateKey]) { state[stateKey].destroy(); state[stateKey] = null; }

  if (pct == null) {
    valueEl.textContent = '—';
    valueEl.style.color = '';
    labelEl.textContent = '';
    return;
  }

  const { index, fraction } = _zoneFraction(pct, _BULL_BEAR_ZONES, _BULL_BEAR_MIN);
  const zone = _BULL_BEAR_ZONES[index];
  valueEl.textContent = `${pct >= 0 ? '+' : ''}${pct}%`;
  valueEl.style.color = zone.color;
  labelEl.textContent = zone.label;
  labelEl.style.color = zone.color;

  state[stateKey] = new Chart(canvas, {
    type: 'doughnut',
    data: {
      datasets: [{
        data: _BULL_BEAR_ZONES.map(() => 1),
        backgroundColor: _BULL_BEAR_ZONES.map(z => z.color),
        borderWidth: 0,
      }],
    },
    options: {
      circumference: 180,
      rotation: -90,
      cutout: '65%',
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 14, left: 20, right: 20 } },
      plugins: {
        legend: { display: false }, tooltip: { enabled: false },
        gaugeNeedle: { fraction },
        gaugeTicks: { labels: _zoneTickLabels(_BULL_BEAR_ZONES, _BULL_BEAR_MIN) },
      },
      animation: { duration: 400 },
    },
  });
}

function renderTaifexGauge(pct) {
  _renderBullBearGauge(pct, 'taifex-gauge-chart', 'taifex-gauge-value', 'taifex-gauge-trend', 'taifexGaugeChart');
}

function renderTaifexGaugeMini(pct) {
  _renderBullBearGauge(pct, 'taifex-gauge-mini-chart', 'taifex-gauge-mini-value', 'taifex-gauge-mini-trend', 'taifexGaugeMiniChart');
}

/* ── TW VIX gauge (5 fixed-width colored zones + needle, matches the
   reference site's meter design) — zone boundaries are the standard TW VIX
   reading bands (低波動/偏低/正常/偏高/高波動), not derived from our own
   data distribution like the bull/bear gauge above. ── */
const _VIX_ZONES = [
  { max: 10, label: '低波動', color: '#2e7d32' },
  { max: 15, label: '偏低',   color: '#66bb6a' },
  { max: 20, label: '正常',   color: '#f2c744' },
  { max: 30, label: '偏高',   color: '#f2994a' },
  { max: 50, label: '高波動', color: '#ef4444' },
];

/* 通用版「等角度分區」計算：不管每一區實際數值寬度多寬，5個區永遠平分
   180°半圓（跟 Fear & Greed 的線性 0-100 尺度刻意不同，見該區塊註解）。
   min 是整條量表的下限（VIX/Fear&Greed 是0，大戶多空比是-15）。*/
function _zoneFraction(v, zones, min) {
  const max = zones[zones.length - 1].max;
  const clamped = Math.max(min, Math.min(max, v));
  let lo = min;
  for (let i = 0; i < zones.length; i++) {
    if (clamped <= zones[i].max || i === zones.length - 1) {
      const hi = zones[i].max;
      const local = hi === lo ? 0 : (clamped - lo) / (hi - lo);
      return { index: i, fraction: (i + local) / zones.length };
    }
    lo = zones[i].max;
  }
}

/* 每一區的邊界值（min + 各區 max，共 zones.length+1 個）對應到外圈刻度
   數字，位置跟 _zoneFraction 用同一套等角度公式，故直接取 i/zones.length。*/
function _zoneTickLabels(zones, min) {
  const boundaries = [min, ...zones.map(z => z.max)];
  return boundaries.map((b, i) => ({ fraction: i / zones.length, text: String(b) }));
}

const _gaugeTicksPlugin = {
  id: 'gaugeTicks',
  afterDatasetsDraw(chart) {
    const opts = chart.config.options.plugins.gaugeTicks;
    if (!opts || !opts.labels) return;
    const meta = chart.getDatasetMeta(0);
    const arc = meta.data[0];
    if (!arc) return;
    const { x: cx, y: cy, outerRadius } = arc;
    const ctx = chart.ctx;
    ctx.save();
    ctx.font = '10px sans-serif';
    ctx.fillStyle = getCssVar('--text2');
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const r = outerRadius + 12;
    for (const { fraction, text } of opts.labels) {
      const angle = Math.PI * (1 + fraction);
      ctx.fillText(text, cx + r * Math.cos(angle), cy + r * Math.sin(angle));
    }
    ctx.restore();
  },
};
Chart.register(_gaugeTicksPlugin);

const _gaugeNeedlePlugin = {
  id: 'gaugeNeedle',
  afterDatasetsDraw(chart) {
    const frac = chart.config.options.plugins.gaugeNeedle && chart.config.options.plugins.gaugeNeedle.fraction;
    if (frac == null) return;
    const meta = chart.getDatasetMeta(0);
    const arc = meta.data[0];
    if (!arc) return;
    const { x: cx, y: cy, outerRadius } = arc;
    const angle = Math.PI * (1 + frac);
    const len = outerRadius * 0.92;
    const tipX = cx + len * Math.cos(angle);
    const tipY = cy + len * Math.sin(angle);
    const ctx = chart.ctx;
    ctx.save();
    ctx.strokeStyle = getCssVar('--text');
    ctx.lineWidth = 3;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(tipX, tipY);
    ctx.stroke();
    ctx.beginPath();
    ctx.arc(cx, cy, 5, 0, Math.PI * 2);
    ctx.fillStyle = getCssVar('--text');
    ctx.fill();
    ctx.restore();
  },
};
Chart.register(_gaugeNeedlePlugin);

function renderTaifexVixGauge(vix) {
  const canvas = document.getElementById('taifex-vix-gauge-chart');
  const valueEl = document.getElementById('taifex-vix-value');
  const statusEl = document.getElementById('taifex-vix-status');
  if (state.taifexVixGaugeChart) { state.taifexVixGaugeChart.destroy(); state.taifexVixGaugeChart = null; }

  if (vix == null) {
    valueEl.textContent = '—';
    valueEl.style.color = '';
    statusEl.textContent = '';
    return;
  }

  const { index, fraction } = _zoneFraction(vix, _VIX_ZONES, 0);
  const zone = _VIX_ZONES[index];
  valueEl.textContent = vix.toFixed(1);
  valueEl.style.color = zone.color;
  statusEl.textContent = zone.label;
  statusEl.style.color = zone.color;

  state.taifexVixGaugeChart = new Chart(canvas, {
    type: 'doughnut',
    data: {
      datasets: [{
        data: _VIX_ZONES.map(() => 1),
        backgroundColor: _VIX_ZONES.map(z => z.color),
        borderWidth: 0,
      }],
    },
    options: {
      circumference: 180,
      rotation: -90,
      cutout: '65%',
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 16, left: 20, right: 20 } },
      plugins: {
        legend: { display: false }, tooltip: { enabled: false },
        gaugeNeedle: { fraction },
        gaugeTicks: { labels: _zoneTickLabels(_VIX_ZONES, 0) },
      },
      animation: { duration: 400 },
    },
  });
}

function openTaifexVixModal() {
  document.getElementById('taifex-vix-modal').classList.remove('hidden');
  loadTaifexVixHistory(+document.getElementById('taifex-vix-range-select').value);
}

function closeTaifexVixModal() {
  document.getElementById('taifex-vix-modal').classList.add('hidden');
}

async function loadTaifexVixHistory(days) {
  const rows = await fetch(`/api/taifex/vix-history?days=${days}`).then(r => r.json()).catch(() => []);
  renderTaifexVixHistoryChart(rows);

  const statsEl = document.getElementById('taifex-vix-stats');
  const vixVals = rows.map(r => r.vix).filter(v => v != null);
  if (!vixVals.length) {
    statsEl.innerHTML = '<span>尚無資料</span>';
    return;
  }
  const avg = vixVals.reduce((a, b) => a + b, 0) / vixVals.length;
  const max = Math.max(...vixVals);
  const min = Math.min(...vixVals);
  const current = vixVals[vixVals.length - 1];
  statsEl.innerHTML = `
    <span>當前: <strong>${current.toFixed(1)}</strong> ｜ 區間平均: <strong>${avg.toFixed(1)}</strong></span>
    <span>區間最高: <strong>${max.toFixed(1)}</strong> ／ 最低: <strong>${min.toFixed(1)}</strong></span>
  `;
}

function renderTaifexVixHistoryChart(rows) {
  const canvas = document.getElementById('taifex-vix-history-chart');
  if (state.taifexVixHistoryChart) { state.taifexVixHistoryChart.destroy(); state.taifexVixHistoryChart = null; }

  state.taifexVixHistoryChart = new Chart(canvas, {
    data: {
      labels: rows.map(r => r.date),
      datasets: [
        {
          type: 'line', label: 'TW VIX', data: rows.map(r => r.vix),
          borderColor: '#ef4444', backgroundColor: 'rgba(239,68,68,.12)',
          fill: true, tension: 0.2, pointRadius: 0, yAxisID: 'y', borderWidth: 2,
        },
        {
          type: 'line', label: '期貨收盤價', data: rows.map(r => r.futures_close),
          borderColor: '#3b82f6', backgroundColor: 'transparent',
          fill: false, tension: 0.2, pointRadius: 2, pointBackgroundColor: '#3b82f6',
          yAxisID: 'y2', borderWidth: 2,
        },
      ],
    },
    options: {
      ...chartOptions(),
      scales: {
        y:  { position: 'left',  title: { display: true, text: 'TW VIX', color: getCssVar('--text2') }, grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
        y2: { position: 'right', title: { display: true, text: '期貨收盤價', color: getCssVar('--text2') }, grid: { drawOnChartArea: false }, ticks: { color: getCssVar('--text2') } },
      },
    },
  });
}

/* ── CNN Fear & Greed Index gauge — same needle-plugin mechanism as the TW
   VIX gauge above, but the source site's own design uses a LINEAR 0-100
   scale (unlike VIX's equal-angular zones), so the needle fraction is just
   score/100; only the colored zone widths follow CNN's real published
   boundaries (0/25/45/55/75/100). ── */
const _FEAR_GREED_ZONES = [
  { max: 25,  width: 25, color: '#b91c1c' },
  { max: 45,  width: 20, color: '#f2994a' },
  { max: 55,  width: 10, color: '#f2c744' },
  { max: 75,  width: 20, color: '#66bb6a' },
  { max: 100, width: 25, color: '#2e7d32' },
];
const _FEAR_GREED_RATING_LABELS = {
  'extreme fear':  { label: '極度恐懼', color: '#b91c1c' },
  'fear':          { label: '恐懼',     color: '#f2994a' },
  'neutral':       { label: '中性',     color: '#f2c744' },
  'greed':         { label: '貪婪',     color: '#66bb6a' },
  'extreme greed': { label: '極度貪婪', color: '#2e7d32' },
};

function renderTaifexFearGreedGauge(score, rating) {
  const canvas = document.getElementById('taifex-fg-gauge-chart');
  const valueEl = document.getElementById('taifex-fg-value');
  const statusEl = document.getElementById('taifex-fg-status');
  if (state.taifexFgGaugeChart) { state.taifexFgGaugeChart.destroy(); state.taifexFgGaugeChart = null; }

  if (score == null) {
    valueEl.textContent = '—';
    valueEl.style.color = '';
    statusEl.textContent = '';
    return;
  }

  const info = _FEAR_GREED_RATING_LABELS[rating] || { label: rating || '—', color: getCssVar('--text2') };
  const fraction = Math.max(0, Math.min(100, score)) / 100;
  valueEl.textContent = Math.round(score);
  valueEl.style.color = info.color;
  statusEl.textContent = info.label;
  statusEl.style.color = info.color;

  state.taifexFgGaugeChart = new Chart(canvas, {
    type: 'doughnut',
    data: {
      datasets: [{
        data: _FEAR_GREED_ZONES.map(z => z.width),
        backgroundColor: _FEAR_GREED_ZONES.map(z => z.color),
        borderWidth: 0,
      }],
    },
    options: {
      circumference: 180,
      rotation: -90,
      cutout: '65%',
      responsive: true,
      maintainAspectRatio: false,
      layout: { padding: { top: 16, left: 20, right: 20 } },
      plugins: {
        legend: { display: false }, tooltip: { enabled: false },
        gaugeNeedle: { fraction },
        // Fear & Greed 是線性 0-100 尺度（不是等角度分區，見上方
        // _FEAR_GREED_ZONES 註解），刻度位置直接用數值本身除以100，不能
        // 套用 _zoneTickLabels 的等角度公式。
        gaugeTicks: { labels: [0, 25, 45, 55, 75, 100].map(v => ({ fraction: v / 100, text: String(v) })) },
      },
      animation: { duration: 400 },
    },
  });
}

function openTaifexFearGreedModal() {
  document.getElementById('taifex-fg-modal').classList.remove('hidden');
  loadTaifexFearGreedHistory(+document.getElementById('taifex-fg-range-select').value);
}

function closeTaifexFearGreedModal() {
  document.getElementById('taifex-fg-modal').classList.add('hidden');
}

async function loadTaifexFearGreedHistory(days) {
  const rows = await fetch(`/api/taifex/fear-greed-history?days=${days}`).then(r => r.json()).catch(() => []);
  renderTaifexFearGreedHistoryChart(rows);

  const statsEl = document.getElementById('taifex-fg-stats');
  const vals = rows.map(r => r.score).filter(v => v != null);
  if (!vals.length) {
    statsEl.innerHTML = '<span>尚無資料</span>';
    return;
  }
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
  const max = Math.max(...vals);
  const min = Math.min(...vals);
  const current = vals[vals.length - 1];
  statsEl.innerHTML = `
    <span>當前: <strong>${current.toFixed(1)}</strong> ｜ 區間平均: <strong>${avg.toFixed(1)}</strong></span>
    <span>區間最高: <strong>${max.toFixed(1)}</strong> ／ 最低: <strong>${min.toFixed(1)}</strong></span>
  `;
}

function renderTaifexFearGreedHistoryChart(rows) {
  const canvas = document.getElementById('taifex-fg-history-chart');
  if (state.taifexFgHistoryChart) { state.taifexFgHistoryChart.destroy(); state.taifexFgHistoryChart = null; }

  state.taifexFgHistoryChart = new Chart(canvas, {
    data: {
      labels: rows.map(r => r.date),
      datasets: [
        {
          type: 'line', label: '期貨收盤價', data: rows.map(r => r.futures_close),
          borderColor: '#7f1d1d', backgroundColor: 'transparent',
          fill: false, tension: 0.2, pointRadius: 2, pointBackgroundColor: '#7f1d1d',
          yAxisID: 'y2', borderWidth: 2,
        },
        {
          type: 'line', label: 'Fear & Greed Index', data: rows.map(r => r.score),
          borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,.15)',
          fill: true, tension: 0.3, pointRadius: 0, yAxisID: 'y', borderWidth: 2,
        },
      ],
    },
    options: {
      ...chartOptions(),
      scales: {
        y:  { position: 'left',  title: { display: true, text: 'Fear & Greed Index', color: getCssVar('--text2') }, grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
        y2: { position: 'right', title: { display: true, text: '期貨收盤價', color: getCssVar('--text2') }, grid: { drawOnChartArea: false }, ticks: { color: getCssVar('--text2') } },
      },
    },
  });
}

function toggleChanlunGuide() {
  const body = document.getElementById('chanlun-guide-body');
  const btn = document.getElementById('chanlun-guide-toggle-btn');
  const show = body.classList.contains('hidden');
  body.classList.toggle('hidden', !show);
  btn.textContent = show ? '收合說明' : '展開說明';
}

function toggleTaifexDetail() {
  const wrap = document.getElementById('taifex-detail-wrap');
  const show = wrap.classList.contains('hidden');
  wrap.classList.toggle('hidden', !show);
  if (show && !_taifexDetailLoaded) {
    renderTaifexDetailTable();
    _taifexDetailLoaded = true;
  }
}

function renderTaifexDetailTable() {
  const tbody = document.getElementById('taifex-detail-tbody');
  const fmt = v => v == null ? '—' : v.toLocaleString();
  const fmtPct = v => v == null ? '—' : v + '%';
  const fmtSignedPct = v => v == null ? '—' : `${v >= 0 ? '+' : ''}${v}%`;
  if (!_taifexDailyDetail.length) {
    tbody.innerHTML = '<tr><td colspan="15" class="ann-empty">尚無資料</td></tr>';
    return;
  }
  tbody.innerHTML = _taifexDailyDetail.map(r => `
    <tr>
      <td>${r.date}</td>
      <td class="num">${fmt(r.close)}</td>
      <td class="num">${r.spread_per != null ? fmtSignedPct(r.spread_per) : '—'}</td>
      <td class="num">${fmt(r.foreign_net)}</td>
      <td class="num">${fmt(r.foreign_contract_equivalent)}</td>
      <td class="num">${fmt(r.dealer_net)}</td>
      <td class="num">${fmt(r.dealer_contract_equivalent)}</td>
      <td class="num">${fmt(r.trust_net)}</td>
      <td class="num">${fmt(r.trust_contract_equivalent)}</td>
      <td class="num">${fmtPct(r.volume_ratio_pct)}</td>
      <td class="num">${fmtPct(r.oi_ratio_pct)}</td>
      <td class="num">${fmt(r.large_traders_net_all)}</td>
      <td class="num">${fmt(r.large_traders_net_near_month)}</td>
      <td class="num">${fmtSignedPct(r.bull_bear_ratio_pct)}</td>
      <td class="td-center">${r.trend || '—'}</td>
    </tr>
  `).join('');
}

function renderTaifexPcRatioChart(rows) {
  const canvas = document.getElementById('taifex-pc-ratio-chart');
  if (state.taifexPcRatioChart) { state.taifexPcRatioChart.destroy(); state.taifexPcRatioChart = null; }
  if (!rows.length) return;
  state.taifexPcRatioChart = new Chart(canvas, {
    data: {
      labels: rows.map(r => r.date),
      datasets: [
        { type: 'bar', label: '成交量比%', data: rows.map(r => r.volume_ratio_pct),
          backgroundColor: getCssVar('--primary') + '99', borderColor: getCssVar('--primary'), borderWidth: 1, yAxisID: 'y' },
        { type: 'line', label: '未平倉比%', data: rows.map(r => r.oi_ratio_pct),
          borderColor: getCssVar('--pos'), borderWidth: 2, pointRadius: 0, tension: 0.2, yAxisID: 'y' },
        { type: 'line', label: '期貨收盤', data: rows.map(r => r.futures_close),
          borderColor: getCssVar('--text2'), borderWidth: 2, pointRadius: 0, tension: 0.2, yAxisID: 'y2' },
      ],
    },
    options: {
      ...chartOptions(),
      scales: {
        y:  { position: 'left',  grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), callback: v => v + '%' } },
        y2: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: getCssVar('--text2'), stepSize: 5000 } },
      },
    },
  });
}

function renderTaifexBullBearChart(rows) {
  // 收盤 & 大戶多空比：時間序列版的 gauge 數字（同一個
  // taifex_analysis.compute_bull_bear_ratio 公式，十大交易人期貨淨部位／
  // 全市場未沖銷部位），不是另外用選擇權算的獨立指標。
  const canvas = document.getElementById('taifex-option-bull-bear-chart');
  if (state.taifexOptionBullBearChart) { state.taifexOptionBullBearChart.destroy(); state.taifexOptionBullBearChart = null; }
  if (!rows.length) return;
  state.taifexOptionBullBearChart = new Chart(canvas, {
    data: {
      labels: rows.map(r => r.date),
      datasets: [
        { type: 'bar', label: '多空比%(約)', data: rows.map(r => r.bull_bear_ratio_pct),
          backgroundColor: rows.map(r => (r.bull_bear_ratio_pct == null || r.bull_bear_ratio_pct >= 0) ? getCssVar('--pos') + '99' : getCssVar('--neg') + '99'),
          borderColor: rows.map(r => (r.bull_bear_ratio_pct == null || r.bull_bear_ratio_pct >= 0) ? getCssVar('--pos') : getCssVar('--neg')),
          borderWidth: 1, yAxisID: 'y' },
        { type: 'line', label: '期貨收盤', data: rows.map(r => _taifexCloseByDate[r.date] ?? null),
          borderColor: getCssVar('--text2'), borderWidth: 2, pointRadius: 0, tension: 0.2, yAxisID: 'y2' },
      ],
    },
    options: {
      ...chartOptions(),
      scales: {
        y:  { position: 'left',  grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2'), callback: v => v + '%' } },
        y2: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: getCssVar('--text2'), stepSize: 5000 } },
      },
    },
  });
}

/* 身份別部位明細（外資／自營商／十大交易人分頁，比照原站架構） */

function renderTaifexEntityFuturesChart(entity) {
  const canvas = document.getElementById('taifex-entity-futures-chart');
  const titleEl = document.getElementById('taifex-entity-futures-title');
  if (state.taifexEntityFuturesChart) { state.taifexEntityFuturesChart.destroy(); state.taifexEntityFuturesChart = null; }

  let rows, positions;
  if (entity === '十大交易人') {
    titleEl.textContent = '期貨部位（淨部位，含期貨收盤價疊圖；十大交易人沒有約當大台——TAIFEX官方的大額交易人統計不公布小台/微台版本）';
    rows = _taifexLargeTraders;
    positions = rows.map(r => r.net_top10);
  } else {
    titleEl.textContent = '期貨部位（約當大台淨部位＝大台+小台/4+微台/20，含期貨收盤價疊圖）';
    rows = _taifexFuturesInst.filter(r => r.institutional_investors === entity);
    positions = rows.map(r => r.contract_equivalent);
  }
  if (!rows.length) return;
  const dates = rows.map(r => r.date);
  const closes = dates.map(d => _taifexCloseByDate[d] ?? null);
  state.taifexEntityFuturesChart = new Chart(canvas, {
    data: {
      labels: dates,
      datasets: [
        { type: 'bar', label: '淨部位(口)', data: positions,
          backgroundColor: getCssVar('--primary') + '99', borderColor: getCssVar('--primary'), borderWidth: 1, yAxisID: 'y' },
        { type: 'line', label: '期貨收盤', data: closes,
          borderColor: getCssVar('--text2'), borderWidth: 2, pointRadius: 0, tension: 0.2, yAxisID: 'y2' },
      ],
    },
    options: {
      ...chartOptions(),
      scales: {
        y:  { position: 'left',  grid: { color: getCssVar('--border') }, ticks: { color: getCssVar('--text2') } },
        y2: { position: 'right', grid: { drawOnChartArea: false }, ticks: { color: getCssVar('--text2'), stepSize: 5000 } },
      },
    },
  });
}

function _renderTaifexCallPutChart(canvasId, stateKey, callRows, putRows, valueFn, yLabel) {
  const canvas = document.getElementById(canvasId);
  canvas.classList.remove('hidden');
  if (state[stateKey]) { state[stateKey].destroy(); state[stateKey] = null; }
  if (!callRows.length && !putRows.length) return;
  const dates = [...new Set([...callRows.map(r => r.date), ...putRows.map(r => r.date)])].sort();
  const callByDate = Object.fromEntries(callRows.map(r => [r.date, valueFn(r)]));
  const putByDate = Object.fromEntries(putRows.map(r => [r.date, valueFn(r)]));
  state[stateKey] = new Chart(canvas, {
    type: 'line',
    data: {
      labels: dates,
      datasets: [
        { label: '買權', data: dates.map(d => callByDate[d] ?? null), borderColor: getCssVar('--primary'), borderWidth: 2, pointRadius: 0, tension: 0.2 },
        { label: '賣權', data: dates.map(d => putByDate[d] ?? null), borderColor: getCssVar('--neg'), borderWidth: 2, pointRadius: 0, tension: 0.2 },
      ],
    },
    options: chartOptions(yLabel),
  });
}

function renderTaifexEntityOptionCharts(entity) {
  const buyAmtCanvas = document.getElementById('taifex-entity-opt-buy-amt-chart');
  const sellAmtCanvas = document.getElementById('taifex-entity-opt-sell-amt-chart');
  const buyAmtEmpty = document.getElementById('taifex-entity-opt-buy-amt-empty');
  const sellAmtEmpty = document.getElementById('taifex-entity-opt-sell-amt-empty');

  if (entity === '十大交易人') {
    const callRows = _taifexOptionLargeTraders.filter(r => r.call_put === 'call');
    const putRows = _taifexOptionLargeTraders.filter(r => r.call_put === 'put');
    _renderTaifexCallPutChart('taifex-entity-opt-buy-vol-chart', 'taifexEntityOptBuyVolChart', callRows, putRows, r => r.buy_top10, '買方口數(口)');
    _renderTaifexCallPutChart('taifex-entity-opt-sell-vol-chart', 'taifexEntityOptSellVolChart', callRows, putRows, r => r.sell_top10, '賣方口數(口)');
    if (state.taifexEntityOptBuyAmtChart) { state.taifexEntityOptBuyAmtChart.destroy(); state.taifexEntityOptBuyAmtChart = null; }
    if (state.taifexEntityOptSellAmtChart) { state.taifexEntityOptSellAmtChart.destroy(); state.taifexEntityOptSellAmtChart = null; }
    buyAmtCanvas.classList.add('hidden');
    sellAmtCanvas.classList.add('hidden');
    buyAmtEmpty.classList.remove('hidden');
    sellAmtEmpty.classList.remove('hidden');
  } else {
    const rows = _taifexOptionInst.filter(r => r.institutional_investors === entity);
    const callRows = rows.filter(r => r.call_put === 'call');
    const putRows = rows.filter(r => r.call_put === 'put');
    _renderTaifexCallPutChart('taifex-entity-opt-buy-vol-chart', 'taifexEntityOptBuyVolChart', callRows, putRows, r => r.long_deal_volume, '買方口數(口)');
    _renderTaifexCallPutChart('taifex-entity-opt-sell-vol-chart', 'taifexEntityOptSellVolChart', callRows, putRows, r => r.short_deal_volume, '賣方口數(口)');
    _renderTaifexCallPutChart('taifex-entity-opt-buy-amt-chart', 'taifexEntityOptBuyAmtChart', callRows, putRows, r => r.long_deal_amount, '買方契約金額(元)');
    _renderTaifexCallPutChart('taifex-entity-opt-sell-amt-chart', 'taifexEntityOptSellAmtChart', callRows, putRows, r => r.short_deal_amount, '賣方契約金額(元)');
    buyAmtEmpty.classList.add('hidden');
    sellAmtEmpty.classList.add('hidden');
  }
}

function renderTaifexSrChart(rows) {
  const canvas = document.getElementById('taifex-sr-chart');
  if (state.taifexSrChart) { state.taifexSrChart.destroy(); state.taifexSrChart = null; }
  if (!rows || !rows.length) return;
  state.taifexSrChart = new Chart(canvas, {
    type: 'line',
    data: {
      labels: rows.map(r => r.date),
      datasets: [
        { label: '期貨收盤', data: rows.map(r => r.futures_close), borderColor: getCssVar('--text2'), borderWidth: 2, pointRadius: 0, tension: 0.2 },
        { label: '壓力(買權OI最大履約價)', data: rows.map(r => r.resistance), borderColor: getCssVar('--neg'), borderWidth: 2, pointRadius: 0, stepped: true },
        { label: '支撐(賣權OI最大履約價)', data: rows.map(r => r.support), borderColor: getCssVar('--pos'), borderWidth: 2, pointRadius: 0, stepped: true },
      ],
    },
    options: chartOptions('價位'),
  });
}

document.getElementById('taifex-entity-tabs').addEventListener('click', function (e) {
  const btn = e.target.closest('.bt-tab');
  if (!btn) return;
  _taifexEntity = btn.dataset.entity;
  this.querySelectorAll('.bt-tab').forEach(b => b.classList.toggle('active', b === btn));
  renderTaifexEntityFuturesChart(_taifexEntity);
  renderTaifexEntityOptionCharts(_taifexEntity);
});

document.getElementById('taifex-sr-tabs').addEventListener('click', function (e) {
  const btn = e.target.closest('.bt-tab');
  if (!btn) return;
  _taifexSrType = btn.dataset.type;
  this.querySelectorAll('.bt-tab').forEach(b => b.classList.toggle('active', b === btn));
  renderTaifexSrChart(_taifexSrByType[_taifexSrType]);
});

document.getElementById('taifex-vix-range-select').addEventListener('change', function () {
  loadTaifexVixHistory(+this.value);
});

document.getElementById('taifex-fg-range-select').addEventListener('change', function () {
  loadTaifexFearGreedHistory(+this.value);
});

/* ── 圖表放大檢視（點擊任一張期權籌碼圖表，比照原站行為） ── */
const _TAIFEX_CHART_STATE_KEYS = {
  'taifex-option-bull-bear-chart': 'taifexOptionBullBearChart',
  'taifex-pc-ratio-chart': 'taifexPcRatioChart',
  'taifex-entity-futures-chart': 'taifexEntityFuturesChart',
  'taifex-entity-opt-buy-vol-chart': 'taifexEntityOptBuyVolChart',
  'taifex-entity-opt-sell-vol-chart': 'taifexEntityOptSellVolChart',
  'taifex-entity-opt-buy-amt-chart': 'taifexEntityOptBuyAmtChart',
  'taifex-entity-opt-sell-amt-chart': 'taifexEntityOptSellAmtChart',
  'taifex-sr-chart': 'taifexSrChart',
};

function _taifexChartTitle(canvas) {
  const wrap = canvas.closest('.chart-wrap');
  const prev = wrap && wrap.previousElementSibling;
  if (prev && prev.tagName === 'H4') {
    const title = prev.textContent.trim();
    return canvas.id.startsWith('taifex-entity-') ? `${_taifexEntity} － ${title}` : title;
  }
  const h3 = canvas.closest('.card')?.querySelector('.card-header h3');
  return h3 ? h3.textContent.replace(/ⓘ[\s\S]*$/, '').trim() : '';
}

function openTaifexChartModal(canvas) {
  const stateKey = _TAIFEX_CHART_STATE_KEYS[canvas.id];
  const sourceChart = stateKey && state[stateKey];
  if (!sourceChart) return;
  document.getElementById('taifex-chart-modal-title').textContent = _taifexChartTitle(canvas);
  document.getElementById('taifex-chart-modal').classList.remove('hidden');
  if (state.taifexChartModalChart) { state.taifexChartModalChart.destroy(); state.taifexChartModalChart = null; }
  // Wait a frame before creating the chart — right after removing "hidden"
  // the modal hasn't been laid out yet, so Chart.js would measure the
  // container's stale/collapsed size and render tiny instead of enlarged.
  requestAnimationFrame(() => {
    state.taifexChartModalChart = new Chart(document.getElementById('taifex-chart-modal-canvas'), {
      type: sourceChart.config.type,
      data: sourceChart.config.data,
      options: sourceChart.config.options,
    });
  });
}

function closeTaifexChartModal() {
  document.getElementById('taifex-chart-modal').classList.add('hidden');
  if (state.taifexChartModalChart) { state.taifexChartModalChart.destroy(); state.taifexChartModalChart = null; }
}

document.getElementById('taifex-view').addEventListener('click', function (e) {
  const canvas = e.target.closest('.taifex-chart-clickable canvas');
  if (!canvas) return;
  openTaifexChartModal(canvas);
});

/* ── Init ── */
initTheme();
initInstallButton();
initNotifications();
initAboutDot();
loadMarketSummary();
initAuth();
setInterval(loadStats, 60000);
initNotifyPoller();
checkUnreadMessages();
setInterval(checkUnreadMessages, 15000);
