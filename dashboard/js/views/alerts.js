/**
 * Modus Dashboard v2 — Alerts & Thresholds View (Combined)
 * Top: Alert KPI stats + Alert History table
 * Bottom: Threshold Rules table with create/delete
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError } from '../tile.js';
import { apiFetch, rawFetch, esc } from '../api.js';
import { fmtCost, fmtNum, timeSince } from '../format.js';
import { get } from '../state.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';
import { toast, confirm as uiConfirm } from '../toast.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _alertsCache = [];
let _thresholdsCache = [];
let _alertSortCol = 'fired_at';
let _alertSortDir = -1; // -1 = desc
let _thrSortCol = 'name';
let _thrSortDir = 1;

// ── Public API ───────────────────────────────────────────────────────────────

export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'alerts');

  const tiles = [_createStatsTile(), _createTableTile(), _createThresholdsTile()];
  const layout = loadLayout('alerts', [
    { id: 'alerts-stats',      x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true, noMove: true },
    { id: 'alerts-table',      x: 0, y: 2,  w: 12, h: 7,  minW: 6,  minH: 4 },
    { id: 'thresholds-table',  x: 0, y: 9,  w: 12, h: 6,  minW: 6,  minH: 4 },
  ]);
  addTiles(_grid, tiles, layout);

  await _loadData();
}

export function destroy() {
  _destroyed = true;
  _alertsCache = [];
  _thresholdsCache = [];
  _grid = null;
  _container = null;
}

/**
 * Re-fetch this view's data. Called by the Settings -> Auto-refresh timer
 * (js/auto-refresh.js); a no-op once the view has been destroyed.
 */
export function refresh() {
  return _destroyed ? Promise.resolve() : _loadData();
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createStatsTile() {
  const tile = createTile({
    id: 'alerts-stats',
    title: '',
    className: 'kpi-row',
  });
  const header = tile.querySelector('.gs-tile-header');
  if (header) header.style.display = 'none';
  return tile;
}

function _createTableTile() {
  return createTile({
    id: 'alerts-table',
    title: 'Alert History',
    icon: 'fa-solid fa-bell',
    iconBg: 'rgba(255,107,107,0.1)',
    iconColor: '#ff6b6b',
    filterable: true,
    filterPlaceholder: 'Filter alerts\u2026',
    onFilter: (q) => _filterTable('alerts-table-body', q),
  });
}

function _createThresholdsTile() {
  return createTile({
    id: 'thresholds-table',
    title: 'Alert Rules',
    icon: 'fa-solid fa-gauge-high',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
    filterable: true,
    filterPlaceholder: 'Filter rules\u2026',
    onFilter: (q) => _filterTable('thresholds-table-body', q),
    actions: [
      {
        label: '+ New Rule',
        onclick: () => _openThresholdModal(),
      },
    ],
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;
  setTileLoading('alerts-stats', 'cards');
  setTileLoading('alerts-table', 'table');
  setTileLoading('thresholds-table', 'table');

  const [alerts, thrResp] = await Promise.all([
    apiFetch('recent-alerts', { limit: 100 }).catch(() => null),
    rawFetch('/api/v1/thresholds').then(r => r && r.ok ? r.json() : []).catch(() => []),
  ]);

  if (_destroyed) return;

  _alertsCache = Array.isArray(alerts) ? alerts : [];
  _thresholdsCache = Array.isArray(thrResp) ? thrResp : [];

  _renderStats(_alertsCache);
  _renderAlerts(_alertsCache);
  _renderThresholds(_thresholdsCache);
}

// ── Sorting helper ──────────────────────────────────────────────────────────

function _sortItems(items, col, dir) {
  return [...items].sort((a, b) => {
    let va = a[col], vb = b[col];
    if (va == null) va = '';
    if (vb == null) vb = '';
    if (typeof va === 'number' && typeof vb === 'number') return (va - vb) * dir;
    return String(va).localeCompare(String(vb)) * dir;
  });
}

// ── Rendering: Stats ─────────────────────────────────────────────────────────

function _renderStats(items) {
  const body = document.getElementById('alerts-stats-body');
  if (!body) return;

  const total = items.length;
  const critical = items.filter(a => a.severity === 'critical').length;
  const warning = items.filter(a => a.severity === 'warning').length;
  const active = items.filter(a => !a.acknowledged).length;
  const rules = _thresholdsCache.length;

  body.innerHTML = `
    <div class="kpi-card"><div class="kpi-label">Total Alerts</div><div class="kpi-value">${fmtNum(total)}</div></div>
    <div class="kpi-card"><div class="kpi-label">Critical</div><div class="kpi-value" style="color:var(--danger)">${fmtNum(critical)}</div></div>
    <div class="kpi-card"><div class="kpi-label">Warning</div><div class="kpi-value" style="color:var(--warn)">${fmtNum(warning)}</div></div>
    <div class="kpi-card"><div class="kpi-label">Active</div><div class="kpi-value">${fmtNum(active)}</div></div>
    <div class="kpi-card"><div class="kpi-label">Alert Rules</div><div class="kpi-value" style="color:var(--accent2)">${fmtNum(rules)}</div></div>
  `;
}

// ── Rendering: Alert Table ──────────────────────────────────────────────────

function _renderAlerts(items) {
  const body = document.getElementById('alerts-table-body');
  if (!body) return;

  if (!items || items.length === 0) {
    setTileEmpty('alerts-table', {
      icon: 'fa-solid fa-bell',
      title: 'No alerts',
      description: 'All clear \u2014 no alerts in this period.',
    });
    return;
  }

  const sorted = _sortItems(items, _alertSortCol, _alertSortDir);
  const sevColors = { critical: 'var(--danger)', warning: 'var(--warn)', info: 'var(--accent2)' };
  const sortIcon = (col) => _alertSortCol === col ? (_alertSortDir > 0 ? ' \u25B2' : ' \u25BC') : '';

  body.innerHTML = `
    <div style="overflow:auto;max-height:360px">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead style="position:sticky;top:0;background:var(--surface);z-index:1">
          <tr>
            <th style="width:6px"></th>
            <th class="sortable-th" data-col="metric" style="cursor:pointer">Metric${sortIcon('metric')}</th>
            <th class="sortable-th" data-col="severity" style="cursor:pointer">Severity${sortIcon('severity')}</th>
            <th class="sortable-th" data-col="app_id" style="cursor:pointer">App${sortIcon('app_id')}</th>
            <th class="sortable-th" data-col="actual_value" style="cursor:pointer">Actual${sortIcon('actual_value')}</th>
            <th class="sortable-th" data-col="threshold_value" style="cursor:pointer">Threshold${sortIcon('threshold_value')}</th>
            <th class="sortable-th" data-col="fired_at" style="cursor:pointer">Fired${sortIcon('fired_at')}</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          ${sorted.map((a, idx) => {
            const sevColor = sevColors[a.severity] || 'var(--muted)';
            const statusLabel = a.acknowledged ? 'Acknowledged' : 'Active';
            const statusColor = a.acknowledged ? 'var(--muted)' : sevColor;
            return `<tr class="clickable" data-idx="${idx}" style="cursor:pointer">
              <td><span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${sevColor}"></span></td>
              <td style="font-weight:500;color:var(--text)">${esc(a.metric || '\u2014')}</td>
              <td><span style="color:${sevColor};font-size:11px;text-transform:uppercase;font-weight:600">${esc(a.severity || 'info')}</span></td>
              <td style="font-size:11px;color:var(--muted)">${esc(a.app_id || 'All')}</td>
              <td style="font-family:var(--mono);font-size:11px">${fmtCost(a.actual_value)}</td>
              <td style="font-family:var(--mono);font-size:11px">${fmtCost(a.threshold_value)}</td>
              <td style="font-size:11px;color:var(--muted)">${a.fired_at ? timeSince(a.fired_at) : '\u2014'}</td>
              <td><span style="color:${statusColor};font-size:11px;font-weight:500">${statusLabel}</span></td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  // Sortable headers
  body.querySelectorAll('.sortable-th').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (_alertSortCol === col) { _alertSortDir *= -1; } else { _alertSortCol = col; _alertSortDir = 1; }
      _renderAlerts(_alertsCache);
    });
  });

  // Click detail
  body.querySelectorAll('tr.clickable').forEach(row => {
    row.addEventListener('click', () => {
      const alert = sorted[parseInt(row.dataset.idx, 10)];
      if (alert) _showAlertModal(alert);
    });
  });
}

// ── Rendering: Thresholds Table ─────────────────────────────────────────────

function _renderThresholds(items) {
  const body = document.getElementById('thresholds-table-body');
  if (!body) return;

  if (!items || items.length === 0) {
    setTileEmpty('thresholds-table', {
      icon: 'fa-solid fa-gauge-high',
      title: 'No alert rules',
      description: 'Create your first alert rule to get notified when metrics cross thresholds.',
    });
    return;
  }

  const sorted = _sortItems(items, _thrSortCol, _thrSortDir);
  const sortIcon = (col) => _thrSortCol === col ? (_thrSortDir > 0 ? ' \u25B2' : ' \u25BC') : '';

  body.innerHTML = `
    <div style="overflow:auto;max-height:300px">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead style="position:sticky;top:0;background:var(--surface);z-index:1">
          <tr>
            <th class="sortable-thr" data-col="name" style="cursor:pointer">Name${sortIcon('name')}</th>
            <th class="sortable-thr" data-col="scope" style="cursor:pointer">Scope${sortIcon('scope')}</th>
            <th class="sortable-thr" data-col="metric" style="cursor:pointer">Metric${sortIcon('metric')}</th>
            <th class="sortable-thr" data-col="period" style="cursor:pointer">Period${sortIcon('period')}</th>
            <th>Warning</th>
            <th>Critical</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${sorted.map((t, idx) => {
            const status = t.is_active
              ? '<span style="color:var(--accent);font-weight:500">Active</span>'
              : '<span style="color:var(--muted)">Inactive</span>';
            return `<tr data-idx="${idx}">
              <td style="font-weight:600">${esc(t.name || '')}</td>
              <td>${esc(t.scope || '')}</td>
              <td style="font-family:var(--mono);font-size:11px">${esc(t.metric || '')}</td>
              <td>${esc(t.period || '')}</td>
              <td>${t.warning_value != null ? t.warning_value : '\u2014'}</td>
              <td style="color:var(--danger)">${t.critical_value != null ? t.critical_value : '\u2014'}</td>
              <td>${status}</td>
              <td>
                <button class="ds-btn ds-btn-ghost ds-btn-sm thr-delete-btn" data-idx="${idx}" style="font-size:11px;color:var(--danger)">Delete</button>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  // Sort
  body.querySelectorAll('.sortable-thr').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (_thrSortCol === col) { _thrSortDir *= -1; } else { _thrSortCol = col; _thrSortDir = 1; }
      _renderThresholds(_thresholdsCache);
    });
  });

  // Delete
  body.querySelectorAll('.thr-delete-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const t = sorted[parseInt(btn.dataset.idx, 10)];
      if (t) _deleteThreshold(t);
    });
  });
}

// ── Filter ───────────────────────────────────────────────────────────────────

function _filterTable(bodyId, query) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  const q = (query || '').toLowerCase();
  body.querySelectorAll('tbody tr').forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

// ── Alert Detail Modal ───────────────────────────────────────────────────────

function _showAlertModal(alert) {
  const sevColors = { critical: 'var(--danger)', warning: 'var(--warn)', info: 'var(--accent2)' };
  const sevColor = sevColors[alert.severity] || 'var(--muted)';
  const firedAt = alert.fired_at ? new Date(alert.fired_at).toLocaleString() : '\u2014';

  openModal({
    title: 'Alert Snapshot',
    maxWidth: '480px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:12px;padding:4px 0">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="display:flex;align-items:center;gap:8px">
              <span style="width:10px;height:10px;border-radius:50%;background:${sevColor};display:inline-block"></span>
              <span style="font-weight:600;font-size:14px;color:var(--text)">${esc(alert.metric || '\u2014')}</span>
            </span>
            <span style="background:${sevColor}20;color:${sevColor};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;text-transform:uppercase">${esc(alert.severity || 'info')}</span>
          </div>
          <div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12px">
            <span style="color:var(--muted)">Actual Value</span>
            <span style="font-family:var(--mono);color:var(--text)">${fmtCost(alert.actual_value)}</span>
            <span style="color:var(--muted)">Threshold</span>
            <span style="font-family:var(--mono);color:var(--text)">${fmtCost(alert.threshold_value)}</span>
            <span style="color:var(--muted)">App</span>
            <span style="color:var(--text)">${esc(alert.app_id || 'All apps')}</span>
            <span style="color:var(--muted)">Fired At</span>
            <span style="font-family:var(--mono);font-size:11px;color:var(--text)">${firedAt}</span>
            <span style="color:var(--muted)">Acknowledged</span>
            <span style="color:var(--text)">${alert.acknowledged ? 'Yes' : 'No'}</span>
          </div>
        </div>
      `;
    },
  });
}

// ── Create Threshold Modal ───────────────────────────────────────────────────

function _openThresholdModal() {
  let _modalId = null;
  _modalId = openModal({
    title: 'New Alert Rule',
    maxWidth: '520px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Name</label>
            <input class="ds-input" id="thr-name" placeholder="Daily cost limit" style="width:100%">
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Scope</label>
              <select class="ds-select" id="thr-scope" style="width:100%">
                <option value="global">Global</option>
                <option value="team">Team</option>
                <option value="app">App</option>
                <option value="provider">Provider</option>
              </select>
            </div>
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Metric</label>
              <select class="ds-select" id="thr-metric" style="width:100%">
                <option value="cost">Cost (USD)</option>
                <option value="tokens">Tokens</option>
                <option value="calls">API Calls</option>
                <option value="latency_p99">Latency P99</option>
                <option value="error_rate">Error Rate</option>
              </select>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Period</label>
              <select class="ds-select" id="thr-period" style="width:100%">
                <option value="hourly">Hourly</option>
                <option value="daily" selected>Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
              </select>
            </div>
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Provider (optional)</label>
              <input class="ds-input" id="thr-provider" placeholder="openai" style="width:100%">
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Warning Value</label>
              <input class="ds-input" id="thr-warning" type="number" step="0.01" placeholder="Optional" style="width:100%">
            </div>
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Critical Value</label>
              <input class="ds-input" id="thr-critical" type="number" step="0.01" placeholder="Required" style="width:100%">
            </div>
          </div>
          <div id="thr-result" style="display:none"></div>
          <button class="ds-btn ds-btn-primary" id="thr-save-btn" style="margin-top:4px">Create Rule</button>
        </div>
      `;
      body.querySelector('#thr-save-btn').addEventListener('click', () => _saveThreshold(_modalId));
    },
  });
}

async function _saveThreshold(modalId) {
  const btn = document.getElementById('thr-save-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Creating\u2026';

  const payload = {
    name: (document.getElementById('thr-name')?.value || '').trim(),
    scope: document.getElementById('thr-scope')?.value || 'global',
    provider: (document.getElementById('thr-provider')?.value || '').trim() || null,
    metric: document.getElementById('thr-metric')?.value || 'cost',
    period: document.getElementById('thr-period')?.value || 'daily',
    warning_value: document.getElementById('thr-warning')?.value ? parseFloat(document.getElementById('thr-warning').value) : null,
    critical_value: parseFloat(document.getElementById('thr-critical')?.value || '0'),
  };

  try {
    const resp = await rawFetch('/api/v1/thresholds', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail?.message || err.detail || resp.statusText);
    }
    closeModal(modalId);
    _loadData();
  } catch (e) {
    const result = document.getElementById('thr-result');
    if (result) {
      result.style.display = 'block';
      result.innerHTML = `<div style="color:var(--danger);font-size:12px">${esc(e.message)}</div>`;
    }
    btn.disabled = false;
    btn.textContent = 'Create Rule';
  }
}

async function _deleteThreshold(threshold) {
  const ok = await uiConfirm(
    `Delete alert rule "${threshold.name}"?`,
    { title: 'Delete Alert Rule', confirmLabel: 'Delete', confirmStyle: 'danger' }
  );
  if (!ok) return;
  try {
    const resp = await rawFetch(`/api/v1/thresholds/${threshold.id}`, { method: 'DELETE' });
    if (!resp.ok && resp.status !== 204) throw new Error(resp.statusText);
    toast('Alert rule deleted', 'success');
    _loadData();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}
