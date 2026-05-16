function togglePwd(inputId, btn) {
  const input = document.getElementById(inputId);
  if (!input) return;
  if (input.type === 'password') {
    input.type = 'text';
    btn.textContent = '🙈';
  } else {
    input.type = 'password';
    btn.textContent = '👁';
  }
}

/* ── Toast System ───────────────────────────────────────────── */
(function() {
  const container = document.createElement('div');
  container.id = 'toast-container';
  document.body.appendChild(container);
})();

function showToast(msg, type = 'success', duration = 3500) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.innerHTML = `<span class="toast-icon">${type === 'success' ? '✅' : type === 'info' ? '⚡' : '❌'}</span><span class="toast-msg">${msg}</span>`;
  container.appendChild(t);
  requestAnimationFrame(() => { t.classList.add('toast-in'); });
  setTimeout(() => {
    t.classList.remove('toast-in');
    t.classList.add('toast-out');
    setTimeout(() => t.remove(), 350);
  }, duration);
}

/* ── Wallet Glow Pulse ──────────────────────────────────────── */
function pulseWallet() {
  const w = document.getElementById('wallet-card');
  if (!w) return;
  w.classList.add('wallet-pulse');
  setTimeout(() => w.classList.remove('wallet-pulse'), 900);
}

/* ── Live Coin Counter Update ───────────────────────────────── */
function updateCoinDisplays(newCoins) {
  const els = document.querySelectorAll('[data-live="coins"]');
  els.forEach(el => { el.textContent = newCoins; });
  const topbar = document.getElementById('topbar-coins-val');
  if (topbar) topbar.textContent = newCoins;
  pulseWallet();
}

/* ── Dashboard AJAX Refresh ─────────────────────────────────── */
let dashboardRefreshTimer = null;
function startDashboardRefresh() {
  if (!document.getElementById('wallet-card')) return;
  dashboardRefreshTimer = setInterval(refreshDashboardStats, 30000);
}
function refreshDashboardStats() {
  fetch('/x/dashboard-stats')
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data) return;
      const coinEls = document.querySelectorAll('[data-live="coins"]');
      coinEls.forEach(el => { el.textContent = data.coins; });
      const topbar = document.getElementById('topbar-coins-val');
      if (topbar) topbar.textContent = data.coins;
      const rupeeEl = document.getElementById('live-rupees');
      if (rupeeEl) rupeeEl.textContent = data.rupees.toFixed(2);
      const todayEl = document.getElementById('live-today-data');
      if (todayEl) todayEl.textContent = data.today_data_saved;
      const lifeEl = document.getElementById('live-lifetime-data');
      if (lifeEl) lifeEl.textContent = data.lifetime_data_saved;
      const adsEl = document.getElementById('live-ads-today');
      if (adsEl) adsEl.textContent = data.ads_today;
      _updateTargetBar(data.today_data_saved, data.daily_data_target);
    })
    .catch(() => {});
}
function _updateTargetBar(saved, target) {
  const bar = document.getElementById('target-bar-fill');
  const label = document.getElementById('target-bar-label');
  const pct = target > 0 ? Math.min(100, Math.round((saved / target) * 100)) : 0;
  if (bar) bar.style.width = pct + '%';
  if (label) label.textContent = target > 0
    ? `${saved} / ${target} MB  (${pct}%)`
    : 'No target set';
}
document.addEventListener('DOMContentLoaded', startDashboardRefresh);
