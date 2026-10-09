/**
 * Modus Dashboard — Help System
 *
 * Centralized hover-help and in-app guide registry. Provides:
 *   - HELP_REGISTRY: per-tile-id explanations + "learn more" links
 *   - GUIDES: catalog of step-by-step guides for the Help portal view
 *   - attachHelpIcon(headerEl, helpKey): inject a [?] icon into a tile header
 *   - showTooltip / hideTooltip: vanilla-JS hover tooltip primitives
 *
 * Makes no network calls itself. The tile tooltips and guide summaries below
 * are bundled; the full guide text lives in docs/guides/ and is linked on
 * GitHub from the Help view.
 */

import { esc } from './api.js';

// ── Tile help registry ──────────────────────────────────────────
// Keyed by the tile id used in createTile({ id }) calls.
// Each entry: { title, body, learnMore? (guide id), learnMoreUrl? (deep link) }
//
// To add new help: append to this object. The tile.js header injection
// will pick it up automatically on the next page load.

export const HELP_REGISTRY = {
  // ── Overview ──────────────────────────────────────────────────
  'overview-kpis': {
    title: 'Overview KPIs',
    body: 'Cost, tokens, calls, active apps, online agents, and budget health for the selected period. Budget cards only appear when at least one team has a budget configured.',
  },
  'overview-cost-chart': {
    title: 'Cost Over Time',
    body: 'Daily AI spend across all instrumented apps. Click any data point to drill into which apps, providers, and models drove that period\'s cost.',
    learnMore: '03-investigate-cost-spike',
  },
  'overview-providers': {
    title: 'Provider Breakdown',
    body: 'Cost split across AI providers (Anthropic, OpenAI, Bedrock, etc.) for the selected period.',
  },
  'overview-top-apps': {
    title: 'Top Apps by Cost',
    body: 'Your most expensive apps in the selected period. Click any row to drill into the app\'s model breakdown, recent alerts, and metadata.',
    learnMore: '03-investigate-cost-spike',
  },
  'overview-top-models': {
    title: 'Top Models',
    body: 'Models ranked by total cost. Useful for spotting expensive models that should be routed or restricted.',
    learnMore: '02-block-expensive-models',
  },
  'overview-alerts': {
    title: 'Recent Alerts',
    body: 'Last 20 fired alerts across all apps. Click any alert for full details (actual vs threshold, fire time, ack status).',
  },
  'overview-agents': {
    title: 'Agent Status',
    body: 'Apps reporting telemetry. Green = heartbeat in last 5 minutes. Red = stale or never seen — likely the SDK is misconfigured.',
    learnMore: '08-instrument-first-app',
  },

  // ── DevOps ────────────────────────────────────────────────────
  'devops-kpis': {
    title: 'DevOps KPIs',
    body: 'At-a-glance health for engineers: session cost, today\'s cost, blocked calls, tokens per call, p95 latency, online agents.',
  },
  'devops-anomalies': {
    title: 'Anomaly Detection',
    body: 'AI cost or behavior that deviates from a 14-day rolling baseline. Z-score ≥ 3 = significant. Click any anomaly for details. The scan runs every 5 minutes on SQLite and PostgreSQL.',
  },
  'devops-recommend': {
    title: 'Model Optimization',
    body: 'Recommendations to swap to cheaper models where high-volume calls produce short outputs. Estimated monthly savings shown per recommendation.',
    learnMore: '05-route-to-cheaper-models',
  },
  'devops-enforcement': {
    title: 'Enforcement Summary',
    body: 'How many calls were allowed, blocked, throttled, or redirected today. Plus total cost saved by blocks.',
  },
  'devops-deployments': {
    title: 'Cost by Deployment',
    body: 'Per git-SHA AI cost. Use this to spot which deploy changed your cost profile (regression detection).',
  },
  'devops-providers': {
    title: 'Provider Breakdown (DevOps)',
    body: 'Same as Overview but DevOps-scoped. Shows cost split across AI providers for engineers triaging issues.',
  },
  'devops-agents': {
    title: 'Agent Registry',
    body: 'Every registered app: environment, last-seen time, instrumented providers, agent SDK version. Click an app for full details.',
    learnMore: '08-instrument-first-app',
  },

  // ── Finance ───────────────────────────────────────────────────
  'fin-kpis': {
    title: 'Finance KPIs',
    body: 'Month-to-date spend, end-of-month forecast, budget remaining, overall risk status. Always shows MTD regardless of period selector.',
  },
  'fin-spend-trend': {
    title: 'Spend Trend (MTD)',
    body: 'Daily spend with the daily budget line overlaid. Toggle Compare to see prior month as a dashed line.',
  },
  'fin-dept-donut': {
    title: 'Spend by Department',
    body: 'Cost split by cost-center department. Empty until teams are assigned to cost centers.',
    learnMore: '04-set-up-chargeback',
  },
  'fin-burn-rate': {
    title: 'Budget Burn Rate by Team',
    body: 'Each team\'s budget, current spend, projected end-of-month, burn %, and risk classification (on-track / at-risk / over-budget).',
    learnMore: '01-set-your-first-budget',
  },
  'fin-provider-bar': {
    title: 'Spend by Provider (Finance)',
    body: 'Horizontal bar chart of AI spend per provider for the current month. Useful for vendor negotiation.',
  },
  'fin-forecast': {
    title: 'Spend Forecast',
    body: 'Statistical projection for end-of-month, end-of-quarter, and end-of-year. R² shows confidence — high (≥0.7), medium (0.4-0.7), low (<0.4).',
  },
  'fin-breach': {
    title: 'Budget Breach Alerts',
    body: 'Teams predicted to breach budget, with the predicted breach date and projected overage.',
    learnMore: '06-handle-budget-breach-alert',
  },
  'fin-team-gauges': {
    title: 'Team Budget Utilization',
    body: 'Visual gauges showing each team\'s burn % against its monthly budget.',
  },
  'fin-reports': {
    title: 'Scheduled Reports',
    body: 'Chargeback, burn-rate, variance, forecast and audit reports on a schedule, delivered to a webhook or Slack. Created through the API (POST /api/v1/finance/reports); this tile lists them.',
    learnMore: '07-export-finance-report',
  },
  'fin-chargeback': {
    title: 'Chargeback',
    body: 'Per-cost-center allocation of AI spend. Click Generate to compute fresh allocations from current usage. Export as CSV for your GL system.',
    learnMore: '04-set-up-chargeback',
  },
  'fin-reconciliation': {
    title: 'Reconciliation Status',
    body: 'Tracked spend vs what the provider billed, per imported invoice total. Use Import CSV (provider, period_start, period_end, actual_cost_usd) to load invoices; Modus does not fetch them from providers. Tracked cost is recomputed from usage each time you open the view; variance within 2% counts as matched.',
  },
  'fin-cost-centers': {
    title: 'Cost Center Assignment',
    body: 'Cost center registry. Click + Assign to create a new cost center. Link teams to centers with POST /api/v1/finance/allocation.',
    learnMore: '04-set-up-chargeback',
  },

  // ── Teams ─────────────────────────────────────────────────────
  'teams-table': {
    title: 'Teams',
    body: 'Teams own apps, budgets and policies. Edit changes name, slug, parent, budgets and cost center. Delete is a soft delete: if the team has apps you choose to move them to another team or deactivate them (their API keys stop working), and child teams must be moved too. Tick Show deleted teams to Restore one. Usage history is always kept.',
  },

  // ── Policies ──────────────────────────────────────────────────
  'policies-table': {
    title: 'Governance Policies',
    body: 'Active and inactive policies. Each policy is one of: budget cap, rate limit, model allowlist, model denylist, provider block, environment block, token cap, latency cap, degradation ladder, amplification gate or retry circuit breaker.',
    learnMore: '01-set-your-first-budget',
  },
  'policies-decisions': {
    title: 'Enforcement Detail',
    body: 'Top denial reasons, decision counts, and the 25 most recent allow/deny/throttle decisions. Use this to confirm your policies are actually doing what you think they\'re doing.',
  },

  // ── Sessions ──────────────────────────────────────────────────
  'sessions-list': {
    title: 'Recent Sessions',
    body: 'End-to-end attribution for multi-step AI workflows. Each session = one user request that may span many AI calls. Confidence = how sure Modus is about the attribution.',
  },
  'sessions-amplification': {
    title: 'Amplification Ratio',
    body: 'Nodes whose output verbosity drives downstream cost. High ratio (>5x) = optimization target — usually a node generating long responses that get fed into expensive downstream calls.',
  },
  'sessions-retry-tax': {
    title: 'Retry Tax',
    body: 'Cost wasted on retries from rate limits, timeouts, and errors. Attributed to the node that *caused* the retry, not the node that retried.',
  },
  'sessions-defensive': {
    title: 'Defensive Spend',
    body: 'Cost of fallback subgraphs and validation calls triggered by earlier failures or guard rails. High defensive spend means your pipeline is paying a tax for safety.',
  },

  // ── Pricing ───────────────────────────────────────────────────
  'pricing-overrides': {
    title: 'Pricing Overrides',
    body: 'Custom prices you\'ve negotiated with providers. Scoped globally, per-team, or per-app. Used for accurate cost calculation when you have a contract rate.',
  },
  'pricing-global': {
    title: 'Global Pricing',
    body: 'List prices Modus uses when no override applies. Bundled with the release; optionally refreshed from provider APIs when MODUS_PRICING_LIVE_FETCH_ENABLED is true.',
  },
  'pricing-history': {
    title: 'Override History',
    body: 'Audit trail of pricing override changes: when, by whom, before/after values.',
  },

  // ── Notifications ─────────────────────────────────────────────
  'notif-config': {
    title: 'Notification Channels',
    body: 'Configure delivery to Slack, Teams, Email, PagerDuty, and generic webhooks. Each channel has its own minimum severity threshold.',
  },
  'notif-subs': {
    title: 'App Subscriptions',
    body: 'Per-app opt-in for threshold alerts, status changes, and issues. Lets you route different apps to different on-call rotations.',
  },
  'notif-delivery': {
    title: 'Delivery Health',
    body: 'Notification success rate, sent/failed counts, per-channel health, and recent failures with reasons. If alerts aren\'t reaching Slack, this is where you\'ll see why.',
  },

  // ── Compliance / Sentinel ─────────────────────────────────────
  'comp-attestation': {
    title: 'Attestation Chain',
    body: 'Tamper-evident hash chain over enforcement decisions. Each entry commits to the previous one, so any later edit to the recorded history is detectable.',
  },
  'comp-pqc': {
    title: 'Post-Quantum Cryptography',
    body: 'Readiness scoring for quantum-safe migration. Note what the default signing tier actually provides: HMAC-SHA-512 is a symmetric MAC, giving quantum-resistant tamper-evidence but NOT a publicly verifiable signature — an outside auditor cannot independently verify it without the shared secret. Real ML-DSA-65 signatures require the optional pqcrypto package. Status never reports ML-DSA when only the HMAC path is active.',
  },
  'sentinel-pqc': {
    title: 'PQC Compliance',
    body: 'Post-quantum posture for this deployment. The default attestation tier is HMAC-SHA-512 (a symmetric MAC — tamper-evidence, not a publicly verifiable signature). Publicly verifiable ML-DSA-65 signatures require the optional pqcrypto package.',
  },
  'sentinel-zk': {
    title: 'Trajectory Receipt Coverage',
    body: 'Share of sessions carrying a tamper-evident trajectory receipt. These are SHA-256 hash-chain commitments binding the action sequence to the active policy set — they prove the record has not been altered. They are NOT zero-knowledge proofs: the receipt does not hide the trajectory, and public inputs include aggregate cost, token, and call figures.',
  },
};

// ── Guide catalog (mirrors docs/guides/INDEX.md) ────────────────
// Each entry: { id, title, audience, time, summary, body (markdown) }
//
// Bodies are inlined here so the Help portal works air-gapped. To add a
// new guide: write the .md file in docs/guides/ AND add an entry here.
// A future build step can auto-sync these from the .md files.

export const GUIDES = [
  {
    id: '01-set-your-first-budget',
    title: 'Set Your First Budget',
    audience: 'FinOps · Eng Manager',
    time: '5 min',
    summary: 'Stop AI overspend before it happens. Create a monthly budget cap through the API and have Modus block calls when the cap is hit.',
    githubPath: 'docs/guides/01-set-your-first-budget.md',
  },
  {
    id: '02-block-expensive-models',
    title: 'Block Expensive Models',
    audience: 'DevOps · Eng Manager',
    time: '5 min',
    summary: 'Prevent specific models (e.g. GPT-4 Turbo, Claude Opus) from being called by certain teams or apps via a denylist policy.',
    githubPath: 'docs/guides/02-block-expensive-models.md',
  },
  {
    id: '03-investigate-cost-spike',
    title: 'Investigate a Cost Spike',
    audience: 'DevOps · On-call',
    time: '8 min',
    summary: 'You opened the dashboard and the cost chart has a peak. Find out what caused it in under 10 minutes.',
    githubPath: 'docs/guides/03-investigate-cost-spike.md',
  },
  {
    id: '04-set-up-chargeback',
    title: 'Set Up Cost Center Chargeback',
    audience: 'FinOps · Finance',
    time: '12 min',
    summary: 'Allocate AI spend back to the business units that consumed it. Cost centers, team allocation, and chargeback.',
    githubPath: 'docs/guides/04-set-up-chargeback.md',
  },
  {
    id: '05-route-to-cheaper-models',
    title: 'Route to Cheaper Models',
    audience: 'DevOps · Eng Manager',
    time: '10 min',
    summary: 'Route eligible calls to cheaper models. See how routing learns which prompt shapes are safe, and watch savings accumulate.',
    githubPath: 'docs/guides/05-route-to-cheaper-models.md',
  },
  {
    id: '06-handle-budget-breach-alert',
    title: 'Handle a Budget Breach Alert',
    audience: 'On-call · FinOps',
    time: '5 min',
    summary: 'Slack just pinged you that the production team is over budget. Confirm the breach, identify the cause, stop the bleeding.',
    githubPath: 'docs/guides/06-handle-budget-breach-alert.md',
  },
  {
    id: '07-export-finance-report',
    title: 'Export a Finance Report',
    audience: 'FinOps · Finance',
    time: '3 min',
    summary: 'Pull spend data into your accounting system or a spreadsheet. CSV/JSON from many tiles, or scheduled webhook/Slack reports.',
    githubPath: 'docs/guides/07-export-finance-report.md',
  },
  {
    id: '08-instrument-first-app',
    title: 'Instrument Your First Application',
    audience: 'Developer',
    time: '10 min',
    summary: 'Get an app reporting AI usage to Modus: install the SDK, set MODUS_URL and MODUS_TEAM_TOKEN, watch your first call appear.',
    githubPath: 'docs/guides/08-instrument-first-app.md',
  },
];

// ── Tooltip primitives ──────────────────────────────────────────
// Single shared tooltip element, repositioned on hover. Lighter than
// per-element tooltips and avoids the title= "blink and disappear" UX.

let _tooltipEl = null;

function _ensureTooltip() {
  if (_tooltipEl) return _tooltipEl;
  _tooltipEl = document.createElement('div');
  _tooltipEl.className = 'modus-help-tooltip';
  _tooltipEl.setAttribute('role', 'tooltip');
  _tooltipEl.style.cssText = `
    position: fixed;
    z-index: 99999;
    max-width: 320px;
    padding: 12px 14px;
    background: var(--surface, #1a1d2e);
    color: var(--text, #f5f7fb);
    border: 1px solid var(--border, #2a2f48);
    border-radius: 8px;
    font-size: 12px;
    line-height: 1.5;
    box-shadow: 0 8px 24px rgba(0,0,0,0.5);
    pointer-events: auto;
    opacity: 0;
    transform: translateY(4px);
    transition: opacity 0.12s ease, transform 0.12s ease;
    display: none;
  `;
  document.body.appendChild(_tooltipEl);
  return _tooltipEl;
}

/**
 * Show the help tooltip anchored to a target element.
 * @param {HTMLElement} target  - Element to anchor against
 * @param {string} helpKey      - Key into HELP_REGISTRY
 */
export function showTooltip(target, helpKey) {
  const entry = HELP_REGISTRY[helpKey];
  if (!entry) return;

  const tip = _ensureTooltip();

  let html = `
    <div style="font-weight:600;font-size:12px;color:var(--text);margin-bottom:6px">${esc(entry.title)}</div>
    <div style="color:var(--muted);font-size:11px;line-height:1.55">${esc(entry.body)}</div>
  `;

  if (entry.learnMore) {
    html += `
      <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">
        <a href="#help/${entry.learnMore}" class="modus-help-link" style="font-size:11px;color:var(--accent);text-decoration:none;font-weight:600">
          <i class="fa-solid fa-book-open" style="margin-right:4px"></i>Learn more →
        </a>
      </div>
    `;
  } else if (entry.learnMoreUrl) {
    html += `
      <div style="margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">
        <a href="${esc(entry.learnMoreUrl)}" target="_blank" rel="noopener" style="font-size:11px;color:var(--accent);text-decoration:none;font-weight:600">
          <i class="fa-solid fa-book-open" style="margin-right:4px"></i>Learn more →
        </a>
      </div>
    `;
  }

  tip.innerHTML = html;
  tip.style.display = 'block';

  // Position: prefer below-left of target, flip if off-screen
  const rect = target.getBoundingClientRect();
  const tipRect = tip.getBoundingClientRect();
  let top = rect.bottom + 8;
  let left = rect.left;

  // Flip up if no room below
  if (top + tipRect.height > window.innerHeight - 12) {
    top = Math.max(12, rect.top - tipRect.height - 8);
  }
  // Pull left if going off the right edge
  if (left + tipRect.width > window.innerWidth - 12) {
    left = Math.max(12, window.innerWidth - tipRect.width - 12);
  }

  tip.style.top = `${top}px`;
  tip.style.left = `${left}px`;

  // Animate in next frame
  requestAnimationFrame(() => {
    tip.style.opacity = '1';
    tip.style.transform = 'translateY(0)';
  });
}

/**
 * Hide the shared help tooltip.
 */
export function hideTooltip() {
  if (!_tooltipEl) return;
  _tooltipEl.style.opacity = '0';
  _tooltipEl.style.transform = 'translateY(4px)';
  // Defer display:none so the fade animation actually plays
  setTimeout(() => {
    if (_tooltipEl && _tooltipEl.style.opacity === '0') {
      _tooltipEl.style.display = 'none';
    }
  }, 120);
}

/**
 * Inject a [?] help icon into a tile header element.
 * Called automatically by tile.js when a tile id has a HELP_REGISTRY entry.
 *
 * @param {HTMLElement} headerEl - The .gs-tile-header element
 * @param {string} helpKey       - HELP_REGISTRY key (the tile id)
 */
export function attachHelpIcon(headerEl, helpKey) {
  if (!headerEl || !HELP_REGISTRY[helpKey]) return;

  const titleEl = headerEl.querySelector('.gs-tile-title');
  if (!titleEl) return;

  const icon = document.createElement('button');
  icon.className = 'modus-help-icon';
  icon.type = 'button';
  icon.setAttribute('aria-label', `Help: ${HELP_REGISTRY[helpKey].title}`);
  icon.innerHTML = '<i class="fa-solid fa-circle-question"></i>';
  icon.style.cssText = `
    background: none;
    border: none;
    color: var(--muted);
    cursor: help;
    font-size: 12px;
    margin-left: 6px;
    padding: 2px 4px;
    opacity: 0.55;
    transition: opacity 0.15s, color 0.15s;
  `;

  let hideTimer = null;

  const show = () => {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
    icon.style.opacity = '1';
    icon.style.color = 'var(--accent)';
    showTooltip(icon, helpKey);
  };
  const scheduleHide = () => {
    hideTimer = setTimeout(() => {
      icon.style.opacity = '0.55';
      icon.style.color = 'var(--muted)';
      hideTooltip();
    }, 200);
  };

  icon.addEventListener('mouseenter', show);
  icon.addEventListener('mouseleave', scheduleHide);
  icon.addEventListener('focus', show);
  icon.addEventListener('blur', scheduleHide);
  icon.addEventListener('click', (e) => {
    e.preventDefault();
    show();
  });

  // Keep tooltip alive while pointer is over it (so users can click learn-more)
  const tip = _ensureTooltip();
  tip.addEventListener('mouseenter', () => {
    if (hideTimer) { clearTimeout(hideTimer); hideTimer = null; }
  });
  tip.addEventListener('mouseleave', scheduleHide);

  titleEl.appendChild(icon);
}

/**
 * Look up a guide by id.
 * @param {string} id
 * @returns {object|null}
 */
export function getGuide(id) {
  return GUIDES.find(g => g.id === id) || null;
}
