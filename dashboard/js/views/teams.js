/**
 * Modus Dashboard v2 — Teams View
 * Teams table with create, edit, delete (with app / child-team handling),
 * and a "show deleted teams" toggle with restore.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileError } from '../tile.js';
import { rawFetch, esc, headers } from '../api.js';
import { openModal, closeModal } from '../modal.js';
import { toast } from '../toast.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _teamsCache = [];
let _showDeleted = false;
let _filterText = '';

// ── Public API ───────────────────────────────────────────────────────────────

export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'teams');

  const tiles = [_createTeamsTile()];
  const layout = loadLayout('teams', LAYOUTS.teams);
  addTiles(_grid, tiles, layout);

  await _loadData();
}

export function destroy() {
  _destroyed = true;
  _teamsCache = [];
  _showDeleted = false;
  _filterText = '';
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

function _createTeamsTile() {
  return createTile({
    id: 'teams-table',
    title: 'Teams',
    icon: 'fa-solid fa-users',
    iconBg: 'rgba(0,229,160,0.1)',
    iconColor: 'var(--accent)',
    filterable: true,
    filterPlaceholder: 'Filter teams…',
    onFilter: (q) => _filterTable(q),
    actions: [
      {
        label: '+ New Team',
        onclick: () => _openCreateTeamModal(),
      },
    ],
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;
  setTileLoading('teams-table', 'table');

  try {
    const qs = _showDeleted ? '?limit=500&include_deleted=true' : '?limit=500';
    const resp = await rawFetch('/api/v1/teams' + qs).catch(() => null);
    if (_destroyed) return;
    if (resp && resp.ok) {
      _teamsCache = await resp.json().catch(() => []);
    } else {
      _teamsCache = [];
    }
    _renderTable(_teamsCache);
    if (_filterText) _filterTable(_filterText);
  } catch (err) {
    if (_destroyed) return;
    console.error('[teams] data fetch failed:', err);
    setTileError('teams-table', 'Failed to load teams', () => _loadData());
  }
}

// ── Shared styles / helpers ──────────────────────────────────────────────────

const _lbl = 'font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px';
const _hint = 'font-size:10px;color:var(--muted);margin-top:4px';
const _err = 'color:var(--danger);font-size:12px';

function _fmtMoney(v) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  return Number.isFinite(n) ? '$' + n.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—';
}

/** Decimal for an input box: no exponent, no trailing zeros. */
function _moneyInput(v) {
  if (v === null || v === undefined || v === '') return '';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return n.toFixed(8).replace(/\.?0+$/, '');
}

async function _errorMessage(resp, fallback) {
  const err = await resp.json().catch(() => ({}));
  const d = err && err.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map((e) => {
      const field = Array.isArray(e.loc) ? e.loc.filter((x) => x !== 'body').join('.') : '';
      return field ? `${field}: ${e.msg}` : e.msg;
    }).join('; ');
  }
  return `${fallback} (HTTP ${resp.status})`;
}

async function _getDetail(id) {
  const resp = await rawFetch(`/api/v1/teams/${encodeURIComponent(id)}`);
  if (!resp.ok) throw new Error(await _errorMessage(resp, 'Could not load team'));
  return resp.json();
}

/** ids of `rootId` and every team below it, from the cached list. */
function _subtreeIds(rootId) {
  const out = new Set([rootId]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const t of _teamsCache) {
      if (t.parent_id && out.has(t.parent_id) && !out.has(t.id)) { out.add(t.id); grew = true; }
    }
  }
  return out;
}

function _teamOptions(excludeIds, selected, blankLabel) {
  const opts = _teamsCache
    .filter((t) => !t.deleted_at && !excludeIds.has(t.id))
    .map((t) => `<option value="${esc(t.id)}" ${t.id === selected ? 'selected' : ''}>${esc(t.name)} (${esc(t.slug)})</option>`);
  return (blankLabel !== null ? `<option value="">${esc(blankLabel)}</option>` : '') + opts.join('');
}

function _showInline(id, msg) {
  const el = document.getElementById(id);
  if (!el) return;
  el.style.display = 'block';
  el.innerHTML = `<div style="${_err}">${esc(msg)}</div>`;
}

// ── Rendering ────────────────────────────────────────────────────────────────

function _renderTable(items) {
  const body = document.getElementById('teams-table-body');
  if (!body) return;

  const toggle = `
    <label style="display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);margin-bottom:8px;cursor:pointer">
      <input type="checkbox" id="teams-show-deleted" ${_showDeleted ? 'checked' : ''}> Show deleted teams
    </label>`;

  if (!items || !Array.isArray(items) || items.length === 0) {
    body.innerHTML = `${toggle}
      <div style="padding:24px;text-align:center;color:var(--muted);font-size:13px">
        ${_showDeleted ? 'No teams.' : 'No teams yet. Create your first team to organize apps and users.'}
      </div>`;
    _bindToggle(body);
    return;
  }

  const byId = new Map(items.map((t) => [t.id, t]));
  body.innerHTML = `
    ${toggle}
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Team Name</th>
            <th>Slug</th>
            <th>Parent</th>
            <th>Department</th>
            <th>Apps</th>
            <th>Monthly Budget</th>
            <th>Created</th>
            <th style="text-align:right">Actions</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((t) => {
            const created = t.created_at ? new Date(t.created_at).toLocaleDateString() : '—';
            const deleted = !!t.deleted_at;
            const parent = t.parent_id ? byId.get(t.parent_id) : null;
            const badge = deleted
              ? ' <span style="font-size:10px;font-weight:600;color:var(--danger);border:1px solid var(--danger);border-radius:4px;padding:0 4px">DELETED</span>'
              : '';
            const actions = deleted
              ? `<button class="ds-btn ds-btn-sm ds-btn-ghost" data-act="restore" data-id="${esc(t.id)}">Restore</button>`
              : `<button class="ds-btn ds-btn-sm ds-btn-ghost" data-act="edit" data-id="${esc(t.id)}">Edit</button>
                 <button class="ds-btn ds-btn-sm ds-btn-ghost" data-act="delete" data-id="${esc(t.id)}" style="color:var(--danger)">Delete</button>`;
            return `<tr data-id="${esc(t.id)}" style="${deleted ? 'opacity:0.65' : ''}">
              <td style="font-weight:600">${esc(t.name || '')}${badge}</td>
              <td style="font-family:var(--mono);font-size:11px">${esc(t.slug || '')}</td>
              <td style="font-size:11px;color:var(--muted)">${esc(parent ? parent.name : (t.parent_id ? '(unlisted)' : '—'))}</td>
              <td style="font-size:11px;color:var(--muted)">${esc(t.department || '—')}</td>
              <td style="font-size:11px">${esc(String(t.active_app_count ?? 0))}</td>
              <td style="font-size:11px">${_fmtMoney(t.budget_monthly_usd)}</td>
              <td style="font-size:11px;color:var(--muted)">${created}</td>
              <td style="text-align:right;white-space:nowrap">${actions}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
  _bindToggle(body);
  body.querySelectorAll('button[data-act]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-id');
      const act = btn.getAttribute('data-act');
      if (act === 'edit') _openEditTeamModal(id);
      else if (act === 'delete') _openDeleteTeamModal(id);
      else if (act === 'restore') _restoreTeam(id);
    });
  });
}

function _bindToggle(body) {
  const cb = body.querySelector('#teams-show-deleted');
  if (cb) cb.addEventListener('change', () => { _showDeleted = cb.checked; _loadData(); });
}

// ── Filter ───────────────────────────────────────────────────────────────────

function _filterTable(query) {
  _filterText = query || '';
  const body = document.getElementById('teams-table-body');
  if (!body) return;
  const q = _filterText.toLowerCase();
  const rows = body.querySelectorAll('tbody tr');
  rows.forEach(row => {
    row.style.display = row.textContent.toLowerCase().includes(q) ? '' : 'none';
  });
}

// ── Create Team Modal ────────────────────────────────────────────────────────

function _openCreateTeamModal() {
  let _modalId = null;
  _modalId = openModal({
    title: 'New Team',
    maxWidth: '440px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
          <div>
            <label style="${_lbl}">Team Name</label>
            <input class="ds-input" id="ct-name" placeholder="Engineering" style="width:100%">
          </div>
          <div>
            <label style="${_lbl}">Slug</label>
            <input class="ds-input" id="ct-slug" placeholder="engineering" style="width:100%">
            <div style="${_hint}">Lowercase, no spaces. Used as an identifier in API calls.</div>
          </div>
          <div id="ct-result" style="display:none"></div>
          <button class="ds-btn ds-btn-primary" id="ct-save-btn" style="margin-top:4px">Create Team</button>
        </div>
      `;

      // Auto-generate slug from name
      const nameInput = body.querySelector('#ct-name');
      const slugInput = body.querySelector('#ct-slug');
      nameInput.addEventListener('input', () => {
        slugInput.value = nameInput.value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      });

      body.querySelector('#ct-save-btn').addEventListener('click', () => _createTeam(_modalId));
    },
  });
}

async function _createTeam(modalId) {
  const btn = document.getElementById('ct-save-btn');
  if (!btn) return;

  const name = (document.getElementById('ct-name')?.value || '').trim();
  const slug = (document.getElementById('ct-slug')?.value || '').trim();

  if (!name || !slug) {
    return _showInline('ct-result', 'Team name and slug are required.');
  }

  btn.disabled = true;
  btn.textContent = 'Creating…';

  try {
    const resp = await rawFetch('/api/v1/teams', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ name, slug }),
    });
    if (!resp.ok) throw new Error(await _errorMessage(resp, 'Failed to create team'));
    closeModal(modalId);
    _loadData();
  } catch (e) {
    _showInline('ct-result', e.message);
    btn.disabled = false;
    btn.textContent = 'Create Team';
  }
}

// ── Edit Team Modal ──────────────────────────────────────────────────────────

async function _openEditTeamModal(teamId) {
  let team;
  try {
    team = await _getDetail(teamId);
  } catch (e) {
    toast(e.message, 'error');
    _loadData();
    return;
  }
  const exclude = _subtreeIds(team.id);
  const costCenters = await rawFetch('/api/v1/finance/cost-centers')
    .then((r) => (r.ok ? r.json() : []))
    .catch(() => []);
  const ccList = Array.isArray(costCenters) ? costCenters : [];
  const durations = [['', '(default: daily)'], ['daily', 'Daily'], ['monthly', 'Monthly'], ['30d', 'Rolling 30 days'], ['1h', 'Hourly']];
  const orig = {
    max_budget_usd: _moneyInput(team.max_budget_usd),
    budget_monthly_usd: _moneyInput(team.budget_monthly_usd),
    budget_quarterly_usd: _moneyInput(team.budget_quarterly_usd),
  };

  let modalId = null;
  modalId = openModal({
    title: `Edit Team — ${esc(team.name)}`,
    maxWidth: '560px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Team Name</label>
              <input class="ds-input" id="et-name" value="${esc(team.name)}" maxlength="256" style="width:100%">
            </div>
            <div>
              <label style="${_lbl}">Slug</label>
              <input class="ds-input" id="et-slug" value="${esc(team.slug)}" maxlength="64" style="width:100%">
              <div style="${_hint}">Lowercase letters, digits, - and _</div>
            </div>
          </div>
          <div>
            <label style="${_lbl}">Description</label>
            <input class="ds-input" id="et-desc" value="${esc(team.description || '')}" style="width:100%">
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Department</label>
              <input class="ds-input" id="et-dept" value="${esc(team.department || '')}" maxlength="256" style="width:100%">
            </div>
            <div>
              <label style="${_lbl}">Parent Team</label>
              <select class="ds-select" id="et-parent" style="width:100%">${_teamOptions(exclude, team.parent_id, '(none — top level)')}</select>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Budget (USD)</label>
              <input class="ds-input" id="et-max" value="${esc(orig.max_budget_usd)}" inputmode="decimal" placeholder="unlimited" style="width:100%">
            </div>
            <div>
              <label style="${_lbl}">Monthly (USD)</label>
              <input class="ds-input" id="et-monthly" value="${esc(orig.budget_monthly_usd)}" inputmode="decimal" placeholder="none" style="width:100%">
            </div>
            <div>
              <label style="${_lbl}">Quarterly (USD)</label>
              <input class="ds-input" id="et-quarterly" value="${esc(orig.budget_quarterly_usd)}" inputmode="decimal" placeholder="none" style="width:100%">
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
            <div>
              <label style="${_lbl}">Budget Period</label>
              <select class="ds-select" id="et-duration" style="width:100%">
                ${durations.map(([v, l]) => `<option value="${v}" ${(team.budget_duration || '') === v ? 'selected' : ''}>${esc(l)}</option>`).join('')}
              </select>
            </div>
            <div>
              <label style="${_lbl}">Cost Center</label>
              <select class="ds-select" id="et-cc" style="width:100%">
                <option value="">(none)</option>
                ${ccList.map((c) => `<option value="${esc(c.id)}" ${c.id === team.cost_center_id ? 'selected' : ''}>${esc(c.code)} — ${esc(c.name)}</option>`).join('')}
              </select>
            </div>
          </div>
          <div id="et-result" style="display:none"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="ds-btn ds-btn-ghost" id="et-cancel-btn">Cancel</button>
            <button class="ds-btn ds-btn-primary" id="et-save-btn">Save Changes</button>
          </div>
        </div>
      `;
      body.querySelector('#et-cancel-btn').addEventListener('click', () => closeModal(modalId));
      body.querySelector('#et-save-btn').addEventListener('click', () => _saveTeam(modalId, team, orig));
    },
  });
}

async function _saveTeam(modalId, team, orig) {
  const val = (id) => (document.getElementById(id)?.value ?? '').trim();
  const name = val('et-name');
  const slug = val('et-slug');
  if (!name || !slug) return _showInline('et-result', 'Team name and slug are required.');
  if (!/^[a-z0-9][a-z0-9\-_]*$/.test(slug)) {
    return _showInline('et-result', 'Slug must start with a lowercase letter or digit and contain only a-z, 0-9, - and _.');
  }

  // Send only what changed. Money goes back as the exact text typed (a decimal
  // string), never a JS number, and only when the user edited the box.
  const patch = {};
  const setIf = (key, now, before) => { if (now !== before) patch[key] = now; };
  setIf('name', name, team.name);
  setIf('slug', slug, team.slug);
  setIf('description', val('et-desc') || null, team.description || null);
  setIf('department', val('et-dept') || null, team.department || null);
  setIf('parent_id', val('et-parent') || null, team.parent_id || null);
  setIf('budget_duration', val('et-duration') || null, team.budget_duration || null);
  setIf('cost_center_id', val('et-cc') || null, team.cost_center_id || null);
  const moneyBoxes = [['max_budget_usd', 'et-max'], ['budget_monthly_usd', 'et-monthly'], ['budget_quarterly_usd', 'et-quarterly']];
  for (const [key, id] of moneyBoxes) {
    const raw = val(id);
    if (raw !== orig[key]) {
      if (raw !== '' && !/^\d+(\.\d{1,8})?$/.test(raw)) {
        return _showInline('et-result', `${key.replace(/_/g, ' ')}: enter a non-negative amount with up to 8 decimals.`);
      }
      patch[key] = raw === '' ? null : raw;
    }
  }

  if (Object.keys(patch).length === 0) {
    closeModal(modalId);
    return;
  }

  const btn = document.getElementById('et-save-btn');
  btn.disabled = true;
  btn.textContent = 'Saving…';
  try {
    const resp = await rawFetch(`/api/v1/teams/${encodeURIComponent(team.id)}`, {
      method: 'PATCH',
      headers: headers(),
      body: JSON.stringify(patch),
    });
    if (!resp.ok) throw new Error(await _errorMessage(resp, 'Failed to save team'));
    closeModal(modalId);
    toast('Team updated.', 'success');
    _loadData();
  } catch (e) {
    _showInline('et-result', e.message);
    btn.disabled = false;
    btn.textContent = 'Save Changes';
  }
}

// ── Delete Team Modal ────────────────────────────────────────────────────────

async function _openDeleteTeamModal(teamId) {
  let team;
  try {
    team = await _getDetail(teamId);
  } catch (e) {
    toast(e.message, 'error');
    _loadData();
    return;
  }
  const apps = team.counts.active_apps;
  const kids = team.child_teams || [];
  const exclude = _subtreeIds(team.id);

  let modalId = null;
  modalId = openModal({
    title: `Delete Team — ${esc(team.name)}`,
    maxWidth: '520px',
    renderBody: (body) => {
      const appBlock = apps > 0 ? `
        <div style="border:1px solid var(--border);border-radius:6px;padding:10px 12px">
          <div style="font-size:12px;font-weight:600;margin-bottom:6px">${apps} active app${apps === 1 ? '' : 's'} in this team</div>
          <label style="display:flex;gap:6px;align-items:flex-start;font-size:12px;margin-bottom:6px">
            <input type="radio" name="dt-apps" value="reassign" checked>
            <span>Move the apps (and this team's policies and thresholds) to another team</span>
          </label>
          <select class="ds-select" id="dt-reassign" style="width:100%;margin:0 0 8px 20px;max-width:calc(100% - 20px)">${_teamOptions(exclude, '', null)}</select>
          <label style="display:flex;gap:6px;align-items:flex-start;font-size:12px">
            <input type="radio" name="dt-apps" value="cascade">
            <span>Deactivate the apps too. Their API keys stop working immediately.</span>
          </label>
        </div>` : '';
      const kidBlock = kids.length > 0 ? `
        <div style="border:1px solid var(--border);border-radius:6px;padding:10px 12px">
          <div style="font-size:12px;font-weight:600;margin-bottom:6px">${kids.length} child team${kids.length === 1 ? '' : 's'}: ${esc(kids.map((k) => k.name).join(', '))}</div>
          <label style="${_lbl};font-weight:400">Move them under</label>
          <select class="ds-select" id="dt-children" style="width:100%">${_teamOptions(exclude, team.parent_id || '', null)}</select>
        </div>` : '';
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:12px;padding:4px 0">
          <div style="font-size:12px;color:var(--muted)">
            The team is soft-deleted: usage and cost history is kept, its registration
            token is revoked, and it can be restored from <em>Show deleted teams</em>.
          </div>
          ${appBlock}${kidBlock}
          <div id="dt-result" style="display:none"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="ds-btn ds-btn-ghost" id="dt-cancel-btn">Cancel</button>
            <button class="ds-btn ds-btn-danger" id="dt-confirm-btn">Delete Team</button>
          </div>
        </div>
      `;
      body.querySelector('#dt-cancel-btn').addEventListener('click', () => closeModal(modalId));
      body.querySelector('#dt-confirm-btn').addEventListener('click', () => _deleteTeam(modalId, team, apps, kids.length));
    },
  });
}

async function _deleteTeam(modalId, team, appCount, kidCount) {
  const params = new URLSearchParams();
  if (appCount > 0) {
    const mode = document.querySelector('input[name="dt-apps"]:checked')?.value;
    if (mode === 'cascade') {
      params.set('cascade', 'true');
    } else {
      const target = document.getElementById('dt-reassign')?.value;
      if (!target) return _showInline('dt-result', 'Choose a team to move the apps to, or deactivate them.');
      params.set('reassign_to', target);
    }
  }
  if (kidCount > 0) {
    const target = document.getElementById('dt-children')?.value;
    if (!target) return _showInline('dt-result', 'Choose a team to move the child teams under.');
    params.set('reassign_children_to', target);
  }

  const btn = document.getElementById('dt-confirm-btn');
  btn.disabled = true;
  btn.textContent = 'Deleting…';
  try {
    const qs = params.toString();
    const resp = await rawFetch(`/api/v1/teams/${encodeURIComponent(team.id)}${qs ? '?' + qs : ''}`, {
      method: 'DELETE',
      headers: headers(),
    });
    if (!resp.ok) throw new Error(await _errorMessage(resp, 'Failed to delete team'));
    const r = await resp.json().catch(() => ({}));
    closeModal(modalId);
    const bits = [];
    if (r.apps_reassigned) bits.push(`${r.apps_reassigned} app(s) moved`);
    if (r.apps_deactivated) bits.push(`${r.apps_deactivated} app(s) deactivated`);
    if (r.child_teams_reassigned) bits.push(`${r.child_teams_reassigned} child team(s) re-parented`);
    toast(`Team "${team.name}" deleted${bits.length ? ' — ' + bits.join(', ') : ''}.`, 'success');
    _loadData();
  } catch (e) {
    _showInline('dt-result', e.message);
    btn.disabled = false;
    btn.textContent = 'Delete Team';
  }
}

// ── Restore ──────────────────────────────────────────────────────────────────

async function _restoreTeam(teamId) {
  try {
    const resp = await rawFetch(`/api/v1/teams/${encodeURIComponent(teamId)}/restore`, {
      method: 'POST',
      headers: headers(),
    });
    if (!resp.ok) throw new Error(await _errorMessage(resp, 'Failed to restore team'));
    const r = await resp.json().catch(() => ({}));
    toast(
      r.parent_cleared
        ? 'Team restored. Its former parent is deleted, so it is now top level.'
        : 'Team restored. Apps that were moved or deactivated are not brought back.',
      'success',
    );
    _loadData();
  } catch (e) {
    toast(e.message, 'error');
  }
}
