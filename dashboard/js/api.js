// ── Modus Dashboard v2 — HTTP Client ───────────────────────────
// Fetch wrapper with timeout, auth headers, HTML escaping,
// in-memory TTL cache, stale-while-revalidate, request dedupe,
// and exponential-backoff retry on transient failures.

import { get } from './state.js';

// ── In-memory cache ──────────────────────────────────────────────
const _cache = new Map();    // key (url string) -> { data, timestamp }
const _inflight = new Map(); // key -> Promise (dedupe concurrent identical requests)

const DEFAULT_TTL_MS = 30_000;            // 30s for dashboard data
export const CONFIG_TTL_MS = 5 * 60_000;  // 5min for config data
const STALE_MAX_MS = 5 * 60_000;          // never serve cache older than 5min as stale

function _now() { return Date.now(); }

function _getCached(key, ttlMs) {
  const entry = _cache.get(key);
  if (!entry) return null;
  const age = _now() - entry.timestamp;
  return { data: entry.data, age, fresh: age <= ttlMs, stale: age > ttlMs };
}

function _setCached(key, data) {
  _cache.set(key, { data, timestamp: _now() });
}

/**
 * Invalidate cached entries whose key contains the given path substring.
 * Called automatically on mutations (POST/PUT/DELETE/PATCH) inside rawFetch.
 * @param {string} pathFragment
 */
export function invalidateCache(pathFragment) {
  if (!pathFragment) return;
  for (const key of Array.from(_cache.keys())) {
    if (key.includes(pathFragment)) _cache.delete(key);
  }
}

/**
 * Clear the entire response cache. Useful for "Refresh All" or auth changes.
 */
export function clearCache() {
  _cache.clear();
  _inflight.clear();
}

/**
 * Build request headers including auth tokens.
 * @returns {Object}
 */
export function headers() {
  return { 'Content-Type': 'application/json', ...authHeaders() };
}

/**
 * Credential headers only (no Content-Type) -- for multipart uploads and
 * other requests that set their own content type.
 *   master key -> X-Modus-APIKey: mds_master_...
 *   JWT        -> Authorization: Bearer <jwt>
 * In stub auth mode neither is set and the server needs nothing.
 * @returns {Object}
 */
export function authHeaders() {
  const h = {};
  const apiKey    = get('apiKey');
  const appId     = get('appId');
  const authToken = get('authToken');
  if (apiKey)    h['X-Modus-APIKey'] = apiKey;
  if (appId)     h['X-App-ID']       = appId;
  if (authToken) h['Authorization']  = `Bearer ${authToken}`;
  return h;
}

/** Tell the login gate that the server rejected our credential (HTTP 401). */
function _notifyIfUnauthorized(resp) {
  if (resp && resp.status === 401 && typeof window !== 'undefined') {
    window.dispatchEvent(new Event('modus:unauthorized'));
  }
  return resp;
}

// ── App display names ───────────────────────────────────────────
// Several endpoints (rewind events, routing fingerprints, alerts, sessions)
// identify an app by its internal UUID. Views resolve that to the app's name
// from GET /apps so tables never show a bare UUID.
const _appNames = new Map();

/** Load (or refresh) the UUID -> app name map. Never throws. */
export async function loadAppNames() {
  try {
    const resp = await rawFetch('/api/v1/apps?active_only=false&limit=500');
    if (!resp.ok) return;
    for (const a of await resp.json()) _appNames.set(a.id, a.app_name || a.app_id);
  } catch (_) { /* names are cosmetic; fall back to the id */ }
}

/** Display name for an app UUID (the id itself when unknown, '' when absent). */
export function appName(uuid) {
  return (uuid && _appNames.get(uuid)) || uuid || '';
}

/**
 * Convenience accessor for the API base URL.
 * @returns {string}
 */
export function apiBase() {
  return get('apiBase');
}

// ── Fetch with retry + timeout ──────────────────────────────────
async function _fetchWithRetry(url, opts, timeoutMs, maxRetries = 3) {
  let lastErr;
  for (let attempt = 0; attempt < maxRetries; attempt++) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const resp = await fetch(url, { ...opts, signal: controller.signal });
      clearTimeout(timer);
      if ((resp.status === 429 || resp.status >= 500) && attempt < maxRetries - 1) {
        const delay = Math.min(200 * Math.pow(2, attempt), 2000);
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
      return resp;
    } catch (err) {
      clearTimeout(timer);
      lastErr = err;
      if (err.name === 'AbortError') {
        if (attempt < maxRetries - 1) {
          const delay = Math.min(200 * Math.pow(2, attempt), 2000);
          await new Promise(r => setTimeout(r, delay));
          continue;
        }
        throw new Error('Request timed out');
      }
      if (attempt < maxRetries - 1) {
        const delay = Math.min(200 * Math.pow(2, attempt), 2000);
        await new Promise(r => setTimeout(r, delay));
        continue;
      }
    }
  }
  throw lastErr || new Error('Request failed after retries');
}

/**
 * Fetch a dashboard API endpoint with cache, SWR, retry, and timeout.
 *
 * @param {string} path        - Relative path under /api/v1/dashboard/
 * @param {Object} [params={}] - Query-string key/value pairs
 * @param {Object} [opts]
 * @param {number} [opts.timeoutMs=15000]
 * @param {number} [opts.ttlMs=30000]
 * @param {boolean} [opts.bypassCache=false]
 * @returns {Promise<any>}
 */
export async function apiFetch(path, params = {}, { timeoutMs = 15000, ttlMs = DEFAULT_TTL_MS, bypassCache = false } = {}) {
  const base = get('apiBase');
  const url  = new URL(`${base}/api/v1/dashboard/${path}`, window.location.origin);
  Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  const urlStr = url.toString();
  const key = urlStr;

  if (!bypassCache) {
    const cached = _getCached(key, ttlMs);
    if (cached && cached.fresh) {
      return cached.data;
    }
    if (cached && cached.stale && cached.age <= STALE_MAX_MS) {
      // Stale-while-revalidate: trigger background refresh, return stale
      if (!_inflight.has(key)) {
        const refresh = _fetchWithRetry(urlStr, { headers: headers() }, timeoutMs)
          .then(async resp => {
            if (resp && resp.ok) {
              const data = normalizeNumbers(await resp.json());
              _setCached(key, data);
              return data;
            }
            return cached.data;
          })
          .catch(() => cached.data)
          .finally(() => _inflight.delete(key));
        _inflight.set(key, refresh);
      }
      return cached.data;
    }
  }

  // Dedupe concurrent identical requests
  if (_inflight.has(key)) {
    return _inflight.get(key);
  }

  const promise = (async () => {
    try {
      const resp = await _fetchWithRetry(urlStr, { headers: headers() }, timeoutMs);
      _notifyIfUnauthorized(resp);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = normalizeNumbers(await resp.json());
      _setCached(key, data);
      return data;
    } finally {
      _inflight.delete(key);
    }
  })();

  _inflight.set(key, promise);
  return promise;
}

/**
 * Fetch any API path (not scoped to /dashboard/).
 * Auto-invalidates cache entries for the given path on mutation methods.
 *
 * @param {string}  path        - Full path, e.g. '/api/v1/users/me'
 * @param {Object}  [options]   - Standard fetch options (method, body, etc.)
 * @param {number}  [timeoutMs=15000]
 * @returns {Promise<Response>}
 */
export async function rawFetch(path, options = {}, timeoutMs = 15000) {
  const base = get('apiBase');
  const url  = new URL(`${base}${path}`, window.location.origin);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const resp = await fetch(url.toString(), {
      headers: headers(),
      signal:  controller.signal,
      ...options,
    });

    _notifyIfUnauthorized(resp);
    withNormalizedJson(resp);

    // Invalidate cache on mutations
    const method = ((options && options.method) || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD') {
      const stem = path.split('?')[0].replace(/\/[^/]*$/, '');
      invalidateCache(stem || path);
    }

    return resp;
  } catch (err) {
    if (err.name === 'AbortError') throw new Error('Request timed out');
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * The API sends money, percentages and other decimals as strings so no
 * precision is lost in transit (e.g. "597.20"). The dashboard only displays
 * and charts them, so convert plain decimal strings to numbers when a
 * response is parsed; otherwise `.toFixed()` throws and `+` concatenates.
 * Keys that name identifiers or labels are left alone, and so are values
 * with leading zeros (e.g. a cost-center code "0042").
 */
// Key segments (split on "_" and camelCase) that mark identifiers and labels.
const NON_NUMERIC_SEGMENTS = new Set([
  'id', 'ids', 'uuid', 'code', 'version', 'phone', 'zip', 'postal', 'sku', 'hash',
  'prefix', 'token', 'secret', 'key', 'name', 'label', 'title', 'slug', 'number', 'sha',
]);

function isNonNumericKey(key) {
  return key
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .some((segment) => NON_NUMERIC_SEGMENTS.has(segment));
}
const DECIMAL_STRING = /^-?(0|[1-9]\d*)(\.\d+)?([eE][-+]?\d+)?$/;

export function normalizeNumbers(value, key = '') {
  if (Array.isArray(value)) return value.map((v) => normalizeNumbers(v, key));
  if (value !== null && typeof value === 'object') {
    for (const k of Object.keys(value)) value[k] = normalizeNumbers(value[k], k);
    return value;
  }
  if (typeof value === 'string' && !isNonNumericKey(key) && DECIMAL_STRING.test(value)) {
    const n = Number(value);
    return Number.isFinite(n) ? n : value;
  }
  return value;
}

/** Make resp.json() return normalized numbers. */
function withNormalizedJson(resp) {
  const json = resp.json.bind(resp);
  resp.json = async () => normalizeNumbers(await json());
  return resp;
}

/**
 * HTML-escape a string for safe interpolation into markup.
 * @param {string} str
 * @returns {string}
 */
export function esc(str) {
  const d = document.createElement('div');
  d.textContent = str ?? '';
  return d.innerHTML.replace(/'/g, '&#39;');
}

/**
 * JSON POST helper for dashboard endpoints. Invalidates the dashboard
 * cache stem after a successful POST so subsequent reads see fresh data.
 *
 * @param {string} path      - Relative path under /api/v1/dashboard/
 * @param {Object} body      - JSON body
 * @param {Object} [opts]
 * @param {number} [opts.timeoutMs=15000]
 * @returns {Promise<any>}
 */
export async function apiPost(path, body, { timeoutMs = 15000 } = {}) {
  const base = get('apiBase');
  const url  = new URL(`${base}/api/v1/dashboard/${path}`, window.location.origin);

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  try {
    const resp = await fetch(url.toString(), {
      method:  'POST',
      headers: headers(),
      body:    JSON.stringify(body),
      signal:  controller.signal,
    });
    _notifyIfUnauthorized(resp);
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = normalizeNumbers(await resp.json());
    const stem = `/api/v1/dashboard/${path.split('?')[0].split('/')[0]}`;
    invalidateCache(stem);
    return data;
  } catch (err) {
    if (err.name === 'AbortError') throw new Error('Request timed out');
    throw err;
  } finally {
    clearTimeout(timer);
  }
}
