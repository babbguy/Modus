// ── Modus Dashboard v2 — Modal Manager ─────────────────────────
// Creates and manages modal dialogs using the ds-modal design system classes.

let _count = 0;

/**
 * Open a modal dialog.
 *
 * @param {Object}   opts
 * @param {string}   opts.title       - Modal header title
 * @param {string|function} opts.renderBody - HTML string or function(bodyEl)
 * @param {string}   [opts.maxWidth='680px']
 * @param {function} [opts.onClose]   - Called when modal is closed
 * @returns {string} Modal element id (for programmatic close)
 */
export function openModal({ title, renderBody, maxWidth = '680px', onClose }) {
  const id = 'modal-' + (++_count);

  // Backdrop
  const backdrop = document.createElement('div');
  backdrop.className = 'ds-modal-backdrop';
  backdrop.id = id;
  backdrop.style.display = 'flex';
  backdrop.onclick = (e) => {
    if (e.target === backdrop) closeModal(id, onClose);
  };

  // Modal container
  const modal = document.createElement('div');
  modal.className = 'ds-modal';
  modal.style.maxWidth = maxWidth;

  // Header
  modal.innerHTML = `
    <div class="ds-modal-header">
      <h3 style="margin:0;font-size:1.1rem;font-weight:700;color:var(--text)">${title}</h3>
      <button class="ds-modal-close" style="background:none;border:none;color:var(--muted);font-size:1.2rem;cursor:pointer;padding:4px 8px">&times;</button>
    </div>
    <div class="ds-modal-body"></div>`;

  // Close button handler
  modal.querySelector('.ds-modal-close').onclick = () => closeModal(id, onClose);

  // Render body
  const body = modal.querySelector('.ds-modal-body');
  if (typeof renderBody === 'function') {
    renderBody(body);
  } else {
    body.innerHTML = renderBody;
  }

  backdrop.appendChild(modal);
  document.body.appendChild(backdrop);

  // Escape key handler
  const escHandler = (e) => {
    if (e.key === 'Escape') {
      closeModal(id, onClose);
      document.removeEventListener('keydown', escHandler);
    }
  };
  document.addEventListener('keydown', escHandler);

  // Store handler ref for cleanup
  backdrop._escHandler = escHandler;

  return id;
}

/**
 * Close and remove a modal by id.
 * @param {string}   id
 * @param {function} [onClose] - Optional callback
 */
export function closeModal(id, onClose) {
  const el = document.getElementById(id);
  if (!el) return;

  // Clean up escape handler
  if (el._escHandler) {
    document.removeEventListener('keydown', el._escHandler);
  }

  el.remove();

  if (typeof onClose === 'function') {
    try { onClose(); } catch (e) { console.error('[modal] onClose error:', e); }
  }
}

/**
 * Close all open modals.
 */
export function closeAllModals() {
  document.querySelectorAll('.ds-modal-backdrop').forEach(el => {
    if (el._escHandler) document.removeEventListener('keydown', el._escHandler);
    el.remove();
  });
}
