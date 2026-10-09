/**
 * Modus Dashboard v2 — Compliance View
 * Renders the compliance dashboard with Gridstack tiles:
 * Attestation chain stats and PQC readiness.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError } from '../tile.js';
import { rawFetch } from '../api.js';
import { fmtNum } from '../format.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;

// ── Public API ───────────────────────────────────────────────────────────────

/**
 * Render the Compliance view into the given container element.
 * @param {HTMLElement} container
 */
export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'compliance');

  const tiles = [
    _createAttestationTile(),
    _createPqcTile(),
  ];

  const layout = loadLayout('compliance', LAYOUTS.compliance);
  addTiles(_grid, tiles, layout);

  await loadData();
}

/**
 * Tear down the Compliance view.
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

function _createAttestationTile() {
  return createTile({
    id: 'comp-attestation',
    title: 'Attestation Chain',
    icon: 'fa-solid fa-link',
    iconBg: 'rgba(0,229,160,0.1)',
    iconColor: 'var(--accent)',
    actions: [{
      label: 'Verify',
      icon: 'fa-solid fa-check-double',
      onclick: () => _showVerifyModal(),
    }],
  });
}

function _createPqcTile() {
  return createTile({
    id: 'comp-pqc',
    title: 'Post-Quantum Cryptography',
    icon: 'fa-solid fa-atom',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function loadData() {
  if (_destroyed) return;

  setTileLoading('comp-attestation', 'cards');
  setTileLoading('comp-pqc', 'cards');

  try {
    const [attStats, pqcScore] = await Promise.all([
      rawFetch('/api/v1/compliance/attestation-stats').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/compliance/pqc/score').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);

    if (_destroyed) return;

    _renderAttestation(attStats);
    _renderPqc(pqcScore);
  } catch (err) {
    if (_destroyed) return;
    console.error('[compliance] data fetch failed:', err);
    _setAllTilesError();
  }
}

// ── Tile renderers ───────────────────────────────────────────────────────────

function _renderAttestation(stats) {
  const body = document.getElementById('comp-attestation-body');
  if (!body) return;

  if (!stats) {
    setTileEmpty('comp-attestation', {
      icon: 'fa-solid fa-link',
      title: 'No attestation data',
      description: 'Attestation chain stats will appear here.',
    });
    return;
  }

  const total = stats.total || 0;
  const verified = stats.verified || 0;
  const merkle = stats.merkle_roots || 0;
  const coverage = total > 0 ? ((verified / total) * 100).toFixed(1) : '0.0';
  const coverageColor = parseFloat(coverage) >= 80 ? 'var(--accent)' : parseFloat(coverage) >= 50 ? 'var(--warn)' : 'var(--danger)';

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:16px;padding:16px">
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div class="kpi-card" style="flex:1;min-width:100px">
          <div class="kpi-label">Total Attestations</div>
          <div class="kpi-value">${fmtNum(total)}</div>
        </div>
        <div class="kpi-card" style="flex:1;min-width:100px">
          <div class="kpi-label">Verified</div>
          <div class="kpi-value" style="color:var(--accent)">${fmtNum(verified)}</div>
        </div>
        <div class="kpi-card" style="flex:1;min-width:100px">
          <div class="kpi-label">Merkle Roots</div>
          <div class="kpi-value">${fmtNum(merkle)}</div>
        </div>
      </div>
      <div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <span style="font-size:12px;color:var(--muted)">Verification Coverage</span>
          <span style="font-family:var(--mono);font-size:12px;font-weight:600;color:${coverageColor}">${coverage}%</span>
        </div>
        <div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden">
          <div style="width:${coverage}%;height:100%;background:${coverageColor};border-radius:3px;transition:width 0.5s ease"></div>
        </div>
      </div>
    </div>
  `;
}

function _renderPqc(pqc) {
  const body = document.getElementById('comp-pqc-body');
  if (!body) return;

  if (!pqc) {
    setTileEmpty('comp-pqc', {
      icon: 'fa-solid fa-atom',
      title: 'No PQC data',
      description: 'Post-quantum readiness assessment will appear here.',
    });
    return;
  }

  const rawScore = pqc.compliance_score ?? 0;
  const scoreNorm = rawScore > 1 ? rawScore / 100 : rawScore; // normalize: API may return 0-100 or 0-1
  const scorePct = Math.min(Math.round(scoreNorm * 100), 100);
  const algorithms = (pqc.pqc_algorithms || []).length;
  const endpoints = pqc.pqc_signed || 0;
  const scoreColor = scoreNorm >= 0.8 ? 'var(--accent)' : scoreNorm >= 0.5 ? 'var(--warn)' : 'var(--danger)';

  let statusBadge;
  if (scoreNorm >= 0.8) {
    statusBadge = '<span class="ds-badge-success" style="font-size:11px;padding:3px 10px">Compliant</span>';
  } else if (scoreNorm >= 0.5) {
    statusBadge = '<span class="ds-badge-warning" style="font-size:11px;padding:3px 10px">Partial</span>';
  } else {
    statusBadge = '<span class="ds-badge-danger" style="font-size:11px;padding:3px 10px">Non-Compliant</span>';
  }

  body.innerHTML = `
    <div style="display:flex;flex-direction:column;gap:16px;padding:16px">
      <div style="display:flex;align-items:center;justify-content:space-between">
        <span style="font-size:13px;font-weight:600;color:var(--text)">PQC Readiness</span>
        ${statusBadge}
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
          <div class="kpi-label">PQC-signed</div>
          <div class="kpi-value">${endpoints}</div>
        </div>
      </div>
      <div>
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <span style="font-size:12px;color:var(--muted)">Compliance Score</span>
          <span style="font-family:var(--mono);font-size:12px;color:${scoreColor}">${scorePct}%</span>
        </div>
        <div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden">
          <div style="width:${scorePct}%;height:100%;background:${scoreColor};border-radius:3px;transition:width 0.5s ease"></div>
        </div>
      </div>
    </div>
  `;
}

// ── Verify attestation modal ─────────────────────────────────────────────────

function _showVerifyModal() {
  openModal({
    title: 'Verify Attestation',
    maxWidth: '480px',
    renderBody: (modalBody) => {
      modalBody.innerHTML = `
        <div style="padding:8px 0">
          <label style="font-size:13px;color:var(--text);display:block;margin-bottom:8px">Decision ID</label>
          <input type="text" class="ds-input" id="verify-decision-id" placeholder="Enter decision ID\u2026" style="width:100%;margin-bottom:12px" />
          <button class="ds-btn ds-btn-primary" id="verify-submit" style="width:100%">Verify</button>
          <div id="verify-result" style="display:none;margin-top:12px;padding:12px;border-radius:6px;font-size:13px"></div>
        </div>
      `;

      modalBody.querySelector('#verify-submit').onclick = async () => {
        const input = modalBody.querySelector('#verify-decision-id');
        const resultEl = modalBody.querySelector('#verify-result');
        const decisionId = (input.value || '').trim();
        if (!decisionId) return;

        try {
          const resp = await rawFetch('/api/v1/compliance/verify-attestation', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ decision_id: decisionId }),
          });
          const data = await resp.json().catch(() => null);
          resultEl.style.display = 'block';
          if (data && data.valid) {
            resultEl.style.background = 'rgba(0,229,160,0.08)';
            resultEl.style.color = 'var(--accent)';
            resultEl.style.border = '1px solid rgba(0,229,160,0.2)';
            resultEl.textContent = 'Attestation verified successfully';
          } else {
            resultEl.style.background = 'rgba(255,77,77,0.08)';
            resultEl.style.color = 'var(--danger)';
            resultEl.style.border = '1px solid rgba(255,77,77,0.2)';
            resultEl.textContent = 'Attestation verification failed';
          }
        } catch (e) {
          resultEl.style.display = 'block';
          resultEl.style.background = 'rgba(255,77,77,0.08)';
          resultEl.style.color = 'var(--danger)';
          resultEl.style.border = '1px solid rgba(255,77,77,0.2)';
          resultEl.textContent = 'Verification error: ' + (e.message || 'Unknown error');
        }
      };
    },
  });
}

// ── Error helper ─────────────────────────────────────────────────────────────

function _setAllTilesError() {
  const ids = ['comp-attestation', 'comp-pqc'];
  ids.forEach(id => setTileError(id, 'Failed to load compliance data', () => loadData()));
}
