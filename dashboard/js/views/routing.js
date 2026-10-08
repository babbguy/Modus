/**
 * Modus Dashboard v2 — Routing View
 * Renders the intelligent routing dashboard with 5 Gridstack tiles:
 * KPIs, Savings Over Time chart, Phase Distribution donut,
 * Fingerprint Table, and Exclusions Table.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError, setTileMeta } from '../tile.js';
import { apiFetch, rawFetch, esc } from '../api.js';
import { fmtCost, fmtNum } from '../format.js';
import { get, subscribe, unsubscribe } from '../state.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _savingsChart = null;
let _phaseChart = null;
let _grid = null;
let _container = null;
let _destroyed = false;
let _savingsResizeHandler = null;

// ── State subscription handler ───────────────────────────────────────────────

function _onDaysChange() {
  if (!_destroyed) loadData();
}

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Routing view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'routing');

  const tiles = [
    _createKpiTile(),
    _createSavingsChartTile(),
    _createPhaseDonutTile(),
    _createFingerprintsTile(),
    _createExclusionsTile(),
  ];

  const layout = loadLayout('routing', LAYOUTS.routing);
  addTiles(_grid, tiles, layout);

  subscribe('currentDays', _onDaysChange);

  await loadData();
}

/**
 * Tear down the Routing view, cleaning up charts, subscriptions, and DOM.
 */
export function destroy() {
  _destroyed = true;

  if (_savingsChart) {
    try { _savingsChart.destroy(); } catch (_) { /* already destroyed */ }
    _savingsChart = null;
  }
  if (_phaseChart) {
    try { _phaseChart.destroy(); } catch (_) { /* already destroyed */ }
    _phaseChart = null;
  }

  if (_savingsResizeHandler) {
    const body = document.getElementById('rt-savings-chart-body');
    if (body) body.removeEventListener('tile-resize', _savingsResizeHandler);
    _savingsResizeHandler = null;
  }

  unsubscribe('currentDays', _onDaysChange);

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
    id: 'rt-kpis',
    title: '',
    className: 'kpi-row',
  });
  const header = tile.querySelector('.gs-tile-header');
  if (header) header.style.display = 'none';
  return tile;
}

function _createSavingsChartTile() {
  return createTile({
    id: 'rt-savings-chart',
    title: 'Savings Over Time',
    icon: 'fa-solid fa-chart-area',
    iconBg: 'rgba(0,229,160,0.1)',
    iconColor: '#00e5a0',
    meta: '30d',
  });
}

function _createPhaseDonutTile() {
  return createTile({
    id: 'rt-phase-donut',
    title: 'Phase Distribution',
    icon: 'fa-solid fa-chart-pie',
    iconBg: 'rgba(59,130,246,0.1)',
    iconColor: '#3b82f6',
  });
}

function _createFingerprintsTile() {
  return createTile({
    id: 'rt-fingerprints',
    title: 'Routing Fingerprints',
    icon: 'fa-solid fa-fingerprint',
    iconBg: 'rgba(168,85,247,0.1)',
    iconColor: '#a855f7',
    filterable: true,
    filterPlaceholder: 'Filter fingerprints\u2026',
    onFilter: (q) => _filterTable('rt-fingerprints-body', q),
  });
}

function _createExclusionsTile() {
  return createTile({
    id: 'rt-exclusions',
    title: 'Excluded Fingerprints',
    icon: 'fa-solid fa-ban',
    iconBg: 'rgba(136,136,136,0.1)',
    iconColor: '#888',
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function loadData() {
  if (_destroyed) return;

  setTileLoading('rt-kpis', 'cards');
  setTileLoading('rt-savings-chart', 'chart');
  setTileLoading('rt-phase-donut', 'chart');
  setTileLoading('rt-fingerprints', 'table');
  setTileLoading('rt-exclusions', 'table');

  try {
    const base = get('apiBase');
    const [summaryResp, fingerprintsResp, savingsResp] = await Promise.all([
      rawFetch('/api/v1/routing/summary').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/routing/fingerprints?limit=100').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/routing/savings-over-time?days=30').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);

    if (_destroyed) return;

    const summary = summaryResp;
    const fingerprints = Array.isArray(fingerprintsResp) ? fingerprintsResp : [];
    const savings = Array.isArray(savingsResp) ? savingsResp : [];

    if (summary) {
      _renderKpis(summary);
      _renderPhaseDonut(summary);
    } else {
      setTileEmpty('rt-kpis', { icon: 'fa-solid fa-gauge', title: 'No routing data' });
      setTileEmpty('rt-phase-donut', { icon: 'fa-solid fa-chart-pie', title: 'No phase data' });
    }

    _renderSavingsChart(savings);
    _renderFingerprints(fingerprints);
  } catch (err) {
    if (_destroyed) return;
    console.error('[routing] data fetch failed:', err);
    _setAllTilesError();
  }
}

// ── Tile renderers ───────────────────────────────────────────────────────────

function _renderKpis(s) {
  const body = document.getElementById('rt-kpis-body');
  if (!body) return;

  if (!s) {
    setTileEmpty('rt-kpis', { icon: 'fa-solid fa-gauge', title: 'No routing data' });
    return;
  }

  const costSaved = fmtCost(s.cost_saved_30d_usd || 0);
  const callsRouted = fmtNum(s.total_routed_calls || 0);
  const avgSavings = s.avg_routing_confidence != null
    ? (s.avg_routing_confidence * 100).toFixed(1) + '%'
    : '\u2014';
  const escRate = s.escalation_rate != null
    ? (s.escalation_rate * 100).toFixed(1) + '%'
    : '\u2014';
  const phase = s.routing_active != null ? `${s.routing_active} active` : '\u2014';

  body.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Cost Saved 30D</div>
      <div class="kpi-value" style="color:var(--accent)">${costSaved}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Calls Routed</div>
      <div class="kpi-value">${callsRouted}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Avg Confidence</div>
      <div class="kpi-value">${avgSavings}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Escalation Rate</div>
      <div class="kpi-value">${escRate}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Current Phase</div>
      <div class="kpi-value">${phase}</div>
    </div>
  `;
}

function _renderSavingsChart(data) {
  const body = document.getElementById('rt-savings-chart-body');
  if (!body) return;

  // Destroy previous chart
  if (_savingsChart) {
    try { _savingsChart.destroy(); } catch (_) { /* noop */ }
    _savingsChart = null;
  }
  if (_savingsResizeHandler) {
    body.removeEventListener('tile-resize', _savingsResizeHandler);
    _savingsResizeHandler = null;
  }

  if (!data || !Array.isArray(data) || data.length === 0) {
    setTileEmpty('rt-savings-chart', {
      icon: 'fa-solid fa-chart-area',
      title: 'No savings data',
      description: 'Routing savings will appear here once routing is active.',
    });
    return;
  }

  const labels = data.map(d => (d.date || '').slice(5));
  const values = data.map(d => parseFloat(d.cost_saved_usd || 0));

  const canvas = document.createElement('canvas');
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  body.innerHTML = '';
  body.style.padding = '8px 12px 12px';
  body.appendChild(canvas);

  if (typeof ModusCharts !== 'undefined') {
    _savingsChart = ModusCharts.area(canvas, {
      labels,
      data: values,
      label: 'Cost Saved ($)',
      // zoom: true, // disabled — Hammer.js compat issue
    });
  } else if (typeof Chart !== 'undefined') {
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createLinearGradient(0, 0, 0, 220);
    gradient.addColorStop(0, 'rgba(0,229,160,0.25)');
    gradient.addColorStop(1, 'rgba(0,229,160,0.0)');
    _savingsChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Cost Saved ($)',
          data: values,
          borderColor: '#9FC131',
          backgroundColor: gradient,
          borderWidth: 2,
          fill: true,
          tension: 0.4,
          pointRadius: 0,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false } },
          y: { grid: { color: 'rgba(255,255,255,0.04)' } },
        },
      },
    });
  }

  _savingsResizeHandler = () => {
    if (_savingsChart && typeof _savingsChart.resize === 'function') {
      _savingsChart.resize();
    }
  };
  body.addEventListener('tile-resize', _savingsResizeHandler);
}

function _renderPhaseDonut(s) {
  const body = document.getElementById('rt-phase-donut-body');
  if (!body) return;

  // Destroy previous chart
  if (_phaseChart) {
    try { _phaseChart.destroy(); } catch (_) { /* noop */ }
    _phaseChart = null;
  }

  const phases = [
    { label: 'Routing', count: s.routing_active || 0, color: '#9FC131' },
    { label: 'Observe', count: s.routing_observe || 0, color: '#3b82f6' },
    { label: 'Calibrating', count: Math.max(0, (s.total_fingerprints || 0) - (s.routing_active || 0) - (s.routing_observe || 0) - (s.routing_excluded || 0) - (s.drift_flagged || 0)), color: '#f59e0b' },
    { label: 'Drift Flagged', count: s.drift_flagged || 0, color: '#ff4d4d' },
    { label: 'Excluded', count: s.routing_excluded || 0, color: '#888' },
  ].filter(p => p.count > 0);

  if (phases.length === 0) {
    setTileEmpty('rt-phase-donut', {
      icon: 'fa-solid fa-chart-pie',
      title: 'No fingerprints',
      description: 'Phase data will appear once fingerprints are created.',
    });
    return;
  }

  const totalFp = phases.reduce((sum, p) => sum + p.count, 0);

  // Build chart + legend container
  body.innerHTML = `
    <div style="display:flex;align-items:center;gap:20px;padding:12px;height:100%">
      <div style="flex:1;display:flex;justify-content:center;align-items:center">
        <canvas id="rt-phase-canvas" width="180" height="180"></canvas>
      </div>
      <div id="rt-phase-legend" style="display:flex;flex-direction:column;gap:6px;min-width:120px"></div>
    </div>
  `;

  const ctx = document.getElementById('rt-phase-canvas');
  if (!ctx) return;

  if (typeof ModusCharts !== 'undefined') {
    _phaseChart = ModusCharts.donut(ctx, {
      labels: phases.map(p => p.label),
      data: phases.map(p => p.count),
      colors: phases.map(p => p.color),
      centerText: totalFp.toString(),
      centerSub: 'Fingerprints',
    });
  } else if (typeof Chart !== 'undefined') {
    _phaseChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: phases.map(p => p.label),
        datasets: [{
          data: phases.map(p => p.count),
          backgroundColor: phases.map(p => p.color),
          borderWidth: 0,
        }],
      },
      options: { responsive: false, cutout: '60%', plugins: { legend: { display: false } } },
    });
  }

  const legendEl = document.getElementById('rt-phase-legend');
  if (legendEl) {
    legendEl.innerHTML = phases.map(p => `
      <div style="display:flex;align-items:center;gap:8px;font-size:12px">
        <div style="width:8px;height:8px;border-radius:50%;background:${p.color};flex-shrink:0"></div>
        <span style="color:var(--text)">${esc(p.label)}</span>
        <span style="margin-left:auto;font-family:var(--mono);color:var(--muted)">${p.count}</span>
      </div>
    `).join('');
  }
}

function _renderFingerprints(fps) {
  const body = document.getElementById('rt-fingerprints-body');
  if (!body) return;

  const active = Array.isArray(fps) ? fps.filter(f => f.phase !== 'excluded') : [];
  const excluded = Array.isArray(fps) ? fps.filter(f => f.phase === 'excluded') : [];

  if (active.length === 0) {
    setTileEmpty('rt-fingerprints', {
      icon: 'fa-solid fa-fingerprint',
      title: 'No routing fingerprints',
      description: 'Fingerprints will appear once routing observes traffic patterns.',
    });
    _renderExclusions(excluded);
    return;
  }

  setTileMeta('rt-fingerprints', `${active.length} fingerprint${active.length !== 1 ? 's' : ''}`);

  body.innerHTML = `
    <div style="overflow:auto;max-height:380px">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead style="position:sticky;top:0;background:var(--surface);z-index:1">
          <tr>
            <th>Hash</th>
            <th>App</th>
            <th>Phase</th>
            <th>Confidence</th>
            <th>Drift</th>
            <th style="text-align:right">Routed</th>
            <th style="text-align:right">Savings</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          ${active.map((fp, idx) => {
            const confPct = Math.round((fp.routing_confidence || 0) * 100);
            const saved = (fp.total_routed_calls || 0) > 0
              ? fmtCost(((fp.total_routed_calls - (fp.total_escalations || 0)) * 0.003).toFixed(2))
              : '$0';
            const isPaused = !fp.allow_routing;
            return `<tr data-fp-idx="${idx}">
              <td style="font-family:var(--mono);font-size:11px">${esc((fp.fingerprint_hash || '').slice(0, 8))}</td>
              <td>${esc(fp.app_id || '')}</td>
              <td><span class="ds-badge-${fp.phase === 'routing' ? 'success' : fp.phase === 'observe' ? 'info' : fp.phase === 'calibrating' ? 'warning' : 'neutral'}">${esc(fp.phase || '')}</span></td>
              <td>
                <div style="display:flex;align-items:center;gap:6px">
                  <div style="flex:1;height:4px;border-radius:2px;background:var(--border);overflow:hidden;max-width:60px">
                    <div style="width:${confPct}%;height:100%;background:var(--accent);border-radius:2px"></div>
                  </div>
                  <span style="font-family:var(--mono);font-size:11px">${confPct}%</span>
                </div>
              </td>
              <td style="font-family:var(--mono);font-size:11px">${(fp.drift_score || 0).toFixed(1)}</td>
              <td style="text-align:right;font-family:var(--mono);font-size:11px">${fmtNum(fp.total_routed_calls || 0)}</td>
              <td style="text-align:right;font-family:var(--mono);font-size:11px">${saved}</td>
              <td>
                <button class="ds-btn ds-btn-ghost ds-btn-sm fp-toggle-btn" data-hash="${esc(fp.fingerprint_hash || '')}" data-paused="${isPaused}">
                  ${isPaused ? 'Resume' : 'Pause'}
                </button>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  // Attach toggle handlers
  body.querySelectorAll('.fp-toggle-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const hash = btn.getAttribute('data-hash');
      const isPaused = btn.getAttribute('data-paused') === 'true';
      _toggleRouting(hash, isPaused);
    });
  });

  _renderExclusions(excluded);
}

function _renderExclusions(excluded) {
  const body = document.getElementById('rt-exclusions-body');
  if (!body) return;

  if (!excluded || excluded.length === 0) {
    setTileEmpty('rt-exclusions', {
      icon: 'fa-solid fa-ban',
      title: 'No exclusions',
      description: 'Excluded fingerprints will appear here.',
    });
    return;
  }

  setTileMeta('rt-exclusions', `${excluded.length} excluded`);

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Hash</th>
            <th>App</th>
            <th>Reason</th>
            <th>Agreement</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          ${excluded.map(fp => {
            const agr = fp.cheap_model_agreement_rate != null
              ? (fp.cheap_model_agreement_rate * 100).toFixed(1) + '%'
              : '\u2014';
            return `<tr>
              <td style="font-family:var(--mono);font-size:11px">${esc((fp.fingerprint_hash || '').slice(0, 8))}</td>
              <td>${esc(fp.app_id || '')}</td>
              <td style="color:var(--muted)">Agreement below threshold</td>
              <td style="font-family:var(--mono);font-size:11px">${agr}</td>
              <td>
                <button class="ds-btn ds-btn-ghost ds-btn-sm excl-reset-btn" data-hash="${esc(fp.fingerprint_hash || '')}">
                  Re-observe
                </button>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  body.querySelectorAll('.excl-reset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      _resetFingerprint(btn.getAttribute('data-hash'));
    });
  });
}

// ── Actions ──────────────────────────────────────────────────────────────────

async function _toggleRouting(hash, currentlyPaused) {
  try {
    const resp = await rawFetch(`/api/v1/routing/fingerprints/${hash}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ allow_routing: currentlyPaused }),
    });
    if (resp.ok) loadData();
  } catch (e) {
    console.error('[routing] toggle failed:', e);
  }
}

async function _resetFingerprint(hash) {
  try {
    const resp = await rawFetch(`/api/v1/routing/fingerprints/${hash}/reset`, {
      method: 'POST',
    });
    if (resp.ok) loadData();
  } catch (e) {
    console.error('[routing] reset failed:', e);
  }
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
  const ids = ['rt-kpis', 'rt-savings-chart', 'rt-phase-donut', 'rt-fingerprints', 'rt-exclusions'];
  ids.forEach(id => setTileError(id, 'Failed to load routing data', () => loadData()));
}
