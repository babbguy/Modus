// ── Modus Dashboard v2 — Central State Store ───────────────────
// Pure pub/sub state management. No imports (root module).

// Credential saved by the login gate (js/login-gate.js). sessionStorage only,
// so it disappears when the tab closes. type is 'master' or 'bearer'.
function _storedCredential() {
  try {
    const token = sessionStorage.getItem('modus_auth_token');
    const type = sessionStorage.getItem('modus_auth_type');
    if (token && (type === 'master' || type === 'bearer')) return { token, type };
  } catch (_) { /* storage unavailable */ }
  return { token: '', type: '' };
}
const _cred = _storedCredential();

const _state = {
  apiBase:           window.MODUS_API_BASE || '',
  // Master key, sent as X-Modus-APIKey (a window global overrides, for embedding).
  apiKey:            window.MODUS_API_KEY  || (_cred.type === 'master' ? _cred.token : ''),
  appId:             window.MODUS_APP_ID   || '',
  autoRefreshMs:     30_000,   // replaced at start-up by js/auto-refresh.js
  currentDays:       7,
  currentPeriodMode: 7,        // 'live' | number
  currentViewMode:   'overview', // 'overview' | 'devops' | 'executive' | …
  isDemoMode:        false,
  // JWT, sent as Authorization: Bearer.
  authToken:         window.MODUS_AUTH_TOKEN || (_cred.type === 'bearer' ? _cred.token : null),
};

const _subs = {};

/**
 * Get a state value by key.
 * @param {string} key
 * @returns {*}
 */
export function get(key) {
  return _state[key];
}

/**
 * Set a state value and notify subscribers if changed.
 * @param {string} key
 * @param {*} value
 */
export function set(key, value) {
  const old = _state[key];
  _state[key] = value;
  if (old !== value && _subs[key]) {
    _subs[key].forEach(fn => {
      try { fn(value, old); } catch (e) { console.error(`[state] subscriber error for "${key}":`, e); }
    });
  }
}

/**
 * Subscribe to changes on a state key.
 * @param {string} key
 * @param {function(newVal, oldVal)} cb
 */
export function subscribe(key, cb) {
  if (!_subs[key]) _subs[key] = new Set();
  _subs[key].add(cb);
}

/**
 * Unsubscribe from changes on a state key.
 * @param {string} key
 * @param {function} cb
 */
export function unsubscribe(key, cb) {
  if (_subs[key]) _subs[key].delete(cb);
}

/**
 * Batch-set multiple keys, notifying once per key.
 * @param {Object} updates
 */
export function patch(updates) {
  for (const [key, value] of Object.entries(updates)) {
    set(key, value);
  }
}

/**
 * Get a snapshot of all state (shallow copy).
 * @returns {Object}
 */
export function snapshot() {
  return { ..._state };
}
