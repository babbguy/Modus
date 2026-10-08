/**
 * Modus Dashboard — View Router
 * Hash-based routing with dynamic module imports.
 * Manages view switching and view lifecycle.
 */

import * as state from './state.js';

// ── View registry: lazy-loaded ES modules ────────────────────────────────────
const VIEW_MODULES = {
  overview:      () => import('./views/overview.js'),
  executive:     () => import('./views/executive.js'),
  finance:       () => import('./views/finance.js'),
  devops:        () => import('./views/devops.js'),
  apps:          () => import('./views/apps.js'),
  thresholds:    () => import('./views/alerts.js'),  // merged into alerts
  alerts:        () => import('./views/alerts.js'),
  pricing:       () => import('./views/pricing.js'),
  policies:      () => import('./views/policies.js'),
  teams:         () => import('./views/teams.js'),
  notifications: () => import('./views/notifications.js'),
  routing:       () => import('./views/routing.js'),
  governance:    () => import('./views/governance.js'),
  compliance:    () => import('./views/compliance.js'),
  sentinel:      () => import('./views/sentinel.js'),
  settings:      () => import('./views/settings.js'),
  connections:   () => import('./views/connections.js'),
  federation:    () => import('./views/federation.js'),
  admin:         () => import('./views/admin.js'),
  profile:       () => import('./views/profile.js'),
  topology:      () => import('./views/topology.js'),
  sessions:      () => import('./views/sessions.js'),
  help:          () => import('./views/help.js'),
};

// All known views (for sidebar highlighting)
const ALL_VIEWS = [
  'overview', 'apps', 'thresholds', 'pricing', 'notifications', 'settings',
  'connections', 'profile', 'devops', 'executive', 'finance', 'routing', 'policies',
  'teams', 'alerts', 'governance', 'compliance', 'federation', 'sentinel', 'admin',
  'topology', 'sessions', 'help',
];

let _currentModule = null;
let _currentView = null;

/**
 * Initialize the router: set up hash listener, navigate to initial view.
 */
export function initRouter() {
  window.addEventListener('hashchange', _onHashChange);

  // Sidebar click handlers
  document.querySelectorAll('.sidebar-item[data-view]').forEach(el => {
    el.addEventListener('click', (e) => {
      e.preventDefault();
      navigate(el.dataset.view);
    });
  });
}

/**
 * Navigate to a view by name.
 * @param {string} viewName
 */
export async function navigate(viewName) {
  if (!viewName || viewName === _currentView) return;

  // 1. Destroy previous view
  if (_currentModule && typeof _currentModule.destroy === 'function') {
    try { _currentModule.destroy(); } catch (e) { console.warn('View cleanup failed:', e); }
  }
  _currentModule = null;
  _currentView = viewName;

  // 2. Update sidebar active state
  document.querySelectorAll('.sidebar-item').forEach(el => {
    el.classList.toggle('active', el.dataset.view === viewName);
  });

  // 3. Update URL hash (without triggering hashchange).
  //    Preserve any sub-path (e.g. #help/01-set-your-first-budget)
  //    if the current hash already targets this view.
  const currentHash = window.location.hash.slice(1);
  const currentBase = currentHash.split('/')[0];
  const newHash = currentBase === viewName ? '#' + currentHash : '#' + viewName;
  if (window.location.hash !== newHash) {
    history.replaceState(null, '', newHash);
  }

  // 4. Update state
  state.set('currentViewMode', viewName);

  // 5. Show/hide period tabs (only for overview)
  const periodTabs = document.querySelector('.period-tabs');
  if (periodTabs) periodTabs.style.display = viewName === 'overview' ? 'flex' : 'none';

  // 6. Get view container
  const container = document.getElementById('view-container');
  if (!container) return;

  // 7. Load and render view module
  const loader = VIEW_MODULES[viewName];
  if (loader) {
    // Clear container
    container.innerHTML = '';

    // Show loading state
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;min-height:300px;color:var(--muted)"><div class="ds-skeleton" style="width:60%;height:200px;border-radius:12px"></div></div>';

    try {
      const mod = await loader();
      _currentModule = mod;

      // Clear loading state
      container.innerHTML = '';

      // Render the view
      if (typeof mod.render === 'function') {
        await mod.render(container);
      }
    } catch (err) {
      console.error(`Failed to load view "${viewName}":`, err);
      container.innerHTML = `
        <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:300px;gap:12px;color:var(--muted)">
          <i class="fa-solid fa-circle-exclamation" style="font-size:32px;color:var(--danger);opacity:0.6"></i>
          <div style="font-size:14px;font-weight:600;color:var(--text)">Failed to load view</div>
          <div style="font-size:12px">${err.message}</div>
          <button class="ds-btn ds-btn-ghost ds-btn-sm" onclick="location.reload()">Reload</button>
        </div>
      `;
    }
  } else {
    // View not yet migrated — show placeholder
    container.innerHTML = `
      <div style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:400px;gap:12px;color:var(--muted)">
        <i class="fa-solid fa-wrench" style="font-size:32px;opacity:0.3"></i>
        <div style="font-size:14px;font-weight:600;color:var(--text)">${viewName.charAt(0).toUpperCase() + viewName.slice(1)}</div>
        <div style="font-size:12px">This view is being migrated. Check back soon.</div>
      </div>
    `;
  }
}

/**
 * Get the active view module (used by the auto-refresh timer to call refresh()).
 * @returns {object|null}
 */
export function getCurrentModule() {
  return _currentModule;
}

/**
 * Get the current view name.
 * @returns {string}
 */
export function getCurrentView() {
  return _currentView;
}

/**
 * Get the initial view from URL hash or default.
 * @returns {string}
 */
export function getInitialView() {
  const hash = window.location.hash.slice(1);
  const baseView = hash.split('/')[0];
  if (baseView && ALL_VIEWS.includes(baseView)) return baseView;

  // Check saved preference
  const saved = localStorage.getItem('modus_default_view');
  if (saved && ALL_VIEWS.includes(saved)) return saved;

  return 'overview';
}

// ── Internal ─────────────────────────────────────────────────────────────────

function _onHashChange() {
  const hash = window.location.hash.slice(1);
  // Support deep links like #help/01-set-your-first-budget
  const baseView = hash.split('/')[0];
  if (baseView && ALL_VIEWS.includes(baseView)) {
    if (baseView !== _currentView) {
      navigate(baseView);
    }
  }
}
