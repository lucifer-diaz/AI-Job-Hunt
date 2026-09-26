/* Settings → AI Models: LiteLLM connection, model registry, tiers, worker assignments, routing, preview/apply,
   version history. Loaded after settings.js and before app.js; it plugs into settings' renderer and action tables.
   Every value comes from /api/settings/ai. Edits change the draft; nothing reaches a worker until Apply. */
'use strict';
const AIM_TABS = [['overview', 'Overview'], ['workers', 'Workers'], ['models', 'Models'], ['routing', 'Routing'], ['litellm', 'LiteLLM'], ['advanced', 'Advanced']];
const AIM_MODES = { FIXED: 'Fixed', TIER: 'Tier', AUTO_ROUTER: 'Auto Router' };
const AIM_CLASSIFIERS = { DISABLED: 'Disabled', HEURISTIC: 'Heuristic', HEURISTIC_V2: 'Heuristic v2 (trained)', LLM: 'LLM classifier', CUSTOM: 'Custom Job Hunt classifier' };
const AIM_CONDITIONS = { LOW_CONFIDENCE: 'Confidence below threshold', SCHEMA_FAILURE: 'Structured-output validation failed', AMBIGUOUS: 'Ambiguity detected', FATAL_REQUIREMENT_UNCERTAIN: 'Fatal requirement uncertain', MANUAL: 'Manual request' };
const AIM_SYNC = { SYNCED: ['Synced', 'good'], PENDING: ['Pending', 'warn'], APPLYING: ['Applying', 'brand'], OUT_OF_SYNC: ['Out of sync', 'bad'], FAILED: ['Failed', 'bad'] };
const AIM_CONN = { CONNECTED: ['Connected', 'good'], DISCONNECTED: ['Disconnected', 'muted'], AUTH_FAILED: ['Authentication failed', 'bad'], UNAVAILABLE: ['LiteLLM unavailable', 'bad'], UNKNOWN: ['Not tested', 'muted'] };
const AIM = { tab: 'overview', data: undefined, loading: false, error: '', drawer: null, preview: null, runtime: null, versions: null, audit: null, tests: {}, ollama: null, adding: false };

const aimData = () => AIM.data || {};
const aimRev = () => aimData().connection?.draft_revision;
const aimModels = () => aimData().models || [];
const aimModel = name => aimModels().find(m => m.name === name);
const aimTokens = n => n == null ? 'unknown' : n >= 1000 ? `${Math.round(n / 1024 * 10) / 10}K`.replace('.0K', 'K') : String(n);
const aimYes = v => v === true ? 'yes' : v === false ? 'no' : 'unknown';
const aimOpt = (value, label, selected) => `<option value="${attr(value)}" ${String(value) === String(selected ?? '') ? 'selected' : ''}>${esc(label)}</option>`;
function aimModelOptions(selected, blank = 'Not set') {
  const names = aimModels().filter(m => m.enabled).map(m => m.name);
  if (selected && !names.includes(selected)) names.push(selected);
  return aimOpt('', blank, selected) + names.map(n => aimOpt(n, n, selected)).join('');
}
function aimTierOptions(selected) { return aimOpt('', 'Not set', selected) + Object.keys(aimData().tiers || {}).map(t => aimOpt(t, t, selected)).join(''); }

async function aimLoad() {
  AIM.loading = true; AIM.error = '';
  try { AIM.data = await api('/api/settings/ai'); }
  catch (err) { AIM.error = err.message; }
  finally { AIM.loading = false; render(); }
}
// Run a change, then reload the overview so every tab shows the saved draft and its new revision.
// A 409 means someone else changed the draft: reload so the next attempt starts from what is saved now.
async function aimRun(key, fn, ok) {
  return busy(key, async () => {
    try { const out = await fn(); AIM.preview = null; await aimLoad(); return out; }
    catch (err) { if (err.status === 409) await aimLoad(); throw err; }
  }, ok);
}

/* ───────────── shell ───────────── */
function renderAIModels() {
  if (AIM.data === undefined && !AIM.loading && !AIM.error) { aimLoad(); return loading(); }
  if (AIM.error && !AIM.data) return `<div class="settings-error" role="alert">AI model settings could not be loaded: ${esc(AIM.error)}</div>${btn('Try again', 'aim-reload', {}, 'mt16')}`;
  if (!AIM.data) return loading();
  const tab = AIM_TABS.some(([id]) => id === AIM.tab) ? AIM.tab : 'overview';
  const body = { overview: aimOverview, workers: aimWorkers, models: aimModelsTab, routing: aimRouting, litellm: aimLiteLLM, advanced: aimAdvanced }[tab]();
  return `${aimChangeBar()}<div class="tabs under aim-tabs" role="tablist" aria-label="AI model settings">${AIM_TABS.map(([id, name]) => `<button type="button" role="tab" aria-selected="${id === tab}" class="${id === tab ? 'on' : ''}" data-act="aim-tab" data-tab="${id}">${name}</button>`).join('')}</div><div class="aim-body" role="tabpanel">${body}</div>${AIM.preview ? aimPreviewPanel() : ''}${AIM.drawer ? aimDrawer() : ''}`;
}
function aimChangeBar() {
  const d = aimData(), pending = (d.pending_changes || []).length, conn = d.connection || {};
  const [syncLabel, tone] = AIM_SYNC[pending ? 'PENDING' : conn.sync_status] || AIM_SYNC.SYNCED;
  return `<div class="aim-bar"><div class="row gap8 wrap">${pill(d.gateway_enabled ? 'LiteLLM gateway on' : `Direct provider: ${d.llm_provider || 'unknown'}`, d.gateway_enabled ? 'good' : 'muted')}${pill(`Active v${d.active_version ?? 0}`, 'brand')}${pill(syncLabel, tone)}<span class="settings-help">${pending ? `${pending} pending change${pending === 1 ? '' : 's'} — nothing reaches a worker until you apply.` : 'The active configuration matches your settings.'}</span></div><div class="row gap8">${btn(isBusy('aim-preview') ? 'Checking…' : 'Preview changes', 'aim-preview', {}, 'sm', !pending || isBusy('aim-preview'))}</div></div>`;
}

/* ───────────── overview ───────────── */
function aimOverview() {
  const d = aimData(), conn = d.connection || {}, [connLabel, connTone] = AIM_CONN[conn.status] || AIM_CONN.UNKNOWN;
  const rows = (d.workers || []).map(w => `<tr class="click" data-act="aim-worker" data-worker="${attr(w.worker_type)}"><td>${esc(w.label)}</td><td class="mono">${esc(w.resolved?.model || '—')}</td><td>${esc(AIM_MODES[w.config?.mode] || '—')}${w.resolved?.note ? ` <span class="settings-help">(${esc(w.resolved.note)})</span>` : ''}</td></tr>`).join('');
  return settingsGroup('Gateway', 'Where every AI worker sends its requests.', `<div class="aim-cards"><div class="aim-card"><strong>${d.gateway_enabled ? 'LiteLLM gateway' : 'Direct provider'}</strong><p>${d.gateway_enabled ? 'Workers use the models configured here, through LiteLLM. Nothing calls Ollama or a provider directly.' : 'Workers call the provider chosen under Advanced → Direct providers. The configuration here is kept ready.'}</p>${btn(d.gateway_enabled ? 'Turn gateway off' : 'Use LiteLLM gateway', 'aim-gateway', { on: d.gateway_enabled ? '' : '1' }, 'sm', isBusy('aim-gateway'))}</div><div class="aim-card"><strong>LiteLLM</strong><p>${pill(connLabel, connTone)} ${conn.litellm_version ? `v${esc(conn.litellm_version)}` : ''}</p><p class="settings-help">${conn.base_url ? esc(conn.base_url) : 'No base URL yet'}${conn.last_successful_sync_at ? ` · models synced ${esc(fmtDateTime(conn.last_successful_sync_at))}` : ''}</p></div><div class="aim-card"><strong>Configuration</strong><p>Active version v${d.active_version ?? 0}</p><p class="settings-help">${conn.last_sync_at ? `Last applied ${esc(fmtDateTime(conn.last_sync_at))}` : 'Not applied yet'}${conn.last_sync_error ? ` · ${esc(conn.last_sync_error)}` : ''}</p></div></div>`) +
    settingsGroup('Workers', 'Which model each worker uses under your current settings. Select one to change it.', `<div class="mon-table-wrap"><table class="mon-table"><thead><tr><th>Worker</th><th>Model</th><th>Mode</th></tr></thead><tbody>${rows}</tbody></table></div>`) +
    settingsGroup('Desired and active', 'Compare your settings with what workers run now and what LiteLLM serves.', `${btn(isBusy('aim-runtime') ? 'Checking…' : 'Check runtime', 'aim-runtime', {}, 'sm', isBusy('aim-runtime'))}${AIM.runtime ? aimRuntimeTable() : ''}`);
}
function aimRuntimeTable() {
  const r = AIM.runtime;
  return `<p class="settings-help">${r.live_error ? `LiteLLM could not be read: ${esc(r.live_error)}` : 'LiteLLM was read just now.'}</p><div class="mon-table-wrap"><table class="mon-table"><thead><tr><th>Worker</th><th>Desired</th><th>Active</th><th>Status</th></tr></thead><tbody>${(r.workers || []).map(w => { const [text, tone] = AIM_SYNC[w.state] || AIM_SYNC.SYNCED; return `<tr><td>${esc(w.label)}</td><td class="mono">${esc(w.desired || '—')}</td><td class="mono">${esc(w.active || '—')}</td><td>${pill(w.state === 'SYNCED' ? 'Synced ✓' : text, tone)}</td></tr>`; }).join('')}</tbody></table></div>`;
}

/* ───────────── workers ───────────── */
function aimWorkers() {
  const rows = (aimData().workers || []).map(w => {
    const c = w.config || {}, test = AIM.tests[`worker:${w.worker_type}`];
    return `<tr><td><strong>${esc(w.label)}</strong>${w.has_prompt ? '' : ' <span class="settings-help">(reserved)</span>'}<div class="settings-help">${esc(w.description || '')}</div></td><td class="mono">${esc(w.resolved?.model || '—')}</td><td>${esc(AIM_MODES[c.mode] || '—')}${c.mode === 'TIER' || c.mode === 'AUTO_ROUTER' ? ` · ${esc(c.tier || '—')}` : ''}</td><td class="mono">${esc(c.fallback_model || '—')}</td><td>${w.structured_output_required ? pill('Structured output required', 'brand') : ''}${c.enabled === false ? pill('Off', 'bad') : ''}</td><td><div class="row gap4">${btn('Edit', 'aim-worker', { worker: w.worker_type }, 'sm')}${btn(isBusy(`aim-wtest-${w.worker_type}`) ? 'Testing…' : 'Test', 'aim-worker-test', { worker: w.worker_type }, 'sm', isBusy(`aim-wtest-${w.worker_type}`))}</div>${test ? aimTestResult(test) : ''}</td></tr>`;
  }).join('');
  return settingsGroup('Worker model assignments', 'Workers reference a model, a tier or the auto router — never a provider model name in code. Edits take effect after Apply.', `<div class="mon-table-wrap aim-wide"><table class="mon-table"><thead><tr><th>Worker</th><th>Model</th><th>Mode</th><th>Fallback</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`);
}
function aimTestResult(t) {
  if (t.error && !t.success) return `<div class="settings-error aim-test">${esc(t.error)}</div>`;
  const r = t.routing || {};
  const bits = [`model ${t.answered_by || t.underlying_model || t.model || '?'}`, r.model_group ? `group ${r.model_group}` : '', r.routing_tier ? `tier ${r.routing_tier}` : '', t.latency_ms != null ? `${t.latency_ms} ms` : '', t.input_tokens != null ? `${t.input_tokens}+${t.output_tokens ?? 0} tokens` : '', t.schema_ok === true || t.structured_output === true ? 'schema ✓' : t.schema_ok === false || t.structured_output === false ? 'schema ✗' : '', r.escalated ? `escalated (${r.escalation_reason})` : ''].filter(Boolean);
  return `<div class="settings-help aim-test" role="status">${t.success ? '✓' : '✗'} ${esc(bits.join(' · '))}</div>`;
}
function aimWorkerBaseline(w) {
  const c = w.config || {}, e = c.escalation || {};
  return { model_mode: c.mode || 'TIER', primary_model: c.primary_model || '', tier_name: c.tier || '', fallback_model: c.fallback_model || '', temperature: c.temperature ?? '', max_output_tokens: c.max_output_tokens ?? '', context_window: c.context_window ?? '', reasoning_effort: c.reasoning_effort || '', timeout_seconds: c.timeout_seconds ?? '', retry_count: c.retry_count ?? '', max_concurrency: c.max_concurrency ?? '', use_structured_output: c.use_structured_output !== false, enabled: c.enabled !== false, esc_target: e.model ? `model:${e.model}` : e.tier ? `tier:${e.tier}` : '', esc_threshold: e.confidence_threshold ?? '', ...Object.fromEntries(Object.keys(AIM_CONDITIONS).map(k => [`esc_${k}`, (e.conditions || []).includes(k)])) };
}
// The model a worker's drawer currently points at, so only options that model supports are shown.
function aimDrawerModel(v) {
  const d = aimData();
  if (v.model_mode === 'FIXED') return v.primary_model;
  if (v.model_mode === 'AUTO_ROUTER' && d.routing?.auto_router_enabled) return d.routing.auto_router_model_name;
  return d.tiers?.[v.tier_name]?.model || '';
}
function aimDrawerValues(w) { const base = aimWorkerBaseline(w); return Object.fromEntries(Object.keys(base).map(k => [k, fv('aiw', k, base[k])])); }
function aimWarnings(w, v) {
  const m = aimModel(aimDrawerModel(v)), out = [];
  if (!m) return out;
  if (w.structured_output_required && m.supports_structured_output === false) out.push(`${m.name} cannot return structured output, which ${w.label} requires.`);
  else if (w.structured_output_required && m.supports_structured_output == null && m.model_type !== 'AUTO_ROUTER') out.push(`Structured Output Required: it is unknown whether ${m.name} returns JSON reliably.`);
  if (m.context_window && m.context_window < w.min_context_tokens) out.push(`This model may not satisfy ${w.label} requirements (${aimTokens(m.context_window)} context; ${aimTokens(w.min_context_tokens)} recommended).`);
  if (v.context_window && m.context_window && Number(v.context_window) > m.context_window) out.push(`Context ${aimTokens(Number(v.context_window))} exceeds the ${aimTokens(m.context_window)} ${m.name} runs with.`);
  if (v.fallback_model && v.fallback_model === m.name) out.push('The fallback cannot be the primary model.');
  return out;
}
function aimDrawer() {
  const w = (aimData().workers || []).find(x => x.worker_type === AIM.drawer);
  if (!w) return '';
  const v = aimDrawerValues(w), model = aimModel(aimDrawerModel(v)), d = aimData(), warnings = aimWarnings(w, v);
  const f = (key, label, extra = '', help = '') => `<label class="field" for="aiw-${key}"><span>${label}</span>${inputF('aiw', key, v[key], extra)}${help ? `<span class="settings-help">${help}</span>` : ''}</label>`;
  const target = v.model_mode === 'FIXED' ? `<label class="field" for="aiw-primary_model"><span>Primary model</span><select class="select" id="aiw-primary_model" data-form="aiw" data-field="primary_model" data-live="1">${aimModelOptions(v.primary_model, 'Choose a model')}</select></label>`
    : `<label class="field" for="aiw-tier_name"><span>${v.model_mode === 'AUTO_ROUTER' ? 'Tier while the auto router is off' : 'Tier'}</span><select class="select" id="aiw-tier_name" data-form="aiw" data-field="tier_name" data-live="1">${aimTierOptions(v.tier_name)}</select></label>`;
  const router = v.model_mode === 'AUTO_ROUTER' ? `<p class="settings-help">Auto Router: ${d.routing?.auto_router_enabled ? `on — requests go to <span class="mono">${esc(d.routing.auto_router_model_name)}</span>` : 'off — the tier above is used until it is turned on (Routing tab)'}.</p>` : '';
  const reasoning = model?.supports_reasoning === true ? `<label class="field" for="aiw-reasoning_effort"><span>Reasoning effort</span><select class="select" id="aiw-reasoning_effort" data-form="aiw" data-field="reasoning_effort">${aimOpt('', 'Model default', v.reasoning_effort)}${['low', 'medium', 'high'].map(x => aimOpt(x, x.toUpperCase(), v.reasoning_effort)).join('')}</select></label>` : `<p class="settings-help">Reasoning effort: ${model ? (model.supports_reasoning === false ? `${esc(model.name)} does not support it.` : `unknown whether ${esc(model.name)} supports it, so it is not offered.`) : 'choose a model first.'}</p>`;
  const conditions = Object.entries(AIM_CONDITIONS).map(([k, label]) => checkF('aiw', `esc_${k}`, v[`esc_${k}`], label)).join('');
  const escTargets = aimOpt('', 'No escalation', v.esc_target) + Object.keys(d.tiers || {}).map(t => aimOpt(`tier:${t}`, `Tier ${t}`, v.esc_target)).join('') + aimModels().filter(m => m.enabled).map(m => aimOpt(`model:${m.name}`, m.name, v.esc_target)).join('');
  return `<aside class="aim-drawer" role="dialog" aria-modal="false" aria-labelledby="aim-drawer-title"><header class="row between"><h3 id="aim-drawer-title">${esc(w.label)}</h3>${btn('Close', 'aim-drawer-close', {}, 'sm')}</header><p class="settings-help">${esc(w.description || '')}${w.structured_output_required ? ' · <strong>Structured Output Required</strong>' : ''}</p>
    ${warnings.length ? `<div class="settings-notice" role="status">${warnings.map(x => `<p>${esc(x)}</p>`).join('')}</div>` : ''}
    <div class="settings-grid"><label class="field" for="aiw-model_mode"><span>Model mode</span><select class="select" id="aiw-model_mode" data-form="aiw" data-field="model_mode" data-live="1">${Object.entries(AIM_MODES).map(([k, t]) => aimOpt(k, t, v.model_mode)).join('')}</select></label>${target}
    <label class="field" for="aiw-fallback_model"><span>Fallback model (infrastructure)</span><select class="select" id="aiw-fallback_model" data-form="aiw" data-field="fallback_model" data-live="1">${aimModelOptions(v.fallback_model, 'No fallback')}</select><span class="settings-help">Used by LiteLLM on timeouts, rate limits and outages only.</span></label></div>${router}
    <div class="settings-grid">${f('temperature', 'Temperature', 'type="number" min="0" max="2" step="0.1" placeholder="Model default"')}${f('max_output_tokens', 'Max output tokens', `type="number" min="1" max="${model?.max_output_tokens || 200000}" placeholder="Model default"`)}${f('context_window', 'Context window (tokens)', `type="number" min="512" placeholder="${attr(model?.context_window ? `Model: ${model.context_window}` : 'Model default')}" data-live="1"`)}${reasoning}${f('timeout_seconds', 'Timeout (seconds)', 'type="number" min="1" max="3600" placeholder="Server default"')}${f('retry_count', 'Retries', 'type="number" min="0" max="10" placeholder="Server default"')}${f('max_concurrency', 'Maximum concurrency', 'type="number" min="1" max="100" placeholder="No limit"')}</div>
    <div class="settings-checks">${checkF('aiw', 'use_structured_output', v.use_structured_output, 'Structured output (JSON mode)')}${checkF('aiw', 'enabled', v.enabled, 'Worker enabled')}</div>
    ${settingsDetails('aim-escalation', 'Quality escalation', `<p class="settings-help">Separate from the fallback above: when an answer is not good enough, Job Hunt OS asks a stronger model once.</p><div class="settings-grid"><label class="field" for="aiw-esc_target"><span>Escalate to</span><select class="select" id="aiw-esc_target" data-form="aiw" data-field="esc_target">${escTargets}</select></label>${f('esc_threshold', 'Confidence threshold', 'type="number" min="0" max="1" step="0.05" placeholder="e.g. 0.85"')}</div><div class="settings-checks">${conditions}</div>`)}
    <footer class="row gap8 mt14">${btn(isBusy('aim-worker-save') ? 'Saving…' : 'Save to draft', 'aim-worker-save', { worker: w.worker_type }, 'primary', isBusy('aim-worker-save'))}${btn(isBusy(`aim-wtest-${w.worker_type}`) ? 'Testing…' : 'Test with draft', 'aim-worker-test', { worker: w.worker_type }, '', isBusy(`aim-wtest-${w.worker_type}`))}</footer>${AIM.tests[`worker:${w.worker_type}`] ? aimTestResult(AIM.tests[`worker:${w.worker_type}`]) : ''}</aside>`;
}
function aimWorkerPayload(w) {
  const v = aimDrawerValues(w), num = x => x === '' || x == null ? null : Number(x);
  const [kind, name] = String(v.esc_target || '').split(/:(.*)/s);
  const conditions = Object.keys(AIM_CONDITIONS).filter(k => v[`esc_${k}`]);
  return { revision: aimRev(), model_mode: v.model_mode, primary_model: v.primary_model || null, tier_name: v.tier_name || null, fallback_model: v.fallback_model || null, temperature: num(v.temperature), max_output_tokens: num(v.max_output_tokens), context_window: num(v.context_window), reasoning_effort: v.reasoning_effort || null, timeout_seconds: num(v.timeout_seconds), retry_count: num(v.retry_count), max_concurrency: num(v.max_concurrency), use_structured_output: !!v.use_structured_output, enabled: !!v.enabled,
    escalation: kind && conditions.length ? { model: kind === 'model' ? name : null, tier: kind === 'tier' ? name : null, conditions, confidence_threshold: num(v.esc_threshold) } : null };
}

/* ───────────── models ───────────── */
function aimModelsTab() {
  const d = aimData(), rows = aimModels().map(m => {
    const test = AIM.tests[`model:${m.name}`], health = m.health_status === 'HEALTHY' ? pill('Healthy', 'good') : m.health_status === 'UNHEALTHY' ? pill('Unhealthy', 'bad') : pill('Unknown', 'muted');
    const caps = [['JSON', m.supports_structured_output], ['tools', m.supports_tools], ['reasoning', m.supports_reasoning], ['vision', m.supports_vision]].map(([n, v]) => `<span class="aim-cap ${v === true ? 'yes' : v === false ? 'no' : ''}" title="${attr(`${n}: ${aimYes(v)}`)}">${n}${v === true ? ' ✓' : v === false ? ' ✗' : ' ?'}</span>`).join('');
    return `<tr><td><strong class="mono">${esc(m.name)}</strong>${m.managed ? ' ' + pill('Managed', 'brand') : ''}${m.present_in_litellm ? '' : ' ' + pill('Not in LiteLLM', 'warn')}<div class="settings-help">${esc(m.provider || 'provider unknown')} · ${esc(m.underlying_model || 'model unknown')}</div></td><td>${esc(aimTokens(m.context_window))}${m.max_context_window ? ` <span class="settings-help">/ ${esc(aimTokens(m.max_context_window))} max</span>` : ''}</td><td><div class="row gap4 wrap">${caps}</div></td><td>${health}${m.health_latency_ms != null ? ` <span class="settings-help">${m.health_latency_ms} ms</span>` : ''}</td><td class="settings-help">${esc((m.used_by || []).join(', ') || '—')}</td><td><select class="select aim-inline" aria-label="Type of ${attr(m.name)}" data-act-change="aim-model-type" data-name="${attr(m.name)}">${(d.options?.model_types || []).map(t => aimOpt(t, t.replace('_', ' ').toLowerCase(), m.model_type)).join('')}</select></td><td><div class="row gap4">${btn(isBusy(`aim-mtest-${m.name}`) ? 'Testing…' : 'Test', 'aim-model-test', { name: m.name }, 'sm', isBusy(`aim-mtest-${m.name}`))}${btn(m.enabled ? 'Disable' : 'Enable', 'aim-model-toggle', { name: m.name, on: m.enabled ? '' : '1' }, 'sm')}${m.managed ? btn('Stop managing', 'aim-model-unmanage', { name: m.name }, 'sm') : ''}</div>${test ? aimTestResult(test) : ''}</td></tr>`;
  }).join('');
  return settingsGroup('Available models', 'Discovered from LiteLLM. Values LiteLLM or Ollama do not report stay unknown.', `<div class="row gap8 mb10">${btn(isBusy('aim-sync') ? 'Syncing…' : 'Sync models from LiteLLM', 'aim-sync', {}, 'primary sm', isBusy('aim-sync'))}${btn(AIM.adding ? 'Cancel' : 'Add local Ollama model', 'aim-add-toggle', {}, 'sm')}</div>${AIM.adding ? aimAddForm() : ''}${rows ? `<div class="mon-table-wrap aim-wide"><table class="mon-table"><thead><tr><th>Model</th><th>Context</th><th>Capabilities</th><th>Health</th><th>Used by</th><th>Type</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="settings-empty"><strong>No models yet</strong><p>Connect LiteLLM, then sync to discover its models.</p></div>'}`);
}
function aimAddForm() {
  const o = AIM.ollama, local = (o?.models || []).map(m => aimOpt(m.name, `${m.name}${m.context_length ? ` · ${aimTokens(m.context_length)} ctx` : ''}`, fv('aim-add', 'ollama_model', ''))).join('');
  return `<div class="settings-group aim-add"><p class="settings-help">Job Hunt OS deploys this model into LiteLLM on the next Apply (needs a sync mode under LiteLLM).${o && !o.reachable ? ` Ollama at ${esc(o.api_base)} did not answer; type the model name instead.` : ''}</p><div class="settings-grid"><label class="field" for="aim-add-litellm_model_name"><span>LiteLLM model name (alias)</span>${inputF('aim-add', 'litellm_model_name', '', 'placeholder="local-fast" required')}</label><label class="field" for="aim-add-ollama_model"><span>Ollama model</span>${local ? `<select class="select" id="aim-add-ollama_model" data-form="aim-add" data-field="ollama_model">${aimOpt('', 'Choose…', fv('aim-add', 'ollama_model', ''))}${local}</select>` : inputF('aim-add', 'ollama_model', '', 'placeholder="llama3.2:3b"')}</label><label class="field" for="aim-add-num_ctx"><span>Context size (num_ctx)</span>${inputF('aim-add', 'num_ctx', '', 'type="number" min="512" placeholder="8192"')}</label><label class="field" for="aim-add-keep_alive"><span>Keep alive</span>${inputF('aim-add', 'keep_alive', '', 'placeholder="15m"')}</label><label class="field" for="aim-add-model_type"><span>Type</span><select class="select" id="aim-add-model_type" data-form="aim-add" data-field="model_type">${(aimData().options?.model_types || []).map(t => aimOpt(t, t.replace('_', ' ').toLowerCase(), fv('aim-add', 'model_type', 'CUSTOM'))).join('')}</select></label></div>${btn(isBusy('aim-add') ? 'Adding…' : 'Add to draft', 'aim-add', {}, 'primary sm', isBusy('aim-add'))}</div>`;
}

/* ───────────── routing: tiers and the auto router ───────────── */
function aimRouting() {
  const d = aimData(), tiers = d.tiers || {}, r = d.routing || {};
  const tierRows = Object.entries(tiers).map(([name, t]) => `<tr><td class="mono">${esc(name)}<div class="settings-help">${esc(t.description || '')}</div></td><td><select class="select aim-inline" aria-label="Model for tier ${attr(name)}" data-act-change="aim-tier-model" data-tier="${attr(name)}">${aimModelOptions(t.model)}</select></td><td>${btn(t.enabled ? 'Disable' : 'Enable', 'aim-tier-toggle', { tier: name, on: t.enabled ? '' : '1' }, 'sm')}</td></tr>`).join('');
  const rv = k => fv('aim-route', k, k.startsWith('tier_') ? (r.tier_models || {})[k.slice(5)] || '' : r[k] ?? '');
  const classifier = rv('auto_router_enabled') ? rv('classifier') : 'DISABLED';
  const tierSelects = (d.options?.routing_tiers || ['SIMPLE', 'MEDIUM', 'COMPLEX', 'REASONING']).map(t => `<label class="field" for="aim-route-tier_${t}"><span>${t}</span><select class="select" id="aim-route-tier_${t}" data-form="aim-route" data-field="tier_${t}">${aimModelOptions(rv(`tier_${t}`))}</select></label>`).join('');
  return settingsGroup('Model tiers', 'Logical tiers workers can point at. Change the model behind a tier and every worker on it follows (after Apply).', `<div class="mon-table-wrap"><table class="mon-table"><thead><tr><th>Tier</th><th>Model</th><th></th></tr></thead><tbody>${tierRows}</tbody></table></div><div class="row gap8 mt14"><label class="field" for="aim-tier-new"><span>New tier</span>${inputF('aim-tier', 'new', '', 'placeholder="e.g. CHEAP_JSON" pattern="[A-Za-z][A-Za-z0-9_]+"')}</label>${btn('Add tier', 'aim-tier-add', {}, 'sm')}</div>`) +
    settingsGroup('LiteLLM Auto Router', 'LiteLLM classifies each request and routes it to the model for its complexity tier.', `<div class="settings-grid"><label class="field" for="aim-route-classifier"><span>Auto Router</span><select class="select" id="aim-route-classifier" data-form="aim-route" data-field="classifier" data-live="1">${Object.entries(AIM_CLASSIFIERS).map(([k, t]) => aimOpt(k, t, classifier)).join('')}</select></label><label class="field" for="aim-route-auto_router_model_name"><span>Router model name</span>${inputF('aim-route', 'auto_router_model_name', r.auto_router_model_name || '', 'placeholder="jobhunt-auto"')}</label></div>
      ${classifier === 'DISABLED' ? '<p class="settings-help">Off: Auto Router workers use their tier.</p>' : `<div class="settings-checks">${checkF('aim-route', 'manage_auto_router', rv('manage_auto_router'), 'Job Hunt OS deploys this router into LiteLLM (otherwise it must already exist there)')}${checkF('aim-route', 'send_metadata', rv('send_metadata'), 'Send Job Hunt OS request metadata (worker, workflow, retry, confidence)')}</div><div class="settings-grid">${tierSelects}<label class="field" for="aim-route-default_model"><span>Default model</span><select class="select" id="aim-route-default_model" data-form="aim-route" data-field="default_model">${aimModelOptions(rv('default_model'))}</select></label>${classifier === 'LLM' ? `<label class="field" for="aim-route-classifier_model"><span>Classifier model</span><select class="select" id="aim-route-classifier_model" data-form="aim-route" data-field="classifier_model">${aimModelOptions(rv('classifier_model'))}</select></label>` : ''}${classifier === 'CUSTOM' ? `<label class="field" for="aim-route-classifier_plugin"><span>Classifier plugin</span>${inputF('aim-route', 'classifier_plugin', r.classifier_plugin || '', 'placeholder="jobhunt_classifier.router"')}<span class="settings-help">A ClassifierPlugin importable by LiteLLM; see integrations/litellm/jobhunt_classifier.py.</span></label>` : ''}</div>`}
      ${btn(isBusy('aim-route') ? 'Saving…' : 'Save routing to draft', 'aim-route-save', {}, 'primary sm', isBusy('aim-route'))}`);
}
function aimRoutingPayload() {
  const r = aimData().routing || {}, form = S.forms['aim-route'] || {}, body = { revision: aimRev() };
  if ('classifier' in form) { body.auto_router_enabled = form.classifier !== 'DISABLED'; if (form.classifier !== 'DISABLED') body.classifier = form.classifier; }
  for (const k of ['auto_router_model_name', 'default_model', 'classifier_model', 'classifier_plugin']) if (k in form) body[k] = form[k] || null;
  for (const k of ['manage_auto_router', 'send_metadata']) if (k in form) body[k] = !!form[k];
  const tiers = Object.keys(form).filter(k => k.startsWith('tier_'));
  if (tiers.length) body.tier_models = Object.fromEntries(['SIMPLE', 'MEDIUM', 'COMPLEX', 'REASONING'].map(t => [t, `tier_${t}` in form ? form[`tier_${t}`] || null : (r.tier_models || {})[t] || null]));
  return body;
}

/* ───────────── LiteLLM connection ───────────── */
function aimLiteLLM() {
  const c = aimData().connection || {}, [label, tone] = AIM_CONN[c.status] || AIM_CONN.UNKNOWN, t = AIM.tests.connection;
  const modes = (c.sync_modes || ['disabled', 'admin_api']).map(m => aimOpt(m, { disabled: 'Disabled (read only)', admin_api: 'Admin API (STORE_MODEL_IN_DB=True)', config_file: 'Managed config file + restart' }[m] || m, fv('aim-conn', 'sync_mode', c.sync_mode))).join('');
  return settingsGroup('Connection', `${pill(label, tone)} ${c.litellm_version ? `LiteLLM ${esc(c.litellm_version)}` : ''}${c.last_successful_sync_at ? ` · last successful sync ${esc(fmtDateTime(c.last_successful_sync_at))}` : ''}`, `${c.secret_storage === false ? '<div class="settings-notice">Key storage is unavailable until the server has a SECRET_KEY.</div>' : ''}<div class="settings-grid"><label class="field" for="aim-conn-base_url"><span>LiteLLM base URL</span>${inputF('aim-conn', 'base_url', c.base_url_saved || '', `type="url" placeholder="${attr(c.base_url_env || 'http://localhost:4000')}"`)}<span class="settings-help">${c.base_url_env && !c.base_url_saved ? `Using the server default ${esc(c.base_url_env)}.` : ''}</span></label><label class="field" for="aim-conn-api_key"><span>LiteLLM API key</span>${inputF('aim-conn', 'api_key', '', `type="password" autocomplete="off" spellcheck="false" placeholder="${attr(c.api_key_configured ? `Saved ${c.api_key_hint || ''}` : 'sk-…')}"`)}<span class="settings-help">Encrypted; never shown again. Leave blank to keep the saved key.</span></label><label class="field" for="aim-conn-sync_mode"><span>Configuration sync</span><select class="select" id="aim-conn-sync_mode" data-form="aim-conn" data-field="sync_mode">${modes}</select><span class="settings-help">Only managed models and the auto router live in LiteLLM; worker settings travel with each request and never need a sync.</span></label></div><div class="row gap8">${btn(isBusy('aim-conn') ? 'Saving…' : 'Save', 'aim-conn-save', {}, 'primary sm', isBusy('aim-conn'))}${btn(isBusy('aim-conn-test') ? 'Testing…' : 'Test Connection', 'aim-conn-test', {}, 'sm', isBusy('aim-conn-test'))}${c.api_key_configured ? btn('Remove saved key', 'aim-conn-key-remove', {}, 'sm') : ''}</div>${t ? `<ul class="aim-checks" role="status"><li>${t.reachable ? '✓' : '✗'} LiteLLM reachable</li><li>${t.authenticated ? '✓' : '✗'} Authentication</li><li>${t.discovery ? '✓' : '✗'} Model discovery (${t.models} models)</li><li>Latency ${t.latency_ms != null ? `${t.latency_ms} ms` : 'unknown'}</li>${t.error ? `<li class="settings-error">${esc(t.error)}</li>` : ''}</ul>` : ''}${c.last_error && !t ? `<p class="settings-error">${esc(c.last_error)}</p>` : ''}`) +
    settingsGroup('Synchronization', `${pill((AIM_SYNC[c.sync_status] || AIM_SYNC.SYNCED)[0], (AIM_SYNC[c.sync_status] || AIM_SYNC.SYNCED)[1])} ${c.last_sync_at ? `last applied ${esc(fmtDateTime(c.last_sync_at))}` : ''}`, `${c.last_sync_error ? `<p class="settings-error">${esc(c.last_sync_error)}</p>` : ''}${c.managed_config_path ? `<p class="settings-help">Managed file: <span class="mono">${esc(c.managed_config_path)}</span>${c.restart_command_configured ? ' · restarted automatically' : ' · restart LiteLLM after applying'}</p>` : ''}${btn(isBusy('aim-verify') ? 'Verifying…' : 'Verify against LiteLLM', 'aim-verify', {}, 'sm', isBusy('aim-verify'))}${AIM.tests.verify ? `<p class="settings-help" role="status">${esc(AIM.tests.verify.status)}${(AIM.tests.verify.differences || []).length ? ` — ${esc(AIM.tests.verify.differences.join('; '))}` : ''}${(AIM.tests.verify.missing_models || []).length ? ` — not served: ${esc(AIM.tests.verify.missing_models.join(', '))}` : ''}</p>` : ''}`);
}

/* ───────────── advanced: direct providers, history, audit ───────────── */
function aimAdvanced() {
  if (AIM.versions === null && !isBusy('aim-history')) aimHistory();
  const versions = (AIM.versions || []).map((v, i) => `<tr><td class="mono">v${v.version}${i === 0 ? ' ' + pill('Active', 'good') : ''}</td><td>${esc(fmtDateTime(v.created_at))}</td><td>${esc(v.changed_by)} <span class="settings-help">${esc(v.source)}</span></td><td>${esc(v.summary)}${v.restored_from_version ? ` <span class="settings-help">(from v${v.restored_from_version})</span>` : ''}</td><td>${i === 0 ? '' : btn('Roll back', 'aim-restore', { id: v.id, version: v.version }, 'sm', isBusy('aim-restore'))}</td></tr>`).join('');
  const audit = (AIM.audit || []).slice(0, 30).map(a => `<tr><td>${esc(fmtDateTime(a.created_at))}</td><td>${esc(a.action)}</td><td>${pill(a.result, a.result === 'SUCCEEDED' ? 'good' : a.result === 'REJECTED' ? 'warn' : 'bad')}</td><td>${esc(a.actor)} <span class="settings-help">${esc(a.source)}</span></td><td class="settings-help">${esc(a.error || (a.version ? `v${a.version}` : ''))}</td></tr>`).join('');
  return settingsGroup('Version history', 'Every applied configuration is kept. Rolling back applies an old version as a new one; history never changes.', versions ? `<div class="mon-table-wrap aim-wide"><table class="mon-table"><thead><tr><th>Version</th><th>When</th><th>By</th><th>Change</th><th></th></tr></thead><tbody>${versions}</tbody></table></div>` : loading()) +
    settingsGroup('Audit trail', 'Every attempt to change the configuration, including rejected and failed ones.', audit ? `<div class="mon-table-wrap aim-wide"><table class="mon-table"><thead><tr><th>When</th><th>Action</th><th>Result</th><th>By</th><th>Detail</th></tr></thead><tbody>${audit}</tbody></table></div>` : '<p class="settings-help">Nothing recorded yet.</p>') +
    settingsDetails('aim-direct', 'Direct providers (used when the LiteLLM gateway is off)', renderAISettings());
}
async function aimHistory() {
  S.busy['aim-history'] = true;
  try { const [v, a] = await Promise.all([api('/api/settings/ai/versions'), api('/api/settings/ai/audit')]); AIM.versions = v.versions || []; AIM.audit = a.entries || []; }
  catch (err) { AIM.versions = []; AIM.audit = []; toast(err.message, true); }
  finally { delete S.busy['aim-history']; render(); }
}

/* ───────────── preview and apply ───────────── */
function aimValue(v, path = '') {
  if (v == null || v === '') return '—';
  if (typeof v === 'number' && /context_window|max_output_tokens|num_ctx/.test(path)) return aimTokens(v);
  return typeof v === 'object' ? JSON.stringify(v) : String(v);
}
function aimPreviewPanel() {
  const p = AIM.preview, errs = p.validation?.errors || [], warns = p.validation?.warnings || [], plan = p.sync_plan || {};
  const changes = p.changes || [], effective = changes.filter(c => c.kind === 'effective'), other = changes.filter(c => c.kind !== 'effective');
  const line = c => `<li><strong>${esc(c.label)}</strong><div class="mono aim-diff">${esc(aimValue(c.before, c.path))} → ${esc(aimValue(c.after, c.path))}</div></li>`;
  return `<section class="aim-preview" aria-labelledby="aim-preview-title"><header class="row between"><h3 id="aim-preview-title">Preview changes</h3>${btn('Close', 'aim-preview-close', {}, 'sm')}</header>
    ${effective.length ? `<h4>Workers</h4><ul class="aim-changes">${effective.map(line).join('')}</ul>` : ''}${other.length ? `<details ${effective.length ? '' : 'open'}><summary>All ${other.length} setting change${other.length === 1 ? '' : 's'}</summary><ul class="aim-changes">${other.map(line).join('')}</ul></details>` : ''}
    ${(plan.create || []).length || (plan.remove || []).length ? `<h4>LiteLLM</h4><p class="settings-help">${(plan.create || []).map(x => `create ${esc(x.model_name)}`).concat((plan.remove || []).map(x => `remove ${esc(x.model_name)}`)).join(' · ')} via ${esc(plan.mode)}${plan.restart_required ? ' · LiteLLM restart required' : ''}</p>` : '<p class="settings-help">Nothing changes inside LiteLLM: these settings travel with each request.</p>'}
    ${errs.length ? `<div class="settings-error" role="alert"><strong>Fix before applying</strong>${errs.map(e => `<p>${esc(e.message)}</p>`).join('')}</div>` : ''}${warns.length ? `<div class="settings-notice">${warns.map(w => `<p>${esc(w.message)}</p>`).join('')}</div>` : ''}${p.validation?.live_checked === false ? `<p class="settings-help">LiteLLM could not be checked: ${esc(p.validation.live_error || 'unreachable')}</p>` : ''}
    <footer class="row gap8">${btn(isBusy('aim-apply') ? 'Applying…' : 'Apply', 'aim-apply', {}, 'primary', !p.can_apply || isBusy('aim-apply'))}<span class="settings-help">Applies v${(p.base_version ?? 0) + 1} as one change; if LiteLLM rejects it, v${p.base_version ?? 0} stays active.</span></footer></section>`;
}

const AI_MODELS_ACTIONS = {
  'aim-reload': () => { AIM.error = ''; AIM.data = undefined; render(); },
  'aim-tab': el => { AIM.tab = el.dataset.tab; AIM.drawer = null; render(); },
  'aim-worker': el => { AIM.drawer = el.dataset.worker; delete S.forms.aiw; AIM.tab = AIM.tab === 'overview' ? 'workers' : AIM.tab; render(); document.getElementById('aiw-model_mode')?.focus(); },
  'aim-drawer-close': () => { AIM.drawer = null; delete S.forms.aiw; render(); },
  'aim-worker-save': el => {
    const w = (aimData().workers || []).find(x => x.worker_type === el.dataset.worker);
    return aimRun('aim-worker-save', async () => {
      await api(`/api/settings/ai/workers/${encodeURIComponent(w.worker_type)}`, { method: 'PUT', body: aimWorkerPayload(w) });
      delete S.forms.aiw; AIM.drawer = null;
    }, `${w.label} saved to the draft — preview and apply to use it`);
  },
  'aim-worker-test': el => { const w = el.dataset.worker; return busy(`aim-wtest-${w}`, async () => { AIM.tests[`worker:${w}`] = await api(`/api/settings/ai/workers/${encodeURIComponent(w)}/test`, { method: 'POST', body: { configuration: 'draft' } }); }); },
  'aim-model-test': el => { const n = el.dataset.name; return busy(`aim-mtest-${n}`, async () => { AIM.tests[`model:${n}`] = await api(`/api/settings/ai/models/${encodeURIComponent(n)}/test`, { method: 'POST' }); await aimLoad(); }); },
  'aim-model-toggle': el => aimRun('aim-model', () => api(`/api/settings/ai/models/${encodeURIComponent(el.dataset.name)}`, { method: 'PUT', body: { revision: aimRev(), enabled: !!el.dataset.on } })),
  'aim-model-type': el => aimRun('aim-model', () => api(`/api/settings/ai/models/${encodeURIComponent(el.dataset.name)}`, { method: 'PUT', body: { revision: aimRev(), model_type: el.value } })),
  'aim-model-unmanage': el => { if (!confirm(`Stop managing ${el.dataset.name}? The next Apply removes its deployment from LiteLLM.`)) return; return aimRun('aim-model', () => api(`/api/settings/ai/models/${encodeURIComponent(el.dataset.name)}?revision=${aimRev()}`, { method: 'DELETE' })); },
  'aim-sync': () => aimRun('aim-sync', () => api('/api/settings/ai/models/sync', { method: 'POST' }), out => `${out.models} models discovered${out.added?.length ? `, ${out.added.length} new` : ''}`),
  'aim-add-toggle': async () => { AIM.adding = !AIM.adding; render(); if (AIM.adding && !AIM.ollama) { AIM.ollama = await api('/api/settings/ai/ollama').catch(() => ({ reachable: false, models: [] })); render(); } },
  'aim-add': () => {
    const f = S.forms['aim-add'] || {};
    if (!String(f.litellm_model_name || '').trim()) { toast('Give the model a LiteLLM model name', true); return; }
    return aimRun('aim-add', async () => { await api('/api/settings/ai/models', { method: 'POST', body: { revision: aimRev(), litellm_model_name: f.litellm_model_name.trim(), ollama_model: f.ollama_model || '', num_ctx: f.num_ctx ? Number(f.num_ctx) : null, keep_alive: f.keep_alive || null, model_type: f.model_type || 'CUSTOM' } }); delete S.forms['aim-add']; AIM.adding = false; }, 'Model added to the draft');
  },
  'aim-tier-model': el => aimRun('aim-tier', () => api(`/api/settings/ai/tiers/${encodeURIComponent(el.dataset.tier)}`, { method: 'PUT', body: { revision: aimRev(), litellm_model_name: el.value || null } })),
  'aim-tier-toggle': el => aimRun('aim-tier', () => api(`/api/settings/ai/tiers/${encodeURIComponent(el.dataset.tier)}`, { method: 'PUT', body: { revision: aimRev(), enabled: !!el.dataset.on } })),
  'aim-tier-add': () => { const name = String(fv('aim-tier', 'new', '')).trim().toUpperCase(); if (!name) return; return aimRun('aim-tier', async () => { await api(`/api/settings/ai/tiers/${encodeURIComponent(name)}`, { method: 'PUT', body: { revision: aimRev(), enabled: true } }); delete S.forms['aim-tier']; }, `Tier ${name} added`); },
  'aim-route-save': () => aimRun('aim-route', async () => { await api('/api/settings/ai/routing', { method: 'PUT', body: aimRoutingPayload() }); delete S.forms['aim-route']; }, 'Routing saved to the draft'),
  'aim-conn-save': () => {
    const f = S.forms['aim-conn'] || {}, body = {};
    if ('base_url' in f) body.base_url = f.base_url; if (f.api_key) body.api_key = f.api_key; if ('sync_mode' in f) body.sync_mode = f.sync_mode;
    return aimRun('aim-conn', async () => { await api('/api/settings/ai/connection', { method: 'PUT', body }); delete S.forms['aim-conn']; }, 'LiteLLM connection saved');
  },
  'aim-conn-key-remove': () => { if (!confirm('Remove the saved LiteLLM key?')) return; return aimRun('aim-conn', () => api('/api/settings/ai/connection', { method: 'PUT', body: { api_key: '' } }), 'Key removed'); },
  'aim-conn-test': () => busy('aim-conn-test', async () => { AIM.tests.connection = await api('/api/settings/ai/test-connection', { method: 'POST' }); await aimLoad(); }, () => AIM.tests.connection?.label),
  'aim-gateway': el => aimRun('aim-gateway', () => api('/api/settings/ai/gateway', { method: 'PUT', body: { enabled: !!el.dataset.on } }), el.dataset.on ? 'Workers now use the LiteLLM gateway' : 'Gateway off: workers use the direct provider'),
  'aim-runtime': () => busy('aim-runtime', async () => { AIM.runtime = await api('/api/settings/ai/runtime'); }),
  'aim-verify': () => busy('aim-verify', async () => { AIM.tests.verify = await api('/api/settings/ai/verify', { method: 'POST' }); await aimLoad(); }),
  'aim-preview': () => busy('aim-preview', async () => { AIM.preview = await api('/api/settings/ai/preview', { method: 'POST' }); }),
  'aim-preview-close': () => { AIM.preview = null; render(); },
  'aim-apply': () => {
    const p = AIM.preview;
    return busy('aim-apply', async () => {
      try {
        const out = await api('/api/settings/ai/apply', { method: 'POST', body: { revision: p.draft_revision, base_version: p.base_version } });
        AIM.preview = null; AIM.versions = null; await aimLoad();
        toast(out.status === 'PENDING' ? `v${out.version} applied — restart LiteLLM to load it` : out.status === 'UNCHANGED' ? 'Nothing to apply' : `v${out.version} applied`);
      } catch (err) {
        if (err.detail?.validation) AIM.preview = { ...p, validation: err.detail.validation, can_apply: false };
        else { if (err.status === 409) { AIM.preview = null; await aimLoad(); } toast(err.message, true); }
      }
    });
  },
  'aim-restore': el => {
    if (!confirm(`Roll back to v${el.dataset.version}? It is applied as a new version; history is kept.`)) return;
    return busy('aim-restore', async () => { const out = await api(`/api/settings/ai/versions/${el.dataset.id}/restore`, { method: 'POST', body: { base_version: aimData().active_version } }); AIM.versions = null; await aimLoad(); return out; }, out => `Restored as v${out.version}`);
  },
};
SETTINGS_RENDERERS.ai = renderAIModels;
Object.assign(SETTINGS_ACTIONS, AI_MODELS_ACTIONS);
