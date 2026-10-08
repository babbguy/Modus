/**
 * Modus Dashboard v2 — Overview View
 * Renders the main Overview dashboard with 7 Gridstack tiles:
 * KPIs, Cost Over Time, Provider Breakdown, Top Apps, Top Models,
 * Recent Alerts, and Agent Status.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError, setTileMeta, exportActions } from '../tile.js';
import { apiFetch, rawFetch, esc, loadAppNames, appName } from '../api.js';
import { fmtCost, fmtTokens, fmtNum, fmtDate, fmtDateSmart, timeSince, providerColor } from '../format.js';
import { get, subscribe, unsubscribe } from '../state.js';
import { openModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _costChart = null;        // Chart.js instance for cost-over-time
let _grid = null;             // Gridstack instance
let _container = null;        // DOM container ref
let _destroyed = false;       // Guard against async renders after destroy
let _resizeHandler = null;    // tile-resize listener ref

// ── State subscription handler ───────────────────────────────────────────────

function _onDaysChange() {
  if (!_destroyed) loadData();
}

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Overview view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  // Initialize grid
  _grid = initGrid(container, 'overview');

  // Create tiles
  const tiles = [
    _createKpiTile(),
    _createCostChartTile(),
    _createProvidersTile(),
    _createTopAppsTile(),
    _createTopModelsTile(),
    _createAlertsTile(),
    _createAgentsTile(),
  ];

  // Load saved or default layout
  const layout = loadLayout('overview', LAYOUTS.overview);

  // Add tiles to grid
  addTiles(_grid, tiles, layout);

  // Subscribe to period changes
  subscribe('currentDays', _onDaysChange);

  // Fetch and render data
  await loadData();
}

/**
 * Tear down the Overview view, cleaning up charts, subscriptions, and DOM.
 */
export function destroy() {
  _destroyed = true;

  // Destroy chart instance
  if (_costChart) {
    try { _costChart.destroy(); } catch (_) { /* already destroyed */ }
    _costChart = null;
  }

  // Remove resize listener
  if (_resizeHandler) {
    const body = document.getElementById('overview-cost-chart-body');
    if (body) body.removeEventListener('tile-resize', _resizeHandler);
    _resizeHandler = null;
  }

  // Unsubscribe from state
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
    id: 'overview-kpis',
    title: '',
    className: 'kpi-row',
  });
  // Hide the header for the KPI row — the KPIs ARE the content
  const header = tile.querySelector('.gs-tile-header');
  if (header) header.style.display = 'none';
  return tile;
}

function _createCostChartTile() {
  return createTile({
    id: 'overview-cost-chart',
    title: 'Cost Over Time',
    icon: 'fa-solid fa-chart-area',
    iconBg: 'rgba(159,193,49,0.1)',
    iconColor: 'var(--accent)',
    meta: '',
  });
}

function _createProvidersTile() {
  return createTile({
    id: 'overview-providers',
    title: 'Provider Breakdown',
    icon: 'fa-solid fa-server',
    iconBg: 'rgba(59,130,246,0.1)',
    iconColor: '#3b82f6',
    meta: '',
  });
}

function _createTopAppsTile() {
  return createTile({
    id: 'overview-top-apps',
    title: 'Top Apps by Cost',
    icon: 'fa-solid fa-cube',
    iconBg: 'rgba(168,85,247,0.1)',
    iconColor: '#a855f7',
    meta: '',
    filterable: true,
    filterPlaceholder: 'Filter apps\u2026',
    onFilter: (q) => _filterTable('overview-top-apps-body', q),
    actions: exportActions('overview-top-apps'),
  });
}

function _createTopModelsTile() {
  return createTile({
    id: 'overview-top-models',
    title: 'Top Models',
    icon: 'fa-solid fa-microchip',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
    meta: '',
    filterable: true,
    filterPlaceholder: 'Filter models\u2026',
    onFilter: (q) => _filterTable('overview-top-models-body', q),
    actions: exportActions('overview-top-models'),
  });
}

function _createAlertsTile() {
  return createTile({
    id: 'overview-alerts',
    title: 'Recent Alerts',
    icon: 'fa-solid fa-bell',
    iconBg: 'rgba(255,107,107,0.1)',
    iconColor: '#ff6b6b',
  });
}

function _createAgentsTile() {
  return createTile({
    id: 'overview-agents',
    title: 'Agent Status',
    icon: 'fa-solid fa-satellite-dish',
    iconBg: 'rgba(0,229,160,0.1)',
    iconColor: 'var(--accent)',
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function loadData() {
  await loadAppNames();
  if (_destroyed) return;

  const days = get('currentDays') || 7;

  // Set loading states on all tiles
  setTileLoading('overview-kpis', 'cards');
  setTileLoading('overview-cost-chart', 'chart');
  setTileLoading('overview-providers', 'chart');
  setTileLoading('overview-top-apps', 'table');
  setTileLoading('overview-top-models', 'table');
  setTileLoading('overview-alerts', 'text');
  setTileLoading('overview-agents', 'cards');

  // Update tile meta labels
  setTileMeta('overview-cost-chart', `${days}d`);
  setTileMeta('overview-providers', `${days}d`);
  setTileMeta('overview-top-apps', `${days}d`);
  setTileMeta('overview-top-models', `${days}d`);

  try {
    const [summary, timeSeries, providers, apps, models, alerts, agentStatus, financeSummary] = await Promise.all([
      apiFetch('summary').catch(() => null),
      apiFetch('cost-over-time', { days, granularity: 'daily' }).catch(() => null),
      apiFetch('by-provider', { days }).catch(() => null),
      apiFetch('by-app', { days, limit: 10 }).catch(() => null),
      apiFetch('top-models', { days, limit: 8 }).catch(() => null),
      apiFetch('recent-alerts', { limit: 20 }).catch(() => null),
      apiFetch('app-status').catch(() => null),
      rawFetch('/api/v1/finance/summary').then(r => r && r.ok ? r.json() : null).catch(() => null),
    ]);

    if (_destroyed) return;

    // ── Fresh deployment detection ────────────────────────────
    // If no apps are registered and no meaningful data exists,
    // show a full-page onboarding prompt instead of empty tiles.
    const hasApps = agentStatus && Array.isArray(agentStatus) && agentStatus.length > 0;
    const hasCostData = Array.isArray(timeSeries) && timeSeries.some(p => parseFloat(p.cost || 0) > 0);
    const hasAlerts = alerts && Array.isArray(alerts) && alerts.length > 0;

    if (!hasApps && !hasCostData && !hasAlerts) {
      _renderFreshDeployment();
      return;
    }

    // Calculate the period cost from time series (matches charts exactly)
    const periodCost = Array.isArray(timeSeries) ? timeSeries.reduce((s, p) => s + parseFloat(p.cost || 0), 0) : 0;
    const periodTokens = Array.isArray(timeSeries) ? timeSeries.reduce((s, p) => s + (p.tokens || 0), 0) : 0;
    const periodCalls = Array.isArray(timeSeries) ? timeSeries.reduce((s, p) => s + (p.calls || 0), 0) : 0;

    // Render each tile with its data
    _renderKpis(summary, days, periodCost, periodTokens, periodCalls, financeSummary);
    _renderCostChart(timeSeries);
    _renderProviders(providers);
    _renderTopApps(apps);
    _renderTopModels(models);
    _renderAlerts(alerts);
    _renderAgents(agentStatus);
  } catch (err) {
    if (_destroyed) return;
    console.error('[overview] data fetch failed:', err);
    _setAllTilesError();
  }
}

// ── Tile renderers ───────────────────────────────────────────────────────────

function _renderKpis(data, days, periodCost, periodTokens, periodCalls, financeSummary) {
  const body = document.getElementById('overview-kpis-body');
  if (!body) return;

  if (!data) {
    setTileEmpty('overview-kpis', { icon: 'fa-solid fa-gauge', title: 'No summary data' });
    return;
  }

  const periodLabel = days === 1 ? 'Today' : `${days}D`;
  const activeApps = data.active_apps != null ? data.active_apps : 0;
  const onlineAgents = data.online_agents != null ? data.online_agents : 0;

  // Trend indicator for 7d cost
  let trendHTML = '';
  if (data.cost_delta_pct_7d != null) {
    const pct = data.cost_delta_pct_7d;
    const sign = pct > 0 ? '+' : '';
    const cls = pct > 0 ? 'up' : 'down';
    trendHTML = `<span class="kpi-sub"><span class="kpi-trend ${cls}">${sign}${pct.toFixed(1)}%</span> vs prior 7d</span>`;
  }

  // Online agents color
  const agentColor = onlineAgents < activeApps ? 'var(--warn)' : 'var(--accent)';
  const agentFrac = `${onlineAgents}/${activeApps}`;

  // Budget KPIs — only show when finance data is available
  const budgetCards = financeSummary ? (() => {
    const burnPct = financeSummary.overall_burn_pct ?? 0;
    const risk = financeSummary.overall_risk ?? 'on-track';
    const riskLabel = risk.replace(/-/g, ' ').toUpperCase();
    const teamsAtRisk = (financeSummary.teams_at_risk ?? 0) + (financeSummary.teams_over_budget ?? 0);

    let riskColor;
    if (risk === 'over-budget') riskColor = 'var(--danger)';
    else if (risk === 'at-risk') riskColor = '#f59e0b';
    else riskColor = 'var(--accent)';

    return `
      <div class="kpi-card">
        <div class="kpi-label">Budget Burn</div>
        <div class="kpi-value" style="color:${riskColor}">${burnPct.toFixed(1)}%</div>
        <span class="kpi-sub">of monthly budget</span>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Budget Status</div>
        <div class="kpi-value" style="color:${riskColor};font-size:16px">${riskLabel}</div>
        <span class="kpi-sub">overall health</span>
      </div>
      ${teamsAtRisk > 0 ? `
      <div class="kpi-card">
        <div class="kpi-label">Teams at Risk</div>
        <div class="kpi-value" style="color:var(--danger)">${teamsAtRisk}</div>
        <span class="kpi-sub">need attention</span>
      </div>` : ''}
    `;
  })() : '';

  // The 7D and 30D costs always have their own tiles; when one of those is
  // the selected period, show month-to-date here instead of repeating it.
  const showMtd = days === 7 || days === 30;
  const firstLabel = showMtd ? 'Cost MTD' : `Cost ${periodLabel}`;
  const firstValue = showMtd ? data.total_cost_mtd : periodCost;

  body.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">${firstLabel}</div>
      <div class="kpi-value">${fmtCost(firstValue)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Cost 7D</div>
      <div class="kpi-value">${fmtCost(data.total_cost_7d)}</div>
      ${trendHTML}
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Cost 30D</div>
      <div class="kpi-value">${fmtCost(data.total_cost_30d)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Tokens ${periodLabel}</div>
      <div class="kpi-value">${fmtTokens(periodTokens)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">API Calls ${periodLabel}</div>
      <div class="kpi-value">${fmtNum(periodCalls)}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Active Apps</div>
      <div class="kpi-value">${activeApps}</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Online Agents</div>
      <div class="kpi-value" style="color:${agentColor}">${agentFrac}</div>
    </div>
    ${budgetCards}
  `;
}

function _renderCostChart(points) {
  const body = document.getElementById('overview-cost-chart-body');
  if (!body) return;

  // Destroy previous chart
  if (_costChart) {
    try { _costChart.destroy(); } catch (_) { /* noop */ }
    _costChart = null;
  }

  // Remove old resize listener
  if (_resizeHandler) {
    body.removeEventListener('tile-resize', _resizeHandler);
    _resizeHandler = null;
  }

  const hasData = points && Array.isArray(points) && points.length > 0 &&
    points.some(p => parseFloat(p.cost || 0) > 0);
  if (!hasData) {
    setTileEmpty('overview-cost-chart', {
      icon: 'fa-solid fa-chart-area',
      title: 'No cost data',
      description: 'No cost data for this time range. Try a wider range or start the traffic drip.',
    });
    return;
  }

  // Aggregate to daily if >48 hourly data points
  let chartPoints = points;
  if (points.length > 48) {
    const daily = {};
    points.forEach(p => {
      const day = (typeof p.period === 'string' ? p.period : new Date(p.period).toISOString()).slice(0, 10);
      if (!daily[day]) daily[day] = { period: day, cost: 0 };
      daily[day].cost += parseFloat(p.cost || 0);
    });
    chartPoints = Object.values(daily).sort((a, b) => a.period.localeCompare(b.period));
  }

  // Always use date format since we request daily granularity
  const labels = chartPoints.map(p => fmtDateSmart(p.period, false));
  const costs = chartPoints.map(p => parseFloat(p.cost || 0));

  // Create canvas with explicit height
  body.innerHTML = '';
  body.style.padding = '8px 12px 12px';
  body.style.height = '280px';
  body.style.position = 'relative';
  const canvas = document.createElement('canvas');
  body.appendChild(canvas);

  // Render chart — use accent color, show points when sparse
  if (typeof ModusCharts !== 'undefined') {
    _costChart = ModusCharts.area(canvas, {
      labels,
      data: costs,
      color: '#9FC131',
      label: 'Cost (USD)',
    });
    // GAP-4: cost spike drill-through (ModusCharts path)
    if (_costChart && _costChart.canvas) {
      _costChart.canvas.style.cursor = 'pointer';
      _costChart.canvas.addEventListener('click', (evt) => {
        if (typeof _costChart.getElementsAtEventForMode === 'function') {
          const els = _costChart.getElementsAtEventForMode(evt, 'nearest', { intersect: true }, false);
          if (els && els.length > 0) {
            const idx = els[0].index;
            const point = chartPoints[idx];
            if (point) _showCostDrillModal(point, labels[idx]);
          }
        }
      });
    }
  } else if (typeof Chart !== 'undefined') {
    // Fallback: basic Chart.js
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createLinearGradient(0, 0, 0, 200);
    gradient.addColorStop(0, 'rgba(159, 193, 49, 0.15)');
    gradient.addColorStop(1, 'rgba(159, 193, 49, 0)');
    _costChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Cost (USD)',
          data: costs,
          borderColor: '#9FC131',
          borderWidth: 2,
          pointRadius: 0,
          fill: true,
          backgroundColor: gradient,
          tension: 0.4,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        // GAP-4: cost spike drill-through (Chart.js fallback path)
        onClick: (evt, elements) => {
          if (elements && elements.length > 0) {
            const idx = elements[0].index;
            const point = chartPoints[idx];
            if (point) _showCostDrillModal(point, labels[idx]);
          }
        },
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false } },
          y: { grid: { color: 'rgba(26,29,46,0.6)' } },
        },
      },
    });
    if (_costChart && _costChart.canvas) _costChart.canvas.style.cursor = 'pointer';
  }

  // Attach resize listener for gridstack tile resizing
  _resizeHandler = () => {
    if (_costChart && typeof _costChart.resize === 'function') {
      _costChart.resize();
    }
  };
  body.addEventListener('tile-resize', _resizeHandler);
}

function _renderProviders(items) {
  const body = document.getElementById('overview-providers-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('overview-providers', {
      icon: 'fa-solid fa-server',
      title: 'No provider data',
    });
    return;
  }

  body.innerHTML = items.map(p => {
    const color = providerColor(p.provider || '');
    const pct = p.cost_pct != null ? p.cost_pct : 0;
    return `
      <div class="provider-row" style="display:flex;align-items:center;justify-content:space-between;padding:6px 12px">
        <div class="provider-name" style="display:flex;align-items:center;gap:8px;font-size:13px;font-weight:500">
          <div style="width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0"></div>
          ${esc(p.provider)}
        </div>
        <div class="provider-cost" style="font-family:var(--mono);font-size:12px;font-weight:600;white-space:nowrap">${fmtCost(p.cost)}</div>
      </div>
      <div class="provider-bar-wrap" style="padding:0 12px 8px">
        <div style="height:4px;border-radius:2px;background:var(--border);overflow:hidden">
          <div class="provider-bar" style="width:${pct}%;height:100%;background:${color};opacity:0.8;border-radius:2px;transition:width 0.5s ease"></div>
        </div>
      </div>
    `;
  }).join('');
}

function _renderTopApps(items) {
  const body = document.getElementById('overview-top-apps-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('overview-top-apps', {
      icon: 'fa-solid fa-cube',
      title: 'No app data',
    });
    return;
  }

  const envBadge = (env) => {
    const e = (env || '').toLowerCase();
    let bg, color;
    if (e === 'production') { bg = 'rgba(0,229,160,0.08)'; color = 'var(--accent)'; }
    else if (e === 'staging') { bg = 'rgba(245,158,11,0.08)'; color = '#f59e0b'; }
    else { bg = 'rgba(0,153,255,0.08)'; color = 'var(--accent2)'; }
    return `<span class="env-badge ${e}" style="font-family:var(--mono);font-size:10px;padding:2px 6px;border-radius:3px;background:${bg};color:${color}">${esc(env)}</span>`;
  };

  body.innerHTML = `
    <div style="overflow:auto;max-height:340px">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead style="position:sticky;top:0;background:var(--surface);z-index:1">
          <tr>
            <th>App</th>
            <th>Team</th>
            <th>Env</th>
            <th style="text-align:right">Calls</th>
            <th style="text-align:right">Tokens</th>
            <th style="text-align:right">Cost (USD)</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((a, idx) => `
            <tr data-app-idx="${idx}" style="cursor:pointer">
              <td>
                <div style="font-weight:500;color:var(--text)">${esc(a.app_name || a.app_id || '')}</div>
                ${a.app_id ? `<div style="font-family:var(--mono);font-size:10px;color:var(--muted)">${esc(a.app_id)}</div>` : ''}
              </td>
              <td><span style="color:var(--muted)">${esc(a.team_slug || '')}</span></td>
              <td>${envBadge(a.environment || 'dev')}</td>
              <td style="text-align:right">${fmtNum(a.calls || 0)}</td>
              <td style="text-align:right">${fmtTokens(a.tokens || 0)}</td>
              <td style="text-align:right;color:var(--text);font-weight:500">${fmtCost(a.cost)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;

  // GAP-2: click-through to app detail modal
  body.querySelectorAll('tr[data-app-idx]').forEach(row => {
    const idx = parseInt(row.getAttribute('data-app-idx'), 10);
    row.addEventListener('click', () => {
      const app = items[idx];
      if (app) _showAppDetailModal(app);
    });
    row.addEventListener('mouseenter', () => { row.style.background = 'rgba(255,255,255,0.02)'; });
    row.addEventListener('mouseleave', () => { row.style.background = ''; });
  });
}

// ── GAP-2: App Detail Drill-Through Modal ────────────────────────────────────

function _showAppDetailModal(app) {
  const appId = app.app_id || '';
  const days = get('currentDays') || 7;

  openModal({
    title: app.app_name || appId || 'App Details',
    maxWidth: '600px',
    renderBody: (body) => {
      body.innerHTML = '<div style="padding:20px"><div class="ds-skeleton" style="height:200px;border-radius:8px"></div></div>';

      Promise.all([
        apiFetch('top-models', { days, app_id: appId, limit: 5 }).catch(() => null),
        apiFetch('recent-alerts', { limit: 10, app_id: appId }).catch(() => null),
      ]).then(([models, alerts]) => {
        const modelList = Array.isArray(models) ? models : [];
        const alertList = Array.isArray(alerts) ? alerts : [];

        const kpi = (label, value) => `
          <div style="flex:1;min-width:120px;padding:12px;border-radius:8px;background:var(--surface);border:1px solid var(--border);text-align:center">
            <div style="font-size:10px;text-transform:uppercase;color:var(--muted)">${label}</div>
            <div style="font-size:20px;font-weight:700;color:var(--text)">${value}</div>
          </div>`;

        const modelTable = modelList.length > 0 ? `
          <div>
            <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Cost by Model</div>
            <table class="ds-table" style="width:100%;font-size:11px">
              <thead><tr><th>Model</th><th style="text-align:right">Calls</th><th style="text-align:right">Cost</th></tr></thead>
              <tbody>${modelList.map(m => `
                <tr>
                  <td style="font-family:var(--mono)">${esc(m.model || '')}<div style="font-size:9px;color:var(--muted)">${esc(m.provider || '')}</div></td>
                  <td style="text-align:right">${fmtNum(m.calls || 0)}</td>
                  <td style="text-align:right;font-weight:500">${fmtCost(m.cost || 0)}</td>
                </tr>`).join('')}
              </tbody>
            </table>
          </div>` : '<div style="font-size:12px;color:var(--muted)">No model breakdown available.</div>';

        const alertSection = alertList.length > 0 ? `
          <div>
            <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Recent Alerts</div>
            ${alertList.slice(0, 5).map(a => {
              const sev = a.severity || 'info';
              const sevColor = sev === 'critical' ? 'var(--danger)' : sev === 'warning' ? 'var(--warn)' : 'var(--accent2)';
              return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border);font-size:11px">
                <span style="width:6px;height:6px;border-radius:50%;background:${sevColor};flex-shrink:0"></span>
                <span style="flex:1;color:var(--text)">${esc(a.metric || 'Alert')}</span>
                <span style="color:var(--muted);font-size:10px">${a.fired_at ? timeSince(a.fired_at) : ''}</span>
              </div>`;
            }).join('')}
          </div>` : '';

        body.innerHTML = `
          <div style="display:flex;flex-direction:column;gap:16px;padding:4px 0">
            <div style="display:flex;gap:12px;flex-wrap:wrap">
              ${kpi('Total Cost', fmtCost(app.cost || 0))}
              ${kpi('API Calls', fmtNum(app.calls || 0))}
              ${kpi('Tokens', fmtTokens(app.tokens || 0))}
            </div>
            <div style="display:grid;grid-template-columns:auto 1fr;gap:4px 16px;font-size:12px;padding:8px 0;border-bottom:1px solid var(--border)">
              <span style="color:var(--muted)">App ID</span>
              <span style="font-family:var(--mono);color:var(--text)">${esc(appId)}</span>
              <span style="color:var(--muted)">Team</span>
              <span style="color:var(--text)">${esc(app.team_slug || '\u2014')}</span>
              <span style="color:var(--muted)">Environment</span>
              <span style="color:var(--text)">${esc(app.environment || '\u2014')}</span>
            </div>
            ${modelTable}
            ${alertSection}
          </div>
        `;
      });
    },
  });
}

// ── GAP-4: Cost Spike Drill-Through Modal ────────────────────────────────────

function _showCostDrillModal(point, label) {
  const totalCost = parseFloat(point.cost || 0);
  const periodKey = point.period || label;

  openModal({
    title: `Cost Breakdown \u2014 ${esc(String(label))}`,
    maxWidth: '600px',
    renderBody: (body) => {
      body.innerHTML = '<div style="padding:20px"><div class="ds-skeleton" style="height:200px;border-radius:8px"></div></div>';

      Promise.all([
        apiFetch('by-app', { days: 1, date: periodKey }).catch(() => null),
        apiFetch('by-provider', { days: 1, date: periodKey }).catch(() => null),
        apiFetch('top-models', { days: 1, date: periodKey, limit: 5 }).catch(() => null),
      ]).then(([apps, providers, models]) => {
        const appList = Array.isArray(apps) ? apps : [];
        const providerList = Array.isArray(providers) ? providers : [];
        const modelList = Array.isArray(models) ? models : [];

        const kpi = (lbl, value) => `
          <div style="flex:1;min-width:120px;padding:12px;border-radius:8px;background:var(--surface);border:1px solid var(--border);text-align:center">
            <div style="font-size:10px;text-transform:uppercase;color:var(--muted)">${lbl}</div>
            <div style="font-size:22px;font-weight:700;color:var(--text)">${value}</div>
          </div>`;

        const section = (title, items, render) => items.length > 0 ? `
          <div>
            <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:8px">${title}</div>
            ${items.map(render).join('')}
          </div>` : '';

        body.innerHTML = `
          <div style="display:flex;flex-direction:column;gap:16px;padding:4px 0">
            <div style="display:flex;gap:12px;flex-wrap:wrap">
              ${kpi('Total Cost', fmtCost(totalCost))}
              ${kpi('Apps', appList.length)}
              ${kpi('Providers', providerList.length)}
            </div>
            ${section('Top Apps', appList.slice(0, 5), a => `
              <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:12px">
                <span style="color:var(--text);font-weight:500">${esc(a.app_name || a.app_id || '\u2014')}</span>
                <span style="font-family:var(--mono);font-weight:600">${fmtCost(a.cost || 0)}</span>
              </div>`)}
            ${section('By Provider', providerList, p => `
              <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:12px">
                <span style="color:${providerColor(p.provider || '')};font-weight:500">${esc(p.provider || '\u2014')}</span>
                <span style="font-family:var(--mono);font-weight:600">${fmtCost(p.cost || 0)}</span>
              </div>`)}
            ${section('Top Models', modelList, m => `
              <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:12px">
                <span style="font-family:var(--mono);color:var(--text)">${esc(m.model || '\u2014')}</span>
                <span style="font-family:var(--mono);font-weight:600">${fmtCost(m.cost || 0)}</span>
              </div>`)}
          </div>
        `;
      });
    },
  });
}

function _renderTopModels(items) {
  const body = document.getElementById('overview-top-models-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('overview-top-models', {
      icon: 'fa-solid fa-microchip',
      title: 'No model data',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow:auto;max-height:340px">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead style="position:sticky;top:0;background:var(--surface);z-index:1">
          <tr>
            <th>Model</th>
            <th style="text-align:right">Calls</th>
            <th style="text-align:right">Avg In</th>
            <th style="text-align:right">Avg Out</th>
            <th style="text-align:right">Cost (USD)</th>
          </tr>
        </thead>
        <tbody>
          ${items.map(m => `
            <tr>
              <td>
                <div class="model-badge">${esc(m.model || '')}</div>
                <div style="font-size:10px;color:var(--muted);margin-top:3px">${esc(m.provider || '')}</div>
              </td>
              <td style="text-align:right">${fmtNum(m.calls || 0)}</td>
              <td style="text-align:right">${m.avg_input_tokens ? fmtTokens(m.avg_input_tokens) : '\u2014'}</td>
              <td style="text-align:right">${m.avg_output_tokens ? fmtTokens(m.avg_output_tokens) : '\u2014'}</td>
              <td style="text-align:right;color:var(--text);font-weight:500">${fmtCost(m.cost)}</td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function _renderAlerts(items) {
  const body = document.getElementById('overview-alerts-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('overview-alerts', {
      icon: 'fa-solid fa-bell',
      title: 'No alerts',
      description: 'All clear \u2014 no alerts in this period.',
    });
    return;
  }

  body.innerHTML = items.map(a => {
    const sev = esc(a.severity || 'info');
    const acked = a.acknowledged ? '<span class="ack-badge" style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(159,193,49,0.12);color:var(--accent);font-weight:600;margin-left:4px">ACK</span>' : '';
    return `
      <div class="alert-item" style="display:flex;align-items:flex-start;gap:10px;padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border);transition:background 0.15s"
           data-alert-id="${esc(a.id || '')}">
        <div class="severity-dot ${sev}" style="width:8px;height:8px;border-radius:50%;margin-top:5px;flex-shrink:0"></div>
        <div style="flex:1;min-width:0">
          <div style="font-size:12px;font-weight:500;color:var(--text)">
            ${esc(a.metric || 'Alert')}${acked}
          </div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">
            actual ${fmtCost(a.actual_value)} \u00b7 threshold ${fmtCost(a.threshold_value)}${a.app_id ? ` \u00b7 ${esc(appName(a.app_id))}` : ''}
          </div>
        </div>
        <div style="font-size:10px;color:var(--muted);white-space:nowrap;flex-shrink:0">${a.fired_at ? timeSince(a.fired_at) : ''}</div>
      </div>
    `;
  }).join('');

  // Attach click listeners for alert detail modals
  body.querySelectorAll('.alert-item').forEach((el, idx) => {
    el.addEventListener('click', () => {
      const alert = items[idx];
      if (alert) _showAlertModal(alert);
    });
    // Hover effect
    el.addEventListener('mouseenter', () => { el.style.background = 'rgba(255,255,255,0.02)'; });
    el.addEventListener('mouseleave', () => { el.style.background = ''; });
  });
}

function _renderAgents(items) {
  const body = document.getElementById('overview-agents-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('overview-agents', {
      icon: 'fa-solid fa-satellite-dish',
      title: 'No apps registered',
    });
    return;
  }

  body.innerHTML = `<div class="agent-grid" style="max-height:360px;overflow:auto">${items.map((a, idx) => {
    const isOnline = !!a.online;
    const statusCls = isOnline ? 'online' : 'offline';
    const providers = (a.instrumented_providers && Array.isArray(a.instrumented_providers))
      ? a.instrumented_providers.map(p => `<span class="provider-chip" style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(255,255,255,0.04);color:var(--muted)">${esc(p)}</span>`).join('')
      : '';

    let statusLine = '';
    if (isOnline) {
      statusLine = a.last_seen_at ? timeSince(a.last_seen_at) : 'online';
    } else {
      statusLine = a.last_seen_at ? `offline \u00b7 ${timeSince(a.last_seen_at)}` : 'never seen';
    }
    if (a.agent_version) statusLine += ` \u00b7 v${esc(a.agent_version)}`;

    return `
      <div class="agent-card ${statusCls}" style="cursor:pointer" data-agent-idx="${idx}">
        <div class="agent-top">
          <div class="agent-name" title="${esc(a.app_name || '')}">${esc(a.app_name || a.app_id || '')}</div>
          <div class="online-dot ${statusCls}"></div>
        </div>
        <div class="agent-team" style="font-size:11px;color:var(--muted)">${esc(a.team_slug || '')} \u00b7 ${esc(a.environment || '')}</div>
        ${providers ? `<div class="agent-providers" style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">${providers}</div>` : ''}
        <div style="font-family:var(--mono);font-size:9px;color:var(--muted);margin-top:5px">${statusLine}</div>
      </div>
    `;
  }).join('')}</div>`;

  // Attach click handlers for agent detail modals
  body.querySelectorAll('.agent-card').forEach(el => {
    const idx = parseInt(el.getAttribute('data-agent-idx'), 10);
    el.addEventListener('click', () => {
      const agent = items[idx];
      if (agent) _showAgentModal(agent);
    });
  });
}

// ── Alert detail modal ───────────────────────────────────────────────────────

function _showAlertModal(alert) {
  const sevColors = { critical: 'var(--danger)', warning: 'var(--warn)', info: 'var(--accent2)' };
  const sevColor = sevColors[alert.severity] || 'var(--muted)';
  const firedAt = alert.fired_at ? new Date(alert.fired_at).toLocaleString() : '\u2014';
  const resolvedAt = alert.resolved_at ? new Date(alert.resolved_at).toLocaleString() : 'Ongoing';

  openModal({
    title: 'Alert Snapshot',
    maxWidth: '480px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:12px;padding:4px 0">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="display:flex;align-items:center;gap:8px">
              <span style="width:10px;height:10px;border-radius:50%;background:${sevColor};display:inline-block"></span>
              <span style="font-weight:600;font-size:14px;color:var(--text)">${esc(alert.metric || '\u2014')}</span>
            </span>
            <span style="background:${sevColor}20;color:${sevColor};padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;text-transform:uppercase">${esc(alert.severity || 'info')}</span>
          </div>
          <div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12px">
            <span style="color:var(--muted)">Actual Value</span>
            <span style="font-family:var(--mono);color:var(--text)">${fmtCost(alert.actual_value)}</span>
            <span style="color:var(--muted)">Threshold</span>
            <span style="font-family:var(--mono);color:var(--text)">${fmtCost(alert.threshold_value)}</span>
            <span style="color:var(--muted)">App</span>
            <span style="color:var(--text)">${esc(appName(alert.app_id) || 'All apps')}</span>
            <span style="color:var(--muted)">Team</span>
            <span style="color:var(--text)">${esc(alert.team_slug || alert.team_id || '\u2014')}</span>
            <span style="color:var(--muted)">Fired At</span>
            <span style="font-family:var(--mono);font-size:11px;color:var(--text)">${firedAt}</span>
            <span style="color:var(--muted)">Resolved</span>
            <span style="font-family:var(--mono);font-size:11px;color:${alert.resolved_at ? 'var(--accent)' : 'var(--warn)'}">${resolvedAt}</span>
            <span style="color:var(--muted)">Acknowledged</span>
            <span style="color:var(--text)">${alert.acknowledged ? 'Yes' : 'No'}</span>
          </div>
          ${alert.message ? `<div style="font-size:12px;color:var(--muted);border-top:1px solid var(--border);padding-top:10px">${esc(alert.message)}</div>` : ''}
        </div>
      `;
    },
  });
}

// ── Agent detail modal ───────────────────────────────────────────────────────

function _showAgentModal(agent) {
  const isOnline = !!agent.online;
  const statusColor = isOnline ? 'var(--accent)' : 'var(--danger)';
  const statusText = isOnline ? 'Online' : 'Offline';

  openModal({
    title: 'Agent Details',
    maxWidth: '480px',
    renderBody: (body) => {
      const providers = (agent.instrumented_providers && Array.isArray(agent.instrumented_providers))
        ? agent.instrumented_providers.map(p => esc(p)).join(', ')
        : '\u2014';

      body.innerHTML = `
        <div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12px;padding:4px 0">
          <span style="color:var(--muted)">App Name</span>
          <span style="font-weight:600;color:var(--text)">${esc(agent.app_name || agent.app_id || '\u2014')}</span>
          <span style="color:var(--muted)">App ID</span>
          <span style="font-family:var(--mono);color:var(--text)">${esc(agent.app_id || '\u2014')}</span>
          <span style="color:var(--muted)">Team</span>
          <span style="color:var(--text)">${esc(agent.team_slug || '\u2014')}</span>
          <span style="color:var(--muted)">Environment</span>
          <span style="color:var(--text)">${esc(agent.environment || '\u2014')}</span>
          <span style="color:var(--muted)">Status</span>
          <span style="color:${statusColor};font-weight:600">${statusText}</span>
          <span style="color:var(--muted)">Last Seen</span>
          <span style="font-family:var(--mono);font-size:11px;color:var(--text)">${agent.last_seen_at ? new Date(agent.last_seen_at).toLocaleString() : 'Never'}</span>
          <span style="color:var(--muted)">Version</span>
          <span style="font-family:var(--mono);color:var(--text)">${agent.agent_version ? `v${esc(agent.agent_version)}` : '\u2014'}</span>
          <span style="color:var(--muted)">Providers</span>
          <span style="color:var(--text)">${providers}</span>
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

// ── Fresh deployment state ───────────────────────────────────────────────────

function _renderFreshDeployment() {
  if (!_container) return;

  // Replace the entire grid with a centered onboarding prompt
  _container.innerHTML = `
    <div class="fresh-deploy" style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:calc(100vh - 180px);gap:24px;padding:40px 20px;text-align:center">
      <div style="width:80px;height:80px;border-radius:20px;background:rgba(159,193,49,0.08);display:flex;align-items:center;justify-content:center">
        <i class="fa-solid fa-satellite-dish" style="font-size:36px;color:var(--accent);opacity:0.7"></i>
      </div>
      <div>
        <h2 style="font-size:22px;font-weight:700;color:var(--text);margin:0 0 8px">Welcome to Modus</h2>
        <p style="font-size:14px;color:var(--muted);max-width:460px;margin:0 auto;line-height:1.6">
          Connect services or scan your environment to start displaying metrics.
          Once your first app reports data, this dashboard will light up automatically.
        </p>
      </div>
      <div style="display:flex;gap:16px;flex-wrap:wrap;justify-content:center;margin-top:8px">
        <div class="fresh-deploy-card" style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 24px;min-width:200px;text-align:left">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
            <div style="width:32px;height:32px;border-radius:8px;background:rgba(59,130,246,0.1);display:flex;align-items:center;justify-content:center">
              <i class="fa-solid fa-plug" style="font-size:14px;color:#3b82f6"></i>
            </div>
            <span style="font-size:13px;font-weight:600;color:var(--text)">Connect an App</span>
          </div>
          <p style="font-size:11px;color:var(--muted);margin:0;line-height:1.5">
            Install the Modus agent in your app and point it at this instance to start tracking AI usage.
          </p>
        </div>
        <div class="fresh-deploy-card" style="background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px 24px;min-width:200px;text-align:left">
          <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px">
            <div style="width:32px;height:32px;border-radius:8px;background:rgba(168,85,247,0.1);display:flex;align-items:center;justify-content:center">
              <i class="fa-solid fa-magnifying-glass" style="font-size:14px;color:#a855f7"></i>
            </div>
            <span style="font-size:13px;font-weight:600;color:var(--text)">Scan Environment</span>
          </div>
          <p style="font-size:11px;color:var(--muted);margin:0;line-height:1.5">
            Run a scan to discover AI services in your infrastructure and begin monitoring automatically.
          </p>
        </div>
      </div>
      <button class="ds-btn ds-btn-ghost ds-btn-sm" style="margin-top:8px;color:var(--muted)" onclick="location.reload()">
        <i class="fa-solid fa-arrows-rotate" style="margin-right:6px"></i>Refresh
      </button>
    </div>
  `;

  // Null out the grid since we replaced the container contents
  _grid = null;
}

// ── Error helper ─────────────────────────────────────────────────────────────

function _setAllTilesError() {
  const ids = [
    'overview-kpis', 'overview-cost-chart', 'overview-providers',
    'overview-top-apps', 'overview-top-models', 'overview-alerts', 'overview-agents',
  ];
  ids.forEach(id => setTileError(id, 'Failed to load data', () => loadData()));
}
