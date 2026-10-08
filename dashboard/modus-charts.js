/**
 * Modus Chart Factory v1.0
 * ============================
 * Wraps Chart.js with consistent defaults: smooth curves, animations,
 * custom tooltips, and zoom support.
 *
 * Dependencies:
 *   - Chart.js 4.x (loaded via CDN)
 *   - chartjs-plugin-zoom (loaded via CDN, optional but recommended)
 *
 * Usage:
 *   const chart = ModusCharts.area(canvasEl, { ... });
 *   ModusCharts.destroy(chart);
 */

(function () {
  'use strict';

  /* ─── Chart Color Palette ──────────────────────────────────────── */
  const PALETTE = [
    '#3b82f6', // blue
    '#10b981', // green
    '#a855f7', // purple
    '#f59e0b', // amber
    '#ef4444', // red
    '#06b6d4', // cyan
    '#ec4899', // pink
    '#8b5cf6', // violet
  ];

  /* ─── Helpers ──────────────────────────────────────────────────── */

  function getColor(index) {
    return PALETTE[index % PALETTE.length];
  }

  function hexToRgba(hex, alpha) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  function isDarkMode() {
    return document.documentElement.getAttribute('data-theme') !== 'light';
  }

  function gridColor() {
    return isDarkMode() ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)';
  }

  function textColor() {
    return isDarkMode() ? '#919191' : '#5e6068';
  }

  function tooltipBg() {
    return isDarkMode() ? '#212121' : '#ffffff';
  }

  function tooltipBorder() {
    return isDarkMode() ? '#3a3a3a' : '#dfe1e6';
  }

  function tooltipText() {
    return isDarkMode() ? '#e0e0e0' : '#1a1a1a';
  }

  /* ─── Custom Tooltip ───────────────────────────────────────────── */

  function customTooltip(context) {
    let el = document.getElementById('ds-chart-tooltip');
    if (!el) {
      el = document.createElement('div');
      el.id = 'ds-chart-tooltip';
      el.style.cssText = `
        position: absolute;
        pointer-events: none;
        z-index: 9999;
        padding: 10px 14px;
        border-radius: 10px;
        font-family: Inter, -apple-system, sans-serif;
        font-size: 12px;
        line-height: 1.5;
        transition: opacity 0.15s ease, transform 0.15s ease;
        box-shadow: 0 4px 16px rgba(0,0,0,0.2);
      `;
      document.body.appendChild(el);
    }

    const tooltip = context.tooltip;
    if (tooltip.opacity === 0) {
      el.style.opacity = '0';
      el.style.transform = 'translateY(4px)';
      return;
    }

    el.style.background = tooltipBg();
    el.style.border = `1px solid ${tooltipBorder()}`;
    el.style.color = tooltipText();

    let html = '';
    if (tooltip.title && tooltip.title.length) {
      html += `<div style="font-weight:600;margin-bottom:4px;opacity:0.7;font-size:11px;">${tooltip.title[0]}</div>`;
    }
    if (tooltip.body) {
      tooltip.body.forEach((b, i) => {
        const color = tooltip.labelColors[i]?.backgroundColor || PALETTE[0];
        const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};margin-right:6px;"></span>`;
        html += `<div style="display:flex;align-items:center;">${dot}${b.lines.join('<br>')}</div>`;
      });
    }
    el.innerHTML = html;

    const pos = context.chart.canvas.getBoundingClientRect();
    el.style.opacity = '1';
    el.style.transform = 'translateY(0)';
    el.style.left = pos.left + window.scrollX + tooltip.caretX + 'px';
    el.style.top = pos.top + window.scrollY + tooltip.caretY - 10 + 'px';
  }

  /* ─── Global Defaults ──────────────────────────────────────────── */

  function applyDefaults() {
    if (!window.Chart) return;
    const defaults = Chart.defaults;

    defaults.font.family = "'Inter', -apple-system, BlinkMacSystemFont, sans-serif";
    defaults.font.size = 11;
    defaults.color = textColor();
    defaults.responsive = true;
    defaults.maintainAspectRatio = false;

    defaults.animation = false; // disabled — _fn error in Chart.js 4.4.1 animation ticker

    defaults.plugins.legend.labels.usePointStyle = true;
    defaults.plugins.legend.labels.pointStyle = 'circle';
    defaults.plugins.legend.labels.padding = 16;
    defaults.plugins.legend.labels.font = { size: 11, weight: '500' };

    defaults.plugins.tooltip.enabled = false;
    defaults.plugins.tooltip.external = customTooltip;

    defaults.elements.line.tension = 0.4;
    defaults.elements.line.borderWidth = 2;
    defaults.elements.point.radius = 0;
    defaults.elements.point.hoverRadius = 5;
    defaults.elements.point.hoverBorderWidth = 2;
    defaults.elements.bar.borderRadius = 4;
  }

  /* ─── Base Options Builder ─────────────────────────────────────── */

  function baseScaleOptions(opts = {}) {
    return {
      x: {
        grid: {
          display: opts.xGrid !== false,
          color: gridColor(),
          drawBorder: false,
          drawTicks: false,
        },
        ticks: {
          color: textColor(),
          padding: 8,
          font: { size: 10 },
          maxRotation: 0,
        },
        border: { display: false },
        ...(opts.xType ? { type: opts.xType } : {}),
      },
      y: {
        grid: {
          display: true,
          color: gridColor(),
          drawBorder: false,
          drawTicks: false,
        },
        ticks: {
          color: textColor(),
          padding: 8,
          font: { size: 10 },
          ...(opts.yCallback ? { callback: opts.yCallback } : {}),
        },
        border: { display: false },
        beginAtZero: opts.beginAtZero !== false,
        ...(opts.yStacked ? { stacked: true } : {}),
      },
    };
  }

  function zoomOptions() {
    if (!window.Chart || !Chart.registry?.plugins?.get('zoom')) return {};
    return {
      zoom: {
        zoom: {
          wheel: { enabled: true },
          pinch: { enabled: true },
          mode: 'x',
        },
        pan: {
          enabled: true,
          mode: 'x',
        },
        limits: {
          x: { minRange: 3 },
        },
      },
    };
  }

  /* ─── Chart Factory Functions ──────────────────────────────────── */

  /**
   * Area chart — filled line with gradient.
   * @param {HTMLCanvasElement} canvas
   * @param {Object} cfg
   * @param {string[]} cfg.labels
   * @param {Array<{label, data, color?}>} cfg.datasets
   * @param {string} [cfg.yFormat] — 'currency' | 'number' | 'percent'
   * @param {number} [cfg.budgetLine] — horizontal budget reference line
   * @param {boolean} [cfg.zoomable] — enable zoom/pan
   */
  function createArea(canvas, cfg) {
    const ctx = canvas.getContext('2d');

    // Support shorthand: { data, label, color } → { datasets: [{ data, label, color }] }
    if (!cfg.datasets && cfg.data) {
      cfg.datasets = [{ data: cfg.data, label: cfg.label || 'Value', color: cfg.color }];
    }

    const datasets = (cfg.datasets || []).map((ds, i) => {
      const color = ds.color || getColor(i);
      const gradient = ctx.createLinearGradient(0, 0, 0, canvas.parentElement?.offsetHeight || 300);
      gradient.addColorStop(0, hexToRgba(color, 0.25));
      gradient.addColorStop(1, hexToRgba(color, 0.0));

      return {
        label: ds.label,
        data: ds.data,
        borderColor: color,
        backgroundColor: gradient,
        fill: true,
        tension: 0.4,
        pointRadius: 0,
        pointHoverRadius: 5,
        pointHoverBackgroundColor: color,
        pointHoverBorderColor: '#fff',
        pointHoverBorderWidth: 2,
      };
    });

    // Optional budget reference line
    if (cfg.budgetLine != null) {
      datasets.push({
        label: 'Budget',
        data: Array(cfg.labels.length).fill(cfg.budgetLine),
        borderColor: hexToRgba('#ef4444', 0.6),
        borderDash: [6, 4],
        borderWidth: 1.5,
        fill: false,
        pointRadius: 0,
        pointHoverRadius: 0,
      });
    }

    const yCallback = cfg.yFormat === 'currency'
      ? (v) => '$' + v.toLocaleString()
      : cfg.yFormat === 'percent'
        ? (v) => v + '%'
        : undefined;

    return new Chart(ctx, {
      type: 'line',
      data: { labels: cfg.labels, datasets },
      options: {
        scales: baseScaleOptions({ yCallback }),
        plugins: {
          legend: {
            display: cfg.datasets.length > 1 || cfg.budgetLine != null,
            position: 'bottom',
          },
          ...zoomOptions(),
        },
        interaction: {
          mode: 'index',
          intersect: false,
        },
      },
    });
  }

  /**
   * Donut chart — ring with center label.
   * @param {HTMLCanvasElement} canvas
   * @param {Object} cfg
   * @param {string[]} cfg.labels
   * @param {number[]} cfg.data
   * @param {string[]} [cfg.colors]
   * @param {string} [cfg.centerLabel] — text in the center
   * @param {string} [cfg.centerValue] — value in the center
   */
  function createDonut(canvas, cfg) {
    const ctx = canvas.getContext('2d');
    const colors = cfg.colors || cfg.labels.map((_, i) => getColor(i));

    // Normalize center text properties (support both naming conventions)
    cfg.centerValue = cfg.centerValue || cfg.centerText;
    cfg.centerLabel = cfg.centerLabel || cfg.centerSub;

    // Center text plugin
    const centerPlugin = {
      id: 'centerText',
      afterDraw(chart) {
        if (!cfg.centerLabel && !cfg.centerValue) return;
        const { ctx: drawCtx, chartArea } = chart;
        const cx = (chartArea.left + chartArea.right) / 2;
        const cy = (chartArea.top + chartArea.bottom) / 2;

        drawCtx.save();
        if (cfg.centerValue) {
          drawCtx.font = "bold 20px 'Inter', sans-serif";
          drawCtx.fillStyle = isDarkMode() ? '#e8ecf4' : '#1a1d2e';
          drawCtx.textAlign = 'center';
          drawCtx.textBaseline = 'middle';
          drawCtx.fillText(cfg.centerValue, cx, cfg.centerLabel ? cy - 8 : cy);
        }
        if (cfg.centerLabel) {
          drawCtx.font = "500 11px 'Inter', sans-serif";
          drawCtx.fillStyle = textColor();
          drawCtx.textAlign = 'center';
          drawCtx.textBaseline = 'middle';
          drawCtx.fillText(cfg.centerLabel, cx, cfg.centerValue ? cy + 14 : cy);
        }
        drawCtx.restore();
      },
    };

    return new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels: cfg.labels,
        datasets: [{
          data: cfg.data,
          backgroundColor: colors,
          borderColor: isDarkMode() ? '#212121' : '#ffffff',
          borderWidth: 2,
          hoverOffset: 6,
        }],
      },
      options: {
        cutout: '70%',
        plugins: {
          legend: {
            position: 'bottom',
            labels: {
              padding: 12,
              usePointStyle: true,
              pointStyle: 'circle',
            },
          },
        },
        animation: {
          animateRotate: true,
          duration: 1000,
          easing: 'easeInOutQuart',
        },
      },
      plugins: [centerPlugin],
    });
  }

  /**
   * Bar chart — vertical or horizontal.
   * @param {HTMLCanvasElement} canvas
   * @param {Object} cfg
   * @param {string[]} cfg.labels
   * @param {Array<{label, data, color?}>} cfg.datasets
   * @param {boolean} [cfg.horizontal]
   * @param {boolean} [cfg.stacked]
   * @param {string} [cfg.yFormat]
   */
  function createBar(canvas, cfg) {
    const ctx = canvas.getContext('2d');

    const datasets = cfg.datasets.map((ds, i) => ({
      label: ds.label,
      data: ds.data,
      backgroundColor: ds.color || getColor(i),
      borderRadius: 4,
      borderSkipped: false,
      maxBarThickness: 48,
    }));

    const yCallback = cfg.yFormat === 'currency'
      ? (v) => '$' + v.toLocaleString()
      : cfg.yFormat === 'percent'
        ? (v) => v + '%'
        : undefined;

    const scales = baseScaleOptions({
      yCallback: cfg.horizontal ? undefined : yCallback,
      yStacked: cfg.stacked,
      beginAtZero: true,
    });
    if (cfg.stacked) {
      scales.x.stacked = true;
      scales.y.stacked = true;
    }

    return new Chart(ctx, {
      type: 'bar',
      data: { labels: cfg.labels, datasets },
      options: {
        indexAxis: cfg.horizontal ? 'y' : 'x',
        scales,
        plugins: {
          legend: {
            display: cfg.datasets.length > 1,
            position: 'bottom',
          },
        },
      },
    });
  }

  /**
   * Stacked area chart — multiple series stacked.
   * @param {HTMLCanvasElement} canvas
   * @param {Object} cfg
   * @param {string[]} cfg.labels
   * @param {Array<{label, data, color?}>} cfg.datasets
   * @param {string} [cfg.yFormat]
   */
  function createStackedArea(canvas, cfg) {
    const ctx = canvas.getContext('2d');

    const datasets = cfg.datasets.map((ds, i) => {
      const color = ds.color || getColor(i);
      return {
        label: ds.label,
        data: ds.data,
        borderColor: color,
        backgroundColor: hexToRgba(color, 0.5),
        fill: true,
        tension: 0.4,
        pointRadius: 0,
        pointHoverRadius: 4,
      };
    });

    const yCallback = cfg.yFormat === 'currency'
      ? (v) => '$' + v.toLocaleString()
      : undefined;

    return new Chart(ctx, {
      type: 'line',
      data: { labels: cfg.labels, datasets },
      options: {
        scales: baseScaleOptions({ yCallback, yStacked: true }),
        plugins: {
          legend: { position: 'bottom' },
          filler: { propagate: true },
          ...zoomOptions(),
        },
        interaction: {
          mode: 'index',
          intersect: false,
        },
      },
    });
  }

  /**
   * Sparkline — tiny inline chart. No axes, no labels.
   * @param {HTMLCanvasElement} canvas
   * @param {number[]} data
   * @param {string} [color]
   */
  function createSparkline(canvas, data, color) {
    const ctx = canvas.getContext('2d');
    const c = color || PALETTE[0];
    const gradient = ctx.createLinearGradient(0, 0, 0, canvas.height || 40);
    gradient.addColorStop(0, hexToRgba(c, 0.3));
    gradient.addColorStop(1, hexToRgba(c, 0.0));

    return new Chart(ctx, {
      type: 'line',
      data: {
        labels: data.map((_, i) => i),
        datasets: [{
          data,
          borderColor: c,
          backgroundColor: gradient,
          fill: true,
          tension: 0.4,
          borderWidth: 1.5,
          pointRadius: 0,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: { display: false },
          y: { display: false },
        },
        plugins: {
          legend: { display: false },
          tooltip: { enabled: false },
        },
        animation: {
          duration: 600,
          easing: 'easeInOutQuart',
        },
      },
    });
  }

  /* ─── Gauge Helper (SVG-based, not Chart.js) ───────────────────── */

  /**
   * Create a radial gauge inside a container element.
   * @param {HTMLElement} container
   * @param {Object} cfg
   * @param {number} cfg.value — 0-100
   * @param {string} [cfg.label]
   * @param {number} [cfg.size] — px, default 120
   */
  function createGauge(container, cfg) {
    const size = cfg.size || 120;
    const strokeWidth = 8;
    const radius = (size - strokeWidth) / 2;
    const circumference = 2 * Math.PI * radius;
    const pct = Math.min(Math.max(cfg.value, 0), 100);
    const offset = circumference * (1 - pct / 100);

    // Color: use cfg.color if provided, otherwise auto from value thresholds
    let color = cfg.color;
    if (!color) {
      color = '#10b981'; // green
      if (pct >= 80) color = '#ef4444'; // red
      else if (pct >= 60) color = '#f59e0b'; // amber
    }

    container.innerHTML = `
      <div class="ds-gauge">
        <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
          <circle class="ds-gauge-track"
            cx="${size / 2}" cy="${size / 2}" r="${radius}"
            stroke-width="${strokeWidth}" />
          <circle class="ds-gauge-fill"
            cx="${size / 2}" cy="${size / 2}" r="${radius}"
            stroke="${color}"
            stroke-dasharray="${circumference}"
            stroke-dashoffset="${circumference}"
            style="--ds-gauge-circumference:${circumference};--ds-gauge-offset:${offset};
                   transition: stroke-dashoffset 1.2s cubic-bezier(0.4, 0, 0.2, 1);"/>
        </svg>
        <div class="ds-gauge-label">${Math.round(pct)}%</div>
        ${cfg.label ? `<div class="ds-gauge-sublabel">${cfg.label}</div>` : ''}
      </div>
    `;

    // Trigger animation after paint
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const fill = container.querySelector('.ds-gauge-fill');
        if (fill) fill.style.strokeDashoffset = offset;
      });
    });
  }

  /* ─── Toast System ─────────────────────────────────────────────── */

  function ensureToastContainer() {
    let c = document.querySelector('.ds-toast-container');
    if (!c) {
      c = document.createElement('div');
      c.className = 'ds-toast-container';
      document.body.appendChild(c);
    }
    return c;
  }

  /**
   * Show a toast notification.
   * @param {string} message
   * @param {'success'|'error'|'warning'|'info'} [type='info']
   * @param {number} [duration=4000]
   */
  function toast(message, type, duration) {
    type = type || 'info';
    duration = duration || 4000;
    const container = ensureToastContainer();

    const icons = {
      success: '✓',
      error: '✕',
      warning: '⚠',
      info: 'ℹ',
    };

    const el = document.createElement('div');
    el.className = `ds-toast ds-toast-${type}`;
    el.innerHTML = `
      <span class="ds-toast-icon">${icons[type]}</span>
      <span class="ds-toast-message">${message}</span>
      <div class="ds-toast-progress" style="animation-duration:${duration}ms"></div>
    `;
    container.appendChild(el);

    setTimeout(() => {
      el.classList.add('removing');
      setTimeout(() => el.remove(), 250);
    }, duration);
  }

  /* ─── Utility: Count-Up Animation ──────────────────────────────── */

  /**
   * Animate a number from 0 to target.
   * @param {HTMLElement} el
   * @param {number} target
   * @param {Object} [opts]
   * @param {string} [opts.prefix] — e.g. '$'
   * @param {string} [opts.suffix] — e.g. '%'
   * @param {number} [opts.decimals] — decimal places
   * @param {number} [opts.duration] — ms
   */
  function countUp(el, target, opts) {
    opts = opts || {};
    const prefix = opts.prefix || '';
    const suffix = opts.suffix || '';
    const decimals = opts.decimals != null ? opts.decimals : 0;
    const duration = opts.duration || 800;
    const start = performance.now();

    function frame(now) {
      const progress = Math.min((now - start) / duration, 1);
      // easeOutQuart
      const eased = 1 - Math.pow(1 - progress, 4);
      const current = target * eased;
      el.textContent = prefix + current.toLocaleString(undefined, {
        minimumFractionDigits: decimals,
        maximumFractionDigits: decimals,
      }) + suffix;
      if (progress < 1) requestAnimationFrame(frame);
    }

    requestAnimationFrame(frame);
  }

  /* ─── Utility: Destroy Chart ───────────────────────────────────── */

  function destroy(chart) {
    if (chart && typeof chart.destroy === 'function') {
      chart.destroy();
    }
  }

  /* ─── Theme Listener ───────────────────────────────────────────── */

  // Re-apply defaults when theme changes
  const observer = new MutationObserver(() => {
    applyDefaults();
    // Update existing tooltip colors
    const tooltip = document.getElementById('ds-chart-tooltip');
    if (tooltip) {
      tooltip.style.background = tooltipBg();
      tooltip.style.borderColor = tooltipBorder();
      tooltip.style.color = tooltipText();
    }
  });
  observer.observe(document.documentElement, {
    attributes: true,
    attributeFilter: ['data-theme'],
  });

  /* ─── Initialize ───────────────────────────────────────────────── */

  // Apply defaults when Chart.js is available
  if (window.Chart) {
    applyDefaults();
  } else {
    // Wait for Chart.js to load
    window.addEventListener('load', applyDefaults);
  }

  /* ─── Public API ───────────────────────────────────────────────── */

  window.ModusCharts = {
    area: createArea,
    donut: createDonut,
    bar: createBar,
    stackedArea: createStackedArea,
    sparkline: createSparkline,
    gauge: createGauge,
    toast: toast,
    countUp: countUp,
    destroy: destroy,
    PALETTE: PALETTE,
    getColor: getColor,
  };

})();
