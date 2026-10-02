import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The production scripts, in page order, without app.js's automatic network boot.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'AIM', 'render', 'setF', 'renderAIModels', 'aimWorkerPayload', 'aimRoutingPayload', 'renderMonRouting'];
let ui, context, fetchMock, routes;
const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });

const worker = (type, label, config, model, extra = {}) => ({ worker_type: type, label, description: `${label} work`, structured_output_required: true, min_context_tokens: 8192, has_prompt: true, config: { mode: 'TIER', tier: 'PRIMARY_REASONING', use_structured_output: true, enabled: true, escalation: null, ...config }, resolved: { model, assignment: config.tier || config.primary_model, note: null }, ...extra });
const model = (name, extra = {}) => ({ name, display_name: name, enabled: true, model_type: 'CUSTOM', provider: 'ollama_chat', underlying_model: name, supports_structured_output: true, supports_tools: true, supports_reasoning: null, supports_vision: null, context_window: 24576, max_context_window: 131072, max_output_tokens: null, managed: false, present_in_litellm: true, health_status: 'UNKNOWN', used_by: [], ...extra });
function overview() {
  return {
    gateway_enabled: true, llm_provider: 'litellm', active_version: 4,
    gateway: { gateway_enabled: true, direct_provider: 'openrouter', direct_label: 'OpenRouter', direct_model: 'openrouter/free', direct_ready: true, litellm_ready: true, litellm_base_url: 'http://localhost:4000' },
    connection: { base_url: 'http://localhost:4000', base_url_saved: 'http://localhost:4000', base_url_env: null, api_key_configured: true, api_key_hint: '…3456', sync_mode: 'admin_api', sync_modes: ['disabled', 'admin_api'], status: 'CONNECTED', litellm_version: '1.102.1', last_successful_sync_at: '2026-09-25T10:00:00', sync_status: 'SYNCED', draft_revision: 7, secret_storage: true },
    draft: { workers: {} }, active: { workers: {} },
    pending_changes: [{ path: 'effective.REVIEWER', label: 'Reviewer · effective model', before: 'local-reasoning', after: 'local-deep', kind: 'effective' }],
    workers: [worker('RANKER', 'Ranker', { tier: 'PRIMARY_REASONING' }, 'local-reasoning'), worker('REVIEWER', 'Reviewer', { mode: 'FIXED', primary_model: 'local-deep' }, 'local-deep', { min_context_tokens: 16384 }), worker('INBOX_CLASSIFIER', 'Inbox Classifier', { mode: 'AUTO_ROUTER', tier: 'FAST' }, 'jobhunt-auto')],
    models: [model('local-fast', { context_window: 4096, supports_reasoning: false }), model('local-reasoning', { supports_reasoning: true, used_by: ['RANKER'] }), model('local-deep', { supports_reasoning: false }), model('mystery', { provider: null, supports_structured_output: null, context_window: null, max_context_window: null })],
    tiers: { FAST: { model: 'local-fast', enabled: true, description: 'quick' }, PRIMARY_REASONING: { model: 'local-reasoning', enabled: true, description: '' }, DEEP_REVIEW: { model: 'local-deep', enabled: true, description: '' } },
    routing: { auto_router_enabled: true, auto_router_model_name: 'jobhunt-auto', manage_auto_router: true, classifier: 'HEURISTIC', classifier_model: null, classifier_plugin: null, tier_models: { SIMPLE: 'local-fast', MEDIUM: 'local-reasoning', COMPLEX: 'local-reasoning', REASONING: 'local-deep' }, default_model: null, send_metadata: true },
    options: { model_types: ['FAST', 'CUSTOM', 'AUTO_ROUTER'], routing_tiers: ['SIMPLE', 'MEDIUM', 'COMPLEX', 'REASONING'] },
  };
}
const flush = () => new Promise(r => setTimeout(r, 0));
async function open(tab = 'overview') {
  ui.S.screen = 'settings'; ui.S.settingsSection = 'ai'; ui.AIM.tab = tab; ui.AIM.data = undefined; ui.render();
  await flush(); await flush();
}
const text = () => document.getElementById('view').textContent;
const call = (method, url) => fetchMock.mock.calls.filter(([u, o]) => u === url && (o?.method || 'GET') === method);
const body = (method, url) => JSON.parse(call(method, url).at(-1)[1].body);

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"me@example.com"},"max_upload_mb":10}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  routes = { 'GET /api/settings/ai': () => overview() };
  fetchMock = vi.fn(async (url, opt = {}) => {
    const handler = routes[`${opt.method || 'GET'} ${url.split('?')[0]}`];
    if (!handler) return response({});
    const out = handler(opt.body ? JSON.parse(opt.body) : undefined);
    return out instanceof Array && out.length === 2 && typeof out[1] === 'number' ? response(out[0], out[1]) : response(out);
  });
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'settings.js', 'ai_models.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirm;
  ui.S.forms = {}; ui.S.busy = {};
  Object.assign(ui.AIM, { tab: 'overview', data: undefined, loading: false, error: '', drawer: null, preview: null, runtime: null, versions: null, audit: null, tests: {}, ollama: null, adding: false });
  Object.assign(ui.D, { profile: { name: 'A', search_preferences: {} }, prefs: {}, settings: { ai: { saved: { stage_models: {} }, env: { llm_provider: 'openrouter', models: {}, stage_models: {} }, effective: {} }, configured: {}, keys: {} }, summary: { model: 'local-reasoning', application_mode: 'manual_approval', counts: {} } });
});

describe('Settings → AI Models', () => {
  it('shows model selection for the route in use: LiteLLM workers, or the direct provider’s models', async () => {
    await open();
    expect(text()).toContain('Models · LiteLLM workers');
    expect(document.querySelector('#view details[data-settings-details="aim-direct"] summary').textContent)
      .toContain('Direct provider · OpenRouter (kept ready for when you switch the route)');
    expect(document.querySelector('#view .aim-direct-models')).toBeNull();

    routes['GET /api/settings/ai'] = () => ({ ...overview(), gateway_enabled: false, llm_provider: 'openrouter',
      gateway: { ...overview().gateway, gateway_enabled: false } });
    await open();
    const direct = document.querySelector('#view .aim-direct-models');
    expect(direct.textContent).toContain('Model defaults');
    expect(direct.querySelector('#ai-llm_provider')).not.toBeNull();
    expect(text()).not.toContain('Models · LiteLLM workers');
  });

  it('keeps direct provider settings off the Advanced tab', async () => {
    await open('advanced');
    expect(text()).not.toContain('Direct providers');
    expect(document.querySelector('#view #ai-llm_provider')).toBeNull();
  });

  it('chooses between LiteLLM and the direct provider in the Gateway section', async () => {
    routes['PUT /api/settings/ai/gateway'] = sent => ({ ...overview().gateway, gateway_enabled: sent.enabled });
    await open();
    const buttons = [...document.querySelectorAll('#view .seg[aria-label="AI gateway"] button')];
    expect(buttons.map(b => b.textContent.trim())).toEqual(['LiteLLM gateway', 'OpenRouter (direct)']);
    expect(buttons[0].getAttribute('aria-pressed')).toBe('true');
    expect(buttons.every(b => b.type === 'button')).toBe(true);  // inside the settings form: never a submit
    buttons[1].click();
    await flush(); await flush(); await flush();
    expect(body('PUT', '/api/settings/ai/gateway')).toEqual({ enabled: false });
    expect(call('GET', '/api/settings/ai').length).toBeGreaterThan(1);  // the tab reloads to show the new route
  });

  it('loads the overview and shows gateway, version, pending changes and each worker', async () => {
    await open();
    expect(call('GET', '/api/settings/ai').length).toBe(1);
    expect(text()).toContain('LiteLLM gateway on');
    expect(text()).toContain('Active v4');
    expect(text()).toContain('1 pending change');
    expect(text()).toContain('Ranker'); expect(text()).toContain('local-reasoning');
  });

  it('labels every form control on every tab', async () => {
    for (const tab of ['overview', 'workers', 'models', 'routing', 'litellm', 'advanced']) {
      await open(tab);
      for (const el of document.querySelectorAll('#settings-editor input:not([type="radio"]), #settings-editor select, #settings-editor textarea')) {
        expect(el.getAttribute('aria-label') || el.labels?.length, `${tab}: ${el.id || el.outerHTML}`).toBeTruthy();
      }
    }
  });

  it('opens a worker drawer that only offers options the model supports', async () => {
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'RANKER' } });
    expect(document.getElementById('aiw-reasoning_effort')).not.toBeNull(); // local-reasoning reasons
    ui.setF('aiw', 'model_mode', 'FIXED'); ui.setF('aiw', 'primary_model', 'local-deep'); ui.render();
    expect(document.getElementById('aiw-reasoning_effort')).toBeNull(); // local-deep does not
    expect(text()).toContain('does not support it');
    ui.setF('aiw', 'primary_model', 'mystery'); ui.render();
    expect(text()).toContain('Structured Output Required');
    ui.setF('aiw', 'primary_model', 'local-fast'); ui.render();
    expect(text()).toContain('may not satisfy Ranker requirements');
  });

  it('saves a worker to the draft with the revision it was based on', async () => {
    routes['PUT /api/settings/ai/workers/RANKER'] = () => ({ worker: {}, draft_revision: 8 });
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'RANKER' } });
    ui.setF('aiw', 'temperature', '0.2'); ui.setF('aiw', 'context_window', '16384');
    ui.setF('aiw', 'esc_target', 'tier:DEEP_REVIEW'); ui.setF('aiw', 'esc_LOW_CONFIDENCE', true); ui.setF('aiw', 'esc_threshold', '0.85');
    await ui.A['aim-worker-save']({ dataset: { worker: 'RANKER' } });
    const sent = body('PUT', '/api/settings/ai/workers/RANKER');
    expect(sent).toMatchObject({ revision: 7, model_mode: 'TIER', tier_name: 'PRIMARY_REASONING', temperature: 0.2, context_window: 16384, max_output_tokens: null,
      escalation: { tier: 'DEEP_REVIEW', model: null, conditions: ['LOW_CONFIDENCE'], confidence_threshold: 0.85 } });
    expect(ui.AIM.drawer).toBeNull();
    expect(call('GET', '/api/settings/ai').length).toBe(2); // reloaded after the save
  });

  it('reloads and keeps the drawer when someone else changed the draft', async () => {
    routes['PUT /api/settings/ai/workers/RANKER'] = () => [{ detail: { message: 'Someone else changed the AI configuration' } }, 409];
    await open('workers');
    ui.A['aim-worker']({ dataset: { worker: 'RANKER' } });
    await ui.A['aim-worker-save']({ dataset: { worker: 'RANKER' } });
    expect(ui.AIM.drawer).toBe('RANKER');
    expect(call('GET', '/api/settings/ai').length).toBe(2);
    expect(document.getElementById('toast').textContent).toContain('Someone else changed');
  });

  it('previews changes, formats token sizes and applies the previewed revision', async () => {
    routes['POST /api/settings/ai/preview'] = () => ({ base_version: 4, draft_revision: 7, can_apply: true, sync_plan: { mode: 'admin_api', create: [], remove: [] },
      validation: { errors: [], warnings: [{ message: 'Reviewer and Tailor use the same model' }] },
      changes: [{ path: 'effective.REVIEWER', label: 'Reviewer · effective model', before: 'local-reasoning', after: 'local-deep', kind: 'effective' },
                { path: 'workers.RANKER.context_window', label: 'Ranker · context window', before: 16384, after: 24576, kind: 'changed' },
                { path: 'workers.RANKER.timeout_seconds', label: 'Ranker · timeout', before: null, after: 3600, kind: 'changed' }] });
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'SYNCED', version: 5 });
    await open();
    await ui.A['aim-preview']();
    expect(text()).toContain('local-reasoning → local-deep');
    expect(text()).toContain('16K → 24K'); expect(text()).toContain('— → 3600');
    expect(text()).toContain('Nothing changes inside LiteLLM');
    await ui.A['aim-apply']();
    expect(body('POST', '/api/settings/ai/apply')).toEqual({ revision: 7, base_version: 4 });
    expect(ui.AIM.preview).toBeNull();
    expect(document.getElementById('toast').textContent).toBe('v5 applied');
  });

  it('keeps Apply disabled on validation errors and shows a rejected apply', async () => {
    routes['POST /api/settings/ai/preview'] = () => ({ base_version: 4, draft_revision: 7, can_apply: false, changes: [], sync_plan: { create: [{ model_name: 'local-small' }], remove: [], mode: 'config_file', restart_required: true },
      validation: { errors: [{ message: 'The fallback model cannot be the primary model' }], warnings: [] } });
    await open();
    await ui.A['aim-preview']();
    expect(document.querySelector('[data-act="aim-apply"]').disabled).toBe(true);
    expect(text()).toContain('cannot be the primary model'); expect(text()).toContain('LiteLLM restart required');
    routes['POST /api/settings/ai/apply'] = () => [{ detail: { message: 'invalid', validation: { errors: [{ message: 'late error' }], warnings: [] } } }, 422];
    ui.AIM.preview.can_apply = true;
    await ui.A['aim-apply']();
    expect(ui.AIM.preview.can_apply).toBe(false); expect(text()).toContain('late error');
  });

  it('shows known, unknown and missing capabilities, and tests a model', async () => {
    routes['POST /api/settings/ai/models/mystery/test'] = () => ({ success: true, latency_ms: 420, model_group: 'mystery', underlying_model: 'ollama_chat/x', structured_output: true });
    await open('models');
    expect(document.querySelector('[title="reasoning: unknown"]')).not.toBeNull();
    expect(document.querySelector('[title="reasoning: no"]')).not.toBeNull();
    expect(text()).toContain('provider unknown');
    await ui.A['aim-model-test']({ dataset: { name: 'mystery' } });
    expect(ui.AIM.tests['model:mystery'].latency_ms).toBe(420);
    expect(text()).toContain('420 ms');
  });

  it('adds a local Ollama model from the server Ollama list', async () => {
    routes['GET /api/settings/ai/ollama'] = () => ({ api_base: 'http://localhost:11434', reachable: true, models: [{ name: 'llama3.2:3b', context_length: 131072 }] });
    routes['POST /api/settings/ai/models'] = () => ({ model: {} });
    await open('models');
    await ui.A['aim-add-toggle'](); await flush();
    expect(document.getElementById('aim-add-ollama_model').textContent).toContain('128K ctx');
    ui.setF('aim-add', 'litellm_model_name', 'local-small'); ui.setF('aim-add', 'ollama_model', 'llama3.2:3b'); ui.setF('aim-add', 'num_ctx', '4096');
    await ui.A['aim-add']();
    expect(body('POST', '/api/settings/ai/models')).toMatchObject({ revision: 7, litellm_model_name: 'local-small', ollama_model: 'llama3.2:3b', num_ctx: 4096 });
  });

  it('turns the auto router off and on through its classifier', async () => {
    await open('routing');
    expect(document.getElementById('aim-route-tier_SIMPLE').value).toBe('local-fast');
    ui.setF('aim-route', 'classifier', 'DISABLED'); ui.render();
    expect(document.getElementById('aim-route-tier_SIMPLE')).toBeNull();
    expect(ui.aimRoutingPayload()).toEqual({ revision: 7, auto_router_enabled: false });
    ui.setF('aim-route', 'classifier', 'LLM'); ui.setF('aim-route', 'tier_REASONING', 'local-reasoning'); ui.render();
    expect(document.getElementById('aim-route-classifier_model')).not.toBeNull();
    expect(ui.aimRoutingPayload()).toMatchObject({ auto_router_enabled: true, classifier: 'LLM', tier_models: { SIMPLE: 'local-fast', REASONING: 'local-reasoning' } });
  });

  it('never shows the saved key and sends a key only when one was typed', async () => {
    routes['PUT /api/settings/ai/connection'] = () => ({});
    routes['POST /api/settings/ai/test-connection'] = () => ({ reachable: true, authenticated: false, discovery: false, models: 0, latency_ms: 12, status: 'AUTH_FAILED', label: 'Authentication failed', error: 'LiteLLM refused the API key' });
    await open('litellm');
    expect(document.getElementById('aim-conn-api_key').value).toBe('');
    expect(document.getElementById('view').innerHTML).not.toContain('sk-');
    ui.setF('aim-conn', 'base_url', 'http://litellm:4000');
    await ui.A['aim-conn-save']();
    expect(body('PUT', '/api/settings/ai/connection')).toEqual({ base_url: 'http://litellm:4000' });
    await ui.A['aim-conn-test']();
    expect(text()).toContain('✓ LiteLLM reachable'); expect(text()).toContain('✗ Authentication');
  });

  it('lists version history and rolls back as a new version', async () => {
    routes['GET /api/settings/ai/versions'] = () => ({ versions: [{ id: 14, version: 4, changed_by: 'me', source: 'USER', summary: 'Reviewer', created_at: '2026-09-25T10:00:00' }, { id: 12, version: 2, changed_by: 'system', source: 'MIGRATION', summary: 'Initial', created_at: '2026-09-24T10:00:00' }] });
    routes['GET /api/settings/ai/audit'] = () => ({ entries: [{ action: 'APPLY', result: 'REJECTED', actor: 'me', source: 'USER', error: 'bad', created_at: '2026-09-25T10:00:00' }] });
    routes['POST /api/settings/ai/versions/12/restore'] = () => ({ version: 5 });
    await open('advanced'); await flush(); await flush();
    expect(text()).toContain('v4'); expect(text()).toContain('Initial'); expect(text()).toContain('REJECTED');
    expect(document.querySelectorAll('[data-act="aim-restore"]').length).toBe(1); // not on the active version
    await ui.A['aim-restore']({ dataset: { id: 12, version: 2 } });
    expect(body('POST', '/api/settings/ai/versions/12/restore')).toEqual({ base_version: 4 });
    expect(document.getElementById('toast').textContent).toBe('Restored as v5');
  });

  it('shows an error with a retry when the overview cannot load', async () => {
    routes['GET /api/settings/ai'] = () => [{ detail: 'boom' }, 500];
    await open();
    expect(text()).toContain('could not be loaded: boom');
    routes['GET /api/settings/ai'] = () => overview();
    ui.A['aim-reload'](); await flush(); await flush();
    expect(text()).toContain('Active v4');
  });
});

describe('Monitoring → model routing', () => {
  it('shows per-worker inference share, escalation and auto router tiers', () => {
    const html = ui.renderMonRouting({ requests: 30, workers: [{ worker_type: 'REVIEWER', requests: 10, inference_ms: 9000, inference_share: 0.6, escalation_rate: 0.2, fallback_rate: 0, schema_failure_rate: 0, p95_latency_ms: 5000, configured: [['DEEP_REVIEW', 10]], models: [['ollama_chat/qwen2.5:32b', 10]] }],
      models: [{ model: 'ollama_chat/qwen2.5:32b', requests: 10, inference_ms: 9000, inference_share: 0.6, schema_failure_rate: 0.1, failure_rate: 0, p95_latency_ms: 5000, avg_confidence: 0.8 }],
      auto_router: { requests: 20, tiers: [{ tier: 'SIMPLE', requests: 15, share: 0.75 }] }, escalations: [{ worker_type: 'REVIEWER', reason: 'LOW_CONFIDENCE', requests: 2 }], slowest_model: 'ollama_chat/qwen2.5:32b', most_schema_failures: null });
    expect(html).toContain('Model routing'); expect(html).toContain('SIMPLE 75%'); expect(html).toContain('Reviewer 2× (LOW_CONFIDENCE)');
    expect(ui.renderMonRouting({ requests: 0 })).toBe('');
  });
});

describe('Settings → AI Models actions', () => {
  const ok = () => ({});
  it('switches tabs, toggles the gateway and checks the runtime', async () => {
    routes['PUT /api/settings/ai/gateway'] = ok;
    routes['GET /api/settings/ai/runtime'] = () => ({ live_error: null, workers: [
      { label: 'Ranker', desired: 'a', active: 'a', state: 'SYNCED' }, { label: 'Reviewer', desired: 'b', active: 'c', state: 'PENDING' },
      { label: 'Tailor', desired: 'd', active: 'd', state: 'OUT_OF_SYNC' }] });
    await open();
    ui.A['aim-tab']({ dataset: { tab: 'models' } }); expect(ui.AIM.tab).toBe('models');
    ui.A['aim-tab']({ dataset: { tab: 'overview' } });
    await ui.A['aim-gateway']({ dataset: { on: '' } });
    expect(body('PUT', '/api/settings/ai/gateway')).toEqual({ enabled: false });
    expect(document.getElementById('toast').textContent).toContain('Gateway off');
    await ui.A['aim-runtime']();
    expect(text()).toContain('Synced ✓'); expect(text()).toContain('Out of sync'); expect(text()).toContain('LiteLLM was read just now');
    routes['GET /api/settings/ai'] = () => ({ ...overview(), gateway_enabled: false, llm_provider: 'openrouter', pending_changes: [], connection: { ...overview().connection, status: 'UNKNOWN', base_url: null, last_sync_at: null, last_sync_error: 'boom' } });
    await open();
    expect(text()).toContain('Direct provider: openrouter'); expect(text()).toContain('Not tested'); expect(text()).toContain('No base URL yet'); expect(text()).toContain('boom');
    expect(document.querySelector('[data-act="aim-preview"]').disabled).toBe(true);
    await ui.A['aim-gateway']({ dataset: { on: '1' } });
    expect(body('PUT', '/api/settings/ai/gateway')).toEqual({ enabled: true });
  });

  it('opens the drawer from the overview, shows the auto router state and closes it', async () => {
    await open();
    ui.A['aim-worker']({ dataset: { worker: 'INBOX_CLASSIFIER' } });
    expect(ui.AIM.tab).toBe('workers');
    expect(text()).toContain('Auto Router: on'); expect(document.getElementById('aiw-tier_name')).not.toBeNull();
    ui.setF('aiw', 'fallback_model', 'jobhunt-auto'); ui.setF('aiw', 'context_window', '999999'); ui.render();
    ui.setF('aiw', 'model_mode', 'FIXED'); ui.setF('aiw', 'primary_model', 'local-reasoning'); ui.setF('aiw', 'fallback_model', 'local-reasoning'); ui.render();
    expect(text()).toContain('The fallback cannot be the primary model'); expect(text()).toContain('exceeds the 24K');
    ui.A['aim-drawer-close']();
    expect(ui.AIM.drawer).toBeNull(); expect(ui.S.forms.aiw).toBeUndefined();
    ui.A['aim-worker']({ dataset: { worker: 'NOPE' } });
    expect(document.querySelector('.aim-drawer')).toBeNull();
  });

  it('runs a worker test and shows success and failure', async () => {
    let answer = { success: true, answered_by: 'ollama_chat/gpt-oss:20b', latency_ms: 800, input_tokens: 40, output_tokens: 12, schema_ok: true, routing: { model_group: 'local-reasoning', routing_tier: 'MEDIUM', escalated: true, escalation_reason: 'LOW_CONFIDENCE' } };
    routes['POST /api/settings/ai/workers/RANKER/test'] = () => answer;
    await open('workers');
    await ui.A['aim-worker-test']({ dataset: { worker: 'RANKER' } });
    expect(body('POST', '/api/settings/ai/workers/RANKER/test')).toEqual({ configuration: 'draft' });
    expect(text()).toContain('group local-reasoning · tier MEDIUM · 800 ms · 40+12 tokens · schema ✓ · escalated (LOW_CONFIDENCE)');
    answer = { success: false, error: 'LiteLLM is unreachable', schema_ok: null };
    await ui.A['aim-worker-test']({ dataset: { worker: 'RANKER' } });
    expect(text()).toContain('LiteLLM is unreachable');
    answer = { success: false, schema_ok: false, model: 'local-reasoning' };
    await ui.A['aim-worker-test']({ dataset: { worker: 'RANKER' } });
    expect(text()).toContain('✗ model local-reasoning · schema ✗');
  });

  it('changes models: enable, type, stop managing, sync', async () => {
    routes['PUT /api/settings/ai/models/local-fast'] = ok;
    routes['DELETE /api/settings/ai/models/local-fast'] = ok;
    routes['POST /api/settings/ai/models/sync'] = () => ({ models: 5, added: ['new-one'] });
    routes['GET /api/settings/ai'] = () => ({ ...overview(), models: [model('local-fast', { managed: true, present_in_litellm: false, health_status: 'HEALTHY', health_latency_ms: 90 }), model('bad', { enabled: false, health_status: 'UNHEALTHY' })] });
    await open('models');
    expect(text()).toContain('Managed'); expect(text()).toContain('Not in LiteLLM'); expect(text()).toContain('Healthy'); expect(text()).toContain('Unhealthy');
    await ui.A['aim-model-toggle']({ dataset: { name: 'local-fast', on: '' } });
    expect(body('PUT', '/api/settings/ai/models/local-fast')).toEqual({ revision: 7, enabled: false });
    await ui.A['aim-model-type']({ dataset: { name: 'local-fast' }, value: 'FAST' });
    expect(body('PUT', '/api/settings/ai/models/local-fast')).toEqual({ revision: 7, model_type: 'FAST' });
    await ui.A['aim-model-unmanage']({ dataset: { name: 'local-fast' } });
    expect(call('DELETE', '/api/settings/ai/models/local-fast?revision=7').length).toBe(1);
    confirm.mockReturnValueOnce(false);
    ui.A['aim-model-unmanage']({ dataset: { name: 'local-fast' } });
    await ui.A['aim-sync']();
    expect(document.getElementById('toast').textContent).toBe('5 models discovered, 1 new');
    routes['GET /api/settings/ai'] = () => ({ ...overview(), models: [] });
    await open('models');
    expect(text()).toContain('No models yet');
  });

  it('adds a model even when Ollama is unreachable, and requires a name', async () => {
    routes['GET /api/settings/ai/ollama'] = () => [{ detail: 'down' }, 500];
    await open('models');
    await ui.A['aim-add-toggle'](); await flush();
    expect(text()).toContain('did not answer'); expect(document.getElementById('aim-add-ollama_model').tagName).toBe('INPUT');
    ui.A['aim-add']();
    expect(document.getElementById('toast').textContent).toContain('Give the model');
    await ui.A['aim-add-toggle']();
    expect(ui.AIM.adding).toBe(false);
  });

  it('edits tiers and saves routing', async () => {
    routes['PUT /api/settings/ai/tiers/FAST'] = ok;
    routes['PUT /api/settings/ai/tiers/CHEAP'] = ok;
    routes['PUT /api/settings/ai/routing'] = ok;
    await open('routing');
    await ui.A['aim-tier-model']({ dataset: { tier: 'FAST' }, value: 'local-reasoning' });
    expect(body('PUT', '/api/settings/ai/tiers/FAST')).toEqual({ revision: 7, litellm_model_name: 'local-reasoning' });
    await ui.A['aim-tier-toggle']({ dataset: { tier: 'FAST', on: '' } });
    expect(body('PUT', '/api/settings/ai/tiers/FAST')).toEqual({ revision: 7, enabled: false });
    ui.A['aim-tier-add']();
    expect(call('PUT', '/api/settings/ai/tiers/CHEAP').length).toBe(0);
    ui.setF('aim-tier', 'new', 'cheap');
    await ui.A['aim-tier-add']();
    expect(body('PUT', '/api/settings/ai/tiers/CHEAP')).toEqual({ revision: 7, enabled: true });
    ui.setF('aim-route', 'classifier', 'CUSTOM'); ui.setF('aim-route', 'classifier_plugin', 'jobhunt_classifier.router');
    ui.setF('aim-route', 'manage_auto_router', false); ui.setF('aim-route', 'default_model', ''); ui.render();
    expect(text()).toContain('integrations/litellm/jobhunt_classifier.py');
    await ui.A['aim-route-save']();
    expect(body('PUT', '/api/settings/ai/routing')).toEqual({ revision: 7, auto_router_enabled: true, classifier: 'CUSTOM', classifier_plugin: 'jobhunt_classifier.router', manage_auto_router: false, default_model: null });
    expect(ui.S.forms['aim-route']).toBeUndefined();
  });

  it('saves a key, removes it, verifies sync and explains file mode', async () => {
    routes['PUT /api/settings/ai/connection'] = ok;
    routes['POST /api/settings/ai/verify'] = () => ({ status: 'OUT_OF_SYNC', differences: ['missing local-small'], missing_models: ['tier-x'] });
    routes['GET /api/settings/ai'] = () => ({ ...overview(), connection: { ...overview().connection, secret_storage: false, base_url_saved: '', base_url_env: 'http://env:4000', managed_config_path: '/srv/jobhunt_models.yaml', restart_command_configured: false, last_error: 'old error', status: 'AUTH_FAILED', sync_status: 'PENDING', last_sync_at: '2026-09-25T10:00:00', last_sync_error: 'restart needed' } });
    await open('litellm');
    expect(text()).toContain('Key storage is unavailable'); expect(text()).toContain('Using the server default http://env:4000');
    expect(text()).toContain('/srv/jobhunt_models.yaml'); expect(text()).toContain('restart LiteLLM after applying'); expect(text()).toContain('old error');
    ui.setF('aim-conn', 'api_key', 'sk-new-key-1234'); ui.setF('aim-conn', 'sync_mode', 'disabled');
    await ui.A['aim-conn-save']();
    expect(body('PUT', '/api/settings/ai/connection')).toEqual({ api_key: 'sk-new-key-1234', sync_mode: 'disabled' });
    expect(ui.S.forms['aim-conn']).toBeUndefined();
    await ui.A['aim-conn-key-remove']();
    expect(body('PUT', '/api/settings/ai/connection')).toEqual({ api_key: '' });
    await ui.A['aim-verify']();
    expect(text()).toContain('OUT_OF_SYNC — missing local-small — not served: tier-x');
  });

  it('closes the preview, reports a conflicting apply and a pending restart', async () => {
    routes['POST /api/settings/ai/preview'] = () => ({ base_version: 4, draft_revision: 7, can_apply: true, changes: [{ path: 'models.x.deployment_params', label: 'Model x · deployment', before: null, after: { num_ctx: 4096 }, kind: 'added' }], sync_plan: { mode: 'config_file', create: [{ model_name: 'x' }], remove: [{ model_name: 'y' }], restart_required: true }, validation: { errors: [], warnings: [], live_checked: false, live_error: 'unreachable' } });
    await open();
    await ui.A['aim-preview']();
    expect(text()).toContain('create x · remove y via config_file'); expect(text()).toContain('{"num_ctx":4096}'); expect(text()).toContain('could not be checked: unreachable');
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'PENDING', version: 5 });
    await ui.A['aim-apply']();
    expect(document.getElementById('toast').textContent).toContain('restart LiteLLM');
    await ui.A['aim-preview']();
    routes['POST /api/settings/ai/apply'] = () => [{ detail: { message: 'The active configuration is now v5' } }, 409];
    await ui.A['aim-apply']();
    expect(ui.AIM.preview).toBeNull(); expect(document.getElementById('toast').textContent).toContain('now v5');
    await ui.A['aim-preview']();
    routes['POST /api/settings/ai/apply'] = () => ({ status: 'UNCHANGED', version: 4 });
    await ui.A['aim-apply']();
    expect(document.getElementById('toast').textContent).toBe('Nothing to apply');
    await ui.A['aim-preview'](); ui.A['aim-preview-close']();
    expect(ui.AIM.preview).toBeNull();
  });

  it('handles an empty history and a failed history load', async () => {
    routes['GET /api/settings/ai/versions'] = () => [{ detail: 'nope' }, 500];
    await open('advanced'); await flush(); await flush();
    expect(ui.AIM.versions).toEqual([]); expect(text()).toContain('Nothing recorded yet');
    confirm.mockReturnValueOnce(false);
    ui.A['aim-restore']({ dataset: { id: 1, version: 1 } });
    expect(call('POST', '/api/settings/ai/versions/1/restore').length).toBe(0);
  });
});
