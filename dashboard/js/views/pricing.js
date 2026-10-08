/**
 * Modus Dashboard v2 — Pricing View
 * Two tiles: Pricing Overrides (editable) and Global Pricing (read-only).
 * Supports add/edit/delete overrides via modals.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError, exportActions } from '../tile.js';
import { rawFetch, esc } from '../api.js';
import { get } from '../state.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';
import { toast, confirm as uiConfirm } from '../toast.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _overridesCache = [];
let _globalCache = [];
let _historyCache = [];

// ── Public API ───────────────────────────────────────────────────────────────

export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'pricing');

  const tiles = [_createOverridesTile(), _createGlobalTile(), _createHistoryTile()];
  const layout = loadLayout('pricing', LAYOUTS.pricing);
  addTiles(_grid, tiles, layout);

  await _loadData();
}

export function destroy() {
  _destroyed = true;
  _overridesCache = [];
  _globalCache = [];
  _historyCache = [];
  _grid = null;
  _container = null;
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createOverridesTile() {
  return createTile({
    id: 'pricing-overrides',
    title: 'Pricing Overrides',
    icon: 'fa-solid fa-pen-to-square',
    iconBg: 'rgba(168,85,247,0.1)',
    iconColor: '#a855f7',
    filterable: true,
    filterPlaceholder: 'Filter overrides\u2026',
    onFilter: (q) => _filterRows('pricing-overrides-body', q),
    actions: [
      {
        label: '+ Add Override',
        onclick: () => _openOverrideModal(),
      },
      ...exportActions('pricing-overrides'),
    ],
  });
}

function _createGlobalTile() {
  return createTile({
    id: 'pricing-global',
    title: 'Global Pricing',
    icon: 'fa-solid fa-tags',
    iconBg: 'rgba(59,130,246,0.1)',
    iconColor: '#3b82f6',
    filterable: true,
    filterPlaceholder: 'Filter models\u2026',
    onFilter: (q) => _filterRows('pricing-global-body', q),
    actions: exportActions('pricing-global'),
  });
}

function _createHistoryTile() {
  return createTile({
    id: 'pricing-history',
    title: 'Override History',
    icon: 'fa-solid fa-clock-rotate-left',
    iconBg: 'rgba(6,182,212,0.1)',
    iconColor: '#06b6d4',
    filterable: true,
    filterPlaceholder: 'Filter history\u2026',
    onFilter: (q) => _filterRows('pricing-history-body', q),
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;
  setTileLoading('pricing-overrides', 'table');
  setTileLoading('pricing-global', 'table');
  setTileLoading('pricing-history', 'table');

  const [overrides, global, history] = await Promise.all([
    rawFetch('/api/v1/pricing/overrides').catch(() => null),
    rawFetch('/api/v1/pricing').catch(() => null),
    rawFetch('/api/v1/audit-log?resource_type=pricing_override&limit=50').catch(() => null),
  ]);

  if (_destroyed) return;

  try {
    _overridesCache = overrides && overrides.ok ? await overrides.json() : [];
  } catch (_) { _overridesCache = []; }

  try {
    if (global && global.ok) {
      const data = await global.json();
      _globalCache = Array.isArray(data) ? data : data.models || data.pricing || [];
    } else {
      _globalCache = [];
    }
  } catch (_) { _globalCache = []; }

  try {
    _historyCache = history && history.ok ? await history.json() : [];
    if (!Array.isArray(_historyCache)) _historyCache = _historyCache.entries || _historyCache.items || [];
  } catch (_) { _historyCache = []; }

  _renderOverrides(_overridesCache);
  _renderGlobal(_globalCache);
  _renderHistory(_historyCache);
}

// ── Rendering: Overrides ─────────────────────────────────────────────────────

function _renderOverrides(items) {
  const body = document.getElementById('pricing-overrides-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('pricing-overrides', {
      icon: 'fa-solid fa-pen-to-square',
      title: 'No overrides',
      description: 'Add a pricing override to apply custom rates.',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Provider</th>
            <th>Model</th>
            <th>Input $/1K</th>
            <th>Output $/1K</th>
            <th>Reason</th>
            <th>Scope</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((o, idx) => {
            const scope = o.team_id ? `Team: ${esc(o.team_id)}` : o.app_id ? `App: ${esc(o.app_id)}` : 'Global';
            return `<tr data-idx="${idx}">
              <td>${esc(o.provider || '')}</td>
              <td style="font-family:var(--mono);font-size:11px">${esc(o.model || '')}</td>
              <td>$${Number(o.input_cost_per_1k || 0).toFixed(4)}</td>
              <td>$${Number(o.output_cost_per_1k || 0).toFixed(4)}</td>
              <td>${esc(o.override_reason || '\u2014')}</td>
              <td style="font-size:11px">${scope}</td>
              <td>
                <div style="display:flex;gap:6px">
                  <button class="ds-btn ds-btn-ghost ds-btn-sm ovr-edit-btn" data-idx="${idx}" style="font-size:11px">Edit</button>
                  <button class="ds-btn ds-btn-ghost ds-btn-sm ovr-delete-btn" data-idx="${idx}" style="font-size:11px;color:var(--danger)">Delete</button>
                </div>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  body.querySelectorAll('.ovr-edit-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const o = items[parseInt(btn.dataset.idx, 10)];
      if (o) _openOverrideModal(o);
    });
  });
  body.querySelectorAll('.ovr-delete-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const o = items[parseInt(btn.dataset.idx, 10)];
      if (o) _deleteOverride(o);
    });
  });
}

// ── Rendering: Global ────────────────────────────────────────────────────────

function _renderGlobal(items) {
  const body = document.getElementById('pricing-global-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('pricing-global', {
      icon: 'fa-solid fa-tags',
      title: 'No pricing data',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow:auto;max-height:400px">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead style="position:sticky;top:0;background:var(--surface);z-index:1">
          <tr>
            <th>Provider</th>
            <th>Model</th>
            <th>Input $/1K</th>
            <th>Output $/1K</th>
          </tr>
        </thead>
        <tbody>
          ${items.map(p => `<tr>
            <td>${esc(p.provider || '')}</td>
            <td style="font-family:var(--mono);font-size:11px">${esc(p.model || '')}</td>
            <td>$${Number(p.input_cost_per_1k || 0).toFixed(4)}</td>
            <td>$${Number(p.output_cost_per_1k || 0).toFixed(4)}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>
  `;
}

// ── Rendering: History ───────────────────────────────────────────────────────

function _renderHistory(items) {
  const body = document.getElementById('pricing-history-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('pricing-history', {
      icon: 'fa-solid fa-clock-rotate-left',
      title: 'No history',
      description: 'Override changes will be recorded here when pricing overrides are created, modified, or deleted.',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>When</th>
            <th>Action</th>
            <th>Provider / Model</th>
            <th>Changed By</th>
            <th>Before</th>
            <th>After</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((entry, idx) => {
            const when = entry.occurred_at || '';
            // API actions are past-tense: created | updated | deleted | bulk_imported
            const action = esc({ created: 'create', updated: 'update', deleted: 'delete', bulk_imported: 'bulk import' }[entry.action] || entry.action || 'update');
            const actor = esc(entry.actor_id || '—');

            const before = entry.before || {};
            const after = entry.after || {};

            const provider = esc((after.provider || before.provider || '—'));
            const model = esc((after.model || before.model || '—'));

            const beforeStr = typeof before === 'object'
              ? `In: $${Number(before.input_cost_per_1k || 0).toFixed(4)} / Out: $${Number(before.output_cost_per_1k || 0).toFixed(4)}`
              : esc(String(before));
            const afterStr = action === 'bulk import'
              ? `${Number(after.created || 0)} created / ${Number(after.skipped || 0)} skipped`
              : typeof after === 'object'
              ? `In: $${Number(after.input_cost_per_1k || 0).toFixed(4)} / Out: $${Number(after.output_cost_per_1k || 0).toFixed(4)}`
              : esc(String(after));

            const actionColor = action === 'delete' ? 'var(--danger)' : action === 'create' ? 'var(--accent)' : 'var(--accent2)';

            return `<tr data-idx="${idx}">
              <td style="font-size:11px;white-space:nowrap">${when ? new Date(when).toLocaleString() : '—'}</td>
              <td><span style="font-size:10px;padding:2px 6px;border-radius:3px;background:${actionColor}15;color:${actionColor};font-weight:600;text-transform:uppercase">${action}</span></td>
              <td style="font-family:var(--mono);font-size:11px">${provider} / ${model}</td>
              <td style="font-size:11px;color:var(--muted)">${actor}</td>
              <td style="font-size:10px;font-family:var(--mono);color:var(--muted)">${action === 'create' || action === 'bulk import' ? '—' : beforeStr}</td>
              <td style="font-size:10px;font-family:var(--mono);color:var(--text)">${action === 'delete' ? '—' : afterStr}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

// ── Filter ───────────────────────────────────────────────────────────────────

function _filterRows(bodyId, query) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  const q = (query || '').toLowerCase();
  const rows = body.querySelectorAll('tbody tr');
  rows.forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

// ── Override Modal ───────────────────────────────────────────────────────────

function _openOverrideModal(existing) {
  const isEdit = !!existing;
  let _modalId = null;

  _modalId = openModal({
    title: isEdit ? 'Edit Pricing Override' : 'Add Pricing Override',
    maxWidth: '520px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Provider</label>
              <input class="ds-input" id="ovr-provider" value="${esc(existing?.provider || '')}" placeholder="openai" style="width:100%">
            </div>
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Model</label>
              <input class="ds-input" id="ovr-model" value="${esc(existing?.model || '')}" placeholder="gpt-4" style="width:100%">
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Input Cost ($/1K tokens)</label>
              <input class="ds-input" id="ovr-input-cost" type="number" step="0.0001" value="${existing?.input_cost_per_1k || ''}" style="width:100%">
            </div>
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Output Cost ($/1K tokens)</label>
              <input class="ds-input" id="ovr-output-cost" type="number" step="0.0001" value="${existing?.output_cost_per_1k || ''}" style="width:100%">
            </div>
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Reason</label>
            <input class="ds-input" id="ovr-reason" value="${esc(existing?.override_reason || '')}" placeholder="Negotiated rate" style="width:100%">
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Team ID (optional)</label>
              <input class="ds-input" id="ovr-team-id" value="${esc(existing?.team_id || '')}" placeholder="Scope to team" style="width:100%">
            </div>
            <div>
              <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">App ID (optional)</label>
              <input class="ds-input" id="ovr-app-id" value="${esc(existing?.app_id || '')}" placeholder="Scope to app" style="width:100%">
            </div>
          </div>
          <div id="ovr-result" style="display:none"></div>
          <button class="ds-btn ds-btn-primary" id="ovr-save-btn" style="margin-top:4px">${isEdit ? 'Update Override' : 'Save Override'}</button>
        </div>
      `;
      body.querySelector('#ovr-save-btn').addEventListener('click', () => _saveOverride(existing?.id, _modalId));
    },
  });
}

async function _saveOverride(editId, modalId) {
  const btn = document.getElementById('ovr-save-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Saving\u2026';

  const payload = {
    provider: (document.getElementById('ovr-provider')?.value || '').trim(),
    model: (document.getElementById('ovr-model')?.value || '').trim(),
    input_cost_per_1k: parseFloat(document.getElementById('ovr-input-cost')?.value || '0'),
    output_cost_per_1k: parseFloat(document.getElementById('ovr-output-cost')?.value || '0'),
    override_reason: (document.getElementById('ovr-reason')?.value || '').trim() || null,
    team_id: (document.getElementById('ovr-team-id')?.value || '').trim() || null,
    app_id: (document.getElementById('ovr-app-id')?.value || '').trim() || null,
  };

  const url = editId ? `/api/v1/pricing/overrides/${editId}` : '/api/v1/pricing/overrides';
  const method = editId ? 'PUT' : 'POST';

  try {
    const resp = await rawFetch(url, {
      method,
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
    const result = document.getElementById('ovr-result');
    if (result) {
      result.style.display = 'block';
      result.innerHTML = `<div style="color:var(--danger);font-size:12px">${esc(e.message)}</div>`;
    }
    btn.disabled = false;
    btn.textContent = editId ? 'Update Override' : 'Save Override';
  }
}

// ── Delete Override ──────────────────────────────────────────────────────────

async function _deleteOverride(override) {
  const ok = await uiConfirm(
    `Delete pricing override for ${override.provider}/${override.model}?`,
    { title: 'Delete Override', confirmLabel: 'Delete', confirmStyle: 'danger' }
  );
  if (!ok) return;
  try {
    const resp = await rawFetch(`/api/v1/pricing/overrides/${override.id}`, { method: 'DELETE' });
    if (!resp.ok && resp.status !== 204) throw new Error(resp.statusText);
    toast('Override deleted', 'success');
    _loadData();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}
