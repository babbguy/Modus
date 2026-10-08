/**
 * Modus Dashboard v2 — Federation Control View
 * Manages federation peers (subsidiary Modus instances).
 * 3 tiles: KPIs, Peer Grid, Sync History.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError } from '../tile.js';
import { rawFetch, esc } from '../api.js';
import { timeSince } from '../format.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _peers = [];

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Federation view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;
  _peers = [];

  // Initialize grid
  _grid = initGrid(container, 'federation');

  // Create tiles
  const tiles = [
    _createKpiTile(),
    _createPeersTile(),
    _createHistoryTile(),
  ];

  // Load saved or default layout
  const layout = loadLayout('federation', LAYOUTS.federation);

  // Add tiles to grid
  addTiles(_grid, tiles, layout);

  // Fetch and render data
  await _loadData();
}

/**
 * Tear down the Federation view, cleaning up DOM.
 */
export function destroy() {
  _destroyed = true;
  _grid = null;
  _container = null;
  _peers = [];
}

/**
 * Re-fetch this view's data. Called by the Settings -> Auto-refresh timer
 * (js/auto-refresh.js); a no-op once the view has been destroyed.
 */
export function refresh() {
  return _destroyed ? Promise.resolve() : _loadData();
}

// ── Data loading ─────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;

  ['fed-kpis', 'fed-peers', 'fed-history'].forEach(id => setTileLoading(id));

  try {
    const resp = await rawFetch('/api/v1/admin/federation/peers');
    if (_destroyed) return;

    if (!resp.ok) {
      const errText = `HTTP ${resp.status}`;
      ['fed-kpis', 'fed-peers', 'fed-history'].forEach(id => setTileError(id, errText));
      return;
    }

    const data = await resp.json();
    if (_destroyed) return;

    const list = Array.isArray(data) ? data : (data.peers || []);
    _peers = await _withHistory(list.map(_normalizePeer));
    if (_destroyed) return;

    _renderKpis(_peers);
    _renderPeers(_peers);
    _renderHistory(_peers);
  } catch (err) {
    if (_destroyed) return;
    ['fed-kpis', 'fed-peers', 'fed-history'].forEach(id => setTileError(id, err.message));
  }
}

/**
 * Map a GET /api/v1/admin/federation/peers item (PeerResponse) onto the field
 * names this view renders. The API reports status "active" | "paused" |
 * "unreachable"; the UI labels an active peer "connected".
 */
function _normalizePeer(p) {
  const status = (p.status || 'unreachable').toLowerCase();
  return {
    ...p,
    status: status === 'active' ? 'connected' : status,
    peer_url: p.peer_url_masked || '',
    last_heartbeat: p.last_heartbeat_at || null,
    latency_ms: p.last_heartbeat_latency_ms ?? null,
    team: p.team_slug || p.team_id || null,
    sync_history: [],
  };
}

/**
 * Attach recent sync history to each peer from
 * GET /api/v1/admin/federation/peers/{id}/history (SyncLogEntry[]:
 * direction, status, latency_ms, error_message, synced_at). A failed history
 * fetch leaves that peer's history empty rather than failing the whole view.
 */
async function _withHistory(peers) {
  const capped = peers.slice(0, 25);
  const histories = await Promise.all(capped.map(async (p) => {
    try {
      const resp = await rawFetch(`/api/v1/admin/federation/peers/${encodeURIComponent(p.id)}/history?limit=20`);
      if (!resp.ok) return [];
      const rows = await resp.json();
      return Array.isArray(rows) ? rows : [];
    } catch (_) {
      return [];
    }
  }));
  return peers.map((p, i) => ({ ...p, sync_history: histories[i] || [] }));
}

// ── Tile creators ────────────────────────────────────────────────────────────

function _createKpiTile() {
  const tile = createTile({
    id: 'fed-kpis',
    title: '',
    className: 'kpi-row',
  });
  // Hide the header for the KPI row — the KPIs ARE the content
  const header = tile.querySelector('.gs-tile-header');
  if (header) header.style.display = 'none';
  return tile;
}

function _createPeersTile() {
  return createTile({
    id: 'fed-peers',
    title: 'Federation Peers',
    icon: 'fa-solid fa-network-wired',
    iconBg: 'rgba(6,182,212,0.15)',
    iconColor: '#06b6d4',
    headerRight: `<button class="ds-btn ds-btn-ghost ds-btn-sm" id="fed-add-peer-btn">
      <i class="fa-solid fa-plus" style="margin-right:4px"></i>Add Peer
    </button>`,
  });
}

function _createHistoryTile() {
  return createTile({
    id: 'fed-history',
    title: 'Recent Sync Activity',
    icon: 'fa-solid fa-clock-rotate-left',
    iconBg: 'rgba(139,92,246,0.15)',
    iconColor: '#8b5cf6',
    filterable: true,
    filterPlaceholder: 'Filter activity\u2026',
    onFilter: (q) => _filterTable('fed-history-body', q),
  });
}

// ── Render helpers ───────────────────────────────────────────────────────────

function _renderKpis(peers) {
  const body = document.getElementById('fed-kpis-body');
  if (!body) return;

  const total = peers.length;
  const online = peers.filter(p => (p.status || '').toLowerCase() === 'connected').length;
  const offline = peers.filter(p => ['error', 'unreachable'].includes((p.status || '').toLowerCase())).length;
  const paused = peers.filter(p => (p.status || '').toLowerCase() === 'paused').length;

  body.className = 'gs-tile-body kpi-row';
  body.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Total Peers</div>
      <div class="kpi-value">${total}</div>
      <div class="kpi-sub">federation</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Online</div>
      <div class="kpi-value" style="color:#10b981">${online}</div>
      <div class="kpi-sub">connected</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Offline</div>
      <div class="kpi-value" style="color:#ef4444">${offline}</div>
      <div class="kpi-sub">unreachable</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Paused</div>
      <div class="kpi-value" style="color:#f59e0b">${paused}</div>
      <div class="kpi-sub">suspended</div>
    </div>
  `;
}

function _renderPeers(peers) {
  const body = document.getElementById('fed-peers-body');
  if (!body) return;

  // Wire up Add Peer button
  const addBtn = document.getElementById('fed-add-peer-btn');
  if (addBtn) {
    addBtn.addEventListener('click', _showAddPeerModal);
  }

  if (!peers || !peers.length) {
    setTileEmpty('fed-peers', {
      icon: 'fa-solid fa-network-wired',
      title: 'No federation peers',
      description: 'Add a peer to start federating data across Modus instances.',
    });
    return;
  }

  body.innerHTML = `<div class="fed-peer-grid">${peers.map((p, idx) => _renderPeerCard(p, idx)).join('')}</div>`;

  // Wire up card clicks
  body.querySelectorAll('.fed-peer-card').forEach(card => {
    const idx = parseInt(card.dataset.peerIdx, 10);
    card.addEventListener('click', (e) => {
      // Don't open detail if clicking an action button
      if (e.target.closest('.fed-peer-actions')) return;
      const peer = _peers[idx];
      if (peer) _showPeerDetailModal(peer);
    });
  });

  // Wire up action buttons
  body.querySelectorAll('.fed-action-test').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _testPeer(btn.dataset.id, btn);
    });
  });

  body.querySelectorAll('.fed-action-pause').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      _togglePausePeer(btn.dataset.id, btn.dataset.status, btn);
    });
  });

  body.querySelectorAll('.fed-action-edit').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const idx = parseInt(btn.dataset.idx, 10);
      const peer = _peers[idx];
      if (peer) _showPeerDetailModal(peer);
    });
  });
}

function _renderPeerCard(peer, idx) {
  const status = (peer.status || 'unreachable').toLowerCase();
  const statusBadge = _statusBadge(status);
  const lastHb = peer.last_heartbeat ? timeSince(peer.last_heartbeat) : 'never';
  const latency = peer.latency_ms != null ? `${peer.latency_ms}ms` : '\u2014';
  const peerUrl = peer.peer_url || '';
  const isPaused = status === 'paused';
  const pauseIcon = isPaused ? 'fa-play' : 'fa-pause';
  const pauseTitle = isPaused ? 'Resume' : 'Pause';

  return `<div class="fed-peer-card" data-peer-idx="${idx}">
    <div class="fed-peer-name">${esc(peer.name || 'Unnamed Peer')}</div>
    <div class="fed-peer-url" title="${esc(peerUrl)}">${esc(peerUrl)}</div>
    <div style="margin-top:8px">${statusBadge}</div>
    <div class="fed-peer-stats">
      <span><i class="fa-regular fa-clock" style="margin-right:3px"></i>${lastHb}</span>
      <span><i class="fa-solid fa-gauge-high" style="margin-right:3px"></i>${latency}</span>
    </div>
    <div class="fed-peer-actions">
      <button class="ds-btn ds-btn-ghost ds-btn-sm fed-action-test" data-id="${esc(peer.id)}" title="Test Connection">
        <i class="fa-solid fa-bolt"></i>
      </button>
      <button class="ds-btn ds-btn-ghost ds-btn-sm fed-action-pause" data-id="${esc(peer.id)}" data-status="${status}" title="${pauseTitle}">
        <i class="fa-solid ${pauseIcon}"></i>
      </button>
      <button class="ds-btn ds-btn-ghost ds-btn-sm fed-action-edit" data-idx="${idx}" title="Edit">
        <i class="fa-solid fa-pen-to-square"></i>
      </button>
    </div>
  </div>`;
}

function _statusBadge(status) {
  const map = {
    connected:   { bg: 'rgba(16,185,129,0.15)', color: '#10b981', label: 'Connected' },
    error:       { bg: 'rgba(239,68,68,0.15)',   color: '#ef4444', label: 'Error' },
    paused:      { bg: 'rgba(245,158,11,0.15)',  color: '#f59e0b', label: 'Paused' },
    unreachable: { bg: 'rgba(145,145,145,0.1)',  color: 'var(--muted)', label: 'Unreachable' },
  };
  const s = map[status] || map.unreachable;
  return `<span style="font-size:10px;font-weight:600;padding:3px 8px;border-radius:4px;background:${s.bg};color:${s.color}">${s.label}</span>`;
}

function _renderHistory(peers) {
  const body = document.getElementById('fed-history-body');
  if (!body) return;

  // Collect history from all peers
  const allHistory = [];
  peers.forEach(p => {
    if (p.sync_history && Array.isArray(p.sync_history)) {
      p.sync_history.forEach(h => {
        allHistory.push({ ...h, peer_name: p.name || 'Unknown' });
      });
    }
  });

  // Sort by time descending
  allHistory.sort((a, b) => {
    const ta = new Date(a.synced_at || a.timestamp || a.time || 0).getTime();
    const tb = new Date(b.synced_at || b.timestamp || b.time || 0).getTime();
    return tb - ta;
  });

  if (!allHistory.length) {
    setTileEmpty('fed-history', {
      icon: 'fa-solid fa-clock-rotate-left',
      title: 'No sync activity',
      description: 'Sync events will appear here once peers begin exchanging data.',
    });
    return;
  }

  const rows = allHistory.slice(0, 50);

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Peer</th>
            <th>Direction</th>
            <th>Status</th>
            <th>Latency</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          ${rows.map(h => {
            const dir = h.direction || 'push';
            const dirIcon = dir === 'pull'
              ? '<i class="fa-solid fa-arrow-down" style="color:#3b82f6;margin-right:4px"></i>'
              : '<i class="fa-solid fa-arrow-up" style="color:#8b5cf6;margin-right:4px"></i>';
            const statusColor = (h.status || '').toLowerCase() === 'success' ? '#10b981'
              : (h.status || '').toLowerCase() === 'error' ? '#ef4444' : 'var(--muted)';
            const lat = h.latency_ms != null ? `${h.latency_ms}ms` : '\u2014';
            const time = h.synced_at || h.timestamp || h.time;
            return `<tr>
              <td style="font-weight:500;color:var(--text)">${esc(h.peer_name)}</td>
              <td>${dirIcon}${esc(dir)}</td>
              <td><span style="color:${statusColor};font-weight:600">${esc(h.status || 'unknown')}</span></td>
              <td style="font-family:var(--mono)">${lat}</td>
              <td style="color:var(--muted)">${time ? timeSince(time) : '\u2014'}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

// ── Actions ──────────────────────────────────────────────────────────────────

/**
 * POST /api/v1/admin/federation/peers/{id}/test.
 * Returns {reachable, latency_ms, peer_version, error}. The endpoint answers
 * HTTP 200 even when the peer is down, so success means `reachable === true`,
 * not just a 2xx status.
 */
async function _probePeer(peerId) {
  try {
    const resp = await rawFetch(`/api/v1/admin/federation/peers/${encodeURIComponent(peerId)}/test`, {
      method: 'POST',
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      return { reachable: false, error: data.detail || `HTTP ${resp.status}` };
    }
    return data;
  } catch (err) {
    return { reachable: false, error: err.message };
  }
}

function _describeProbe(probe) {
  if (probe.reachable) {
    const ms = probe.latency_ms != null ? ` in ${probe.latency_ms} ms` : '';
    const ver = probe.peer_version ? `, version ${probe.peer_version}` : '';
    return `Peer reachable${ms}${ver}.`;
  }
  return `Peer unreachable${probe.error ? ': ' + probe.error : ''}.`;
}

async function _testPeer(peerId, btn) {
  if (_destroyed || !peerId) return;

  const origHTML = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';

  try {
    const probe = await _probePeer(peerId);

    if (_destroyed) return;

    const card = btn.closest('.fed-peer-card');
    if (!card) return;
    card.title = _describeProbe(probe);

    if (probe.reachable) {
      card.style.transition = 'background 0.3s ease';
      card.style.background = 'rgba(16,185,129,0.08)';
      setTimeout(() => { card.style.background = ''; }, 1500);
    } else {
      card.style.transition = 'background 0.3s ease';
      card.style.background = 'rgba(239,68,68,0.08)';
      setTimeout(() => { card.style.background = ''; }, 1500);
    }
  } catch (err) {
    if (_destroyed) return;
    const card = btn.closest('.fed-peer-card');
    if (card) {
      card.style.transition = 'background 0.3s ease';
      card.style.background = 'rgba(239,68,68,0.08)';
      setTimeout(() => { card.style.background = ''; }, 1500);
    }
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHTML;
  }
}

async function _togglePausePeer(peerId, currentStatus, btn) {
  if (_destroyed || !peerId) return;

  const isPaused = currentStatus === 'paused';
  const action = isPaused ? 'resume' : 'pause';

  const origHTML = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';

  try {
    const resp = await rawFetch(`/api/v1/admin/federation/peers/${encodeURIComponent(peerId)}/${action}`, {
      method: 'POST',
    });

    if (_destroyed) return;

    if (resp.ok) {
      // Reload data to reflect new state
      await _loadData();
    }
  } catch (err) {
    if (_destroyed) return;
    console.warn(`Failed to ${action} peer:`, err);
  } finally {
    if (!_destroyed && btn) {
      btn.disabled = false;
      btn.innerHTML = origHTML;
    }
  }
}

async function _deletePeer(peerId) {
  if (_destroyed || !peerId) return;

  try {
    const resp = await rawFetch(`/api/v1/admin/federation/peers/${encodeURIComponent(peerId)}`, {
      method: 'DELETE',
    });

    if (_destroyed) return;

    if (resp.ok) {
      await _loadData();
    }
  } catch (err) {
    if (_destroyed) return;
    console.warn('Failed to delete peer:', err);
  }
}

// ── Add Peer Modal ───────────────────────────────────────────────────────────

function _showAddPeerModal() {
  openModal({
    title: 'Add Federation Peer',
    maxWidth: '480px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
          <div>
            <label style="display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:4px">Peer Name</label>
            <input type="text" id="fed-add-name" class="ds-input" placeholder="e.g. US-West Production" style="width:100%">
          </div>
          <div>
            <label style="display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:4px">Peer URL</label>
            <input type="text" id="fed-add-url" class="ds-input" placeholder="https://modus.example.com" style="width:100%">
          </div>
          <div>
            <label style="display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:4px">API Key</label>
            <input type="password" id="fed-add-key" class="ds-input" placeholder="Peer API key" style="width:100%">
          </div>
          <div style="font-size:11px;color:var(--muted)">The connection is tested right after the peer is saved, and you can re-test it any time from the peer card.</div>
          <div id="fed-add-result" style="display:none;font-size:12px;padding:8px 12px;border-radius:6px;margin-top:2px"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:4px">
            <button class="ds-btn ds-btn-sm" id="fed-save-peer-btn" style="background:var(--accent);color:var(--bg);font-weight:600">
              <i class="fa-solid fa-plus" style="margin-right:4px"></i>Save
            </button>
          </div>
        </div>
      `;

      // Save peer
      body.querySelector('#fed-save-peer-btn').addEventListener('click', async () => {
        const name = body.querySelector('#fed-add-name').value.trim();
        const peer_url = body.querySelector('#fed-add-url').value.trim();
        const api_key = body.querySelector('#fed-add-key').value.trim();
        const resultEl = body.querySelector('#fed-add-result');

        if (!name || !peer_url) {
          resultEl.style.display = 'block';
          resultEl.style.background = 'rgba(239,68,68,0.1)';
          resultEl.style.color = '#ef4444';
          resultEl.textContent = 'Name and URL are required.';
          return;
        }

        const saveBtn = body.querySelector('#fed-save-peer-btn');
        const origHTML = saveBtn.innerHTML;
        saveBtn.disabled = true;
        saveBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin" style="margin-right:4px"></i>Saving...';

        try {
          const resp = await rawFetch('/api/v1/admin/federation/peers', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, peer_url, api_key }),
          });

          if (resp.ok) {
            const result = await resp.json().catch(() => ({}));
            resultEl.style.display = 'block';
            resultEl.style.background = 'rgba(16,185,129,0.1)';
            resultEl.style.color = '#10b981';

            if (result.api_key) {
              resultEl.innerHTML = `<i class="fa-solid fa-check" style="margin-right:4px"></i>Peer added successfully!<br><span style="font-family:var(--mono);font-size:11px;margin-top:4px;display:block">API Key: <strong>${esc(result.api_key)}</strong></span><span style="font-size:10px;color:var(--muted);display:block;margin-top:2px">Save this key \u2014 it won't be shown again.</span>`;
            } else {
              resultEl.innerHTML = '<i class="fa-solid fa-check" style="margin-right:4px"></i>Peer added successfully!';
            }

            // Probe the new peer (POST /peers/{id}/test) and report the outcome.
            if (result.id) {
              const probe = await _probePeer(result.id);
              resultEl.insertAdjacentHTML('beforeend', `<div style="margin-top:6px;color:${probe.reachable ? '#10b981' : '#f59e0b'}">${esc(_describeProbe(probe))}</div>`);
            }

            // Reload data after short delay
            setTimeout(async () => {
              if (!_destroyed) await _loadData();
            }, 1000);
          } else {
            const err = await resp.json().catch(() => ({}));
            resultEl.style.display = 'block';
            resultEl.style.background = 'rgba(239,68,68,0.1)';
            resultEl.style.color = '#ef4444';
            resultEl.textContent = err.detail || `Failed to add peer (HTTP ${resp.status})`;
          }
        } catch (err) {
          resultEl.style.display = 'block';
          resultEl.style.background = 'rgba(239,68,68,0.1)';
          resultEl.style.color = '#ef4444';
          resultEl.textContent = `Error: ${err.message}`;
        } finally {
          saveBtn.disabled = false;
          saveBtn.innerHTML = origHTML;
        }
      });
    },
  });
}

// ── Peer Detail Modal ────────────────────────────────────────────────────────

function _showPeerDetailModal(peer) {
  const status = (peer.status || 'unreachable').toLowerCase();
  const statusInfo = {
    connected:   { color: '#10b981', label: 'Connected' },
    error:       { color: '#ef4444', label: 'Error' },
    paused:      { color: '#f59e0b', label: 'Paused' },
    unreachable: { color: 'var(--muted)', label: 'Unreachable' },
  };
  const si = statusInfo[status] || statusInfo.unreachable;
  const isPaused = status === 'paused';
  const peerUrl = peer.peer_url || peer.url || '\u2014';
  const keyPrefix = peer.api_key_prefix || peer.key_prefix || '\u2014';
  const team = peer.team || peer.team_slug || '\u2014';
  const history = peer.sync_history || [];

  const modalId = openModal({
    title: `${esc(peer.name || 'Peer')}`,
    maxWidth: '600px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:16px;padding:4px 0">
          <!-- Status badge -->
          <div style="display:flex;align-items:center;gap:8px">
            <span style="width:10px;height:10px;border-radius:50%;background:${si.color};display:inline-block"></span>
            <span style="font-weight:600;color:${si.color}">${si.label}</span>
          </div>

          <!-- Config section -->
          <div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12px">
            <span style="color:var(--muted)">Name</span>
            <span style="font-weight:600;color:var(--text)">${esc(peer.name || '\u2014')}</span>
            <span style="color:var(--muted)">URL</span>
            <span style="font-family:var(--mono);color:var(--text);word-break:break-all">${esc(peerUrl)}</span>
            <span style="color:var(--muted)">Key Prefix</span>
            <span style="font-family:var(--mono);color:var(--text)">${esc(keyPrefix)}</span>
            <span style="color:var(--muted)">Team</span>
            <span style="color:var(--text)">${esc(team)}</span>
            <span style="color:var(--muted)">Last Heartbeat</span>
            <span style="font-family:var(--mono);font-size:11px;color:var(--text)">${peer.last_heartbeat ? timeSince(peer.last_heartbeat) : 'Never'}</span>
            <span style="color:var(--muted)">Latency</span>
            <span style="font-family:var(--mono);color:var(--text)">${peer.latency_ms != null ? `${peer.latency_ms}ms` : '\u2014'}</span>
          </div>

          <!-- Actions -->
          <div style="display:flex;gap:8px;border-top:1px solid var(--border);padding-top:12px">
            <button class="ds-btn ds-btn-ghost ds-btn-sm" id="fed-detail-test">
              <i class="fa-solid fa-bolt" style="margin-right:4px"></i>Test
            </button>
            <button class="ds-btn ds-btn-ghost ds-btn-sm" id="fed-detail-toggle">
              <i class="fa-solid ${isPaused ? 'fa-play' : 'fa-pause'}" style="margin-right:4px"></i>${isPaused ? 'Resume' : 'Pause'}
            </button>
            <button class="ds-btn ds-btn-ghost ds-btn-sm" id="fed-detail-delete" style="color:#ef4444;margin-left:auto">
              <i class="fa-solid fa-trash" style="margin-right:4px"></i>Delete
            </button>
          </div>

          <!-- Delete confirmation -->
          <div id="fed-delete-confirm" style="display:none;padding:10px 12px;border-radius:6px;background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.2)">
            <div style="font-size:12px;font-weight:600;color:#ef4444;margin-bottom:8px">Are you sure you want to delete this peer?</div>
            <div style="display:flex;gap:8px">
              <button class="ds-btn ds-btn-sm" id="fed-delete-yes" style="background:#ef4444;color:#fff;font-weight:600">Yes, Delete</button>
              <button class="ds-btn ds-btn-ghost ds-btn-sm" id="fed-delete-no">Cancel</button>
            </div>
          </div>

          <!-- Sync history -->
          ${history.length > 0 ? `
          <div style="border-top:1px solid var(--border);padding-top:12px">
            <div style="font-size:12px;font-weight:600;color:var(--text);margin-bottom:8px">Sync History (Last 20)</div>
            <div style="overflow-x:auto;max-height:200px;overflow-y:auto">
              <table class="ds-table" style="width:100%;font-size:11px">
                <thead>
                  <tr>
                    <th>Direction</th>
                    <th>Status</th>
                    <th>Latency</th>
                    <th>Time</th>
                  </tr>
                </thead>
                <tbody>
                  ${history.slice(0, 20).map(h => {
                    const dir = h.direction || 'push';
                    const dirIcon = dir === 'pull'
                      ? '<i class="fa-solid fa-arrow-down" style="color:#3b82f6;margin-right:3px"></i>'
                      : '<i class="fa-solid fa-arrow-up" style="color:#8b5cf6;margin-right:3px"></i>';
                    const statusColor = (h.status || '').toLowerCase() === 'success' ? '#10b981'
                      : (h.status || '').toLowerCase() === 'error' ? '#ef4444' : 'var(--muted)';
                    const lat = h.latency_ms != null ? `${h.latency_ms}ms` : '\u2014';
                    const time = h.synced_at || h.timestamp || h.time;
                    return `<tr>
                      <td>${dirIcon}${esc(dir)}</td>
                      <td><span style="color:${statusColor};font-weight:600">${esc(h.status || 'unknown')}</span></td>
                      <td style="font-family:var(--mono)">${lat}</td>
                      <td style="color:var(--muted)">${time ? timeSince(time) : '\u2014'}</td>
                    </tr>`;
                  }).join('')}
                </tbody>
              </table>
            </div>
          </div>` : ''}
        </div>
      `;

      // Wire up action buttons
      body.querySelector('#fed-detail-test').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        const origHTML = btn.innerHTML;
        btn.disabled = true;
        btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i>';

        try {
          const probe = await _probePeer(peer.id);
          btn.title = _describeProbe(probe);
          btn.innerHTML = probe.reachable
            ? `<i class="fa-solid fa-check" style="color:#10b981"></i> OK${probe.latency_ms != null ? ' (' + probe.latency_ms + ' ms)' : ''}`
            : '<i class="fa-solid fa-xmark" style="color:#ef4444"></i> Unreachable';
        } catch {
          btn.innerHTML = '<i class="fa-solid fa-xmark" style="color:#ef4444"></i> Error';
        }

        setTimeout(() => {
          btn.disabled = false;
          btn.innerHTML = origHTML;
        }, 2000);
      });

      body.querySelector('#fed-detail-toggle').addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        const action = isPaused ? 'resume' : 'pause';
        btn.disabled = true;

        try {
          await rawFetch(`/api/v1/admin/federation/peers/${encodeURIComponent(peer.id)}/${action}`, {
            method: 'POST',
          });
          closeModal(modalId);
          if (!_destroyed) await _loadData();
        } catch {
          btn.disabled = false;
        }
      });

      const deleteBtn = body.querySelector('#fed-detail-delete');
      const confirmEl = body.querySelector('#fed-delete-confirm');

      deleteBtn.addEventListener('click', () => {
        confirmEl.style.display = 'block';
      });

      body.querySelector('#fed-delete-no').addEventListener('click', () => {
        confirmEl.style.display = 'none';
      });

      body.querySelector('#fed-delete-yes').addEventListener('click', async () => {
        closeModal(modalId);
        await _deletePeer(peer.id);
      });
    },
  });
}

// ── Table filter ─────────────────────────────────────────────────────────────

function _filterTable(bodyId, query) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  const q = (query || '').toLowerCase();
  const rows = body.querySelectorAll('tbody tr');
  rows.forEach(row => {
    const text = row.textContent.toLowerCase();
    row.style.display = text.includes(q) ? '' : 'none';
  });
}
