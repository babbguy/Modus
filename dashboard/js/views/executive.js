/**
 * Modus Dashboard v2 — Executive View
 * Renders the Executive dashboard with 9 Gridstack tiles:
 * KPIs, Narrative, Forecast, Savings, Chargeback, Enforcement Mix,
 * ROI Breakdown, Model Risk, and Topology Discovery.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError, setTileMeta } from '../tile.js';
import { apiFetch, rawFetch, esc } from '../api.js';
import { fmtCost, fmtTokens, fmtNum, fmtDate, timeSince, providerColor } from '../format.js';
import { get, subscribe, unsubscribe } from '../state.js';
import { openModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _forecastChart = null;   // Chart.js instance for forecast
let _savingsChart = null;    // Chart.js instance for savings over time
let _enforcementChart = null; // Chart.js instance for enforcement donut
let _grid = null;            // Gridstack instance
let _container = null;       // DOM container ref
let _destroyed = false;      // Guard against async renders after destroy
let _resizeHandlers = {};    // tile-resize listener refs { tileId: handler }
let _narrativeCache = null;  // Cached narrative text
let _narrativeTs = 0;        // Cache timestamp

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Executive view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  // Initialize grid
  _grid = initGrid(container, 'executive');

  // Create tiles
  const tiles = [
    _createKpiTile(),
    _createNarrativeTile(),
    _createForecastTile(),
    _createSavingsTile(),
    _createChargebackTile(),
    _createEnforcementTile(),
    _createRoiTile(),
    _createModelRiskTile(),
    _createTopologyTile(),
  ];

  // Load saved or default layout
  const layout = loadLayout('executive', LAYOUTS.executive);

  // Add tiles to grid
  addTiles(_grid, tiles, layout);

  // Subscribe to period changes (sync with overview's date range)
  subscribe('currentDays', _onDaysChange);

  // Fetch and render data
  await loadData();
}

function _onDaysChange() {
  // Invalidate narrative cache when period changes
  _narrativeCache = null;
  _narrativeTs = 0;
  loadData();
}

/**
 * Tear down the Executive view, cleaning up charts, subscriptions, and DOM.
 */
export function destroy() {
  _destroyed = true;

  unsubscribe('currentDays', _onDaysChange);

  // Destroy chart instances
  _destroyChart('_forecastChart');
  _destroyChart('_savingsChart');
  _destroyChart('_enforcementChart');

  // Remove resize listeners
  for (const [tileId, handler] of Object.entries(_resizeHandlers)) {
    const body = document.getElementById(`${tileId}-body`);
    if (body) body.removeEventListener('tile-resize', handler);
  }
  _resizeHandlers = {};

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

function _destroyChart(name) {
  const chart = name === '_forecastChart' ? _forecastChart :
                name === '_savingsChart' ? _savingsChart :
                name === '_enforcementChart' ? _enforcementChart : null;
  if (chart) {
    try { chart.destroy(); } catch (_) { /* already destroyed */ }
  }
  if (name === '_forecastChart') _forecastChart = null;
  if (name === '_savingsChart') _savingsChart = null;
  if (name === '_enforcementChart') _enforcementChart = null;
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createKpiTile() {
  const tile = createTile({
    id: 'exec-kpis',
    title: '',
    className: 'kpi-row',
  });
  // Hide the header for the KPI row — the KPIs ARE the content
  const header = tile.querySelector('.gs-tile-header');
  if (header) header.style.display = 'none';
  return tile;
}

function _createNarrativeTile() {
  return createTile({
    id: 'exec-narrative',
    title: 'Executive Summary',
    icon: 'fa-solid fa-chart-pie',
    iconBg: 'rgba(159,122,255,0.1)',
    iconColor: 'var(--purple, #a855f7)',
    meta: 'AI-generated \u00b7 updated hourly',
  });
}

function _createForecastTile() {
  return createTile({
    id: 'exec-forecast',
    title: 'Spend Forecast',
    icon: 'fa-solid fa-chart-line',
    iconBg: 'rgba(159,122,255,0.1)',
    iconColor: 'var(--purple, #a855f7)',
    meta: 'cumulative MTD + projection',
  });
}

function _createSavingsTile() {
  return createTile({
    id: 'exec-savings',
    title: 'Savings Over Time',
    icon: 'fa-solid fa-piggy-bank',
    iconBg: 'rgba(159,122,255,0.1)',
    iconColor: 'var(--purple, #a855f7)',
    meta: 'USD saved by enforcement',
  });
}

function _createChargebackTile() {
  return createTile({
    id: 'exec-chargeback',
    title: 'Chargeback by Team',
    icon: 'fa-solid fa-dollar-sign',
    iconBg: 'rgba(159,122,255,0.1)',
    iconColor: 'var(--purple, #a855f7)',
    meta: 'month to date',
    filterable: true,
    filterPlaceholder: 'Filter teams\u2026',
    onFilter: (q) => _filterRows('exec-chargeback-body', q),
    actions: [
      {
        label: '',
        icon: 'fa-solid fa-download',
        onclick: () => _exportChargeback(),
      },
    ],
  });
}

function _createEnforcementTile() {
  return createTile({
    id: 'exec-enforcement',
    title: 'Enforcement Mix',
    icon: 'fa-solid fa-shield-halved',
    iconBg: 'rgba(159,122,255,0.1)',
    iconColor: 'var(--purple, #a855f7)',
    meta: 'action distribution',
  });
}

function _createRoiTile() {
  return createTile({
    id: 'exec-roi',
    title: 'ROI Breakdown',
    icon: 'fa-solid fa-hand-holding-dollar',
    iconBg: 'rgba(159,193,49,0.1)',
    iconColor: 'var(--accent)',
    meta: 'enforcement savings vs platform cost',
  });
}

function _createModelRiskTile() {
  return createTile({
    id: 'exec-model-risk',
    title: 'Model Risk Concentration',
    icon: 'fa-solid fa-triangle-exclamation',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
    meta: 'top cost drivers',
  });
}

function _createTopologyTile() {
  return createTile({
    id: 'exec-topology',
    title: 'Topology Discovery',
    icon: 'fa-solid fa-sitemap',
    iconBg: 'rgba(0,153,255,0.1)',
    iconColor: 'var(--accent2, #0099ff)',
    meta: 'AI-powered architectural mapping',
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function loadData() {
  if (_destroyed) return;

  // Set loading states on all tiles
  setTileLoading('exec-kpis', 'cards');
  setTileLoading('exec-narrative', 'text');
  setTileLoading('exec-forecast', 'chart');
  setTileLoading('exec-savings', 'chart');
  setTileLoading('exec-chargeback', 'table');
  setTileLoading('exec-enforcement', 'chart');
  setTileLoading('exec-roi', 'text');
  setTileLoading('exec-model-risk', 'table');
  setTileLoading('exec-topology', 'cards');

  // Use the global period from overview tabs (synced via state)
  const days = get('currentDays') || 30;

  // Parallel data fetches — use SAME endpoints as overview for consistency
  const [summary, costOverTime, teams, models, execCharts] = await Promise.all([
    apiFetch('summary').catch(() => null),
    apiFetch('cost-over-time', { days, granularity: 'daily' }).catch(() => null),
    apiFetch('by-team', { days }).catch(() => null),
    apiFetch('top-models', { days, limit: 5 }).catch(() => null),
    apiFetch('executive-charts', { days }).catch(() => null),
  ]);

  if (_destroyed) return;

  // Separate fetches for non-dashboard endpoints
  const [forecast, roi, financeSummary] = await Promise.all([
    rawFetch('/api/v1/insights/forecast').then(r => r && r.ok ? r.json() : null).catch(() => null),
    rawFetch('/api/v1/reports/roi').then(r => r && r.ok ? r.json() : null).catch(() => null),
    rawFetch('/api/v1/finance/summary').then(r => r && r.ok ? r.json() : null).catch(() => null),
  ]);

  if (_destroyed) return;

  // Period cost from cost-over-time (SAME source as overview chart — guarantees match)
  const periodCost = Array.isArray(costOverTime) ? costOverTime.reduce((s, p) => s + parseFloat(p.cost || 0), 0) : 0;

  // Sum forecast EOM across all team forecasts
  const totalForecastEom = Array.isArray(forecast) ?
    forecast.reduce((s, f) => s + (f.forecast_eom ?? 0), 0) :
    (forecast?.forecast_eom ?? 0);

  // Build KPI data — uses summary for MTD (authoritative), cost-over-time for period
  const kpis = summary ? {
    mtd: summary.total_cost_mtd ?? summary.total_cost_30d ?? 0,
    forecast_eom: totalForecastEom || financeSummary?.total_projected_eom_usd || 0,
    period_cost: periodCost,
    period_label: `${days}D`,
    roi_savings: roi?.total_savings ?? 0,
    active_teams: summary.active_teams ?? 0,
    cost_per_1k: summary.cost_per_1k_tokens ?? 0,
  } : null;

  // Render each section independently — one failure never blocks others
  try { _renderKpis(kpis); } catch (e) { console.warn('[executive] kpis:', e); }
  try { _renderNarrative(summary, teams, models); } catch (e) { console.warn('[executive] narrative:', e); }
  try { _renderForecast(costOverTime, forecast); } catch (e) { console.warn('[executive] forecast:', e); }  // cumulative MTD chart
  try { _renderSavings(execCharts); } catch (e) { console.warn('[executive] savings:', e); }
  try { _renderChargeback(teams); } catch (e) { console.warn('[executive] chargeback:', e); }
  try { _renderEnforcement(execCharts); } catch (e) { console.warn('[executive] enforcement:', e); }
  try { _renderRoi(roi); } catch (e) { console.warn('[executive] roi:', e); }
  try { _renderModelRisk(models); } catch (e) { console.warn('[executive] model-risk:', e); }
  try { _loadTopology(); } catch (e) { console.warn('[executive] topology:', e); }
}

// ── Tile renderers ───────────────────────────────────────────────────────────

function _renderKpis(k) {
  const body = document.getElementById('exec-kpis-body');
  if (!body) return;

  if (!k) {
    setTileEmpty('exec-kpis', { icon: 'fa-solid fa-gauge', title: 'No executive data' });
    return;
  }

  body.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">MTD Spend</div>
      <div class="kpi-value">${fmtCost(k.mtd)}</div>
      <div class="kpi-sub" style="font-size:10px;color:var(--muted)">month to date</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">EOM Forecast</div>
      <div class="kpi-value">${fmtCost(k.forecast_eom)}</div>
      <div class="kpi-sub" style="font-size:10px;color:var(--muted)">projected</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">${k.period_label} Spend</div>
      <div class="kpi-value">${fmtCost(k.period_cost)}</div>
      <div class="kpi-sub" style="font-size:10px;color:var(--muted)">selected period</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">ROI \u2014 Savings</div>
      <div class="kpi-value" style="color:var(--accent)">${fmtCost(k.roi_savings)}</div>
      <div class="kpi-sub" style="font-size:10px;color:var(--muted)">blocked call savings MTD</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Active Teams</div>
      <div class="kpi-value">${k.active_teams != null ? k.active_teams : '\u2014'}</div>
      <div class="kpi-sub" style="font-size:10px;color:var(--muted)">with AI activity</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Cost / 1K Tokens</div>
      <div class="kpi-value">$${(k.cost_per_1k != null ? k.cost_per_1k : 0).toFixed(4)}</div>
      <div class="kpi-sub" style="font-size:10px;color:var(--muted)">blended avg this month</div>
    </div>
  `;
}

async function _renderNarrative(summary, teams, models) {
  const body = document.getElementById('exec-narrative-body');
  if (!body) return;

  // Use cached version if under 1h old
  if (_narrativeCache && (Date.now() - _narrativeTs) < 3600000) {
    body.style.padding = '16px 24px'; body.innerHTML = `<div class="exec-narrative" style="font-size:13px;line-height:1.7;color:var(--text)">${esc(_narrativeCache)}</div>`;
    return;
  }

  // Build narrative from live data (no external API calls — data sovereignty)
  const text = _buildLocalNarrative(summary, teams, models);
  _narrativeCache = text;
  _narrativeTs = Date.now();

  body.style.padding = '16px 24px'; body.innerHTML = `<div class="exec-narrative" style="font-size:13px;line-height:1.7;color:var(--text)">${esc(text)}</div>`;
}

function _buildLocalNarrative(summary, teams, models) {
  if (!summary) {
    return 'No usage data available yet. Connect your first application to begin tracking AI spend.';
  }
  const s = summary;
  const totalSpend = s.total_cost_mtd ?? s.total_cost_30d ?? s.total_cost_usd ?? s.total_cost ?? 0;
  const totalCalls = s.total_calls_mtd ?? s.total_calls ?? s.total_records ?? 0;
  const avgCost = totalCalls > 0 ? totalSpend / totalCalls : 0;
  const topTeams = (teams || []).slice(0, 3).map(t => t.team_slug || t.team || 'unknown');
  const topModels = (models || []).slice(0, 3).map(m => m.model || 'unknown');
  const denied = s.policy_denials || s.denied || 0;

  const spendStr = totalSpend >= 1000 ? '$' + (totalSpend / 1000).toFixed(1) + 'k' : '$' + totalSpend.toFixed(2);
  const parts = [];
  parts.push(`Total AI spend over the past 30 days is ${spendStr} across ${totalCalls.toLocaleString()} calls (avg ${avgCost < 0.01 ? '<$0.01' : '$' + avgCost.toFixed(3)}/call).`);
  if (topTeams.length) parts.push(`Top spending teams: ${topTeams.join(', ')}.`);
  if (topModels.length) parts.push(`Most-used models: ${topModels.join(', ')}.`);
  if (denied > 0) parts.push(`Policy enforcement blocked ${denied} request${denied !== 1 ? 's' : ''}.`);
  return parts.join(' ');
}

function _renderForecast(costOverTime, forecastRaw) {
  const body = document.getElementById('exec-forecast-body');
  if (!body) return;

  // Destroy previous chart
  if (_forecastChart) {
    try { _forecastChart.destroy(); } catch (_) { /* noop */ }
    _forecastChart = null;
  }
  _removeResizeHandler('exec-forecast');

  const forecasts = Array.isArray(forecastRaw) ? forecastRaw : (forecastRaw ? [forecastRaw] : []);
  const totalMtd = forecasts.reduce((s, f) => s + (f.mtd_actual ?? 0), 0);
  const totalEom = forecasts.reduce((s, f) => s + (f.forecast_eom ?? 0), 0);

  if ((totalMtd <= 0 && totalEom <= 0) && (!costOverTime || !costOverTime.length)) {
    setTileEmpty('exec-forecast', {
      icon: 'fa-solid fa-chart-line',
      title: 'No forecast data',
      description: 'Forecast data will appear after a few days of usage.',
    });
    return;
  }

  // Build cumulative actual + forecast data for the current month
  const labels = [];
  const actualData = [];
  const forecastData = [];

  const now = new Date();
  const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
  const currentDay = now.getDate();
  const dailyActual = currentDay > 0 ? totalMtd / currentDay : 0;
  const dailyForecast = daysInMonth > 0 ? totalEom / daysInMonth : dailyActual;

  for (let d = 1; d <= daysInMonth; d++) {
    labels.push(d.toString());
    if (d <= currentDay) {
      actualData.push(Math.round(dailyActual * d * 100) / 100);
      forecastData.push(null);
    } else {
      actualData.push(null);
      forecastData.push(Math.round(dailyForecast * d * 100) / 100);
    }
  }
  // Connect the lines at the current day
  if (currentDay < daysInMonth) {
    forecastData[currentDay - 1] = actualData[currentDay - 1];
  }

  let trend = forecasts.length ? forecasts.reduce((s, f) => s + (f.trend_pct ?? 0), 0) / forecasts.length : 0;

  // Create canvas inside a fixed-height wrapper to prevent infinite growth
  body.innerHTML = '<div style="position:relative;width:100%;height:260px;padding:8px 12px 12px"><canvas></canvas></div>';
  body.style.overflow = 'hidden';
  const canvas = body.querySelector('canvas');

  if (typeof Chart !== 'undefined') {
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createLinearGradient(0, 0, 0, 200);
    gradient.addColorStop(0, 'rgba(159, 193, 49, 0.12)');
    gradient.addColorStop(1, 'rgba(159, 193, 49, 0)');

    _forecastChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Cumulative Spend',
            data: actualData,
            borderColor: '#9FC131',
            borderWidth: 2,
            pointRadius: 0,
            pointHoverRadius: 4,
            fill: true,
            backgroundColor: gradient,
            tension: 0.4,
            spanGaps: false,
          },
          {
            label: 'Forecast',
            data: forecastData,
            borderColor: '#a855f7',
            borderWidth: 2,
            borderDash: [6, 4],
            pointRadius: 0,
            pointHoverRadius: 4,
            fill: false,
            tension: 0.4,
            spanGaps: false,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            display: true,
            position: 'top',
            align: 'end',
            labels: { color: '#8b8fa3', boxWidth: 12, font: { size: 10 }, usePointStyle: true, pointStyle: 'line' },
          },
          tooltip: {
            callbacks: {
              label: function (c) {
                return c.dataset.label + ': $' + (c.parsed.y ?? 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
              },
            },
          },
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: '#5a5f78', font: { size: 10 }, maxTicksLimit: 10 },
            title: { display: true, text: 'Day of Month', color: '#5a5f78', font: { size: 10 } },
          },
          y: {
            grid: { color: 'rgba(26,29,46,0.6)' },
            ticks: { color: '#5a5f78', font: { size: 10 }, callback: function (v) { return '$' + v.toLocaleString(); } },
          },
        },
      },
    });
  }

  // Add trend annotation
  if (trend) {
    const trendEl = document.createElement('div');
    trendEl.className = 'forecast-trend-note';
    trendEl.style.cssText = 'font-size:11px;color:var(--muted);margin-top:8px;text-align:right';
    const color = trend > 0 ? '#f59e0b' : '#9FC131';
    trendEl.innerHTML = `Trend: <span style="color:${color};font-weight:600">${trend > 0 ? '+' : ''}${parseFloat(trend).toFixed(1)}% MoM</span> &middot; 28-day linear regression`;
    body.appendChild(trendEl);
  }

  _attachResizeHandler('exec-forecast', _forecastChart);
}

function _renderSavings(data) {
  const body = document.getElementById('exec-savings-body');
  if (!body) return;

  // Destroy previous chart
  if (_savingsChart) {
    try { _savingsChart.destroy(); } catch (_) { /* noop */ }
    _savingsChart = null;
  }
  _removeResizeHandler('exec-savings');

  if (!data || !data.savings_over_time || !data.savings_over_time.length) {
    setTileEmpty('exec-savings', {
      icon: 'fa-solid fa-piggy-bank',
      title: 'No savings data yet',
      description: 'Check back after enforcement policies have been active for a few days.',
    });
    return;
  }

  const savingsLabels = data.savings_over_time.map(p => fmtDate(p.period));
  const savingsData = data.savings_over_time.map(p => parseFloat(p.cost));

  // Create canvas inside fixed wrapper
  body.innerHTML = '<div style="position:relative;width:100%;height:260px;padding:8px 12px 12px"><canvas></canvas></div>';
  body.style.overflow = 'hidden';
  const canvas = body.querySelector('canvas');

  if (typeof ModusCharts !== 'undefined') {
    _savingsChart = ModusCharts.area(canvas, {
      labels: savingsLabels,
      data: savingsData,
      label: 'USD Saved',
      color: '#a855f7',
    });
  } else if (typeof Chart !== 'undefined') {
    const ctx = canvas.getContext('2d');
    const gradient = ctx.createLinearGradient(0, 0, 0, 200);
    gradient.addColorStop(0, 'rgba(159, 122, 255, 0.25)');
    gradient.addColorStop(1, 'rgba(159, 122, 255, 0)');
    _savingsChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels: savingsLabels,
        datasets: [{
          label: 'USD Saved',
          data: savingsData,
          borderColor: '#a855f7',
          borderWidth: 2,
          pointRadius: 3,
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

  _attachResizeHandler('exec-savings', _savingsChart);
}

function _renderChargeback(items) {
  const body = document.getElementById('exec-chargeback-body');
  if (!body) return;

  if (!items || !items.length) {
    setTileEmpty('exec-chargeback', {
      icon: 'fa-solid fa-plug',
      title: 'No chargeback data',
      description: 'Connect services to start tracking team chargebacks.',
    });
    return;
  }

  const total = items.reduce((s, t) => s + parseFloat(t.cost ?? t.total_cost ?? 0), 0);
  body.innerHTML = items.map(t => {
    const cost = parseFloat(t.cost ?? t.total_cost ?? 0);
    const pct = total > 0 ? (cost / total * 100) : (t.pct ?? 0);
    return `<div class="chargeback-row" style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--border)">
      <span class="chargeback-team" style="font-size:12px;font-weight:500;color:var(--text);min-width:100px">${esc(t.team ?? t.team_slug ?? '')}</span>
      <div class="chargeback-bar" style="flex:1;height:6px;border-radius:3px;background:var(--border);overflow:hidden">
        <div class="chargeback-fill" style="width:${pct.toFixed(0)}%;height:100%;background:var(--accent);border-radius:3px;transition:width 0.5s ease"></div>
      </div>
      <span class="chargeback-pct" style="font-family:var(--mono);font-size:11px;color:var(--muted);min-width:44px;text-align:right">${pct.toFixed(1)}%</span>
      <span class="chargeback-cost" style="font-family:var(--mono);font-size:12px;font-weight:600;color:var(--text);min-width:70px;text-align:right">${fmtCost(cost)}</span>
    </div>`;
  }).join('');
}

function _renderEnforcement(data) {
  const body = document.getElementById('exec-enforcement-body');
  if (!body) return;

  // Destroy previous chart
  if (_enforcementChart) {
    try { _enforcementChart.destroy(); } catch (_) { /* noop */ }
    _enforcementChart = null;
  }
  _removeResizeHandler('exec-enforcement');

  if (!data || !data.enforcement_mix) {
    setTileEmpty('exec-enforcement', {
      icon: 'fa-solid fa-shield-halved',
      title: 'No enforcement data yet',
      description: 'Data will appear once governance policies start evaluating calls.',
    });
    return;
  }

  const mix = data.enforcement_mix;

  // Create canvas inside fixed wrapper
  body.innerHTML = '<div style="position:relative;width:100%;height:260px;padding:8px 12px 12px"><canvas></canvas></div>';
  body.style.overflow = 'hidden';
  const canvas = body.querySelector('canvas');

  if (typeof ModusCharts !== 'undefined') {
    _enforcementChart = ModusCharts.donut(canvas, {
      labels: Object.keys(mix).map(k => k.toUpperCase()),
      data: Object.values(mix),
      centerText: Object.values(mix).reduce((a, b) => a + b, 0).toLocaleString(),
      centerSub: 'Total Actions',
    });
  } else if (typeof Chart !== 'undefined') {
    const ctx = canvas.getContext('2d');
    _enforcementChart = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: Object.keys(mix).map(k => k.toUpperCase()),
        datasets: [{
          data: Object.values(mix),
          backgroundColor: ['#ef4444', '#f59e0b', '#9FC131'],
          borderWidth: 0,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '70%',
        plugins: {
          legend: {
            position: 'right',
            labels: { color: '#e8ecf8', boxWidth: 10, font: { size: 10 } },
          },
        },
      },
    });
  }

  _attachResizeHandler('exec-enforcement', _enforcementChart);
}

function _renderRoi(r) {
  const body = document.getElementById('exec-roi-body');
  if (!body) return;

  if (!r) {
    setTileEmpty('exec-roi', {
      icon: 'fa-solid fa-hand-holding-dollar',
      title: 'No ROI data',
      description: 'ROI data will appear once enforcement policies begin blocking calls.',
    });
    return;
  }

  body.innerHTML = `
    <div style="text-align:center;padding:12px 0 16px;border-bottom:1px solid var(--border);margin-bottom:8px">
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">Net Savings This Month</div>
      <div style="font-size:28px;font-weight:700;color:var(--accent)">${fmtCost(r.net_roi ?? r.total_savings)}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:4px">${r.roi_multiple ? r.roi_multiple.toFixed(1) + 'x ROI on platform cost' : 'enforcement savings'}</div>
    </div>
    <div class="roi-rows" style="font-size:12px">
      <div style="display:flex;justify-content:space-between;padding:6px 12px;border-bottom:1px solid var(--border)">
        <span style="color:var(--muted)">Calls blocked</span>
        <span style="font-family:var(--mono);color:var(--text)">${(r.blocked_calls ?? 0).toLocaleString()}</span>
      </div>
      <div style="display:flex;justify-content:space-between;padding:6px 12px;border-bottom:1px solid var(--border)">
        <span style="color:var(--muted)">Avg cost per blocked call</span>
        <span style="font-family:var(--mono);color:var(--text)">$${(r.cost_per_blocked ?? 0).toFixed(4)}</span>
      </div>
      <div style="display:flex;justify-content:space-between;padding:6px 12px;border-bottom:1px solid var(--border)">
        <span style="color:var(--muted)">Gross savings</span>
        <span style="font-family:var(--mono);color:var(--text)">${fmtCost(r.total_savings)}</span>
      </div>
      <div style="display:flex;justify-content:space-between;padding:6px 12px;border-bottom:1px solid var(--border)">
        <span style="color:var(--muted)">Platform cost (est.)</span>
        <span style="font-family:var(--mono);color:var(--text)">${fmtCost(r.platform_cost ?? 68)}</span>
      </div>
      <div style="display:flex;justify-content:space-between;padding:6px 12px;font-weight:700">
        <span style="color:var(--muted)">Net</span>
        <span style="font-family:var(--mono);color:var(--accent)">${fmtCost(r.net_roi ?? r.total_savings)}</span>
      </div>
    </div>
  `;
}

function _renderModelRisk(items) {
  const body = document.getElementById('exec-model-risk-body');
  if (!body) return;

  if (!items || !items.length) {
    setTileEmpty('exec-model-risk', {
      icon: 'fa-solid fa-chart-bar',
      title: 'No model data',
      description: 'Model risk data will appear once apps begin sending usage records.',
    });
    return;
  }

  body.innerHTML = items.map(m => {
    const pct = parseFloat(m.pct ?? m.cost_pct ?? 0);
    const modelName = esc((m.model ?? '').split('-').slice(0, 3).join('-'));
    return `<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--border)">
      <span style="font-size:12px;font-weight:500;color:var(--text);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${modelName}</span>
      <span style="font-family:var(--mono);font-size:11px;color:var(--muted);min-width:44px;text-align:right">${pct.toFixed(1)}%</span>
      <span style="font-family:var(--mono);font-size:12px;font-weight:600;color:var(--text);min-width:70px;text-align:right">${fmtCost(m.cost ?? m.total_cost)}</span>
    </div>`;
  }).join('');
}

async function _loadTopology() {
  const body = document.getElementById('exec-topology-body');
  if (!body) return;

  const emptyHTML = `<div style="display:flex;align-items:center;justify-content:center;min-height:100px;color:var(--muted);font-size:12px;text-align:center;padding:20px">
    <div>
      <div style="font-size:1.5rem;margin-bottom:8px;opacity:0.4"><i class="fa-solid fa-plug"></i></div>
      Connect services to start tracking topology.<br>Register your first app to enable architectural discovery.
    </div>
  </div>`;

  try {
    const resp = await rawFetch('/api/v1/topology');
    if (!resp || !resp.ok) {
      body.innerHTML = emptyHTML;
      return;
    }
    const data = await resp.json();
    const items = Array.isArray(data) ? data : (data.items || []);

    if (_destroyed) return;

    if (!items.length) {
      body.innerHTML = emptyHTML;
      return;
    }

    _renderTopology(body, items);
  } catch (err) {
    console.warn('[executive] topology load failed:', err);
    if (!_destroyed) body.innerHTML = emptyHTML;
  }
}

function _renderTopology(body, items) {
  if (!body || !items || !items.length) return;

  body.innerHTML = `<div class="topology-container">${items.map(t => `
    <div class="topology-card" style="background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div style="font-weight:600;font-size:13px;color:var(--text)">${esc(t.app_name || '')}</div>
        <div style="font-size:10px;color:var(--muted)">${esc(t.deployment_type || 'deployed')} \u00b7 ${esc(t.cloud_provider || 'cloud')}</div>
      </div>
      <div style="font-size:12px;color:var(--muted);margin-bottom:10px;line-height:1.5">${esc(t.ai_summary || 'No architectural summary available.')}</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;font-size:11px">
        <div>
          <div style="color:var(--muted);margin-bottom:3px">Framework</div>
          <div style="color:var(--text)">${esc(t.web_framework || '\u2014')}</div>
        </div>
        <div>
          <div style="color:var(--muted);margin-bottom:3px">Providers</div>
          <div style="display:flex;flex-wrap:wrap;gap:3px">
            ${Object.keys(t.ai_providers || {}).map(p => `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(255,255,255,0.04);color:var(--muted)">${esc(p)}</span>`).join('') || '<span style="color:var(--text)">\u2014</span>'}
          </div>
        </div>
        <div>
          <div style="color:var(--muted);margin-bottom:3px">Dependencies</div>
          <div style="display:flex;flex-wrap:wrap;gap:3px">
            ${Object.keys(t.service_dependencies || {}).map(d => `<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(255,255,255,0.04);color:var(--muted)">${esc(d)}</span>`).join('') || '<span style="color:var(--text)">\u2014</span>'}
          </div>
        </div>
      </div>
    </div>
  `).join('')}</div>`;
}

// ── Chart resize helpers ─────────────────────────────────────────────────────

function _attachResizeHandler(tileId, chart) {
  if (!chart) return;
  const body = document.getElementById(`${tileId}-body`);
  if (!body) return;

  const handler = () => {
    if (chart && typeof chart.resize === 'function') {
      chart.resize();
    }
  };
  _resizeHandlers[tileId] = handler;
  body.addEventListener('tile-resize', handler);
}

function _removeResizeHandler(tileId) {
  const handler = _resizeHandlers[tileId];
  if (handler) {
    const body = document.getElementById(`${tileId}-body`);
    if (body) body.removeEventListener('tile-resize', handler);
    delete _resizeHandlers[tileId];
  }
}

// ── Filter / Export helpers ──────────────────────────────────────────────────

function _filterRows(bodyId, query) {
  const body = document.getElementById(bodyId);
  if (!body) return;
  const q = (query || '').toLowerCase();
  const rows = body.querySelectorAll('.chargeback-row');
  rows.forEach(row => {
    const text = row.textContent.toLowerCase();
    row.style.display = text.includes(q) ? '' : 'none';
  });
}

function _exportChargeback() {
  const body = document.getElementById('exec-chargeback-body');
  if (!body) return;
  const rows = body.querySelectorAll('.chargeback-row');
  if (!rows.length) return;

  let csv = 'Team,Cost (USD),Pct\n';
  rows.forEach(r => {
    const team = r.querySelector('.chargeback-team')?.textContent ?? '';
    const cost = r.querySelector('.chargeback-cost')?.textContent ?? '';
    const pct = r.querySelector('.chargeback-pct')?.textContent ?? '';
    csv += `"${team}",${cost.replace('$', '')},${pct.replace('%', '')}\n`;
  });

  const blob = new Blob([csv], { type: 'text/csv' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `chargeback-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(url);
}
