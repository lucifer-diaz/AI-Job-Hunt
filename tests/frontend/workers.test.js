import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'go', 'wkState', 'loadWorkers', 'wkPoll', 'wkPollDelay', 'scheduleWkPoll',
  'stopWkPoll', 'readHash', 'WK_KINDS'];
let ui, context, fetchMock, confirmMock, routes;

const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const counts = (running = 0, queued = 0, retrying = 0) => ({ running, queued, retrying, active: running + queued + retrying });
const summary = (over = {}) => ({
  cooldowns: [], active: 4, running: 1,
  workers: [
    { kind: 'parsing', label: 'JD parsing', description: 'Parses postings', counts: counts(1, 1, 1), scheduler: { enabled: true, interval_minutes: 2, running: false, next_run_at: null } },
    { kind: 'recovery', label: 'JD recovery', description: 'Reads postings', counts: counts(), scheduler: { enabled: true, interval_minutes: 2 } },
    { kind: 'ranking', label: 'Ranking', description: 'Scores fit', counts: counts(0, 1), scheduler: { enabled: true, interval_minutes: 2 } },
    { kind: 'tailoring', label: 'Tailoring', description: 'Tailors packets', counts: counts(), scheduler: { enabled: true, poll_seconds: 2, concurrency: 2 } }],
  ...over });
const row = (over = {}) => ({ task_id: 11, job_id: 7, title: 'Staff SWE', company: 'Canva', kind: 'parsing', status: 'RUNNING',
  stage: 'running', attempt: 1, max_attempts: 3, started_at: '2026-10-04T10:00:00', next_attempt_at: null, last_error: null,
  error_category: null, run_now: false, can_kill: true, can_run_now: false, can_remove: false, ...over });
const parsing = () => ({ kind: 'parsing', metrics: { tasks_created: 92, completed: 56, failed: 6, retrying: 22, success_rate: 0.903,
  average_duration_ms: 88300, jobs_parsed: 64, failures_by_category: {}, top_errors: [],
  queue: { pending: 22, processing: 8, oldest_pending_at: null },
  llm: { requests: 182, failed: 95, input_tokens: 260611, output_tokens: 1132032, estimated_cost: 0, average_latency_ms: 139500, models: {} },
  scheduler: null },
  rows: [row(), row({ task_id: 12, status: 'RETRYING', can_kill: true, can_run_now: true, attempt: 2, next_attempt_at: '2099-01-01T00:00:00', last_error: 'LLMRateLimitError: timed out' }),
    row({ task_id: 13, status: 'FAILED', can_kill: false, can_run_now: true, last_error: 'ValidationError' })] });

const hours = () => [...Array(24)].map((_, i) => ({ hour: `2026-10-04T${String(i).padStart(2, '0')}:00:00`, queued: i === 23 ? 4 : 0, done: i === 22 ? 6 : 0, failed: i === 23 ? 2 : 0 }));
const activity = () => ({ kind: 'parsing', hours: hours(), done: 6, failed: 2, queued: 4, success_rate: 0.75, median_run_seconds: 95, median_wait_seconds: 30 });
const settings = () => ({ application_mode: 'manual_approval', min_review_score: 70, ranking_concurrency: 3, tailoring_concurrency: 2,
  ranking_interval_minutes: 5, processing_maximums: { ranking_concurrency: 5, tailoring_concurrency: 2 }, ranking_interval_server_minutes: 2,
  rate_limits: { enabled: true, base_seconds: 30, multiplier: 2, max_seconds: 900 }, auto_tailor: { enabled: false } });

const field = (name, type, def, min = 1, max = 10) => ({ name, label: name.replace(/_/g, ' '), help: '', type, min, max, default: def });
const workerSettings = (editable = true) => ({
  account: {
    parsing: { values: { auto: true, max_attempts: 3, retry_seconds: 120 }, fields: [field('auto', 'bool', true), field('max_attempts', 'int', 3), field('retry_seconds', 'int', 120, 10, 86400)] },
    recovery: { values: { auto: true, max_attempts: 3, retry_seconds: 300, use_browser: true, use_model: true }, fields: [field('auto', 'bool', true), field('max_attempts', 'int', 3), field('retry_seconds', 'int', 300, 10, 86400), field('use_browser', 'bool', true), field('use_model', 'bool', true)] },
    ranking: { values: { auto: false, max_attempts: 3, retry_base_seconds: 60, retry_max_seconds: 3600 }, fields: [field('auto', 'bool', true), field('max_attempts', 'int', 3), field('retry_base_seconds', 'int', 60, 10, 86400), field('retry_max_seconds', 'int', 3600, 10, 86400)] },
    tailoring: { values: { auto: true, max_attempts: 2 }, fields: [field('auto', 'bool', true), field('max_attempts', 'int', 2, 1, 5)] } },
  server: { jd_parse_batch_size: { worker: 'parsing', label: 'Tasks per run', help: '', min: 1, max: 200, value: 10, default: 10, overridden: false },
    ranking_interval_minutes: { worker: 'ranking', label: 'Run every (minutes)', help: '', min: 0, max: 1440, value: 2, default: 2, overridden: false } },
  server_editable: editable, schedules: {} });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":false}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  confirmMock = vi.fn(() => true);
  vi.stubGlobal('confirm', confirmMock);
  routes = { summary: summary(), parsing: parsing(), activity: activity(), settings: settings(), workerSettings: workerSettings(), fail: false };
  fetchMock = vi.fn(async (url, opt = {}) => {
    if (routes.fail && url.startsWith('/api/workers')) return response({ detail: 'Workers are unavailable' }, 503);
    if (opt.method === 'POST' && url.includes('/run-now')) return response(row({ status: 'QUEUED', run_now: true }));
    if (opt.method === 'POST' && url.includes('/cooldowns/reset')) return response({ cleared: ['OPENROUTER'] });
    if (opt.method === 'POST' && url.includes('kill')) return response({ stopped: 1, requeued: 2, cooldowns_cleared: url.endsWith('kill') ? [] : ['OPENROUTER'] });
    if (opt.method === 'POST' && url.endsWith('/retry')) { routes.retryPost = { url, body: JSON.parse(opt.body) }; return response({ kind: 'parsing', retried: routes.retryPost.body.task_ids.slice(1).map(id => ({ task_id: id, new_task_id: id + 100, job_id: 1 })), skipped: [{ task_id: routes.retryPost.body.task_ids[0], reason: 'Already run again as task #99' }] }); }
    if (opt.method === 'PUT' && url === '/api/workers/settings') { routes.accountPut = JSON.parse(opt.body); return response({ account: routes.workerSettings.account }); }
    if (opt.method === 'PUT' && url === '/api/workers/server-settings') { routes.serverPut = JSON.parse(opt.body); return response({ server: routes.workerSettings.server }); }
    if (opt.method === 'PATCH' && url === '/api/settings') { routes.patched = JSON.parse(opt.body); return response({ ...routes.settings, ...routes.patched }); }
    return response(url.includes('/workspace/summary') ? ui.D.summary
      : url === '/api/workers' ? routes.summary
      : url === '/api/workers/settings' ? routes.workerSettings
      : url.endsWith('/activity') ? routes.activity
      : url === '/api/settings' ? routes.settings
      : url.startsWith('/api/workers/parsing') ? routes.parsing
      : url.startsWith('/api/workers/recovery') ? { kind: 'recovery', rows: [], metrics: { by_result: {} }, diagnostics: { http: {}, playwright: {}, llm: {}, settings: {} } }
      : url.startsWith('/api/ranking/tracker') ? { counts: { filtering: 0, queued: 1, processing: 0, retry_scheduled: 0, waiting: 0, completed: 0, failed: 0, skipped: 0, qualified: 0, archived: 0 },
        progress: { finished: 0, total: 1 }, concurrency: 2, interval_minutes: 5, active: [{ id: 31, job_id: 9, title: 'AI Engineer', company: 'Grab', status: 'PENDING', attempt_count: 0, max_attempts: 3, reason: 'new' }],
        recent: [], locations: [], worker: { enabled: true, interval_minutes: 2 }, auto_rank: true }
      : url.startsWith('/api/tailor-tasks') ? [{ id: 41, job_id: 5, title: 'Backend Lead', company: 'Stripe', mode: 'tailor', status: 'running', stage: 'tailoring', progress: 25, started_at: '2026-10-04T10:00:00' }]
      : {});
  });
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history: window.history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirmMock;
  ui.stopWkPoll();
  window.history.replaceState(null, '', '#workers/parsing');  // a hash left by an earlier test must not switch tabs
  Object.assign(ui.S, { forms: {}, busy: {}, screen: 'workers', wk: null, rf: { q: '', location: 'all' } });
  for (const key of ['wk', 'wkData', 'wkAct', 'ranking', 'tasks', 'settings', 'wkSettings']) delete ui.D[key];
  ui.D.summary = { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
    auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } };
});

const view = () => document.getElementById('view');
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0)); };
async function open(tab = 'parsing') { ui.wkState().tab = tab; await ui.loadWorkers(); ui.render(); return view(); }
const posts = () => fetchMock.mock.calls.filter(([, opt]) => opt?.method === 'POST').map(([u]) => u);

describe('Running workers', () => {
  it('sits in the SYSTEM group between AI Orchestration and Monitoring, with a live count', async () => {
    await open();
    const system = [...document.querySelectorAll('#nav .nav-group')].find(g => g.querySelector('.nav-caps').textContent === 'SYSTEM');
    const items = [...system.querySelectorAll('.nav-btn')].map(a => a.textContent.trim());
    expect(items).toEqual(['AI Orchestration—', 'Running workers4', 'Monitoring—']);
  });

  it('has a tab per worker with its queue size, kept in the URL', async () => {
    await open();
    const tabs = [...view().querySelectorAll('[data-act="wk-tab"]')].map(b => b.textContent.trim());
    expect(tabs).toEqual(['JD parsing 3', 'JD recovery', 'Ranking 1', 'Tailoring', '⚙ Settings']);
    ui.A['wk-tab']({ dataset: { id: 'recovery' } });
    expect(location.hash).toBe('#workers/recovery');
    await flush();
    expect(view().textContent).toContain('JD recovery');
  });

  it('shows the JD parsing metrics that moved from Monitoring, and its tasks with their controls', async () => {
    const v = await open();
    expect(v.textContent).toContain('Parse tasks');
    expect(v.textContent).toContain('90.3%');
    const rows = [...v.querySelectorAll('.wk-row')];
    expect(rows.map(r => r.querySelector('.orch-chip').textContent.trim())).toEqual(['● Running', '↻ Retrying', '✕ Failed']);
    expect(rows[0].querySelector('[data-act="wk-kill"]')).not.toBeNull();
    expect(rows[0].querySelector('[data-act="wk-run-now"]')).toBeNull();  // a running task cannot be run "now"
    expect(rows[1].textContent).toContain('attempt 2 of 3');
    expect(rows[1].textContent).toContain('timed out');
    expect(rows[2].querySelector('[data-act="wk-kill"]')).toBeNull();
    expect(rows[2].querySelector('[data-act="wk-run-now"]')).not.toBeNull();
  });

  it('kills one task, and asks before killing a whole worker or every worker', async () => {
    await open();
    await ui.A['wk-kill']({ dataset: { kind: 'parsing', id: '11' } });
    expect(posts()).toContain('/api/workers/parsing/tasks/11/kill');
    expect(document.getElementById('toast').textContent).toContain('Requeued 2 tasks fresh');
    confirmMock.mockReturnValueOnce(false);
    await ui.A['wk-kill-all']({ dataset: { kind: 'parsing' } });
    expect(posts()).not.toContain('/api/workers/parsing/kill-all');
    await ui.A['wk-kill-all']({ dataset: { kind: 'parsing' } });
    expect(confirmMock.mock.calls.at(-1)[0]).toContain('rate-limit backoff');
    expect(posts()).toContain('/api/workers/parsing/kill-all');
    expect(document.getElementById('toast').textContent).toContain('backoff cleared for openrouter');
    await ui.A['wk-kill-everything']();
    expect(posts()).toContain('/api/workers/kill-all');
  });

  it('runs a task now and says it is first in line', async () => {
    await open();
    await ui.A['wk-run-now']({ dataset: { kind: 'parsing', id: '12' } });
    expect(posts()).toContain('/api/workers/parsing/tasks/12/run-now');
    expect(document.getElementById('toast').textContent).toContain('runs next');
    routes.parsing = { ...parsing(), rows: [row({ status: 'RETRYING', run_now: true, can_run_now: true, attempt: 1 })] };
    await open();
    expect(view().querySelector('.wk-row').textContent).toContain('First in line');
  });

  it('shows the rate-limit backoff and resets it', async () => {
    routes.summary = summary({ cooldowns: [{ provider: 'OPENROUTER', seconds_left: 31, calls_skipped: 4 }] });
    await open();
    expect(view().textContent).toContain('openrouter 31s left');
    await ui.A['wk-reset-cooldowns']();
    expect(posts()).toContain('/api/workers/cooldowns/reset');
    expect(document.getElementById('toast').textContent).toContain('Backoff cleared for openrouter');
  });

  it('keeps the ranking tracker in the Ranking tab, with Run now on waiting jobs', async () => {
    const v = await open('ranking');
    expect(v.textContent).toContain('Queue unranked jobs');
    expect(v.textContent).toContain('AI Engineer');
    const rowEl = [...v.querySelectorAll('.lrow')].find(r => r.textContent.includes('AI Engineer'));
    expect(rowEl.querySelector('[data-act="wk-run-now"]').dataset.id).toBe('31');
    expect(rowEl.querySelector('[data-act="wk-kill"]')).toBeNull();  // nothing to stop yet
  });

  it('keeps the tailoring task list in the Tailoring tab, with Kill on a running task', async () => {
    const v = await open('tailoring');
    expect(v.textContent).toContain('Backend Lead');
    expect(v.querySelector('[data-act="wk-kill"][data-kind="tailoring"]').dataset.id).toBe('41');
  });

  it('sends old links to the ranking tracker here', () => {
    location.hash = '#discovery/ranking';
    ui.readHash();
    expect(ui.S.screen).toBe('workers');
    expect(ui.wkState().tab).toBe('ranking');
  });

  it('shows an empty queue, and an error when the workers cannot be read', async () => {
    routes.parsing = { ...parsing(), rows: [] };
    await open();
    expect(view().textContent).toContain('Nothing in this queue');
    routes.fail = true;
    await expect(ui.loadWorkers()).rejects.toThrow('Workers are unavailable');
  });

  it('polls fast while work runs, slowly when idle, and stops on another screen or in a hidden tab', async () => {
    await open();
    expect(ui.wkPollDelay()).toBe(3000);
    ui.D.wk = { ...ui.D.wk, active: 0 };
    expect(ui.wkPollDelay()).toBe(20000);
    ui.scheduleWkPoll();
    expect(runInContext('wkTimer', context)).not.toBeNull();
    ui.S.screen = 'overview';
    expect(ui.wkPollDelay()).toBeNull();
    await ui.wkPoll();
    expect(runInContext('wkTimer', context)).toBeNull();
    ui.S.screen = 'workers';
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    expect(ui.wkPollDelay()).toBeNull();
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
  });

  it('filters each tab by status and name, asking the server for parsing and recovery', async () => {
    routes.parsing = { ...parsing(), status_counts: { all: 3, active: 2, running: 1, queued: 0, retrying: 1, failed: 1, done: 0 } };
    const v = await open();
    const options = [...v.querySelectorAll('#wk-status-parsing option')].map(o => o.textContent);
    expect(options).toEqual(['All statuses (3)', 'Active (running, queued, retrying) (2)', 'Running (1)', 'Queued (0)', 'Retrying (1)', 'Failed or blocked (1)', 'Done (0)']);
    const status = v.querySelector('#wk-status-parsing');
    status.value = 'failed';
    status.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();
    expect(fetchMock.mock.calls.some(([u]) => u.startsWith('/api/workers/parsing?') && u.includes('status=failed'))).toBe(true);
    const search = view().querySelector('#wk-q-parsing');
    search.value = 'canva';
    search.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise(r => setTimeout(r, 300)); await flush();
    expect(fetchMock.mock.calls.some(([u]) => u.includes('q=canva') && u.includes('status=failed'))).toBe(true);
    expect(view().querySelector('[data-act="wk-clear-filters"]')).not.toBeNull();
    routes.parsing = { ...parsing(), rows: [] };
    await ui.loadWorkers(); ui.render();
    expect(view().textContent).toContain('No tasks match these filters');
    ui.A['wk-clear-filters']({ dataset: { tab: 'parsing' } });
    await flush();
    expect(ui.wkState().f.parsing).toEqual({ q: '', status: 'all' });
  });

  it('filters the ranking tracker by status and keeps its server search and location', async () => {
    const v = await open('ranking');
    expect(v.querySelector('#rf-q')).not.toBeNull();
    expect(v.querySelector('#rf-location')).not.toBeNull();
    expect([...v.querySelectorAll('#wk-status-ranking option')].map(o => o.textContent)).toContain('Queued (1)');
    ui.wkState().f.ranking = { q: '', status: 'running' };
    ui.render();
    expect(view().textContent).not.toContain('AI Engineer');
    expect(view().textContent).toContain('0 in progress');
    ui.wkState().f.ranking.status = 'queued';
    ui.render();
    expect(view().textContent).toContain('AI Engineer');
  });

  it('filters the tailoring list in place by name and status, keeping the totals', async () => {
    await open('tailoring');
    ui.wkState().f.tailoring = { q: 'stripe', status: 'running' };
    ui.render();
    expect(view().textContent).toContain('Backend Lead');
    ui.wkState().f.tailoring = { q: 'canva', status: 'all' };
    ui.render();
    expect(view().textContent).toContain('No tailoring tasks match these filters');
    expect(view().textContent).toContain('1 running');  // the header still counts every task
  });

  it('escapes job titles and errors', async () => {
    routes.parsing = { ...parsing(), rows: [row({ title: '<img src=x onerror=alert(1)>', last_error: '<b>x</b>', status: 'FAILED' })] };
    const v = await open();
    expect(v.querySelector('.wk-row img')).toBeNull();
    expect(v.querySelector('.wk-row').textContent).toContain('<img src=x onerror=alert(1)>');
  });

  it('shows the queue at a glance and the last 24 hours hour by hour', async () => {
    const v = await open();
    const kpis = [...v.querySelectorAll('.wk-kpi')].map(k => k.textContent.replace(/\s+/g, ' ').trim());
    expect(kpis[0]).toContain('Running 1');
    expect(kpis[3]).toContain('Finished · 24h 8');
    expect(kpis[3]).toContain('6 done · 2 failed');
    expect(v.querySelector('.wk-stack').getAttribute('aria-label')).toBe('1 running, 1 queued, 1 retrying');
    expect(v.querySelectorAll('.wk-col')).toHaveLength(24);
    expect(v.querySelectorAll('.wk-col')[23].dataset.tip).toContain('2 failed · 4 queued');
    const facts = v.querySelector('.wk-facts').textContent;
    for (const bit of ['75%', '1m 35s', '30s']) expect(facts).toContain(bit);
    expect(fetchMock.mock.calls.map(([u]) => u)).toContain('/api/workers/parsing/activity');
  });

  it('changes the metrics range without touching the task filters', async () => {
    await open();
    ui.A['wk-range']({ dataset: { id: '30d' } });
    await flush();
    expect(fetchMock.mock.calls.map(([u]) => u).some(u => u.startsWith('/api/workers/parsing?') && u.includes('range=30d'))).toBe(true);
    expect(view().querySelector('[data-act="wk-range"][data-id="30d"]').classList.contains('on')).toBe(true);
  });

  it('edits every worker setting in its Settings tab: limits, per-worker settings and the server (admin)', async () => {
    const v = await open('settings');
    expect(v.querySelector('#workers-ranking_concurrency').value).toBe('3');
    expect(v.querySelector('#workers-a__parsing__max_attempts').value).toBe('3');
    expect(v.querySelector('#workers-a__recovery__use_browser').checked).toBe(true);
    expect(v.querySelector('#workers-s__jd_parse_batch_size').value).toBe('10');
    expect(v.querySelector('#workers-a__ranking__auto').checked).toBe(false);
    expect(v.querySelector('#wk-set-ranking').textContent).toContain('Paused');
    expect([...v.querySelectorAll('.wk-ladder span')].map(x => x.textContent)).toEqual(['130s', '21m', '32m', '44m', '5+8m']);
    const set = (id, value) => { const el = v.querySelector(id); if (el.type === 'checkbox') { el.checked = value; el.dispatchEvent(new Event('change', { bubbles: true })); } else { el.value = value; el.dispatchEvent(new Event('input', { bubbles: true })); } };
    set('#workers-ranking_concurrency', '4');
    set('#workers-a__parsing__max_attempts', '5');
    set('#workers-s__jd_parse_batch_size', '25');
    set('#workers-a__recovery__use_model', false);
    expect(document.querySelector('[data-settings-save="workers"]').disabled).toBe(false);
    // A changed value says it is custom and offers its default back.
    ui.render();
    const row = view().querySelector('#workers-a__parsing__max_attempts').closest('.wk-srow');
    expect(row.classList.contains('custom')).toBe(true);
    expect(row.querySelector('[data-act="wk-set-reset"]').textContent).toBe('reset to 3');
    expect(view().querySelector('#workers-a__parsing__retry_seconds').closest('.wk-srow').textContent).toContain('Default · 2m');
    await ui.A['settings-save']({ dataset: { form: 'workers' } });
    await flush();
    expect(routes.patched).toEqual({ ranking_concurrency: 4 });
    expect(routes.accountPut).toEqual({ workers: { parsing: { max_attempts: 5 }, recovery: { use_model: false } } });
    expect(routes.serverPut).toEqual({ values: { jd_parse_batch_size: 25 } });
    expect(document.getElementById('toast').textContent).toContain('Changes saved');
    expect(ui.wkPollDelay()).toBeNull();  // no polling under a form
    window.history.replaceState(null, '', '#workers/settings');
    ui.readHash();
    expect(ui.wkState().tab).toBe('settings');
    ui.S.screen = 'settings'; ui.S.settingsSection = 'workflow'; ui.D.prefs = { weekly_target: 5, followup_days: 7 };
    ui.D.profile = {};
    ui.render();
    expect(view().querySelector('#workflow-ranking_concurrency')).toBeNull();
    expect(view().querySelector('a[href="#workers/settings"]')).not.toBeNull();
  });

  it('shows the server settings read-only to an account that is not an admin', async () => {
    routes.workerSettings = workerSettings(false);
    const v = await open('settings');
    expect(v.querySelector('#workers-s__jd_parse_batch_size')).toBeNull();
    const server = v.querySelector('#wk-set-parsing .wk-scol.server');
    expect(server.textContent).toContain('Tasks per run');
    expect(server.querySelector('.wk-ro-val').textContent).toContain('10');
    expect(v.textContent).toContain('set by your admin');
  });

  it('never redraws the status dropdown while it is open, and catches up when it closes', async () => {
    const v = await open();
    const select = v.querySelector('#wk-status-parsing');
    select.focus();
    expect(document.activeElement).toBe(select);
    routes.summary = summary({ active: 9 });
    await ui.wkPoll();
    expect(v.querySelector('#wk-status-parsing')).toBe(select);  // the same element: its open list stays open
    select.value = 'failed';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    await flush();
    expect(fetchMock.mock.calls.map(([u]) => u).some(u => u.startsWith('/api/workers/parsing?') && u.includes('status=failed'))).toBe(true);
    expect(view().querySelector('#wk-status-parsing').value).toBe('failed');
    view().querySelector('#wk-status-parsing').focus();
    await ui.wkPoll();
    const held = view().querySelector('#wk-status-parsing');
    held.blur();
    await flush();
    expect(view().querySelector('#wk-status-parsing')).not.toBe(held);  // the held refresh ran after the dropdown closed
  });

  it('shows a failed task that was already run again without a second Run now', async () => {
    routes.parsing = { ...parsing(), rows: [row({ task_id: 13, status: 'FAILED', can_kill: false, can_run_now: false, last_error: '502', finished_at: '2026-10-04T16:04:00', retried_by: { task_id: 20, status: 'QUEUED' } }),
      row({ task_id: 14, job_id: 8, status: 'FAILED', can_kill: false, can_run_now: true, last_error: 'boom' })] };
    const v = await open();
    const [retried, fresh] = v.querySelectorAll('.wk-row');
    expect(retried.querySelector('[data-act="wk-run-now"]')).toBeNull();
    expect(retried.textContent).toContain('↻ Run again');
    expect(retried.textContent).toContain('run again as task #20 (queued)');
    expect(fresh.querySelector('[data-act="wk-run-now"]')).not.toBeNull();
  });

  it('selects failed tasks and retries them in bulk', async () => {
    const failed = (id, extra = {}) => row({ task_id: id, job_id: id, title: `Job ${id}`, status: 'FAILED', can_kill: false, can_run_now: true, last_error: 'ollama.com:443', ...extra });
    routes.parsing = { ...parsing(), rows: [row(), failed(21), failed(22), failed(23), failed(24, { retried_by: { task_id: 40, status: 'QUEUED' }, can_run_now: false })] };
    const v = await open();
    const boxes = () => [...view().querySelectorAll('[data-wk-select="parsing"]')];
    expect(boxes().map(b => b.dataset.id)).toEqual(['21', '22', '23']);  // never on a running or already-retried row
    expect(view().querySelector('.wk-bulk').textContent).toContain('Select all 3 failed tasks shown');
    expect(view().querySelector('[data-act="wk-retry-selected"]').disabled).toBe(true);
    const tick = el => { el.checked = !el.checked; el.dispatchEvent(new Event('change', { bubbles: true })); };
    tick(boxes()[1]);
    expect(view().querySelector('.wk-bulk').textContent).toContain('1 selected of 3 failed shown');
    expect(view().querySelector('.wk-bulk .wk-check').classList.contains('partial')).toBe(true);
    tick(view().querySelector('[data-wk-select-all="parsing"]'));
    expect(boxes().every(b => b.checked)).toBe(true);
    expect(view().querySelector('[data-act="wk-retry-selected"]').textContent).toContain('Retry 3');
    // A poll that no longer lists one of them drops it from the selection.
    routes.parsing = { ...routes.parsing, rows: routes.parsing.rows.filter(r => r.task_id !== 23) };
    await ui.loadWorkers(); ui.render();
    expect(view().querySelector('[data-act="wk-retry-selected"]').textContent).toContain('Retry 2');
    await ui.A['wk-retry-selected']({ dataset: { tab: 'parsing' } });
    expect(confirmMock.mock.calls.at(-1)[0]).toContain('Retry 2 failed tasks?');
    expect(routes.retryPost).toEqual({ url: '/api/workers/parsing/retry', body: { task_ids: [21, 22] } });
    expect(document.getElementById('toast').textContent).toContain('Queued 1 task again · 1 skipped: already run again');
    expect(ui.wkState().sel.parsing.size).toBe(0);
  });

  it('offers the same selection on the tailoring and ranking tabs', async () => {
    fetchMock.mockImplementation(async (url, opt = {}) => response(
      url === '/api/workers' ? routes.summary : url.endsWith('/activity') ? routes.activity
      : url.startsWith('/api/tailor-tasks') ? [{ id: 41, job_id: 5, title: 'Backend Lead', company: 'Stripe', mode: 'tailor', status: 'failed', stage: 'done', progress: 0, error: 'x' },
        { id: 42, job_id: 6, title: 'Staff', company: 'Grab', mode: 'tailor', status: 'failed', stage: 'done', progress: 0, error: 'x' }, { id: 43, job_id: 6, title: 'Staff', company: 'Grab', mode: 'tailor', status: 'queued', stage: 'queued', progress: 0 }]
      : url.startsWith('/api/ranking/tracker') ? { counts: { filtering: 0, queued: 0, processing: 0, retry_scheduled: 0, waiting: 0, completed: 0, failed: 1, skipped: 0, qualified: 0, archived: 0 },
        progress: { finished: 1, total: 1 }, concurrency: 2, interval_minutes: 5, active: [], recent: [{ id: 51, job_id: 9, title: 'AI Engineer', company: 'Grab', status: 'FAILED', attempt_count: 3, max_attempts: 3, reason: 'new' }],
        locations: [], worker: { enabled: true, interval_minutes: 2 }, auto_rank: true } : {}));
    let v = await open('tailoring');
    expect([...v.querySelectorAll('[data-wk-select="tailoring"]')].map(b => b.dataset.id)).toEqual(['41']);  // 42 was retried as 43
    v = await open('ranking');
    expect([...v.querySelectorAll('[data-wk-select="ranking"]')].map(b => b.dataset.id)).toEqual(['51']);
    expect(v.querySelector('.wk-bulk')).not.toBeNull();
  });

  it('explains a waiting or retrying task in full behind an ⓘ', async () => {
    const long = 'LLMRateLimitError: LiteLLM request failed for model=Auto-Router: litellm.RateLimitError: Ollama_chatException - timed out waiting for a concurrent request';
    routes.parsing = { ...parsing(), rows: [row({ task_id: 31, status: 'RETRYING', attempt: 2, max_attempts: 3, last_error: long, error_category: 'RATE_LIMITED', next_attempt_at: '2099-01-01T00:00:00', reason: 'description_changed', can_run_now: true }),
      row({ task_id: 32, status: 'QUEUED', attempt: 0, last_error: null, can_kill: false, can_run_now: true })] };
    const v = await open();
    const [retrying, queued] = v.querySelectorAll('.wk-row');
    const tip = retrying.querySelector('.wk-info').dataset.tip;
    expect(tip.split('\n')).toEqual(['Why it is retrying', long, 'Kind: Rate limited', 'Attempt 2 of 3 used',
      expect.stringMatching(/^Next try /), 'Queued because: Description changed']);
    expect(retrying.querySelector('.wk-info').getAttribute('aria-label')).toBe(tip);
    expect(queued.querySelector('.wk-info')).toBeNull();  // nothing to explain
    // The ranking tracker's "Waiting to retry" line gets the same ⓘ.
    expect(context.wkRankWhy({ status: 'PENDING', attempt_count: 0, last_error: 'The job description is still being parsed', next_attempt_at: '2099-01-01T00:00:00', reason: 'resume_changed', max_attempts: 3 }))
      .toContain('Why it is waiting\nThe job description is still being parsed');
  });
});

