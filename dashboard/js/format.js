// ── Modus Dashboard v2 — Pure Formatting Utilities ─────────────
// No imports. No side effects. All pure functions.

/**
 * Format a cost value for display.
 * @param {number|string} v
 * @returns {string}
 */
export const fmtCost = (v) => {
  const n = parseFloat(v);
  if (isNaN(n)) return '$0.00';
  const sign = n < 0 ? '-' : '';
  const a = Math.abs(n);
  if (a >= 10_000) return `${sign}$${(a / 1_000).toFixed(1)}k`;
  // Sub-dollar amounts keep four decimals: per-call costs are fractions of a cent.
  if (a > 0 && a < 1) return `${sign}$${a.toFixed(4)}`;
  return `${sign}$${a.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
};

/**
 * Format a token count for display.
 * @param {number} v
 * @returns {string}
 */
export const fmtTokens = (v) => {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000)     return `${(v / 1_000).toFixed(0)}K`;
  return String(v);
};

/**
 * Format a generic number for display.
 * @param {number} v
 * @returns {string}
 */
export const fmtNum = (v) => {
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000)     return `${(v / 1_000).toFixed(1)}K`;
  return String(v);
};

/**
 * Format a date string as "Mon D" (e.g. "Mar 16").
 * @param {string} s - ISO date string
 * @returns {string}
 */
export const fmtDate = (s) => {
  const d = new Date(s);
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
};

/**
 * Format a datetime string as time "3:00 PM" for hourly charts.
 * @param {string} s - ISO datetime string
 * @returns {string}
 */
export const fmtTime = (s) => {
  const d = new Date(s);
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
};

/**
 * Smart date/time format — uses time for hourly data, date for daily.
 * @param {string} s - ISO date/datetime string
 * @param {boolean} [hourly=false] - true to format as time
 * @returns {string}
 */
export const fmtDateSmart = (s, hourly = false) => {
  if (hourly) return fmtTime(s);
  return fmtDate(s);
};

/**
 * Relative time since a given date string (e.g. "5m ago").
 * @param {string} s - ISO date string
 * @returns {string}
 */
export const timeSince = (s) => {
  const diff = (Date.now() - new Date(s).getTime()) / 1000;
  if (diff < 60)    return `${Math.floor(diff)}s ago`;
  if (diff < 3600)  return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
};

/**
 * Provider brand colors.
 */
export const PROVIDER_COLORS = {
  anthropic:  '#9FC131',
  openai:     '#3b82f6',
  bedrock:    '#f59e0b',
  azure:      '#a855f7',
  gcp:        '#ff6b6b',
  databricks: '#e94a00',
};

/**
 * Get the brand color for a provider, falling back to a neutral gray.
 * @param {string} name
 * @returns {string}
 */
export function providerColor(name) {
  return PROVIDER_COLORS[(name || '').toLowerCase()] || '#5a5f78';
}
