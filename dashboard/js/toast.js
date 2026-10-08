/**
 * Modus Dashboard — Toast Notification Helper
 *
 * Replaces native alert() and confirm() with non-blocking styled toasts
 * and a promise-based inline confirmation modal. This is the GAP-6
 * standard applied dashboard-wide.
 *
 * Usage:
 *   import { toast, confirm as uiConfirm } from './toast.js';
 *
 *   toast('Saved', 'success');           // green, auto-dismisses
 *   toast('Error: ' + e.message, 'error');   // red, 6s dismiss
 *   toast('Test sent to Slack', 'info');  // blue, 4s dismiss
 *
 *   if (await uiConfirm('Delete this override?')) {
 *     // user clicked confirm
 *   }
 *
 * Air-gap safe — zero deps, all inline CSS.
 */

let _container = null;
let _idCounter = 0;

function _ensureContainer() {
  if (_container) return _container;
  _container = document.createElement('div');
  _container.id = 'modus-toast-container';
  _container.style.cssText = `
    position: fixed;
    top: 20px;
    right: 20px;
    z-index: 100000;
    display: flex;
    flex-direction: column;
    gap: 10px;
    pointer-events: none;
  `;
  document.body.appendChild(_container);
  return _container;
}

/**
 * Show a non-blocking toast notification.
 * @param {string} message
 * @param {'success'|'error'|'info'|'warn'} [type='info']
 * @param {number} [durationMs] - auto-dismiss timeout (defaults to 4000 for info/success, 6000 for error/warn)
 */
export function toast(message, type = 'info', durationMs) {
  const container = _ensureContainer();
  const id = ++_idCounter;

  const colors = {
    success: { bg: 'rgba(0,229,160,0.12)', border: '#00e5a0', icon: 'fa-solid fa-circle-check' },
    error:   { bg: 'rgba(239,68,68,0.12)', border: '#ef4444', icon: 'fa-solid fa-circle-exclamation' },
    warn:    { bg: 'rgba(245,158,11,0.12)', border: '#f59e0b', icon: 'fa-solid fa-triangle-exclamation' },
    info:    { bg: 'rgba(59,130,246,0.12)', border: '#3b82f6', icon: 'fa-solid fa-circle-info' },
  };
  const c = colors[type] || colors.info;

  const el = document.createElement('div');
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  el.style.cssText = `
    pointer-events: auto;
    min-width: 280px;
    max-width: 420px;
    padding: 12px 14px;
    background: var(--surface, #1a1d2e);
    border: 1px solid ${c.border};
    border-left: 4px solid ${c.border};
    border-radius: 8px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.4);
    font-size: 12px;
    line-height: 1.5;
    color: var(--text, #f5f7fb);
    display: flex;
    align-items: flex-start;
    gap: 10px;
    opacity: 0;
    transform: translateX(20px);
    transition: opacity 0.18s ease, transform 0.18s ease;
  `;

  // Build via textContent to prevent XSS from untrusted messages
  const iconEl = document.createElement('i');
  iconEl.className = c.icon;
  iconEl.style.cssText = `color:${c.border};margin-top:2px;flex-shrink:0`;

  const msgEl = document.createElement('div');
  msgEl.style.cssText = 'flex:1;min-width:0;white-space:pre-wrap;word-break:break-word';
  msgEl.textContent = String(message ?? '');

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.innerHTML = '&times;';
  closeBtn.setAttribute('aria-label', 'Dismiss');
  closeBtn.style.cssText = `
    background: none;
    border: none;
    color: var(--muted, #8a8fa8);
    cursor: pointer;
    font-size: 18px;
    line-height: 1;
    padding: 0 4px;
    flex-shrink: 0;
  `;

  el.append(iconEl, msgEl, closeBtn);
  container.appendChild(el);

  // Animate in
  requestAnimationFrame(() => {
    el.style.opacity = '1';
    el.style.transform = 'translateX(0)';
  });

  const dismiss = () => {
    if (!el.isConnected) return;
    el.style.opacity = '0';
    el.style.transform = 'translateX(20px)';
    setTimeout(() => {
      if (el.isConnected) el.remove();
    }, 200);
  };

  closeBtn.addEventListener('click', dismiss);

  const autoMs = durationMs ?? (type === 'error' || type === 'warn' ? 6000 : 4000);
  if (autoMs > 0) setTimeout(dismiss, autoMs);

  return { id, dismiss };
}

/**
 * Promise-based inline confirmation modal. Replaces native confirm().
 * @param {string} message
 * @param {object} [opts]
 * @param {string} [opts.title='Confirm']
 * @param {string} [opts.confirmLabel='Confirm']
 * @param {string} [opts.cancelLabel='Cancel']
 * @param {'danger'|'primary'} [opts.confirmStyle='primary']
 * @returns {Promise<boolean>}
 */
export function confirm(message, opts = {}) {
  const {
    title = 'Confirm',
    confirmLabel = 'Confirm',
    cancelLabel = 'Cancel',
    confirmStyle = 'primary',
  } = opts;

  return new Promise((resolve) => {
    const backdrop = document.createElement('div');
    backdrop.className = 'ds-modal-backdrop';
    backdrop.style.cssText = `
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.6);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 99999;
    `;

    const modal = document.createElement('div');
    modal.className = 'ds-modal';
    modal.style.cssText = `
      background: var(--surface, #1a1d2e);
      border: 1px solid var(--border, #2a2f48);
      border-radius: 10px;
      max-width: 420px;
      width: 92%;
      padding: 0;
      box-shadow: 0 16px 48px rgba(0,0,0,0.6);
    `;

    const header = document.createElement('div');
    header.style.cssText = 'padding:16px 20px;border-bottom:1px solid var(--border)';
    const titleEl = document.createElement('h3');
    titleEl.style.cssText = 'margin:0;font-size:14px;font-weight:700;color:var(--text)';
    titleEl.textContent = title;
    header.appendChild(titleEl);

    const body = document.createElement('div');
    body.style.cssText = 'padding:16px 20px;font-size:13px;color:var(--text);line-height:1.55;white-space:pre-wrap';
    body.textContent = message;

    const footer = document.createElement('div');
    footer.style.cssText = 'padding:12px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px';

    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'ds-btn ds-btn-ghost';
    cancelBtn.textContent = cancelLabel;

    const confirmBtn = document.createElement('button');
    confirmBtn.type = 'button';
    confirmBtn.className = confirmStyle === 'danger' ? 'ds-btn ds-btn-danger' : 'ds-btn ds-btn-primary';
    confirmBtn.textContent = confirmLabel;
    if (confirmStyle === 'danger') {
      confirmBtn.style.background = 'var(--danger,#ef4444)';
      confirmBtn.style.color = '#fff';
    }

    footer.append(cancelBtn, confirmBtn);
    modal.append(header, body, footer);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);
    confirmBtn.focus();

    const cleanup = (result) => {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
      resolve(result);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') cleanup(false);
      if (e.key === 'Enter') cleanup(true);
    };

    cancelBtn.addEventListener('click', () => cleanup(false));
    confirmBtn.addEventListener('click', () => cleanup(true));
    backdrop.addEventListener('click', (e) => {
      if (e.target === backdrop) cleanup(false);
    });
    document.addEventListener('keydown', onKey);
  });
}

/**
 * Ask for a single secret value (e.g. the master key) in a modal.
 * The value is returned to the caller only; it is never stored.
 * @param {string} message
 * @param {Object} [opts]
 * @param {string} [opts.title]
 * @param {string} [opts.confirmLabel]
 * @param {string} [opts.placeholder]
 * @returns {Promise<string|null>} the entered value, or null if cancelled/empty
 */
export function promptSecret(message, opts = {}) {
  const { title = 'Enter value', confirmLabel = 'Continue', placeholder = '' } = opts;

  return new Promise((resolve) => {
    const backdrop = document.createElement('div');
    backdrop.className = 'ds-modal-backdrop';
    backdrop.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);display:flex;align-items:center;justify-content:center;z-index:99999';

    const modal = document.createElement('div');
    modal.className = 'ds-modal';
    modal.style.cssText = 'background:var(--surface,#1a1d2e);border:1px solid var(--border,#2a2f48);border-radius:10px;max-width:440px;width:92%;padding:0;box-shadow:0 16px 48px rgba(0,0,0,0.6)';

    const header = document.createElement('div');
    header.style.cssText = 'padding:16px 20px;border-bottom:1px solid var(--border)';
    const titleEl = document.createElement('h3');
    titleEl.style.cssText = 'margin:0;font-size:14px;font-weight:700;color:var(--text)';
    titleEl.textContent = title;
    header.appendChild(titleEl);

    const body = document.createElement('div');
    body.style.cssText = 'padding:16px 20px;font-size:13px;color:var(--text);line-height:1.55';
    const msg = document.createElement('div');
    msg.style.cssText = 'margin-bottom:12px;white-space:pre-wrap';
    msg.textContent = message;
    const input = document.createElement('input');
    input.type = 'password';
    input.autocomplete = 'off';
    input.placeholder = placeholder;
    input.className = 'ds-input';
    input.style.cssText = 'width:100%;font-family:monospace';
    body.append(msg, input);

    const footer = document.createElement('div');
    footer.style.cssText = 'padding:12px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end;gap:8px';
    const cancelBtn = document.createElement('button');
    cancelBtn.type = 'button';
    cancelBtn.className = 'ds-btn ds-btn-ghost';
    cancelBtn.textContent = 'Cancel';
    const okBtn = document.createElement('button');
    okBtn.type = 'button';
    okBtn.className = 'ds-btn ds-btn-primary';
    okBtn.textContent = confirmLabel;

    footer.append(cancelBtn, okBtn);
    modal.append(header, body, footer);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);
    input.focus();

    const cleanup = (result) => {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
      resolve(result);
    };
    const submit = () => cleanup(input.value.trim() || null);
    const onKey = (e) => {
      if (e.key === 'Escape') cleanup(null);
      if (e.key === 'Enter') submit();
    };

    cancelBtn.addEventListener('click', () => cleanup(null));
    okBtn.addEventListener('click', submit);
    backdrop.addEventListener('click', (e) => {
      if (e.target === backdrop) cleanup(null);
    });
    document.addEventListener('keydown', onKey);
  });
}

/**
 * Show an inline prompt for a single-value reveal, e.g. a newly rotated
 * API key that the user must copy now. Displays the value in a
 * monospace readonly field with a copy button and an acknowledgement.
 * @param {string} title
 * @param {string} value
 * @param {string} [note]
 * @returns {Promise<void>}
 */
export function reveal(title, value, note) {
  return new Promise((resolve) => {
    const backdrop = document.createElement('div');
    backdrop.style.cssText = `
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.7);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 99999;
    `;

    const modal = document.createElement('div');
    modal.style.cssText = `
      background: var(--surface, #1a1d2e);
      border: 1px solid var(--border, #2a2f48);
      border-radius: 10px;
      max-width: 560px;
      width: 92%;
      padding: 0;
      box-shadow: 0 16px 48px rgba(0,0,0,0.6);
    `;

    const header = document.createElement('div');
    header.style.cssText = 'padding:16px 20px;border-bottom:1px solid var(--border)';
    const titleEl = document.createElement('h3');
    titleEl.style.cssText = 'margin:0;font-size:14px;font-weight:700;color:var(--text)';
    titleEl.textContent = title;
    header.appendChild(titleEl);

    const body = document.createElement('div');
    body.style.cssText = 'padding:16px 20px;display:flex;flex-direction:column;gap:12px';

    const noteEl = document.createElement('div');
    noteEl.style.cssText = 'font-size:12px;color:var(--warn,#f59e0b);line-height:1.5';
    noteEl.textContent = note || 'Save this value now — it will not be shown again.';

    const valWrap = document.createElement('div');
    valWrap.style.cssText = 'display:flex;gap:8px;align-items:stretch';

    const valInput = document.createElement('input');
    valInput.type = 'text';
    valInput.readOnly = true;
    valInput.value = value;
    valInput.className = 'ds-input';
    valInput.style.cssText = 'flex:1;font-family:var(--mono,monospace);font-size:12px';
    valInput.addEventListener('focus', () => valInput.select());

    const copyBtn = document.createElement('button');
    copyBtn.type = 'button';
    copyBtn.className = 'ds-btn ds-btn-ghost';
    copyBtn.textContent = 'Copy';
    copyBtn.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(value);
        copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
      } catch (_) {
        valInput.select();
        document.execCommand('copy');
        copyBtn.textContent = 'Copied!';
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1500);
      }
    });

    valWrap.append(valInput, copyBtn);
    body.append(noteEl, valWrap);

    const footer = document.createElement('div');
    footer.style.cssText = 'padding:12px 20px;border-top:1px solid var(--border);display:flex;justify-content:flex-end';
    const okBtn = document.createElement('button');
    okBtn.type = 'button';
    okBtn.className = 'ds-btn ds-btn-primary';
    okBtn.textContent = 'I saved it';
    footer.appendChild(okBtn);

    modal.append(header, body, footer);
    backdrop.appendChild(modal);
    document.body.appendChild(backdrop);
    valInput.focus();
    valInput.select();

    const cleanup = () => {
      document.removeEventListener('keydown', onKey);
      backdrop.remove();
      resolve();
    };
    const onKey = (e) => { if (e.key === 'Escape' || e.key === 'Enter') cleanup(); };
    okBtn.addEventListener('click', cleanup);
    backdrop.addEventListener('click', (e) => { if (e.target === backdrop) cleanup(); });
    document.addEventListener('keydown', onKey);
  });
}
