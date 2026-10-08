/**
 * Modus Dashboard v2 — Thresholds View
 * Renders alert rules / threshold configuration with a table
 * and create/delete operations via modal.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError } from '../tile.js';
import { rawFetch, esc } from '../api.js';
import { get } from '../state.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';
import { toast, confirm as uiConfirm } from '../toast.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _thresholdsCache = [];

// ── Public API ───────────────────────────────────────────────────────────────

export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'thresholds');

  const tiles = [_createThresholdsTile()];
  const layout = loadLayout('thresholds', LAYOUTS.thresholds);
  addTiles(_grid, tiles, layout);

  await _loadData();
}

export function destroy() {
  _destroyed = true;
  _thresholdsCache = [];
  _grid = null;
  _container = null;
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createThresholdsTile() {
  return createTile({
    id: 'thresholds-table',
    title: 'Alert Rules',
    icon: 'fa-solid fa-gauge-high',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
    filterable: true,
    filterPlaceholder: 'Filter thresholds\u2026',
    onFilter: (q) => _filterTable(q),
    actions: [
      {
        label: '+ New Threshold',
        onclick: () => _openThresholdModal(),
      },
    ],
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;
  setTileLoading('thresholds-table', 'table');

  try {
    const resp = await rawFetch('/api/v1/thresholds').catch(() => null);
    if (_destroyed) return;
    if (resp && resp.ok) {
      _thresholdsCache = await resp.json().catch(() => []);
    } else {
      _thresholdsCache = [];
    }
    _renderTable(_thresholdsCache);
  } catch (err) {
    if (_destroyed) return;
    console.error('[thresholds] data fetch failed:', err);
    setTileError('thresholds-table', 'Failed to load thresholds', () => _loadData());
  }
}

// ── Rendering ────────────────────────────────────────────────────────────────

function _renderTable(items) {
  const body = document.getElementById('thresholds-table-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('thresholds-table', {
      icon: 'fa-solid fa-gauge-high',
      title: 'No alert rules',
      description: 'Create your first alert rule to get notified when metrics cross thresholds.',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Name</th>
            <th>Scope</th>
            <th>Metric</th>
            <th>Period</th>
            <th>Warning</th>
            <th>Critical</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((t, idx) => {
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

  body.querySelectorAll('.thr-delete-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const t = items[parseInt(btn.dataset.idx, 10)];
      if (t) _deleteThreshold(t);
    });
  });
}

// ── Filter ───────────────────────────────────────────────────────────────────

function _filterTable(query) {
  const body = document.getElementById('thresholds-table-body');
  if (!body) return;
  const q = (query || '').toLowerCase();
  const rows = body.querySelectorAll('tbody tr');
  rows.forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
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

// ── Delete Threshold ─────────────────────────────────────────────────────────

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
