/**
 * Modus Dashboard v2 -- Global LLM Assistant
 * Sparkle icon in the topbar that opens a chat modal.
 * Sends messages to /api/v1/assistant/chat using the customer's configured LLM.
 */

import { openModal, closeModal } from './modal.js';
import { esc, headers } from './api.js';
import { get } from './state.js';

let _modalId = null;
let _messages = []; // session chat history
let _thinking = false;

// ── Public init ──────────────────────────────────────────────────────────────

export function initAssistant() {
  const btn = document.getElementById('assistant-btn');
  if (btn) btn.addEventListener('click', toggleAssistant);
}

// ── Toggle ───────────────────────────────────────────────────────────────────

function toggleAssistant() {
  if (_modalId) {
    closeModal(_modalId);
    _modalId = null;
    return;
  }
  _modalId = openModal({
    title: 'AI Assistant',
    maxWidth: '640px',
    renderBody: (body) => _renderChat(body),
    onClose: () => { _modalId = null; },
  });
}

// ── Chat renderer ────────────────────────────────────────────────────────────

function _renderChat(body) {
  body.style.cssText = 'display:flex;flex-direction:column;padding:0;height:480px;';

  // Context indicator
  const view = get('currentViewMode') || 'overview';
  const ctxLabel = view.charAt(0).toUpperCase() + view.slice(1);

  body.innerHTML = `
    <div class="assistant-context">
      <i class="fa-solid fa-location-dot" style="font-size:10px"></i>
      Context: ${esc(ctxLabel)}
    </div>
    <div class="assistant-messages" id="assistant-messages"></div>
    <div class="assistant-input-bar">
      <input type="text" id="assistant-input" class="assistant-input"
             placeholder="Ask about your data..." autocomplete="off" />
      <button class="assistant-send-btn" id="assistant-send-btn" title="Send">
        <i class="fa-solid fa-paper-plane"></i>
      </button>
    </div>`;

  _renderMessages();

  const input = body.querySelector('#assistant-input');
  const sendBtn = body.querySelector('#assistant-send-btn');

  sendBtn.addEventListener('click', () => _sendMessage(input));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      _sendMessage(input);
    }
  });

  // Auto-focus
  requestAnimationFrame(() => input.focus());
}

// ── Message list ─────────────────────────────────────────────────────────────

function _renderMessages() {
  const container = document.getElementById('assistant-messages');
  if (!container) return;

  if (_messages.length === 0 && !_thinking) {
    container.innerHTML = `
      <div class="assistant-empty">
        <i class="fa-solid fa-sparkles" style="font-size:28px;color:var(--accent);margin-bottom:12px"></i>
        <div style="font-weight:600;color:var(--text);margin-bottom:4px">Ask anything about your data</div>
        <div style="font-size:12px;color:var(--muted)">
          Cost breakdowns, policy compliance, spending trends, and more.
        </div>
      </div>`;
    return;
  }

  let html = '';
  for (const msg of _messages) {
    const isUser = msg.role === 'user';
    const cls = isUser ? 'assistant-msg user' : 'assistant-msg bot';
    html += `<div class="${cls}">${_formatContent(msg.content)}</div>`;
  }

  if (_thinking) {
    html += `
      <div class="assistant-msg bot assistant-thinking">
        <span class="assistant-dot"></span>
        <span class="assistant-dot"></span>
        <span class="assistant-dot"></span>
      </div>`;
  }

  container.innerHTML = html;
  container.scrollTop = container.scrollHeight;
}

function _formatContent(text) {
  // Basic markdown-like formatting: bold, code, newlines
  return esc(text)
    .replace(/\*\*(.*?)\*\*/g, '<strong>$1</strong>')
    .replace(/`(.*?)`/g, '<code style="background:var(--surface);padding:1px 4px;border-radius:3px;font-size:0.9em">$1</code>')
    .replace(/\n/g, '<br>');
}

// ── Send message ─────────────────────────────────────────────────────────────

async function _sendMessage(input) {
  const message = input.value.trim();
  if (!message || _thinking) return;

  _messages.push({ role: 'user', content: message });
  input.value = '';
  _setThinking(true);

  try {
    const resp = await fetch('/api/v1/assistant/chat', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({
        message,
        context_view: get('currentViewMode') || 'overview',
        context_data: null,
      }),
    });
    const data = await resp.json();
    if (resp.ok) {
      _messages.push({ role: 'assistant', content: data.response || 'No response.' });
    } else {
      _messages.push({ role: 'assistant', content: data.detail || 'Error from assistant.' });
    }
  } catch (_e) {
    _messages.push({
      role: 'assistant',
      content: 'Failed to reach AI assistant. Check Settings > LLM Configuration.',
    });
  }

  _setThinking(false);
}

function _setThinking(val) {
  _thinking = val;
  _renderMessages();
  // Disable/enable input
  const input = document.getElementById('assistant-input');
  const btn = document.getElementById('assistant-send-btn');
  if (input) input.disabled = val;
  if (btn) btn.disabled = val;
}
