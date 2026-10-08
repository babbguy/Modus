// ── Modus Dashboard — Topology View ─────────────────────────────
// LangFlow-style visual architecture map per application.
// Renders card nodes with headers, connection ports, and bezier edges.

import { rawFetch, esc }     from '../api.js';
import { fmtDate }           from '../format.js';

// ── Node type colors ──────────────────────────────────────────────

const TYPE_STYLE = {
  app:          { bg: '#1e293b', border: '#3b82f6', header: '#1e40af', text: '#f8fafc', icon: '\u2B22' },
  ai_provider:  { bg: '#2d1b4e', border: '#a855f7', header: '#7c3aed', text: '#f3e8ff', icon: '\uD83E\uDD16' },
  ai_framework: { bg: '#312e81', border: '#8b5cf6', header: '#6d28d9', text: '#ede9fe', icon: '\uD83E\uDDE0' },
  database:     { bg: '#172554', border: '#3b82f6', header: '#1d4ed8', text: '#dbeafe', icon: '\uD83D\uDDC4' },
  cache:        { bg: '#064e3b', border: '#10b981', header: '#059669', text: '#d1fae5', icon: '\u26A1' },
  queue:        { bg: '#431407', border: '#f97316', header: '#ea580c', text: '#ffedd5', icon: '\uD83D\uDCE8' },
  storage:      { bg: '#422006', border: '#eab308', header: '#ca8a04', text: '#fef9c3', icon: '\uD83D\uDCC1' },
};

const SERVICE_TYPE_MAP = {
  postgres: 'database', mysql: 'database', mongodb: 'database',
  redis: 'cache', rabbitmq: 'queue', kafka: 'queue', sqs: 'queue',
  s3: 'storage', gcs: 'storage', elasticsearch: 'database',
};

const CLOUD_LABELS = {
  aws: 'AWS', gcp: 'GCP', azure: 'Azure', 'on-prem': 'On-Prem',
};

const DEPLOY_LABELS = {
  kubernetes: 'Kubernetes', 'docker-compose': 'Docker Compose',
  ecs: 'ECS', lambda: 'Lambda', docker: 'Docker',
};

// ── Build graph nodes ──────────────────────────────────────────────

function buildNodes(topo) {
  const nodes = [];
  const edges = [];

  // Central app node
  nodes.push({
    id: 'app', type: 'app',
    title: topo.app_name || topo.app_id,
    fields: [
      { label: 'Framework', value: topo.web_framework || 'Unknown' },
      { label: 'Deploy', value: DEPLOY_LABELS[topo.deployment_type] || topo.deployment_type || 'Unknown' },
      { label: 'Cloud', value: CLOUD_LABELS[topo.cloud_provider] || topo.cloud_provider || 'Unknown' },
    ],
  });

  // AI providers
  for (const [name, version] of Object.entries(topo.ai_providers || {})) {
    const id = `ai-${name}`;
    nodes.push({ id, type: 'ai_provider', title: name.charAt(0).toUpperCase() + name.slice(1), fields: [{ label: 'Version', value: `v${version}` }] });
    edges.push({ from: 'app', to: id });
  }

  // AI frameworks
  for (const [name, version] of Object.entries(topo.ai_frameworks || {})) {
    const id = `fw-${name}`;
    nodes.push({ id, type: 'ai_framework', title: name.charAt(0).toUpperCase() + name.slice(1), fields: [{ label: 'Version', value: `v${version}` }] });
    edges.push({ from: 'app', to: id });
  }

  // Service dependencies
  for (const [service, connStr] of Object.entries(topo.service_dependencies || {})) {
    const id = `svc-${service}`;
    const type = SERVICE_TYPE_MAP[service] || 'database';
    let host = service;
    try {
      const m = connStr.match(/:\/\/([^:/]+)/);
      if (m) host = m[1];
    } catch (_) {}
    nodes.push({ id, type, title: service.charAt(0).toUpperCase() + service.slice(1), fields: [{ label: 'Host', value: host }] });
    edges.push({ from: 'app', to: id });
  }

  return { nodes, edges };
}

// ── SVG Card Rendering ──────────────────────────────────────────────

function renderTopologyGraph(container, topo) {
  const { nodes, edges } = buildNodes(topo);
  if (nodes.length <= 1) {
    container.innerHTML = '<p style="color:var(--muted);text-align:center;padding:2rem">No topology data available.</p>';
    return;
  }

  const CARD_W = 200;
  const CARD_H = 90;
  const HEADER_H = 28;
  const PAD = 40;
  const width = container.clientWidth || 800;

  // Layout: app node on left-center, others in a column on the right
  const orbitNodes = nodes.filter(n => n.id !== 'app');
  const totalH = Math.max(400, orbitNodes.length * (CARD_H + PAD));
  const appX = PAD + 20;
  const appY = totalH / 2 - CARD_H / 2;
  const orbitX = width - CARD_W - PAD - 20;
  const orbitStartY = (totalH - orbitNodes.length * (CARD_H + 16)) / 2;

  const positions = {};
  positions['app'] = { x: appX, y: appY };
  orbitNodes.forEach((node, i) => {
    positions[node.id] = { x: orbitX, y: orbitStartY + i * (CARD_H + 16) };
  });

  const svg = [`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${width} ${totalH}" style="width:100%;height:${totalH}px;font-family:system-ui,-apple-system,sans-serif">`];

  // Bezier edges
  for (const edge of edges) {
    const from = positions[edge.from];
    const to = positions[edge.to];
    if (!from || !to) continue;
    const x1 = from.x + CARD_W;
    const y1 = from.y + CARD_H / 2;
    const x2 = to.x;
    const y2 = to.y + CARD_H / 2;
    const cx1 = x1 + (x2 - x1) * 0.5;
    const cx2 = x2 - (x2 - x1) * 0.5;

    // Port dots
    svg.push(`<circle cx="${x1}" cy="${y1}" r="4" fill="#3b82f6" opacity="0.8"/>`);
    svg.push(`<circle cx="${x2}" cy="${y2}" r="4" fill="${TYPE_STYLE[nodes.find(n => n.id === edge.to)?.type]?.border || '#64748b'}" opacity="0.8"/>`);

    // Bezier curve
    svg.push(`<path d="M${x1},${y1} C${cx1},${y1} ${cx2},${y2} ${x2},${y2}" fill="none" stroke="#475569" stroke-width="1.5" opacity="0.5"/>`);
  }

  // Card nodes
  for (const node of nodes) {
    const pos = positions[node.id];
    if (!pos) continue;
    const s = TYPE_STYLE[node.type] || TYPE_STYLE.database;
    const x = pos.x;
    const y = pos.y;

    // Card background
    svg.push(`<rect x="${x}" y="${y}" width="${CARD_W}" height="${CARD_H}" rx="8" fill="${s.bg}" stroke="${s.border}" stroke-width="1.5"/>`);

    // Header bar
    svg.push(`<rect x="${x}" y="${y}" width="${CARD_W}" height="${HEADER_H}" rx="8" fill="${s.header}"/>`);
    svg.push(`<rect x="${x}" y="${y + HEADER_H - 4}" width="${CARD_W}" height="4" fill="${s.header}"/>`);

    // Title
    svg.push(`<text x="${x + 12}" y="${y + 18}" fill="${s.text}" font-size="12" font-weight="700">${esc(node.title)}</text>`);

    // Fields
    node.fields.forEach((f, fi) => {
      const fy = y + HEADER_H + 14 + fi * 16;
      svg.push(`<text x="${x + 12}" y="${fy}" fill="${s.text}" font-size="10" opacity="0.6">${esc(f.label)}</text>`);
      svg.push(`<text x="${x + CARD_W - 12}" y="${fy}" fill="${s.text}" font-size="10" font-weight="500" text-anchor="end">${esc(f.value)}</text>`);
    });
  }

  // AI endpoints badge
  const routes = topo.api_routes || [];
  const aiRoutes = routes.filter(r => r.likely_ai_endpoint);
  if (aiRoutes.length > 0) {
    const badgeY = totalH - 40;
    svg.push(`<text x="16" y="${badgeY}" fill="#a855f7" font-size="11" font-weight="600">AI Endpoints (${aiRoutes.length})</text>`);
    aiRoutes.slice(0, 5).forEach((r, i) => {
      svg.push(`<text x="16" y="${badgeY + 16 + i * 14}" fill="#94a3b8" font-size="10">${esc((r.methods || []).join(','))} ${esc(r.path)}</text>`);
    });
  }

  svg.push('</svg>');
  container.innerHTML = svg.join('\n');
}

// ── App Card ──────────────────────────────────────────────────────

function renderAppCard(topo) {
  const providers = Object.keys(topo.ai_providers || {});
  const deps = Object.keys(topo.service_dependencies || {});
  const routes = topo.api_routes || [];
  const aiRoutes = routes.filter(r => r.likely_ai_endpoint);

  const cloudColor = topo.cloud_provider === 'aws' ? '#f97316' : topo.cloud_provider === 'gcp' ? '#3b82f6' : topo.cloud_provider === 'azure' ? '#06b6d4' : '#64748b';

  return `
    <div class="topo-card" data-app-id="${esc(topo.app_id)}" style="
      background:var(--surface);border:1px solid var(--border);
      border-radius:8px;padding:14px;cursor:pointer;transition:border-color 0.2s;
    ">
      <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:8px">
        <div style="font-weight:600;font-size:13px">${esc(topo.app_name || topo.app_id)}</div>
        <span style="font-size:10px;padding:2px 8px;border-radius:4px;background:${cloudColor};color:#fff;font-weight:600">${esc(DEPLOY_LABELS[topo.deployment_type] || topo.deployment_type || '?')}</span>
      </div>
      <div style="font-size:11px;color:var(--muted);margin-bottom:10px;line-height:1.5">
        ${topo.ai_summary ? esc(topo.ai_summary.substring(0, 100)) + (topo.ai_summary.length > 100 ? '...' : '') : 'No summary'}
      </div>
      <div style="display:flex;gap:12px;font-size:11px;color:var(--muted)">
        <span><i class="fa-solid fa-robot" style="color:#a855f7;margin-right:3px"></i>${providers.length}</span>
        <span><i class="fa-solid fa-database" style="color:#3b82f6;margin-right:3px"></i>${deps.length}</span>
        <span><i class="fa-solid fa-route" style="color:#f59e0b;margin-right:3px"></i>${aiRoutes.length} AI</span>
      </div>
    </div>
  `;
}

// ── Main view ──────────────────────────────────────────────────────

let _selectedApp = null;

export async function render(container) {
  container.innerHTML = '<p style="color:var(--muted);padding:1rem">Loading topology...</p>';

  let topologies = [];
  try {
    const resp = await rawFetch('/api/v1/topology');
    if (resp && resp.ok) topologies = await resp.json();
  } catch (e) {
    container.innerHTML = '<p style="color:var(--muted);padding:1rem">Failed to load topology data.</p>';
    return;
  }

  if (!topologies || topologies.length === 0) {
    container.innerHTML = `
      <div style="text-align:center;padding:3rem;color:var(--muted)">
        <i class="fa-solid fa-diagram-project" style="font-size:48px;opacity:0.3;margin-bottom:1rem;display:block"></i>
        <div style="font-size:16px;font-weight:600;margin-bottom:0.5rem">No Topology Data Yet</div>
        <div style="font-size:13px">Install the Modus SDK in an application to see its architecture map here.</div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div style="display:flex;gap:1.5rem;height:100%">
      <div id="topo-list" style="flex:0 0 300px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;padding-right:8px">
        <div style="position:sticky;top:0;background:var(--bg);padding-bottom:8px;z-index:1">
          <input type="text" class="ds-input" id="topo-search" placeholder="Search apps..." style="width:100%;font-size:12px;padding:6px 10px" />
        </div>
        <div style="font-size:11px;color:var(--muted);font-weight:600;margin-bottom:2px">
          ${topologies.length} Application${topologies.length !== 1 ? 's' : ''} Discovered
        </div>
        ${topologies.map(t => renderAppCard(t)).join('')}
      </div>
      <div id="topo-detail" style="flex:1;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:1rem;overflow:auto;min-height:400px">
        <div style="text-align:center;padding:3rem;color:var(--muted)">
          <i class="fa-solid fa-diagram-project" style="font-size:32px;opacity:0.3;display:block;margin-bottom:8px"></i>
          <div style="font-size:13px">Select an application to view its architecture map</div>
        </div>
      </div>
    </div>
  `;

  // Search
  const searchInput = document.getElementById('topo-search');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      const q = searchInput.value.toLowerCase();
      container.querySelectorAll('.topo-card').forEach(card => {
        card.style.display = card.textContent.toLowerCase().includes(q) ? '' : 'none';
      });
    });
  }

  // Card click handlers
  const cards = container.querySelectorAll('.topo-card');
  cards.forEach(card => {
    card.addEventListener('click', () => {
      const appId = card.dataset.appId;
      const topo = topologies.find(t => t.app_id === appId);
      if (!topo) return;

      cards.forEach(c => c.style.borderColor = 'var(--border)');
      card.style.borderColor = '#3b82f6';
      _selectedApp = appId;

      const detail = document.getElementById('topo-detail');
      detail.innerHTML = `
        <div style="margin-bottom:1rem">
          <div style="font-size:16px;font-weight:600">${esc(topo.app_name || topo.app_id)}</div>
          <div style="font-size:12px;color:var(--muted);margin-top:4px;line-height:1.6">
            ${topo.ai_summary ? esc(topo.ai_summary) : 'No AI summary available'}
          </div>
        </div>
        <div id="topo-graph"></div>
      `;

      renderTopologyGraph(document.getElementById('topo-graph'), topo);
    });
  });

  // Auto-select first
  if (cards.length > 0) cards[0].click();
}

export function destroy() {
  _selectedApp = null;
}
