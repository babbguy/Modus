// ── Modus Dashboard — Policy form model ────────────────────────
// Pure functions (no DOM, no imports) that map the New/Edit Policy form to the
// request bodies of the real API:
//
//   POST /api/v1/policies        PolicyCreate: name, description, scope,
//                                policy_type, effect, priority, team_id, app_id,
//                                conditions, config, action
//   PUT  /api/v1/policies/{id}   PolicyUpdate: name, description, effect,
//                                priority, conditions, config, action, is_active
//
// Keep POLICY_TYPES in step with VALID_POLICY_TYPES in orchestrator/api/policies.py
// (tests/test_policy_types_consistency.py checks this file too).

export const SCOPES = [
  { value: 'platform', label: 'Platform (all teams and apps)' },
  { value: 'team',     label: 'Team' },
  { value: 'app',      label: 'App' },
];

export const EFFECTS = [
  { value: 'deny',     label: 'Deny (block the call)' },
  { value: 'throttle', label: 'Throttle (ask the caller to retry later)' },
  { value: 'warn',     label: 'Warn (allow, raise an alert)' },
];

const PERIODS = [
  { value: 'hourly',  label: 'Hourly' },
  { value: 'daily',   label: 'Daily' },
  { value: 'monthly', label: 'Monthly' },
];

/**
 * Field kinds:
 *   money   decimal string, > 0, up to 10 integer + 8 fraction digits (sent as a string)
 *   int     positive integer
 *   number  number >= min
 *   select  one of options
 *   list    comma/newline separated strings, non-empty
 *   tiers   one tier per line: "<pct> <model>" or "<pct> deny"
 */
export const POLICY_TYPES = [
  {
    value: 'budget_cap', label: 'Budget cap',
    help: 'Denies calls once spend in the period reaches the cap.',
    fields: [
      { key: 'cap_usd', label: 'Cap (USD)', kind: 'money', required: true, placeholder: '100.00' },
      { key: 'period', label: 'Period', kind: 'select', options: PERIODS, required: true, default: 'monthly' },
    ],
  },
  {
    value: 'rate_limit', label: 'Rate limit',
    help: 'Limits the number of calls in a sliding window.',
    fields: [
      { key: 'max_calls', label: 'Max calls', kind: 'int', required: true, placeholder: '1000' },
      { key: 'window_seconds', label: 'Window (seconds)', kind: 'int', required: true, default: '60' },
    ],
  },
  {
    value: 'token_cap', label: 'Token cap',
    help: 'Denies calls once token usage in the period reaches the cap.',
    fields: [
      { key: 'max_tokens', label: 'Max tokens', kind: 'int', required: true, placeholder: '5000000' },
      { key: 'period', label: 'Period', kind: 'select', options: PERIODS, required: true, default: 'daily' },
    ],
  },
  {
    value: 'latency_cap', label: 'Latency cap',
    help: 'Fires when the rolling average latency exceeds the limit.',
    fields: [
      { key: 'max_ms', label: 'Max average latency (ms)', kind: 'int', required: true, placeholder: '2000' },
      { key: 'period', label: 'Window', kind: 'select', options: [{ value: '', label: 'Default (hourly)' }, ...PERIODS] },
    ],
  },
  {
    value: 'model_allowlist', label: 'Model allowlist',
    help: 'Denies any model that is not in the list.',
    fields: [
      { key: 'models', label: 'Allowed models (comma-separated)', kind: 'list', required: true, placeholder: 'gpt-4o, claude-sonnet-4-5' },
    ],
  },
  {
    value: 'model_denylist', label: 'Model denylist',
    help: 'Denies the listed models.',
    fields: [
      { key: 'models', label: 'Denied models (comma-separated)', kind: 'list', required: true, placeholder: 'gpt-4-turbo, claude-opus-4-1' },
    ],
  },
  {
    value: 'provider_block', label: 'Provider block',
    help: 'Denies calls to the listed providers.',
    fields: [
      { key: 'providers', label: 'Blocked providers (comma-separated)', kind: 'list', required: true, placeholder: 'openai, cohere' },
    ],
  },
  {
    value: 'environment_block', label: 'Environment block',
    help: 'Denies calls from the listed environments.',
    fields: [
      { key: 'environments', label: 'Blocked environments (comma-separated)', kind: 'list', required: true, placeholder: 'development, test' },
    ],
  },
  {
    value: 'degradation_ladder', label: 'Degradation ladder',
    help: 'Suggests cheaper models as the budget is consumed; a deny tier blocks calls.',
    fields: [
      { key: 'budget_usd', label: 'Budget (USD)', kind: 'money', required: true, placeholder: '1000.00' },
      { key: 'period', label: 'Period', kind: 'select', options: PERIODS, required: true, default: 'monthly' },
      {
        key: 'tiers', label: 'Tiers (one per line)', kind: 'tiers', required: true,
        placeholder: '70 claude-sonnet-4-5\n90 claude-haiku-4-5\n100 deny',
        help: 'Each line is a budget percentage followed by a model, or the word deny.',
      },
    ],
  },
  {
    value: 'amplification_gate', label: 'Amplification gate',
    help: 'Fires when the token amplification factor of recent calls is too high.',
    fields: [
      { key: 'max_amplification', label: 'Max amplification factor (>= 1)', kind: 'number', min: 1, required: true, default: '5' },
    ],
  },
  {
    value: 'retry_circuit_breaker', label: 'Retry circuit breaker',
    help: 'Denies calls when recent retries exceed the threshold.',
    fields: [
      { key: 'max_retries', label: 'Max retries', kind: 'int', required: true, default: '3' },
      { key: 'window_minutes', label: 'Window (minutes)', kind: 'int', default: '5' },
    ],
  },
];

const MONEY_RE = /^\d{1,10}(\.\d{1,8})?$/;

export function typeSpec(type) {
  return POLICY_TYPES.find(t => t.value === type) || null;
}

function splitList(text) {
  return String(text || '').split(/[\n,]/).map(s => s.trim()).filter(Boolean);
}

function parseTiers(text) {
  const tiers = [];
  const problems = [];
  String(text || '').split('\n').map(l => l.trim()).filter(Boolean).forEach((line, i) => {
    const m = line.match(/^(\d+(?:\.\d+)?)\s*%?\s+(\S+)$/);
    if (!m) { problems.push(`line ${i + 1}: use "<percent> <model>" or "<percent> deny"`); return; }
    const pct = Number(m[1]);
    if (pct < 0 || pct > 100) { problems.push(`line ${i + 1}: percent must be between 0 and 100`); return; }
    tiers.push(m[2].toLowerCase() === 'deny' ? { pct, action: 'deny' } : { pct, model: m[2] });
  });
  return { tiers, problems };
}

/**
 * Convert one field's raw form text into its config value.
 * @returns {{value?: any, error?: string, skip?: boolean}}
 */
function parseField(field, raw) {
  const text = String(raw ?? '').trim();
  if (text === '') {
    return field.required ? { error: `${field.label} is required` } : { skip: true };
  }
  switch (field.kind) {
    case 'money': {
      if (!MONEY_RE.test(text)) return { error: `${field.label} must be a decimal amount like 100.00` };
      if (!(Number(text) > 0)) return { error: `${field.label} must be greater than 0` };
      return { value: text };   // keep as a string: money is never a float
    }
    case 'int': {
      if (!/^\d+$/.test(text) || Number(text) < 1) return { error: `${field.label} must be a whole number of 1 or more` };
      return { value: Number(text) };
    }
    case 'number': {
      const n = Number(text);
      if (!Number.isFinite(n) || n < (field.min ?? -Infinity)) {
        return { error: `${field.label} must be a number${field.min != null ? ' >= ' + field.min : ''}` };
      }
      return { value: n };
    }
    case 'select': {
      if (!(field.options || []).some(o => o.value === text)) return { error: `${field.label} has an invalid value` };
      return { value: text };
    }
    case 'list': {
      const items = splitList(text);
      return items.length ? { value: items } : { error: `${field.label} needs at least one entry` };
    }
    case 'tiers': {
      const { tiers, problems } = parseTiers(text);
      if (problems.length) return { error: `${field.label}: ${problems.join('; ')}` };
      return tiers.length ? { value: tiers } : { error: `${field.label} is required` };
    }
    default:
      return { value: text };
  }
}

/** Build the `config` object for a type from raw field text keyed by field key. */
export function buildConfig(type, raw) {
  const spec = typeSpec(type);
  const errors = [];
  const config = {};
  if (!spec) return { config, errors: [`Unknown policy type "${type}"`] };
  for (const field of spec.fields) {
    const r = parseField(field, (raw || {})[field.key]);
    if (r.error) errors.push(r.error);
    else if (!r.skip) config[field.key] = r.value;
  }
  return { config, errors };
}

/**
 * Convert a stored config back into raw form text (for the Edit form).
 * @returns {Object<string,string>}
 */
export function configToFormValues(type, config) {
  const spec = typeSpec(type);
  const cfg = config || {};
  const out = {};
  if (!spec) return out;
  for (const field of spec.fields) {
    const v = cfg[field.key];
    if (v === undefined || v === null) { out[field.key] = field.default ?? ''; continue; }
    if (field.kind === 'list') out[field.key] = v.join(', ');
    else if (field.kind === 'tiers') {
      out[field.key] = v.map(t => `${t.pct} ${t.action === 'deny' ? 'deny' : (t.model || '')}`).join('\n');
    } else out[field.key] = String(v);
  }
  return out;
}

/**
 * Validate the form and build the request body.
 *
 * @param {Object} form
 *   name, description, policy_type, scope, effect, priority, team_id, app_id,
 *   message (action message), is_active, config: {fieldKey: rawText}
 * @param {Object} [opts]
 * @param {'create'|'edit'} [opts.mode='create']
 * @param {Object} [opts.existing]  the policy being edited (to preserve config/action keys the form does not edit)
 * @returns {{payload: Object|null, errors: string[]}}
 */
export function buildPolicyPayload(form, { mode = 'create', existing = null } = {}) {
  const errors = [];
  const name = String(form.name || '').trim();
  if (!name) errors.push('Policy name is required');
  if (name.length > 256) errors.push('Policy name is at most 256 characters');

  const priority = Number(form.priority === '' || form.priority == null ? 100 : form.priority);
  if (!Number.isInteger(priority) || priority < 1 || priority > 999) {
    errors.push('Priority must be a whole number from 1 to 999 (lower runs first)');
  }

  const effect = form.effect || 'deny';
  if (!EFFECTS.some(e => e.value === effect)) errors.push('Choose an effect');

  const type = mode === 'edit' && existing ? existing.policy_type : form.policy_type;
  if (!typeSpec(type)) errors.push('Choose a policy type');

  const { config, errors: configErrors } = buildConfig(type, form.config);
  errors.push(...configErrors);

  // Action: free-text message shown to the caller; keep other stored keys.
  const action = { ...((mode === 'edit' && existing && existing.action) || {}) };
  const message = String(form.message || '').trim();
  if (message) action.message = message; else delete action.message;

  const description = String(form.description || '').trim();

  if (mode === 'edit') {
    if (errors.length) return { payload: null, errors };
    // Keep config keys the form does not manage; the form-managed ones are
    // replaced (so clearing an optional field really clears it).
    const managed = new Set((typeSpec(type) || { fields: [] }).fields.map(f => f.key));
    const kept = Object.fromEntries(
      Object.entries((existing && existing.config) || {}).filter(([k]) => !managed.has(k))
    );
    return {
      payload: {
        name,
        description,
        effect,
        priority,
        config: { ...kept, ...config },
        action,
        is_active: form.is_active !== false,
      },
      errors,
    };
  }

  const scope = form.scope || 'team';
  if (!SCOPES.some(s => s.value === scope)) errors.push('Choose a scope');
  const teamId = String(form.team_id || '').trim();
  const appId = String(form.app_id || '').trim();
  if ((scope === 'team' || scope === 'app') && !teamId) errors.push('Choose a team for this scope');
  if (scope === 'app' && !appId) errors.push('Choose an app for app scope');

  if (errors.length) return { payload: null, errors };

  const payload = {
    name,
    scope,
    policy_type: type,
    effect,
    priority,
    config,
  };
  if (description) payload.description = description;
  if (scope === 'team' || scope === 'app') payload.team_id = teamId;
  if (scope === 'app') payload.app_id = appId;
  if (Object.keys(action).length) payload.action = action;
  return { payload, errors };
}

/**
 * Turn an API error body into one readable line.
 * Handles FastAPI shapes: {detail: "text"}, {detail: {message, errors}},
 * and 422 validation lists {detail: [{loc, msg}]}.
 */
export function formatApiError(body, status) {
  const d = body && body.detail;
  if (typeof d === 'string') return d;
  if (Array.isArray(d)) {
    return d.map(e => {
      const where = Array.isArray(e.loc) ? e.loc.filter(x => x !== 'body').join('.') : '';
      return where ? `${where}: ${e.msg}` : (e.msg || JSON.stringify(e));
    }).join('; ');
  }
  if (d && typeof d === 'object') {
    const list = Array.isArray(d.errors) ? ` ${d.errors.join('; ')}` : '';
    return `${d.message || 'Request rejected.'}${list}`;
  }
  return `Request failed (HTTP ${status})`;
}
