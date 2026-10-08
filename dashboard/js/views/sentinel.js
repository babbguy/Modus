/**
 * Modus Dashboard v2 — Sentinel View
 * Renders the security sentinel dashboard with 4 Gridstack tiles:
 * KPIs, Threats table, trajectory receipt stats, and PQC compliance.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError, setTileMeta } from '../tile.js';
import { rawFetch, esc } from '../api.js';
import { fmtNum } from '../format.js';
import { get } from '../state.js';
import { openModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Sentinel view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'sentinel');

  const tiles = [
    _createKpiTile(),
    _createThreatsTile(),
    _createZkTile(),
    _createPqcTile(),
  ];

  const layout = loadLayout('sentinel', LAYOUTS.sentinel);
  addTiles(_grid, tiles, layout);

  await loadData();
}

/**
 * Tear down the Sentinel view.
 */
export function destroy() {
  _destroyed = true;
  _grid = null;
  _container = null;
}

/**
 * Re-fetch this view's data. Called by the Settings -> Auto-refresh timer
 * (js/auto-refresh.js); a no-op once the view has been destroyed.
 */
export function refresh() {
  return _destroyed ? Promise.resolve() : loadData();
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createKpiTile() {
  const tile = createTile({
    id: 'sentinel-kpis',
    title: '',
    className: 'kpi-row',
  });
  const header = tile.querySelector('.gs-tile-header');
  if (header) header.style.display = 'none';
  return tile;
}

function _createThreatsTile() {
  return createTile({
    id: 'sentinel-threats',
    title: 'Recent Threats',
    icon: 'fa-solid fa-shield-virus',
    iconBg: 'rgba(255,77,77,0.1)',
    iconColor: '#ff4d4d',
    filterable: true,
    filterPlaceholder: 'Filter threats\u2026',
    onFilter: (q) => _filterTable('sentinel-threats-body', q),
  });
}

function _createZkTile() {
  return createTile({
    id: 'sentinel-zk',
    title: 'Trajectory Receipt Coverage',
    icon: 'fa-solid fa-lock',
    iconBg: 'rgba(0,229,160,0.1)',
    iconColor: 'var(--accent)',
  });
}

function _createPqcTile() {
  return createTile({
    id: 'sentinel-pqc',
    title: 'PQC Compliance',
    icon: 'fa-solid fa-atom',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function loadData() {
  if (_destroyed) return;

  setTileLoading('sentinel-kpis', 'cards');
  setTileLoading('sentinel-threats', 'table');
  setTileLoading('sentinel-zk', 'cards');
  setTileLoading('sentinel-pqc', 'cards');

  try {
    const [stats, threats, pqc, zk] = await Promise.all([
      rawFetch('/api/v1/sentinel/stats').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/sentinel/threats?limit=20').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/compliance/pqc/score').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/compliance/zk-proofs/stats').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);

    if (_destroyed) return;

    _renderKpis(stats);
    _renderThreats(threats);
    _renderZk(zk);
    _renderPqc(pqc);
  } catch (err) {
    if (_destroyed) return;
    console.error('[sentinel] data fetch failed:', err);
    _setAllTilesError();
  }
}

// ── Tile renderers ───────────────────────────────────────────────────────────

function _renderKpis(stats) {
  const body = document.getElementById('sentinel-kpis-body');
  if (!body) return;

  if (!stats) {
    setTileEmpty('sentinel-kpis', { icon: 'fa-solid fa-shield', title: 'No sentinel data' });
    return;
  }

  const total = stats.total_threats || 0;
  const critical = stats.critical_threats || 0;
  const blocked = stats.blocked_threats || 0;
  const avgConf = stats.avg_confidence ? (stats.avg_confidence * 100).toFixed(1) + '%' : '\u2014';

  const sev = stats.severity_distribution || {};
  const riskScore = critical > 5 ? 'High' : critical > 0 ? 'Medium' : 'Low';
  const riskColor = critical > 5 ? 'var(--danger)' : critical > 0 ? 'var(--warn)' : 'var(--accent)';

  body.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Threats Detected</div>
      <div class="kpi-value">${fmtNum(total)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Blocked</div>
      <div class="kpi-value" style="color:var(--accent)">${fmtNum(blocked)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Critical</div>
      <div class="kpi-value" style="color:${critical > 0 ? 'var(--danger)' : 'var(--muted)'}">${fmtNum(critical)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Avg Confidence</div>
      <div class="kpi-value">${avgConf}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Risk Score</div>
      <div class="kpi-value" style="color:${riskColor}">${riskScore}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Severity Dist.</div>
      <div style="display:flex;gap:8px;font-size:10px;margin-top:4px">
        <span style="color:var(--danger)">C:${sev.critical || 0}</span>
        <span style="color:var(--warn)">H:${sev.high || 0}</span>
        <span style="color:#3b82f6">M:${sev.medium || 0}</span>
        <span style="color:var(--accent)">L:${sev.low || 0}</span>
      </div>
    </div>
  `;
}

function _renderThreats(threats) {
  const body = document.getElementById('sentinel-threats-body');
  if (!body) return;

  const items = threats ? (threats.items || threats) : [];

  if (!Array.isArray(items) || items.length === 0) {
    setTileEmpty('sentinel-threats', {
      icon: 'fa-solid fa-shield-virus',
      title: 'No threats detected',
      description: 'All clear \u2014 no security threats in this period.',
    });
    return;
  }

  setTileMeta('sentinel-threats', `${items.length} threat${items.length !== 1 ? 's' : ''}`);

  body.innerHTML = `
    <div style="overflow:auto;max-height:380px">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead style="position:sticky;top:0;background:var(--surface);z-index:1">
          <tr>
            <th>Session</th>
            <th>Type</th>
            <th>Severity</th>
            <th>Confidence</th>
            <th>Action</th>
            <th>Timestamp</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((t, idx) => {
            const sevClass = t.severity === 'critical' ? 'danger' : t.severity === 'high' ? 'warning' : t.severity === 'medium' ? 'info' : 'success';
            const actionClass = t.action === 'blocked' ? 'danger' : t.action === 'flagged' ? 'warning' : 'neutral';
            const conf = t.confidence ? (t.confidence * 100).toFixed(1) + '%' : '\u2014';
            const ts = t.timestamp ? new Date(t.timestamp).toLocaleString() : '\u2014';
            return `<tr class="threat-row" data-threat-idx="${idx}" style="cursor:pointer">
              <td style="font-family:var(--mono);font-size:11px">${esc(t.session_id || '\u2014')}</td>
              <td><span class="ds-badge-neutral">${esc(t.threat_type || '\u2014')}</span></td>
              <td><span class="ds-badge-${sevClass}">${esc(t.severity || '\u2014')}</span></td>
              <td style="font-family:var(--mono);font-size:11px">${conf}</td>
              <td><span class="ds-badge-${actionClass}">${esc(t.action || '\u2014')}</span></td>
              <td style="font-family:var(--mono);font-size:11px;white-space:nowrap">${ts}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  // Attach click handlers for threat detail modal
  body.querySelectorAll('.threat-row').forEach(row => {
    row.addEventListener('click', () => {
      const idx = parseInt(row.getAttribute('data-threat-idx'), 10);
      if (items[idx]) _showThreatModal(items[idx]);
    });
    row.addEventListener('mouseenter', () => { row.style.background = 'rgba(255,255,255,0.02)'; });
    row.addEventListener('mouseleave', () => { row.style.background = ''; });
  });
}

function _renderZk(zk) {
  const body = document.getElementById('sentinel-zk-body');
  if (!body) return;

  if (!zk) {
    setTileEmpty('sentinel-zk', {
      icon: 'fa-solid fa-lock',
      title: 'No ZK proof data',
      description: 'Zero-knowledge proof coverage stats will appear here.',
    });
    return;
  }

  const total = zk.total_proofs || 0;
  const verified = zk.verified || 0;
  const coverage = zk.coverage ? (zk.coverage * 100).toFixed(1) : '0.0';
  const coverageColor = parseFloat(coverage) >= 80 ? 'var(--accent)' : parseFloat(coverage) >= 50 ? 'var(--warn)' : 'var(--danger)';

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:16px;padding:16px">
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Total Proofs</div>
          <div class="kpi-value">${fmtNum(total)}</div>
        </div>
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Verified</div>
          <div class="kpi-value" style="color:var(--accent)">${fmtNum(verified)}</div>
        </div>
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Coverage</div>
          <div class="kpi-value" style="color:${coverageColor}">${coverage}%</div>
        </div>
      </div>
      <div>
        <div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden">
          <div style="width:${coverage}%;height:100%;background:${coverageColor};border-radius:3px;transition:width 0.5s ease"></div>
        </div>
      </div>
    </div>
  `;
}

function _renderPqc(pqc) {
  const body = document.getElementById('sentinel-pqc-body');
  if (!body) return;

  if (!pqc) {
    setTileEmpty('sentinel-pqc', {
      icon: 'fa-solid fa-atom',
      title: 'No PQC data',
      description: 'Post-quantum compliance assessment will appear here.',
    });
    return;
  }

  const rawScore = pqc.compliance_score ?? pqc.score ?? 0;
  const scoreNorm = rawScore > 1 ? rawScore / 100 : rawScore;
  const scorePct = Math.min(Math.round(scoreNorm * 100), 100);
  const algorithms = pqc.algorithms_count || pqc.pqc_algorithms || 0;
  const endpoints = pqc.protected_endpoints || 0;
  const scoreColor = scoreNorm >= 0.8 ? 'var(--accent)' : scoreNorm >= 0.5 ? 'var(--warn)' : 'var(--danger)';

  let statusText, statusColor;
  if (scoreNorm >= 0.8) { statusText = 'Compliant'; statusColor = 'var(--accent)'; }
  else if (scoreNorm >= 0.5) { statusText = 'Partial'; statusColor = 'var(--warn)'; }
  else { statusText = 'Non-Compliant'; statusColor = 'var(--danger)'; }

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:16px;padding:16px">
      <div style="display:flex;align-items:center;justify-content:space-between">
        <span style="font-size:13px;font-weight:600;color:var(--text)">PQC Status</span>
        <span style="background:${statusColor}20;color:${statusColor};padding:3px 10px;border-radius:4px;font-size:11px;font-weight:600">${statusText}</span>
      </div>
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Score</div>
          <div class="kpi-value" style="color:${scoreColor}">${scorePct}%</div>
        </div>
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Algorithms</div>
          <div class="kpi-value">${algorithms}</div>
        </div>
        <div class="kpi-card" style="flex:1;min-width:80px">
          <div class="kpi-label">Endpoints</div>
          <div class="kpi-value">${endpoints}</div>
        </div>
      </div>
      <div>
        <div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden">
          <div style="width:${scorePct}%;height:100%;background:${scoreColor};border-radius:3px;transition:width 0.5s ease"></div>
        </div>
      </div>
    </div>
  `;
}

// ── Threat detail modal ──────────────────────────────────────────────────────

function _showThreatModal(threat) {
  const sevColors = { critical: 'var(--danger)', high: 'var(--warn)', medium: '#3b82f6', low: 'var(--accent)' };
  const sevColor = sevColors[threat.severity] || 'var(--muted)';

  openModal({
    title: 'Threat Details',
    maxWidth: '480px',
    renderBody: (body) => {
      const conf = threat.confidence ? (threat.confidence * 100).toFixed(1) + '%' : '\u2014';
      const ts = threat.timestamp ? new Date(threat.timestamp).toLocaleString() : '\u2014';

      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:12px;padding:4px 0">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="display:flex;align-items:center;gap:8px">
              <span style="width:10px;height:10px;border-radius:50%;background:${sevColor};display:inline-block"></span>
              <span style="font-weight:600;font-size:14px;color:var(--text)">${esc(threat.threat_type || '\u2014')}</span>
            </span>
            <span style="background:${sevColor}20;color:${sevColor};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;text-transform:uppercase">${esc(threat.severity || 'unknown')}</span>
          </div>
          <div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12px">
            <span style="color:var(--muted)">Session</span>
            <span style="font-family:var(--mono);color:var(--text)">${esc(threat.session_id || '\u2014')}</span>
            <span style="color:var(--muted)">Confidence</span>
            <span style="font-family:var(--mono);color:var(--text)">${conf}</span>
            <span style="color:var(--muted)">Action Taken</span>
            <span style="color:var(--text)">${esc(threat.action || '\u2014')}</span>
            <span style="color:var(--muted)">Timestamp</span>
            <span style="font-family:var(--mono);font-size:11px;color:var(--text)">${ts}</span>
            ${threat.app_id ? `<span style="color:var(--muted)">App</span><span style="color:var(--text)">${esc(threat.app_id)}</span>` : ''}
            ${threat.description ? `<span style="color:var(--muted)">Details</span><span style="color:var(--text)">${esc(threat.description)}</span>` : ''}
          </div>
        </div>
      `;
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

// ── Error helper ─────────────────────────────────────────────────────────────

function _setAllTilesError() {
  const ids = ['sentinel-kpis', 'sentinel-threats', 'sentinel-zk', 'sentinel-pqc'];
  ids.forEach(id => setTileError(id, 'Failed to load sentinel data', () => loadData()));
}
