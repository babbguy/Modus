/**
 * Modus Dashboard v2 — Profile View
 * Renders a user profile page with avatar, name, email, role,
 * edit form, change password, and API key management.
 * This is a simple form view — no Gridstack tiles, just a static container.
 */

import { rawFetch, esc } from '../api.js';
import { get } from '../state.js';
import { openModal, closeModal } from '../modal.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _container = null;
let _destroyed = false;
let _isEditing = false;

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Profile view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;
  _isEditing = false;

  _renderProfile();
  await _loadFromApi();
}

/**
 * Tear down the Profile view.
 */
export function destroy() {
  _destroyed = true;
  _container = null;
}

// ── Profile renderer ─────────────────────────────────────────────────────────

function _renderProfile() {
  if (!_container) return;

  const profile = _getProfile();
  const name = profile.displayName || 'User';
  const email = profile.email || '';
  const role = profile.role || 'Admin';
  const initial = name.charAt(0).toUpperCase();
  const apiKey = localStorage.getItem('modus_api_key') || '';
  const maskedKey = apiKey ? apiKey.slice(0, 8) + '\u2022'.repeat(Math.max(0, apiKey.length - 12)) + apiKey.slice(-4) : 'No API key configured';

  _container.innerHTML = `
    <div style="max-width:600px;margin:0 auto;padding:24px 16px">

      <!-- Profile Card -->
      <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:20px">
        <div style="display:flex;align-items:center;gap:16px;margin-bottom:20px">
          <div style="width:56px;height:56px;border-radius:50%;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:22px;font-weight:700;color:#fff;flex-shrink:0">${initial}</div>
          <div>
            <div id="profile-name-display" style="font-size:18px;font-weight:700;color:var(--text)">${esc(name)}</div>
            <div id="profile-email-display" style="font-size:13px;color:var(--muted);margin-top:2px">${esc(email) || 'No email set'}</div>
            <div style="margin-top:4px">
              <span style="font-size:11px;padding:2px 8px;border-radius:4px;background:rgba(159,193,49,0.12);color:var(--accent);font-weight:600">${esc(role)}</span>
            </div>
          </div>
          <button class="ds-btn ds-btn-ghost ds-btn-sm" id="profile-edit-toggle" style="margin-left:auto">
            <i class="fa-solid fa-pen"></i> Edit
          </button>
        </div>

        <!-- Display mode -->
        <div id="profile-display-section">
          <div style="display:grid;grid-template-columns:100px 1fr;gap:6px 16px;font-size:13px">
            <span style="color:var(--muted)">Name</span>
            <span id="profile-display-name" style="color:var(--text)">${esc(name)}</span>
            <span style="color:var(--muted)">Email</span>
            <span id="profile-display-email" style="color:var(--text)">${esc(email) || '\u2014'}</span>
            <span style="color:var(--muted)">Role</span>
            <span id="profile-display-role" style="color:var(--text)">${esc(role)}</span>
          </div>
        </div>

        <!-- Edit mode (hidden by default) -->
        <div id="profile-edit-section" style="display:none">
          <div style="display:flex;flex-direction:column;gap:12px">
            <div>
              <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Display Name</label>
              <input type="text" class="ds-input" id="profile-edit-name" value="${esc(name)}" style="width:100%" />
            </div>
            <div>
              <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Email</label>
              <input type="email" class="ds-input" id="profile-edit-email" value="${esc(email)}" style="width:100%" />
            </div>
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:4px">
              <button class="ds-btn ds-btn-ghost" id="profile-edit-cancel">Cancel</button>
              <button class="ds-btn ds-btn-primary" id="profile-edit-save">Save</button>
            </div>
            <div id="profile-save-msg" style="display:none;font-size:12px;color:var(--accent);text-align:right"></div>
          </div>
        </div>
      </div>

      <!-- Change Password -->
      <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px;margin-bottom:20px">
        <h4 style="margin:0 0 16px;font-size:14px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px">
          <i class="fa-solid fa-lock" style="color:#3b82f6;font-size:12px"></i> Change Password
        </h4>
        <div style="display:flex;flex-direction:column;gap:12px">
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Current Password</label>
            <input type="password" class="ds-input" id="profile-current-pw" placeholder="\u2022\u2022\u2022\u2022\u2022\u2022\u2022\u2022" style="width:100%" />
          </div>
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">New Password</label>
            <input type="password" class="ds-input" id="profile-new-pw" placeholder="Enter new password" style="width:100%" />
          </div>
          <div>
            <label style="font-size:12px;color:var(--muted);display:block;margin-bottom:4px">Confirm New Password</label>
            <input type="password" class="ds-input" id="profile-confirm-pw" placeholder="Confirm new password" style="width:100%" />
          </div>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:4px">
            <button class="ds-btn ds-btn-primary" id="profile-change-pw">Update Password</button>
          </div>
          <div id="profile-pw-msg" style="display:none;font-size:12px;text-align:right"></div>
        </div>
      </div>

      <!-- API Key -->
      <div style="background:var(--card);border:1px solid var(--border);border-radius:12px;padding:24px">
        <h4 style="margin:0 0 16px;font-size:14px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px">
          <i class="fa-solid fa-key" style="color:#f59e0b;font-size:12px"></i> API Key
        </h4>
        <div style="display:flex;align-items:center;gap:12px">
          <div style="flex:1;font-family:var(--mono);font-size:12px;padding:8px 12px;background:rgba(0,0,0,0.2);border-radius:6px;border:1px solid var(--border);color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" id="profile-api-key-display">${esc(maskedKey)}</div>
          <button class="ds-btn ds-btn-ghost ds-btn-sm" id="profile-copy-key" title="Copy to clipboard" ${!apiKey ? 'disabled' : ''}>
            <i class="fa-solid fa-copy"></i>
          </button>
          <button class="ds-btn ds-btn-ghost ds-btn-sm" id="profile-regen-key" title="Regenerate API key" style="color:var(--warn)">
            <i class="fa-solid fa-rotate"></i>
          </button>
        </div>
        <p style="font-size:11px;color:var(--muted);margin:8px 0 0">
          Your API key is used to authenticate SDK connections. Keep it secret.
        </p>
      </div>

    </div>
  `;

  _attachHandlers();
}

// ── Event handlers ───────────────────────────────────────────────────────────

function _attachHandlers() {
  if (!_container) return;

  // Toggle edit mode
  const editToggle = _container.querySelector('#profile-edit-toggle');
  if (editToggle) {
    editToggle.addEventListener('click', () => _toggleEdit());
  }

  // Cancel edit
  const editCancel = _container.querySelector('#profile-edit-cancel');
  if (editCancel) {
    editCancel.addEventListener('click', () => _toggleEdit(false));
  }

  // Save profile
  const editSave = _container.querySelector('#profile-edit-save');
  if (editSave) {
    editSave.addEventListener('click', () => _saveProfile());
  }

  // Change password
  const changePw = _container.querySelector('#profile-change-pw');
  if (changePw) {
    changePw.addEventListener('click', () => _changePassword());
  }

  // Copy API key
  const copyKey = _container.querySelector('#profile-copy-key');
  if (copyKey) {
    copyKey.addEventListener('click', () => _copyApiKey());
  }

  // Regenerate API key
  const regenKey = _container.querySelector('#profile-regen-key');
  if (regenKey) {
    regenKey.addEventListener('click', () => _regenApiKey());
  }
}

function _toggleEdit(show) {
  if (!_container) return;
  const displaySection = _container.querySelector('#profile-display-section');
  const editSection = _container.querySelector('#profile-edit-section');
  const toggleBtn = _container.querySelector('#profile-edit-toggle');
  if (!displaySection || !editSection) return;

  _isEditing = show !== undefined ? show : !_isEditing;

  if (_isEditing) {
    displaySection.style.display = 'none';
    editSection.style.display = '';
    if (toggleBtn) toggleBtn.innerHTML = '<i class="fa-solid fa-xmark"></i> Cancel';
  } else {
    displaySection.style.display = '';
    editSection.style.display = 'none';
    if (toggleBtn) toggleBtn.innerHTML = '<i class="fa-solid fa-pen"></i> Edit';
  }
}

function _saveProfile() {
  if (!_container) return;

  const nameInput = _container.querySelector('#profile-edit-name');
  const emailInput = _container.querySelector('#profile-edit-email');
  if (!nameInput || !emailInput) return;

  const displayName = nameInput.value.trim() || 'User';
  const email = emailInput.value.trim();
  const profile = _getProfile();
  profile.displayName = displayName;
  profile.email = email;

  localStorage.setItem('modus_profile', JSON.stringify(profile));

  // Update sidebar
  _updateSidebar(profile);

  // Try saving to API (best effort)
  rawFetch('/api/v1/users/me', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ display_name: displayName, email }),
  }).catch(() => null);

  // Show save message
  const msg = _container.querySelector('#profile-save-msg');
  if (msg) {
    msg.textContent = 'Profile saved.';
    msg.style.display = '';
    setTimeout(() => { if (msg) msg.style.display = 'none'; }, 2500);
  }

  // Re-render to reflect changes
  _renderProfile();
}

async function _changePassword() {
  if (!_container) return;

  const currentPw = _container.querySelector('#profile-current-pw')?.value || '';
  const newPw = _container.querySelector('#profile-new-pw')?.value || '';
  const confirmPw = _container.querySelector('#profile-confirm-pw')?.value || '';
  const msgEl = _container.querySelector('#profile-pw-msg');

  if (!currentPw || !newPw) {
    _showMsg(msgEl, 'Please fill in all password fields.', 'var(--warn)');
    return;
  }

  if (newPw !== confirmPw) {
    _showMsg(msgEl, 'New passwords do not match.', 'var(--danger)');
    return;
  }

  if (newPw.length < 8) {
    _showMsg(msgEl, 'Password must be at least 8 characters.', 'var(--warn)');
    return;
  }

  try {
    const resp = await rawFetch('/api/v1/users/me/password', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ current_password: currentPw, new_password: newPw }),
    });
    if (resp.ok) {
      _showMsg(msgEl, 'Password updated successfully.', 'var(--accent)');
      if (_container) {
        _container.querySelector('#profile-current-pw').value = '';
        _container.querySelector('#profile-new-pw').value = '';
        _container.querySelector('#profile-confirm-pw').value = '';
      }
    } else {
      _showMsg(msgEl, 'Failed to update password.', 'var(--danger)');
    }
  } catch (e) {
    _showMsg(msgEl, 'Password update is not available in this deployment.', 'var(--muted)');
  }
}

function _copyApiKey() {
  const apiKey = localStorage.getItem('modus_api_key') || get('apiKey') || '';
  if (!apiKey) return;

  navigator.clipboard.writeText(apiKey).then(() => {
    const display = _container?.querySelector('#profile-api-key-display');
    if (display) {
      const orig = display.textContent;
      display.textContent = 'Copied!';
      display.style.color = 'var(--accent)';
      setTimeout(() => {
        if (display) {
          display.textContent = orig;
          display.style.color = 'var(--muted)';
        }
      }, 1500);
    }
  }).catch(() => null);
}

function _regenApiKey() {
  openModal({
    title: 'Regenerate API Key',
    maxWidth: '400px',
    renderBody: (modalBody) => {
      modalBody.innerHTML = `
        <div style="padding:8px 0">
          <p style="color:var(--text);font-size:13px;margin:0 0 16px">
            This will invalidate your current API key. All SDK connections using the old key will stop working.
          </p>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="ds-btn ds-btn-ghost" id="regen-cancel">Cancel</button>
            <button class="ds-btn ds-btn-primary" id="regen-confirm" style="background:var(--warn)">Regenerate</button>
          </div>
        </div>
      `;

      const modalId = modalBody.closest('.ds-modal-backdrop').id;
      modalBody.querySelector('#regen-cancel').onclick = () => closeModal(modalId);
      modalBody.querySelector('#regen-confirm').onclick = async () => {
        closeModal(modalId);
        try {
          const resp = await rawFetch('/api/v1/users/me/api-key', { method: 'POST' });
          if (resp.ok) {
            const data = await resp.json().catch(() => null);
            if (data && data.api_key) {
              localStorage.setItem('modus_api_key', data.api_key);
              if (!_destroyed) _renderProfile();
            }
          }
        } catch (e) {
          // Generate a local placeholder key for demo
          const newKey = 'cntl_' + Array.from(crypto.getRandomValues(new Uint8Array(24)), b => b.toString(16).padStart(2, '0')).join('');
          localStorage.setItem('modus_api_key', newKey);
          if (!_destroyed) _renderProfile();
        }
      };
    },
  });
}

// ── Utility helpers ──────────────────────────────────────────────────────────

function _getProfile() {
  try {
    return JSON.parse(localStorage.getItem('modus_profile') || 'null') || {
      displayName: 'User', email: '', role: 'Admin',
    };
  } catch (_) {
    return { displayName: 'User', email: '', role: 'Admin' };
  }
}

function _updateSidebar(profile) {
  const name = profile.displayName || 'User';
  const avatarEl = document.getElementById('sidebar-profile-avatar');
  const nameEl = document.getElementById('sidebar-profile-name');
  const roleEl = document.getElementById('sidebar-profile-role');
  if (avatarEl) avatarEl.textContent = name.charAt(0).toUpperCase();
  if (nameEl) nameEl.textContent = name;
  if (roleEl) roleEl.textContent = profile.role || 'Admin';
}

async function _loadFromApi() {
  try {
    const resp = await rawFetch('/api/v1/users/me');
    if (resp.ok) {
      const me = await resp.json();
      if (_destroyed) return;
      if (me.email || me.display_name) {
        const profile = {
          displayName: me.display_name || me.email || 'User',
          email: me.email || '',
          role: me.role || 'Admin',
        };
        localStorage.setItem('modus_profile', JSON.stringify(profile));
        _updateSidebar(profile);
        _renderProfile();
      }
    }
  } catch (_) { /* API not available, use localStorage */ }
}

function _showMsg(el, text, color) {
  if (!el) return;
  el.textContent = text;
  el.style.color = color;
  el.style.display = '';
  setTimeout(() => { if (el) el.style.display = 'none'; }, 3000);
}
