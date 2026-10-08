/**
 * Modus Dashboard v2 — First-Start Policy Wizard
 * Full-screen overlay that guides new deployments through initial setup.
 *
 * Exports: showWizard(), hideWizard()
 */

import { navigate } from '../router.js';
import { headers, authHeaders } from '../api.js';

// ── State ────────────────────────────────────────────────────────────────────

let _overlay = null;
let _step = 0;
const _totalSteps = 4;

const _state = {
  orgName: '',
  teamName: '',
  teamSlug: '',
  policyMode: 'managed',
  budgetDaily: 200,
  budgetMonthly: 5000,
  uploadedPolicies: [],
};

// ── Public API ───────────────────────────────────────────────────────────────

export function showWizard() {
  if (_overlay) return;
  _step = 0;
  _overlay = document.createElement('div');
  _overlay.className = 'wizard-overlay';
  _overlay.innerHTML = '<div class="wizard-card" id="wizard-card"></div>';
  document.body.appendChild(_overlay);
  // fade in
  requestAnimationFrame(() => _overlay.style.opacity = '1');
  _render();
}

export function hideWizard() {
  if (!_overlay) return;
  _overlay.style.opacity = '0';
  setTimeout(() => {
    _overlay?.remove();
    _overlay = null;
  }, 200);
}

// ── Render dispatcher ────────────────────────────────────────────────────────

function _render() {
  const card = document.getElementById('wizard-card');
  if (!card) return;
  const steps = [_renderWelcome, _renderOrganization, _renderPolicyMode, _renderReview];
  card.innerHTML = _stepDots() + steps[_step]();
  _bind();
}

// ── Step dots + progress bar ─────────────────────────────────────────────────

function _stepDots() {
  const pct = ((_step) / (_totalSteps - 1)) * 100;
  let dots = '<div class="wizard-steps">';
  for (let i = 0; i < _totalSteps; i++) {
    const cls = i === _step ? 'active' : i < _step ? 'completed' : '';
    dots += `<div class="wizard-step-dot ${cls}"></div>`;
  }
  dots += '</div>';
  dots += `<div class="wizard-progress"><div class="wizard-progress-bar" style="width:${pct}%"></div></div>`;
  return dots;
}

// ── Step 1: Welcome ──────────────────────────────────────────────────────────

function _renderWelcome() {
  return `
    <div class="wizard-header">
      <div class="wizard-logo">
        <svg width="48" height="48" viewBox="0 0 48 48" fill="none">
          <polygon points="24,4 44,14 44,34 24,44 4,34 4,14" fill="none" stroke="var(--accent)" stroke-width="2.5"/>
          <polygon points="24,12 36,18 36,30 24,36 12,30 12,18" fill="var(--accent)" opacity="0.15"/>
          <circle cx="24" cy="24" r="4" fill="var(--accent)"/>
        </svg>
      </div>
      <h2 class="wizard-title">Welcome to Modus</h2>
      <p class="wizard-subtitle">Let's set up your AI cost governance in under 2 minutes.</p>
    </div>
    <div class="wizard-footer" style="justify-content:center;">
      <button class="wizard-btn wizard-btn-primary" id="wiz-next">Get Started</button>
    </div>`;
}

// ── Step 2: Organization ─────────────────────────────────────────────────────

function _renderOrganization() {
  return `
    <div class="wizard-header">
      <h2 class="wizard-title">Your Organization</h2>
      <p class="wizard-subtitle">Tell us about your org and first team.</p>
    </div>
    <div class="wizard-body">
      <label class="wizard-label">Organization Name</label>
      <input class="wizard-input" id="wiz-org" type="text" placeholder="Acme Corp"
             value="${_esc(_state.orgName)}" maxlength="256" autofocus />

      <label class="wizard-label" style="margin-top:16px;">First Team Name</label>
      <input class="wizard-input" id="wiz-team" type="text" placeholder="Engineering"
             value="${_esc(_state.teamName)}" maxlength="256" />

      <label class="wizard-label" style="margin-top:8px;">Team Slug</label>
      <input class="wizard-input wizard-input-mono" id="wiz-slug" type="text"
             placeholder="engineering" value="${_esc(_state.teamSlug)}" maxlength="64"
             pattern="^[a-z0-9][a-z0-9\\-_]*$" />
      <div class="wizard-hint">Lowercase letters, numbers, hyphens only. Auto-generated from team name.</div>
    </div>
    <div class="wizard-footer">
      <button class="wizard-btn wizard-btn-ghost" id="wiz-back">Back</button>
      <button class="wizard-btn wizard-btn-primary" id="wiz-next" disabled>Continue</button>
    </div>`;
}

// ── Step 3: Policy Mode ──────────────────────────────────────────────────────

function _renderPolicyMode() {
  const m = _state.policyMode;
  return `
    <div class="wizard-header">
      <h2 class="wizard-title">Policy Configuration</h2>
      <p class="wizard-subtitle">How do you want to manage governance policies?</p>
    </div>
    <div class="wizard-body">
      <div class="wizard-options">
        <div class="wizard-option-card ${m === 'managed' ? 'selected' : ''}" data-mode="managed">
          <div class="wizard-option-icon"><i class="fa-solid fa-wand-magic-sparkles"></i></div>
          <div class="wizard-option-text">
            <div class="wizard-option-title">Let Modus manage <span class="wizard-rec-badge">Recommended</span></div>
            <div class="wizard-option-desc">Modus will automatically create sensible policies based on your usage patterns. You can review and adjust them anytime.</div>
          </div>
        </div>
        <div class="wizard-option-card ${m === 'manual' ? 'selected' : ''}" data-mode="manual">
          <div class="wizard-option-icon"><i class="fa-solid fa-sliders"></i></div>
          <div class="wizard-option-text">
            <div class="wizard-option-title">I'll set my own</div>
            <div class="wizard-option-desc">Configure policies manually or upload a YAML file.</div>
          </div>
        </div>
      </div>

      ${m === 'managed' ? `
        <div class="wizard-managed-summary" style="margin-top:16px;">
          <div class="wizard-hint" style="margin-bottom:8px;">Default policies that will be created:</div>
          <div class="wizard-policy-list">
            <div class="wizard-policy-item"><i class="fa-solid fa-shield-halved"></i> Daily budget cap — $${_state.budgetDaily}</div>
            <div class="wizard-policy-item"><i class="fa-solid fa-shield-halved"></i> Monthly budget cap — $${_state.budgetMonthly}</div>
            <div class="wizard-policy-item"><i class="fa-solid fa-gauge-high"></i> Rate limit — 1,000 calls/hr</div>
          </div>
        </div>
      ` : `
        <div class="wizard-manual-config" style="margin-top:16px;">
          <div class="wizard-input-row">
            <div class="wizard-input-group">
              <label class="wizard-label">Daily Budget ($)</label>
              <input class="wizard-input" id="wiz-budget-daily" type="number" min="1"
                     value="${_state.budgetDaily}" />
            </div>
            <div class="wizard-input-group">
              <label class="wizard-label">Monthly Budget ($)</label>
              <input class="wizard-input" id="wiz-budget-monthly" type="number" min="1"
                     value="${_state.budgetMonthly}" />
            </div>
          </div>
          <div style="margin-top:12px;">
            <label class="wizard-btn wizard-btn-outline wizard-upload-btn" id="wiz-upload-label">
              <i class="fa-solid fa-file-arrow-up"></i> Upload YAML Policy File
              <input type="file" id="wiz-upload" accept=".yaml,.yml" style="display:none;" />
            </label>
            ${_state.uploadedPolicies.length ? `
              <div class="wizard-hint" style="margin-top:8px;">${_state.uploadedPolicies.length} policies loaded from file.</div>
            ` : ''}
          </div>
        </div>
      `}
    </div>
    <div class="wizard-footer">
      <button class="wizard-btn wizard-btn-ghost" id="wiz-back">Back</button>
      <button class="wizard-btn wizard-btn-primary" id="wiz-next">Continue</button>
    </div>`;
}

// ── Step 4: Review & Launch ──────────────────────────────────────────────────

function _renderReview() {
  const policyLines = _state.policyMode === 'managed'
    ? [
        `Daily Budget Cap — $${_state.budgetDaily}`,
        `Monthly Budget Cap — $${_state.budgetMonthly}`,
        'Rate Limit — 1,000 calls/hr',
      ]
    : _state.uploadedPolicies.length
      ? _state.uploadedPolicies.map(p => `${p.name} (${p.type})`)
      : [`Daily Budget Cap — $${_state.budgetDaily}`, `Monthly Budget Cap — $${_state.budgetMonthly}`];

  return `
    <div class="wizard-header">
      <h2 class="wizard-title">Review & Launch</h2>
      <p class="wizard-subtitle">Here's what we'll set up for you.</p>
    </div>
    <div class="wizard-body">
      <div class="wizard-review-section">
        <div class="wizard-review-label">Organization</div>
        <div class="wizard-review-value">${_esc(_state.orgName)}</div>
      </div>
      <div class="wizard-review-section">
        <div class="wizard-review-label">Team</div>
        <div class="wizard-review-value">${_esc(_state.teamName)} <span class="wizard-slug-badge">${_esc(_state.teamSlug)}</span></div>
      </div>
      <div class="wizard-review-section">
        <div class="wizard-review-label">Mode</div>
        <div class="wizard-review-value">${_state.policyMode === 'managed' ? 'Modus Managed' : 'Manual'}</div>
      </div>
      <div class="wizard-review-section">
        <div class="wizard-review-label">Policies</div>
        <div class="wizard-review-value">
          ${policyLines.map(l => `<div class="wizard-policy-item-sm">${_esc(l)}</div>`).join('')}
        </div>
      </div>
      <div id="wiz-error" class="wizard-error" style="display:none;"></div>
    </div>
    <div class="wizard-footer">
      <button class="wizard-btn wizard-btn-ghost" id="wiz-back">Back</button>
      <button class="wizard-btn wizard-btn-launch" id="wiz-launch">
        <i class="fa-solid fa-rocket"></i> Launch Modus
      </button>
    </div>`;
}

// ── Event binding ────────────────────────────────────────────────────────────

function _bind() {
  const card = document.getElementById('wizard-card');
  if (!card) return;

  // Next / Get Started
  const nextBtn = card.querySelector('#wiz-next');
  if (nextBtn) nextBtn.addEventListener('click', _onNext);

  // Back
  const backBtn = card.querySelector('#wiz-back');
  if (backBtn) backBtn.addEventListener('click', () => { _step--; _render(); });

  // Launch
  const launchBtn = card.querySelector('#wiz-launch');
  if (launchBtn) launchBtn.addEventListener('click', _onLaunch);

  // Step 2: org/team inputs
  if (_step === 1) {
    const orgInput = card.querySelector('#wiz-org');
    const teamInput = card.querySelector('#wiz-team');
    const slugInput = card.querySelector('#wiz-slug');
    const validate = () => {
      _state.orgName = orgInput?.value?.trim() || '';
      _state.teamName = teamInput?.value?.trim() || '';
      _state.teamSlug = slugInput?.value?.trim() || '';
      const valid = _state.orgName.length > 0 && _state.teamName.length > 0 && /^[a-z0-9][a-z0-9\-_]*$/.test(_state.teamSlug);
      if (nextBtn) nextBtn.disabled = !valid;
    };
    orgInput?.addEventListener('input', validate);
    teamInput?.addEventListener('input', () => {
      _state.teamName = teamInput.value.trim();
      // auto-slug if user hasn't manually edited
      const auto = _state.teamName.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      if (slugInput) slugInput.value = auto;
      _state.teamSlug = auto;
      validate();
    });
    slugInput?.addEventListener('input', validate);
    validate();
  }

  // Step 3: policy mode cards
  if (_step === 2) {
    card.querySelectorAll('.wizard-option-card').forEach(c => {
      c.addEventListener('click', () => {
        _state.policyMode = c.dataset.mode;
        _render();
      });
    });
    // Budget inputs (manual mode)
    const dInput = card.querySelector('#wiz-budget-daily');
    const mInput = card.querySelector('#wiz-budget-monthly');
    if (dInput) dInput.addEventListener('input', () => { _state.budgetDaily = parseFloat(dInput.value) || 200; });
    if (mInput) mInput.addEventListener('input', () => { _state.budgetMonthly = parseFloat(mInput.value) || 5000; });
    // File upload
    const fileInput = card.querySelector('#wiz-upload');
    if (fileInput) fileInput.addEventListener('change', _onUpload);
  }
}

// ── Handlers ─────────────────────────────────────────────────────────────────

function _onNext() {
  if (_step < _totalSteps - 1) {
    _step++;
    _render();
  }
}

async function _onUpload(e) {
  const file = e.target.files?.[0];
  if (!file) return;

  const form = new FormData();
  form.append('file', file);

  try {
    const resp = await fetch('/api/v1/onboarding/upload', {
      method: 'POST',
      headers: authHeaders(), // no Content-Type: the browser sets the multipart boundary
      body: form,
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert(err.detail || 'Failed to parse YAML file.');
      return;
    }
    const data = await resp.json();
    _state.uploadedPolicies = data.policies || [];
    _render();
  } catch (err) {
    alert('Upload failed: ' + err.message);
  }
}

async function _onLaunch() {
  const launchBtn = document.getElementById('wiz-launch');
  const errorEl = document.getElementById('wiz-error');
  if (launchBtn) {
    launchBtn.disabled = true;
    launchBtn.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Setting up...';
  }
  if (errorEl) errorEl.style.display = 'none';

  // Build policy list for manual mode
  let policies = [];
  if (_state.policyMode === 'manual' && _state.uploadedPolicies.length) {
    policies = _state.uploadedPolicies;
  }

  const payload = {
    org_name: _state.orgName,
    first_team: { name: _state.teamName, slug: _state.teamSlug },
    budget_daily: _state.budgetDaily,
    budget_monthly: _state.budgetMonthly,
    policy_mode: _state.policyMode,
    policies,
  };

  try {
    // Apply config
    const applyResp = await fetch('/api/v1/onboarding/apply', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify(payload),
    });
    if (!applyResp.ok) {
      const err = await applyResp.json().catch(() => ({}));
      throw new Error(err.detail || `Setup failed (${applyResp.status})`);
    }

    // Mark complete
    await fetch('/api/v1/onboarding/complete', {
      method: 'POST',
      headers: headers(),
    });

    // Done — hide wizard, go to overview
    hideWizard();
    navigate('overview');
  } catch (err) {
    if (errorEl) {
      errorEl.textContent = err.message;
      errorEl.style.display = 'block';
    }
    if (launchBtn) {
      launchBtn.disabled = false;
      launchBtn.innerHTML = '<i class="fa-solid fa-rocket"></i> Launch Modus';
    }
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function _esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
