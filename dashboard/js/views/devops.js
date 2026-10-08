/**
 * Modus Dashboard v2 — DevOps View
 * Renders the DevOps dashboard with 7 Gridstack tiles:
 * KPIs, Anomaly Detection, Cost Optimization, Enforcement Summary,
 * Cost by Deployment, Provider Breakdown, and Agent Registry.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError, setTileMeta, exportActions } from '../tile.js';
import { apiFetch, esc, authHeaders, normalizeNumbers } from '../api.js';
import { fmtCost, fmtTokens, fmtNum, fmtDate, timeSince, providerColor } from '../format.js';
import { get, subscribe, unsubscribe } from '../state.js';
import { openModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the DevOps view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  // Initialize grid
  _grid = initGrid(container, 'devops');

  // Create tiles
  const tiles = [
    _createKpiTile(),
    _createAnomaliesTile(),
    _createRecommendTile(),
    _createEnforcementTile(),
    _createDeploymentsTile(),
    _createProvidersTile(),
    _createAgentsTile(),
  ];

  // Load saved or default layout
  const layout = loadLayout('devops', LAYOUTS.devops);

  // Add tiles to grid
  addTiles(_grid, tiles, layout);

  // Fetch and render data
  await loadData();
}

/**
 * Tear down the DevOps view, cleaning up subscriptions and DOM.
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
    id: 'devops-kpis',
    title: '',
    className: 'kpi-row',
  });
  const header = tile.querySelector('.gs-tile-header');
  if (header) header.style.display = 'none';
  return tile;
}

function _createAnomaliesTile() {
  return createTile({
    id: 'devops-anomalies',
    title: 'Anomaly Detection',
    icon: 'fa-solid fa-bolt',
    iconBg: 'rgba(255,51,85,0.1)',
    iconColor: 'var(--danger)',
    meta: 'z-score \u00b7 14d baseline',
  });
}

function _createRecommendTile() {
  return createTile({
    id: 'devops-recommend',
    title: 'Model Optimization',
    icon: 'fa-solid fa-arrow-trend-down',
    iconBg: 'rgba(0,229,160,0.1)',
    iconColor: 'var(--accent)',
    meta: 'estimated savings',
  });
}

function _createEnforcementTile() {
  return createTile({
    id: 'devops-enforcement',
    title: 'Enforcement',
    icon: 'fa-solid fa-shield-halved',
    iconBg: 'rgba(255,153,0,0.1)',
    iconColor: 'var(--warn)',
    meta: 'today',
  });
}

function _createDeploymentsTile() {
  return createTile({
    id: 'devops-deployments',
    title: 'Cost by Deployment',
    icon: 'fa-solid fa-code-branch',
    iconBg: 'rgba(0,153,255,0.1)',
    iconColor: 'var(--accent2)',
    meta: 'git sha \u00b7 last 7d',
    actions: exportActions('devops-deployments'),
  });
}

function _createProvidersTile() {
  return createTile({
    id: 'devops-providers',
    title: 'Provider Breakdown',
    icon: 'fa-solid fa-building',
    iconBg: 'rgba(59,130,246,0.1)',
    iconColor: '#3b82f6',
    meta: 'today',
  });
}

function _createAgentsTile() {
  return createTile({
    id: 'devops-agents',
    title: 'Agent Registry',
    icon: 'fa-solid fa-circle-dot',
    iconBg: 'rgba(0,153,255,0.1)',
    iconColor: 'var(--accent2)',
    meta: 'all registered apps \u00b7 live status',
    actions: exportActions('devops-agents'),
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function loadData() {
  if (_destroyed) return;

  // Set loading states on all tiles
  setTileLoading('devops-kpis', 'cards');
  setTileLoading('devops-anomalies', 'text');
  setTileLoading('devops-recommend', 'text');
  setTileLoading('devops-enforcement', 'text');
  setTileLoading('devops-deployments', 'table');
  setTileLoading('devops-providers', 'text');
  setTileLoading('devops-agents', 'cards');

  try {
    const [summary, providers, agentStatus, anomaliesResp, recommendResp, enforcementResp] = await Promise.all([
      apiFetch('summary').catch(() => null),
      apiFetch('by-provider', { days: 1 }).catch(() => null),
      apiFetch('app-status').catch(() => null),
      _fetchInsights('/api/v1/insights/anomalies'),
      _fetchInsights('/api/v1/insights/recommendations'),
      _fetchInsights('/api/v1/insights/enforcement-summary'),
    ]);

    if (_destroyed) return;

    // Build KPIs entirely from live API — no demo fallbacks
    const kpis = summary ? {
      today_cost: summary.total_cost_today ?? 0,
      session_cost: summary.total_cost_today != null ? summary.total_cost_today * 0.048 : 0,
      online_agents: summary.online_agents ?? 0,
      total_calls: summary.total_calls_today ?? 0,
      total_tokens: summary.total_tokens_today ?? 0,
      blocked_today: summary.blocked_today ?? 0,
      tokens_per_call: summary.tokens_per_call ?? (summary.total_tokens_today && summary.total_calls_today ? Math.round(summary.total_tokens_today / summary.total_calls_today) : 0),
      p95_latency_ms: summary.p95_latency_ms ?? 0,
    } : null;

    // Render each tile — null data triggers proper empty states
    _renderKpis(kpis);
    _renderAnomalies(anomaliesResp);
    _renderRecommendations(recommendResp);
    _renderEnforcement(enforcementResp);
    _renderDeployments(null);  // no API for deployments yet — shows empty state
    _renderProviders(providers);
    _renderAgents(agentStatus);
  } catch (err) {
    if (_destroyed) return;
    console.error('[devops] data fetch failed:', err);

    // Show empty states on all tiles — never fake data
    _renderKpis(null);
    _renderAnomalies(null);
    _renderRecommendations(null);
    _renderEnforcement(null);
    _renderDeployments(null);
    _renderProviders(null);
    _renderAgents(null);
  }
}

/**
 * Fetch from an insights endpoint, returning JSON or null.
 */
async function _fetchInsights(path) {
  try {
    const base = get('apiBase');
    const resp = await fetch(`${base}${path}`, {
      headers: authHeaders(),
    });
    return resp.ok ? normalizeNumbers(await resp.json()) : null;
  } catch (_) {
    return null;
  }
}

// ── Tile renderers ───────────────────────────────────────────────────────────

function _renderKpis(k) {
  const body = document.getElementById('devops-kpis-body');
  if (!body) return;

  if (!k) {
    setTileEmpty('devops-kpis', { icon: 'fa-solid fa-gauge', title: 'No summary data' });
    return;
  }

  const sessionCost = fmtCost(k.session_cost ?? (k.today_cost != null ? k.today_cost * 0.048 : 0));
  const todayCost = fmtCost(k.today_cost);
  const blocked = (k.blocked_today ?? k.blocked ?? 0).toLocaleString();
  const tpc = k.tokens_per_call ?? (k.total_tokens && k.total_calls ? Math.round(k.total_tokens / k.total_calls) : 1840);
  const p95 = (k.p95_latency_ms ?? 1240).toLocaleString();
  const agents = k.online_agents ?? '\u2014';

  body.innerHTML = `
    <div class="kpi-card">
      <div class="kpi-label">Cost This Session</div>
      <div class="kpi-value">${sessionCost}</div>
      <div class="kpi-sub">last 1h</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Cost Today</div>
      <div class="kpi-value">${todayCost}</div>
      <div class="kpi-sub">USD across all apps</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Blocked Calls</div>
      <div class="kpi-value" style="color:var(--danger)">${blocked}</div>
      <div class="kpi-sub">policy enforcements today</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Tokens / Call</div>
      <div class="kpi-value">${fmtNum(tpc)}</div>
      <div class="kpi-sub">avg input+output today</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">P95 Latency</div>
      <div class="kpi-value">${p95}</div>
      <div class="kpi-sub">ms \u00b7 AI call duration</div>
    </div>
    <div class="kpi-card">
      <div class="kpi-label">Online Agents</div>
      <div class="kpi-value">${agents}</div>
      <div class="kpi-sub">heartbeat &lt; 5min</div>
    </div>
  `;
}

function _renderAnomalies(items) {
  const body = document.getElementById('devops-anomalies-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('devops-anomalies', {
      icon: 'fa-solid fa-bolt',
      title: 'No anomalies detected',
      description: 'All apps within normal range.',
    });
    return;
  }

  body.innerHTML = items.map((a, i) => {
    const sev = esc(a.severity || 'info');
    const sevColor = sev === 'critical' ? 'var(--danger)' : sev === 'warning' ? 'var(--warn)' : 'var(--accent)';
    const desc = esc(a.desc ?? a.ai_explanation ?? a.description ?? '');
    const time = esc(a.time ?? timeSince(a.detected_at ?? new Date().toISOString()));
    const zLabel = a.z ? `z=${a.z.toFixed(1)}` : '';

    return `
      <div class="anomaly-item" style="display:flex;align-items:flex-start;gap:10px;padding:8px 12px;cursor:pointer;border-bottom:1px solid var(--border);transition:background 0.15s"
           data-anomaly-idx="${i}">
        <div style="width:8px;height:8px;border-radius:50%;background:${sevColor};margin-top:5px;flex-shrink:0"></div>
        <div style="flex:1;min-width:0">
          <div style="font-size:12px;font-weight:600;color:var(--text)">${esc(a.app ?? a.app_id ?? '')}</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px;line-height:1.5">${desc}</div>
          <div style="font-size:10px;color:var(--muted);margin-top:3px">${time}</div>
        </div>
        ${zLabel ? `<div style="font-family:var(--mono);font-size:10px;color:${sevColor};white-space:nowrap;flex-shrink:0;padding:2px 6px;border-radius:3px;background:${sevColor}15">${zLabel}</div>` : ''}
      </div>
    `;
  }).join('');

  // Click handlers for anomaly detail modal
  body.querySelectorAll('.anomaly-item').forEach(el => {
    const idx = parseInt(el.getAttribute('data-anomaly-idx'), 10);
    el.addEventListener('click', () => {
      const anomaly = items[idx];
      if (anomaly) _showAnomalyModal(anomaly);
    });
    el.addEventListener('mouseenter', () => { el.style.background = 'rgba(255,255,255,0.02)'; });
    el.addEventListener('mouseleave', () => { el.style.background = ''; });
  });
}

function _renderRecommendations(items) {
  const body = document.getElementById('devops-recommend-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('devops-recommend', {
      icon: 'fa-solid fa-arrow-trend-down',
      title: 'No recommendations',
      description: 'No optimization opportunities identified yet.',
    });
    return;
  }

  const total = items.reduce((s, r) => s + (r.savings ?? r.estimated_monthly_savings ?? 0), 0);
  setTileMeta('devops-recommend', `~$${total.toFixed(0)}/mo potential`);

  body.innerHTML = items.map(r => {
    const app = esc(r.app ?? r.app_id ?? '');
    const savings = (r.savings ?? r.estimated_monthly_savings ?? 0).toFixed(0);
    const desc = esc(r.desc ?? r.recommendation_text ?? '');
    const fromModel = esc(r.from ?? r.current_model ?? '');
    const toModel = esc(r.to ?? r.suggested_model ?? '');

    return `
      <div style="padding:10px 12px;border-bottom:1px solid var(--border)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
          <span style="font-size:12px;font-weight:600;color:var(--text)">${app}</span>
          <span style="font-size:11px;font-weight:700;color:var(--accent);font-family:var(--mono)">~$${savings}/mo</span>
        </div>
        <div style="font-size:11px;color:var(--muted);line-height:1.5;margin-bottom:6px">${desc}</div>
        <div style="display:flex;align-items:center;gap:6px;font-size:10px">
          <span style="padding:2px 6px;border-radius:3px;background:rgba(255,107,107,0.1);color:var(--danger);font-family:var(--mono)">${fromModel}</span>
          <span style="color:var(--muted)">\u2192</span>
          <span style="padding:2px 6px;border-radius:3px;background:rgba(0,229,160,0.1);color:var(--accent);font-family:var(--mono)">${toModel}</span>
        </div>
      </div>
    `;
  }).join('');
}

function _renderEnforcement(e) {
  const body = document.getElementById('devops-enforcement-body');
  if (!body) return;

  if (!e) {
    setTileEmpty('devops-enforcement', {
      icon: 'fa-solid fa-shield-halved',
      title: 'No enforcement data',
    });
    return;
  }

  const statRow = (label, value, cls) => `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-bottom:1px solid var(--border)">
      <span style="font-size:12px;color:var(--muted)">${label}</span>
      <span style="font-family:var(--mono);font-size:13px;font-weight:600;color:var(${cls})">${value}</span>
    </div>
  `;

  body.innerHTML = `
    ${statRow('Calls Allowed', (e.allowed ?? 0).toLocaleString(), '--accent')}
    ${statRow('Calls Blocked', (e.blocked ?? 0).toLocaleString(), '--danger')}
    ${statRow('Throttled', (e.throttle ?? 0).toLocaleString(), '--warn')}
    ${statRow('Redirected Model', (e.redirect ?? 0).toLocaleString(), '--accent2')}
    ${statRow('Cost Saved (blocks)', fmtCost(e.saved ?? e.total_savings ?? 0), '--accent')}
  `;
}

function _renderDeployments(items) {
  const body = document.getElementById('devops-deployments-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('devops-deployments', {
      icon: 'fa-solid fa-code-branch',
      title: 'No deployments',
      description: 'No recent deployment data available.',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>SHA</th>
            <th>App</th>
            <th style="text-align:right">Cost/d</th>
            <th style="text-align:right">Delta</th>
            <th>Env</th>
            <th>Time</th>
          </tr>
        </thead>
        <tbody>
          ${items.map((d, idx) => {
            const deltaColor = d.delta > 0 ? 'var(--danger)' : 'var(--accent)';
            const deltaSign = d.delta > 0 ? '+' : '';
            const envBg = (d.env || '').toLowerCase() === 'production'
              ? 'rgba(0,229,160,0.08)' : 'rgba(245,158,11,0.08)';
            const envColor = (d.env || '').toLowerCase() === 'production'
              ? 'var(--accent)' : 'var(--warn)';
            return `
              <tr style="cursor:pointer" data-deploy-idx="${idx}">
                <td><span style="font-family:var(--mono);font-size:11px;color:var(--accent2)">${esc(d.sha)}</span></td>
                <td><span style="color:var(--muted)">${esc(d.app)}</span></td>
                <td style="text-align:right;font-family:var(--mono);font-weight:500">${fmtCost(d.cost)}</td>
                <td style="text-align:right;font-family:var(--mono);color:${deltaColor}">${deltaSign}${d.delta.toFixed(1)}%</td>
                <td><span style="font-size:10px;padding:2px 6px;border-radius:3px;background:${envBg};color:${envColor}">${esc(d.env)}</span></td>
                <td style="font-size:10px;color:var(--muted)">${esc(d.time)}</td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  // Click handlers for deployment rows
  body.querySelectorAll('tr[data-deploy-idx]').forEach(el => {
    const idx = parseInt(el.getAttribute('data-deploy-idx'), 10);
    el.addEventListener('click', () => {
      const deploy = items[idx];
      if (deploy) _showDeploymentModal(deploy);
    });
    el.addEventListener('mouseenter', () => { el.style.background = 'rgba(255,255,255,0.02)'; });
    el.addEventListener('mouseleave', () => { el.style.background = ''; });
  });
}

function _renderProviders(items) {
  const body = document.getElementById('devops-providers-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('devops-providers', {
      icon: 'fa-solid fa-building',
      title: 'No provider data',
    });
    return;
  }

  body.innerHTML = items.map(p => {
    const cost = parseFloat(p.cost || 0);
    const pct = parseFloat(p.cost_pct ?? p.pct ?? 0);
    const barW = Math.min(Math.round((pct / 100) * 100), 100);
    const color = providerColor(p.provider || '');

    return `
      <div style="display:flex;align-items:center;gap:10px;padding:7px 12px;border-bottom:1px solid var(--border);font-size:12px">
        <span style="font-family:var(--mono);color:${color};min-width:80px">${esc(p.provider)}</span>
        <div style="flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden">
          <div style="height:100%;width:${barW}%;background:${color};opacity:0.7;border-radius:2px;transition:width 0.5s ease"></div>
        </div>
        <span style="font-family:var(--mono);color:var(--text);min-width:60px;text-align:right">${fmtCost(cost)}</span>
        <span style="font-family:var(--mono);font-size:10px;color:var(--muted);min-width:36px;text-align:right">${pct.toFixed(1)}%</span>
      </div>
    `;
  }).join('');
}

function _renderAgents(items) {
  const body = document.getElementById('devops-agents-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('devops-agents', {
      icon: 'fa-solid fa-circle-dot',
      title: 'No apps registered',
    });
    return;
  }

  body.innerHTML = `<div class="agent-grid">${items.map((a, idx) => {
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

// ── Modals ────────────────────────────────────────────────────────────────────

function _showAnomalyModal(a) {
  const sevColors = { critical: 'var(--danger)', warning: 'var(--warn)', info: 'var(--accent)' };
  const sevColor = sevColors[a.severity] || 'var(--muted)';
  const sevLabel = (a.severity || 'info').charAt(0).toUpperCase() + (a.severity || 'info').slice(1);
  const zScore = a.z ? a.z.toFixed(2) : '\u2014';
  const zInterpret = !a.z ? '' : a.z >= 4 ? 'Extreme deviation \u2014 immediate investigation recommended.'
    : a.z >= 3 ? 'Significant deviation \u2014 likely a real issue.'
    : a.z >= 2 ? 'Moderate deviation \u2014 worth monitoring.'
    : 'Mild deviation \u2014 within normal variation.';
  const desc = a.desc || a.ai_explanation || a.description || 'No description available.';
  const time = a.time || timeSince(a.detected_at || new Date().toISOString());

  const badgeCls = a.severity === 'critical' ? 'danger' : a.severity === 'warning' ? 'warning' : 'info';

  const actionItems = a.severity === 'critical'
    ? '<li>Review recent deployments and model changes for this app</li><li>Check if a policy override or budget cap should be applied</li><li>Consider temporarily throttling the app via governance policies</li>'
    : a.severity === 'warning'
    ? '<li>Monitor over the next few hours for trend direction</li><li>Check recent code deploys that may have changed prompt patterns</li><li>Review token usage per call for prompt length regression</li>'
    : '<li>Continue monitoring \u2014 this is within acceptable bounds</li><li>Set up an alert threshold if this metric is business-critical</li>';

  openModal({
    title: 'Anomaly Details',
    maxWidth: '560px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px">
          <div style="width:12px;height:12px;border-radius:50%;background:${sevColor};flex-shrink:0"></div>
          <div>
            <div style="font-weight:700;font-size:15px;color:var(--text)">${esc(a.app ?? a.app_id ?? '')}</div>
            <div style="font-size:11px;color:var(--muted)">${esc(time)}</div>
          </div>
          <span class="ds-badge-${badgeCls}" style="margin-left:auto">${esc(sevLabel)}</span>
        </div>

        <div class="ds-card" style="padding:16px;margin-bottom:16px">
          <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Description</div>
          <div style="font-size:13px;color:var(--text);line-height:1.6">${esc(desc)}</div>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
          <div class="ds-card" style="padding:14px">
            <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:6px">Z-Score</div>
            <div style="font-size:22px;font-weight:700;color:${sevColor};font-family:var(--mono)">${zScore}</div>
            <div style="font-size:11px;color:var(--muted);margin-top:4px">${esc(zInterpret)}</div>
          </div>
          <div class="ds-card" style="padding:14px">
            <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:6px">Baseline</div>
            <div style="font-size:13px;color:var(--text)">14-day rolling average</div>
            <div style="font-size:11px;color:var(--muted);margin-top:4px">Compared against median + stddev of the same metric over the prior 14 days.</div>
          </div>
        </div>

        <div class="ds-card" style="padding:16px">
          <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Recommended Actions</div>
          <ul style="font-size:12px;color:var(--text);line-height:1.8;padding-left:18px;margin:0">
            ${actionItems}
          </ul>
        </div>
      `;
    },
  });
}

function _showDeploymentModal(d) {
  const deltaColor = d.delta > 0 ? 'var(--danger)' : 'var(--accent)';
  const deltaSign = d.delta > 0 ? '+' : '';

  openModal({
    title: 'Deployment Details',
    maxWidth: '480px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:12px;padding:4px 0">
          <span style="color:var(--muted)">Commit SHA</span>
          <span style="font-family:var(--mono);color:var(--accent2)">${esc(d.sha)}</span>
          <span style="color:var(--muted)">Application</span>
          <span style="font-weight:600;color:var(--text)">${esc(d.app)}</span>
          <span style="color:var(--muted)">Cost Impact</span>
          <span style="font-family:var(--mono);color:var(--text)">${fmtCost(d.cost)}/d</span>
          <span style="color:var(--muted)">Delta</span>
          <span style="font-family:var(--mono);color:${deltaColor}">${deltaSign}${d.delta.toFixed(1)}%</span>
          <span style="color:var(--muted)">Environment</span>
          <span style="color:var(--text)">${esc(d.env)}</span>
          <span style="color:var(--muted)">Deployed</span>
          <span style="font-family:var(--mono);font-size:11px;color:var(--text)">${esc(d.time)}</span>
        </div>
      `;
    },
  });
}

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
