// ── Modus Dashboard — Auto-refresh ─────────────────────────────
// Re-fetches the current view's data on the interval chosen in
// Settings -> Auto-refresh (localStorage "modus_refresh_interval", seconds;
// "0" = off). A view takes part by exporting refresh(). The timer skips ticks
// while the tab is hidden and never starts a refresh while the previous one is
// still running.

import { get, set, subscribe } from './state.js';
import { clearCache } from './api.js';

export const STORAGE_KEY = 'modus_refresh_interval';
export const DEFAULT_INTERVAL_MS = 30_000;
export const MIN_INTERVAL_MS = 5_000;

/**
 * Convert a stored/selected value (seconds, "0" or "off") to milliseconds.
 * 0 means disabled; missing or invalid values fall back to the 30s default.
 * @param {string|number|null|undefined} raw
 * @returns {number}
 */
export function parseIntervalMs(raw) {
  if (raw === null || raw === undefined || String(raw).trim() === '') return DEFAULT_INTERVAL_MS;
  const text = String(raw).trim().toLowerCase();
  if (text === 'off' || text === '0') return 0;
  const seconds = Number(text);
  if (!Number.isFinite(seconds) || seconds < 0) return DEFAULT_INTERVAL_MS;
  if (seconds === 0) return 0;
  return Math.max(MIN_INTERVAL_MS, Math.round(seconds * 1000));
}

/** Read the saved interval (ms) from localStorage. */
export function loadIntervalMs() {
  try {
    return parseIntervalMs(localStorage.getItem(STORAGE_KEY));
  } catch (_) {
    return DEFAULT_INTERVAL_MS;
  }
}

/**
 * Build a scheduler. All environment access is injected so it is easy to test.
 * @param {Object} deps
 * @param {() => number} deps.getIntervalMs
 * @param {() => boolean} deps.isHidden
 * @param {() => (null|(() => Promise<any>))} deps.getRefresh  current view's refresh fn
 * @param {() => void} [deps.beforeRefresh]
 * @param {(fn: Function, ms: number) => any} deps.setTimer
 * @param {(handle: any) => void} deps.clearTimer
 * @param {(err: Error) => void} [deps.onError]
 */
export function createScheduler(deps) {
  let handle = null;
  let busy = false;

  async function tick() {
    if (busy || deps.isHidden()) return false;
    const refresh = deps.getRefresh();
    if (typeof refresh !== 'function') return false;
    busy = true;
    try {
      if (deps.beforeRefresh) deps.beforeRefresh();
      await refresh();
      return true;
    } catch (err) {
      if (deps.onError) deps.onError(err);
      return false;
    } finally {
      busy = false;
    }
  }

  function stop() {
    if (handle !== null) {
      deps.clearTimer(handle);
      handle = null;
    }
  }

  function start() {
    stop();
    const ms = deps.getIntervalMs();
    if (!ms || ms <= 0) return;
    handle = deps.setTimer(tick, ms);
  }

  return { start, stop, restart: start, tick, isRunning: () => handle !== null };
}

let _scheduler = null;

/**
 * Start auto-refresh. Reads the saved interval, then follows changes made via
 * setRefreshInterval() and tab visibility.
 * @param {() => (object|null)} getCurrentModule  returns the active view module
 */
export function initAutoRefresh(getCurrentModule) {
  set('autoRefreshMs', loadIntervalMs());

  _scheduler = createScheduler({
    getIntervalMs: () => get('autoRefreshMs'),
    isHidden: () => document.hidden,
    getRefresh: () => {
      const mod = getCurrentModule();
      return mod && typeof mod.refresh === 'function' ? () => mod.refresh() : null;
    },
    beforeRefresh: () => clearCache(), // bypass the 30s response cache
    setTimer: (fn, ms) => setInterval(fn, ms),
    clearTimer: (h) => clearInterval(h),
    onError: (err) => console.warn('[auto-refresh] refresh failed:', err),
  });

  subscribe('autoRefreshMs', () => _scheduler.restart());
  document.addEventListener('visibilitychange', () => {
    // Catch up immediately when the user comes back to the tab.
    if (!document.hidden && get('autoRefreshMs') > 0) _scheduler.tick();
  });
  _scheduler.start();
}

/**
 * Apply a new interval chosen in Settings (seconds as string/number, "0" = off).
 * Persists it and restarts the timer.
 */
export function setRefreshInterval(rawSeconds) {
  try {
    localStorage.setItem(STORAGE_KEY, String(rawSeconds));
  } catch (_) { /* storage unavailable: still apply for this session */ }
  set('autoRefreshMs', parseIntervalMs(rawSeconds));
}
