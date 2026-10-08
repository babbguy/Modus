/**
 * Modus Dashboard — Default Tile Layouts
 * Each view defines its tiles' default positions in a 12-column grid.
 * Layout format: { id, x, y, w, h, minW, minH, [noResize], [noMove] }
 */

export const LAYOUTS = {
  overview: [
    { id: 'overview-kpis',       x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true, noMove: false },
    { id: 'overview-cost-chart', x: 0, y: 2,  w: 8,  h: 5,  minW: 4,  minH: 3 },
    { id: 'overview-providers',  x: 8, y: 2,  w: 4,  h: 5,  minW: 3,  minH: 3 },
    { id: 'overview-top-apps',   x: 0, y: 7,  w: 6,  h: 6,  minW: 4,  minH: 4 },
    { id: 'overview-top-models', x: 6, y: 7,  w: 6,  h: 6,  minW: 4,  minH: 4 },
    { id: 'overview-alerts',     x: 0, y: 13, w: 4,  h: 5,  minW: 3,  minH: 3 },
    { id: 'overview-agents',     x: 4, y: 13, w: 8,  h: 5,  minW: 4,  minH: 3 },
  ],

  executive: [
    { id: 'exec-kpis',          x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true },
    { id: 'exec-narrative',     x: 0, y: 2,  w: 12, h: 3,  minW: 6,  minH: 2 },
    { id: 'exec-forecast',      x: 0, y: 5,  w: 8,  h: 5,  minW: 4,  minH: 3 },
    { id: 'exec-savings',       x: 8, y: 5,  w: 4,  h: 5,  minW: 3,  minH: 3 },
    { id: 'exec-chargeback',    x: 0, y: 10, w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'exec-enforcement',   x: 6, y: 10, w: 6,  h: 5,  minW: 3,  minH: 3 },
    { id: 'exec-roi',           x: 0, y: 15, w: 6,  h: 5,  minW: 3,  minH: 3 },
    { id: 'exec-model-risk',    x: 6, y: 15, w: 6,  h: 5,  minW: 3,  minH: 3 },
    { id: 'exec-topology',      x: 0, y: 20, w: 12, h: 6,  minW: 6,  minH: 4 },
  ],

  finance: [
    { id: 'fin-kpis',           x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true },
    { id: 'fin-spend-trend',    x: 0, y: 2,  w: 8,  h: 5,  minW: 4,  minH: 3 },
    { id: 'fin-dept-donut',     x: 8, y: 2,  w: 4,  h: 5,  minW: 3,  minH: 3 },
    { id: 'fin-burn-rate',      x: 0, y: 7,  w: 8,  h: 6,  minW: 4,  minH: 4 },
    { id: 'fin-provider-bar',   x: 8, y: 7,  w: 4,  h: 6,  minW: 3,  minH: 3 },
    { id: 'fin-forecast',       x: 0, y: 13, w: 8,  h: 6,  minW: 4,  minH: 4 },
    { id: 'fin-breach',         x: 8, y: 13, w: 4,  h: 6,  minW: 3,  minH: 3 },
    { id: 'fin-team-gauges',    x: 0, y: 19, w: 12, h: 4,  minW: 6,  minH: 3 },
    { id: 'fin-reports',        x: 0, y: 23, w: 12, h: 4,  minW: 6,  minH: 3 },
    { id: 'fin-chargeback',     x: 0, y: 27, w: 8,  h: 6,  minW: 4,  minH: 4 },
    { id: 'fin-reconciliation', x: 8, y: 27, w: 4,  h: 6,  minW: 3,  minH: 3 },
    { id: 'fin-cost-centers',   x: 0, y: 33, w: 12, h: 6,  minW: 8,  minH: 4 },
  ],

  devops: [
    { id: 'devops-kpis',        x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true },
    { id: 'devops-anomalies',   x: 0, y: 2,  w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'devops-recommend',   x: 6, y: 2,  w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'devops-enforcement', x: 0, y: 7,  w: 4,  h: 4,  minW: 3,  minH: 3 },
    { id: 'devops-deployments', x: 4, y: 7,  w: 8,  h: 4,  minW: 4,  minH: 3 },
    { id: 'devops-providers',   x: 0, y: 11, w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'devops-agents',      x: 6, y: 11, w: 6,  h: 5,  minW: 4,  minH: 3 },
  ],

  routing: [
    { id: 'rt-kpis',            x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true },
    { id: 'rt-savings-chart',   x: 0, y: 2,  w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'rt-phase-donut',     x: 6, y: 2,  w: 6,  h: 5,  minW: 3,  minH: 3 },
    { id: 'rt-fingerprints',    x: 0, y: 7,  w: 12, h: 6,  minW: 6,  minH: 4 },
    { id: 'rt-exclusions',      x: 0, y: 13, w: 12, h: 4,  minW: 6,  minH: 3 },
  ],

  apps: [
    { id: 'apps-table',         x: 0, y: 0,  w: 12, h: 12, minW: 8,  minH: 6 },
  ],

  thresholds: [
    { id: 'thresholds-table',   x: 0, y: 0,  w: 12, h: 12, minW: 8,  minH: 6 },
  ],

  alerts: [
    { id: 'alerts-stats',       x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true },
    { id: 'alerts-table',       x: 0, y: 2,  w: 12, h: 10, minW: 8,  minH: 6 },
  ],

  pricing: [
    { id: 'pricing-overrides',  x: 0, y: 0,  w: 12, h: 5,  minW: 8,  minH: 4 },
    { id: 'pricing-global',     x: 0, y: 5,  w: 12, h: 7,  minW: 8,  minH: 5 },
    { id: 'pricing-history',    x: 0, y: 12, w: 12, h: 5,  minW: 8,  minH: 4 },
  ],

  policies: [
    { id: 'policies-table',     x: 0, y: 0,  w: 12, h: 12, minW: 8,  minH: 6 },
    { id: 'policies-decisions', x: 0, y: 12, w: 12, h: 7,  minW: 6,  minH: 4 },
  ],

  teams: [
    { id: 'teams-table',        x: 0, y: 0,  w: 12, h: 12, minW: 8,  minH: 6 },
  ],

  notifications: [
    { id: 'notif-config',       x: 0, y: 0,  w: 6,  h: 8,  minW: 4,  minH: 5 },
    { id: 'notif-subs',         x: 6, y: 0,  w: 6,  h: 8,  minW: 4,  minH: 5 },
    { id: 'notif-delivery',     x: 0, y: 8,  w: 12, h: 5,  minW: 6,  minH: 3 },
  ],

  governance: [
    { id: 'gov-status',         x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true },
    { id: 'gov-cot-ledger',     x: 0, y: 2,  w: 12, h: 8,  minW: 8,  minH: 5 },
    { id: 'gov-proposals',      x: 0, y: 10, w: 6,  h: 6,  minW: 4,  minH: 4 },
    { id: 'gov-rewind',         x: 6, y: 10, w: 6,  h: 6,  minW: 4,  minH: 4 },
    { id: 'gov-evolution',      x: 0, y: 16, w: 12, h: 5,  minW: 6,  minH: 3 },
  ],

  federation: [
    { id: 'fed-kpis',    x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true },
    { id: 'fed-peers',   x: 0, y: 2,  w: 12, h: 8,  minW: 8,  minH: 5 },
    { id: 'fed-history', x: 0, y: 10, w: 12, h: 5,  minW: 8,  minH: 4 },
  ],

  compliance: [
    { id: 'comp-attestation',   x: 0, y: 0,  w: 6,  h: 6,  minW: 4,  minH: 4 },
    { id: 'comp-pqc',           x: 6, y: 0,  w: 6,  h: 6,  minW: 4,  minH: 4 },
  ],

  sentinel: [
    { id: 'sentinel-kpis',      x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true },
    { id: 'sentinel-threats',   x: 0, y: 2,  w: 12, h: 6,  minW: 6,  minH: 4 },
    { id: 'sentinel-zk',        x: 0, y: 8,  w: 6,  h: 4,  minW: 3,  minH: 3 },
    { id: 'sentinel-pqc',       x: 6, y: 8,  w: 6,  h: 4,  minW: 3,  minH: 3 },
  ],

  settings: [
    { id: 'settings-form',      x: 0, y: 0,  w: 12, h: 14, minW: 8, minH: 8, noResize: true, noMove: true },
  ],

  connections: [
    { id: 'conn-summary',       x: 0, y: 0,  w: 12, h: 2,  minW: 12, minH: 2, noResize: true },
    { id: 'conn-platform',      x: 0, y: 2,  w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'conn-ai',            x: 6, y: 2,  w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'conn-notif',         x: 0, y: 7,  w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'conn-integrations',  x: 6, y: 7,  w: 6,  h: 5,  minW: 4,  minH: 3 },
  ],

  admin: [
    { id: 'admin-panel',        x: 0, y: 0,  w: 12, h: 14, minW: 8, minH: 8, noResize: true, noMove: true },
  ],

  sessions: [
    { id: 'sessions-list',         x: 0, y: 0,  w: 12, h: 6,  minW: 8,  minH: 4 },
    { id: 'sessions-amplification', x: 0, y: 6,  w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'sessions-retry-tax',     x: 6, y: 6,  w: 6,  h: 5,  minW: 4,  minH: 3 },
    { id: 'sessions-defensive',     x: 0, y: 11, w: 12, h: 5,  minW: 8,  minH: 3 },
  ],
};
