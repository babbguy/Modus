/**
 * Modus Dashboard — login gate (classic script, loaded before the app module).
 *
 * Flow:
 *  1. GET /version to discover auth_mode.
 *  2. auth_mode=stub  -> no sign-in at all (any stored credential is dropped).
 *  3. Otherwise a credential is required. Two kinds are accepted:
 *       - a JWT          -> sent as "Authorization: Bearer <jwt>"
 *       - the master key -> sent as "X-Modus-APIKey: mds_master_..."
 *  4. The credential is verified against a real authenticated endpoint
 *     (GET /api/v1/users/me; /health is public and proves nothing).
 *
 * The credential lives in sessionStorage only (cleared when the tab closes,
 * never written to localStorage) and is never accepted from the URL.
 * js/state.js reads the same two keys when the app module starts.
 */
(function () {
  'use strict';

  var STORAGE_KEY = 'modus_auth_token';
  var STORAGE_KEY_TYPE = 'modus_auth_type'; // 'master' | 'bearer'
  var VERIFY_PATH = '/api/v1/users/me';

  // ── storage ────────────────────────────────────────────────────────────────
  function readCredential() {
    try {
      var token = sessionStorage.getItem(STORAGE_KEY);
      var type = sessionStorage.getItem(STORAGE_KEY_TYPE);
      if (token && (type === 'master' || type === 'bearer')) return { token: token, type: type };
    } catch (e) { /* storage unavailable */ }
    return null;
  }

  function storeCredential(token, type) {
    try {
      sessionStorage.setItem(STORAGE_KEY, token);
      sessionStorage.setItem(STORAGE_KEY_TYPE, type);
    } catch (e) { /* storage unavailable: the session just will not survive a reload */ }
  }

  function clearCredential() {
    try {
      sessionStorage.removeItem(STORAGE_KEY);
      sessionStorage.removeItem(STORAGE_KEY_TYPE);
    } catch (e) { /* ignore */ }
  }

  // Earlier versions persisted credentials in localStorage; remove any leftover.
  try {
    localStorage.removeItem(STORAGE_KEY);
    localStorage.removeItem(STORAGE_KEY_TYPE);
  } catch (e) { /* ignore */ }

  // ── credential handling ───────────────────────────────────────────────────
  /** 'master' | 'bearer' | 'app_key' | null */
  function detectCredentialType(value) {
    var v = (value || '').trim();
    if (!v) return null;
    if (v.indexOf('mds_master_') === 0) return 'master';
    if (v.indexOf('mds_') === 0) return 'app_key';
    if (v.split('.').length === 3) return 'bearer';
    return null;
  }

  function authHeadersFor(token, type) {
    var h = { 'Accept': 'application/json' };
    if (type === 'master') h['X-Modus-APIKey'] = token;
    else if (type === 'bearer') h['Authorization'] = 'Bearer ' + token;
    return h;
  }

  /** Resolves 'ok' | 'denied' | 'error'. 403 means authenticated but limited. */
  function verifyCredential(token, type) {
    return fetch(VERIFY_PATH, { headers: authHeadersFor(token, type) })
      .then(function (r) {
        if (r.status === 401) return 'denied';
        if (r.ok || r.status === 403) return 'ok';
        return 'error';
      })
      .catch(function () { return 'error'; });
  }

  // ── DOM helpers ────────────────────────────────────────────────────────────
  function byId(id) { return document.getElementById(id); }
  function showLogin() { var s = byId('login-screen'); if (s) s.style.display = 'flex'; }
  function hideLogin() { var s = byId('login-screen'); if (s) s.style.display = 'none'; }
  function setError(msg) { var el = byId('login-error'); if (el) el.textContent = msg || ''; }
  function setLoading(loading) {
    var text = byId('login-btn-text');
    var spinner = byId('login-btn-spinner');
    var btn = byId('login-btn');
    if (text) text.textContent = loading ? 'Verifying...' : 'Authenticate';
    if (spinner) spinner.style.display = loading ? 'inline-block' : 'none';
    if (btn) btn.disabled = loading;
  }

  var authMode = null;

  // ── master-key shortcut button ─────────────────────────────────────────────
  var masterBtn = byId('login-master-btn');
  if (masterBtn) {
    masterBtn.addEventListener('click', function () {
      var input = byId('login-token');
      if (input) {
        input.value = '';
        input.placeholder = 'Paste your MODUS_MASTER_API_KEY value here';
        input.focus();
      }
    });
  }

  // ── form submit ────────────────────────────────────────────────────────────
  var form = byId('login-form');
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      setError('');
      var input = byId('login-token');
      var value = (input ? input.value : '').trim();
      if (!value) { setError('Please enter the master key or a JWT.'); return; }

      var type = detectCredentialType(value);
      if (type === 'app_key') {
        setError('That is an application key. Sign in with the master key (mds_master_...) or a JWT.');
        return;
      }
      if (!type) {
        setError('Unrecognised credential. Use the master key (mds_master_...) or a JWT.');
        return;
      }

      setLoading(true);
      verifyCredential(value, type).then(function (result) {
        setLoading(false);
        if (result === 'ok') {
          storeCredential(value, type);
          // The app module reads credentials once at start-up, so restart it.
          window.location.reload();
        } else if (result === 'denied') {
          setError('Authentication failed. Check the key or token and try again.');
        } else {
          setError('Could not reach the server to verify the credential. Try again.');
        }
      });
    });
  }

  // ── session expiry: api.js raises this on any 401 ─────────────────────────
  var expiredShown = false;
  window.addEventListener('modus:unauthorized', function () {
    if (authMode === 'stub' || expiredShown) return;
    expiredShown = true;
    clearCredential();
    showLogin();
    setError('Your session has expired or the credential was rejected. Sign in again.');
  });

  // Resolves once requests can be authenticated (stub mode, or a stored
  // credential that verified). The app waits on it before loading data, so
  // nothing is requested from behind the sign-in screen.
  var resolveReady;
  var ready = new Promise(function (resolve) { resolveReady = resolve; });

  // ── boot ───────────────────────────────────────────────────────────────────
  fetch('/version')
    .then(function (r) { return r.ok ? r.json() : null; })
    .catch(function () { return null; })
    .then(function (data) {
      authMode = data && data.auth_mode;
      if (authMode === 'stub') {
        clearCredential();
        hideLogin();
        resolveReady();
        return;
      }

      var stored = readCredential();
      if (!stored) { showLogin(); return; }
      verifyCredential(stored.token, stored.type).then(function (result) {
        if (result === 'ok') {
          hideLogin();
          resolveReady();
        } else if (result === 'denied') {
          clearCredential();
          showLogin();
          setError('Your session has expired. Sign in again.');
        } else {
          // Server unreachable: keep the credential, let the user retry.
          showLogin();
          setError('Could not reach the server to verify your session.');
        }
      });
    });

  // Exposed for tests and for a future sign-out control.
  window.ModusLogin = {
    ready: ready,
    detectCredentialType: detectCredentialType,
    authHeadersFor: authHeadersFor,
    signOut: function () { clearCredential(); window.location.reload(); },
  };
})();
