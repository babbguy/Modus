/**
 * Modus Dashboard v2 — Notifications View
 * Two tiles: Channel Configuration (form-based) and App Subscriptions (table).
 * Supports save, test, and per-app subscription toggles.
 */

import { initGrid, addTiles, loadLayout } from '../grid.js';
import { createTile, setTileLoading, setTileEmpty, setTileError } from '../tile.js';
import { rawFetch, apiFetch, esc } from '../api.js';
import { get } from '../state.js';
import { openModal, closeModal } from '../modal.js';
import { LAYOUTS } from '../layouts/defaults.js';
import { toast } from '../toast.js';

// ── Module state ─────────────────────────────────────────────────────────────

let _grid = null;
let _container = null;
let _destroyed = false;
let _configCache = {};
let _appsCache = [];

// ── Public API ───────────────────────────────────────────────────────────────

export async function render(container) {
  _destroyed = false;
  _container = container;

  _grid = initGrid(container, 'notifications');

  const tiles = [_createConfigTile(), _createSubsTile(), _createDeliveryTile()];
  const layout = loadLayout('notifications', LAYOUTS.notifications);
  addTiles(_grid, tiles, layout);

  await _loadData();
}

export function destroy() {
  _destroyed = true;
  _configCache = {};
  _appsCache = [];
  _grid = null;
  _container = null;
}

// ── Tile factories ───────────────────────────────────────────────────────────

function _createConfigTile() {
  return createTile({
    id: 'notif-config',
    title: 'Notification Channels',
    icon: 'fa-solid fa-tower-broadcast',
    iconBg: 'rgba(59,130,246,0.1)',
    iconColor: '#3b82f6',
  });
}

function _createSubsTile() {
  return createTile({
    id: 'notif-subs',
    title: 'App Subscriptions',
    icon: 'fa-solid fa-bell',
    iconBg: 'rgba(245,158,11,0.1)',
    iconColor: '#f59e0b',
    filterable: true,
    filterPlaceholder: 'Filter apps\u2026',
    onFilter: (q) => _filterSubs(q),
  });
}

function _createDeliveryTile() {
  return createTile({
    id: 'notif-delivery',
    title: 'Delivery Health',
    icon: 'fa-solid fa-heart-pulse',
    iconBg: 'rgba(16,185,129,0.1)',
    iconColor: '#10b981',
  });
}

// ── Data fetching ────────────────────────────────────────────────────────────

async function _loadData() {
  if (_destroyed) return;
  setTileLoading('notif-config', 'text');
  setTileLoading('notif-subs', 'table');
  setTileLoading('notif-delivery', 'cards');

  const [configResp, appsResp, alertsData] = await Promise.all([
    rawFetch('/api/v1/notifications/config').catch(() => null),
    rawFetch('/api/v1/apps').catch(() => null),
    apiFetch('recent-alerts', { limit: 100 }).catch(() => null),
  ]);

  if (_destroyed) return;

  try {
    _configCache = configResp && configResp.ok ? await configResp.json() : {};
  } catch (_) { _configCache = {}; }

  try {
    _appsCache = appsResp && appsResp.ok ? await appsResp.json() : [];
  } catch (_) { _appsCache = []; }

  _renderConfig(_configCache);
  _renderSubs(_appsCache);
  _renderDelivery(alertsData);
}

// ── Rendering: Config ────────────────────────────────────────────────────────

function _renderConfig(cfg) {
  const body = document.getElementById('notif-config-body');
  if (!body) return;

  const s = cfg.slack || {};
  const t = cfg.teams || {};
  const e = cfg.email || {};
  const p = cfg.pagerduty || {};
  const w = cfg.webhook || {};

  body.innerHTML = `
    <div style="padding:12px;display:flex;flex-direction:column;gap:16px;font-size:12px">
      ${_channelSection('Slack', 'slack', [
        { id: 'notif-slack-enabled', type: 'checkbox', label: 'Enabled', checked: !!s.enabled },
        { id: 'notif-slack-webhook', type: 'text', label: 'Webhook URL', value: s.webhook_url || '', placeholder: 'https://hooks.slack.com/...' },
        { id: 'notif-slack-channel', type: 'text', label: 'Channel', value: s.channel || '', placeholder: '#alerts' },
        { id: 'notif-slack-severity', type: 'select', label: 'Min Severity', value: s.min_severity || 'warning', options: ['info', 'warning', 'critical'] },
      ])}

      ${_channelSection('Microsoft Teams', 'teams', [
        { id: 'notif-teams-enabled', type: 'checkbox', label: 'Enabled', checked: !!t.enabled },
        { id: 'notif-teams-webhook', type: 'text', label: 'Webhook URL', value: t.webhook_url || '', placeholder: 'https://...' },
        { id: 'notif-teams-severity', type: 'select', label: 'Min Severity', value: t.min_severity || 'warning', options: ['info', 'warning', 'critical'] },
      ])}

      ${_channelSection('Email', 'email', [
        { id: 'notif-email-enabled', type: 'checkbox', label: 'Enabled', checked: !!e.enabled },
        { id: 'notif-email-host', type: 'text', label: 'SMTP Host', value: e.smtp_host || '' },
        { id: 'notif-email-port', type: 'number', label: 'SMTP Port', value: e.smtp_port || 587 },
        { id: 'notif-email-user', type: 'text', label: 'Username', value: e.smtp_username || '' },
        { id: 'notif-email-pass', type: 'password', label: 'Password', value: '' },
        { id: 'notif-email-from', type: 'text', label: 'From Address', value: e.from_address || '' },
        { id: 'notif-email-recipients', type: 'text', label: 'Recipients (comma-separated)', value: (e.recipients || []).join(', ') },
        { id: 'notif-email-severity', type: 'select', label: 'Min Severity', value: e.min_severity || 'warning', options: ['info', 'warning', 'critical'] },
      ])}

      ${_channelSection('PagerDuty', 'pagerduty', [
        { id: 'notif-pd-enabled', type: 'checkbox', label: 'Enabled', checked: !!p.enabled },
        { id: 'notif-pd-key', type: 'text', label: 'Integration Key', value: p.integration_key || '' },
        { id: 'notif-pd-service', type: 'text', label: 'Service Name', value: p.service_name || '' },
        { id: 'notif-pd-severity', type: 'select', label: 'Min Severity', value: p.min_severity || 'warning', options: ['info', 'warning', 'critical'] },
      ])}

      ${_channelSection('Webhook', 'webhook', [
        { id: 'notif-webhook-enabled', type: 'checkbox', label: 'Enabled', checked: !!w.enabled },
        { id: 'notif-webhook-url', type: 'text', label: 'URL', value: w.url || '', placeholder: 'https://...' },
        { id: 'notif-webhook-header', type: 'text', label: 'Secret Header Name', value: w.secret_header || '' },
        { id: 'notif-webhook-secret', type: 'password', label: 'Secret Value', value: '' },
        { id: 'notif-webhook-severity', type: 'select', label: 'Min Severity', value: w.min_severity || 'warning', options: ['info', 'warning', 'critical'] },
      ])}

      <div style="display:flex;gap:8px;margin-top:4px">
        <button class="ds-btn ds-btn-primary" id="notif-save-btn">Save Configuration</button>
      </div>
    </div>
  `;

  // Attach save handler
  body.querySelector('#notif-save-btn')?.addEventListener('click', _saveConfig);

  // Attach test buttons
  body.querySelectorAll('.notif-test-btn').forEach(btn => {
    btn.addEventListener('click', () => _testChannel(btn.dataset.channel));
  });
}

function _channelSection(title, channel, fields) {
  const fieldHTML = fields.map(f => {
    if (f.type === 'checkbox') {
      return `<label style="display:flex;align-items:center;gap:6px;cursor:pointer">
        <input type="checkbox" id="${f.id}" ${f.checked ? 'checked' : ''}>
        <span style="font-weight:500;color:var(--text)">${esc(f.label)}</span>
      </label>`;
    }
    if (f.type === 'select') {
      return `<div>
        <label style="font-size:11px;color:var(--muted);display:block;margin-bottom:3px">${esc(f.label)}</label>
        <select class="ds-select" id="${f.id}" style="width:100%">${f.options.map(o => `<option value="${o}" ${f.value === o ? 'selected' : ''}>${o}</option>`).join('')}</select>
      </div>`;
    }
    return `<div>
      <label style="font-size:11px;color:var(--muted);display:block;margin-bottom:3px">${esc(f.label)}</label>
      <input class="ds-input" id="${f.id}" type="${f.type}" value="${esc(String(f.value || ''))}" ${f.placeholder ? `placeholder="${esc(f.placeholder)}"` : ''} style="width:100%">
    </div>`;
  }).join('');

  return `
    <div style="border:1px solid var(--border);border-radius:8px;padding:12px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <span style="font-weight:600;font-size:13px;color:var(--text)">${esc(title)}</span>
        <button class="ds-btn ds-btn-ghost ds-btn-sm notif-test-btn" data-channel="${channel}" style="font-size:10px">Test</button>
      </div>
      <div style="display:flex;flex-direction:column;gap:8px">${fieldHTML}</div>
    </div>`;
}

// ── Save Config ──────────────────────────────────────────────────────────────

async function _saveConfig() {
  const btn = document.getElementById('notif-save-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Saving\u2026';

  const val = (id) => document.getElementById(id)?.value || '';
  const checked = (id) => document.getElementById(id)?.checked || false;

  const payload = {
    slack: {
      enabled: checked('notif-slack-enabled'),
      webhook_url: val('notif-slack-webhook').trim(),
      channel: val('notif-slack-channel').trim() || null,
      min_severity: val('notif-slack-severity'),
    },
    teams: {
      enabled: checked('notif-teams-enabled'),
      webhook_url: val('notif-teams-webhook').trim(),
      min_severity: val('notif-teams-severity'),
    },
    email: {
      enabled: checked('notif-email-enabled'),
      smtp_host: val('notif-email-host').trim(),
      smtp_port: parseInt(val('notif-email-port')) || 587,
      smtp_username: val('notif-email-user').trim(),
      smtp_password: val('notif-email-pass') || undefined,
      from_address: val('notif-email-from').trim(),
      recipients: val('notif-email-recipients').split(',').map(s => s.trim()).filter(Boolean),
      min_severity: val('notif-email-severity'),
    },
    pagerduty: {
      enabled: checked('notif-pd-enabled'),
      integration_key: val('notif-pd-key').trim(),
      service_name: val('notif-pd-service').trim(),
      min_severity: val('notif-pd-severity'),
    },
    webhook: {
      enabled: checked('notif-webhook-enabled'),
      url: val('notif-webhook-url').trim(),
      secret_header: val('notif-webhook-header').trim() || null,
      secret_value: val('notif-webhook-secret') || undefined,
      min_severity: val('notif-webhook-severity'),
    },
  };

  try {
    const resp = await rawFetch('/api/v1/notifications/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    btn.textContent = 'Saved!';
    toast('Notification configuration saved', 'success');
    setTimeout(() => { btn.textContent = 'Save Configuration'; btn.disabled = false; }, 2000);
  } catch (e) {
    toast('Error saving: ' + e.message, 'error');
    btn.textContent = 'Save Configuration';
    btn.disabled = false;
  }
}

// ── Test Channel ─────────────────────────────────────────────────────────────

async function _testChannel(channel) {
  try {
    const resp = await rawFetch(`/api/v1/notifications/test/${channel}`, { method: 'POST' });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.detail || resp.statusText);
    }
    toast(`Test notification sent to ${channel}`, 'success');
  } catch (e) {
    toast('Test failed: ' + e.message, 'error');
  }
}

// ── Rendering: Subscriptions ─────────────────────────────────────────────────

function _renderSubs(apps) {
  const body = document.getElementById('notif-subs-body');
  if (!body) return;

  if (!apps || !Array.isArray(apps) || apps.length === 0) {
    setTileEmpty('notif-subs', {
      icon: 'fa-solid fa-bell',
      title: 'No apps',
      description: 'Register apps to configure notification subscriptions.',
    });
    return;
  }

  const prefs = _loadSubscriptionPrefs();

  // Group by team
  const teams = {};
  apps.forEach(a => {
    const team = a.team_slug || 'Unknown';
    if (!teams[team]) teams[team] = [];
    teams[team].push(a);
  });

  let html = '<div style="padding:8px">';
  Object.keys(teams).sort().forEach(team => {
    const teamApps = teams[team];
    html += `
      <div style="margin-bottom:8px" class="notif-team-group">
        <div class="notif-team-hdr" style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:var(--surface);border:1px solid var(--border);border-radius:6px;cursor:pointer;margin-bottom:4px">
          <span style="font-size:10px;color:var(--muted);display:inline-block;transition:transform 0.2s">\u25BC</span>
          <span style="font-weight:600;font-size:12px;color:var(--text)">${esc(team)}</span>
          <span style="font-size:11px;color:var(--muted)">${teamApps.length} app${teamApps.length !== 1 ? 's' : ''}</span>
        </div>
        <div class="notif-team-apps">`;
    teamApps.forEach(a => {
      const sub = prefs[a.app_id] || { threshold_alerts: false, connection: false, issues: false };
      html += `
        <div style="display:flex;align-items:center;gap:12px;padding:6px 10px 6px 28px;border-bottom:1px solid var(--border)">
          <div style="flex:1;min-width:0">
            <div style="font-weight:500;font-size:12px;color:var(--text)">${esc(a.app_name || '')}</div>
            <div style="font-family:var(--mono);font-size:10px;color:var(--muted)">${esc(a.app_id || '')}</div>
          </div>
          <label style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted)">
            Alerts <input type="checkbox" class="notif-sub-toggle" data-app="${esc(a.app_id)}" data-type="threshold_alerts" ${sub.threshold_alerts ? 'checked' : ''}>
          </label>
          <label style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted)">
            Status <input type="checkbox" class="notif-sub-toggle" data-app="${esc(a.app_id)}" data-type="connection" ${sub.connection ? 'checked' : ''}>
          </label>
          <label style="display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted)">
            Issues <input type="checkbox" class="notif-sub-toggle" data-app="${esc(a.app_id)}" data-type="issues" ${sub.issues ? 'checked' : ''}>
          </label>
        </div>`;
    });
    html += '</div></div>';
  });
  html += '</div>';
  body.innerHTML = html;

  // Toggle collapse
  body.querySelectorAll('.notif-team-hdr').forEach(hdr => {
    hdr.addEventListener('click', () => {
      hdr.parentElement.classList.toggle('notif-collapsed');
    });
  });

  // Subscription toggles
  body.querySelectorAll('.notif-sub-toggle').forEach(cb => {
    cb.addEventListener('change', () => {
      _toggleSubscription(cb.dataset.app, cb.dataset.type, cb.checked);
    });
  });
}

// ── Filter Subs ──────────────────────────────────────────────────────────────

function _filterSubs(query) {
  const q = (query || '').toLowerCase();
  if (!q) {
    _renderSubs(_appsCache);
    return;
  }
  const filtered = _appsCache.filter(a =>
    (a.app_name || '').toLowerCase().includes(q) ||
    (a.app_id || '').toLowerCase().includes(q) ||
    (a.team_slug || '').toLowerCase().includes(q)
  );
  _renderSubs(filtered);
}

// ── Subscription Prefs (localStorage) ────────────────────────────────────────

function _loadSubscriptionPrefs() {
  try {
    return JSON.parse(localStorage.getItem('modus_notif_subs') || '{}');
  } catch (_) { return {}; }
}

function _toggleSubscription(appId, type, enabled) {
  const prefs = _loadSubscriptionPrefs();
  if (!prefs[appId]) prefs[appId] = {};
  prefs[appId][type] = enabled;
  try {
    localStorage.setItem('modus_notif_subs', JSON.stringify(prefs));
  } catch (_) { /* quota exceeded, ignore */ }
}

// ── Rendering: Delivery ──────────────────────────────────────────────────────

function _renderDelivery(alerts) {
  const body = document.getElementById('notif-delivery-body');
  if (!body) return;

  if (!alerts || !Array.isArray(alerts) || alerts.length === 0) {
    setTileEmpty('notif-delivery', {
      icon: 'fa-solid fa-heart-pulse',
      title: 'No delivery data',
      description: 'Delivery stats will appear after alerts fire and notifications are sent.',
    });
    return;
  }

  // Calculate delivery stats
  const withNotif = alerts.filter(a => a.notification_sent != null);
  const sent = withNotif.filter(a => a.notification_sent === true);
  const failed = withNotif.filter(a => a.notification_sent === false);
  const total = withNotif.length;
  const successRate = total > 0 ? ((sent.length / total) * 100).toFixed(1) : '—';

  // Per-channel health from notification_result
  const channelStats = {};
  withNotif.forEach(a => {
    const result = a.notification_result;
    if (result && typeof result === 'object') {
      Object.keys(result).forEach(ch => {
        if (!channelStats[ch]) channelStats[ch] = { success: 0, fail: 0 };
        if (result[ch] === true || (result[ch] && result[ch].success)) {
          channelStats[ch].success++;
        } else {
          channelStats[ch].fail++;
        }
      });
    }
  });

  // Recent failures
  const recentFailures = failed.slice(0, 5);

  body.innerHTML = `
    <div style="padding:12px;display:flex;flex-direction:column;gap:16px">
      <!-- Summary KPIs -->
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:100px;padding:12px;border-radius:8px;background:var(--surface);border:1px solid var(--border);text-align:center">
          <div style="font-size:10px;text-transform:uppercase;color:var(--muted);letter-spacing:0.5px">Success Rate</div>
          <div style="font-size:22px;font-weight:700;color:${parseFloat(successRate) >= 90 ? 'var(--accent)' : parseFloat(successRate) >= 70 ? '#f59e0b' : 'var(--danger)'}">${successRate}%</div>
        </div>
        <div style="flex:1;min-width:100px;padding:12px;border-radius:8px;background:var(--surface);border:1px solid var(--border);text-align:center">
          <div style="font-size:10px;text-transform:uppercase;color:var(--muted);letter-spacing:0.5px">Sent</div>
          <div style="font-size:22px;font-weight:700;color:var(--accent)">${sent.length}</div>
        </div>
        <div style="flex:1;min-width:100px;padding:12px;border-radius:8px;background:var(--surface);border:1px solid var(--border);text-align:center">
          <div style="font-size:10px;text-transform:uppercase;color:var(--muted);letter-spacing:0.5px">Failed</div>
          <div style="font-size:22px;font-weight:700;color:${failed.length > 0 ? 'var(--danger)' : 'var(--muted)'}">${failed.length}</div>
        </div>
      </div>

      <!-- Per-Channel Health -->
      ${Object.keys(channelStats).length > 0 ? `
      <div>
        <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Channel Health</div>
        ${Object.entries(channelStats).map(([ch, stats]) => {
          const chTotal = stats.success + stats.fail;
          const chRate = chTotal > 0 ? ((stats.success / chTotal) * 100).toFixed(0) : '—';
          const chColor = parseFloat(chRate) >= 90 ? 'var(--accent)' : parseFloat(chRate) >= 70 ? '#f59e0b' : 'var(--danger)';
          return `
            <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)">
              <span style="font-size:12px;font-weight:500;color:var(--text);text-transform:capitalize">${esc(ch)}</span>
              <div style="display:flex;align-items:center;gap:8px">
                <span style="font-size:11px;color:var(--muted)">${stats.success}/${chTotal}</span>
                <span style="font-size:11px;font-weight:600;color:${chColor}">${chRate}%</span>
              </div>
            </div>`;
        }).join('')}
      </div>` : ''}

      <!-- Recent Failures -->
      ${recentFailures.length > 0 ? `
      <div>
        <div style="font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;margin-bottom:8px">Recent Failures</div>
        ${recentFailures.map(a => `
          <div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:11px">
            <div style="display:flex;justify-content:space-between">
              <span style="color:var(--danger);font-weight:500">${esc(a.metric || 'Alert')}</span>
              <span style="color:var(--muted)">${a.fired_at ? new Date(a.fired_at).toLocaleString() : '—'}</span>
            </div>
            ${a.notification_result ? `<div style="color:var(--muted);margin-top:2px;font-size:10px">${esc(typeof a.notification_result === 'string' ? a.notification_result : JSON.stringify(a.notification_result))}</div>` : ''}
          </div>
        `).join('')}
      </div>` : ''}
    </div>
  `;
}
