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
      columnDefs: [
        { targets: [3, 4, 5, 6, 7, 9, 10, 11, 12, 13], className: 'dt-right', type: 'num-cell' },
        { targets: [0, 1, 2, 8, 14, 15], className: 'dt-left' },
      ],
    });
    // Row click
    $('#stocks-table tbody').on('click', 'td', function() {
      const $link = $(this).find('[data-code]');
      const code = $link.data('code') || $(this).closest('tr').find('[data-code]').data('code');
      if (code) loadStockDetail(code);
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
async function loadStockDetail(code) {
  code = String(code);
  state.currentCode = code;
  showDetailView();

  // Stock info
  const info = state.allData.find(s => s.code === code) || {};
  document.getElementById('d-name').textContent     = info.name || code;
  document.getElementById('d-code').textContent     = code + (info.name ? ` ${info.name}` : '');
  document.getElementById('d-market').textContent   = info.market || '';
  document.getElementById('d-industry').textContent = info.industry || '';

  // Load data in parallel
  const [prices, revenues, financials] = await Promise.all([
    fetch(`/api/stocks/${code}/prices?days=90`).then(r => r.json()).catch(() => []),
    fetch(`/api/stocks/${code}/revenue`).then(r => r.json()).catch(() => []),
    fetch(`/api/stocks/${code}/financials`).then(r => r.json()).catch(() => []),
  ]);

  renderPriceChart(prices);
  renderPriceTable(prices);
  renderRevenueChart(revenues);
  renderRevenueTable(revenues);
  renderEpsChart(financials);
  renderQuarterlyTable(financials);
}

/* ── Days selector ── */
document.querySelectorAll('.days-btn').forEach(btn => {
  btn.addEventListener('click', async function() {
    if (!state.currentCode) return;
    document.querySelectorAll('.days-btn').forEach(b => b.classList.remove('active'));
    this.classList.add('active');
    const days = this.dataset.days;
    const prices = await fetch(`/api/stocks/${state.currentCode}/prices?days=${days}`)
      .then(r => r.json()).catch(() => []);
    renderPriceChart(prices);
    renderPriceTable(prices);
  });
});

/* ── Price chart ── */
function renderPriceChart(prices) {
  const canvas = document.getElementById('price-chart');
  if (state.priceChart) { state.priceChart.destroy(); state.priceChart = null; }
  if (!prices.length) return;

  const labels = prices.map(p => p.date);
  const closes = prices.map(p => p.close);
  const n = prices.length;

  // Determine x-axis tick density based on period length
  const maxTicks = n > 1000 ? 10 : n > 365 ? 12 : n > 90 ? 18 : 30;

  const opts = chartOptions('收盤價 (元)');
  opts.scales.x.ticks = { ...opts.scales.x.ticks, maxTicksLimit: maxTicks };
  if (n > 500) {
    opts.plugins.decimation = { enabled: true, algorithm: 'min-max' };
  }

  state.priceChart = new Chart(canvas, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        label: '收盤價',
        data:   closes,
        borderColor:     getCssVar('--primary'),
        backgroundColor: getCssVar('--primary') + '22',
        borderWidth:     n > 500 ? 1 : 2,
        pointRadius:     0,
        fill:            true,
        tension:         n > 500 ? 0 : 0.3,
      }],
    },
    options: opts,
  });
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
    language: dtLang(), destroy: true,
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
    data: rows, pageLength: 24, order: [],
    language: dtLang(), destroy: true,
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
    data: rows, pageLength: 20, order: [],
    language: dtLang(), destroy: true,
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
    if (state.activeTab === 'star') renderStarTable();
    if (state.activeTab === 'watchlist') renderWatchlistView();
    if (state.activeTab === 'ann') loadAnnouncements();
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
      columnDefs: [
        { targets: [3, 4, 5, 6, 7, 8, 10, 11, 12, 13], className: 'dt-right', type: 'num-cell' },
        { targets: [0, 1, 2, 9], className: 'dt-left' },
      ],
    });
    $('#star-table tbody').on('click', 'td', function() {
      const code = $(this).find('[data-code]').data('code') ||
                   $(this).closest('tr').find('[data-code]').data('code');
      if (code) loadStockDetail(code);
    });
  }
}

/* ── View switching ── */
function showDetailView() {
  document.getElementById('list-view').classList.remove('active');
  document.getElementById('star-view').classList.remove('active');
  document.getElementById('watchlist-view').classList.remove('active');
  document.getElementById('ann-view').classList.remove('active');
  document.getElementById('detail-view').classList.add('active');
  document.getElementById('page-tabs-bar').classList.add('hidden');
  window.scrollTo(0, 0);
}

function showListView() {
  document.getElementById('detail-view').classList.remove('active');
  document.getElementById('page-tabs-bar').classList.remove('hidden');
  const viewMap = { star: 'star-view', watchlist: 'watchlist-view', ann: 'ann-view' };
  document.getElementById(viewMap[state.activeTab] || 'list-view').classList.add('active');
  state.currentCode = null;
}

/* ── Watchlists ── */
let wlDt = null;

function wlActive() {
  return state.watchlists.find(w => w.id === state.activeWlId) || state.watchlists[0] || null;
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

async function wlAddStock(code) {
  if (!state.user) return;
  const wl = wlActive();
  if (!wl || wl.codes.includes(code)) return;
  wl.codes.push(code);
  renderWlTable();
  try {
    await fetch(`/api/watchlists/${wl.id}/stocks`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code}),
    });
  } catch { wl.codes = wl.codes.filter(c => c !== code); renderWlTable(); showToast('新增失敗'); }
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

function renderWlTable() {
  const wl = wlActive();
  if (!wl) return;
  document.getElementById('wl-count').textContent = `${wl.codes.length} 支`;

  const rows = wl.codes.map(code => {
    const s = state.allData.find(d => d.code === code);
    const rmBtn = `<button class="wl-remove-btn" data-rm-code="${code}" title="移除">✕</button>`;
    if (!s) return [rmBtn, code, '(未載入)', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—', '—'];
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
    ];
  });

  if (wlDt) {
    wlDt.clear().rows.add(rows).draw();
  } else {
    wlDt = $('#wl-table').DataTable({
      data: rows, pageLength: 25, order: [], language: dtLang(), destroy: true,
      columnDefs: [
        { targets: 0, orderable: false, className: 'dt-center', width: '32px' },
        { targets: [4,5,6,7,8,9,11,12,13,14], className: 'dt-right', type: 'num-cell' },
        { targets: [1,2,3,10,15],             className: 'dt-left' },
      ],
    });
    $('#wl-table tbody').on('click', '.wl-remove-btn', function(e) {
      e.stopImmediatePropagation();
      wlRemoveStock(this.dataset.rmCode);
    });
    $('#wl-table tbody').on('click', 'td', function() {
      const code = $(this).find('[data-code]').data('code') ||
                   $(this).closest('tr').find('[data-code]').data('code');
      if (code) loadStockDetail(code);
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

function _annTruncate(s, n) {
  if (!s) return '—';
  return s.length > n ? s.slice(0, n) + '…' : s;
}

function renderAnnRow(a, i) {
  return `<tr>
    <td>${a.announce_date}${a.announce_time ? ' ' + a.announce_time.slice(0, 5) : ''}</td>
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
    <td class="td-center"><a class="btn btn-sm ann-ai-link" href="https://gemini.google.com" target="_blank" rel="noopener" data-idx="${i}">🤖 AI分析</a></td>
  </tr>`;
}

async function loadAnnouncements() {
  const tbody = document.getElementById('ann-tbody');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="12" class="ann-empty">載入中…</td></tr>';
  try {
    _annData = await fetch('/api/announcements/today').then(r => r.json());
    const countEl = document.getElementById('ann-count');
    if (countEl) countEl.textContent = `共 ${_annData.length} 筆`;
    tbody.innerHTML = _annData.length
      ? _annData.map(renderAnnRow).join('')
      : '<tr><td colspan="12" class="ann-empty">近期無公告</td></tr>';
    tbody.querySelectorAll('[data-code]').forEach(el => {
      el.addEventListener('click', () => loadStockDetail(el.dataset.code));
    });
    tbody.querySelectorAll('.ann-subject-link').forEach(el => {
      el.addEventListener('click', () => openAnnModal(+el.dataset.idx));
    });
    tbody.querySelectorAll('.ann-ai-link').forEach(el => {
      el.addEventListener('click', () => copyAnnForAI(+el.dataset.idx));
    });
  } catch (_) {
    tbody.innerHTML = '<tr><td colspan="12" class="ann-empty">載入失敗</td></tr>';
  }
}

function openAnnModal(i) {
  const a = _annData[i];
  if (!a) return;
  document.getElementById('ann-modal-title').textContent = `${a.stock_code} ${a.name || ''}`;
  document.getElementById('ann-modal-body').innerHTML = `
    <div class="ann-modal-subject">${a.subject || ''}</div>
    <div class="ann-modal-date">${a.announce_date}</div>
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
    el.querySelectorAll('.today-stock-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        closeTodayPanel();
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

/* ── Admin user panel ── */
document.getElementById('admin-btn').addEventListener('click', () => {
  const panel = document.getElementById('admin-panel');
  panel.classList.toggle('hidden');
  if (!panel.classList.contains('hidden')) loadAdminUsers();
});

document.getElementById('admin-panel-close').addEventListener('click', () =>
  document.getElementById('admin-panel').classList.add('hidden'));

async function loadAdminUsers() {
  const listEl = document.getElementById('admin-user-list');
  try {
    const data = await fetch('/api/admin/users').then(r => r.json());
    document.getElementById('admin-user-count').textContent = `共 ${data.total} 人`;
    listEl.innerHTML = data.users.map(u => `
      <div class="admin-user-item" data-id="${u.id}">
        <span class="admin-user-name">${_escapeHtml(u.username)}</span>
        <span class="admin-user-meta">自選股 ${u.watchlist_count} 組　註冊於 ${u.created_at}</span>
        <button class="admin-user-del" title="刪除帳號">✕</button>
      </div>
    `).join('');
  } catch {
    listEl.innerHTML = '<div class="msg-empty">載入失敗</div>';
  }
}

document.getElementById('admin-user-list').addEventListener('click', async function(e) {
  const btn = e.target.closest('.admin-user-del');
  if (!btn) return;
  const item = btn.closest('.admin-user-item');
  const name = item.querySelector('.admin-user-name').textContent;
  if (!confirm(`確定要刪除帳號「${name}」？將同時移除其自選股清單。`)) return;
  try {
    const resp = await fetch(`/api/admin/users/${item.dataset.id}`, {method: 'DELETE'}).then(r => r.json());
    if (resp.ok) {
      item.remove();
      const countEl = document.getElementById('admin-user-count');
      const n = Number(countEl.textContent.match(/\d+/)?.[0] || 0);
      countEl.textContent = `共 ${Math.max(0, n - 1)} 人`;
    } else {
      showToast(resp.error || '刪除失敗');
    }
  } catch {
    showToast('刪除失敗');
  }
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
