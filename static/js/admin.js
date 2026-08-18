/* ── Theme (same logic as app.js) ── */
(function () {
  const saved = localStorage.getItem('theme') || 'dark';
  document.documentElement.setAttribute('data-theme', saved);
})();
document.addEventListener('DOMContentLoaded', () => {
  const btn = document.getElementById('theme-btn');
  btn.textContent = document.documentElement.getAttribute('data-theme') === 'dark' ? '☀' : '🌙';
  btn.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme');
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
    btn.textContent = next === 'dark' ? '☀' : '🌙';
  });
});

/* ── Helpers ── */
function showToast(msg, ms = 2500) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function _escapeHtml(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : s;
  return div.innerHTML;
}

/* ── Auth guard: this page is admin-only, enforced server-side too (GET /admin
   redirects non-admins to "/"), but the initial HTML is served before that
   check can run client-side JS, so also bounce here for the case someone's
   session expired after the page loaded (e.g. long-open tab). ── */
async function initAuth() {
  const data = await fetch('/api/auth/me').then(r => r.json());
  if (!data.user || !data.user.is_admin) {
    location.href = '/';
    return false;
  }
  document.getElementById('auth-area').innerHTML = `
    <span class="auth-user">${_escapeHtml(data.user.username)}</span>
    <button class="auth-logout-btn" id="logout-btn">登出</button>`;
  document.getElementById('logout-btn').addEventListener('click', async () => {
    await fetch('/api/auth/logout', { method: 'POST' });
    location.href = '/';
  });
  return true;
}

/* ── System health ── */
async function loadHealth() {
  try {
    const data = await fetch('/api/admin/health').then(r => r.json());
    const d = data.db;
    document.getElementById('health-grid').innerHTML = `
      <div class="health-tile"><div class="health-tile-label">股票數</div><div class="health-tile-value">${d.stocks}</div></div>
      <div class="health-tile"><div class="health-tile-label">股價筆數</div><div class="health-tile-value">${d.prices.toLocaleString()}</div></div>
      <div class="health-tile"><div class="health-tile-label">月營收筆數</div><div class="health-tile-value">${d.revenues.toLocaleString()}</div></div>
      <div class="health-tile"><div class="health-tile-label">季財報筆數</div><div class="health-tile-value">${d.quarterly.toLocaleString()}</div></div>
      <div class="health-tile"><div class="health-tile-label">會員數</div><div class="health-tile-value">${d.users}</div></div>
      <div class="health-tile"><div class="health-tile-label">留言數</div><div class="health-tile-value">${d.messages}</div></div>
      <div class="health-tile"><div class="health-tile-label">最新股價日期</div><div class="health-tile-value">${d.last_price_date || '—'}</div></div>
    `;
    const tokenEl = document.getElementById('token-badge');
    tokenEl.textContent = data.finmind_token_set ? '✓ FINMIND_TOKEN 已設定' : '✕ FINMIND_TOKEN 未設定';
    tokenEl.className = 'token-badge ' + (data.finmind_token_set ? 'ok' : 'bad');

    document.getElementById('task-health-tbody').innerHTML = data.tasks.map(t => `
      <tr>
        <td>${t.label}</td>
        <td>${t.last_run ? t.last_run.slice(0, 16) : '—'}</td>
        <td>${t.last_status ? `<span class="log-dot ${t.last_status}" style="display:inline-block;"></span> ${t.last_status}` : '—'}</td>
        <td class="num">${t.success_rate}</td>
        <td>${_escapeHtml(t.last_message || '')}</td>
      </tr>
    `).join('');
  } catch (_) {
    showToast('系統健康資料載入失敗');
  }
}

/* ── Crawler control ── */
async function runCrawler(task) {
  showToast(`已觸發：${task}，請稍候…`);
  try {
    const resp = await fetch(`/api/crawler/run/${task}`, { method: 'POST' });
    const info = await resp.json();
    if (info.detail) showToast(`${task} → ${info.detail}`);
    if (task === 'quarterly' && info.detail) {
      document.getElementById('quarterly-btn').textContent = `更新季財報 (${info.detail.replace('quarterly ', '')})`;
    }
    setTimeout(() => { loadCrawlerStatus(); loadHealth(); }, 1500);
  } catch (_) {
    showToast('呼叫失敗');
  }
}

async function loadCrawlerStatus() {
  try {
    const logs = await fetch('/api/crawler/status').then(r => r.json());
    document.getElementById('status-logs').innerHTML = logs.map(l => `
      <div class="log-item">
        <div class="log-dot ${l.status}"></div>
        <div>
          <div class="log-task">${l.task}</div>
          <div class="log-msg">${_escapeHtml(l.message || '')}</div>
        </div>
        <div class="log-time">${l.created_at.slice(0, 16)}</div>
      </div>
    `).join('');
  } catch (_) {}
}

/* ── Visitor log ── */
function _visitReferrer(r) {
  if (!r) return '<span class="ann-dot-empty">直接造訪</span>';
  try {
    return _escapeHtml(new URL(r).hostname);
  } catch {
    return _escapeHtml(r.slice(0, 40));
  }
}

async function loadVisits() {
  const tbody = document.getElementById('visit-tbody');
  try {
    const data = await fetch('/api/admin/visits').then(r => r.json());
    document.getElementById('visit-stats').innerHTML = `
      <div class="health-tile"><div class="health-tile-label">今日訪客(不重複IP)</div><div class="health-tile-value">${data.today_unique_ips}</div></div>
      <div class="health-tile"><div class="health-tile-label">今日進站次數</div><div class="health-tile-value">${data.today_visits}</div></div>
      <div class="health-tile"><div class="health-tile-label">累計進站次數</div><div class="health-tile-value">${data.total_visits.toLocaleString()}</div></div>
    `;
    tbody.innerHTML = data.visits.length ? data.visits.map(v => `
      <tr>
        <td>${v.created_at.slice(0, 19)}</td>
        <td>${_escapeHtml(v.ip)}</td>
        <td>${v.username ? _escapeHtml(v.username) : '<span class="ann-dot-empty">訪客</span>'}</td>
        <td>${_visitReferrer(v.referrer)}</td>
        <td class="admin-msg-content" title="${_escapeHtml(v.user_agent)}">${_escapeHtml(v.user_agent)}</td>
      </tr>
    `).join('') : '<tr><td colspan="5" class="ann-empty">尚無紀錄</td></tr>';
  } catch {
    tbody.innerHTML = '<tr><td colspan="5" class="ann-empty">載入失敗</td></tr>';
  }
}

/* ── Member management ── */
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

document.getElementById('admin-user-list').addEventListener('click', async function (e) {
  const btn = e.target.closest('.admin-user-del');
  if (!btn) return;
  const item = btn.closest('.admin-user-item');
  const name = item.querySelector('.admin-user-name').textContent;
  if (!confirm(`確定要刪除帳號「${name}」？將同時移除其自選股清單。`)) return;
  try {
    const resp = await fetch(`/api/admin/users/${item.dataset.id}`, { method: 'DELETE' }).then(r => r.json());
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

/* ── Message board management ── */
async function loadMessages() {
  const tbody = document.getElementById('msg-admin-tbody');
  try {
    const data = await fetch('/api/admin/messages').then(r => r.json());
    document.getElementById('msg-admin-count').textContent = `共 ${data.total} 則`;
    tbody.innerHTML = data.messages.length ? data.messages.map(m => `
      <tr data-id="${m.id}">
        <td><input type="checkbox" class="admin-chk msg-row-chk"></td>
        <td>${_escapeHtml(m.username)}</td>
        <td class="admin-msg-content">${_escapeHtml(m.content)}</td>
        <td>${m.created_at}</td>
        <td><button class="btn btn-sm msg-row-del" style="background:var(--neg);">刪除</button></td>
      </tr>
    `).join('') : '<tr><td colspan="5" class="ann-empty">尚無留言</td></tr>';
  } catch {
    tbody.innerHTML = '<tr><td colspan="5" class="ann-empty">載入失敗</td></tr>';
  }
}

document.getElementById('msg-select-all').addEventListener('change', function () {
  document.querySelectorAll('.msg-row-chk').forEach(c => c.checked = this.checked);
});

document.getElementById('msg-admin-tbody').addEventListener('click', async function (e) {
  const btn = e.target.closest('.msg-row-del');
  if (!btn) return;
  const tr = btn.closest('tr');
  if (!confirm('確定要刪除這則留言？')) return;
  try {
    const resp = await fetch(`/api/messages/${tr.dataset.id}`, { method: 'DELETE' }).then(r => r.json());
    if (resp.ok) { tr.remove(); loadMessages(); } else { showToast(resp.error || '刪除失敗'); }
  } catch {
    showToast('刪除失敗');
  }
});

document.getElementById('msg-bulk-del-btn').addEventListener('click', async function () {
  const ids = Array.from(document.querySelectorAll('.msg-row-chk:checked'))
    .map(c => Number(c.closest('tr').dataset.id));
  if (!ids.length) { showToast('請先勾選要刪除的留言'); return; }
  if (!confirm(`確定要刪除 ${ids.length} 則留言？`)) return;
  try {
    const resp = await fetch('/api/admin/messages/bulk-delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids }),
    }).then(r => r.json());
    if (resp.ok) {
      showToast(`已刪除 ${resp.deleted} 則`);
      document.getElementById('msg-select-all').checked = false;
      loadMessages();
    } else {
      showToast(resp.error || '刪除失敗');
    }
  } catch {
    showToast('刪除失敗');
  }
});

/* ── Init ── */
(async function init() {
  const ok = await initAuth();
  if (!ok) return;
  loadHealth();
  loadCrawlerStatus();
  loadVisits();
  loadAdminUsers();
  loadMessages();
})();
