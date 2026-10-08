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
import { formatApiError } from '../policy-form.js';

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
        onclick: () => _openThresholdModal(null),
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
                <button class="ds-btn ds-btn-ghost ds-btn-sm thr-edit-btn" data-idx="${idx}" style="font-size:11px">Edit</button>
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

  // Edit
  body.querySelectorAll('.thr-edit-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const t = sorted[parseInt(btn.dataset.idx, 10)];
      if (t) _openThresholdModal(t);
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

// ── Create / Edit Threshold Modal ────────────────────────────────────────────
// The form posts exactly what POST /api/v1/thresholds validates: team_id (a
// real team), scope in app|team|provider, metric in total_cost|input_tokens|
// output_tokens|call_count, period, critical_value > 0, warning_value <
// critical_value, plus app_id / provider when the scope needs them.

const _lbl = 'font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px';

async function _fetchList(path) {
  try {
    const resp = await rawFetch(path);
    if (!resp.ok) return null;
    const data = await resp.json();
    return Array.isArray(data) ? data : null;
  } catch (_) {
    return null;
  }
}

function _opt(value, label, selected) {
  return `<option value="${esc(value)}" ${selected ? 'selected' : ''}>${esc(label)}</option>`;
}

function _openThresholdModal(existing = null) {
  const isEdit = !!existing;
  let _modalId = null;
  const lock = isEdit ? 'disabled' : '';
  _modalId = openModal({
    title: isEdit ? 'Edit Alert Rule' : 'New Alert Rule',
    maxWidth: '520px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
          <div>
            <label style="${_lbl}">Name</label>
            <input class="ds-input" id="thr-name" placeholder="Daily cost limit" value="${esc(existing?.name || '')}" style="width:100%">
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Scope</label>
              <select class="ds-select" id="thr-scope" ${lock} style="width:100%">
                <option value="team" ${existing?.scope === 'team' ? 'selected' : ''}>Team</option>
                <option value="app" ${!existing || existing.scope === 'app' ? 'selected' : ''}>App</option>
                <option value="provider" ${existing?.scope === 'provider' ? 'selected' : ''}>Provider</option>
              </select>
            </div>
            <div>
              <label style="${_lbl}">Metric</label>
              <select class="ds-select" id="thr-metric" ${lock} style="width:100%">
                <option value="total_cost">Cost (USD)</option>
                <option value="input_tokens">Input tokens</option>
                <option value="output_tokens">Output tokens</option>
                <option value="call_count">API calls</option>
              </select>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Team</label>
              <select class="ds-select" id="thr-team" ${lock} style="width:100%"><option value="">Loading teams…</option></select>
            </div>
            <div id="thr-app-wrap">
              <label style="${_lbl}">App</label>
              <select class="ds-select" id="thr-app" ${lock} style="width:100%"><option value="">Loading apps…</option></select>
            </div>
            <div id="thr-provider-wrap" style="display:none">
              <label style="${_lbl}">Provider</label>
              <input class="ds-input" id="thr-provider" ${lock} placeholder="openai" value="${esc(existing?.provider || '')}" style="width:100%">
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Period</label>
              <select class="ds-select" id="thr-period" ${lock} style="width:100%">
                <option value="hourly">Hourly</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
              </select>
            </div>
            ${isEdit ? `<div>
              <label style="${_lbl}">Active</label>
              <label style="display:flex;align-items:center;gap:6px;padding-top:4px;cursor:pointer">
                <input type="checkbox" id="thr-active" ${existing.is_active !== false ? 'checked' : ''}>
                <span style="font-size:12px;color:var(--muted)">Enabled</span>
              </label>
            </div>` : '<div></div>'}
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Warning value (90% tier override)</label>
              <input class="ds-input" id="thr-warning" type="number" min="0" step="any" placeholder="Optional" value="${existing?.warning_value ?? ''}" style="width:100%">
            </div>
            <div>
              <label style="${_lbl}">Critical value *</label>
              <input class="ds-input" id="thr-critical" type="number" min="0" step="any" placeholder="Required" value="${existing?.critical_value ?? ''}" style="width:100%">
            </div>
          </div>
          <div id="thr-result" style="display:none"></div>
          <button class="ds-btn ds-btn-primary" id="thr-save-btn" style="margin-top:4px">${isEdit ? 'Save Changes' : 'Create Rule'}</button>
        </div>
      `;
      if (existing) {
        body.querySelector('#thr-metric').value = existing.metric;
        body.querySelector('#thr-period').value = existing.period;
      } else {
        body.querySelector('#thr-period').value = 'daily';
      }

      const scopeSel = body.querySelector('#thr-scope');
      const teamSel = body.querySelector('#thr-team');
      const appSel = body.querySelector('#thr-app');
      let apps = [];
      const syncScope = () => {
        const s = scopeSel.value;
        body.querySelector('#thr-app-wrap').style.display = s === 'app' ? '' : 'none';
        body.querySelector('#thr-provider-wrap').style.display = s === 'provider' ? '' : 'none';
        const teamId = teamSel.value;
        const choices = apps.filter(a => !teamId || a.team_id === teamId);
        appSel.innerHTML = '<option value="">Select an app…</option>' +
          choices.map(a => _opt(a.id, `${a.app_name || a.app_id} (${a.app_id})`, existing?.app_id === a.id)).join('');
      };
      scopeSel.addEventListener('change', syncScope);
      teamSel.addEventListener('change', syncScope);
      syncScope();

      (async () => {
        const [teams, appList] = await Promise.all([
          _fetchList('/api/v1/teams?limit=500'),
          _fetchList('/api/v1/apps?limit=500'),
        ]);
        if (_destroyed) return;
        apps = appList || [];
        if (teams === null) {
          teamSel.innerHTML = '<option value="">Could not load teams</option>';
        } else {
          teamSel.innerHTML = '<option value="">Select a team…</option>' +
            teams.map(t => _opt(t.id, `${t.name} (${t.slug})`, existing?.team_id === t.id)).join('');
        }
        syncScope();
      })();

      body.querySelector('#thr-save-btn').addEventListener('click', () => _saveThreshold(existing, _modalId));
    },
  });
}

function _showThrError(msg) {
  const result = document.getElementById('thr-result');
  if (!result) return;
  result.style.display = 'block';
  result.innerHTML = `<div style="color:var(--danger);font-size:12px">${esc(msg)}</div>`;
}

/**
 * Validate the form and build the request body. Returns {payload, error}.
 * Mirrors the server rules so the user gets a precise message before the call.
 */
function _buildThresholdPayload(existing) {
  const val = (id) => (document.getElementById(id)?.value ?? '').trim();
  const name = val('thr-name');
  if (!name) return { error: 'Name is required.' };
  const critical = val('thr-critical');
  const warning = val('thr-warning');
  if (critical === '' || !(parseFloat(critical) > 0)) return { error: 'Critical value must be a number greater than 0.' };
  if (warning !== '' && (!(parseFloat(warning) >= 0) || parseFloat(warning) >= parseFloat(critical))) {
    return { error: 'Warning value must be lower than the critical value.' };
  }
  // Decimals go over the wire as strings so no precision is lost.
  if (existing) {
    return {
      payload: {
        name,
        warning_value: warning === '' ? null : warning,
        critical_value: critical,
        is_active: document.getElementById('thr-active')?.checked !== false,
      },
    };
  }
  const scope = val('thr-scope');
  const teamId = val('thr-team');
  if (!teamId) return { error: 'Select a team.' };
  const payload = {
    name,
    team_id: teamId,
    scope,
    metric: val('thr-metric'),
    period: val('thr-period'),
    critical_value: critical,
  };
  if (warning !== '') payload.warning_value = warning;
  if (scope === 'app') {
    const appId = val('thr-app');
    if (!appId) return { error: 'Select an app for an app-scoped rule.' };
    payload.app_id = appId;
  }
  if (scope === 'provider') {
    const provider = val('thr-provider');
    if (!provider) return { error: 'Enter a provider for a provider-scoped rule.' };
    payload.provider = provider;
  }
  return { payload };
}

async function _saveThreshold(existing, modalId) {
  const btn = document.getElementById('thr-save-btn');
  if (!btn) return;
  const idle = existing ? 'Save Changes' : 'Create Rule';

  const { payload, error } = _buildThresholdPayload(existing);
  if (error) { _showThrError(error); return; }

  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const resp = await rawFetch(existing ? `/api/v1/thresholds/${encodeURIComponent(existing.id)}` : '/api/v1/thresholds', {
      method: existing ? 'PATCH' : 'POST',
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(formatApiError(err, resp.status));
    }
    closeModal(modalId);
    toast(existing ? 'Alert rule updated' : 'Alert rule created', 'success');
    _loadData();
  } catch (e) {
    _showThrError(e.message);
    btn.disabled = false;
    btn.textContent = idle;
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
    if (!resp.ok && resp.status !== 204) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(formatApiError(err, resp.status));
    }
    toast('Alert rule deleted', 'success');
    _loadData();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}
