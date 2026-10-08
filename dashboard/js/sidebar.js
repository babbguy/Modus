// ── Modus Dashboard v2 — Sidebar Behavior ──────────────────────
// Handles sidebar collapse/expand and mobile drawer.

/**
 * Initialize sidebar state from localStorage.
 */
export function initSidebar() {
  const collapsed = localStorage.getItem('modus_sidebar_collapsed') === 'true';
  if (collapsed) collapseSidebar(true);

  // Wire up collapse button
  const collapseBtn = document.getElementById('sidebar-collapse-btn');
  if (collapseBtn) collapseBtn.addEventListener('click', toggleSidebar);

  // Wire up mobile menu button
  const mobileBtn = document.getElementById('mobile-menu-btn');
  if (mobileBtn) mobileBtn.addEventListener('click', openMobileSidebar);

  // Wire up mobile overlay close
  const overlay = document.getElementById('mobile-overlay');
  if (overlay) overlay.addEventListener('click', closeMobileSidebar);
}

/**
 * Toggle sidebar between collapsed and expanded.
 */
export function toggleSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const isCollapsed = sidebar.classList.contains('collapsed');
  collapseSidebar(!isCollapsed);
}

/**
 * Set sidebar collapsed state.
 * @param {boolean} collapsed
 */
export function collapseSidebar(collapsed) {
  const sidebar = document.getElementById('sidebar');
  const icon    = document.getElementById('sidebar-collapse-icon');
  if (!sidebar) return;

  if (collapsed) {
    sidebar.classList.add('collapsed');
    if (icon) icon.textContent = '\u203A'; // ›
  } else {
    sidebar.classList.remove('collapsed');
    if (icon) icon.textContent = '\u2039'; // ‹
  }
  localStorage.setItem('modus_sidebar_collapsed', String(collapsed));
}

/**
 * Open the sidebar as a mobile drawer overlay.
 */
export function openMobileSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('mobile-overlay');
  if (sidebar) sidebar.classList.add('mobile-open');
  if (overlay) overlay.classList.add('visible');
  document.body.style.overflow = 'hidden';
}

/**
 * Close the mobile sidebar drawer.
 */
export function closeMobileSidebar() {
  const sidebar = document.getElementById('sidebar');
  const overlay = document.getElementById('mobile-overlay');
  if (sidebar) sidebar.classList.remove('mobile-open');
  if (overlay) overlay.classList.remove('visible');
  document.body.style.overflow = '';
}

// Expose to global scope for inline onclick handlers in HTML
if (typeof window !== 'undefined') {
  window.openMobileSidebar  = openMobileSidebar;
  window.closeMobileSidebar = closeMobileSidebar;
  window.toggleSidebar      = toggleSidebar;
}
