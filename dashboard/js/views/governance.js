/**
 * Modus Dashboard v2 — Governance View
 * Renders the governance dashboard with 5 Gridstack tiles:
 * Status KPIs, CoT Ledger Timeline, Proposals table, Rewind events,
 * and Evolution status.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError, setTileMeta } from '../tile.js';
import { rawFetch, esc, loadAppNames, appName } from '../api.js';
import { fmtCost, fmtNum, fmtDate } from '../format.js';
import { get } from '../state.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _ledgerEntries = [];  // cached for modal lookups

// ── Decision type config ─────────────────────────────────────────────────────

const DECISION_COLORS = {
  governance_proposal: { bg: 'rgba(168,85,247,0.12)', color: '#a855f7', label: 'Governance Proposal' },
  evolution_proposal:  { bg: 'rgba(59,130,246,0.12)',  color: '#3b82f6', label: 'Evolution Proposal' },
  anomaly_signal:      { bg: 'rgba(245,158,11,0.12)',  color: '#f59e0b', label: 'Anomaly Signal' },
  policy_applied:      { bg: 'rgba(0,229,160,0.12)',   color: 'var(--accent)', label: 'Policy Applied' },
  policy_dismissed:    { bg: 'rgba(255,107,107,0.12)', color: '#ff6b6b', label: 'Policy Dismissed' },
};

const TRIGGER_COLORS = {
  pattern_detection: { bg: 'rgba(59,130,246,0.10)', color: '#3b82f6' },
  evolution_cycle:   { bg: 'rgba(168,85,247,0.10)', color: '#a855f7' },
  rewind_event:      { bg: 'rgba(245,158,11,0.10)', color: '#f59e0b' },
  anomaly_detection: { bg: 'rgba(255,107,107,0.10)', color: '#ff6b6b' },
  manual:            { bg: 'rgba(255,255,255,0.06)', color: 'var(--muted)' },
};

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Governance view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'governance');

  const tiles = [
    _createStatusTile(),
    _createCotLedgerTile(),
    _createProposalsTile(),
    _createRewindTile(),
    _createEvolutionTile(),
  ];

  const layout = loadLayout('governance', LAYOUTS.governance);
  addTiles(_grid, tiles, layout);

  await loadData();
}

/**
 * Tear down the Governance view.
 */
export function destroy() {
  _destroyed = true;
  _grid = null;
  _container = null;
  _ledgerEntries = [];
}

/**
 * Re-fetch this view's data. Called by the Settings -> Auto-refresh timer
 * (js/auto-refresh.js); a no-op once the view has been destroyed.
 */
export function refresh() {
  return _destroyed ? Promise.resolve() : loadData();
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createStatusTile() {
  return createTile({
    id: 'gov-status',
    title: 'Governance Health',
    icon: 'fa-solid fa-shield-halved',
    iconBg: 'rgba(0,229,160,0.1)',
    iconColor: 'var(--accent)',
  });
}

function _createCotLedgerTile() {
  const tile = createTile({
    id: 'gov-cot-ledger',
    title: 'Chain-of-Thought Ledger',
    icon: 'fa-solid fa-link',
    iconBg: 'rgba(59,130,246,0.1)',
    iconColor: '#3b82f6',
    actions: `
      <button class="ds-btn ds-btn-sm ds-btn-ghost cot-verify-btn" title="Verify chain integrity">
        <i class="fa-solid fa-shield-check" style="margin-right:4px"></i>Verify Chain
      </button>
      <div class="cot-export-wrap" style="position:relative;display:inline-block">
        <button class="ds-btn ds-btn-sm ds-btn-ghost cot-export-btn" title="Export ledger">
          <i class="fa-solid fa-download" style="margin-right:4px"></i>Export<i class="fa-solid fa-chevron-down" style="margin-left:4px;font-size:8px"></i>
        </button>
        <div class="cot-export-dropdown" style="display:none;position:absolute;top:100%;right:0;margin-top:4px;background:var(--card);border:1px solid var(--border);border-radius:6px;box-shadow:0 8px 24px rgba(0,0,0,0.3);z-index:50;min-width:100px;overflow:hidden">
          <button class="cot-export-json" style="display:block;width:100%;padding:8px 14px;background:none;border:none;color:var(--text);font-size:12px;text-align:left;cursor:pointer">JSON</button>
          <button class="cot-export-csv" style="display:block;width:100%;padding:8px 14px;background:none;border:none;color:var(--text);font-size:12px;text-align:left;cursor:pointer;border-top:1px solid var(--border)">CSV</button>
        </div>
      </div>
    `,
  });

  // Wire up header action buttons after tile is created
  const verifyBtn = tile.querySelector('.cot-verify-btn');
  if (verifyBtn) verifyBtn.addEventListener('click', _verifyChain);

  const exportBtn = tile.querySelector('.cot-export-btn');
  const dropdown = tile.querySelector('.cot-export-dropdown');
  if (exportBtn && dropdown) {
    exportBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      dropdown.style.display = dropdown.style.display === 'none' ? 'block' : 'none';
    });
    // Close dropdown on outside click
    document.addEventListener('click', () => { dropdown.style.display = 'none'; });
  }

  const jsonBtn = tile.querySelector('.cot-export-json');
  if (jsonBtn) jsonBtn.addEventListener('click', () => { window.open('/api/v1/governance/cot-ledger/export?format=json'); });

  const csvBtn = tile.querySelector('.cot-export-csv');
  if (csvBtn) csvBtn.addEventListener('click', () => { window.open('/api/v1/governance/cot-ledger/export?format=csv'); });

  return tile;
}

function _createProposalsTile() {
  return createTile({
    id: 'gov-proposals',
    title: 'Proposals',
    icon: 'fa-solid fa-file-lines',
    iconBg: 'rgba(59,130,246,0.1)',
    iconColor: '#3b82f6',
    filterable: true,
    filterPlaceholder: 'Filter proposals\u2026',
    onFilter: (q) => _filterTable('gov-proposals-body', q),
  });
}

function _createRewindTile() {
  return createTile({
    id: 'gov-rewind',
    title: 'Rewind Events',
    icon: 'fa-solid fa-rotate-left',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
  });
}

function _createEvolutionTile() {
  return createTile({
    id: 'gov-evolution',
    title: 'Constitutional Evolution',
    icon: 'fa-solid fa-dna',
    iconBg: 'rgba(168,85,247,0.1)',
    iconColor: '#a855f7',
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function loadData() {
  if (_destroyed) return;

  setTileLoading('gov-status', 'cards');
  setTileLoading('gov-cot-ledger', 'table');
  setTileLoading('gov-proposals', 'table');
  setTileLoading('gov-rewind', 'table');
  setTileLoading('gov-evolution', 'text');

  try {
    await loadAppNames();
    const [stats, proposals, rewinds, evolution, ledgerEntries, ledgerStats, ledgerVerify] = await Promise.all([
      rawFetch('/api/v1/governance/stats').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/governance/proposals?limit=50').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/governance/rewind-events?limit=20').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/governance/evolution/status').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/governance/cot-ledger/entries?limit=50').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/governance/cot-ledger/stats').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/governance/cot-ledger/verify').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);

    if (_destroyed) return;

    _renderStatus(stats);
    _renderCotLedger(Array.isArray(ledgerEntries) ? ledgerEntries : [], ledgerStats, ledgerVerify);
    _renderProposals(Array.isArray(proposals) ? proposals : []);
    _renderRewinds(Array.isArray(rewinds) ? rewinds : []);
    _renderEvolution(evolution);
  } catch (err) {
    if (_destroyed) return;
    console.error('[governance] data fetch failed:', err);
    _setAllTilesError();
  }
}

// ── Tile renderers ───────────────────────────────────────────────────────────

function _renderStatus(stats) {
  const body = document.getElementById('gov-status-body');
  if (!body) return;

  if (!stats) {
    setTileEmpty('gov-status', { icon: 'fa-solid fa-shield-halved', title: 'No governance data' });
    return;
  }

  const pending = stats.pending || 0;
  const applied = stats.applied || 0;
  const dismissed = stats.dismissed || 0;
  const savings = fmtCost(stats.total_estimated_savings || 0);

  body.innerHTML = `
    <div style="display:flex;flex-wrap:wrap;gap:16px;padding:12px 16px;align-items:center">
      <div class="kpi-card" style="flex:1;min-width:100px">
        <div class="kpi-label">Pending</div>
        <div class="kpi-value" style="color:${pending > 0 ? 'var(--warn)' : 'var(--muted)'}">${pending}</div>
      </div>
      <div class="kpi-card" style="flex:1;min-width:100px">
        <div class="kpi-label">Applied</div>
        <div class="kpi-value" style="color:var(--accent)">${applied}</div>
      </div>
      <div class="kpi-card" style="flex:1;min-width:100px">
        <div class="kpi-label">Dismissed</div>
        <div class="kpi-value" style="color:var(--muted)">${dismissed}</div>
      </div>
      <div class="kpi-card" style="flex:1;min-width:100px">
        <div class="kpi-label">Est. Savings</div>
        <div class="kpi-value" style="color:var(--accent)">${savings}</div>
      </div>
    </div>
  `;
}

// ── CoT Ledger ───────────────────────────────────────────────────────────────

function _renderCotLedger(entries, stats, verify) {
  const body = document.getElementById('gov-cot-ledger-body');
  if (!body) return;

  _ledgerEntries = entries;

  if (!entries || entries.length === 0) {
    setTileEmpty('gov-cot-ledger', {
      icon: 'fa-solid fa-link',
      title: 'No ledger entries',
      description: 'Chain-of-thought decisions will appear here as governance events occur.',
    });
    return;
  }

  if (stats) {
    const total = stats.total_entries || entries.length;
    const verified = verify && typeof verify.valid === 'boolean' ? (verify.valid ? 'Verified' : 'Broken') : null;
    let metaText = `${fmtNum(total)} entries`;
    if (verified) metaText += ` \u00b7 Chain: ${verified}`;
    setTileMeta('gov-cot-ledger', metaText);
  }

  // Build filter bar + timeline
  body.innerHTML = `
    <div class="cot-filter-bar" style="display:flex;flex-wrap:nowrap;gap:8px;padding:10px 16px;border-bottom:1px solid var(--border);align-items:center;overflow-x:auto">
      <select class="cot-filter-type" style="font-size:11px;padding:4px 8px;width:160px;max-width:160px;flex:0 0 auto;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px">
        <option value="">All Types</option>
        <option value="governance_proposal">Governance Proposal</option>
        <option value="evolution_proposal">Evolution Proposal</option>
        <option value="anomaly_signal">Anomaly Signal</option>
        <option value="policy_applied">Policy Applied</option>
        <option value="policy_dismissed">Policy Dismissed</option>
      </select>
      <select class="cot-filter-trigger" style="font-size:11px;padding:4px 8px;width:150px;max-width:150px;flex:0 0 auto;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px">
        <option value="">All Triggers</option>
        <option value="pattern_detection">Pattern Detection</option>
        <option value="evolution_cycle">Evolution Cycle</option>
        <option value="rewind_event">Rewind Event</option>
        <option value="anomaly_detection">Anomaly Detection</option>
        <option value="manual">Manual</option>
      </select>
      <input type="date" class="cot-filter-from" style="font-size:11px;padding:4px 8px;width:130px;max-width:130px;flex:0 0 auto;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px" title="From date" />
      <input type="date" class="cot-filter-to" style="font-size:11px;padding:4px 8px;width:130px;max-width:130px;flex:0 0 auto;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:4px" title="To date" />
      <button class="ds-btn ds-btn-sm ds-btn-ghost cot-filter-clear" style="font-size:11px;flex:0 0 auto">Clear</button>
    </div>
    <div class="cot-timeline" style="overflow-y:auto;padding:12px 16px;display:flex;flex-direction:column;gap:8px;max-height:480px">
      ${entries.map((entry, idx) => _renderLedgerEntry(entry, idx)).join('')}
    </div>
  `;

  // Wire up filter controls
  const typeFilter = body.querySelector('.cot-filter-type');
  const triggerFilter = body.querySelector('.cot-filter-trigger');
  const fromFilter = body.querySelector('.cot-filter-from');
  const toFilter = body.querySelector('.cot-filter-to');
  const clearBtn = body.querySelector('.cot-filter-clear');

  const applyFilters = () => _applyLedgerFilters(body, entries);
  if (typeFilter) typeFilter.addEventListener('change', applyFilters);
  if (triggerFilter) triggerFilter.addEventListener('change', applyFilters);
  if (fromFilter) fromFilter.addEventListener('change', applyFilters);
  if (toFilter) toFilter.addEventListener('change', applyFilters);
  if (clearBtn) {
    clearBtn.addEventListener('click', () => {
      if (typeFilter) typeFilter.value = '';
      if (triggerFilter) triggerFilter.value = '';
      if (fromFilter) fromFilter.value = '';
      if (toFilter) toFilter.value = '';
      applyFilters();
    });
  }

  // Wire up entry click handlers
  body.querySelectorAll('.cot-entry').forEach(el => {
    el.addEventListener('click', () => {
      const idx = parseInt(el.getAttribute('data-entry-idx'), 10);
      if (_ledgerEntries[idx]) _showLedgerEntryModal(_ledgerEntries[idx]);
    });
    el.addEventListener('mouseenter', () => { el.style.background = 'rgba(255,255,255,0.02)'; });
    el.addEventListener('mouseleave', () => { el.style.background = ''; });
  });
}

function _renderLedgerEntry(entry, idx) {
  const dt = DECISION_COLORS[entry.decision_type] || { bg: 'rgba(255,255,255,0.06)', color: 'var(--muted)', label: entry.decision_type || 'Unknown' };
  const trig = TRIGGER_COLORS[entry.trigger] || { bg: 'rgba(255,255,255,0.06)', color: 'var(--muted)' };
  const trigLabel = (entry.trigger || 'unknown').replace(/_/g, ' ');

  const ts = entry.created_at ? new Date(entry.created_at) : null;
  const timeStr = ts ? ts.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '\u2014';

  const hash = entry.entry_hash ? entry.entry_hash.substring(0, 16) + '\u2026' : '';

  const regTags = (entry.regulatory_tags && Array.isArray(entry.regulatory_tags))
    ? entry.regulatory_tags.map(t => `<span style="display:inline-block;padding:1px 6px;border-radius:3px;background:rgba(255,255,255,0.04);color:var(--muted);font-size:9px;font-family:var(--mono)">${esc(t)}</span>`).join('')
    : '';

  return `
    <div class="cot-entry" data-entry-idx="${idx}" data-type="${esc(entry.decision_type || '')}" data-trigger="${esc(entry.trigger || '')}" data-ts="${entry.created_at || ''}"
         style="display:flex;gap:12px;padding:10px 12px;border:1px solid var(--border);border-radius:8px;cursor:pointer;transition:background 0.15s,border-color 0.15s">
      <div class="cot-entry-time" style="flex-shrink:0;width:100px;font-family:var(--mono);font-size:10px;color:var(--muted);padding-top:2px;line-height:1.5">
        ${timeStr}
      </div>
      <div style="flex:1;min-width:0">
        <div style="display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:4px">
          <span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;background:${dt.bg};color:${dt.color}">${esc(dt.label)}</span>
          <span style="display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;background:${trig.bg};color:${trig.color}">${esc(trigLabel)}</span>
          ${regTags ? `<span style="display:inline-flex;gap:3px">${regTags}</span>` : ''}
        </div>
        <div style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:3px">${esc(entry.decision_summary || 'Untitled Decision')}</div>
        ${hash ? `<div style="font-family:var(--mono);font-size:9px;color:var(--muted);opacity:0.6">${esc(hash)}</div>` : ''}
      </div>
    </div>
  `;
}

function _applyLedgerFilters(body, entries) {
  const typeVal = body.querySelector('.cot-filter-type')?.value || '';
  const triggerVal = body.querySelector('.cot-filter-trigger')?.value || '';
  const fromVal = body.querySelector('.cot-filter-from')?.value || '';
  const toVal = body.querySelector('.cot-filter-to')?.value || '';

  const fromDate = fromVal ? new Date(fromVal + 'T00:00:00') : null;
  const toDate = toVal ? new Date(toVal + 'T23:59:59') : null;

  body.querySelectorAll('.cot-entry').forEach(el => {
    const type = el.getAttribute('data-type');
    const trigger = el.getAttribute('data-trigger');
    const ts = el.getAttribute('data-ts');
    const entryDate = ts ? new Date(ts) : null;

    let visible = true;
    if (typeVal && type !== typeVal) visible = false;
    if (triggerVal && trigger !== triggerVal) visible = false;
    if (fromDate && entryDate && entryDate < fromDate) visible = false;
    if (toDate && entryDate && entryDate > toDate) visible = false;

    el.style.display = visible ? '' : 'none';
  });
}

// ── Ledger entry detail modal ────────────────────────────────────────────────

function _showLedgerEntryModal(entry) {
  const dt = DECISION_COLORS[entry.decision_type] || { bg: 'rgba(255,255,255,0.06)', color: 'var(--muted)', label: entry.decision_type || 'Unknown' };
  const ts = entry.created_at ? new Date(entry.created_at).toLocaleString() : '\u2014';

  openModal({
    title: 'CoT Ledger Entry',
    maxWidth: '680px',
    renderBody: (modalBody) => {
      // Evidence Snapshot
      const evidenceHtml = entry.evidence_snapshot
        ? `<pre style="background:rgba(0,0,0,0.3);border:1px solid var(--border);border-radius:6px;padding:10px;font-family:var(--mono);font-size:11px;color:var(--text);overflow-x:auto;max-height:200px;white-space:pre-wrap">${esc(JSON.stringify(entry.evidence_snapshot, null, 2))}</pre>`
        : '<span style="color:var(--muted);font-size:12px">No evidence snapshot</span>';

      // Rules Evaluated
      let rulesHtml = '<span style="color:var(--muted);font-size:12px">No rules evaluated</span>';
      if (entry.rules_evaluated && Array.isArray(entry.rules_evaluated) && entry.rules_evaluated.length > 0) {
        rulesHtml = `
          <table class="ds-table" style="width:100%;font-size:11px">
            <thead><tr><th>Rule</th><th>Fired</th><th>Confidence</th><th>Detail</th></tr></thead>
            <tbody>
              ${entry.rules_evaluated.map(r => `
                <tr>
                  <td style="font-weight:500">${esc(r.rule || r.name || '')}</td>
                  <td><span style="color:${r.fired ? 'var(--accent)' : 'var(--muted)'}">${r.fired ? 'Yes' : 'No'}</span></td>
                  <td style="font-family:var(--mono)">${r.confidence != null ? (r.confidence * 100).toFixed(0) + '%' : '\u2014'}</td>
                  <td style="color:var(--muted)">${esc(r.detail || '')}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      }

      // Reasoning Steps
      let reasoningHtml = '<span style="color:var(--muted);font-size:12px">No reasoning steps</span>';
      if (entry.reasoning_steps && Array.isArray(entry.reasoning_steps) && entry.reasoning_steps.length > 0) {
        reasoningHtml = `
          <ol style="margin:0;padding-left:20px;font-size:12px;color:var(--text);line-height:1.8">
            ${entry.reasoning_steps.map(step => `<li>${esc(typeof step === 'string' ? step : step.description || JSON.stringify(step))}</li>`).join('')}
          </ol>
        `;
      }

      // Alternatives Considered
      let alternativesHtml = '<span style="color:var(--muted);font-size:12px">No alternatives recorded</span>';
      if (entry.alternatives_considered && Array.isArray(entry.alternatives_considered) && entry.alternatives_considered.length > 0) {
        alternativesHtml = `
          <table class="ds-table" style="width:100%;font-size:11px">
            <thead><tr><th>Action</th><th>Rejected Because</th></tr></thead>
            <tbody>
              ${entry.alternatives_considered.map(a => `
                <tr>
                  <td style="font-weight:500">${esc(a.action || '')}</td>
                  <td style="color:var(--muted)">${esc(a.rejected_because || a.reason || '')}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      }

      // Linked Entities
      const links = [];
      if (entry.linked_proposal_id) links.push({ label: 'Proposal ID', value: entry.linked_proposal_id });
      if (entry.linked_policy_id) links.push({ label: 'Policy ID', value: entry.linked_policy_id });
      if (entry.linked_evolution_gen_id) links.push({ label: 'Evolution Gen ID', value: entry.linked_evolution_gen_id });

      const linkedHtml = links.length > 0
        ? `<div style="display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:11px">
            ${links.map(l => `
              <span style="color:var(--muted)">${esc(l.label)}</span>
              <span style="font-family:var(--mono);color:var(--text)">${esc(String(l.value))}</span>
            `).join('')}
          </div>`
        : '<span style="color:var(--muted);font-size:12px">No linked entities</span>';

      modalBody.innerHTML = `
        <div style="display:flex;flex-direction:column;gap:16px;padding:4px 0">
          <!-- Header -->
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px">
            <div>
              <div style="font-weight:600;font-size:14px;color:var(--text);margin-bottom:6px">${esc(entry.decision_summary || 'Untitled Decision')}</div>
              <div style="display:flex;flex-wrap:wrap;gap:6px;align-items:center">
                <span style="display:inline-block;padding:2px 8px;border-radius:4px;font-size:10px;font-weight:600;background:${dt.bg};color:${dt.color}">${esc(dt.label)}</span>
                <span style="font-size:11px;color:var(--muted)">${ts}</span>
              </div>
            </div>
          </div>

          <!-- Evidence Snapshot -->
          <div>
            <div style="font-weight:600;font-size:12px;color:var(--text);margin-bottom:6px"><i class="fa-solid fa-magnifying-glass" style="margin-right:4px;opacity:0.5"></i>Evidence Snapshot</div>
            ${evidenceHtml}
          </div>

          <!-- Rules Evaluated -->
          <div>
            <div style="font-weight:600;font-size:12px;color:var(--text);margin-bottom:6px"><i class="fa-solid fa-list-check" style="margin-right:4px;opacity:0.5"></i>Rules Evaluated</div>
            ${rulesHtml}
          </div>

          <!-- Reasoning Steps -->
          <div>
            <div style="font-weight:600;font-size:12px;color:var(--text);margin-bottom:6px"><i class="fa-solid fa-brain" style="margin-right:4px;opacity:0.5"></i>Reasoning Steps</div>
            ${reasoningHtml}
          </div>

          <!-- Alternatives Considered -->
          <div>
            <div style="font-weight:600;font-size:12px;color:var(--text);margin-bottom:6px"><i class="fa-solid fa-code-branch" style="margin-right:4px;opacity:0.5"></i>Alternatives Considered</div>
            ${alternativesHtml}
          </div>

          <!-- Linked Entities -->
          <div>
            <div style="font-weight:600;font-size:12px;color:var(--text);margin-bottom:6px"><i class="fa-solid fa-link" style="margin-right:4px;opacity:0.5"></i>Linked Entities</div>
            ${linkedHtml}
          </div>

          <!-- Audit Hashes -->
          <div style="border-top:1px solid var(--border);padding-top:12px">
            <div style="font-weight:600;font-size:12px;color:var(--text);margin-bottom:6px"><i class="fa-solid fa-fingerprint" style="margin-right:4px;opacity:0.5"></i>Audit Trail</div>
            <div style="display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:11px">
              <span style="color:var(--muted)">Entry Hash</span>
              <span style="font-family:var(--mono);color:var(--text);word-break:break-all">${esc(entry.entry_hash || '\u2014')}</span>
              <span style="color:var(--muted)">Prev Hash</span>
              <span style="font-family:var(--mono);color:var(--text);word-break:break-all">${esc(entry.prev_hash || '\u2014')}</span>
            </div>
          </div>
        </div>
      `;
    },
  });
}

// ── Verify Chain ─────────────────────────────────────────────────────────────

async function _verifyChain() {
  try {
    const resp = await rawFetch('/api/v1/governance/cot-ledger/verify');
    const result = resp.ok ? await resp.json() : null;

    if (_destroyed) return;

    openModal({
      title: 'Chain Verification',
      maxWidth: '420px',
      renderBody: (modalBody) => {
        if (!result) {
          modalBody.innerHTML = `<div style="padding:8px 0;font-size:13px;color:var(--danger)"><i class="fa-solid fa-triangle-exclamation" style="margin-right:6px"></i>Failed to verify chain. The API may be unavailable.</div>`;
          return;
        }

        const valid = result.valid === true;
        const icon = valid ? 'fa-solid fa-circle-check' : 'fa-solid fa-circle-xmark';
        const color = valid ? 'var(--accent)' : 'var(--danger)';
        const status = valid ? 'Chain Integrity Verified' : 'Chain Integrity Broken';
        const totalEntries = result.entries_checked ?? '\u2014';
        const brokenAt = result.first_invalid_seq != null ? result.first_invalid_seq : null;

        modalBody.innerHTML = `
          <div style="display:flex;flex-direction:column;align-items:center;gap:12px;padding:16px 0">
            <i class="${icon}" style="font-size:48px;color:${color}"></i>
            <div style="font-size:16px;font-weight:600;color:${color}">${status}</div>
            <div style="font-size:12px;color:var(--muted)">${totalEntries} entries checked</div>
            ${brokenAt != null ? `<div style="font-size:12px;color:var(--danger)">Break detected at sequence ${brokenAt}</div>` : ''}
            ${result.error ? `<div style="font-size:12px;color:var(--muted);text-align:center">${esc(result.error)}</div>` : ''}
          </div>
        `;
      },
    });
  } catch (err) {
    console.error('[governance] chain verify failed:', err);
  }
}

// ── Proposals ────────────────────────────────────────────────────────────────

function _renderProposals(proposals) {
  const body = document.getElementById('gov-proposals-body');
  if (!body) return;

  if (!proposals || proposals.length === 0) {
    setTileEmpty('gov-proposals', {
      icon: 'fa-solid fa-file-lines',
      title: 'No proposals',
      description: 'Governance proposals will appear here.',
    });
    return;
  }

  setTileMeta('gov-proposals', `${proposals.length} proposal${proposals.length !== 1 ? 's' : ''}`);

  body.innerHTML = `
    <div style="overflow:auto;max-height:380px">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead style="position:sticky;top:0;background:var(--surface);z-index:1">
          <tr>
            <th>Title</th>
            <th>Type</th>
            <th>Severity</th>
            <th>Source</th>
            <th>Status</th>
            <th>Created</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${proposals.map((p, idx) => {
            const sevClass = p.severity === 'critical' ? 'danger' : p.severity === 'warning' ? 'warning' : 'info';
            const statusClass = p.status === 'applied' ? 'success' : p.status === 'pending' ? 'warning' : 'neutral';
            const created = p.created_at ? new Date(p.created_at).toLocaleDateString() : '\u2014';
            return `<tr data-proposal-idx="${idx}">
              <td style="font-weight:500;color:var(--text)">${esc(p.title || '')}</td>
              <td><span class="ds-badge-neutral">${esc(p.proposal_type || '')}</span></td>
              <td><span class="ds-badge-${sevClass}">${esc(p.severity || '')}</span></td>
              <td style="color:var(--muted)">${esc(p.source || '')}</td>
              <td><span class="ds-badge-${statusClass}">${esc(p.status || '')}</span></td>
              <td style="font-family:var(--mono);font-size:11px">${created}</td>
              <td>
                ${p.status === 'pending' ? `
                  <button class="ds-btn ds-btn-sm ds-btn-primary proposal-apply-btn" data-id="${esc(p.id || '')}">Apply</button>
                  <button class="ds-btn ds-btn-sm ds-btn-ghost proposal-dismiss-btn" data-id="${esc(p.id || '')}">Dismiss</button>
                ` : '\u2014'}
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;

  // Attach action handlers
  body.querySelectorAll('.proposal-apply-btn').forEach(btn => {
    btn.addEventListener('click', () => _applyProposal(btn.getAttribute('data-id')));
  });
  body.querySelectorAll('.proposal-dismiss-btn').forEach(btn => {
    btn.addEventListener('click', () => _dismissProposal(btn.getAttribute('data-id')));
  });
}

function _renderRewinds(rewinds) {
  const body = document.getElementById('gov-rewind-body');
  if (!body) return;

  if (!rewinds || rewinds.length === 0) {
    setTileEmpty('gov-rewind', {
      icon: 'fa-solid fa-rotate-left',
      title: 'No rewind events',
      description: 'Rewind history will appear here when sessions are rolled back.',
    });
    return;
  }

  body.innerHTML = `
    <div style="overflow-x:auto">
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead>
          <tr>
            <th>Session</th>
            <th>App</th>
            <th>Trigger</th>
            <th>Rolled Back</th>
            <th>Date</th>
          </tr>
        </thead>
        <tbody>
          ${rewinds.map(r => {
            const rolledBack = Array.isArray(r.actions_rolled_back) ? r.actions_rolled_back.length : 0;
            const created = r.created_at ? new Date(r.created_at).toLocaleDateString() : '\u2014';
            return `<tr>
              <td style="font-family:var(--mono);font-size:11px">${esc(r.session_id || '\u2014')}</td>
              <td>${esc(appName(r.app_id))}</td>
              <td><span class="ds-badge-warning">${esc(r.trigger_reason || '')}</span></td>
              <td style="text-align:center;font-family:var(--mono)">${rolledBack}</td>
              <td style="font-family:var(--mono);font-size:11px">${created}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function _renderEvolution(evo) {
  const body = document.getElementById('gov-evolution-body');
  if (!body) return;

  if (!evo) {
    setTileEmpty('gov-evolution', {
      icon: 'fa-solid fa-dna',
      title: 'No evolution data',
      description: 'Constitutional evolution status will appear here.',
    });
    return;
  }

  const gen = evo.latest_generation ?? 0;
  const best = evo.latest_best_fitness;
  const fitness = typeof best === 'number' ? best.toFixed(3) : '\u2014';
  const mutations = evo.total_mutations || 0;
  const population = evo.latest_population_size || 0;
  const fitnessColor = best >= 0.8 ? 'var(--accent)' : best >= 0.5 ? 'var(--warn)' : 'var(--danger)';

  body.innerHTML = `
    <div style="display:flex;flex-wrap:wrap;gap:20px;padding:16px">
      <div class="kpi-card" style="flex:1;min-width:120px">
        <div class="kpi-label">Generation</div>
        <div class="kpi-value">${gen}</div>
      </div>
      <div class="kpi-card" style="flex:1;min-width:120px">
        <div class="kpi-label">Fitness Score</div>
        <div class="kpi-value" style="color:${fitnessColor}">${fitness}</div>
      </div>
      <div class="kpi-card" style="flex:1;min-width:120px">
        <div class="kpi-label">Total Mutations</div>
        <div class="kpi-value">${fmtNum(mutations)}</div>
      </div>
      <div class="kpi-card" style="flex:1;min-width:120px">
        <div class="kpi-label">Population Size</div>
        <div class="kpi-value">${fmtNum(population)}</div>
      </div>
    </div>
    <div style="padding:0 16px 16px;font-size:12px;color:var(--muted)">
      The constitutional evolution engine continuously optimizes governance rules
      through generational fitness evaluation. Higher fitness scores indicate
      better alignment with organizational policies.
    </div>
  `;
}

// ── Actions ──────────────────────────────────────────────────────────────────

async function _applyProposal(id) {
  if (!id) return;

  openModal({
    title: 'Apply Proposal',
    maxWidth: '400px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="padding:8px 0">
          <p style="color:var(--text);font-size:13px;margin:0 0 16px">Are you sure you want to apply this governance proposal?</p>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="ds-btn ds-btn-ghost" id="apply-cancel">Cancel</button>
            <button class="ds-btn ds-btn-primary" id="apply-confirm">Apply</button>
          </div>
        </div>
      `;
      body.querySelector('#apply-cancel').onclick = () => closeModal(body.closest('.ds-modal-backdrop').id);
      body.querySelector('#apply-confirm').onclick = async () => {
        closeModal(body.closest('.ds-modal-backdrop').id);
        try {
          const resp = await rawFetch(`/api/v1/governance/proposals/${id}/apply`, { method: 'POST' });
          if (resp.ok && !_destroyed) loadData();
        } catch (e) {
          console.error('[governance] apply failed:', e);
        }
      };
    },
  });
}

async function _dismissProposal(id) {
  if (!id) return;

  openModal({
    title: 'Dismiss Proposal',
    maxWidth: '400px',
    renderBody: (body) => {
      body.innerHTML = `
        <div style="padding:8px 0">
          <label style="color:var(--text);font-size:13px;display:block;margin-bottom:8px">Reason for dismissal:</label>
          <input type="text" class="ds-input" id="dismiss-reason" placeholder="Enter reason\u2026" style="width:100%;margin-bottom:16px" />
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="ds-btn ds-btn-ghost" id="dismiss-cancel">Cancel</button>
            <button class="ds-btn ds-btn-primary" id="dismiss-confirm">Dismiss</button>
          </div>
        </div>
      `;
      body.querySelector('#dismiss-cancel').onclick = () => closeModal(body.closest('.ds-modal-backdrop').id);
      body.querySelector('#dismiss-confirm').onclick = async () => {
        const reason = body.querySelector('#dismiss-reason').value.trim();
        if (!reason) return;
        closeModal(body.closest('.ds-modal-backdrop').id);
        try {
          const resp = await rawFetch(`/api/v1/governance/proposals/${id}/dismiss`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reason }),
          });
          if (resp.ok && !_destroyed) loadData();
        } catch (e) {
          console.error('[governance] dismiss failed:', e);
        }
      };
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
  const ids = ['gov-status', 'gov-cot-ledger', 'gov-proposals', 'gov-rewind', 'gov-evolution'];
  ids.forEach(id => setTileError(id, 'Failed to load governance data', () => loadData()));
}
