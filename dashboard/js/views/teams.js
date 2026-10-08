/**
 * Modus Dashboard v2 — Teams View
 * Renders the teams management page with a table and Create Team modal.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError } from '../tile.js';
import { rawFetch, esc } from '../api.js';
import { fmtDate } from '../format.js';
import { get } from '../state.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _teamsCache = [];

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
    filterPlaceholder: 'Filter teams\u2026',
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
    const resp = await rawFetch('/api/v1/teams').catch(() => null);
    if (_destroyed) return;
    if (resp && resp.ok) {
      _teamsCache = await resp.json().catch(() => []);
    } else {
      _teamsCache = [];
    }
    _renderTable(_teamsCache);
  } catch (err) {
    if (_destroyed) return;
    console.error('[teams] data fetch failed:', err);
    setTileError('teams-table', 'Failed to load teams', () => _loadData());
  }
}

// ── Rendering ────────────────────────────────────────────────────────────────

function _renderTable(items) {
  const body = document.getElementById('teams-table-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('teams-table', {
      icon: 'fa-solid fa-users',
      title: 'No teams yet',
      description: 'Create your first team to organize apps and users.',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Team Name</th>
            <th>Slug</th>
            <th>Description</th>
            <th>Created</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((t, idx) => {
            const created = t.created_at ? new Date(t.created_at).toLocaleDateString() : '\u2014';
            return `<tr data-idx="${idx}">
              <td style="font-weight:600">${esc(t.name || '')}</td>
              <td style="font-family:var(--mono);font-size:11px">${esc(t.slug || '')}</td>
              <td style="font-size:11px;color:var(--muted)">${esc(t.description || '\u2014')}</td>
              <td style="font-size:11px;color:var(--muted)">${created}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

// ── Filter ───────────────────────────────────────────────────────────────────

function _filterTable(query) {
  const body = document.getElementById('teams-table-body');
  if (!body) return;
  const q = (query || '').toLowerCase();
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
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Team Name</label>
            <input class="ds-input" id="ct-name" placeholder="Engineering" style="width:100%">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Slug</label>
            <input class="ds-input" id="ct-slug" placeholder="engineering" style="width:100%">
            <div style="font-size:10px;color:var(--muted);margin-top:4px">Lowercase, no spaces. Used as an identifier in API calls.</div>
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
    const result = document.getElementById('ct-result');
    if (result) {
      result.style.display = 'block';
      result.innerHTML = '<div style="color:var(--danger);font-size:12px">Team name and slug are required.</div>';
    }
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Creating\u2026';

  try {
    const resp = await rawFetch('/api/v1/teams', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, slug }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || 'Failed to create team.');
    }
    closeModal(modalId);
    _loadData();
  } catch (e) {
    const result = document.getElementById('ct-result');
    if (result) {
      result.style.display = 'block';
      result.innerHTML = `<div style="color:var(--danger);font-size:12px">${esc(e.message)}</div>`;
    }
    btn.disabled = false;
    btn.textContent = 'Create Team';
  }
}
