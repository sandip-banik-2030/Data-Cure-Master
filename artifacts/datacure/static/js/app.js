/* ── Datacure App JS — Premium UX Layer ─────────────────────── */

/* ── Password Toggle ─────────────────────────────────────────── */
function togglePwd(inputId, btn) {
  const input = document.getElementById(inputId);
  if (!input) return;
  if (input.type === 'password') {
    input.type = 'text'; btn.textContent = '🙈';
  } else {
    input.type = 'password'; btn.textContent = '👁';
  }
}

/* ── Toast System ─────────────────────────────────────────────── */
(function() {
  const c = document.createElement('div');
  c.id = 'toast-container';
  document.body.appendChild(c);
})();

function showToast(msg, type = 'success', duration = 3500) {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const icons = { success: '✓', error: '✕', info: '⚡' };
  const t = document.createElement('div');
  t.className = `toast toast-${type}`;
  t.innerHTML = `<span class="toast-icon">${icons[type] || '●'}</span><span class="toast-msg">${msg}</span>`;
  container.appendChild(t);
  requestAnimationFrame(() => requestAnimationFrame(() => t.classList.add('toast-in')));
  const remove = () => {
    t.classList.remove('toast-in');
    t.classList.add('toast-out');
    setTimeout(() => t.remove(), 350);
  };
  const timer = setTimeout(remove, duration);
  t.addEventListener('click', () => { clearTimeout(timer); remove(); });
}

/* ── Wallet Pulse ─────────────────────────────────────────────── */
function pulseWallet() {
  const w = document.getElementById('wallet-card');
  if (!w) return;
  w.classList.remove('wallet-pulse');
  void w.offsetWidth; // reflow
  w.classList.add('wallet-pulse');
  setTimeout(() => w.classList.remove('wallet-pulse'), 900);
}

/* ── Animated Coin Counter ────────────────────────────────────── */
function animateNumber(el, from, to, duration = 600) {
  if (!el || from === to) { if (el) el.textContent = to; return; }
  const start = performance.now();
  const diff = to - from;
  function step(now) {
    const elapsed = Math.min(now - start, duration);
    const progress = elapsed / duration;
    // Ease-out cubic
    const ease = 1 - Math.pow(1 - progress, 3);
    el.textContent = Math.round(from + diff * ease);
    if (elapsed < duration) requestAnimationFrame(step);
    else el.textContent = to;
  }
  requestAnimationFrame(step);
}

/* ── Live Coin Counter Update ────────────────────────────────── */
function updateCoinDisplays(newCoins) {
  const topbar = document.getElementById('topbar-coins-val');
  if (topbar) {
    const old = parseInt(topbar.textContent) || 0;
    animateNumber(topbar, old, newCoins);
  }
  document.querySelectorAll('[data-live="coins"]').forEach(el => {
    const old = parseInt(el.textContent) || 0;
    animateNumber(el, old, newCoins);
  });
  pulseWallet();
}

/* ── Dashboard AJAX Refresh ──────────────────────────────────── */
function refreshDashboardStats() {
  fetch('/x/dashboard-stats')
    .then(r => r.ok ? r.json() : null)
    .then(data => {
      if (!data) return;
      updateCoinDisplays(data.coins);
      const rupeeEl = document.getElementById('live-rupees');
      if (rupeeEl) rupeeEl.textContent = Math.floor(data.rupees);
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

/* ── Card Stagger Animation on Page Load ─────────────────────── */
function initCardAnimations() {
  const cards = document.querySelectorAll(
    '.card, .wallet-card, .streak-card, .target-card, .stat-card, .action-card, .request-card, .sla-notice-card'
  );
  if (!cards.length) return;
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('card-visible');
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.1, rootMargin: '0px 0px -20px 0px' });
  cards.forEach((card, i) => {
    card.style.transitionDelay = `${i * 40}ms`;
    observer.observe(card);
  });
}

/* ── Button Tap Ripple ───────────────────────────────────────── */
function initButtonRipples() {
  document.querySelectorAll('.btn, .action-card, .pack-card, .bnav-item').forEach(el => {
    el.addEventListener('pointerdown', function(e) {
      this.classList.add('btn-tapped');
      setTimeout(() => this.classList.remove('btn-tapped'), 150);
    }, { passive: true });
  });
}

/* ── Bottom Nav Active Pulse ─────────────────────────────────── */
function initNavHighlight() {
  const active = document.querySelector('.bnav-item.active');
  if (active) {
    const wrap = active.querySelector('.bnav-icon-wrap');
    if (wrap) wrap.classList.add('bnav-active-glow');
  }
}

/* ── Page Fade-in ─────────────────────────────────────────────── */
function initPageTransition() {
  const main = document.getElementById('main-content');
  if (main) {
    main.classList.add('page-entering');
    requestAnimationFrame(() =>
      requestAnimationFrame(() => main.classList.add('page-entered'))
    );
  }
  // Intercept nav clicks for smooth exit
  document.querySelectorAll('a:not([target]):not([href^="#"]):not([href^="mailto"])').forEach(link => {
    link.addEventListener('click', function(e) {
      const href = this.getAttribute('href');
      if (!href || href.startsWith('http') || href.startsWith('javascript')) return;
      if (main) {
        e.preventDefault();
        main.classList.remove('page-entered');
        setTimeout(() => { window.location.href = href; }, 180);
      }
    });
  });
}

/* ── Auto-start Dashboard Refresh ───────────────────────────── */
document.addEventListener('DOMContentLoaded', function() {
  initPageTransition();
  initCardAnimations();
  initButtonRipples();
  initNavHighlight();

  // Auto-refresh dashboard stats every 30s
  if (document.getElementById('wallet-card')) {
    setInterval(refreshDashboardStats, 30000);
  }
});
