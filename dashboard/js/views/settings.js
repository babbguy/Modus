/**
 * Modus Dashboard v2 — Settings View
 * Organized settings with collapsible sections, LLM assistant config,
 * and clean visual hierarchy.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading } from '../tile.js';
import { rawFetch, esc, headers } from '../api.js';
import { get } from '../state.js';
import { setRefreshInterval } from '../auto-refresh.js';
import { LAYOUTS } from '../layouts/defaults.js';

let _grid = null;
let _destroyed = false;
let _routingSettings = null;
let _nomusStatus = null;

export async function render(container) {
  _destroyed = false;
  _grid = initGrid(container, 'settings');
  const tiles = [createTile({
    id: 'settings-form',
    title: 'Settings',
    icon: 'fa-solid fa-sliders',
    iconBg: 'rgba(159,193,49,0.1)',
    iconColor: 'var(--accent)',
  })];
  addTiles(_grid, tiles, loadLayout('settings', [
    { id: 'settings-form', x: 0, y: 0, w: 12, h: 14, minW: 12, minH: 8, noResize: true, noMove: true },
  ]));
  await _loadData();
}

export function destroy() {
  _destroyed = true;
  _routingSettings = null;
  _nomusStatus = null;
  _grid = null;
}

async function _loadData() {
  if (_destroyed) return;
  setTileLoading('settings-form', 'text');

  try {
    const [verResp, settingsResp, nomusResp] = await Promise.all([
      rawFetch('/version').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/admin/settings').then(r => r.ok ? r.json() : null).catch(() => null),
      rawFetch('/api/v1/admin/nomus/status').then(r => r.ok ? r.json() : null).catch(() => null),
    ]);
    if (_destroyed) return;

    if (settingsResp && Array.isArray(settingsResp)) {
      const sm = {};
      settingsResp.forEach(s => { sm[s.key] = s.value; });
      _routingSettings = {
        enabled: sm['routing.enabled'] !== 'false',
        cheap_model: sm['routing.cheap_model'] || 'claude-haiku-4-5-20251001',
        obs_threshold: parseInt(sm['routing.observe_threshold'] || '100', 10),
        misroute_rate: parseFloat(sm['routing.max_misroute_rate'] || '0.05'),
        cal_interval: parseInt(sm['routing.calibrator_interval_seconds'] || '300', 10),
        drift_interval: parseInt(sm['routing.drift_monitor_interval_seconds'] || '600', 10),
        decay_rate: parseFloat(sm['routing.confidence_decay_rate'] || '0.85'),
      };
    }
    _nomusStatus = nomusResp;
    _renderSettings(verResp);
  } catch (err) {
    if (_destroyed) return;
    _renderSettings(null);
  }
}

function _renderSettings(ver) {
  const body = document.getElementById('settings-form-body');
  if (!body) return;

  const apiBase = get('apiBase') || window.location.origin;
  const savedTheme = localStorage.getItem('modus_theme') || 'dark';
  const savedRefresh = localStorage.getItem('modus_refresh_interval') || '30';
  const savedView = localStorage.getItem('modus_default_view') || 'overview';
  const walkthroughDisabled = !!localStorage.getItem('modus_walkthrough_disabled');

  // About
  const version = ver?.version || 'unknown';
  const environment = ver?.environment || 'unknown';
  const authMode = ver?.auth_mode || 'unknown';

  // Routing
  const rt = _routingSettings || {};
  const rtEnabled = rt.enabled !== false;

  // Nomus
  const gs = _nomusStatus || {};
  const gsStatus = gs.status || 'not_configured';
  const gsConfigured = gs.configured || false;
  const gsUrl = gs.nomus_url || '';
  const gsVersion = gs.version || null;
  const gsLastSync = gs.last_sync ? new Date(gs.last_sync).toLocaleString() : null;
  const gsRegsCount = gs.regulations_count || 0;
  const gsAutoSync = gs.auto_sync !== false;
  let gsStatusColor = 'var(--muted)';
  let gsStatusLabel = 'Not Configured';
  if (gsStatus === 'connected') { gsStatusColor = 'var(--accent)'; gsStatusLabel = 'Connected'; }
  else if (gsStatus === 'error') { gsStatusColor = 'var(--danger)'; gsStatusLabel = 'Error'; }
  else if (gsStatus === 'disconnected') { gsStatusColor = '#f59e0b'; gsStatusLabel = 'Disconnected'; }

  // Saved LLM config
  const savedProvider = localStorage.getItem('modus_assistant_provider') || '';
  const savedModel = localStorage.getItem('modus_assistant_model') || '';
  const savedApiKey = localStorage.getItem('modus_assistant_api_key') || '';

  body.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;padding:20px;width:100%">

      <!-- ═══ ABOUT ═══ -->
      <section>
        <div class="settings-section-header">
          <i class="fa-solid fa-circle-info" style="color:var(--accent)"></i>
          <span>About this deployment</span>
        </div>
        <div class="settings-card">
          <div class="settings-row">
            <span class="settings-label">Version</span>
            <span class="settings-value" style="font-family:var(--mono)">${esc(version)}</span>
          </div>
          <div class="settings-row">
            <span class="settings-label">Environment</span>
            <span class="settings-value">${esc(environment)}</span>
          </div>
          <div class="settings-row">
            <span class="settings-label">Auth mode</span>
            <span class="settings-value">${esc(authMode)}</span>
          </div>
          <div class="settings-row">
            <span class="settings-label">License</span>
            <span class="settings-value">Apache-2.0</span>
          </div>
        </div>
      </section>

      <!-- ═══ AI ASSISTANT ═══ -->
      <section>
        <div class="settings-section-header">
          <i class="fa-solid fa-wand-magic-sparkles" style="color:#a855f7"></i>
          <span>AI Assistant</span>
          <span style="font-size:10px;font-weight:400;color:var(--muted);margin-left:auto">Your LLM, your infrastructure</span>
        </div>
        <div class="settings-card">
          <div class="settings-row">
            <label class="settings-label" for="settings-llm-provider">Provider</label>
            <select class="ds-select" id="settings-llm-provider" style="width:200px">
              <option value="" ${!savedProvider ? 'selected' : ''}>Not configured</option>
              <option value="anthropic" ${savedProvider === 'anthropic' ? 'selected' : ''}>Anthropic</option>
              <option value="openai" ${savedProvider === 'openai' ? 'selected' : ''}>OpenAI</option>
              <option value="google" ${savedProvider === 'google' ? 'selected' : ''}>Google (Gemini)</option>
              <option value="azure" ${savedProvider === 'azure' ? 'selected' : ''}>Azure OpenAI</option>
            </select>
          </div>
          <div class="settings-row">
            <label class="settings-label" for="settings-llm-model">Model</label>
            <input type="text" class="ds-input" id="settings-llm-model" value="${esc(savedModel)}" placeholder="e.g. claude-sonnet-4-6" style="width:200px" />
          </div>
          <div class="settings-row">
            <label class="settings-label" for="settings-llm-key">API Key</label>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="password" class="ds-input" id="settings-llm-key" value="${esc(savedApiKey)}" placeholder="sk-..." style="width:200px;font-family:var(--mono);font-size:11px" />
              <button class="ds-btn ds-btn-ghost ds-btn-sm" id="settings-llm-test" title="Test connection">Test</button>
            </div>
          </div>
          <div id="settings-llm-status" style="display:none;font-size:11px;padding:0 0 0 176px"></div>
        </div>
      </section>

      <!-- ═══ NOMUS REGULATORY ENGINE ═══ -->
      <section>
        <div class="settings-section-header">
          <i class="fa-solid fa-shield-halved" style="color:#10b981"></i>
          <span>Nomus Regulatory Engine</span>
          <span style="margin-left:auto;font-size:11px;font-weight:600;color:${gsStatusColor}">${gsStatusLabel}</span>
        </div>
        <div class="settings-card">
          <div class="settings-row">
            <label class="settings-label">Nomus URL</label>
            <div style="display:flex;gap:8px;align-items:center">
              <input type="text" class="ds-input" id="settings-nomus-url" value="${esc(gsUrl)}" placeholder="https://nomus.example.com" style="width:260px;font-family:var(--mono);font-size:11px" />
            </div>
          </div>
          <div class="settings-row">
            <span class="settings-label">Status</span>
            <span><span style="padding:4px 12px;border-radius:6px;font-size:11px;font-weight:700;background:${gsStatusColor}20;color:${gsStatusColor};border:1px solid ${gsStatusColor}40">${gsStatusLabel}</span></span>
          </div>
          ${gsVersion ? '<div class="settings-row"><span class="settings-label">Ruleset Version</span><span class="settings-value" style="font-family:var(--mono);font-size:11px">' + esc(gsVersion) + '</span></div>' : ''}
          ${gsLastSync ? '<div class="settings-row"><span class="settings-label">Last Sync</span><span class="settings-value" style="font-family:var(--mono);font-size:11px">' + gsLastSync + '</span></div>' : ''}
          <div class="settings-row">
            <span class="settings-label">Regulations</span>
            <span class="settings-value" style="font-family:var(--mono)">${gsRegsCount} loaded</span>
          </div>
          <div class="settings-row">
            <label class="settings-label">Auto-Sync</label>
            <input type="checkbox" id="settings-nomus-autosync" ${gsAutoSync ? 'checked' : ''} style="width:16px;height:16px;cursor:pointer;accent-color:var(--accent)" />
          </div>
          <div class="settings-row" style="border-bottom:none">
            <span class="settings-label">Actions</span>
            <div style="display:flex;gap:8px">
              <button class="ds-btn ds-btn-ghost ds-btn-sm" id="settings-nomus-test" title="Test connectivity">
                <i class="fa-solid fa-plug" style="font-size:10px;margin-right:4px"></i>Test Connection
              </button>
              <button class="ds-btn ds-btn-ghost ds-btn-sm" id="settings-nomus-sync" title="Sync ruleset now">
                <i class="fa-solid fa-arrows-rotate" style="font-size:10px;margin-right:4px"></i>Sync Now
              </button>
            </div>
          </div>
          <div id="settings-nomus-status-msg" style="display:none;font-size:11px;padding:8px 0 0 176px"></div>
        </div>
      </section>

      <!-- ═══ DASHBOARD PREFERENCES ═══ -->
      <section>
        <div class="settings-section-header">
          <i class="fa-solid fa-palette" style="color:#a855f7"></i>
          <span>Dashboard Preferences</span>
        </div>
        <div class="settings-card">
          <div class="settings-row">
            <label class="settings-label">Theme</label>
            <select class="ds-select" id="settings-theme" style="width:200px">
              <option value="dark" ${savedTheme === 'dark' ? 'selected' : ''}>Dark</option>
              <option value="light" ${savedTheme === 'light' ? 'selected' : ''}>Light</option>
            </select>
          </div>
          <div class="settings-row">
            <label class="settings-label">Auto-refresh</label>
            <select class="ds-select" id="settings-refresh" style="width:200px">
              <option value="10" ${savedRefresh === '10' ? 'selected' : ''}>10 seconds</option>
              <option value="30" ${savedRefresh === '30' ? 'selected' : ''}>30 seconds</option>
              <option value="60" ${savedRefresh === '60' ? 'selected' : ''}>1 minute</option>
              <option value="300" ${savedRefresh === '300' ? 'selected' : ''}>5 minutes</option>
              <option value="0" ${savedRefresh === '0' ? 'selected' : ''}>Off</option>
            </select>
          </div>
          <div class="settings-row">
            <label class="settings-label">Default View</label>
            <select class="ds-select" id="settings-default-view" style="width:200px">
              <option value="overview" ${savedView === 'overview' ? 'selected' : ''}>Overview</option>
              <option value="executive" ${savedView === 'executive' ? 'selected' : ''}>Executive</option>
              <option value="finance" ${savedView === 'finance' ? 'selected' : ''}>Finance</option>
              <option value="devops" ${savedView === 'devops' ? 'selected' : ''}>DevOps</option>
            </select>
          </div>
          <div class="settings-row">
            <label class="settings-label">Disable Walkthrough</label>
            <input type="checkbox" id="settings-walkthrough" ${walkthroughDisabled ? 'checked' : ''} style="width:16px;height:16px;cursor:pointer;accent-color:var(--accent)" />
          </div>
        </div>
      </section>

      <!-- ═══ API CONFIGURATION ═══ -->
      <section>
        <div class="settings-section-header">
          <i class="fa-solid fa-plug" style="color:#3b82f6"></i>
          <span>API Configuration</span>
        </div>
        <div class="settings-card">
          <div class="settings-row">
            <span class="settings-label">API URL</span>
            <span class="settings-value" style="font-family:var(--mono);font-size:11px">${esc(apiBase)}</span>
          </div>
          <div class="settings-row">
            <span class="settings-label">Data Retention</span>
            <span class="settings-value">Customer-managed</span>
          </div>
        </div>
      </section>

      <!-- ═══ ROUTING CONFIGURATION ═══ -->
      <section>
        <div class="settings-section-header">
          <i class="fa-solid fa-route" style="color:#f59e0b"></i>
          <span>Routing Engine</span>
          <span style="margin-left:auto;font-size:11px;font-weight:600;color:${rtEnabled ? 'var(--accent)' : 'var(--muted)'}">${rtEnabled ? 'Enabled' : 'Disabled'}</span>
        </div>
        <div class="settings-card">
          <div class="settings-row">
            <span class="settings-label">Cheap Model</span>
            <span class="settings-value" style="font-family:var(--mono);font-size:11px">${esc(rt.cheap_model || '\u2014')}</span>
          </div>
          <div class="settings-row">
            <span class="settings-label">Observe Threshold</span>
            <span class="settings-value" style="font-family:var(--mono)">${rt.obs_threshold || '\u2014'} calls</span>
          </div>
          <div class="settings-row">
            <span class="settings-label">Max Misroute Rate</span>
            <span class="settings-value" style="font-family:var(--mono)">${rt.misroute_rate != null ? (rt.misroute_rate * 100).toFixed(0) + '%' : '\u2014'}</span>
          </div>
          <div class="settings-row">
            <span class="settings-label">Calibrator Interval</span>
            <span class="settings-value" style="font-family:var(--mono)">${rt.cal_interval || '\u2014'}s</span>
          </div>
          <div class="settings-row">
            <span class="settings-label">Drift Monitor</span>
            <span class="settings-value" style="font-family:var(--mono)">${rt.drift_interval || '\u2014'}s</span>
          </div>
          <div class="settings-row" style="border-bottom:none">
            <span class="settings-label">Confidence Decay</span>
            <span class="settings-value" style="font-family:var(--mono)">${rt.decay_rate || '\u2014'}</span>
          </div>
        </div>
      </section>

      <!-- ═══ DIAGNOSTICS ═══ -->
      <section>
        <div class="settings-section-header">
          <i class="fa-solid fa-stethoscope" style="color:#ef4444"></i>
          <span>System Diagnostics</span>
        </div>
        <div class="settings-card">
          <div class="settings-row">
            <span class="settings-label">Health Check</span>
            <div style="display:flex;gap:8px;align-items:center">
              <button class="ds-btn ds-btn-ghost ds-btn-sm" id="settings-diag-scan">
                <i class="fa-solid fa-stethoscope" style="font-size:10px;margin-right:4px"></i>Run Diagnostic Scan
              </button>
              <button class="ds-btn ds-btn-ghost ds-btn-sm" id="settings-diag-download" style="display:none">
                <i class="fa-solid fa-download" style="font-size:10px;margin-right:4px"></i>Download Report
              </button>
            </div>
          </div>
          <div id="settings-diag-status" style="display:none;font-size:11px;padding:8px 16px"></div>
          <div id="settings-diag-summary" style="display:none;padding:12px 16px"></div>
        </div>
      </section>

      <!-- ═══ SAVE ═══ -->
      <div style="display:flex;gap:12px;justify-content:flex-end;align-items:center">
        <span id="settings-save-msg" style="display:none;font-size:12px;color:var(--accent)"></span>
        <button class="ds-btn ds-btn-primary" id="settings-save-btn" style="padding:8px 24px">
          <i class="fa-solid fa-check" style="font-size:11px;margin-right:4px"></i> Save Preferences
        </button>
      </div>

    </div>
  `;

  _attachHandlers();
}

function _attachHandlers() {
  const body = document.getElementById('settings-form-body');
  if (!body) return;

  // Theme instant preview
  const themeSelect = body.querySelector('#settings-theme');
  if (themeSelect) {
    themeSelect.addEventListener('change', () => {
      document.documentElement.setAttribute('data-theme', themeSelect.value);
      localStorage.setItem('modus_theme', themeSelect.value);
    });
  }

  // Walkthrough toggle
  const wtCheck = body.querySelector('#settings-walkthrough');
  if (wtCheck) {
    wtCheck.addEventListener('change', () => {
      if (wtCheck.checked) localStorage.setItem('modus_walkthrough_disabled', '1');
      else localStorage.removeItem('modus_walkthrough_disabled');
    });
  }

  // LLM test button
  const testBtn = body.querySelector('#settings-llm-test');
  if (testBtn) {
    testBtn.addEventListener('click', _testLlmConnection);
  }

  // Nomus test button
  const nomusTestBtn = body.querySelector('#settings-nomus-test');
  if (nomusTestBtn) {
    nomusTestBtn.addEventListener('click', _testNomusConnection);
  }

  // Nomus sync button
  const nomusSyncBtn = body.querySelector('#settings-nomus-sync');
  if (nomusSyncBtn) {
    nomusSyncBtn.addEventListener('click', _syncNomusRuleset);
  }

  // Diagnostics scan button
  const diagScanBtn = body.querySelector('#settings-diag-scan');
  if (diagScanBtn) {
    diagScanBtn.addEventListener('click', _runDiagnosticScan);
  }

  // Diagnostics download button
  const diagDownloadBtn = body.querySelector('#settings-diag-download');
  if (diagDownloadBtn) {
    diagDownloadBtn.addEventListener('click', _downloadDiagnosticReport);
  }

  // Save
  const saveBtn = body.querySelector('#settings-save-btn');
  if (saveBtn) {
    saveBtn.addEventListener('click', _savePreferences);
  }
}

async function _testLlmConnection() {
  const provider = document.getElementById('settings-llm-provider')?.value;
  const apiKey = document.getElementById('settings-llm-key')?.value;
  const statusEl = document.getElementById('settings-llm-status');
  if (!statusEl) return;

  if (!provider || !apiKey) {
    statusEl.style.display = 'block';
    statusEl.innerHTML = '<span style="color:var(--warn)">Select a provider and enter an API key first.</span>';
    return;
  }

  statusEl.style.display = 'block';
  statusEl.innerHTML = '<span style="color:var(--muted)">Testing connection...</span>';

  try {
    const resp = await fetch('/api/v1/assistant/chat', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ message: 'ping', context_view: 'settings' }),
    });
    const data = await resp.json();
    if (data.response) {
      statusEl.innerHTML = '<span style="color:var(--accent)">Connection successful.</span>';
    } else {
      statusEl.innerHTML = `<span style="color:var(--danger)">${esc(data.error || 'Connection failed')}</span>`;
    }
  } catch (e) {
    statusEl.innerHTML = '<span style="color:var(--danger)">Connection failed. Check your API key and provider.</span>';
  }
}

async function _testNomusConnection() {
  const statusEl = document.getElementById('settings-nomus-status-msg');
  if (!statusEl) return;

  statusEl.style.display = 'block';
  statusEl.innerHTML = '<span style="color:var(--muted)">Testing connection...</span>';

  try {
    const resp = await rawFetch('/api/v1/admin/nomus/test', { method: 'POST' });
    const data = await resp.json();
    if (data.reachable) {
      statusEl.innerHTML = `<span style="color:var(--accent)">Connected to Nomus${data.nomus_version ? ' v' + esc(data.nomus_version) : ''}.</span>`;
    } else {
      statusEl.innerHTML = `<span style="color:var(--danger)">${esc(data.error || 'Connection failed')}</span>`;
    }
  } catch (e) {
    statusEl.innerHTML = '<span style="color:var(--danger)">Connection test failed.</span>';
  }
}

async function _syncNomusRuleset() {
  const statusEl = document.getElementById('settings-nomus-status-msg');
  if (!statusEl) return;

  statusEl.style.display = 'block';
  statusEl.innerHTML = '<span style="color:var(--muted)">Syncing ruleset...</span>';

  try {
    const resp = await rawFetch('/api/v1/admin/nomus/sync', { method: 'POST' });
    const data = await resp.json();
    if (data.success) {
      statusEl.innerHTML = `<span style="color:var(--accent)">Synced v${esc(data.version || '?')} \u2014 ${data.regulations_count} regulations, ${data.rules} rules.</span>`;
      // Refresh the view after a short delay
      setTimeout(() => { if (!_destroyed) _loadData(); }, 1500);
    } else {
      statusEl.innerHTML = `<span style="color:var(--danger)">${esc(data.error || 'Sync failed')}</span>`;
    }
  } catch (e) {
    statusEl.innerHTML = '<span style="color:var(--danger)">Sync request failed.</span>';
  }
}

async function _runDiagnosticScan() {
  const statusEl = document.getElementById('settings-diag-status');
  const summaryEl = document.getElementById('settings-diag-summary');
  const downloadBtn = document.getElementById('settings-diag-download');
  const scanBtn = document.getElementById('settings-diag-scan');
  if (!statusEl) return;

  // Show scanning state
  statusEl.style.display = 'block';
  statusEl.innerHTML = '<span style="color:var(--muted)"><i class="fa-solid fa-spinner fa-spin" style="margin-right:4px"></i>Scanning...</span>';
  if (summaryEl) summaryEl.style.display = 'none';
  if (downloadBtn) downloadBtn.style.display = 'none';
  if (scanBtn) scanBtn.disabled = true;

  try {
    const resp = await rawFetch('/api/v1/admin/diagnostics/scan', { method: 'POST' });
    const data = await resp.json();

    if (!resp.ok) {
      statusEl.innerHTML = `<span style="color:var(--danger)">Scan failed: ${esc(data.detail || 'Unknown error')}</span>`;
      if (scanBtn) scanBtn.disabled = false;
      return;
    }

    // Show success
    statusEl.innerHTML = `<span style="color:var(--accent)">Scan complete in ${data.scan_duration_ms}ms</span>`;

    // Render summary grid
    if (summaryEl && data.summary) {
      const s = data.summary;
      const rc = s.recommendation_counts || {};
      const dot = (status) => {
        const colors = { ok: 'var(--accent)', warning: '#f59e0b', error: 'var(--danger)', unknown: 'var(--muted)' };
        const c = colors[status] || colors.unknown;
        return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${c};margin-right:6px"></span>`;
      };
      const label = (status) => {
        return status === 'ok' ? 'OK' : status.charAt(0).toUpperCase() + status.slice(1);
      };

      summaryEl.style.display = 'block';
      summaryEl.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:12px">
          <div style="font-size:12px">${dot(s.system)}<strong>System:</strong> ${label(s.system)}</div>
          <div style="font-size:12px">${dot(s.database)}<strong>Database:</strong> ${label(s.database)}</div>
          <div style="font-size:12px">${dot(s.background_tasks)}<strong>Tasks:</strong> ${label(s.background_tasks)}</div>
          <div style="font-size:12px">${dot(s.connections)}<strong>Connections:</strong> ${label(s.connections)}</div>
        </div>
        ${(data.recommendations && data.recommendations.length > 0) ? `
          <div style="font-size:11px;color:var(--muted);margin-top:4px">
            ${rc.error ? `<span style="color:var(--danger);margin-right:12px">${rc.error} error${rc.error > 1 ? 's' : ''}</span>` : ''}
            ${rc.warning ? `<span style="color:#f59e0b;margin-right:12px">${rc.warning} warning${rc.warning > 1 ? 's' : ''}</span>` : ''}
            ${rc.info ? `<span style="color:var(--muted)">${rc.info} info</span>` : ''}
          </div>
        ` : ''}
      `;
    }

    // Show download button
    if (downloadBtn) downloadBtn.style.display = '';
  } catch (e) {
    statusEl.innerHTML = '<span style="color:var(--danger)">Diagnostic scan failed. Check console for details.</span>';
  }

  if (scanBtn) scanBtn.disabled = false;
}

async function _downloadDiagnosticReport() {
  try {
    const resp = await rawFetch('/api/v1/admin/diagnostics/download');
    if (!resp.ok) {
      const statusEl = document.getElementById('settings-diag-status');
      if (statusEl) {
        statusEl.style.display = 'block';
        statusEl.innerHTML = '<span style="color:var(--danger)">No report available. Run a scan first.</span>';
      }
      return;
    }

    const blob = await resp.blob();
    const disposition = resp.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="?([^"]+)"?/);
    const filename = match ? match[1] : 'modus-diagnostics.json';

    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  } catch (e) {
    const statusEl = document.getElementById('settings-diag-status');
    if (statusEl) {
      statusEl.style.display = 'block';
      statusEl.innerHTML = '<span style="color:var(--danger)">Download failed.</span>';
    }
  }
}

function _savePreferences() {
  const theme = document.getElementById('settings-theme')?.value || 'dark';
  const refresh = document.getElementById('settings-refresh')?.value || '30';
  const defaultView = document.getElementById('settings-default-view')?.value || 'overview';
  const provider = document.getElementById('settings-llm-provider')?.value || '';
  const model = document.getElementById('settings-llm-model')?.value || '';
  const apiKey = document.getElementById('settings-llm-key')?.value || '';

  localStorage.setItem('modus_theme', theme);
  setRefreshInterval(refresh); // persists + restarts the auto-refresh timer
  localStorage.setItem('modus_default_view', defaultView);
  localStorage.setItem('modus_assistant_provider', provider);
  localStorage.setItem('modus_assistant_model', model);
  if (apiKey) localStorage.setItem('modus_assistant_api_key', apiKey);

  document.documentElement.setAttribute('data-theme', theme);

  const msg = document.getElementById('settings-save-msg');
  if (msg) {
    msg.textContent = 'Preferences saved.';
    msg.style.display = '';
    setTimeout(() => { if (msg) msg.style.display = 'none'; }, 2500);
  }
}
