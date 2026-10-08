/**
 * Modus Dashboard v2 — Apps View
 * Renders the Applications management page with a full-width table,
 * team/env/status filtering, and a Register App modal.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError } from '../tile.js';
import { apiFetch, rawFetch, esc, headers } from '../api.js';
import { fmtDate, timeSince } from '../format.js';
import { get } from '../state.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';
import { toast, confirm as uiConfirm, reveal, promptSecret } from '../toast.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _appsCache = [];

// ── Public API ───────────────────────────────────────────────────────────────

export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'apps');

  const tiles = [_createAppsTile()];
  const layout = loadLayout('apps', LAYOUTS.apps);
  addTiles(_grid, tiles, layout);

  await _loadData();
}

export function destroy() {
  _destroyed = true;
  _appsCache = [];
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

function _createAppsTile() {
  return createTile({
    id: 'apps-table',
    title: 'Applications',
    icon: 'fa-solid fa-cubes',
    iconBg: 'rgba(168,85,247,0.1)',
    iconColor: '#a855f7',
    filterable: true,
    filterPlaceholder: 'Filter apps\u2026',
    onFilter: (q) => _filterTable(q),
    actions: [
      {
        label: '+ Register App',
        onclick: () => _openRegisterModal(),
      },
    ],
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;
  setTileLoading('apps-table', 'table');

  try {
    const resp = await rawFetch('/api/v1/apps?active_only=false').catch(() => null);
    if (_destroyed) return;
    if (resp && resp.ok) {
      _appsCache = await resp.json().catch(() => []);
    } else {
      _appsCache = [];
    }
    _renderTable(_appsCache);
  } catch (err) {
    if (_destroyed) return;
    console.error('[apps] data fetch failed:', err);
    setTileError('apps-table', 'Failed to load apps', () => _loadData());
  }
}

// ── Rendering ────────────────────────────────────────────────────────────────

function _renderTable(apps) {
  const body = document.getElementById('apps-table-body');
  if (!body) return;

  if (!apps || !Array.isArray(apps) || apps.length === 0) {
    setTileEmpty('apps-table', {
      icon: 'fa-solid fa-cubes',
      title: 'No applications',
      description: 'Register your first app to get started.',
    });
    return;
  }

  const envBadge = (env) => {
    const e = (env || '').toLowerCase();
    let bg, color;
    if (e === 'production') { bg = 'rgba(0,229,160,0.08)'; color = 'var(--accent)'; }
    else if (e === 'staging') { bg = 'rgba(245,158,11,0.08)'; color = '#f59e0b'; }
    else { bg = 'rgba(0,153,255,0.08)'; color = 'var(--accent2)'; }
    return `<span style="font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:3px;background:${bg};color:${color}">${esc(env || 'dev')}</span>`;
  };

  // Runtime enforcement state from GET /apps: active | budget_suspended |
  // rate_limited | admin_suspended, with the reason and time it was set.
  const ENFORCEMENT = {
    active:           { label: 'Active',           color: 'var(--accent)' },
    budget_suspended: { label: 'Budget suspended', color: 'var(--danger)' },
    rate_limited:     { label: 'Rate limited',     color: '#f59e0b' },
    admin_suspended:  { label: 'Admin suspended',  color: 'var(--danger)' },
  };
  const enforcementBadge = (a) => {
    const state = a.enforcement_state || 'active';
    const meta = ENFORCEMENT[state] || { label: state.replace(/_/g, ' '), color: 'var(--muted)' };
    const since = a.enforcement_suspended_at ? `Since ${new Date(a.enforcement_suspended_at).toLocaleString()}` : '';
    const reason = a.enforcement_suspended_reason || '';
    const title = [reason, since].filter(Boolean).join(' \u2014 ');
    const detail = state !== 'active' && reason
      ? `<div style="font-size:10px;color:var(--muted);max-width:220px;white-space:normal;margin-top:2px">${esc(reason)}</div>`
      : '';
    return `<span title="${esc(title)}" style="color:${meta.color};font-weight:500">${esc(meta.label)}</span>${detail}`;
  };

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>App Name</th>
            <th>App ID</th>
            <th>Team</th>
            <th>Environment</th>
            <th>Status</th>
            <th>Enforcement</th>
            <th>Agent</th>
            <th>Last Seen</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${apps.map((a, idx) => {
            const status = a.is_active
              ? '<span style="color:var(--accent);font-weight:500">Active</span>'
              : '<span style="color:var(--muted)">Inactive</span>';
            const lastSeen = a.last_seen_at ? timeSince(a.last_seen_at) : '\u2014';
            return `<tr data-idx="${idx}">
              <td style="font-weight:600">${esc(a.app_name || '')}</td>
              <td style="font-family:var(--mono);font-size:11px">${esc(a.app_id || '')}</td>
              <td><span style="color:var(--muted)">${esc(a.team_slug || '')}</span></td>
              <td>${envBadge(a.environment)}</td>
              <td>${status}</td>
              <td>${enforcementBadge(a)}</td>
              <td style="font-family:var(--mono);font-size:11px">${esc(a.agent_version || '\u2014')}</td>
              <td style="font-size:11px;color:var(--muted)">${lastSeen}</td>
              <td>
                <div style="display:flex;gap:6px">
                  <button class="ds-btn ds-btn-ghost ds-btn-sm apps-rotate-btn" data-idx="${idx}" style="font-size:11px">Rotate Key</button>
                  <button class="ds-btn ds-btn-ghost ds-btn-sm apps-deactivate-btn" data-idx="${idx}" style="font-size:11px;color:var(--danger)">Deactivate</button>
                </div>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  // Attach action button handlers
  body.querySelectorAll('.apps-rotate-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const app = apps[parseInt(btn.dataset.idx, 10)];
      if (app) _rotateKey(app);
    });
  });
  body.querySelectorAll('.apps-deactivate-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const app = apps[parseInt(btn.dataset.idx, 10)];
      if (app) _deactivateApp(app);
    });
  });
}

// ── Filter ───────────────────────────────────────────────────────────────────

function _filterTable(query) {
  const body = document.getElementById('apps-table-body');
  if (!body) return;
  const q = (query || '').toLowerCase();
  const rows = body.querySelectorAll('tbody tr');
  rows.forEach(row => {
    const text = row.textContent.toLowerCase();
    row.style.display = text.includes(q) ? '' : 'none';
  });
}

// ── Register App Modal ───────────────────────────────────────────────────────

function _openRegisterModal() {
  let _modalId = null;
  _modalId = openModal({
    title: 'Register Application',
    maxWidth: '520px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">App ID</label>
            <input class="ds-input" id="reg-app-id" placeholder="my-app" style="width:100%">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">App Name</label>
            <input class="ds-input" id="reg-app-name" placeholder="My Application" style="width:100%">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Team Slug</label>
            <input class="ds-input" id="reg-team-slug" placeholder="engineering" style="width:100%">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Environment</label>
            <select class="ds-select" id="reg-environment" style="width:100%">
              <option value="production">Production</option>
              <option value="staging">Staging</option>
              <option value="development">Development</option>
            </select>
          </div>
          <div id="reg-result" style="display:none"></div>
          <button class="ds-btn ds-btn-primary" id="reg-submit-btn" style="margin-top:4px">Register</button>
        </div>
      `;
      body.querySelector('#reg-submit-btn').addEventListener('click', () => _registerApp(_modalId));
    },
  });
}

async function _registerApp(modalId) {
  const btn = document.getElementById('reg-submit-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Registering\u2026';

  const payload = {
    app_id: (document.getElementById('reg-app-id')?.value || '').trim(),
    app_name: (document.getElementById('reg-app-name')?.value || '').trim(),
    team_slug: (document.getElementById('reg-team-slug')?.value || '').trim(),
    environment: document.getElementById('reg-environment')?.value || 'production',
  };

  try {
    const resp = await rawFetch('/api/v1/apps/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail?.message || data.detail || resp.statusText);

    const result = document.getElementById('reg-result');
    if (result) {
      result.style.display = 'block';
      result.innerHTML = `
        <div style="background:rgba(0,229,160,0.08);border:1px solid rgba(0,229,160,0.2);border-radius:8px;padding:14px">
          <div style="color:var(--accent);font-weight:600;margin-bottom:8px">Registration Successful</div>
          <div style="font-size:11px;color:var(--muted);margin-bottom:6px">Save this API key now -- it will not be shown again:</div>
          <div style="display:flex;align-items:center;gap:8px">
            <code style="flex:1;background:var(--bg);padding:8px;border-radius:4px;font-family:var(--mono);font-size:12px;word-break:break-all;color:var(--text)">${esc(data.api_key)}</code>
            <button class="ds-btn ds-btn-ghost ds-btn-sm" id="reg-copy-btn">Copy</button>
          </div>
        </div>`;
      const copyBtn = document.getElementById('reg-copy-btn');
      if (copyBtn) {
        copyBtn.addEventListener('click', () => {
          navigator.clipboard.writeText(data.api_key);
          copyBtn.textContent = 'Copied!';
        });
      }
    }
    btn.textContent = 'Done';
    _loadData();
  } catch (e) {
    const result = document.getElementById('reg-result');
    if (result) {
      result.style.display = 'block';
      result.innerHTML = `<div style="color:var(--danger);margin-top:8px;font-size:12px">${esc(e.message)}</div>`;
    }
    btn.disabled = false;
    btn.textContent = 'Register';
  }
}

// ── Rotate Key ───────────────────────────────────────────────────────────────

async function _rotateKey(app) {
  const ok = await uiConfirm(
    `Rotate API key for "${app.app_id}"? The old key will be immediately invalidated.`,
    { title: 'Rotate API Key', confirmLabel: 'Rotate', confirmStyle: 'danger' }
  );
  if (!ok) return;

  // Rotation is authorised by the master key (X-Modus-APIKey), not by a user
  // session. If we signed in with it, reuse it; otherwise (stub mode, or a JWT
  // session) ask for it. It is used for this one request and never stored.
  let masterKey = get('apiKey');
  if (!masterKey) {
    masterKey = await promptSecret(
      'Rotating an application key requires the master key (MODUS_MASTER_API_KEY). It is sent with this request only and is not stored.',
      { title: 'Master key required', confirmLabel: 'Rotate', placeholder: 'mds_master_...' }
    );
    if (!masterKey) return;
  }

  try {
    const resp = await rawFetch(`/api/v1/apps/${app.id}/rotate-key`, {
      method: 'POST',
      headers: { ...headers(), 'X-Modus-APIKey': masterKey },
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || resp.statusText);
    await reveal(
      `New API key for ${app.app_id}`,
      data.api_key,
      'Save this key now — it will not be shown again. The old key has been invalidated.'
    );
    _loadData();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}

// ── Deactivate App ───────────────────────────────────────────────────────────

async function _deactivateApp(app) {
  const ok = await uiConfirm(
    `Deactivate "${app.app_id}"? This will soft-delete the app.`,
    { title: 'Deactivate App', confirmLabel: 'Deactivate', confirmStyle: 'danger' }
  );
  if (!ok) return;
  try {
    const resp = await rawFetch(`/api/v1/apps/${app.id}`, { method: 'DELETE' });
    if (!resp.ok && resp.status !== 204) throw new Error(resp.statusText);
    toast(`App "${app.app_id}" deactivated`, 'success');
    _loadData();
  } catch (e) {
    toast('Error: ' + e.message, 'error');
  }
}
