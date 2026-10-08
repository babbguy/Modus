/**
 * Modus Dashboard v2 — Session Attribution View
 * Renders 4 tiles: Sessions list, Amplification, Retry Tax, Defensive Spend.
 * Backend endpoints:
 *   - GET /api/v1/attribution/sessions — session list with cost breakdown
 *   - GET /api/v1/attribution/amplification — amplification ratio per app
 *   - GET /api/v1/attribution/retry-tax — retry-related cost overhead per app
 *   - GET /api/v1/attribution/defensive-spend — defensive coding spend per app
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError } from '../tile.js';
import { rawFetch, esc } from '../api.js';
import { fmtCost, fmtNum } from '../format.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Sessions Attribution view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  // Initialize grid
  _grid = initGrid(container, 'sessions');

  // Create tiles
  const tiles = [
    _createSessionsTile(),
    _createAmplificationTile(),
    _createRetryTaxTile(),
    _createDefensiveSpendTile(),
  ];

  // Load saved or default layout
  const layout = loadLayout('sessions', LAYOUTS.sessions);

  // Add tiles to grid
  addTiles(_grid, tiles, layout);

  // Fetch and render data
  await _loadData();
}

/**
 * Tear down the Sessions view, cleaning up subscriptions and DOM.
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
  return _destroyed ? Promise.resolve() : _loadData();
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createSessionsTile() {
  return createTile({
    id: 'sessions-list',
    title: 'Recent Sessions',
    icon: 'fa-solid fa-stream',
    iconBg: 'rgba(99,102,241,0.1)',
    iconColor: '#6366f1',
    meta: 'last 50 sessions',
    filterable: true,
    filterPlaceholder: 'Filter sessions...',
    onFilter: (q) => _filterRows('sessions-list-body', q),
  });
}

function _createAmplificationTile() {
  return createTile({
    id: 'sessions-amplification',
    title: 'Amplification Ratio',
    icon: 'fa-solid fa-arrow-up-right-dots',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
    meta: 'user request → AI calls',
    filterable: true,
    filterPlaceholder: 'Filter apps...',
    onFilter: (q) => _filterRows('sessions-amplification-body', q),
  });
}

function _createRetryTaxTile() {
  return createTile({
    id: 'sessions-retry-tax',
    title: 'Retry Tax',
    icon: 'fa-solid fa-arrows-rotate',
    iconBg: 'rgba(239,68,68,0.1)',
    iconColor: '#ef4444',
    meta: 'wasted spend from retries',
    filterable: true,
    filterPlaceholder: 'Filter apps...',
    onFilter: (q) => _filterRows('sessions-retry-tax-body', q),
  });
}

function _createDefensiveSpendTile() {
  return createTile({
    id: 'sessions-defensive',
    title: 'Defensive Spend',
    icon: 'fa-solid fa-shield-halved',
    iconBg: 'rgba(168,85,247,0.1)',
    iconColor: '#a855f7',
    meta: 'safety-critical AI spend',
    filterable: true,
    filterPlaceholder: 'Filter apps...',
    onFilter: (q) => _filterRows('sessions-defensive-body', q),
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;

  // Set loading states on all tiles
  setTileLoading('sessions-list', 'table');
  setTileLoading('sessions-amplification', 'table');
  setTileLoading('sessions-retry-tax', 'table');
  setTileLoading('sessions-defensive', 'table');

  try {
    const [sessionsResp, ampResp, retryResp, defResp] = await Promise.all([
      rawFetch('/api/v1/attribution/sessions?limit=50').catch(() => null),
      rawFetch('/api/v1/attribution/amplification').catch(() => null),
      rawFetch('/api/v1/attribution/retry-tax').catch(() => null),
      rawFetch('/api/v1/attribution/defensive-spend').catch(() => null),
    ]);

    if (_destroyed) return;

    const parse = async (r) => {
      if (!r || !r.ok) return [];
      try {
        const data = await r.json();
        return Array.isArray(data) ? data : (data.items || data.sessions || data.apps || []);
      } catch (_) {
        return [];
      }
    };

    const [sessions, amp, retry, defs] = await Promise.all([
      parse(sessionsResp),
      parse(ampResp),
      parse(retryResp),
      parse(defResp),
    ]);

    _renderSessions(sessions);
    _renderAmplification(amp);
    _renderRetryTax(retry);
    _renderDefensive(defs);
  } catch (err) {
    if (_destroyed) return;
    console.error('[sessions] data fetch failed:', err);

    // Show empty states on all tiles — never fake data
    _renderSessions(null);
    _renderAmplification(null);
    _renderRetryTax(null);
    _renderDefensive(null);
  }
}

// ── Tile renderers ───────────────────────────────────────────────────────────

/**
 * Render sessions list table.
 * @param {Array} items
 */
function _renderSessions(items) {
  const body = document.getElementById('sessions-list-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('sessions-list', {
      icon: 'fa-solid fa-stream',
      title: 'No sessions',
      description: 'Sessions appear once apps emit session_id with their telemetry.',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Session ID</th>
            <th>App</th>
            <th>Tier</th>
            <th style="text-align:right">Calls</th>
            <th style="text-align:right">Tokens</th>
            <th style="text-align:right">Cost</th>
            <th style="text-align:right">Confidence</th>
            <th>Started</th>
          </tr>
        </thead>
        <tbody>
          ${items.slice(0, 50).map(s => {
            const totalTokens = (s.total_input_tokens || 0) + (s.total_output_tokens || 0);
            const conf = s.attribution_confidence != null ? (s.attribution_confidence * 100).toFixed(0) + '%' : '\u2014';
            return `
            <tr>
              <td style="font-family:var(--mono);font-size:11px">${esc((s.session_id || '').slice(0, 12))}\u2026</td>
              <td style="font-size:11px;font-family:var(--mono)">${esc((s.app_id || '').slice(0, 12))}</td>
              <td style="font-size:11px"><span style="font-size:10px;padding:2px 6px;border-radius:3px;background:rgba(99,102,241,0.1);color:#6366f1">${esc(s.framework_tier || '\u2014')}</span></td>
              <td style="text-align:right;font-size:11px">${fmtNum(s.total_calls || 0)}</td>
              <td style="text-align:right;font-size:11px">${fmtNum(totalTokens)}</td>
              <td style="text-align:right;font-weight:600;font-size:11px">${fmtCost(parseFloat(s.total_cost || 0))}</td>
              <td style="text-align:right;font-size:11px;color:var(--muted)">${conf}</td>
              <td style="font-size:10px;color:var(--muted)">${s.started_at ? new Date(s.started_at).toLocaleString() : '\u2014'}</td>
            </tr>
          `;}).join('')}
        </tbody>
      </table>
    </div>
  `;
}

/**
 * Render amplification ratio table (user requests vs AI calls).
 * @param {Array} items
 */
function _renderAmplification(items) {
  const body = document.getElementById('sessions-amplification-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('sessions-amplification', {
      icon: 'fa-solid fa-arrow-up-right-dots',
      title: 'No amplification data',
      description: 'Tracks how each user request fans out into multiple AI calls.',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Node</th>
            <th>App</th>
            <th>Provider / Model</th>
            <th style="text-align:right">Direct Cost</th>
            <th style="text-align:right">Attributed</th>
            <th style="text-align:right">Amplification</th>
          </tr>
        </thead>
        <tbody>
          ${items.map(a => {
            const ratio = parseFloat(a.amplification_factor || 0);
            const ratioColor = ratio >= 5 ? 'var(--danger)' : ratio >= 3 ? 'var(--warn)' : 'var(--accent)';
            return `<tr>
              <td style="font-weight:500;font-size:11px">${esc(a.node_label || '\u2014')}</td>
              <td style="font-size:10px;font-family:var(--mono);color:var(--muted)">${esc((a.app_id || '').slice(0, 10))}</td>
              <td style="font-size:10px;font-family:var(--mono)">${esc(a.provider || '\u2014')} / ${esc(a.model || '\u2014')}</td>
              <td style="text-align:right;font-family:var(--mono);font-size:11px">${fmtCost(parseFloat(a.direct_cost || 0))}</td>
              <td style="text-align:right;font-family:var(--mono);font-size:11px">${fmtCost(parseFloat(a.attributed_cost || 0))}</td>
              <td style="text-align:right;font-family:var(--mono);font-weight:700;color:${ratioColor};font-size:11px">${ratio.toFixed(2)}x</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

/**
 * Render retry tax table (cost from retries, timeouts, rate limits).
 * @param {Array} items
 */
function _renderRetryTax(items) {
  const body = document.getElementById('sessions-retry-tax-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('sessions-retry-tax', {
      icon: 'fa-solid fa-arrows-rotate',
      title: 'No retry data',
      description: 'Retry tax shows wasted spend from retries, timeouts, and rate-limit re-attempts.',
    });
    return;
  }

  const totalTax = items.reduce((s, a) => s + parseFloat(a.retry_tax || 0), 0);

  body.innerHTML = `
    <div style="padding:12px">
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">Total Retry Tax</div>
      <div style="font-size:28px;font-weight:700;color:var(--danger);margin-bottom:16px">${fmtCost(totalTax)}</div>
      <div style="overflow-x:auto">
        <table class="ds-table" style="width:100%;font-size:12px">
          <thead>
            <tr>
              <th>Node</th>
              <th>App</th>
              <th style="text-align:right">Direct Cost</th>
              <th style="text-align:right">Retry Tax</th>
              <th style="text-align:right">% of Direct</th>
            </tr>
          </thead>
          <tbody>
            ${items.map(a => {
              const cost = parseFloat(a.retry_tax || 0);
              const direct = parseFloat(a.direct_cost || 0);
              const pct = parseFloat(a.retry_tax_pct || 0).toFixed(1);
              return `<tr>
                <td style="font-weight:500;font-size:11px">${esc(a.node_label || '\u2014')}</td>
                <td style="font-size:10px;font-family:var(--mono);color:var(--muted)">${esc((a.app_id || '').slice(0, 10))}</td>
                <td style="text-align:right;font-family:var(--mono);font-size:11px">${fmtCost(direct)}</td>
                <td style="text-align:right;font-family:var(--mono);color:var(--danger);font-size:11px">${fmtCost(cost)}</td>
                <td style="text-align:right;font-family:var(--mono);font-size:11px">${pct}%</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

/**
 * Render defensive spend table (safety-critical AI validations).
 * @param {Array} items
 */
function _renderDefensive(items) {
  const body = document.getElementById('sessions-defensive-body');
  if (!body) return;

  if (!items || !Array.isArray(items) || items.length === 0) {
    setTileEmpty('sessions-defensive', {
      icon: 'fa-solid fa-shield-halved',
      title: 'No defensive spend data',
      description: 'Tracks safety-critical AI spend (validations, double-checks, guard rails).',
    });
    return;
  }

  const totalDef = items.reduce((s, a) => s + parseFloat(a.defensive_spend || 0), 0);

  body.innerHTML = `
    <div style="padding:12px">
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:0.5px;margin-bottom:4px">Total Defensive Spend</div>
      <div style="font-size:28px;font-weight:700;color:#a855f7;margin-bottom:16px">${fmtCost(totalDef)}</div>
      <div style="overflow-x:auto">
        <table class="ds-table" style="width:100%;font-size:12px">
          <thead>
            <tr>
              <th>Node</th>
              <th>App</th>
              <th>Type</th>
              <th style="text-align:right">Direct Cost</th>
              <th style="text-align:right">Defensive Spend</th>
            </tr>
          </thead>
          <tbody>
            ${items.map(a => {
              const cost = parseFloat(a.defensive_spend || 0);
              const direct = parseFloat(a.direct_cost || 0);
              const isDef = a.is_defensive ? 'guard' : 'fallback';
              return `<tr>
                <td style="font-weight:500;font-size:11px">${esc(a.node_label || '\u2014')}</td>
                <td style="font-size:10px;font-family:var(--mono);color:var(--muted)">${esc((a.app_id || '').slice(0, 10))}</td>
                <td style="font-size:11px"><span style="font-size:10px;padding:2px 6px;border-radius:3px;background:rgba(168,85,247,0.1);color:#a855f7;white-space:nowrap">${isDef}</span></td>
                <td style="text-align:right;font-family:var(--mono);font-size:11px">${fmtCost(direct)}</td>
                <td style="text-align:right;font-family:var(--mono);font-size:11px;color:#a855f7">${fmtCost(cost)}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

// ── Filter helper ────────────────────────────────────────────────────────────

/**
 * Filter table rows by query string (case-insensitive).
 * @param {string} bodyId
 * @param {string} query
 */
function _filterRows(bodyId, query) {
  const body = document.getElementById(bodyId);
  if (!body) return;

  const q = (query || '').toLowerCase();
  const rows = body.querySelectorAll('tbody tr');
  rows.forEach(row => {
    const text = row.textContent.toLowerCase();
    row.style.display = text.includes(q) ? '' : 'none';
  });
}
