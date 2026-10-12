import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'BOOT', 'render', 'go', 'orchState', 'loadOrchestration', 'scheduleOrchPoll', 'stopOrchPoll',
  'orchPoll', 'orchPollDelay', 'orchLayout', 'orchFit', 'renderOrchestration'];
let ui, context, fetchMock, routes;

const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const types = { AI: { label: 'AI worker', symbol: '●' }, DETERMINISTIC: { label: 'Deterministic', symbol: '◆' },
  EXTERNAL: { label: 'External service', symbol: '■' }, HUMAN_GATE: { label: 'Human / policy gate', symbol: '◇' },
  ASYNC_WORKER: { label: 'Async worker', symbol: '↻' }, OUTCOME: { label: 'Outcome', symbol: '○' } };
const node = (id, label, nodeTypes, col, row, extra = {}) => ({ id, label, types: nodeTypes, primary_type: nodeTypes[0],
  position: { col, row }, workers: [], llm_stages: [], external: [], description: `${label} does its job`,
  monitoring_tab: null, screen: null, optional_ai: false, implemented: true, lane: 'pipeline', ...extra });
const llm = (extra = {}) => ({ requests: 328, succeeded: 324, failed: 4, success_rate: 0.987, failure_rate: 0.013,
  p50_latency_ms: 4900, p95_latency_ms: 8300, p99_latency_ms: 9900, avg_input_tokens: 4200, avg_output_tokens: 750,
  estimated_cost: 0, unpriced_requests: 0, free: true, retry_rate: 0.01, timeout_rate: 0, schema_checked: 300,
  schema_failures: 1, schema_failure_rate: 0.003, schema_pass_rate: 0.997, fallbacks: 2, fallback_rate: 0.006,
  escalations: 0, escalation_rate: 0, avg_context_utilization_percent: 40, requests_without_usage: 0, low_sample: false, ...extra });
const assignment = { worker_type: 'RANKER', label: 'Ranker', has_prompt: true, mode: 'TIER', configured: 'PRIMARY_REASONING',
  resolved_model: 'ollama/gpt-oss:20b', fallback_model: 'qwen-32b', escalation: null, enabled: true, config_version: 17,
  note: null, router: null };
const graph = () => ({
  generated_at: '2026-10-04T10:00:00', scope: { range: '7d', all_accounts: false },
  gateway: { mode: 'litellm', provider: 'litellm', config_version: 17 },
  topology: { node_types: types, lanes: ['pipeline'], reserved: [{ id: 'company_insights', label: 'Company insights' }],
    nodes: [node('discovery', 'Discovery', ['EXTERNAL', 'DETERMINISTIC'], 0, 0, { external: [{ provider: 'APIFY' }] }),
      node('domain_filter', 'Domain filter', ['DETERMINISTIC'], 1, 0),
      node('ranker', 'Ranker', ['AI', 'ASYNC_WORKER'], 2, 0, { workers: ['RANKER'], llm_stages: ['RANKER'], monitoring_tab: 'llm' }),
      node('tailor', 'Tailor', ['AI', 'ASYNC_WORKER'], 2, 1, { workers: ['TAILOR'], llm_stages: ['TAILOR'] }),
      node('approval', 'Approval', ['HUMAN_GATE'], 1, 1),
      node('archived', 'Archived', ['OUTCOME'], 3, 0)],
    edges: [{ id: 'discovery__domain_filter', source: 'discovery', target: 'domain_filter', label: 'imported', kind: 'flow', measure: 'jobs created', timed: true },
      { id: 'domain_filter__ranker', source: 'domain_filter', target: 'ranker', label: 'kept', kind: 'flow', measure: 'kept' },
      { id: 'ranker__tailor', source: 'ranker', target: 'tailor', label: 'qualified', kind: 'branch', measure: 'QUALIFIED' },
      { id: 'ranker__archived', source: 'ranker', target: 'archived', label: 'archived', kind: 'branch', measure: 'ARCHIVED' },
      { id: 'tailor__approval', source: 'tailor', target: 'approval', label: 'tailored', kind: 'flow', measure: 'variants' }] },
  nodes: {
    discovery: { status: 'HEALTHY', status_reasons: [], live: {}, assignments: [], llm: null, external: { requests: 40, success_rate: 1, p95_latency_ms: 900 }, tasks: null, recommendations: 0 },
    domain_filter: { status: 'NO_DATA', status_reasons: ['Nothing recorded in this range'], live: {}, assignments: [], llm: null, tasks: null, recommendations: 0 },
    ranker: { status: 'HEALTHY', status_reasons: [], live: { running: 0 }, assignments: [assignment], llm: llm(), observed_models: ['ollama/gpt-oss:20b'], tasks: { tasks: 300, completed: 296, failed: 4, median_run_seconds: 9 }, recommendations: 2 },
    tailor: { status: 'ERROR', status_reasons: ['The last 3 model calls in the past 24h failed'], live: {}, assignments: [{ ...assignment, worker_type: 'TAILOR', label: 'Tailor', mode: 'AUTO_ROUTER', resolved_model: 'jobhunt-auto', router: { active: true, model: 'jobhunt-auto', classifier: 'HEURISTIC', tiers: { SIMPLE: 'llama3.2:3b', COMPLEX: 'gpt-oss:20b' } } }], llm: llm({ requests: 3, failed: 3, success_rate: 0 }), recommendations: 0 },
    approval: { status: 'HEALTHY', status_reasons: [], live: { waiting: 3 }, assignments: [], llm: null, recommendations: 0 },
    archived: { status: 'HEALTHY', status_reasons: [], live: {}, assignments: [], llm: null, recommendations: 0 },
  },
  edges: { discovery__domain_filter: { count: 482, median_seconds: 2 }, domain_filter__ranker: { count: 451 },
    ranker__tailor: { count: 174 }, ranker__archived: { count: 277 }, tailor__approval: { count: 90 } },
  system: { status: 'ERROR', statuses: { ERROR: 1 } },
  health_policy: { rules: [{ status: 'ERROR', rule: 'The last 3 model calls within 24h all failed' }] },
});
const live = (packets = []) => ({ counts: { ranker: { running: packets.length ? 1 : 0, queued: 0, retrying: 0 }, approval: { waiting: 3 } },
  packets, active: packets.length > 0, total_running: packets.length ? 1 : 0, total_queued: 0 });
const packet = { job_id: 1842, title: 'Senior Backend Engineer', company: 'Stripe', node_id: 'tailor', node_label: 'Tailor',
  activity: 'RUNNING', since: '2026-10-04T09:59:40', attempt: 1, max_attempts: 2, model: 'gpt-oss:20b' };
const jobState = (overrides = {}) => ({
  packet: { job_id: 1842, title: 'Senior Backend Engineer', company: 'Stripe', node_id: 'tailor', node_label: 'Tailor',
    activity: 'RUNNING', since: '2026-10-04T09:59:40', attempt: 1, max_attempts: 3, model: 'gpt-oss:20b', model_source: 'telemetry',
    active: true, terminal: false, ...overrides },
  path: [{ node_id: 'discovery', label: 'Discovery', status: 'COMPLETED' }, { node_id: 'domain_filter', label: 'Domain filter', status: 'COMPLETED' },
    { node_id: 'ranker', label: 'Ranker', status: 'COMPLETED', llm: { latest: { actual_model: 'ollama/gpt-oss:20b', latency_ms: 8200 } } },
    { node_id: 'tailor', label: 'Tailor', status: overrides.activity === 'RETRYING' ? 'RETRYING' : 'CURRENT', current: true, activity: overrides.activity || 'RUNNING' },
    { node_id: 'approval', label: 'Approval', status: 'WAITING' }],
});
const trace = { job_id: 1842, title: 'Senior Backend Engineer', company: 'Stripe', truncated: false, trace_ids: ['abcdef0123456789'],
  sources: { workflow: 1, llm: 1 }, note: 'Metadata only.', events: [
    { at: '2026-10-04T09:30:01', source: 'workflow', kind: 'DISCOVERED', title: 'Discovered' },
    { at: '2026-10-04T09:30:13', source: 'llm', kind: 'LLM_CALL', title: 'Ranker model call', status: 'SUCCEEDED',
      llm: { requested_model: 'tier-2', actual_model: 'ollama/gpt-oss:20b', model_mode: 'TIER', latency_ms: 8200, input_tokens: 4372, output_tokens: 812, schema_valid: true, fallback_used: false } }] };
const nodeDetail = () => ({ ...graph().nodes.ranker, node: graph().topology.nodes[2],
  recommendations: [{ title: 'Assign Ranker directly', priority: 'HIGH', category: 'MODEL', recommended_change: 'Use PRIMARY_REASONING' }],
  routing: { modes: { TIER: 300 }, configured: { PRIMARY_REASONING: 300 }, requested: { 'tier-2': 300 }, actual: { 'ollama/gpt-oss:20b': 298 },
    actual_unknown: 2, auto_router_requests: 0, tiers_recorded: false, tiers: [], routing_causes: {}, primary_requests: 296,
    fallback_requests: 2, fallback_unknown: 2, escalations: 0, escalation_reasons: {}, config_versions: [17], assignments: [assignment] },
  recent: { successes: [], failures: [{ at: '2026-10-04T09:00:00', actual_model: null, requested_model: 'tier-2', latency_ms: 120000, error_type: 'TIMEOUT', job_id: 7 }], retries: [] } });
const history = { node_id: 'ranker', note: 'Observed metrics; not proof that one model is better.', mode_note: null, workers: [{ worker_type: 'RANKER', label: 'Ranker',
  periods: [{ model: 'gpt-oss-20b', mode: 'FIXED', assignment: 'gpt-oss-20b', from_version: 4, to_version: null, started_at: '2026-09-28T00:00:00', ended_at: null, current: true, changed_by: 'me', observed: { requests: 182, success_rate: 0.991, p95_latency_ms: 7900, avg_input_tokens: 4800, low_sample: false } },
    { model: 'qwen-14b', mode: 'FIXED', assignment: 'qwen-14b', from_version: 2, to_version: 3, started_at: '2026-09-14T00:00:00', ended_at: '2026-09-28T00:00:00', current: false, observed: { requests: 11, success_rate: 0.9, p95_latency_ms: 14200, avg_input_tokens: 5100, low_sample: true } }],
  changes: [{ version: 4, at: '2026-09-28T00:00:00', changed_by: 'me', from: { model: 'qwen-14b', mode: 'FIXED' }, to: { model: 'gpt-oss-20b', mode: 'FIXED' } }],
  by_actual_model: [{ model: 'gpt-oss-20b', requests: 182, success_rate: 0.991, p95_latency_ms: 7900, avg_input_tokens: 4800, first_seen: '2026-09-28T00:00:00', last_seen: '2026-10-04T00:00:00', low_sample: false },
    { model: 'qwen-14b', requests: 11, success_rate: 0.9, p95_latency_ms: 14200, avg_input_tokens: 5100, first_seen: '2026-09-14T00:00:00', last_seen: '2026-09-28T00:00:00', low_sample: true }] }] };

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  routes = { graph: graph(), live: live(), job: jobState(), trace, node: nodeDetail(), history, fail: false };
  fetchMock = vi.fn(async url => {
    if (routes.fail && url.includes('/api/orchestration/')) return response({ detail: 'Orchestration is unavailable' }, 503);
    return response(url.includes('/workspace/summary') ? ui.D.summary
      : url.includes('/orchestration/graph') ? routes.graph
      : url.includes('/orchestration/live') ? routes.live
      : /\/orchestration\/jobs\/\d+\/trace/.test(url) ? routes.trace
      : /\/orchestration\/jobs\/\d+/.test(url) ? routes.job
      : url.includes('/orchestration/jobs?') ? { items: [{ job_id: 1842, title: 'Senior Backend Engineer', company: 'Stripe' }] }
      : url.includes('/history') ? routes.history
      : url.includes('/orchestration/nodes/') ? routes.node
      : url.includes('/orchestration/edges/') ? { label: 'qualified', measure: 'ranking decisions QUALIFIED', count: 174, jobs: 174, last_at: '2026-10-04T09:00:00', median_seconds: null, timed_items: 0, timing_note: 'No per-item time is recorded for this transition', split: [{ target: 'tailor', label: 'qualified', count: 174, share: 0.386 }, { target: 'archived', label: 'archived', count: 277, share: 0.614 }], recent: [] }
      : url.includes('/monitoring/logs') ? { total: 0, page: 1, pages: 1, per_page: 50, levels: [], all_categories: [], categories: [], items: [] }
      : url.includes('/monitoring/') ? { report: null, tasks: [], active: false } : {});
  });
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history: window.history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock;
  ui.stopOrchPoll();
  Object.assign(ui.S, { forms: {}, busy: {}, screen: 'orchestration', orch: null, mon: null });
  ui.BOOT.user.is_admin = true;
  for (const key of ['orchGraph', 'orchLive', 'orchJob', 'orchTrace', 'orchNode', 'orchHistory', 'orchEdge', 'orchError', 'orchJobResults']) delete ui.D[key];
  ui.D.summary = { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
    auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } };
});

const view = () => document.getElementById('view');
const flush = () => new Promise(r => setTimeout(r, 0));
async function open() { await ui.loadOrchestration(); ui.render(); return view(); }

describe('AI Orchestration workspace', () => {
  it('is a top-level workspace of its own, next to Monitoring and outside it', () => {
    ui.render();
    const groups = [...document.querySelectorAll('#nav .nav-group')].map(g => [g.querySelector('.nav-caps').textContent, [...g.querySelectorAll('.nav-btn')].map(a => a.textContent.trim())]);
    const system = groups.find(([caps]) => caps === 'SYSTEM');
    expect(system[1].map(t => t.replace(/—$/, ''))).toEqual(['AI Orchestration', 'Running workers', 'Monitoring']);
    expect(groups.find(([caps]) => caps === 'WORKSPACE')[1].join(' ')).not.toMatch(/Monitoring|Orchestration/);
    expect(document.getElementById('top').textContent).toContain('AI Orchestration');
  });

  it('shows a loading state, then an error state with a retry when the API fails', async () => {
    ui.render();
    expect(view().textContent).toContain('Loading');
    routes.fail = true;
    await open();
    expect(view().textContent).toContain('AI Orchestration could not load');
    expect(view().textContent).toContain('Orchestration is unavailable');
    routes.fail = false;
    ui.A['orch-retry']();
    await flush(); await flush(); await flush();
    expect(view().querySelector('[data-orch-canvas]')).not.toBeNull();
  });

  it('draws every node from the registry with its kind, model and a text status', async () => {
    await open();
    const nodes = [...view().querySelectorAll('.orch-node')];
    expect(nodes.map(n => n.dataset.id)).toEqual(['discovery', 'domain_filter', 'ranker', 'tailor', 'approval', 'archived']);
    const ranker = view().querySelector('[data-id="ranker"].orch-node');
    expect(ranker.textContent).toContain('ollama/gpt-oss:20b');
    expect(ranker.textContent).toContain('Tier · PRIMARY_REASONING');
    expect(ranker.textContent).toContain('p95 8.3s');
    expect(ranker.querySelector('.orch-rec').textContent).toBe('2');
    expect(ranker.getAttribute('aria-label')).toContain('AI worker');
    const tailor = view().querySelector('[data-id="tailor"].orch-node');
    expect(tailor.classList.contains('status-ERROR')).toBe(true);
    expect(tailor.textContent).toContain('Error');  // never colour alone
    expect(tailor.textContent).toContain('Auto router');
    expect(view().querySelector('[data-id="domain_filter"].orch-node').textContent).toContain('Deterministic');
    expect(view().querySelector('[data-id="approval"].orch-node').textContent).toContain('3 waiting');
    expect(view().querySelectorAll('.orch-edge').length).toBe(5);
    expect(view().textContent).toContain('System');
  });

  it('opens a node drawer with configured, resolved and observed models, routing and recommendations', async () => {
    await open();
    await ui.A['orch-node']({ dataset: { id: 'ranker' } });
    const drawer = document.querySelector('.orch-drawer');
    expect(fetchMock.mock.calls.some(([u]) => u.startsWith('/api/orchestration/nodes/ranker?'))).toBe(true);
    expect(drawer.textContent).toContain('Configured');
    expect(drawer.textContent).toContain('PRIMARY_REASONING');
    expect(drawer.textContent).toContain('Requests go to');
    expect(drawer.textContent).toContain('qwen-32b');
    expect(drawer.textContent).toContain('Actually answered (telemetry)');
    expect(drawer.textContent).toContain('Assign Ranker directly');
    expect(drawer.textContent).toContain('Recommendations only');
    expect(drawer.querySelector('[data-act="orch-settings-ai"]')).not.toBeNull();
    await ui.A['orch-tab']({ dataset: { id: 'routing' } });
    const routing = document.querySelector('.orch-drawer').textContent;
    expect(routing).toContain('Actually answered');
    expect(routing).toContain('2 with no model recorded (unknown)');
    await ui.A['orch-tab']({ dataset: { id: 'performance' } });
    expect(document.querySelector('.orch-drawer').textContent).toContain('328 requests');
    await ui.A['orch-tab']({ dataset: { id: 'activity' } });
    expect(document.querySelector('.orch-drawer').textContent).toContain('model unknown');
    ui.A['orch-close']();
    expect(document.querySelector('.orch-drawer')).toBeNull();
  });

  it('shows the model history with sample sizes and an observational note', async () => {
    await open();
    await ui.A['orch-node']({ dataset: { id: 'ranker' } });
    await ui.A['orch-tab']({ dataset: { id: 'history' } });
    const text = document.querySelector('.orch-drawer').textContent;
    expect(text).toContain('gpt-oss-20b');
    expect(text).toContain('182 requests');
    expect(text).toContain('small sample');
    expect(text).toContain('By the model that answered');
    expect(text).toContain('v4');
    expect(text).toContain('not proof');
    expect(document.querySelectorAll('.orch-periods li.current').length).toBe(1);
  });

  it('opens an edge drawer with the split and an honest timing note', async () => {
    await open();
    await ui.A['orch-edge']({ dataset: { source: 'ranker', target: 'tailor' } });
    const text = document.querySelector('.orch-drawer').textContent;
    expect(text).toContain('Ranker → Tailor');
    expect(text).toContain('39%');
    expect(text).toContain('No per-item time is recorded');
    expect(view().querySelector('.orch-edge.selected').dataset.target).toBe('tailor');
  });

  it('lists live packets, and selecting one follows the job through the graph', async () => {
    routes.live = live([packet]);
    await open();
    expect(view().querySelector('.orch-packets').textContent).toContain('Senior Backend Engineer');
    await ui.A['orch-packet']({ dataset: { id: '1842' } });
    expect(ui.orchState().mode).toBe('live');
    const current = view().querySelector('.orch-node.is-current');
    expect(current.dataset.id).toBe('tailor');
    expect(view().querySelector('[data-id="ranker"].orch-node').classList.contains('step-COMPLETED')).toBe(true);
    expect(view().querySelector('[data-id="approval"].orch-node').classList.contains('ahead')).toBe(true);
    expect(view().querySelector('[data-id="archived"].orch-node').classList.contains('dim')).toBe(true);
    expect([...view().querySelectorAll('.orch-edge.walked')].map(e => e.dataset.target)).toContain('tailor');
    const card = view().querySelector('.orch-packet-card').textContent;
    expect(card).toContain('Tailor');
    expect(card).toContain('gpt-oss:20b');
    expect(card).toContain('recorded');
    expect(card).toContain('1 / 3');
    expect(view().querySelector('.orch-step.current').textContent).toContain('Tailor');
    expect(view().textContent).toContain('Execution timeline');
    expect(view().textContent).toContain('4.4K → 812 tokens');
  });

  it('marks a retrying and a failed current stage distinctly', async () => {
    routes.job = jobState({ activity: 'RETRYING' });
    await open();
    await ui.A['orch-pick-job']({ dataset: { id: '1842' } });
    expect(view().querySelector('[data-id="tailor"].orch-node').classList.contains('step-RETRYING')).toBe(true);
    expect(view().querySelector('.orch-step.current').classList.contains('step-RETRYING')).toBe(true);
    routes.job = { ...jobState({ activity: 'FAILED', active: false }), path: jobState().path.map(s => s.node_id === 'tailor' ? { ...s, status: 'FAILED', activity: 'FAILED' } : s) };
    await ui.A['orch-pick-job']({ dataset: { id: '1842' } });
    expect(view().querySelector('[data-id="tailor"].orch-node').classList.contains('step-FAILED')).toBe(true);
    expect(view().querySelector('.orch-step.current').textContent).toContain('Failed');
  });

  it('opens the trace drawer with requested and actual models and links to Monitoring logs', async () => {
    await open();
    await ui.A['orch-pick-job']({ dataset: { id: '1842' } });
    await ui.A['orch-trace']();
    const modal = document.querySelector('[aria-label="Job trace"]').textContent;
    expect(modal).toContain('tier-2');
    expect(modal).toContain('ollama/gpt-oss:20b');
    expect(modal).toContain('Metadata only');
    ui.A['orch-mon-trace']({ dataset: { id: 'abcdef0123456789' } });
    expect(ui.S.screen).toBe('monitoring');
    expect(ui.S.mon.tab).toBe('logs');
    expect(ui.S.mon.logs.trace_id).toBe('abcdef0123456789');
  });

  it('renders History mode with throughput on edges and a per-stage table', async () => {
    await open();
    ui.A['orch-mode']({ dataset: { id: 'history' } });
    const labels = [...view().querySelectorAll('.orch-edge-label')].map(t => t.textContent);
    expect(labels).toContain('482 · 2.0s');
    expect(labels).toContain('277');
    expect(view().textContent).toContain('How work moved in this range');
  });

  it('shows an empty state when nothing is running', async () => {
    await open();
    expect(view().querySelector('.orch-packets').textContent).toContain('Nothing is running right now');
    expect(view().querySelector('.orch-packets').textContent).toContain('3 at Approval');
  });

  it('polls fast while work runs, slowly when idle, and stops off-screen or in a hidden tab', async () => {
    await open();
    expect(ui.orchPollDelay()).toBe(30000);
    ui.D.orchLive = live([packet]);
    expect(ui.orchPollDelay()).toBe(4000);
    ui.scheduleOrchPoll();
    expect(runInContext('orchTimer', context)).not.toBeNull();
    ui.S.screen = 'overview';
    expect(ui.orchPollDelay()).toBeNull();
    await ui.orchPoll();
    expect(runInContext('orchTimer', context)).toBeNull();
    ui.S.screen = 'orchestration';
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    expect(ui.orchPollDelay()).toBeNull();
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
  });

  it('does not poll a finished job again', async () => {
    await open();
    ui.orchState().jobId = 1842;
    ui.D.orchJob = jobState({ terminal: true, active: false, node_id: null });
    const before = fetchMock.mock.calls.length;
    await ui.orchPoll();
    const calls = fetchMock.mock.calls.slice(before).map(([u]) => u);
    expect(calls.some(u => u.includes('/orchestration/jobs/'))).toBe(false);
    expect(calls.some(u => u.includes('/orchestration/live'))).toBe(true);
    ui.stopOrchPoll();
  });

  it('offers the every-account view to the admin only', async () => {
    await open();
    expect(view().querySelector('[data-act="orch-all"]')).not.toBeNull();
    ui.BOOT.user.is_admin = false;
    ui.render();
    expect(view().querySelector('[data-act="orch-all"]')).toBeNull();
    ui.BOOT.user.is_admin = true;
    ui.orchState().all = true;
    await ui.loadOrchestration();
    expect(fetchMock.mock.calls.some(([u]) => u.includes('all_accounts=true'))).toBe(true);
    ui.render();
    expect(view().textContent).toContain('Packets, job titles and traces stay your own');
  });

  it('pans, zooms and fits without re-rendering the page', async () => {
    await open();
    ui.A['orch-zoom']({ dataset: { id: 'fit' } });
    const fitted = { ...ui.orchState().view };
    ui.A['orch-zoom']({ dataset: { id: 'in' } });
    expect(ui.orchState().view.k).toBeGreaterThan(fitted.k);
    expect(view().querySelector('[data-orch-world]').style.transform).toContain(`scale(${ui.orchState().view.k})`);
    ui.A['orch-zoom']({ dataset: { id: 'out' } });
    ui.A['orch-zoom']({ dataset: { id: 'reset' } });
    expect(ui.orchState().view.k).toBeGreaterThanOrEqual(fitted.k);
  });

  it('crosses over from a Monitoring request to the job it belongs to', async () => {
    ui.S.screen = 'monitoring';
    ui.A['orch-view-job']({ dataset: { id: '1842' } });
    expect(ui.S.screen).toBe('orchestration');
    expect(ui.orchState().jobId).toBe(1842);
    expect(ui.orchState().mode).toBe('live');
    await flush(); await flush(); await flush();
    expect(fetchMock.mock.calls.some(([u]) => u === '/api/orchestration/jobs/1842')).toBe(true);
  });

  it('escapes job titles and model names it did not write', async () => {
    routes.live = live([{ ...packet, title: '<img src=x onerror=alert(1)>', model: '<b>x</b>' }]);
    await open();
    expect(view().querySelector('.orch-packets img')).toBeNull();
    expect(view().querySelector('.orch-packets').textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('shows compact model health on an AI node and the model comparison in its details', async () => {
    const group = (key, extra = {}) => ({ key, label: key, requests: 328, low_sample: false, share: { requests: 1 },
      reliability: { success_rate: 0.987, retry_adjusted_success_rate: 0.95, schema_pass_rate: 0.992 },
      latency: { p50_ms: 4200, p95_ms: 8100, ttft_p50_ms: null, tokens_per_sec: 42.3 },
      throughput: {}, tokens: {}, context: {}, quality: {}, cost: { free: true, per_request: 0 }, ...extra });
    routes.graph.nodes.ranker.model_health = [{ model: 'gpt-oss:20b', requests: 328, success_rate: 0.987, p95_latency_ms: 8100,
      requests_per_min: 0.5, schema_pass_rate: 0.992, cost: 0, free: true }];
    routes.node.performance = { series_by: 'day', models: { totals: { requests: 328 }, groups: [group('gpt-oss:20b')] },
      config_versions: [group('v17')], series: [group('2026-10-04')] };
    await open();
    const line = view().querySelector('[data-id="ranker"].orch-node .orch-node-metric').textContent;
    expect(line).toContain('gpt-oss:20b');
    expect(line).toContain('99% ok');
    expect(line).toContain('p95 8.1s');
    expect(line).toContain('schema 99%');
    expect(line).toContain('$0');
    await ui.A['orch-node']({ dataset: { id: 'ranker' } });
    await ui.A['orch-tab']({ dataset: { id: 'performance' } });
    const drawer = view().querySelector('.orch-drawer-body').textContent;
    expect(drawer).toContain('By model that answered');
    expect(drawer).toContain('42');
    expect(drawer).toContain('TTFT');
    expect(drawer).toContain('—');  // TTFT was not reported: a dash, never a zero
  });
});
