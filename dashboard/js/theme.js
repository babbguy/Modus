// ── Modus Dashboard v2 — Theme Toggle ──────────────────────────
// Manages dark/light theme with localStorage persistence and optional API sync.

import { headers, apiBase } from './api.js';

/**
 * Apply a theme ('dark' or 'light') to the document.
 * @param {string} theme
 */
export function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const track = document.getElementById('theme-toggle');
  if (track) {
    if (theme === 'dark') {
      track.classList.add('active');
    } else {
      track.classList.remove('active');
    }
  }
}

/**
 * Toggle between dark and light themes.
 * Persists to localStorage and optionally syncs to the API.
 */
export function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'dark';
  const next = current === 'dark' ? 'light' : 'dark';
  localStorage.setItem('modus_theme', next);
  applyTheme(next);

  // Best-effort save to API
  const base = apiBase();
  fetch(`${base}/api/v1/users/me/preferences`, {
    method:  'PUT',
    headers: { ...headers(), 'Content-Type': 'application/json' },
    body:    JSON.stringify({ theme: next }),
  }).catch(() => {}); // Silently ignore — localStorage is the source of truth
}

/**
 * Initialize theme from localStorage (defaults to 'dark')
 * and attach click handler to the toggle switch.
 */
export function initTheme() {
  const saved = localStorage.getItem('modus_theme') || 'dark';
  applyTheme(saved);

  // Bind click handler to the toggle track
  const track = document.getElementById('theme-toggle');
  if (track) {
    track.addEventListener('click', toggleTheme);
  }
  // Also support clicking the entire row (sun/moon icons)
  const row = document.getElementById('theme-toggle-row');
  if (row && row !== track) {
    row.addEventListener('click', (e) => {
      // Avoid double-fire if they clicked the track itself
      if (e.target.closest('#theme-toggle')) return;
      toggleTheme();
    });
  }
}
