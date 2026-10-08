/**
 * Modus Dashboard v2 — External Connections View
 * Shows all outbound connections from the Modus instance with real-time status.
 * 5 tiles: Summary KPIs, Platform, AI Providers, Notifications, Integrations.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError } from '../tile.js';
import { rawFetch, esc } from '../api.js';
import { timeSince } from '../format.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;

// ── Icon / color map per connection type ─────────────────────────────────────

const CONN_ICONS = {
  gateway:      { icon: 'fa-solid fa-server',          color: '#3b82f6' },
  ai_provider:  { icon: 'fa-solid fa-robot',           color: '#8b5cf6' },
  slack:        { icon: 'fa-solid fa-hashtag',          color: '#a855f7' },
  teams:        { icon: 'fa-solid fa-users',            color: '#3b82f6' },
  email:        { icon: 'fa-solid fa-envelope',         color: '#14b8a6' },
  pagerduty:    { icon: 'fa-solid fa-pager',            color: '#10b981' },
  webhook:      { icon: 'fa-solid fa-link',             color: '#6b7280' },
  nomus:     { icon: 'fa-solid fa-gavel',            color: '#f59e0b' },
  federation:   { icon: 'fa-solid fa-network-wired',    color: '#06b6d4' },
};

// AI provider-specific colors
const AI_COLORS = {
  anthropic: '#e87b35',
  openai:    '#10b981',
  google:    '#4285f4',
  azure:     '#0078d4',
  aws:       '#ff9900',
  mistral:   '#f97316',
  cohere:    '#6366f1',
};

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Connections view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  // Initialize grid
  _grid = initGrid(container, 'connections');

  // Create tiles
  const tiles = [
    _createSummaryTile(),
    _createPlatformTile(),
    _createAiTile(),
    _createNotifTile(),
    _createIntegrationsTile(),
  ];

  // Load saved or default layout
  const layout = loadLayout('connections', LAYOUTS.connections);

  // Add tiles to grid
  addTiles(_grid, tiles, layout);

  // Fetch and render data (small delay to ensure DOM is ready)
  await new Promise(r => setTimeout(r, 50));
  await _loadData();
}

/**
 * Tear down the Connections view, cleaning up DOM.
 */
export function destroy() {
  _destroyed = true;
  _grid = null;
  _container = null;
}

// ── Data loading ─────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;

  // Set all tiles to loading
  ['conn-summary', 'conn-platform', 'conn-ai', 'conn-notif', 'conn-integrations']
    .forEach(id => setTileLoading(id));

  try {
    const resp = await rawFetch('/api/v1/admin/connections');
    if (_destroyed) return;

    if (!resp.ok) {
      const errText = `HTTP ${resp.status}`;
      ['conn-summary', 'conn-platform', 'conn-ai', 'conn-notif', 'conn-integrations']
        .forEach(id => setTileError(id, errText));
      return;
    }

    const data = await resp.json();
    if (_destroyed) return;

    try { _renderSummary(data.summary || {}); } catch (e) { console.error('[connections] summary render:', e); }
    try { _renderCategory('conn-platform', data.connections, 'platform'); } catch (e) { console.error('[connections] platform render:', e); }
    try { _renderCategory('conn-ai', data.connections, 'ai_provider'); } catch (e) { console.error('[connections] ai render:', e); }
    try { _renderCategory('conn-notif', data.connections, 'notification'); } catch (e) { console.error('[connections] notif render:', e); }
    try { _renderCategory('conn-integrations', data.connections, 'integration'); } catch (e) { console.error('[connections] integrations render:', e); }
  } catch (err) {
    if (_destroyed) return;
    console.error('[connections] fetch failed:', err);
    ['conn-summary', 'conn-platform', 'conn-ai', 'conn-notif', 'conn-integrations']
      .forEach(id => setTileError(id, err.message));
  }
}

// ── Tile creators ────────────────────────────────────────────────────────────

function _createSummaryTile() {
  return createTile({
    id: 'conn-summary',
    title: 'Connection Status',
    icon: 'fa-solid fa-plug',
    iconBg: 'rgba(59,130,246,0.15)',
    iconColor: '#3b82f6',
    headerRight: `<button class="ds-btn ds-btn-ghost ds-btn-sm" id="conn-test-all-btn">
      <i class="fa-solid fa-arrows-rotate" style="margin-right:4px"></i>Test All
    </button>`,
  });
}

function _createPlatformTile() {
  return createTile({
    id: 'conn-platform',
    title: 'Platform',
    icon: 'fa-solid fa-server',
    iconBg: 'rgba(59,130,246,0.15)',
    iconColor: '#3b82f6',
  });
}

function _createAiTile() {
  return createTile({
    id: 'conn-ai',
    title: 'AI Providers',
    icon: 'fa-solid fa-robot',
    iconBg: 'rgba(139,92,246,0.15)',
    iconColor: '#8b5cf6',
  });
}

function _createNotifTile() {
  return createTile({
    id: 'conn-notif',
    title: 'Notifications',
    icon: 'fa-regular fa-bell',
    iconBg: 'rgba(245,158,11,0.15)',
    iconColor: '#f59e0b',
  });
}

function _createIntegrationsTile() {
  return createTile({
    id: 'conn-integrations',
    title: 'Integrations',
    icon: 'fa-solid fa-puzzle-piece',
    iconBg: 'rgba(6,182,212,0.15)',
    iconColor: '#06b6d4',
  });
}

// ── Render helpers ───────────────────────────────────────────────────────────

function _renderSummary(summary) {
  const body = document.getElementById('conn-summary-body');
  if (!body) return;

  const total = summary.total || 0;
  const connected = summary.connected || 0;
  const errors = summary.error || 0;
  const degraded = summary.degraded || 0;
  const notConfigured = summary.not_configured || 0;
  const disabled = summary.disabled || 0;

  body.className = 'gs-tile-body kpi-row';
  body.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Total</div>
      <div class="kpi-value">${total}</div>
      <div class="kpi-sub">connections</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Connected</div>
      <div class="kpi-value" style="color:#10b981">${connected}</div>
      <div class="kpi-sub">healthy</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Degraded</div>
      <div class="kpi-value" style="color:#f59e0b">${degraded}</div>
      <div class="kpi-sub">warning</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Errors</div>
      <div class="kpi-value" style="color:#ef4444">${errors}</div>
      <div class="kpi-sub">failing</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Not Configured</div>
      <div class="kpi-value" style="color:var(--muted)">${notConfigured + disabled}</div>
      <div class="kpi-sub">inactive</div>
    </div>
  `;

  // Wire up Test All button
  const testAllBtn = document.getElementById('conn-test-all-btn');
  if (testAllBtn) {
    testAllBtn.addEventListener('click', _testAll);
  }
}

function _renderCategory(tileId, connections, category) {
  const body = document.getElementById(`${tileId}-body`);
  if (!body) return;

  const items = (connections || []).filter(c => c.category === category);

  if (!items.length) {
    setTileEmpty(tileId, { icon: 'fa-solid fa-plug', title: 'No connections configured', description: 'Configure connections in your environment to see them here.' });
    return;
  }

  body.innerHTML = items.map(conn => _renderConnCard(conn)).join('');

  // Wire up test buttons
  body.querySelectorAll('.conn-test-btn').forEach(btn => {
    btn.addEventListener('click', () => _testConnection(btn.dataset.id, btn));
  });
}

function _renderConnCard(conn) {
  const iconInfo = _getIcon(conn);
  const statusClass = (conn.status || 'not_configured').replace(/\s+/g, '_').toLowerCase();
  const statusLabel = _statusLabel(conn.status);
  const lastCheck = conn.last_check ? timeSince(conn.last_check) : 'never';
  const endpoint = conn.endpoint ? esc(_maskEndpoint(conn.endpoint)) : '';

  return `<div class="conn-card" data-conn-id="${esc(conn.id)}">
  <div class="conn-card-left">
    <i class="${iconInfo.icon}" style="color:${iconInfo.color}"></i>
    <div>
      <div class="conn-card-name">${esc(conn.name)}</div>
      ${endpoint ? `<div class="conn-card-endpoint">${endpoint}</div>` : ''}
    </div>
  </div>
  <div class="conn-card-status">
    <span class="conn-badge ${statusClass}">${statusLabel}</span>
    <span class="conn-card-meta">${lastCheck === 'never' ? 'never checked' : lastCheck}</span>
  </div>
  <button class="ds-btn ds-btn-ghost ds-btn-sm conn-test-btn" data-id="${esc(conn.id)}">Test</button>
</div>`;
}

function _getIcon(conn) {
  // AI providers get custom colors based on provider name
  if (conn.category === 'ai_provider' && conn.type) {
    const key = conn.type.toLowerCase();
    const color = AI_COLORS[key] || '#8b5cf6';
    return { icon: 'fa-solid fa-robot', color };
  }
  // Notification types
  if (conn.category === 'notification' && conn.type) {
    const key = conn.type.toLowerCase();
    if (CONN_ICONS[key]) return CONN_ICONS[key];
  }
  // Integration types
  if (conn.category === 'integration' && conn.type) {
    const key = conn.type.toLowerCase();
    if (CONN_ICONS[key]) return CONN_ICONS[key];
  }
  // Platform types
  if (conn.category === 'platform' && conn.type) {
    const key = conn.type.toLowerCase();
    if (CONN_ICONS[key]) return CONN_ICONS[key];
  }
  // Fallback: match on id
  if (CONN_ICONS[conn.id]) return CONN_ICONS[conn.id];
  // Default
  return { icon: 'fa-solid fa-plug', color: 'var(--muted)' };
}

function _statusLabel(status) {
  if (!status) return 'Not Configured';
  const s = status.toLowerCase();
  if (s === 'connected') return 'Connected';
  if (s === 'degraded') return 'Degraded';
  if (s === 'error') return 'Error';
  if (s === 'disabled') return 'Disabled';
  if (s === 'not_configured') return 'Not Configured';
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function _maskEndpoint(url) {
  try {
    const u = new URL(url);
    const host = u.hostname;
    // Mask middle of hostname: api***.example.com
    const parts = host.split('.');
    if (parts.length >= 2 && parts[0].length > 3) {
      parts[0] = parts[0].slice(0, 3) + '***';
    }
    return u.protocol + '//' + parts.join('.');
  } catch {
    // Not a valid URL, mask partially
    if (url.length > 20) {
      return url.slice(0, 10) + '***' + url.slice(-8);
    }
    return url;
  }
}

// ── Connection testing ───────────────────────────────────────────────────────

async function _testConnection(connId, btn) {
  if (_destroyed) return;

  const origHTML = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';

  try {
    const resp = await rawFetch(`/api/v1/admin/connections/${encodeURIComponent(connId)}/test`, {
      method: 'POST',
    });

    if (_destroyed) return;

    const result = await resp.json();
    const card = btn.closest('.conn-card');
    if (!card) return;

    const badge = card.querySelector('.conn-badge');
    const meta = card.querySelector('.conn-card-meta');

    if (resp.ok && result.status === 'connected') {
      // Success flash
      if (badge) {
        badge.className = 'conn-badge connected';
        badge.textContent = 'Connected';
      }
      if (meta) meta.textContent = 'just now';
      card.style.transition = 'background 0.3s ease';
      card.style.background = 'rgba(16,185,129,0.08)';
      setTimeout(() => { card.style.background = ''; }, 1500);
    } else {
      // Error flash
      const status = result.status || 'error';
      if (badge) {
        badge.className = `conn-badge ${status}`;
        badge.textContent = _statusLabel(status);
      }
      if (meta) meta.textContent = result.error || 'test failed';
      card.style.transition = 'background 0.3s ease';
      card.style.background = 'rgba(239,68,68,0.08)';
      setTimeout(() => { card.style.background = ''; }, 1500);
    }
  } catch (err) {
    if (_destroyed) return;
    const card = btn.closest('.conn-card');
    if (card) {
      const badge = card.querySelector('.conn-badge');
      if (badge) {
        badge.className = 'conn-badge error';
        badge.textContent = 'Error';
      }
      card.style.transition = 'background 0.3s ease';
      card.style.background = 'rgba(239,68,68,0.08)';
      setTimeout(() => { card.style.background = ''; }, 1500);
    }
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHTML || 'Test';
  }
}

async function _testAll() {
  if (_destroyed) return;

  const btn = document.getElementById('conn-test-all-btn');
  if (!btn) return;

  const origHTML = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="margin-right:4px"></i>Testing...';

  try {
    const resp = await rawFetch('/api/v1/admin/connections/test-all', { method: 'POST' });
    if (_destroyed) return;

    // Reload all data to reflect updated statuses
    await _loadData();
  } catch (err) {
    if (_destroyed) return;
    console.warn('Test all connections failed:', err);
  } finally {
    if (!_destroyed && btn) {
      btn.disabled = false;
      btn.innerHTML = origHTML || '<i class="fa-solid fa-arrows-rotate" style="margin-right:4px"></i>Test All';
    }
  }
}
