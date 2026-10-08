/**
 * Modus Dashboard v2 — Admin View
 * Renders the admin panel as a single full-width Gridstack tile
 * with internal tabs: Users, Teams, Roles & Permissions, Audit Log.
 *
 * Backed by the real endpoints:
 *   GET    /api/v1/users                        -> UserResponse[]
 *   PATCH  /api/v1/users/{id}                   {display_name, is_active}
 *   DELETE /api/v1/users/{id}
 *   POST   /api/v1/users/invite                 {email, display_name, role_id, team_id, channel}
 *   GET    /api/v1/users/invitations            -> InvitationResponse[]
 *   POST   /api/v1/users/invitations/{id}/revoke
 *   GET    /api/v1/users/invite/channels        -> {configured, all}
 *   GET    /api/v1/teams                        -> TeamResponse[]
 *   GET    /api/v1/roles                        -> RoleResponse[]
 *   GET    /api/v1/audit-log?limit=N            -> AuditEntry[]
 * A section that fails to load shows its error; nothing is faked.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading } from '../tile.js';
import { rawFetch, esc } from '../api.js';
import { openModal, closeModal } from '../modal.js';
import { toast } from '../toast.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _data = _emptyData();
let _currentTab = 'users';

const AVATAR_COLORS = ['#005C53', '#042940', '#9FC131', '#3b82f6', '#a855f7', '#ef4444', '#f59e0b', '#ec4899'];

function _emptyData() {
  return {
    users: [], invitations: [], teams: [], roles: [], audit: [],
    channels: { configured: [], all: ['email', 'slack', 'teams'] },
    errors: {},   // section -> message for sections that failed to load
  };
}

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Admin view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;
  _currentTab = 'users';

  _grid = initGrid(container, 'admin');

  const tiles = [_createAdminTile()];
  const layout = loadLayout('admin', LAYOUTS.admin);
  addTiles(_grid, tiles, layout);

  await loadData();
}

/**
 * Tear down the Admin view.
 */
export function destroy() {
  _destroyed = true;
  _data = _emptyData();
  _grid = null;
  _container = null;
}

// ── Tile factory ─────────────────────────────────────────────────────────────

function _createAdminTile() {
  const tile = createTile({
    id: 'admin-panel',
    title: 'Administration',
    icon: 'fa-solid fa-gear',
    iconBg: 'rgba(159,193,49,0.1)',
    iconColor: 'var(--accent)',
    actions: [{
      label: 'Invite User',
      icon: 'fa-solid fa-user-plus',
      onclick: () => _showInviteModal(),
    }],
  });
  return tile;
}

// ── Data fetching ────────────────────────────────────────────────────────────

/** GET a JSON array/object; never throws. Resolves {ok, data, error}. */
async function _getJson(path) {
  try {
    const resp = await rawFetch(path);
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      return { ok: false, data: null, error: body.detail || `HTTP ${resp.status}` };
    }
    return { ok: true, data: await resp.json(), error: null };
  } catch (err) {
    return { ok: false, data: null, error: err.message };
  }
}

async function loadData() {
  if (_destroyed) return;

  setTileLoading('admin-panel', 'table');

  const [users, invitations, teams, roles, audit, channels] = await Promise.all([
    _getJson('/api/v1/users?limit=500'),
    _getJson('/api/v1/users/invitations?status=pending'),
    _getJson('/api/v1/teams?limit=500'),
    _getJson('/api/v1/roles'),
    _getJson('/api/v1/audit-log?limit=100'),
    _getJson('/api/v1/users/invite/channels'),
  ]);

  if (_destroyed) return;

  const next = _emptyData();
  const take = (key, res, fallback) => {
    if (res.ok && Array.isArray(res.data)) next[key] = res.data;
    else if (res.ok) next[key] = fallback;
    else next.errors[key] = res.error;
  };
  take('users', users, []);
  take('invitations', invitations, []);
  take('teams', teams, []);
  take('roles', roles, []);
  take('audit', audit, []);
  if (channels.ok && channels.data && Array.isArray(channels.data.all)) next.channels = channels.data;
  _data = next;

  _renderPanel();
}

// ── Panel renderer ───────────────────────────────────────────────────────────

function _errorBox(what, msg) {
  return `<div style="padding:24px 16px;color:var(--danger);font-size:12px">
    <i class="fa-solid fa-circle-exclamation" style="margin-right:6px"></i>
    Could not load ${esc(what)}: ${esc(msg)}
  </div>`;
}

function _renderPanel() {
  const body = document.getElementById('admin-panel-body');
  if (!body) return;

  const { users, invitations, roles, errors } = _data;
  const activeCount = users.filter(u => u.is_active).length;

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;height:100%">
      <!-- Stats row -->
      <div style="display:flex;gap:12px;padding:12px 16px;border-bottom:1px solid var(--border);flex-wrap:wrap">
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Total Users</div>
          <div class="kpi-value">${errors.users ? '—' : users.length}</div>
        </div>
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Active</div>
          <div class="kpi-value" style="color:var(--accent)">${errors.users ? '—' : activeCount}</div>
        </div>
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Pending Invites</div>
          <div class="kpi-value" style="color:${invitations.length > 0 ? 'var(--warn)' : 'var(--muted)'}">${errors.invitations ? '—' : invitations.length}</div>
        </div>
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Roles</div>
          <div class="kpi-value">${errors.roles ? '—' : roles.length}</div>
        </div>
      </div>

      <!-- Tab bar -->
      <div style="display:flex;gap:0;border-bottom:1px solid var(--border);padding:0 16px">
        <button class="admin-tab-btn ${_currentTab === 'users' ? 'active' : ''}" data-tab="users">
          <i class="fa-solid fa-users" style="font-size:11px"></i> Users
        </button>
        <button class="admin-tab-btn ${_currentTab === 'teams' ? 'active' : ''}" data-tab="teams">
          <i class="fa-solid fa-people-group" style="font-size:11px"></i> Teams
        </button>
        <button class="admin-tab-btn ${_currentTab === 'roles' ? 'active' : ''}" data-tab="roles">
          <i class="fa-solid fa-key" style="font-size:11px"></i> Roles & Permissions
        </button>
        <button class="admin-tab-btn ${_currentTab === 'audit' ? 'active' : ''}" data-tab="audit">
          <i class="fa-solid fa-clock-rotate-left" style="font-size:11px"></i> Audit Log
        </button>
      </div>

      <!-- Tab content -->
      <div style="flex:1;overflow:auto">
        <div id="admin-tab-users" style="${_currentTab !== 'users' ? 'display:none' : ''}">
          ${errors.users ? _errorBox('users', errors.users) : _renderUsersTable(users)}
          ${errors.invitations ? _errorBox('invitations', errors.invitations) : _renderInvitations(invitations)}
        </div>
        <div id="admin-tab-teams" style="${_currentTab !== 'teams' ? 'display:none' : ''}">
          ${errors.teams ? _errorBox('teams', errors.teams) : _renderTeamsTable(_data.teams)}
        </div>
        <div id="admin-tab-roles" style="${_currentTab !== 'roles' ? 'display:none' : ''}">
          ${errors.roles ? _errorBox('roles', errors.roles) : _renderRolesTable(roles)}
        </div>
        <div id="admin-tab-audit" style="${_currentTab !== 'audit' ? 'display:none' : ''}">
          ${errors.audit ? _errorBox('the audit log', errors.audit) : _renderAuditTable(_data.audit)}
        </div>
      </div>
    </div>
    <style>
      .admin-tab-btn {
        background: none;
        border: none;
        border-bottom: 2px solid transparent;
        padding: 10px 16px;
        font-size: 12px;
        font-weight: 500;
        color: var(--muted);
        cursor: pointer;
        display: flex;
        align-items: center;
        gap: 6px;
        transition: color 0.15s, border-color 0.15s;
      }
      .admin-tab-btn:hover { color: var(--text); }
      .admin-tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
      .admin-avatar {
        width: 32px; height: 32px; border-radius: 50%;
        display: flex; align-items: center; justify-content: center;
        font-size: 12px; font-weight: 700; color: #fff; flex-shrink: 0;
      }
      .admin-role-badge {
        font-size: 11px; padding: 2px 8px; border-radius: 4px; font-weight: 600;
        background: rgba(59,130,246,0.12); color: #3b82f6;
      }
      .admin-status-dot {
        width: 8px; height: 8px; border-radius: 50%; display: inline-block;
        margin-right: 6px; vertical-align: middle;
      }
      .admin-status-dot.active { background: var(--accent); }
      .admin-status-dot.invited { background: var(--warn); }
      .admin-status-dot.disabled { background: var(--muted); }
    </style>
  `;

  // Attach tab switching
  body.querySelectorAll('.admin-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => _switchTab(btn.getAttribute('data-tab')));
  });

  // Attach user action handlers
  body.querySelectorAll('.admin-edit-btn').forEach(btn => {
    btn.addEventListener('click', () => _showEditUserModal(btn.getAttribute('data-user-id')));
  });
  body.querySelectorAll('.admin-remove-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      _confirmRemoveUser(btn.getAttribute('data-user-id'), btn.getAttribute('data-user-name'));
    });
  });
  body.querySelectorAll('.admin-revoke-btn').forEach(btn => {
    btn.addEventListener('click', () => _revokeInvitation(btn.getAttribute('data-invite-id')));
  });

  _syncInviteButton(body);
}

function _renderUsersTable(users) {
  if (!users || users.length === 0) {
    return '<div style="text-align:center;padding:40px;color:var(--muted)">No users found. Invite your first team member.</div>';
  }

  return `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Name</th>
            <th>Status</th>
            <th>Last Login</th>
            <th>Created</th>
            <th style="text-align:right">Actions</th>
          </tr>
        </thead>
        <tbody>
          ${users.map(u => {
            const label = u.display_name || u.email || '';
            const status = u.is_active ? 'active' : 'disabled';
            return `<tr>
              <td>
                <div style="display:flex;align-items:center;gap:10px">
                  <div class="admin-avatar" style="background:${_avatarColor(label)}">${esc(_initials(label))}</div>
                  <div>
                    <div style="font-weight:500;color:var(--text)">${esc(u.display_name || '—')}</div>
                    <div style="font-size:11px;color:var(--muted)">${esc(u.email || '')}</div>
                  </div>
                </div>
              </td>
              <td><span class="admin-status-dot ${status}"></span>${status.charAt(0).toUpperCase() + status.slice(1)}</td>
              <td style="font-size:11px;color:var(--muted)">${_relativeTime(u.last_login_at)}</td>
              <td style="font-size:11px;color:var(--muted)">${u.created_at ? new Date(u.created_at).toLocaleDateString() : '—'}</td>
              <td style="text-align:right">
                <div style="display:flex;gap:4px;justify-content:flex-end">
                  <button class="ds-btn ds-btn-ghost ds-btn-sm admin-edit-btn" data-user-id="${esc(u.id || '')}" title="Edit user">
                    <i class="fa-solid fa-pen-to-square"></i>
                  </button>
                  <button class="ds-btn ds-btn-ghost ds-btn-sm admin-remove-btn" data-user-id="${esc(u.id || '')}" data-user-name="${esc(label)}" title="Remove user" style="color:var(--danger)">
                    <i class="fa-solid fa-trash-can"></i>
                  </button>
                </div>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function _renderInvitations(invitations) {
  if (!invitations || invitations.length === 0) return '';
  return `
    <div style="padding:12px 16px 4px;font-size:12px;font-weight:600;color:var(--text)">Pending invitations</div>
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr><th>Email</th><th>Role</th><th>Team</th><th>Channel</th><th>Expires</th><th style="text-align:right">Actions</th></tr>
        </thead>
        <tbody>
          ${invitations.map(inv => `<tr>
            <td>${esc(inv.email)}</td>
            <td><span class="admin-role-badge">${esc(inv.role_name || '—')}</span></td>
            <td style="color:var(--muted)">${esc(inv.team_name || '—')}</td>
            <td style="color:var(--muted)">${esc(inv.channel || '—')}</td>
            <td style="font-size:11px;color:var(--muted)">${inv.expires_at ? new Date(inv.expires_at).toLocaleDateString() : '—'}</td>
            <td style="text-align:right">
              <button class="ds-btn ds-btn-ghost ds-btn-sm admin-revoke-btn" data-invite-id="${esc(inv.id)}" title="Revoke invitation" style="color:var(--danger)">
                <i class="fa-solid fa-ban"></i>
              </button>
            </td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function _renderTeamsTable(teams) {
  if (!teams || teams.length === 0) {
    return '<div style="text-align:center;padding:40px;color:var(--muted)">No teams yet. Create your first team from the Teams view.</div>';
  }

  return `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Team</th>
            <th>Slug</th>
            <th>Department</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          ${teams.map(t => {
            const created = t.created_at ? new Date(t.created_at).toLocaleDateString() : '—';
            return `<tr>
              <td style="font-weight:500;color:var(--text)">${esc(t.name || t.slug || '')}</td>
              <td style="font-family:var(--mono);font-size:11px;color:var(--muted)">${esc(t.slug || '')}</td>
              <td style="color:var(--muted)">${esc(t.department || '—')}</td>
              <td style="font-family:var(--mono);font-size:11px">${created}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function _renderRolesTable(roles) {
  if (!roles || roles.length === 0) {
    return '<div style="text-align:center;padding:40px;color:var(--muted)">No roles defined.</div>';
  }

  return `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Role</th>
            <th>Description</th>
            <th>Type</th>
            <th style="text-align:right">Allow</th>
            <th style="text-align:right">Deny</th>
          </tr>
        </thead>
        <tbody>
          ${roles.map(r => `<tr>
            <td><span class="admin-role-badge">${esc(r.name)}</span></td>
            <td style="color:var(--muted);max-width:360px">${esc(r.description || '—')}</td>
            <td style="color:var(--muted)">${r.is_system ? 'System' : 'Custom'}</td>
            <td style="text-align:right;font-family:var(--mono)" title="${esc((r.allow || []).join(', '))}">${(r.allow || []).length}</td>
            <td style="text-align:right;font-family:var(--mono)" title="${esc((r.deny || []).join(', '))}">${(r.deny || []).length}</td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function _renderAuditTable(events) {
  if (!events || events.length === 0) {
    return '<div style="text-align:center;padding:40px;color:var(--muted)">No audit events recorded yet.</div>';
  }

  return `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Timestamp</th>
            <th>Actor</th>
            <th>Action</th>
            <th>Resource</th>
            <th style="text-align:right">Seq</th>
          </tr>
        </thead>
        <tbody>
          ${events.map(e => {
            const ts = e.occurred_at ? new Date(e.occurred_at).toLocaleString() : '—';
            const resource = e.resource_id ? `${e.resource_type} ${e.resource_id}` : (e.resource_type || '—');
            return `<tr>
              <td style="white-space:nowrap;font-family:var(--mono);font-size:11px">${esc(ts)}</td>
              <td>${esc(e.actor_id || '—')}</td>
              <td><span class="ds-badge-info" style="font-size:11px">${esc(e.action || '—')}</span></td>
              <td style="color:var(--muted);max-width:320px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(resource)}">${esc(resource)}</td>
              <td style="text-align:right;font-family:var(--mono);font-size:11px;color:var(--muted)">${e.chain_seq != null ? e.chain_seq : '—'}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

// ── Tab switching ────────────────────────────────────────────────────────────

function _syncInviteButton(body) {
  const inviteBtn = body.closest('.gs-tile')?.querySelector('.gs-tile-actions button');
  if (inviteBtn) inviteBtn.style.display = _currentTab === 'users' ? '' : 'none';
}

function _switchTab(tab) {
  _currentTab = tab;
  const body = document.getElementById('admin-panel-body');
  if (!body) return;

  body.querySelectorAll('.admin-tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-tab') === tab);
  });

  ['users', 'teams', 'roles', 'audit'].forEach(t => {
    const el = document.getElementById('admin-tab-' + t);
    if (el) el.style.display = t === tab ? '' : 'none';
  });

  _syncInviteButton(body);
}

// ── User actions ─────────────────────────────────────────────────────────────

/** Show an API error inside a modal's message slot. */
function _modalError(modalBody, msg) {
  const el = modalBody.querySelector('.admin-modal-error');
  if (el) {
    el.textContent = msg;
    el.style.display = msg ? 'block' : 'none';
  }
}

/** Extract a readable message from a failed fetch Response. */
async function _errorText(resp) {
  const body = await resp.json().catch(() => ({}));
  const d = body.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) return d.map(x => x.msg || JSON.stringify(x)).join('; ');
  return `HTTP ${resp.status}`;
}

function _showInviteModal() {
  const roleOptions = _data.roles.map(r => `<option value="${esc(r.id)}">${esc(r.name)}</option>`).join('');
  const teamOptions = _data.teams.map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join('');
  const configured = new Set(_data.channels.configured || []);
  const channelOptions = (_data.channels.all || ['email']).map(c =>
    `<option value="${esc(c)}">${esc(c)}${configured.has(c) ? '' : ' (not configured)'}</option>`).join('');

  openModal({
    title: 'Invite User',
    maxWidth: '440px',
    renderBody: (modalBody) => {
      modalBody.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:12px;padding:4px 0">
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Email *</label>
            <input type="email" class="ds-input" id="invite-email" placeholder="user@company.com" style="width:100%" />
          </div>
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Name *</label>
            <input type="text" class="ds-input" id="invite-name" placeholder="Full name" style="width:100%" />
          </div>
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Role *</label>
            <select class="ds-select" id="invite-role" style="width:100%">${roleOptions}</select>
          </div>
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Team</label>
            <select class="ds-select" id="invite-team" style="width:100%"><option value="">No specific team</option>${teamOptions}</select>
          </div>
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Delivery channel</label>
            <select class="ds-select" id="invite-channel" style="width:100%">${channelOptions}</select>
            ${configured.size === 0 ? '<div style="font-size:11px;color:var(--warn);margin-top:4px">No delivery channel is configured, so the invitation is recorded but not sent.</div>' : ''}
          </div>
          <div class="admin-modal-error" style="display:none;font-size:12px;color:var(--danger)"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
            <button class="ds-btn ds-btn-ghost" id="invite-cancel">Cancel</button>
            <button class="ds-btn ds-btn-primary" id="invite-submit">Send Invite</button>
          </div>
        </div>
      `;

      const modalId = modalBody.closest('.ds-modal-backdrop').id;
      modalBody.querySelector('#invite-cancel').onclick = () => closeModal(modalId);
      modalBody.querySelector('#invite-submit').onclick = async () => {
        const email = (modalBody.querySelector('#invite-email').value || '').trim();
        const displayName = (modalBody.querySelector('#invite-name').value || '').trim();
        const roleId = modalBody.querySelector('#invite-role').value;
        const teamId = modalBody.querySelector('#invite-team').value;
        const channel = modalBody.querySelector('#invite-channel').value;
        if (!email || !displayName || !roleId) {
          _modalError(modalBody, 'Email, name and role are required.');
          return;
        }

        try {
          const resp = await rawFetch('/api/v1/users/invite', {
            method: 'POST',
            body: JSON.stringify({
              email, display_name: displayName, role_id: roleId,
              team_id: teamId || null, channel,
            }),
          });
          if (!resp.ok) {
            _modalError(modalBody, await _errorText(resp));
            return;
          }
          closeModal(modalId);
          toast(`Invitation created for ${email}`, 'success');
          if (!_destroyed) loadData();
        } catch (e) {
          _modalError(modalBody, e.message);
        }
      };
    },
  });
}

function _showEditUserModal(userId) {
  const user = _data.users.find(u => u.id === userId);
  if (!user) return;

  openModal({
    title: 'Edit User',
    maxWidth: '440px',
    renderBody: (modalBody) => {
      modalBody.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:12px;padding:4px 0">
          <div style="font-size:12px;color:var(--muted)">${esc(user.email || '')}</div>
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Name</label>
            <input type="text" class="ds-input" id="edit-name" value="${esc(user.display_name || '')}" style="width:100%" />
          </div>
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Status</label>
            <select class="ds-select" id="edit-status" style="width:100%">
              <option value="active" ${user.is_active ? 'selected' : ''}>Active</option>
              <option value="disabled" ${!user.is_active ? 'selected' : ''}>Disabled</option>
            </select>
          </div>
          <div style="font-size:11px;color:var(--muted)">Roles are assigned through role assignments, not from this form.</div>
          <div class="admin-modal-error" style="display:none;font-size:12px;color:var(--danger)"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
            <button class="ds-btn ds-btn-ghost" id="edit-cancel">Cancel</button>
            <button class="ds-btn ds-btn-primary" id="edit-submit">Save Changes</button>
          </div>
        </div>
      `;

      const modalId = modalBody.closest('.ds-modal-backdrop').id;
      modalBody.querySelector('#edit-cancel').onclick = () => closeModal(modalId);
      modalBody.querySelector('#edit-submit').onclick = async () => {
        const displayName = (modalBody.querySelector('#edit-name').value || '').trim();
        const isActive = modalBody.querySelector('#edit-status').value === 'active';
        if (!displayName) {
          _modalError(modalBody, 'Name cannot be empty.');
          return;
        }

        try {
          const resp = await rawFetch(`/api/v1/users/${encodeURIComponent(userId)}`, {
            method: 'PATCH',
            body: JSON.stringify({ display_name: displayName, is_active: isActive }),
          });
          if (!resp.ok) {
            _modalError(modalBody, await _errorText(resp));
            return;
          }
          closeModal(modalId);
          toast('User updated', 'success');
          if (!_destroyed) loadData();
        } catch (e) {
          _modalError(modalBody, e.message);
        }
      };
    },
  });
}

function _confirmRemoveUser(userId, userName) {
  openModal({
    title: 'Remove User',
    maxWidth: '400px',
    renderBody: (modalBody) => {
      modalBody.innerHTML = `
        <div style="padding:8px 0">
          <p style="color:var(--text);font-size:13px;margin:0 0 16px">
            Remove <strong>${esc(userName)}</strong> from this organization? This cannot be undone.
          </p>
          <div class="admin-modal-error" style="display:none;font-size:12px;color:var(--danger);margin-bottom:8px"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="ds-btn ds-btn-ghost" id="remove-cancel">Cancel</button>
            <button class="ds-btn ds-btn-primary" id="remove-confirm" style="background:var(--danger)">Remove</button>
          </div>
        </div>
      `;

      const modalId = modalBody.closest('.ds-modal-backdrop').id;
      modalBody.querySelector('#remove-cancel').onclick = () => closeModal(modalId);
      modalBody.querySelector('#remove-confirm').onclick = async () => {
        try {
          const resp = await rawFetch(`/api/v1/users/${encodeURIComponent(userId)}`, { method: 'DELETE' });
          if (!resp.ok) {
            _modalError(modalBody, await _errorText(resp));
            return;
          }
          closeModal(modalId);
          toast('User removed', 'success');
          if (!_destroyed) loadData();
        } catch (e) {
          _modalError(modalBody, e.message);
        }
      };
    },
  });
}

async function _revokeInvitation(inviteId) {
  try {
    const resp = await rawFetch(`/api/v1/users/invitations/${encodeURIComponent(inviteId)}/revoke`, { method: 'POST' });
    if (!resp.ok) {
      toast('Could not revoke invitation: ' + await _errorText(resp), 'error');
      return;
    }
    toast('Invitation revoked', 'success');
    if (!_destroyed) loadData();
  } catch (e) {
    toast('Could not revoke invitation: ' + e.message, 'error');
  }
}

// ── Utility helpers ──────────────────────────────────────────────────────────

function _avatarColor(name) {
  let h = 0;
  for (let i = 0; i < (name || '').length; i++) h = ((h << 5) - h + name.charCodeAt(i)) | 0;
  return AVATAR_COLORS[Math.abs(h) % AVATAR_COLORS.length];
}

function _initials(name) {
  if (!name) return '?';
  const parts = name.trim().split(/\s+/);
  return parts.length >= 2
    ? (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
    : name.slice(0, 2).toUpperCase();
}

function _relativeTime(iso) {
  if (!iso) return 'Never';
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  if (diff < 60000) return 'Just now';
  if (diff < 3600000) return Math.floor(diff / 60000) + 'm ago';
  if (diff < 86400000) return Math.floor(diff / 3600000) + 'h ago';
  if (diff < 604800000) return Math.floor(diff / 86400000) + 'd ago';
  return d.toLocaleDateString();
}
