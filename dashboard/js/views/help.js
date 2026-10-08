/**
 * Modus Dashboard v2 — Help Portal View
 *
 * In-app guide library and reference. Renders the GUIDES catalog from
 * help.js with a sticky left-side TOC ("jumper") and a scrollable
 * content area on the right. Supports deep links: #help/<guide-id>
 * (used by the "Learn more" links inside hover tooltips).
 *
 * Each guide card shows a summary from help.js and links to the full text
 * (docs/guides/*.md) on GitHub, which needs network access.
 */

import { GUIDES } from '../help.js';
import { esc } from '../api.js';

let _container = null;
let _destroyed = false;
let _hashListener = null;

const SECTIONS = [
  {
    id: 'getting-started',
    label: 'Getting Started',
    items: [
      { type: 'link', anchor: 'welcome', label: 'Welcome to Modus' },
      { type: 'link', anchor: 'first-5-min', label: 'First 5 Minutes' },
      { type: 'link', anchor: 'navigation', label: 'Navigation Basics' },
    ],
  },
  {
    id: 'guides',
    label: 'Step-by-Step Guides',
    items: GUIDES.map(g => ({ type: 'guide', id: g.id, label: g.title })),
  },
  {
    id: 'reference',
    label: 'Reference',
    items: [
      { type: 'link', anchor: 'glossary', label: 'Glossary of Metrics' },
      { type: 'link', anchor: 'troubleshooting', label: 'Troubleshooting' },
      { type: 'link', anchor: 'faq', label: 'FAQ' },
      { type: 'link', anchor: 'where-to-get-help', label: 'Where to Get Help' },
    ],
  },
];

export async function render(container) {
  _destroyed = false;
  _container = container;

  container.innerHTML = `
    <div class="help-view" style="display:flex;gap:24px;padding:20px;max-width:1280px;margin:0 auto;align-items:flex-start">
      <!-- Sticky TOC sidebar -->
      <aside class="help-toc" style="position:sticky;top:20px;flex:0 0 240px;max-height:calc(100vh - 60px);overflow-y:auto;padding-right:12px;border-right:1px solid var(--border)">
        ${_renderToc()}
      </aside>

      <!-- Main content -->
      <main class="help-content" style="flex:1;min-width:0;font-size:13px;line-height:1.65;color:var(--text)">
        ${_renderContent()}
      </main>
    </div>
  `;

  _wireTocLinks();
  _wireDeepLink();
}

export function destroy() {
  _destroyed = true;
  _container = null;
  if (_hashListener) {
    window.removeEventListener('hashchange', _hashListener);
    _hashListener = null;
  }
}

// ── TOC ──────────────────────────────────────────────────────────

function _renderToc() {
  const sections = SECTIONS.map(section => {
    const items = section.items.map(item => {
      if (item.type === 'guide') {
        return `<li><a href="#help/${esc(item.id)}" data-help-jump="guide-${esc(item.id)}" class="help-toc-link" style="display:block;padding:5px 8px;font-size:12px;color:var(--muted);text-decoration:none;border-radius:4px">${esc(item.label)}</a></li>`;
      }
      return `<li><a href="#help" data-help-jump="${esc(item.anchor)}" class="help-toc-link" style="display:block;padding:5px 8px;font-size:12px;color:var(--muted);text-decoration:none;border-radius:4px">${esc(item.label)}</a></li>`;
    }).join('');

    return `
      <div style="margin-bottom:18px">
        <div style="font-size:10px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:0.6px;padding:4px 8px;margin-bottom:4px">${esc(section.label)}</div>
        <ul style="list-style:none;margin:0;padding:0">${items}</ul>
      </div>
    `;
  }).join('');

  return `
    <div style="margin-bottom:16px;padding-bottom:12px;border-bottom:1px solid var(--border)">
      <div style="font-size:14px;font-weight:700;color:var(--text);display:flex;align-items:center;gap:8px">
        <i class="fa-solid fa-circle-question" style="color:var(--accent)"></i>
        Help &amp; Guides
      </div>
      <div style="font-size:11px;color:var(--muted);margin-top:4px">Everything you need to use Modus</div>
    </div>
    ${sections}
  `;
}

function _wireTocLinks() {
  if (!_container) return;
  _container.querySelectorAll('.help-toc-link').forEach(link => {
    link.addEventListener('mouseenter', () => { link.style.background = 'rgba(255,255,255,0.04)'; link.style.color = 'var(--text)'; });
    link.addEventListener('mouseleave', () => { link.style.background = ''; link.style.color = ''; });
    link.addEventListener('click', (e) => {
      const target = link.getAttribute('data-help-jump');
      if (target) {
        e.preventDefault();
        _scrollTo(target);
      }
    });
  });
}

function _scrollTo(anchorId) {
  if (!_container) return;
  const el = _container.querySelector(`[data-help-anchor="${anchorId}"]`);
  if (!el) return;
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  // Mark active in TOC
  _container.querySelectorAll('.help-toc-link').forEach(l => {
    l.classList.toggle('active', l.getAttribute('data-help-jump') === anchorId);
  });
}

// Honor #help/<guide-id> deep links from hover tooltips
function _wireDeepLink() {
  const apply = () => {
    const hash = window.location.hash || '';
    const m = hash.match(/^#help\/(.+)$/);
    if (m && m[1]) _scrollTo(`guide-${m[1]}`);
  };
  setTimeout(apply, 100);
  // Also respond to in-view link clicks that change the hash
  _hashListener = apply;
  window.addEventListener('hashchange', _hashListener);
}

// ── Content body ─────────────────────────────────────────────────

function _renderContent() {
  return `
    ${_section('welcome', 'Welcome to Modus', `
      <p>Modus is a self-hosted AI cost governance platform. It watches the AI calls your instrumented applications make, enforces budgets and policies <strong>before</strong> the call happens, can route calls to cheaper models when quality allows, and shows you where your spend is going. Usage data stays in your own database.</p>
      <h4>What you can do with Modus</h4>
      <ul>
        <li><strong>See every AI dollar spent</strong> across all apps, teams, models, and providers</li>
        <li><strong>Stop overspend</strong> with budgets that block calls before they incur cost</li>
        <li><strong>Catch anomalies</strong> before they show up on the next bill</li>
        <li><strong>Reduce spend</strong> by routing eligible traffic to cheaper models automatically</li>
        <li><strong>Allocate cost</strong> to cost centers for chargeback and accounting</li>
        <li><strong>Predict end-of-month spend</strong> before the month ends</li>
      </ul>
    `)}

    ${_section('first-5-min', 'First 5 Minutes', `
      <ol>
        <li>Open the dashboard at the URL your administrator gave you.</li>
        <li>Land on the <strong>Overview</strong> view — this is the home page.</li>
        <li>Look at the top KPI row: today's cost, 7-day cost, monthly budget burn, and risk status. If burn is green, you're on track.</li>
        <li>Scroll down to <strong>Top Apps by Cost</strong>. Click any row to drill into that app's model breakdown and recent alerts.</li>
        <li>Click the <strong>Cost Over Time</strong> chart at any peak — a modal will show what apps and models drove that period's spend.</li>
      </ol>
      <p>That's the loop: <strong>observe → drill → act</strong>. Everything else is depth.</p>
    `)}

    ${_section('navigation', 'Navigation Basics', `
      <p>The left sidebar lists every view. Use the period selector at the top to control the time range for cost-over-time charts and breakdown tables.</p>
      <p><strong>The Finance view always shows month-to-date</strong> — it doesn't change with the period selector.</p>
      <h4>Hover for help</h4>
      <p>Tiles with registered help text show a <code>?</code> icon next to the title. Hover for a short explanation; "Learn more" in the tooltip jumps to the matching guide card on this page.</p>
    `)}

    ${_renderGuidesSection()}

    ${_section('glossary', 'Glossary of Metrics', `
      <table class="ds-table" style="width:100%;font-size:12px">
        <thead><tr><th style="width:30%">Term</th><th>Meaning</th></tr></thead>
        <tbody>
          <tr><td><strong>MTD</strong></td><td>Month-to-date — cost from the 1st of the current calendar month through now</td></tr>
          <tr><td><strong>EOM</strong></td><td>End-of-month — projected total for the full current month</td></tr>
          <tr><td><strong>Burn %</strong></td><td>Percent of monthly budget consumed so far this month</td></tr>
          <tr><td><strong>Risk</strong></td><td><code>on-track</code> (below 80% burn), <code>at-risk</code> (80% up to 100%), <code>over-budget</code> (100% or more)</td></tr>
          <tr><td><strong>Z-score</strong></td><td>Standard deviations from a 14-day rolling baseline; ≥3 = significant</td></tr>
          <tr><td><strong>R²</strong></td><td>Forecast confidence; ≥0.7 = high, 0.4-0.7 = medium, &lt;0.4 = low</td></tr>
          <tr><td><strong>Amplification factor</strong></td><td>(downstream calls + tokens) / (this node's direct cost)</td></tr>
          <tr><td><strong>Retry tax</strong></td><td>Cost of retries attributed to the node that <em>caused</em> them</td></tr>
          <tr><td><strong>Defensive spend</strong></td><td>Cost of fallback subgraphs triggered by earlier failures</td></tr>
          <tr><td><strong>Drift score</strong></td><td>How much a routing decision's quality has deviated from baseline</td></tr>
          <tr><td><strong>Enforcement mix</strong></td><td>Counts of allowed / blocked / throttled / redirected calls</td></tr>
          <tr><td><strong>Online agent</strong></td><td>App SDK that has sent a heartbeat in the last 5 minutes</td></tr>
          <tr><td><strong>Cost center</strong></td><td>Accounting bucket teams are assigned to for chargeback</td></tr>
          <tr><td><strong>Variance</strong></td><td>Tracked spend minus billed spend for a reconciliation period</td></tr>
        </tbody>
      </table>
    `)}

    ${_section('troubleshooting', 'Troubleshooting', `
      <h4>The dashboard is empty / shows "No data"</h4>
      <p>Most tiles need at least one app reporting telemetry. If your app isn't listed in <strong>Agent Registry</strong>, the SDK isn't connecting. See the <a href="#help/08-instrument-first-app" data-help-jump="guide-08-instrument-first-app">Instrument Your First App</a> guide.</p>

      <h4>An app shows "offline" but I know it's running</h4>
      <p>The agent's heartbeat is older than 5 minutes. Check the app's logs for SDK errors. Likely causes: a wrong <code>MODUS_TEAM_TOKEN</code> or an unreachable <code>MODUS_URL</code>.</p>

      <h4>My budget cap isn't blocking calls</h4>
      <p>Check <strong>Policies → Enforcement Detail</strong>. If your policy isn't listed in the recent decisions, it may not be active, may have a higher-priority policy in front of it, or may have a scope mismatch.</p>

      <h4>Compare mode shows 0% deltas</h4>
      <p>Make sure you're comparing periods with data on both sides. Brand-new deployments won't have prior-month data yet.</p>

      <h4>I see different cost numbers in different views</h4>
      <p>Overview and Executive use the period selector (default 7D). Finance always uses MTD. They will not match unless you switch all to the same window.</p>
    `)}

    ${_section('faq', 'FAQ', `
      <h4>Q: Does Modus send my data anywhere?</h4>
      <p>Usage data stays in your database. Optional features that contact other services (price-list sync, Nomus rule pull, assistant, topology summaries, Conductor, federation, and an opt-in AI insight-text request to api.anthropic.com that stays off unless you enable it and configure your own Anthropic key) are listed in the README's "Data flow and privacy" table.</p>

      <h4>Q: Can I run Modus air-gapped?</h4>
      <p>Mostly. Leave the Nomus integration unconfigured, keep <code>MODUS_PRICING_LIVE_FETCH_ENABLED</code> at its default (<code>false</code>) and turn off the insight explanations. The dashboard ships its own copies of all CSS, JS and fonts — no CDN calls. The guide cards link to GitHub.</p>

      <h4>Q: How accurate is the cost calculation?</h4>
      <p>Modus multiplies recorded tokens by your pricing override (if set) or the bundled vendor list price, so it can differ from your invoice (discounts, cached tokens, batch pricing). Add pricing overrides for negotiated rates.</p>

      <h4>Q: Can I export data?</h4>
      <p>Many data tables have CSV and JSON export buttons in the tile header.</p>

      <h4>Q: How often does the dashboard refresh?</h4>
      <p>Every 30 seconds by default for views that show live data; change it under Settings, Auto-refresh (10 s, 30 s, 1 min, 5 min or Off). The timer pauses while the tab is hidden and never starts a refresh while the previous one is still running. Forms and settings pages are not refreshed underneath you.</p>
    `)}

    ${_section('where-to-get-help', 'Where to Get Help', `
      <ul>
        <li><strong>In-app:</strong> hover the <code>?</code> icon on any tile, or stay in this Help view for the full guide library.</li>
        <li><strong>Step-by-step guides:</strong> see the Step-by-Step Guides section above, or browse <code>docs/guides/</code> on GitHub.</li>
        <li><strong>Master reference:</strong> <code>docs/USER_GUIDE.md</code> in the Modus repo.</li>
        <li><strong>GitHub issues:</strong> report bugs or request features at github.com/babbguy/Modus/issues.</li>
      </ul>
    `)}
  `;
}

function _section(anchor, title, html) {
  return `
    <section data-help-anchor="${esc(anchor)}" style="margin-bottom:36px;scroll-margin-top:20px">
      <h2 style="font-size:20px;font-weight:700;color:var(--text);margin:0 0 12px;padding-bottom:8px;border-bottom:1px solid var(--border)">${esc(title)}</h2>
      <div class="help-section-body">${html}</div>
    </section>
  `;
}

function _renderGuidesSection() {
  const cards = GUIDES.map(g => `
    <article data-help-anchor="guide-${esc(g.id)}" style="scroll-margin-top:20px;margin-bottom:20px;padding:16px;border:1px solid var(--border);border-radius:8px;background:var(--surface)">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;flex-wrap:wrap;gap:8px">
        <h3 style="font-size:15px;font-weight:700;color:var(--text);margin:0">${esc(g.title)}</h3>
        <div style="display:flex;gap:8px">
          <span style="font-size:10px;padding:2px 8px;border-radius:10px;background:rgba(99,102,241,0.12);color:#6366f1;font-weight:600">${esc(g.audience)}</span>
          <span style="font-size:10px;padding:2px 8px;border-radius:10px;background:rgba(159,193,49,0.12);color:var(--accent);font-weight:600"><i class="fa-regular fa-clock" style="margin-right:3px"></i>${esc(g.time)}</span>
        </div>
      </div>
      <p style="font-size:12px;color:var(--muted);line-height:1.55;margin:0 0 10px">${esc(g.summary)}</p>
      <a href="https://github.com/babbguy/Modus/blob/main/${esc(g.githubPath)}" target="_blank" rel="noopener" style="font-size:11px;color:var(--accent);text-decoration:none;font-weight:600">
        <i class="fa-brands fa-github" style="margin-right:4px"></i>Read full guide on GitHub →
      </a>
    </article>
  `).join('');

  return `
    <section data-help-anchor="guides" style="margin-bottom:36px;scroll-margin-top:20px">
      <h2 style="font-size:20px;font-weight:700;color:var(--text);margin:0 0 8px;padding-bottom:8px;border-bottom:1px solid var(--border)">Step-by-Step Guides</h2>
      <p style="font-size:12px;color:var(--muted);margin:0 0 16px">Common workflows, end-to-end. Each guide takes 3-12 minutes and includes verification steps.</p>
      ${cards}
    </section>
  `;
}
