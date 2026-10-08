/**
 * Modus Dashboard v2 — Main Entry Point
 * Initializes all subsystems and navigates to the initial view.
 */

import * as state from './state.js';
import { authHeaders } from './api.js';
import { initRouter, navigate, getInitialView, getCurrentModule } from './router.js';
import { initAutoRefresh } from './auto-refresh.js';
import { initSidebar } from './sidebar.js';
import { initTheme } from './theme.js';
import { initAssistant } from './assistant.js';

/**
 * Initialize the dashboard application.
 * Called from <script type="module"> in index.html.
 */
export async function init() {
  // 1. Theme (must be first to prevent flash)
  initTheme();

  // 2. Sidebar behavior
  initSidebar();

  // 3. Router (sets up hash listener + sidebar click handlers)
  initRouter();

  // Wait for the login gate (js/login-gate.js) before requesting any data.
  if (window.ModusLogin && window.ModusLogin.ready) await window.ModusLogin.ready;

  // 4. Navigate to initial view
  const initialView = getInitialView();
  await navigate(initialView);

  // 5. Onboarding wizard — show on first-ever deployment
  const _obResp = await fetch('/api/v1/onboarding/status', { headers: authHeaders() }).then(r => r.ok ? r.json() : null).catch(() => null);
  if (_obResp?.needs_onboarding) {
    const { showWizard } = await import('./views/onboarding.js');
    showWizard();
  }

  // 6. Status indicator
  _updateStatus('connected');

  // 7. Period tab click handlers
  _initPeriodTabs();

  // 8. Auto-refresh (interval from Settings -> Auto-refresh)
  initAutoRefresh(getCurrentModule);

  // 9. Global LLM Assistant
  initAssistant();

  console.log('%c⬡ Modus Dashboard v2', 'color: #9FC131; font-weight: bold; font-size: 14px');
}

// ── Status indicator ──────────────────────────────────────────────────────────

// ── Period tab handlers ──────────────────────────────────────────────────────

function _initPeriodTabs() {
  document.querySelectorAll('.period-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.period-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const days = tab.dataset.days;
      const numDays = days === 'live' ? 1 : parseInt(days);
      state.set('currentDays', numDays);
      state.set('currentPeriodMode', days);
    });
  });
}

// ── Status indicator ──────────────────────────────────────────────────────────

function _updateStatus(status) {
  const el = document.getElementById('status-indicator');
  if (!el) return;

  const dot = el.querySelector('.status-dot');
  const label = el.querySelector('.status-label');
  const time = el.querySelector('.status-time');

  if (dot) {
    dot.className = 'status-dot ' + (status === 'connected' ? 'online' : 'offline');
  }
  if (label) {
    label.textContent = status === 'connected' ? 'Connected' : 'Disconnected';
  }
  if (time) {
    time.textContent = 'Updated ' + new Date().toLocaleTimeString();
  }
}
