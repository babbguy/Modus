/**
 * Modus Dashboard v2 — Finance Intelligence View
 * Renders the Finance Intelligence dashboard with 9 Gridstack tiles:
 * KPIs, Spend Trend, Department Donut, Burn Rate, Provider Bar,
 * Forecast, Breach Predictions, Team Gauges, and Scheduled Reports.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError, setTileMeta, exportActions } from '../tile.js';
import { apiFetch, rawFetch, esc, authHeaders } from '../api.js';
import { fmtCost, fmtTokens, fmtNum, fmtDate, timeSince, providerColor } from '../format.js';
import { subscribe, unsubscribe } from '../state.js';
import { openModal } from '../modal.js';
import { toast } from '../toast.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;

// Chart instances
let _spendTrendChart = null;
let _deptDonutChart = null;
let _providerBarChart = null;

// Resize handlers
let _resizeHandlers = {};   // { tileId: handler }

// Cached finance summary for cross-tile use
let _financeData = null;

// Comparison mode state (shared with overview via localStorage)
let _compareMode = localStorage.getItem('modus_compare_mode') === 'true';

// Chargeback and reconciliation caches
let _chargebackCache = null;
let _reconciliationCache = null;
let _costCentersCache = [];

// ── State subscription handler ───────────────────────────────────────────────

function _onDaysChange() {
  if (!_destroyed) loadData();
}

/**
 * Toggle comparison mode and re-render data.
 */
function _toggleCompareMode() {
  _compareMode = !_compareMode;
  try {
    localStorage.setItem('modus_compare_mode', _compareMode ? 'true' : 'false');
  } catch (_) {
    // localStorage may be disabled, silently fail
  }
  if (!_destroyed) loadData();
}

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Finance Intelligence view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  // Initialize grid
  _grid = initGrid(container, 'finance');

  // Create tiles
  const tiles = [
    _createKpiTile(),
    _createSpendTrendTile(),
    _createDeptDonutTile(),
    _createBurnRateTile(),
    _createProviderBarTile(),
    _createForecastTile(),
    _createBreachTile(),
    _createTeamGaugesTile(),
    _createReportsTile(),
    _createChargebackTile(),
    _createReconciliationTile(),
    _createCostCentersTile(),
  ];

  // Load saved or default layout
  const layout = loadLayout('finance', LAYOUTS.finance);

  // Add tiles to grid
  addTiles(_grid, tiles, layout);

  // Subscribe to period changes
  subscribe('currentDays', _onDaysChange);

  // Fetch and render data
  await loadData();
}

/**
 * Tear down the Finance Intelligence view, cleaning up charts, subscriptions, and DOM.
 */
export function destroy() {
  _destroyed = true;

  // Destroy chart instances
  _destroyChart('_spendTrendChart');
  _destroyChart('_deptDonutChart');
  _destroyChart('_providerBarChart');

  // Remove all resize listeners
  for (const [tileId, handler] of Object.entries(_resizeHandlers)) {
    const body = document.getElementById(`${tileId}-body`);
    if (body) body.removeEventListener('tile-resize', handler);
  }
  _resizeHandlers = {};

  // Unsubscribe from state
  unsubscribe('currentDays', _onDaysChange);

  _financeData = null;
  _chargebackCache = null;
  _reconciliationCache = null;
  _costCentersCache = [];
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

// ── Chart cleanup helper ─────────────────────────────────────────────────────

function _destroyChart(varName) {
  const chart = varName === '_spendTrendChart' ? _spendTrendChart
    : varName === '_deptDonutChart' ? _deptDonutChart
    : varName === '_providerBarChart' ? _providerBarChart
    : null;
  if (chart) {
    try { chart.destroy(); } catch (_) { /* already destroyed */ }
  }
  if (varName === '_spendTrendChart') _spendTrendChart = null;
  if (varName === '_deptDonutChart') _deptDonutChart = null;
  if (varName === '_providerBarChart') _providerBarChart = null;
}

function _attachResize(tileId, chartGetter) {
  // Remove old listener if present
  if (_resizeHandlers[tileId]) {
    const body = document.getElementById(`${tileId}-body`);
    if (body) body.removeEventListener('tile-resize', _resizeHandlers[tileId]);
  }
  const handler = () => {
    const c = chartGetter();
    if (c && typeof c.resize === 'function') c.resize();
  };
  _resizeHandlers[tileId] = handler;
  const body = document.getElementById(`${tileId}-body`);
  if (body) body.addEventListener('tile-resize', handler);
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createKpiTile() {
  const tile = createTile({
    id: 'fin-kpis',
    title: '',
    className: 'kpi-row',
  });
  const header = tile.querySelector('.gs-tile-header');
  if (header) header.style.display = 'none';
  return tile;
}

function _createSpendTrendTile() {
  return createTile({
    id: 'fin-spend-trend',
    title: 'Spend Trend (MTD)',
    icon: 'fa-solid fa-chart-area',
    iconBg: 'rgba(159,193,49,0.1)',
    iconColor: 'var(--accent)',
    meta: 'Scroll to zoom',
    actions: [
      {
        label: _compareMode ? 'Compare: ON' : 'Compare: OFF',
        icon: 'fa-solid fa-arrows-left-right',
        onclick: () => _toggleCompareMode(),
      },
    ],
  });
}

function _createDeptDonutTile() {
  return createTile({
    id: 'fin-dept-donut',
    title: 'Spend by Department',
    icon: 'fa-solid fa-chart-pie',
    iconBg: 'rgba(59,130,246,0.1)',
    iconColor: '#3b82f6',
    meta: '',
  });
}

function _createBurnRateTile() {
  return createTile({
    id: 'fin-burn-rate',
    title: 'Budget Burn Rate by Team',
    icon: 'fa-solid fa-fire',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
    meta: '',
    filterable: true,
    filterPlaceholder: 'Filter teams\u2026',
    onFilter: (q) => _filterTable('fin-burn-rate-body', q),
    actions: exportActions('fin-burn-rate'),
  });
}

function _createProviderBarTile() {
  return createTile({
    id: 'fin-provider-bar',
    title: 'Spend by Provider',
    icon: 'fa-solid fa-server',
    iconBg: 'rgba(168,85,247,0.1)',
    iconColor: '#a855f7',
    meta: '',
  });
}

function _createForecastTile() {
  return createTile({
    id: 'fin-forecast',
    title: 'Spend Forecast',
    icon: 'fa-solid fa-crystal-ball',
    iconBg: 'rgba(6,182,212,0.1)',
    iconColor: '#06b6d4',
    meta: '',
  });
}

function _createBreachTile() {
  return createTile({
    id: 'fin-breach',
    title: 'Budget Breach Alerts',
    icon: 'fa-solid fa-triangle-exclamation',
    iconBg: 'rgba(239,68,68,0.1)',
    iconColor: '#ef4444',
    meta: '',
  });
}

function _createTeamGaugesTile() {
  return createTile({
    id: 'fin-team-gauges',
    title: 'Team Budget Utilization',
    icon: 'fa-solid fa-gauge-high',
    iconBg: 'rgba(16,185,129,0.1)',
    iconColor: '#10b981',
    meta: '',
  });
}

function _createReportsTile() {
  return createTile({
    id: 'fin-reports',
    title: 'Scheduled Reports',
    icon: 'fa-solid fa-clock',
    iconBg: 'rgba(124,58,237,0.1)',
    iconColor: '#7c3aed',
    meta: '',
  });
}

function _createChargebackTile() {
  return createTile({
    id: 'fin-chargeback',
    title: 'Chargeback',
    icon: 'fa-solid fa-money-bill-transfer',
    iconBg: 'rgba(34,197,94,0.1)',
    iconColor: '#22c55e',
    actions: [
      {
        label: 'Generate',
        icon: 'fa-solid fa-play',
        onclick: () => _generateChargeback(),
      },
    ],
  });
}

function _createReconciliationTile() {
  return createTile({
    id: 'fin-reconciliation',
    title: 'Reconciliation Status',
    icon: 'fa-solid fa-scale-balanced',
    iconBg: 'rgba(99,102,241,0.1)',
    iconColor: '#6366f1',
    actions: [
      {
        label: 'Import CSV',
        icon: 'fa-solid fa-file-arrow-up',
        onclick: () => _importBillingActuals(),
      },
    ],
  });
}

function _createCostCentersTile() {
  return createTile({
    id: 'fin-cost-centers',
    title: 'Cost Center Assignment',
    icon: 'fa-solid fa-sitemap',
    iconBg: 'rgba(236,72,153,0.1)',
    iconColor: '#ec4899',
    actions: [
      {
        label: '+ Assign',
        onclick: () => _openCostCenterModal(),
      },
    ],
  });
}

// ── Finance API helper ───────────────────────────────────────────────────────

/**
 * Fetch a finance endpoint and parse JSON. Returns null on failure.
 * @param {string} path  - Path under /api/v1/finance/
 * @param {Object} [opts] - Extra fetch options (method, body, etc.)
 * @returns {Promise<any|null>}
 */
async function _financeFetch(path, opts = {}) {
  try {
    const resp = await rawFetch(`/api/v1/finance/${path}`, opts);
    if (!resp.ok) return null;
    return resp.json();
  } catch (_) {
    return null;
  }
}

/**
 * POST to a finance endpoint and parse JSON. Returns null on failure.
 */
async function _financePost(path, body) {
  return _financeFetch(path, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function loadData() {
  if (_destroyed) return;

  // Set loading states
  setTileLoading('fin-kpis', 'cards');
  setTileLoading('fin-spend-trend', 'chart');
  setTileLoading('fin-dept-donut', 'chart');
  setTileLoading('fin-burn-rate', 'table');
  setTileLoading('fin-provider-bar', 'chart');
  setTileLoading('fin-forecast', 'text');
  setTileLoading('fin-breach', 'text');
  setTileLoading('fin-team-gauges', 'cards');
  setTileLoading('fin-reports', 'table');
  setTileLoading('fin-chargeback', 'table');
  setTileLoading('fin-reconciliation', 'cards');
  setTileLoading('fin-cost-centers', 'table');

  try {
    // Fire all API calls in parallel — include dashboard summary for authoritative MTD
    const [summary, dashSummary, spendTrend, burnRate, forecast, breaches, reports, chargeback, reconciliation, costCenters] = await Promise.all([
      _financeFetch('summary'),
      apiFetch('summary').catch(() => null),
      _financeFetch('spend-trend?period=mtd'),
      _financeFetch('burn-rate'),
      _financePost('forecast', { method: 'auto', basis_days: 28 }),
      _financeFetch('breach-predictions'),
      _financeFetch('reports'),
      _financeFetch('chargeback'),
      _financeFetch('reconciliation'),
      _financeFetch('cost-centers').catch(() => null),
    ]);

    if (_destroyed) return;

    // Fetch prior period data for comparison mode (GAP-7 fix)
    let priorBurnRate = null;
    if (_compareMode) {
      priorBurnRate = await _financeFetch('burn-rate?period=prior_month').catch(() => null);
    }

    if (_destroyed) return;

    // Align MTD spend with dashboard summary (authoritative source)
    if (summary && dashSummary) {
      summary.total_current_spend_usd = parseFloat(dashSummary.total_cost_mtd || summary.total_current_spend_usd || 0);
    }

    // Cache summary and chargeback data for cross-tile use
    _financeData = summary;
    _chargebackCache = chargeback;
    _reconciliationCache = reconciliation;
    _costCentersCache = Array.isArray(costCenters) ? costCenters : [];

    // Render each tile
    _renderKpis(summary);
    _renderSpendTrend(spendTrend);
    _renderDeptDonut(summary);
    _renderBurnRate(burnRate, priorBurnRate);
    _renderProviderBar(summary);
    _renderForecast(forecast);
    _renderBreach(breaches);
    _renderTeamGauges(burnRate);
    _renderReports(reports);
    _renderChargeback(_chargebackCache);
    _renderReconciliation(_reconciliationCache);
    _renderCostCenters(_costCentersCache);
  } catch (err) {
    if (_destroyed) return;
    console.error('[finance] data fetch failed:', err);
    _setAllTilesError();
  }
}

// ── Tile renderers ───────────────────────────────────────────────────────────

function _renderKpis(data) {
  const body = document.getElementById('fin-kpis-body');
  if (!body) return;

  if (!data) {
    // No data — show zeros, not fake numbers
    _renderKpisInner(body, {
      total_monthly_budget_usd: 0,
      total_current_spend_usd: 0,
      total_projected_eom_usd: 0,
      overall_burn_pct: 0,
      overall_risk: 'on-track',
    });
    return;
  }
  _renderKpisInner(body, data);
}

function _renderKpisInner(body, d) {
  const fmt = v => v >= 1000 ? '$' + (v / 1000).toFixed(1) + 'k' : '$' + (v ?? 0).toFixed(2);

  const budget = d.total_monthly_budget_usd ?? 0;
  const spend = d.total_current_spend_usd ?? 0;
  const projected = d.total_projected_eom_usd ?? 0;
  const burnPct = d.overall_burn_pct ?? 0;
  const risk = d.overall_risk ?? 'on-track';
  const riskLabel = risk.replace(/-/g, ' ').toUpperCase();

  let riskColor;
  if (risk === 'over-budget') riskColor = 'var(--ds-red-500, var(--danger))';
  else if (risk === 'at-risk') riskColor = 'var(--ds-amber-500, #f59e0b)';
  else riskColor = 'var(--ds-green-500, var(--accent))';

  // Determine cost trend arrow
  let trendHTML = '';
  if (d.cost_trend_pct != null) {
    const pct = d.cost_trend_pct;
    const sign = pct > 0 ? '+' : '';
    const cls = pct > 0 ? 'up' : 'down';
    trendHTML = `<span class="kpi-sub"><span class="kpi-trend ${cls}">${sign}${pct.toFixed(1)}%</span> vs prior period</span>`;
  }

  body.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">MTD Spend</div>
      <div class="kpi-value">${fmt(spend)}</div>
      <span class="kpi-sub">${burnPct.toFixed(1)}% of budget</span>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Forecast EOM</div>
      <div class="kpi-value">${fmt(projected)}</div>
      <span class="kpi-sub">End of month</span>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Budget Remaining</div>
      <div class="kpi-value">${fmt(Math.max(budget - spend, 0))}</div>
      <span class="kpi-sub">of ${fmt(budget)} total</span>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Overall Status</div>
      <div class="kpi-value" style="color:${riskColor}">${riskLabel}</div>
      ${trendHTML || '<span class="kpi-sub">Budget health</span>'}
    </div>
  `;

  // Animate KPI values if ModusCharts is available
  if (typeof ModusCharts !== 'undefined') {
    const kpiValues = body.querySelectorAll('.kpi-value');
    if (kpiValues[0]) ModusCharts.countUp(kpiValues[0], spend, { prefix: '$', decimals: 2 });
    if (kpiValues[1]) ModusCharts.countUp(kpiValues[1], projected, { prefix: '$', decimals: 2 });
    if (kpiValues[2]) ModusCharts.countUp(kpiValues[2], Math.max(budget - spend, 0), { prefix: '$', decimals: 0 });
  }
}

// ── Spend Trend Chart ────────────────────────────────────────────────────────

function _renderSpendTrend(data) {
  const body = document.getElementById('fin-spend-trend-body');
  if (!body) return;

  _destroyChart('_spendTrendChart');

  if (!data || !data.days || !data.days.length) {
    setTileEmpty('fin-spend-trend', {
      icon: 'fa-solid fa-chart-area',
      title: 'No spend data',
      description: 'Spend trend data will appear after usage is recorded.',
    });
    return;
  }

  const budget = _financeData ? (_financeData.total_monthly_budget_usd ?? 0) : 0;
  const daysInMonth = new Date(new Date().getFullYear(), new Date().getMonth() + 1, 0).getDate();
  const dailyBudget = budget / daysInMonth;

  const totalSeries = data.series ? data.series.find(s => s.label === 'Total') : null;
  const chartData = totalSeries ? totalSeries.data : [];

  if (!chartData.length) {
    setTileEmpty('fin-spend-trend', {
      icon: 'fa-solid fa-chart-area',
      title: 'No spend data',
    });
    return;
  }

  // Create canvas
  const canvas = document.createElement('canvas');
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  body.innerHTML = '';
  body.style.padding = '8px 12px 12px';
  body.appendChild(canvas);

  if (typeof ModusCharts !== 'undefined') {
    _spendTrendChart = ModusCharts.area(canvas, {
      labels: data.days,
      data: chartData,
      label: 'Daily Spend',
      budgetLine: data.budget_line ? (data.budget_line / daysInMonth) : (dailyBudget > 0 ? dailyBudget : null),
      // zoom: true, // disabled — Hammer.js compat issue with Chart.js 4.4.1
    });
  } else if (typeof Chart !== 'undefined') {
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createLinearGradient(0, 0, 0, 200);
    gradient.addColorStop(0, 'rgba(159, 193, 49, 0.15)');
    gradient.addColorStop(1, 'rgba(159, 193, 49, 0)');
    _spendTrendChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: data.days,
        datasets: [{
          label: 'Daily Spend',
          data: chartData,
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
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false } },
          y: { grid: { color: 'rgba(26,29,46,0.6)' } },
        },
      },
    });
  }

  _attachResize('fin-spend-trend', () => _spendTrendChart);
}

// ── Department Donut Chart ───────────────────────────────────────────────────

function _renderDeptDonut(data) {
  const body = document.getElementById('fin-dept-donut-body');
  if (!body) return;

  _destroyChart('_deptDonutChart');

  const departments = (data && data.spend_by_department) ? data.spend_by_department : [];

  if (!departments.length) {
    setTileEmpty('fin-dept-donut', {
      icon: 'fa-solid fa-chart-pie',
      title: 'No department data',
      description: 'Assign teams to cost centers to see department breakdown.',
    });
    return;
  }

  _renderDonutInner(body, departments);
}

function _renderDonutInner(body, departments) {
  if (typeof ModusCharts === 'undefined') {
    // Fallback: simple list
    const total = departments.reduce((s, d) => s + (d.spend_usd || 0), 0);
    body.innerHTML = departments.map(d => {
      const pct = total > 0 ? ((d.spend_usd / total) * 100).toFixed(1) : '0.0';
      return `<div style="display:flex;justify-content:space-between;padding:4px 12px;font-size:12px">
        <span>${esc(d.department)}</span>
        <span style="font-weight:500">$${(d.spend_usd || 0).toFixed(2)} (${pct}%)</span>
      </div>`;
    }).join('');
    return;
  }

  const total = departments.reduce((s, d) => s + (d.spend_usd || 0), 0);

  // Canvas container
  const wrap = document.createElement('div');
  wrap.style.cssText = 'display:flex;flex-direction:column;align-items:center;height:100%;padding:8px';

  const canvasWrap = document.createElement('div');
  canvasWrap.style.cssText = 'flex:1;display:flex;align-items:center;justify-content:center;width:100%';
  const canvas = document.createElement('canvas');
  canvasWrap.appendChild(canvas);
  wrap.appendChild(canvasWrap);

  // Legend
  const legend = document.createElement('div');
  legend.style.cssText = 'font-size:11px;width:100%;padding:0 4px';
  const palette = ['#3b82f6', '#10b981', '#8b5cf6', '#f59e0b', '#ef4444', '#06b6d4', '#ec4899', '#7c3aed'];
  legend.innerHTML = departments.map((d, i) =>
    `<div style="display:flex;justify-content:space-between;padding:3px 0">
      <span><span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${palette[i % palette.length]};margin-right:6px"></span>${esc(d.department)}</span>
      <span style="font-weight:500">$${(d.spend_usd || 0).toFixed(2)}</span>
    </div>`
  ).join('');
  wrap.appendChild(legend);

  body.innerHTML = '';
  body.appendChild(wrap);

  _deptDonutChart = ModusCharts.donut(canvas, {
    labels: departments.map(d => d.department),
    data: departments.map(d => d.spend_usd || 0),
    centerText: '$' + (total >= 1000 ? (total / 1000).toFixed(1) + 'k' : total.toFixed(0)),
    centerSub: 'MTD Spend',
  });

  _attachResize('fin-dept-donut', () => _deptDonutChart);
}

// ── Burn Rate Table ──────────────────────────────────────────────────────────

function _renderBurnRate(data, priorData) {
  const body = document.getElementById('fin-burn-rate-body');
  if (!body) return;

  const teams = (data && data.teams) ? data.teams : [];

  if (!teams.length) {
    setTileEmpty('fin-burn-rate', {
      icon: 'fa-solid fa-fire',
      title: 'No budget data',
      description: 'Set team budgets to enable burn tracking.',
    });
    return;
  }

  _renderBurnRateInner(body, teams, priorData);
}

function _renderBurnRateInner(body, teams, priorData) {
  // Build a map of prior teams by team_name for delta calculation
  const priorMap = {};
  if (_compareMode && priorData && priorData.teams && Array.isArray(priorData.teams)) {
    priorData.teams.forEach(t => {
      if (t.team_name) priorMap[t.team_name] = t;
    });
  }

  // Helper to compute and render spend delta
  const renderSpendDelta = (current, prior) => {
    if (!_compareMode || !prior) return '';
    const currentSpend = parseFloat(current || 0);
    const priorSpend = parseFloat(prior || 0);
    if (priorSpend === 0) return '<span style="color:var(--muted)">—</span>';
    const delta = ((currentSpend - priorSpend) / priorSpend) * 100;
    const sign = delta > 0 ? '+' : '';
    const cls = delta > 0 ? 'up' : 'down';
    const icon = delta > 0 ? '↑' : '↓';
    return `<span style="color:var(--${cls});font-size:11px">${sign}${delta.toFixed(1)}% ${icon}</span>`;
  };

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Team</th>
            <th>Dept</th>
            <th style="text-align:right">Budget</th>
            <th style="text-align:right">Spend</th>
            ${_compareMode ? '<th style="text-align:right">Δ Spend</th>' : ''}
            <th style="text-align:right">Projected</th>
            <th style="text-align:center">Burn %</th>
            <th style="text-align:center">Status</th>
          </tr>
        </thead>
        <tbody>
          ${teams.map(t => {
            const prior = priorMap[t.team_name];
            const riskColor = t.risk === 'over-budget' ? 'var(--ds-red-500, var(--danger))' : t.risk === 'at-risk' ? 'var(--ds-amber-500, #f59e0b)' : 'var(--ds-green-500, var(--accent))';
            const barWidth = Math.min(t.burn_pct || 0, 100);
            const badgeClass = t.risk === 'over-budget' ? 'ds-badge-red' : t.risk === 'at-risk' ? 'ds-badge-amber' : 'ds-badge-green';
            return `<tr>
              <td style="font-weight:500">${esc(t.team_name || '')}</td>
              <td style="color:var(--ds-gray-500)">${esc(t.department || '-')}</td>
              <td style="text-align:right">${t.budget_monthly_usd ? '$' + t.budget_monthly_usd.toFixed(0) : '-'}</td>
              <td style="text-align:right">$${(t.current_spend_usd || 0).toFixed(2)}</td>
              ${_compareMode ? `<td style="text-align:right">${renderSpendDelta(t.current_spend_usd, prior?.current_spend_usd)}</td>` : ''}
              <td style="text-align:right">$${(t.projected_eom_usd || 0).toFixed(2)}</td>
              <td style="text-align:center">
                <div style="display:flex;align-items:center;gap:6px;justify-content:center">
                  <div style="width:60px;height:6px;background:var(--ds-gray-200, var(--border));border-radius:3px;overflow:hidden">
                    <div style="width:${barWidth}%;height:100%;background:${riskColor};border-radius:3px;transition:width 0.8s ease-out"></div>
                  </div>
                  <span style="font-size:10px">${(t.burn_pct || 0).toFixed(0)}%</span>
                </div>
              </td>
              <td style="text-align:center"><span class="ds-badge ${badgeClass}">${esc(t.risk || 'on-track')}</span></td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

// ── Provider Bar Chart ───────────────────────────────────────────────────────

function _renderProviderBar(data) {
  const body = document.getElementById('fin-provider-bar-body');
  if (!body) return;

  _destroyChart('_providerBarChart');

  const departments = (data && data.spend_by_department) ? data.spend_by_department : [];

  if (!departments.length) {
    setTileEmpty('fin-provider-bar', {
      icon: 'fa-solid fa-server',
      title: 'No provider data',
      description: 'Provider spend data will appear after usage is recorded.',
    });
    return;
  }

  _renderProviderBarInner(body, departments);
}

function _renderProviderBarInner(body, departments) {
  if (typeof ModusCharts === 'undefined') {
    // Fallback: simple horizontal bars
    const maxSpend = Math.max(...departments.map(d => d.spend_usd || 0), 1);
    body.innerHTML = departments.map(d => {
      const pct = ((d.spend_usd || 0) / maxSpend * 100).toFixed(0);
      return `<div style="padding:6px 12px">
        <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
          <span>${esc(d.department)}</span>
          <span style="font-weight:500">$${(d.spend_usd || 0).toFixed(2)}</span>
        </div>
        <div style="height:6px;background:var(--border);border-radius:3px;overflow:hidden">
          <div style="width:${pct}%;height:100%;background:var(--accent);border-radius:3px;transition:width 0.5s ease"></div>
        </div>
      </div>`;
    }).join('');
    return;
  }

  const canvas = document.createElement('canvas');
  canvas.style.width = '100%';
  canvas.style.height = '100%';
  body.innerHTML = '';
  body.style.padding = '8px 16px 16px';
  body.appendChild(canvas);

  _providerBarChart = ModusCharts.bar(canvas, {
    labels: departments.map(d => d.department),
    datasets: [{ label: 'Spend (USD)', data: departments.map(d => d.spend_usd || 0) }],
    horizontal: true,
  });

  _attachResize('fin-provider-bar', () => _providerBarChart);
}

// ── Forecast Panel ───────────────────────────────────────────────────────────

function _renderForecast(data) {
  const body = document.getElementById('fin-forecast-body');
  if (!body) return;

  if (!data) {
    setTileEmpty('fin-forecast', {
      icon: 'fa-solid fa-crystal-ball',
      title: 'Forecast unavailable',
      description: 'Forecast data will appear after a few days of usage.',
    });
    return;
  }

  _renderForecastInner(body, data);
}

function _renderForecastInner(body, f) {
  const fmt = v => (v || 0) >= 10000 ? '$' + ((v || 0) / 1000).toFixed(1) + 'k' : '$' + (v || 0).toFixed(2);
  const trendColor = (f.trend_daily || 0) > 0 ? 'var(--ds-red-500, var(--danger))' : 'var(--ds-green-500, var(--accent))';

  const methodLabel = {
    'ols+holt+seasonal': 'Built-In (OLS + Holt + Seasonal)',
    'naive_average': 'Built-In (Limited Data)',
    'custom_ml': 'Your ML Endpoint',
  }[f.method] || (f.method && f.method.startsWith('ai_') ? 'AI-Powered (' + f.method.replace('ai_', '') + ')' : (f.method || 'auto'));

  const confidenceLevel = (f.r_squared || 0) >= 0.7 ? 'High' : (f.r_squared || 0) >= 0.4 ? 'Medium' : 'Low';

  body.innerHTML = `
    <div style="padding:8px 12px">
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:16px">
        <div style="text-align:center">
          <div style="font-size:10px;text-transform:uppercase;color:var(--ds-gray-500, var(--muted));letter-spacing:0.5px">End of Month</div>
          <div style="font-size:22px;font-weight:700;color:var(--text)">${fmt(f.forecast_eom)}</div>
          <div style="font-size:9px;color:var(--ds-gray-500, var(--muted))">${fmt(f.confidence_low_eom)} \u2014 ${fmt(f.confidence_high_eom)}</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:10px;text-transform:uppercase;color:var(--ds-gray-500, var(--muted));letter-spacing:0.5px">End of Quarter</div>
          <div style="font-size:22px;font-weight:700;color:var(--text)">${fmt(f.forecast_eoq)}</div>
          <div style="font-size:9px;color:var(--ds-gray-500, var(--muted))">Projected</div>
        </div>
        <div style="text-align:center">
          <div style="font-size:10px;text-transform:uppercase;color:var(--ds-gray-500, var(--muted));letter-spacing:0.5px">End of Year</div>
          <div style="font-size:22px;font-weight:700;color:var(--text)">${fmt(f.forecast_eoy)}</div>
          <div style="font-size:9px;color:var(--ds-gray-500, var(--muted))">Projected</div>
        </div>
      </div>
      <div style="display:flex;gap:16px;font-size:11px;flex-wrap:wrap;margin-bottom:8px">
        <div><span style="color:var(--ds-gray-500, var(--muted))">MTD Actual:</span> <strong>${fmt(f.mtd_actual)}</strong></div>
        <div><span style="color:var(--ds-gray-500, var(--muted))">R\u00b2:</span> <strong>${(f.r_squared || 0).toFixed(2)}</strong></div>
        <div><span style="color:var(--ds-gray-500, var(--muted))">Daily Trend:</span> <strong style="color:${trendColor}">${(f.trend_daily || 0) > 0 ? '+' : ''}$${(f.trend_daily || 0).toFixed(2)}/day</strong></div>
        ${f.budget_monthly ? `<div><span style="color:var(--ds-gray-500, var(--muted))">Budget:</span> <strong>${fmt(f.budget_monthly)}</strong></div>` : ''}
      </div>
      <div style="display:flex;justify-content:space-between;font-size:11px;padding:6px 0;border-top:1px solid var(--ds-gray-200, var(--border))">
        <span style="color:var(--ds-gray-500, var(--muted))">Method</span>
        <span style="font-weight:500">${esc(methodLabel)}</span>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:11px;padding:6px 0">
        <span style="color:var(--ds-gray-500, var(--muted))">Confidence</span>
        <span style="font-weight:500">${confidenceLevel} (R\u00b2 ${(f.r_squared || 0).toFixed(2)})</span>
      </div>
      ${f.narrative ? `<div style="margin-top:8px;padding:10px;border-radius:6px;background:var(--ds-surface, rgba(255,255,255,0.02));font-size:11px;line-height:1.5;color:var(--ds-gray-500, var(--muted))">${esc(f.narrative)}</div>` : ''}
      ${f.patterns_detected && f.patterns_detected.length ? `<div style="margin-top:6px;font-size:10px;color:var(--ds-gray-500, var(--muted))">Patterns: ${f.patterns_detected.map(p => esc(p)).join(' | ')}</div>` : ''}
    </div>
  `;
}

// ── Breach Predictions ───────────────────────────────────────────────────────

function _renderBreach(data) {
  const body = document.getElementById('fin-breach-body');
  if (!body) return;

  if (!data || !Array.isArray(data)) {
    setTileEmpty('fin-breach', {
      icon: 'fa-solid fa-triangle-exclamation',
      title: 'No breach data',
      description: 'Set team budgets to enable breach predictions.',
    });
    return;
  }

  _renderBreachInner(body, data);
}

function _renderBreachInner(body, breaches) {
  if (!breaches.length) {
    body.innerHTML = `
      <div style="padding:16px 12px;text-align:center">
        <div style="font-size:20px;margin-bottom:8px;opacity:0.6"><i class="fa-solid fa-shield-check"></i></div>
        <div style="color:var(--ds-green-500, var(--accent));font-weight:500;font-size:13px">No budget breaches predicted</div>
        <div style="color:var(--ds-gray-500, var(--muted));font-size:11px;margin-top:4px">All teams are within budget</div>
      </div>
    `;
    return;
  }

  body.innerHTML = `<div style="padding:4px 12px">${breaches.map(b => {
    const isBreach = b.already_breached;
    const color = isBreach ? 'var(--ds-red-500, var(--danger))' : 'var(--ds-amber-500, #f59e0b)';
    const icon = isBreach ? 'fa-solid fa-circle-xmark' : 'fa-solid fa-triangle-exclamation';
    const badgeClass = isBreach ? 'ds-badge-red' : 'ds-badge-amber';
    const badgeText = isBreach ? 'BREACHED' : (b.confidence || 'likely');

    return `<div style="padding:8px 0;border-bottom:1px solid var(--ds-gray-200, var(--border))">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="display:flex;align-items:center;gap:6px;color:${color};font-weight:500;font-size:12px">
          <i class="${icon}" style="font-size:11px"></i>
          ${esc(b.team_name || 'Unknown')}
        </span>
        <span class="ds-badge ${badgeClass}">${esc(badgeText)}</span>
      </div>
      <div style="font-size:10px;color:var(--ds-gray-500, var(--muted));margin-top:3px">
        ${isBreach
          ? 'Over budget by $' + (b.projected_overage || 0).toFixed(2)
          : 'Breach by ' + esc(b.breach_date || '?') + ' (+$' + (b.projected_overage || 0).toFixed(2) + ')'
        }
      </div>
    </div>`;
  }).join('')}</div>`;
}

// ── Team Budget Gauges ───────────────────────────────────────────────────────

function _renderTeamGauges(data) {
  const body = document.getElementById('fin-team-gauges-body');
  if (!body) return;

  const teams = (data && data.teams) ? data.teams : [];
  const budgetTeams = teams.filter(t => (t.budget_monthly_usd || 0) > 0).slice(0, 8);

  if (!budgetTeams.length) {
    setTileEmpty('fin-team-gauges', {
      icon: 'fa-solid fa-gauge-high',
      title: 'No teams with budgets',
      description: 'Set team budgets to see utilization gauges.',
    });
    return;
  }

  _renderGaugesInner(body, budgetTeams);
}

function _renderGaugesInner(body, teams) {
  if (typeof ModusCharts !== 'undefined' && typeof ModusCharts.gauge === 'function') {
    body.innerHTML = '';
    body.style.cssText = 'display:flex;gap:16px;flex-wrap:wrap;justify-content:center;padding:12px';

    teams.forEach(t => {
      const gaugeEl = document.createElement('div');
      gaugeEl.style.textAlign = 'center';
      body.appendChild(gaugeEl);

      const color = t.risk === 'over-budget' ? '#ef4444' : t.risk === 'at-risk' ? '#f59e0b' : '#10b981';
      ModusCharts.gauge(gaugeEl, {
        value: Math.min(t.burn_pct || 0, 100),
        max: 100,
        label: t.team_name || 'Team',
        suffix: '%',
        color: color,
        size: 100,
      });
    });
    return;
  }

  // Fallback: progress bars
  body.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill, minmax(160px, 1fr));gap:12px;padding:12px">
    ${teams.map(t => {
      const pct = Math.min(t.burn_pct || 0, 100);
      const color = t.risk === 'over-budget' ? '#ef4444' : t.risk === 'at-risk' ? '#f59e0b' : '#10b981';
      return `<div style="text-align:center">
        <div style="font-size:11px;font-weight:500;color:var(--text);margin-bottom:6px">${esc(t.team_name || 'Team')}</div>
        <div style="height:8px;background:var(--border);border-radius:4px;overflow:hidden;margin-bottom:4px">
          <div style="width:${pct}%;height:100%;background:${color};border-radius:4px;transition:width 0.8s ease-out"></div>
        </div>
        <div style="font-size:10px;color:var(--muted)">${pct.toFixed(0)}% used</div>
      </div>`;
    }).join('')}
  </div>`;
}

// ── Scheduled Reports Table ──────────────────────────────────────────────────

function _renderReports(data) {
  const body = document.getElementById('fin-reports-body');
  if (!body) return;

  if (!data || !Array.isArray(data)) {
    setTileEmpty('fin-reports', {
      icon: 'fa-solid fa-clock',
      title: 'No scheduled reports',
      description: 'Schedule automated chargeback, burn rate, or forecast reports.',
    });
    return;
  }

  if (!data.length) {
    setTileEmpty('fin-reports', {
      icon: 'fa-solid fa-clock',
      title: 'No scheduled reports',
      description: 'Schedule automated chargeback, burn rate, or forecast reports delivered to Slack or any webhook.',
    });
    return;
  }

  _renderReportsInner(body, data);
}

function _renderReportsInner(body, reports) {
  const typeLabels = {
    chargeback: 'Chargeback',
    burn_rate: 'Burn Rate',
    forecast: 'Forecast',
    variance: 'Variance',
    audit_trail: 'Audit',
  };

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Name</th>
            <th>Type</th>
            <th>Frequency</th>
            <th>Channel</th>
            <th>Last Run</th>
            <th style="text-align:center">Status</th>
          </tr>
        </thead>
        <tbody>
          ${reports.map(r => {
            const typeLabel = typeLabels[r.report_type] || r.report_type || '';
            const channelIcon = r.delivery_channel === 'slack' ? '\uD83D\uDCAC' : '\uD83D\uDD17';
            const statusBadge = r.is_active ? 'ds-badge-green' : 'ds-badge-default';
            const statusText = r.is_active ? 'Active' : 'Paused';
            return `<tr>
              <td style="font-weight:500">${esc(r.name || '')}</td>
              <td><span class="ds-badge ds-badge-blue">${esc(typeLabel)}</span></td>
              <td style="text-transform:capitalize">${esc(r.schedule || '')}</td>
              <td>${channelIcon} ${esc(r.delivery_channel || '')}</td>
              <td style="color:var(--ds-gray-500, var(--muted))">${r.last_run_at ? r.last_run_at.slice(0, 16).replace('T', ' ') : 'Never'}</td>
              <td style="text-align:center"><span class="ds-badge ${statusBadge}">${statusText}</span></td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

// ── Chargeback Tile ─────────────────────────────────────────────────────────

function _renderChargeback(data) {
  const body = document.getElementById('fin-chargeback-body');
  if (!body) return;

  if (!data || (Array.isArray(data) && data.length === 0)) {
    setTileEmpty('fin-chargeback', {
      icon: 'fa-solid fa-money-bill-transfer',
      title: 'No chargeback data',
      description: 'Click Generate to create chargeback allocations from current usage.',
    });
    return;
  }

  // GET /finance/chargeback returns one row per team/app/provider/model line
  // ({team_slug, team_name, cost_center_code, department, period, cost, ...}).
  // The tile shows allocation per cost center and team, so roll the lines up.
  const lines = Array.isArray(data) ? data : [];
  const grouped = new Map();
  for (const l of lines) {
    const key = `${l.cost_center_code}|${l.team_slug}`;
    const g = grouped.get(key) || { cost_center_code: l.cost_center_code, team_name: l.team_name, period: l.period, amount: 0 };
    g.amount += Number(l.cost ?? 0);
    grouped.set(key, g);
  }
  const items = Array.from(grouped.values()).sort((a, b) => b.amount - a.amount);
  const total = items.reduce((s, i) => s + i.amount, 0);

  body.innerHTML = `
    <div style="padding:12px">
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Total Allocated</div>
      <div style="font-size:24px;font-weight:700;color:var(--text);margin-bottom:16px">$${total.toFixed(2)}</div>
      <table class="ds-table" style="width:100%;font-size:11px">
        <thead>
          <tr><th>Cost Center</th><th>Team</th><th style="text-align:right">Allocated</th><th style="text-align:right">Period</th></tr>
        </thead>
        <tbody>
          ${items.slice(0, 20).map(i => `
            <tr>
              <td style="font-weight:500">${esc(i.cost_center_code || '—')}</td>
              <td style="color:var(--muted)">${esc(i.team_name || '—')}</td>
              <td style="text-align:right;font-family:var(--mono)">$${i.amount.toFixed(2)}</td>
              <td style="text-align:right;font-size:10px;color:var(--muted)">${esc(i.period || '—')}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
  `;
}

// ── Reconciliation Tile ──────────────────────────────────────────────────────

/**
 * GET /api/v1/finance/reconciliation returns one row per imported invoice
 * total: {provider, service, period_start, period_end, actual_cost_usd,
 * inferred_cost_usd, delta_usd, delta_pct}. Money arrives as decimal strings.
 * Rows with |variance| of 2% or less count as matched.
 */
const RECON_MATCH_PCT = 2;

function _renderReconciliation(data) {
  const body = document.getElementById('fin-reconciliation-body');
  if (!body) return;

  const rows = Array.isArray(data) ? data : [];
  if (rows.length === 0) {
    setTileEmpty('fin-reconciliation', {
      icon: 'fa-solid fa-scale-balanced',
      title: 'No invoice totals imported',
      description: 'Modus does not fetch provider invoices. Use Import CSV (columns: provider, period_start, period_end, actual_cost_usd) to compare what a provider billed with what Modus tracked.',
    });
    return;
  }

  const billed = rows.reduce((t, r) => t + Number(r.actual_cost_usd || 0), 0);
  const tracked = rows.reduce((t, r) => t + Number(r.inferred_cost_usd || 0), 0);
  const variance = billed - tracked;
  const pct = billed > 0 ? (variance / billed) * 100 : 0;
  const matched = Math.abs(pct) <= RECON_MATCH_PCT;
  const statusColor = matched ? 'var(--accent)' : 'var(--warn)';
  const money = (n) => '$' + Number(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const signed = (n) => (n >= 0 ? '+' : '-') + money(Math.abs(n));
  const day = (iso) => (iso ? String(iso).slice(0, 10) : '\u2014');

  body.innerHTML = `
    <div style="padding:12px;display:flex;flex-direction:column;gap:16px">
      <div style="display:flex;align-items:center;gap:12px">
        <div style="width:12px;height:12px;border-radius:50%;background:${statusColor}"></div>
        <div>
          <div style="font-size:14px;font-weight:600;color:var(--text);text-transform:uppercase">${matched ? 'Matched' : 'Variance'}</div>
          <div style="font-size:11px;color:var(--muted)">${rows.length} imported invoice total${rows.length === 1 ? '' : 's'}</div>
        </div>
      </div>

      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:100px;padding:10px;border-radius:8px;background:var(--surface);border:1px solid var(--border);text-align:center">
          <div style="font-size:10px;text-transform:uppercase;color:var(--muted)">Tracked</div>
          <div style="font-size:18px;font-weight:700;color:var(--text)">${money(tracked)}</div>
        </div>
        <div style="flex:1;min-width:100px;padding:10px;border-radius:8px;background:var(--surface);border:1px solid var(--border);text-align:center">
          <div style="font-size:10px;text-transform:uppercase;color:var(--muted)">Billed</div>
          <div style="font-size:18px;font-weight:700;color:var(--text)">${money(billed)}</div>
        </div>
        <div style="flex:1;min-width:100px;padding:10px;border-radius:8px;background:var(--surface);border:1px solid var(--border);text-align:center">
          <div style="font-size:10px;text-transform:uppercase;color:var(--muted)">Variance</div>
          <div style="font-size:18px;font-weight:700;color:${statusColor}">${signed(variance)}</div>
          <div style="font-size:10px;color:var(--muted)">${pct.toFixed(2)}% of billed</div>
        </div>
      </div>

      <div style="overflow-x:auto">
        <table class="ds-table" style="width:100%;font-size:12px">
          <thead><tr><th>Provider</th><th>Period</th><th style="text-align:right">Billed</th><th style="text-align:right">Tracked</th><th style="text-align:right">Variance</th></tr></thead>
          <tbody>
            ${rows.map(r => {
              const d = Number(r.delta_usd || 0);
              const ok = r.delta_pct == null || Math.abs(r.delta_pct) <= RECON_MATCH_PCT;
              return `<tr>
                <td>${esc(r.provider)}${r.service ? ' <span style="color:var(--muted)">' + esc(r.service) + '</span>' : ''}</td>
                <td style="font-family:var(--mono);font-size:11px">${esc(day(r.period_start))} \u2192 ${esc(day(r.period_end))}</td>
                <td style="text-align:right">${money(r.actual_cost_usd)}</td>
                <td style="text-align:right">${money(r.inferred_cost_usd || 0)}</td>
                <td style="text-align:right;color:${ok ? 'var(--accent)' : 'var(--warn)'}">${signed(d)}${r.delta_pct == null ? '' : ' (' + Number(r.delta_pct).toFixed(1) + '%)'}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

/** Pick a CSV file and POST it to /api/v1/finance/reconciliation/import/csv. */
function _importBillingActuals() {
  const input = document.createElement('input');
  input.type = 'file';
  input.accept = '.csv,text/csv';
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    try {
      const resp = await fetch('/api/v1/finance/reconciliation/import/csv', {
        method: 'POST',
        headers: authHeaders(), // no Content-Type: the browser sets the multipart boundary
        body: form,
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const d = body.detail;
        const lines = d && Array.isArray(d.errors)
          ? d.errors.slice(0, 5).map(e => `row ${e.row}${e.field ? ' ' + e.field : ''}: ${e.message}`).join('\n')
          : (typeof d === 'string' ? d : `HTTP ${resp.status}`);
        toast(`Import rejected, nothing was imported.\n${lines}`, 'error');
        return;
      }
      toast(`Imported ${body.imported} row(s): ${body.created} new, ${body.updated} updated, ${body.unchanged} unchanged.`, 'success');
      loadData();
    } catch (err) {
      toast('Import failed: ' + err.message, 'error');
    }
  });
  input.click();
}

// ── Cost Centers Tile ───────────────────────────────────────────────────────

function _renderCostCenters(items) {
  const body = document.getElementById('fin-cost-centers-body');
  if (!body) return;

  if (!items || items.length === 0) {
    setTileEmpty('fin-cost-centers', {
      icon: 'fa-solid fa-sitemap',
      title: 'No cost centers',
      description: 'Click + Assign to create cost centers and assign teams to them for chargeback.',
    });
    return;
  }

  body.innerHTML = `
    <div style="padding:12px">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr><th>Cost Center</th><th>Code</th><th>Owner</th><th>Teams</th><th style="text-align:right">Monthly Budget</th></tr>
        </thead>
        <tbody>
          ${items.map(c => `
            <tr>
              <td style="font-weight:500">${esc(c.name || '—')}</td>
              <td style="font-family:var(--mono);font-size:11px">${esc(c.code || '—')}</td>
              <td style="color:var(--muted)">${esc(c.budget_owner_name || c.budget_owner_email || '—')}</td>
              <td style="font-size:11px">${c.team_count || 0} team${c.team_count !== 1 ? 's' : ''}</td>
              <td style="text-align:right;font-family:var(--mono)">${c.budget_monthly_usd != null ? '$' + Number(c.budget_monthly_usd).toFixed(2) : '—'}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
  `;
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

// ── Action handlers ─────────────────────────────────────────────────────────

async function _generateChargeback() {
  // Inline confirmation modal — no native confirm/alert (GAP-6 polish)
  openModal({
    title: 'Generate Chargeback',
    maxWidth: '420px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="padding:8px 0;display:flex;flex-direction:column;gap:14px">
          <div style="font-size:13px;color:var(--text);line-height:1.5">
            Generate chargeback allocations for the current period?
            This computes per-cost-center spend and writes new allocation records.
          </div>
          <div id="cb-result" style="display:none"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="ds-btn ds-btn-ghost" id="cb-cancel-btn">Cancel</button>
            <button class="ds-btn ds-btn-primary" id="cb-confirm-btn">Generate</button>
          </div>
        </div>
      `;
      const cancelBtn = body.querySelector('#cb-cancel-btn');
      const confirmBtn = body.querySelector('#cb-confirm-btn');
      const result = body.querySelector('#cb-result');

      const closeAll = () => document.querySelectorAll('.ds-modal-backdrop').forEach(el => el.remove());

      cancelBtn.addEventListener('click', closeAll);
      confirmBtn.addEventListener('click', async () => {
        confirmBtn.disabled = true;
        confirmBtn.textContent = 'Generating\u2026';
        try {
          const data = await _financePost('chargeback/generate', { period: 'current_month' });
          if (data) {
            result.style.display = 'block';
            result.innerHTML = '<div style="color:var(--accent);font-size:12px">Chargeback generated successfully.</div>';
            setTimeout(() => { closeAll(); loadData(); }, 700);
          } else {
            throw new Error('Empty response');
          }
        } catch (e) {
          result.style.display = 'block';
          result.innerHTML = '<div style="color:var(--danger);font-size:12px">Error: ' + esc(e.message || 'failed') + '</div>';
          confirmBtn.disabled = false;
          confirmBtn.textContent = 'Generate';
        }
      });
    },
  });
}

function _openCostCenterModal() {
  openModal({
    title: 'Assign Cost Center',
    maxWidth: '480px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:14px;padding:4px 0">
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Cost Center Name</label>
            <input class="ds-input" id="cc-name" placeholder="Engineering" style="width:100%">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Code</label>
            <input class="ds-input" id="cc-code" placeholder="ENG-001" style="width:100%">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Owner Email</label>
            <input class="ds-input" id="cc-owner" placeholder="finance@example.com" style="width:100%">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:var(--text);display:block;margin-bottom:4px">Monthly Budget ($)</label>
            <input class="ds-input" id="cc-budget" type="number" step="0.01" placeholder="10000" style="width:100%">
          </div>
          <div id="cc-result" style="display:none"></div>
          <button class="ds-btn ds-btn-primary" id="cc-save-btn">Create Cost Center</button>
        </div>
      `;
      body.querySelector('#cc-save-btn').addEventListener('click', _saveCostCenter);
    },
  });
}

async function _saveCostCenter() {
  const btn = document.getElementById('cc-save-btn');
  const result = document.getElementById('cc-result');
  const showError = (msg) => {
    if (result) {
      result.style.display = 'block';
      result.innerHTML = '<div style="color:var(--danger);font-size:12px">' + esc(msg) + '</div>';
    }
  };

  const payload = {
    name: document.getElementById('cc-name')?.value.trim(),
    code: document.getElementById('cc-code')?.value.trim(),
  };
  // Field names follow POST /finance/cost-centers (CostCenterRequest).
  const owner = document.getElementById('cc-owner')?.value.trim();
  if (owner) payload.budget_owner_email = owner;
  const budget = parseFloat(document.getElementById('cc-budget')?.value || '0');
  if (budget > 0) payload.budget_monthly_usd = budget.toFixed(2);
  if (!payload.name) { showError('Name is required'); return; }
  if (!payload.code) { showError('Code is required'); return; }

  if (btn) { btn.disabled = true; btn.textContent = 'Saving\u2026'; }

  try {
    const data = await _financePost('cost-centers', payload);
    if (data) {
      if (result) {
        result.style.display = 'block';
        result.innerHTML = '<div style="color:var(--accent);font-size:12px">Cost center created.</div>';
      }
      setTimeout(() => {
        document.querySelectorAll('.ds-modal-backdrop').forEach(el => el.remove());
        loadData();
      }, 600);
    } else {
      showError('Failed to create cost center');
      if (btn) { btn.disabled = false; btn.textContent = 'Create Cost Center'; }
    }
  } catch (e) {
    showError('Error: ' + (e.message || 'failed'));
    if (btn) { btn.disabled = false; btn.textContent = 'Create Cost Center'; }
  }
}

// ── Error helper ─────────────────────────────────────────────────────────────

function _setAllTilesError() {
  const ids = [
    'fin-kpis', 'fin-spend-trend', 'fin-dept-donut', 'fin-burn-rate',
    'fin-provider-bar', 'fin-forecast', 'fin-breach', 'fin-team-gauges',
    'fin-reports', 'fin-chargeback', 'fin-reconciliation', 'fin-cost-centers',
  ];
  ids.forEach(id => setTileError(id, 'Failed to load data', () => loadData()));
}

