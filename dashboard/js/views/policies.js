/**
 * Modus Dashboard v2 — Policies View
 * Renders the governance policies table with create/edit modal.
 * Policy modal dynamically renders config fields based on policy_type.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError, exportActions } from '../tile.js';
import { rawFetch, esc } from '../api.js';
import { fmtDate, fmtCost } from '../format.js';
import { get } from '../state.js';
import { openModal, closeModal } from '../modal.js';
import {
  POLICY_TYPES, SCOPES, EFFECTS, typeSpec, configToFormValues,
  buildPolicyPayload, formatApiError,
} from '../policy-form.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _policiesCache = [];
let _decisionsCache = [];

// ── Public API ───────────────────────────────────────────────────────────────

export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'policies');

  const tiles = [_createPoliciesTile(), _createDecisionsTile()];
  const layout = loadLayout('policies', LAYOUTS.policies);
  addTiles(_grid, tiles, layout);

  await _loadData();
}

export function destroy() {
  _destroyed = true;
  _policiesCache = [];
  _decisionsCache = [];
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

// ── Decisions Tile (GAP-3) ───────────────────────────────────────────────────

function _createDecisionsTile() {
  return createTile({
    id: 'policies-decisions',
    title: 'Enforcement Detail',
    icon: 'fa-solid fa-gavel',
    iconBg: 'rgba(239,68,68,0.1)',
    iconColor: '#ef4444',
    filterable: true,
    filterPlaceholder: 'Filter decisions\u2026',
    onFilter: (q) => _filterDecisions(q),
    actions: [
      {
        icon: 'fa-solid fa-rotate-right',
        onclick: () => _loadDecisions(),
      },
    ],
  });
}

async function _loadDecisions() {
  if (_destroyed) return;
  setTileLoading('policies-decisions', 'table');
  try {
    const resp = await rawFetch('/api/v1/policy/decisions?limit=50').catch(() => null);
    if (_destroyed) return;
    if (resp && resp.ok) {
      const data = await resp.json().catch(() => []);
      _decisionsCache = Array.isArray(data) ? data : (data.decisions || data.items || []);
    } else {
      _decisionsCache = [];
    }
    _renderDecisions(_decisionsCache);
  } catch (err) {
    if (_destroyed) return;
    console.error('[policies] decisions fetch failed:', err);
    setTileError('policies-decisions', 'Failed to load decisions', () => _loadDecisions());
  }
}

function _filterDecisions(query) {
  const body = document.getElementById('policies-decisions-body');
  if (!body) return;
  const q = (query || '').toLowerCase();
  body.querySelectorAll('tbody tr').forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

function _renderDecisions(items) {
  const body = document.getElementById('policies-decisions-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('policies-decisions', {
      icon: 'fa-solid fa-gavel',
      title: 'No enforcement decisions',
      description: 'Policy decisions will appear here when policies block, throttle, or redirect API calls.',
    });
    return;
  }

  // Backend field is `decision` (deny/allow/throttle), not `action`
  const total = items.length;
  const denied = items.filter(d => d.decision === 'deny' || d.decision === 'block').length;
  const throttled = items.filter(d => d.decision === 'throttle').length;
  const totalDeniedCost = items
    .filter(d => d.decision === 'deny' || d.decision === 'block')
    .reduce((s, d) => s + (parseFloat(d.request_estimated_cost) || 0), 0);

  const reasonCounts = {};
  items.filter(d => d.decision === 'deny' || d.decision === 'block').forEach(d => {
    const reason = d.reason || d.policy_name || 'Unknown';
    reasonCounts[reason] = (reasonCounts[reason] || 0) + 1;
  });
  const topReasons = Object.entries(reasonCounts).sort((a, b) => b[1] - a[1]).slice(0, 5);

  const statCard = (label, value, color) => `
    <div style="flex:1;min-width:80px;padding:10px;border-radius:8px;background:var(--surface);border:1px solid var(--border);text-align:center">
      <div style="font-size:10px;text-transform:uppercase;color:var(--muted)">${label}</div>
      <div style="font-size:18px;font-weight:700;color:${color}">${value}</div>
    </div>`;

  const rows = items.slice(0, 25).map(d => {
    const decision = d.decision || '\u2014';
    const actionColor = (decision === 'deny' || decision === 'block') ? 'var(--danger)' : decision === 'throttle' ? 'var(--warn)' : 'var(--accent2)';
    const time = d.decided_at ? new Date(d.decided_at).toLocaleString() : '\u2014';
    return '<tr>' +
      '<td style="font-size:10px;white-space:nowrap">' + esc(time) + '</td>' +
      '<td style="font-weight:500">' + esc(d.policy_name || d.policy_id || '\u2014') + '</td>' +
      '<td><span style="font-size:10px;padding:2px 6px;border-radius:3px;background:' + actionColor + '15;color:' + actionColor + ';font-weight:600;text-transform:uppercase">' + esc(decision) + '</span></td>' +
      '<td style="font-family:var(--mono);font-size:10px">' + esc(d.request_provider || '\u2014') + '</td>' +
      '<td style="font-family:var(--mono);font-size:10px">' + esc(d.request_model || '\u2014') + '</td>' +
      '<td style="text-align:right;font-family:var(--mono)">' + fmtCost(d.request_estimated_cost || 0) + '</td>' +
      '<td style="font-size:10px;color:var(--muted);max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(d.reason || '\u2014') + '</td>' +
      '</tr>';
  }).join('');

  body.innerHTML = `
    <div style="padding:12px;display:flex;flex-direction:column;gap:16px">
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        ${statCard('Total', total, 'var(--text)')}
        ${statCard('Denied', denied, 'var(--danger)')}
        ${statCard('Throttled', throttled, 'var(--warn)')}
        ${statCard('Denied Cost', fmtCost(totalDeniedCost), 'var(--danger)')}
      </div>

      ${topReasons.length > 0 ? `
      <div>
        <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Top Denial Reasons</div>
        ${topReasons.map(([reason, count]) => `
          <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:12px">
            <span style="color:var(--text)">${esc(reason)}</span>
            <span style="font-family:var(--mono);color:var(--danger);font-weight:600">${count}</span>
          </div>
        `).join('')}
      </div>` : ''}

      <div style="overflow-x:auto">
        <table class="ds-table" style="width:100%;font-size:11px">
          <thead>
            <tr>
              <th>Time</th>
              <th>Policy</th>
              <th>Action</th>
              <th>Provider</th>
              <th>Model</th>
              <th style="text-align:right">Est. Cost</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createPoliciesTile() {
  return createTile({
    id: 'policies-table',
    title: 'Governance Policies',
    icon: 'fa-solid fa-scroll',
    iconBg: 'rgba(59,130,246,0.1)',
    iconColor: '#3b82f6',
    filterable: true,
    filterPlaceholder: 'Filter policies\u2026',
    onFilter: (q) => _filterTable(q),
    actions: [
      {
        label: '+ New Policy',
        onclick: () => _openPolicyModal(),
      },
      {
        icon: 'fa-solid fa-rotate-right',
        onclick: () => _loadData(),
      },
      ...exportActions('policies-table'),
    ],
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;
  setTileLoading('policies-table', 'table');

  try {
    const resp = await rawFetch('/api/v1/policies').catch(() => null);
    if (_destroyed) return;
    if (resp && resp.ok) {
      const data = await resp.json().catch(() => []);
      _policiesCache = Array.isArray(data) ? data : (data.policies || []);
    } else {
      _policiesCache = [];
    }
    _renderTable(_policiesCache);
  } catch (err) {
    if (_destroyed) return;
    console.error('[policies] data fetch failed:', err);
    setTileError('policies-table', 'Failed to load policies', () => _loadData());
  }

  // Load enforcement decisions tile in parallel (GAP-3)
  _loadDecisions();
}

// ── Rendering ────────────────────────────────────────────────────────────────

function _renderTable(items) {
  const body = document.getElementById('policies-table-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('policies-table', {
      icon: 'fa-solid fa-scroll',
      title: 'No policies yet',
      description: 'Create your first governance policy to enforce budget caps, rate limits, and model restrictions.',
    });
    return;
  }

  const effectColor = (effect) => {
    if (effect === 'deny') return 'var(--danger)';
    if (effect === 'warn') return 'var(--warn)';
    return 'var(--accent2)';
  };

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Scope</th>
            <th>Effect</th>
            <th>Priority</th>
            <th>Status</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((p, idx) => {
            const typeBadge = (p.policy_type || '').replace(/_/g, ' ');
            const statusDot = p.is_active
              ? '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent)"></span> Active'
              : '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--muted)"></span> Inactive';
            return `<tr data-idx="${idx}" style="cursor:pointer" class="clickable">
              <td style="font-weight:600">${esc(p.name || 'Unnamed')}</td>
              <td><span style="font-size:10px;padding:2px 6px;border-radius:3px;background:rgba(255,255,255,0.04);color:var(--muted)">${esc(typeBadge)}</span></td>
              <td>${esc(p.scope || '\u2014')}</td>
              <td><span style="color:${effectColor(p.effect)};font-size:11px;font-weight:600;text-transform:uppercase">${esc(p.effect || '\u2014')}</span></td>
              <td style="font-family:var(--mono);font-size:11px">P${p.priority || 100}</td>
              <td style="font-size:11px">${statusDot}</td>
              <td style="font-size:11px;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.description || '')}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  body.querySelectorAll('tr.clickable').forEach(row => {
    row.addEventListener('click', () => {
      const p = items[parseInt(row.dataset.idx, 10)];
      if (p) _openPolicyModal(p);
    });
  });
}

// ── Filter ───────────────────────────────────────────────────────────────────

function _filterTable(query) {
  const body = document.getElementById('policies-table-body');
  if (!body) return;
  const q = (query || '').toLowerCase();
  const rows = body.querySelectorAll('tbody tr');
  rows.forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

// ── Policy Modal ─────────────────────────────────────────────────────────────
// The form mirrors the real API contract (POST/PUT /api/v1/policies); all
// validation and payload building lives in ../policy-form.js.

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

const _lbl = 'font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px';

function _opt(value, label, selected) {
  return `<option value="${esc(value)}" ${selected ? 'selected' : ''}>${esc(label)}</option>`;
}

function _openPolicyModal(existing) {
  const isEdit = !!existing;
  let _modalId = null;

  _modalId = openModal({
    title: isEdit ? 'Edit Policy' : 'New Policy',
    maxWidth: '620px',
    renderBody: (body) => {
      const type = existing?.policy_type || POLICY_TYPES[0].value;
      const scope = existing?.scope || 'team';
      const effect = existing?.effect || 'deny';
      const message = existing?.action?.message || '';

      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
          <div>
            <label style="${_lbl}">Policy Name</label>
            <input class="ds-input" id="pm-name" value="${esc(existing?.name || '')}" placeholder="Budget cap - production" style="width:100%">
          </div>
          <div>
            <label style="${_lbl}">Description</label>
            <input class="ds-input" id="pm-desc" value="${esc(existing?.description || '')}" placeholder="Optional description" style="width:100%">
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Policy Type</label>
              <select class="ds-select" id="pm-type" style="width:100%" ${isEdit ? 'disabled' : ''}>
                ${POLICY_TYPES.map(t => _opt(t.value, t.label, t.value === type)).join('')}
              </select>
            </div>
            <div>
              <label style="${_lbl}">Effect</label>
              <select class="ds-select" id="pm-effect" style="width:100%">
                ${EFFECTS.map(e => _opt(e.value, e.label, e.value === effect)).join('')}
              </select>
            </div>
          </div>
          <div id="pm-type-help" style="font-size:11px;color:var(--muted);margin-top:-6px"></div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Scope</label>
              <select class="ds-select" id="pm-scope" style="width:100%" ${isEdit ? 'disabled' : ''}>
                ${SCOPES.map(s => _opt(s.value, s.label, s.value === scope)).join('')}
              </select>
            </div>
            <div>
              <label style="${_lbl}">Priority (1-999)</label>
              <input class="ds-input" id="pm-priority" type="number" min="1" max="999" value="${esc(String(existing?.priority ?? 100))}" style="width:100%">
            </div>
            ${isEdit ? `
            <div>
              <label style="${_lbl}">Active</label>
              <label style="display:flex;align-items:center;gap:6px;padding-top:4px;cursor:pointer">
                <input type="checkbox" id="pm-active" ${existing.is_active !== false ? 'checked' : ''}>
                <span style="font-size:12px;color:var(--muted)">Enabled</span>
              </label>
            </div>` : '<div></div>'}
          </div>
          <div id="pm-anchors" style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div id="pm-team-wrap">
              <label style="${_lbl}">Team</label>
              <select class="ds-select" id="pm-team" style="width:100%" ${isEdit ? 'disabled' : ''}><option value="">Loading teams…</option></select>
            </div>
            <div id="pm-app-wrap">
              <label style="${_lbl}">App</label>
              <select class="ds-select" id="pm-app" style="width:100%" ${isEdit ? 'disabled' : ''}><option value="">Loading apps…</option></select>
            </div>
          </div>
          <div id="pm-config-fields" style="display:flex;flex-direction:column;gap:12px"></div>
          <div>
            <label style="${_lbl}">Message shown when the policy triggers</label>
            <input class="ds-input" id="pm-action-message" value="${esc(message)}" placeholder="Optional" style="width:100%">
          </div>
          <div id="pm-result" style="display:none"></div>
          <button class="ds-btn ds-btn-primary" id="pm-save-btn" style="margin-top:4px">${isEdit ? 'Save Changes' : 'Create Policy'}</button>
        </div>
      `;

      const typeSelect = body.querySelector('#pm-type');
      const scopeSelect = body.querySelector('#pm-scope');
      const teamSelect = body.querySelector('#pm-team');
      const appSelect = body.querySelector('#pm-app');

      // Config fields for the current type (values from the stored policy when editing).
      const renderType = (values) => {
        _renderConfigFields(typeSelect.value, values || {});
        const spec = typeSpec(typeSelect.value);
        body.querySelector('#pm-type-help').textContent = spec ? spec.help : '';
      };
      renderType(isEdit ? configToFormValues(type, existing.config) : null);
      typeSelect.addEventListener('change', () => renderType(null));

      // Team / app selectors, filled from GET /api/v1/teams and /api/v1/apps.
      let apps = [];
      const syncAnchors = () => {
        const s = scopeSelect.value;
        body.querySelector('#pm-team-wrap').style.display = s === 'platform' ? 'none' : '';
        body.querySelector('#pm-app-wrap').style.display = s === 'app' ? '' : 'none';
        const teamId = teamSelect.value;
        const choices = apps.filter(a => !teamId || a.team_id === teamId);
        appSelect.innerHTML = '<option value="">Select an app…</option>' +
          choices.map(a => _opt(a.id, `${a.app_name || a.app_id} (${a.app_id})`, existing?.app_id === a.id)).join('');
      };
      scopeSelect.addEventListener('change', syncAnchors);
      teamSelect.addEventListener('change', syncAnchors);

      (async () => {
        const [teams, appList] = await Promise.all([
          _fetchList('/api/v1/teams?limit=500'),
          _fetchList('/api/v1/apps?limit=500'),
        ]);
        if (_destroyed) return;
        apps = appList || [];
        if (teams === null) {
          teamSelect.innerHTML = '<option value="">Could not load teams</option>';
        } else {
          teamSelect.innerHTML = '<option value="">Select a team…</option>' +
            teams.map(t => _opt(t.id, `${t.name} (${t.slug})`, existing?.team_id === t.id)).join('');
        }
        syncAnchors();
      })();
      syncAnchors();

      body.querySelector('#pm-save-btn').addEventListener('click', () => _savePolicy(existing, _modalId));
    },
  });
}

// ── Config fields by type ────────────────────────────────────────────────────

function _renderConfigFields(type, values) {
  const container = document.getElementById('pm-config-fields');
  if (!container) return;
  const spec = typeSpec(type);
  if (!spec) { container.innerHTML = ''; return; }

  const fieldHtml = (f) => {
    const id = `pm-cfg-${f.key}`;
    const v = values[f.key] ?? f.default ?? '';
    let input;
    if (f.kind === 'select') {
      input = `<select class="ds-select" id="${id}" style="width:100%">${(f.options || []).map(o => _opt(o.value, o.label, o.value === v)).join('')}</select>`;
    } else if (f.kind === 'tiers') {
      input = `<textarea class="ds-input" id="${id}" rows="4" placeholder="${esc(f.placeholder || '')}" style="width:100%;font-family:var(--mono);font-size:12px">${esc(v)}</textarea>`;
    } else {
      const numeric = f.kind === 'int' || f.kind === 'number';
      input = `<input class="ds-input" id="${id}" ${numeric ? 'type="number" min="' + (f.min ?? 1) + '" step="' + (f.kind === 'int' ? '1' : 'any') + '"' : 'type="text"'} value="${esc(v)}" placeholder="${esc(f.placeholder || '')}" style="width:100%">`;
    }
    return `<div><label style="${_lbl}">${esc(f.label)}${f.required ? ' *' : ''}</label>${input}${f.help ? `<div style="font-size:11px;color:var(--muted);margin-top:3px">${esc(f.help)}</div>` : ''}</div>`;
  };

  container.innerHTML = `<div style="display:grid;grid-template-columns:${spec.fields.length > 1 && spec.fields.every(f => f.kind !== 'tiers' && f.kind !== 'list') ? '1fr 1fr' : '1fr'};gap:12px">${spec.fields.map(fieldHtml).join('')}</div>`;
}

/** Read the raw form state into the shape policy-form.js expects. */
function _readForm(existing) {
  const val = (id) => document.getElementById(id)?.value ?? '';
  const type = existing?.policy_type || val('pm-type');
  const spec = typeSpec(type);
  const config = {};
  (spec ? spec.fields : []).forEach(f => { config[f.key] = val(`pm-cfg-${f.key}`); });
  return {
    name: val('pm-name'),
    description: val('pm-desc'),
    policy_type: type,
    effect: val('pm-effect'),
    priority: val('pm-priority'),
    scope: val('pm-scope'),
    team_id: val('pm-team'),
    app_id: val('pm-app'),
    message: val('pm-action-message'),
    is_active: document.getElementById('pm-active')?.checked !== false,
    config,
  };
}

function _showResult(html) {
  const result = document.getElementById('pm-result');
  if (!result) return;
  result.style.display = 'block';
  result.innerHTML = html;
}

// ── Save Policy ──────────────────────────────────────────────────────────────

async function _savePolicy(existing, modalId) {
  const btn = document.getElementById('pm-save-btn');
  if (!btn) return;
  const editId = existing?.id;
  const idleLabel = editId ? 'Save Changes' : 'Create Policy';

  const { payload, errors } = buildPolicyPayload(_readForm(existing), {
    mode: editId ? 'edit' : 'create',
    existing,
  });
  if (!payload) {
    _showResult(`<ul style="margin:0;padding-left:18px;color:var(--danger);font-size:12px">${errors.map(e => `<li>${esc(e)}</li>`).join('')}</ul>`);
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Saving…';

  try {
    const resp = await rawFetch(editId ? `/api/v1/policies/${encodeURIComponent(editId)}` : '/api/v1/policies', {
      method: editId ? 'PUT' : 'POST',
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(formatApiError(err, resp.status));
    }
    closeModal(modalId);
    _loadData();
  } catch (e) {
    _showResult(`<div style="color:var(--danger);font-size:12px">${esc(e.message)}</div>`);
    btn.disabled = false;
    btn.textContent = idleLabel;
  }
}
