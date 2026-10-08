/**
 * Modus Dashboard — Tile Component Factory
 * Creates standardized Gridstack tile elements with consistent
 * header, body, loading/empty/error states, filter support, and
 * automatic hover-help icon injection for any tile id registered
 * in help.js.
 */

import { attachHelpIcon, HELP_REGISTRY } from './help.js';

/**
 * Create a tile DOM element for use in a Gridstack grid.
 *
 * @param {object} config
 * @param {string} config.id - Unique tile ID (e.g., 'overview-cost-chart')
 * @param {string} config.title - Panel header title
 * @param {string} [config.icon] - FontAwesome class (e.g., 'fa-solid fa-chart-area')
 * @param {string} [config.iconBg] - Icon background color (e.g., 'rgba(159,193,49,0.1)')
 * @param {string} [config.iconColor] - Icon text color (e.g., 'var(--accent)')
 * @param {string} [config.meta] - Header right-side metadata text
 * @param {Array}  [config.actions] - [{label, icon, onclick}] header action buttons
 * @param {boolean} [config.filterable] - Show filter input in header
 * @param {string} [config.filterPlaceholder] - Placeholder for filter input
 * @param {Function} [config.onFilter] - Called with filter query string
 * @param {string} [config.className] - Additional CSS class for tile body
 * @returns {HTMLElement} The grid-stack-item element
 */
export function createTile(config) {
  const wrapper = document.createElement('div');
  wrapper.className = 'tile-item';
  wrapper.setAttribute('data-tile-id', config.id);
  wrapper.setAttribute('gs-id', config.id); // backward compat

  const content = document.createElement('div');
  content.className = 'gs-tile';

  // ── Header ──────────────────────────────────────────────
  const header = document.createElement('div');
  header.className = 'gs-tile-header';

  let titleHTML = '';
  if (config.icon) {
    const bg = config.iconBg || 'rgba(159,193,49,0.1)';
    const color = config.iconColor || 'var(--accent)';
    titleHTML += `<div class="panel-icon" style="background:${bg};color:${color}"><i class="${config.icon}"></i></div> `;
  }
  titleHTML += config.title || '';

  header.innerHTML = `
    <div class="gs-tile-title">${titleHTML}</div>
    <div class="gs-tile-right">
      ${config.meta ? `<div class="gs-tile-meta">${config.meta}</div>` : ''}
      <div class="gs-tile-actions"></div>
    </div>
  `;

  // Actions — supports array of {label, icon, onclick} OR raw HTML string
  if (config.actions) {
    const actionsEl = header.querySelector('.gs-tile-actions');
    if (typeof config.actions === 'string') {
      actionsEl.innerHTML = config.actions;
    } else if (Array.isArray(config.actions)) {
      config.actions.forEach(action => {
        const btn = document.createElement('button');
        btn.className = 'ds-btn ds-btn-ghost ds-btn-sm';
        btn.innerHTML = action.icon ? `<i class="${action.icon}"></i>${action.label ? ' ' + action.label : ''}` : action.label;
        btn.onclick = action.onclick;
        actionsEl.appendChild(btn);
      });
    }
  }

  // ── Filter bar (optional) ──────────────────────────────
  let filterBar = null;
  if (config.filterable) {
    filterBar = document.createElement('div');
    filterBar.className = 'gs-tile-filter';
    filterBar.innerHTML = `<span class="gs-tile-filter-icon"><i class="fa-solid fa-magnifying-glass"></i></span><input type="text" class="ds-input ds-input-sm" placeholder="${config.filterPlaceholder || 'Filter...'}" />`;
    const input = filterBar.querySelector('input');
    input.addEventListener('input', () => {
      if (config.onFilter) config.onFilter(input.value);
    });
  }

  // ── Body ────────────────────────────────────────────────
  const body = document.createElement('div');
  body.className = 'gs-tile-body' + (config.className ? ' ' + config.className : '');
  body.id = `${config.id}-body`;

  // Assemble
  content.appendChild(header);
  if (filterBar) content.appendChild(filterBar);
  content.appendChild(body);
  wrapper.appendChild(content);

  // Store config reference
  wrapper._tileConfig = config;

  // Auto-inject hover help icon if this tile id is registered in help.js
  if (config.id && HELP_REGISTRY[config.id]) {
    attachHelpIcon(header, config.id);
  }

  return wrapper;
}

/**
 * Show loading skeleton in a tile body.
 * @param {string} tileId
 * @param {string} [type='chart'] - 'chart', 'table', 'cards', 'text'
 */
export function setTileLoading(tileId, type = 'chart') {
  const body = document.getElementById(`${tileId}-body`);
  if (!body) return;

  const skeletons = {
    chart: `
      <div style="padding:16px">
        <div class="ds-skeleton" style="height:20px;width:40%;margin-bottom:12px;border-radius:4px"></div>
        <div class="ds-skeleton" style="height:180px;border-radius:8px"></div>
      </div>`,
    table: `
      <div style="padding:16px">
        <div class="ds-skeleton" style="height:16px;width:100%;margin-bottom:8px;border-radius:4px"></div>
        <div class="ds-skeleton" style="height:16px;width:90%;margin-bottom:8px;border-radius:4px"></div>
        <div class="ds-skeleton" style="height:16px;width:95%;margin-bottom:8px;border-radius:4px"></div>
        <div class="ds-skeleton" style="height:16px;width:85%;margin-bottom:8px;border-radius:4px"></div>
        <div class="ds-skeleton" style="height:16px;width:92%;border-radius:4px"></div>
      </div>`,
    cards: `
      <div style="padding:16px;display:flex;gap:12px;flex-wrap:wrap">
        <div class="ds-skeleton" style="height:80px;flex:1;min-width:120px;border-radius:8px"></div>
        <div class="ds-skeleton" style="height:80px;flex:1;min-width:120px;border-radius:8px"></div>
        <div class="ds-skeleton" style="height:80px;flex:1;min-width:120px;border-radius:8px"></div>
      </div>`,
    text: `
      <div style="padding:16px">
        <div class="ds-skeleton" style="height:14px;width:80%;margin-bottom:8px;border-radius:4px"></div>
        <div class="ds-skeleton" style="height:14px;width:60%;border-radius:4px"></div>
      </div>`,
  };

  body.innerHTML = skeletons[type] || skeletons.chart;
}

/**
 * Show empty state in a tile body.
 * @param {string} tileId
 * @param {object} config
 * @param {string} [config.icon='fa-solid fa-inbox']
 * @param {string} [config.title='No data']
 * @param {string} [config.description='']
 */
export function setTileEmpty(tileId, config = {}) {
  const body = document.getElementById(`${tileId}-body`);
  if (!body) return;
  body.innerHTML = `
    <div class="ds-empty" style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:160px;gap:8px;padding:20px;text-align:center">
      <i class="${config.icon || 'fa-solid fa-inbox'}" style="font-size:28px;opacity:0.3;color:var(--muted)"></i>
      <div style="font-size:13px;font-weight:600;color:var(--text);opacity:0.7">${config.title || 'No data'}</div>
      ${config.description ? `<div style="font-size:11px;color:var(--muted);max-width:280px">${config.description}</div>` : ''}
    </div>
  `;
}

/**
 * Show error state in a tile body.
 * @param {string} tileId
 * @param {string} message
 * @param {Function} [retryFn]
 */
export function setTileError(tileId, message, retryFn) {
  const body = document.getElementById(`${tileId}-body`);
  if (!body) return;
  body.innerHTML = `
    <div class="ds-empty" style="display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:160px;gap:8px;padding:20px;text-align:center">
      <i class="fa-solid fa-circle-exclamation" style="font-size:28px;color:var(--danger);opacity:0.6"></i>
      <div style="font-size:13px;font-weight:600;color:var(--text);opacity:0.7">${message}</div>
      ${retryFn ? '<button class="ds-btn ds-btn-ghost ds-btn-sm" style="margin-top:4px">Retry</button>' : ''}
    </div>
  `;
  if (retryFn) {
    const btn = body.querySelector('button');
    if (btn) btn.onclick = retryFn;
  }
}

/**
 * Update the meta text in a tile header.
 * @param {string} tileId
 * @param {string} text
 */
export function setTileMeta(tileId, text) {
  const wrapper = document.querySelector(`[gs-id="${tileId}"]`);
  if (!wrapper) return;
  const meta = wrapper.querySelector('.gs-tile-meta');
  if (meta) meta.textContent = text;
}

/**
 * Export visible table data from a tile to CSV or JSON.
 * @param {string} tileId - The tile containing the table
 * @param {string} format - 'csv' or 'json'
 * @param {string} [filename] - Base filename (without extension)
 */
export function exportTileData(tileId, format = 'csv', filename) {
  const body = document.getElementById(`${tileId}-body`);
  if (!body) return;

  const table = body.querySelector('table');
  if (!table) return;

  const headers = [];
  const rows = [];

  // Extract headers
  table.querySelectorAll('thead th').forEach(th => {
    headers.push(th.textContent.trim());
  });

  // Extract visible rows only (respects filtering)
  table.querySelectorAll('tbody tr').forEach(tr => {
    if (tr.style.display === 'none') return;
    const cells = [];
    tr.querySelectorAll('td').forEach(td => {
      cells.push(td.textContent.trim());
    });
    rows.push(cells);
  });

  if (!rows.length) return;

  const baseName = filename || `modus-${tileId}-${new Date().toISOString().slice(0, 10)}`;

  if (format === 'json') {
    const jsonData = rows.map(row => {
      const obj = {};
      headers.forEach((h, i) => { obj[h] = row[i] || ''; });
      return obj;
    });
    _downloadFile(`${baseName}.json`, JSON.stringify(jsonData, null, 2), 'application/json');
  } else {
    const csvRows = [headers.join(',')];
    rows.forEach(row => {
      csvRows.push(row.map(cell => {
        // Escape CSV: wrap in quotes if contains comma, quote, or newline
        const escaped = String(cell).replace(/"/g, '""');
        return /[,"\n\r]/.test(cell) ? `"${escaped}"` : escaped;
      }).join(','));
    });
    _downloadFile(`${baseName}.csv`, csvRows.join('\n'), 'text/csv');
  }
}

/**
 * Create a download action config for use in tile actions.
 * Returns an actions array with CSV and JSON export buttons.
 * @param {string} tileId
 * @param {string} [filename] - Base filename
 * @returns {Array} Array of action objects for createTile
 */
export function exportActions(tileId, filename) {
  return [
    {
      label: 'CSV',
      icon: 'fa-solid fa-file-csv',
      onclick: () => exportTileData(tileId, 'csv', filename),
    },
    {
      label: 'JSON',
      icon: 'fa-solid fa-file-code',
      onclick: () => exportTileData(tileId, 'json', filename),
    },
  ];
}

function _downloadFile(filename, content, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
