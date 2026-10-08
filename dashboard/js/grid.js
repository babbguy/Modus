/**
 * Modus Dashboard — Grid Manager
 * Uses CSS Grid for consistent, predictable tile layouts.
 * Layout persistence via localStorage for tile visibility/order.
 */

let _currentView = null;

const STORAGE_PREFIX = 'modus_layout_';

/**
 * Initialize a CSS Grid container for tiles.
 * @param {HTMLElement} containerEl - The container element
 * @param {string} viewName - View identifier for persistence
 * @returns {HTMLElement} The grid container
 */
export function initGrid(containerEl, viewName) {
  _currentView = viewName;
  containerEl.innerHTML = '';

  const grid = document.createElement('div');
  grid.className = 'tile-grid';
  containerEl.appendChild(grid);

  return grid;
}

/**
 * Add tile elements to the grid with layout positioning.
 * @param {HTMLElement} grid - The grid container
 * @param {HTMLElement[]} tiles - Array of tile DOM elements
 * @param {object[]} layoutItems - Array of {id, w, h, ...}
 */
export function addTiles(grid, tiles, layoutItems) {
  tiles.forEach(tile => {
    const tileId = tile.getAttribute('gs-id') || tile.getAttribute('data-tile-id');
    const layout = layoutItems.find(l => l.id === tileId);
    if (layout) {
      tile.className = 'tile-item';
      tile.setAttribute('data-tile-id', layout.id);
      tile.setAttribute('data-w', layout.w || 12);
      tile.setAttribute('data-h', layout.h || 4);
    }
    grid.appendChild(tile);
  });
}

/**
 * Save layout state to localStorage.
 * @param {string} viewName
 */
export function saveLayout(viewName) {
  // Future: save tile order/visibility preferences
}

/**
 * Load saved layout, falling back to defaults.
 * @param {string} viewName
 * @param {object[]} defaultLayout
 * @returns {object[]}
 */
export function loadLayout(viewName, defaultLayout) {
  return defaultLayout || [];
}

/**
 * Reset layout to defaults.
 * @param {string} viewName
 */
export function resetLayout(viewName) {
  localStorage.removeItem(STORAGE_PREFIX + viewName);
}

/**
 * Destroy/cleanup.
 */
export function destroy() {
  _currentView = null;
}

/**
 * Get current view name.
 * @returns {string|null}
 */
export function getGrid() {
  return _currentView;
}
