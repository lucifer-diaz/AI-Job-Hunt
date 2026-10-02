import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Job description recovery in the UI: the job modal says where recovery stands and offers Recover / Retry, and
// Monitoring → JD recovery shows its metrics and what it can use. Never a stack trace.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'render', 'A', 'recoveryStrip', 'monState', 'MON_TABS', 'renderMonJdRecovery', 'renderMonJdParsing', 'MON_STAGES'];
let ui, context, fetchMock;

const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const recovery = (extra = {}) => ({ state: 'NOT_STARTED', label: 'Job description unavailable', usable: false,
  quality: 'PLACEHOLDER', has_url: true, can_recover: true, description_source: 'SOURCE', fetch_method: null,
  fetched_at: null, has_original: false, task: null, ...extra });
const job = r => ({ id: 7, title: 'Platform Engineer', company: 'Northwind', description_recovery: r });
const strip = r => {
  const div = document.createElement('div');
  div.innerHTML = ui.recoveryStrip(job(r));
  return div;
};

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  fetchMock = vi.fn(async url => response(url.includes('/recover-description')
    ? recovery({ state: 'PENDING', label: 'Queued', can_recover: false, queued: true })
    : url.includes('/workspace/summary') ? ui.D.summary : {}));
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock;
  ui.S.forms = {}; ui.S.busy = {};
});

describe('job modal recovery strip', () => {
  it('offers recovery when the description is unavailable', () => {
    const div = strip(recovery());
    expect(div.textContent).toContain('Job description unavailable.');
    const button = div.querySelector('[data-act="recover-description"]');
    expect(button.textContent).toBe('Recover Description');
    expect(button.dataset.id).toBe('7');
  });

  it('shows queued and fetching work without a button', () => {
    for (const [state, label] of [['PENDING', 'Queued'], ['PROCESSING', 'Fetching']]) {
      const div = strip(recovery({ state, label, can_recover: false }));
      expect(div.textContent).toContain(label);
      expect(div.textContent).toContain('Reading the posting in the background');
      expect(div.querySelector('[data-act="recover-description"]')).toBeNull();
    }
  });

  it('says why a recovery was blocked or failed and offers a retry', () => {
    const blocked = strip(recovery({ state: 'BLOCKED', label: 'Blocked - login required',
      task: { message: 'Login required' } }));
    expect(blocked.textContent).toContain('Blocked - login required');
    expect(blocked.textContent).toContain('open the posting and paste the description');
    expect(blocked.querySelector('[data-act="recover-description"]').textContent).toBe('Retry');
    const failed = strip(recovery({ state: 'SKIPPED', label: 'Failed - page no longer exists',
      task: { message: 'Page no longer exists' } }));
    expect(failed.textContent).toContain('Failed - page no longer exists');
    expect(failed.textContent).not.toMatch(/Traceback|Error:/);
  });

  it('shows where a recovered description came from', () => {
    const div = strip(recovery({ state: 'COMPLETED', label: 'Recovered', usable: true, description_source: 'RECOVERED',
      fetch_method: 'PLAYWRIGHT', fetched_at: '2026-09-27T10:00:00', has_original: true }));
    expect(div.textContent).toContain('Recovered from the posting page (browser)');
    expect(div.textContent).toContain("the board's original text is kept");
    expect(div.querySelector('[data-act="recover-description"]')).toBeNull();
  });

  it('stays out of the way for an ordinary posting', () => {
    expect(ui.recoveryStrip(job(recovery({ state: 'NOT_NEEDED', label: '', usable: true })))).toBe('');
    expect(ui.recoveryStrip({ id: 1 })).toBe('');
  });

  it('explains a job with no posting URL', () => {
    const div = strip(recovery({ has_url: false, can_recover: false }));
    expect(div.textContent).toContain('no posting URL');
    expect(div.querySelector('[data-act="recover-description"]')).toBeNull();
  });

  it('queues recovery through the API without fetching anything itself', async () => {
    ui.D.jobModal = { ...job(recovery()), description: '' };
    await ui.A['recover-description']({ dataset: { id: '7' } });
    const call = fetchMock.mock.calls.find(([url]) => url.includes('/recover-description'));
    expect(call[0]).toBe('/api/jobs/7/recover-description');
    expect(call[1].method).toBe('POST');
    expect(ui.D.jobModal.description_recovery.state).toBe('PENDING');
  });
});

describe('Monitoring → JD recovery', () => {
  it('is a monitoring section with metrics and diagnostics', () => {
    expect(ui.MON_TABS.map(([id]) => id)).toContain('jd_recovery');
    ui.D.monJdr = {
      metrics: { missing_descriptions: 12, tasks_created: 20, active: 2, finished: 18, recovered: 14, success_rate: 0.78,
        http_only_success_rate: 0.6, playwright_fallback_rate: 0.3, playwright_success_rate: 0.5, llm_fallback_rate: 0.1,
        llm_success_rate: 1, average_latency_ms: 2400, blocked: 3, failed: 1, jobs_removed: 2,
        average_recovered_length: 3200, subsequently_parsed: 13, subsequently_ranked: 9,
        by_result: { RECOVERED: 14, LOGIN_REQUIRED: 2, CAPTCHA_OR_ANTI_BOT: 1, JOB_REMOVED: 1 } },
      diagnostics: { enabled: true, http: { status: 'AVAILABLE' },
        playwright: { status: 'UNAVAILABLE', reason: 'Chromium executable missing: run `python -m playwright install chromium`', browser: 'chromium', headless: true },
        llm: { status: 'AVAILABLE', reason: null },
        settings: { batch_size: 10, max_concurrency: 2, max_attempts: 3, min_characters: 500, interval_minutes: 2 } } };
    const div = document.createElement('div');
    div.innerHTML = ui.renderMonJdRecovery();
    const text = div.textContent;
    expect(text).toContain('78.0%');
    expect(text).toContain('Browser fallback');
    expect(text).toContain('Chromium executable missing');
    expect(text).toContain('HTTP extraction');
    expect(text).toContain('LOGIN_REQUIRED');
    expect(text).toContain('13 · 9');
  });
});

describe('Monitoring → JD parsing', () => {
  it('shows parse outcomes, the queue, the scheduler job and the parser model calls', () => {
    expect(ui.MON_TABS.map(([id]) => id)).toContain('jd_parsing');
    expect(ui.MON_STAGES.JD_PARSER).toBe('JD parser');
    ui.D.monJdp = { metrics: {
      tasks_created: 40, completed: 30, failed: 8, retrying: 2, success_rate: 0.789, average_duration_ms: 4200, jobs_parsed: 30,
      failures_by_category: { RATE_LIMITED: 7, LEASE_EXPIRED: 1 },
      top_errors: [{ error: 'LLMRateLimitError: OpenRouter rate limit exceeded', tasks: 7 }],
      queue: { pending: 3, processing: 1, oldest_pending_at: '2026-10-01T10:00:00' },
      llm: { requests: 41, failed: 9, input_tokens: 120000, output_tokens: 30000, estimated_cost: null,
        average_latency_ms: 3800, models: { 'openrouter/free': 41 }, errors: { RATE_LIMIT: 9 } },
      scheduler: { enabled: true, last_status: 'failed', last_finished_at: '2026-10-01T12:00:00', next_run_at: null,
        last_error: 'ProgrammingError: column jobs.auto_tailor_status does not exist', last_result: null } } };
    const div = document.createElement('div');
    div.innerHTML = ui.renderMonJdParsing();
    const text = div.textContent;
    for (const bit of ['Parse tasks', '78.9%', '3 · 1', 'Model calls', '120,000 · 30,000', 'not reported',
      'column jobs.auto_tailor_status does not exist', 'openrouter/free (41)', 'LLMRateLimitError']) {
      expect(text).toContain(bit);
    }
  });
});
