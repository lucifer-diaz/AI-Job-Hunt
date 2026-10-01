import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The tracker filters its applications in the browser, by search text, location (grouped by city) and fit.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui;

const app = (id, title, company, location, fit, stage = 'applied') => ({
  id, job_id: id, title, company, location, fit, stage, age: '4d ago', tag: '', tone: 'muted', meta: 'applied 22 Sep',
  url: '', recruiters: [], docs: 'not recorded', draft: '', next: 'Send the follow-up', next_date: '', timeline: [] });
const tracker = () => ({
  stages: [{ id: 'applied', label: 'Applied', dot: '#5b4ae8' }, { id: 'interviewing', label: 'Interviewing', dot: '#0f9c86' }],
  available_stages: [], extra_stages: [], stats: { live: 4, replies: 1, due: 0, response_rate: '25%' },
  applications: [
    app(1, 'Lead AI/ML Engineer', 'Optum', 'Bengaluru, Karnataka, India', 87),
    app(2, 'Senior AI platform Engineer', 'Sparix Global', 'Bengaluru, India', 95),
    app(3, 'Staff Software Engineer', 'Agoda', 'Gurugram, India', 82, 'interviewing'),
    app(4, 'AI Forward Deployed Engineer', 'Positron AI', 'Remote - India', 30),
  ] });
const view = () => { document.getElementById('view').innerHTML = ui.renderTracker(); return document.getElementById('view'); };
const cards = root => [...root.querySelectorAll('.kcard')].map(c => Number(c.dataset.id));

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script><div id="view"></div><div id="toast"></div>';
  if (!ui) {
    const context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl,
      setTimeout, clearTimeout, setInterval, clearInterval, console, fetch: vi.fn() });
    for (const file of ['monitoring.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, D, renderTracker })', context);
    ui.ctx = context;
  }
  ui.D.tracker = tracker(); ui.S.selApp = null; ui.S.trackerView = 'board'; ui.S.busy = {};
  ui.S.tf = { q: '', location: 'all', min_fit: 0 };
});

describe('Tracker · filters', () => {
  it('offers each city once, with how many applications it has', () => {
    const options = [...view().querySelectorAll('#tf-location option')].map(o => o.textContent);
    expect(options).toEqual(['All locations', 'Bengaluru (2)', 'Gurugram (1)', 'Remote (1)']);
  });

  it('shows only the chosen location on the board, and says how many are hidden', () => {
    ui.S.tf.location = 'Bengaluru';
    const root = view();
    expect(cards(root)).toEqual([1, 2]);
    expect(root.textContent).toContain('Showing 2 of 4');
  });

  it('combines location, fit and search', () => {
    ui.S.tf = { q: 'sparix', location: 'Bengaluru', min_fit: 80 };
    expect(cards(view())).toEqual([2]);
  });

  it('says so when nothing matches instead of showing an empty board', () => {
    ui.S.tf = { q: 'nothing like this', location: 'all', min_fit: 0 };
    const root = view();
    expect(cards(root)).toEqual([]);
    expect(root.textContent).toContain('No applications match');
  });
});

describe('Location option lists', () => {
  it('turn server facets into options with counts', () => {
    const opts = runInContext('locationOptions([["Bengaluru", 3], ["Remote", 1]])', ui.ctx);
    expect(opts).toEqual([['all', 'All locations'], ['Bengaluru', 'Bengaluru (3)'], ['Remote', 'Remote (1)']]);
  });
});

describe('Posting dates', () => {
  const posted = j => runInContext(`postedTxt(${JSON.stringify(j)})`, ui.ctx);

  it('show the employer date as a calendar date, with how long ago', () => {
    const year = new Date().getUTCFullYear();
    expect(posted({ posted_at: `${year}-09-14T00:00:00`, posted_ago: '12d ago' })).toMatch(/^posted (14 Sep|Sep 14) \(12d ago\)$/);
  });

  it('add the year for an older posting, and fall back to what the board wrote', () => {
    expect(posted({ posted_at: '2024-01-05T00:00:00', posted_ago: '600d ago' })).toContain('2024');
    expect(posted({ posted_at: null, posted_raw: '30+ days ago' })).toBe('posted 30+ days ago');
    expect(posted({ posted_at: null, posted_raw: null })).toBe('');
  });
});

describe('Description state badges', () => {
  const badge = (state, always) => runInContext(`descBadge(${JSON.stringify(state)}, ${!!always})`, ui.ctx);

  it('stay quiet for a parsed posting in lists, and name every other state', () => {
    expect(badge('PARSED')).toBe('');
    expect(badge('PARSED', true)).toContain('Parsed');
    expect(badge('FAILED')).toContain('Parse failed');
    expect(badge('PARSING')).toContain('Parsing');
    expect(badge('NO_DESCRIPTION')).toContain('No description');
  });
});

describe('Overview · AI gateway switch', () => {
  const gateway = over => ({ gateway_enabled: false, direct_provider: 'openrouter', direct_label: 'OpenRouter',
    direct_model: 'openrouter/free', direct_ready: true, litellm_ready: true, litellm_base_url: 'http://localhost:4000', ...over });
  const render = g => { ui.D.gateway = g; const div = document.createElement('div'); div.innerHTML = runInContext('renderGatewaySwitch()', ui.ctx); return div; };
  const buttons = root => [...root.querySelectorAll('.seg button')];

  it('offers LiteLLM and the chosen direct provider, and marks the one in use', () => {
    const root = render(gateway());
    expect(buttons(root).map(b => b.textContent.trim())).toEqual(['LiteLLM gateway', 'OpenRouter (direct)']);
    expect(buttons(root).map(b => b.getAttribute('aria-pressed'))).toEqual(['false', 'true']);
    expect(root.textContent).toContain('calls OpenRouter directly, default model openrouter/free');
    expect(buttons(root)[0].dataset.act).toBe('set-gateway');
    expect(buttons(root)[0].dataset.on).toBe('1');
  });

  it('will not switch to a route that is not set up, and says what is missing', () => {
    const root = render(gateway({ gateway_enabled: true, direct_ready: false }));
    expect(buttons(root)[1].disabled).toBe(true);
    expect(root.textContent).toContain('OpenRouter needs an API key first');
    expect(render(gateway({ litellm_ready: false })).querySelector('.seg button').disabled).toBe(true);
  });
});

describe('Monitoring · prompt capture and links to logs', () => {
  const html = code => { const div = document.createElement('div'); div.innerHTML = runInContext(code, ui.ctx); return div; };

  it('shows the full input and output with a copy button when they were captured', () => {
    ui.D.monCapture = { enabled: true, server_wide: false, retention_days: 7 };
    const x = { status: 'FAILED', debug: { prompt: 'SYSTEM\n\nUSER: parse this', response: '{"requirements": [{"skill": "GCP"}]}' } };
    const root = html(`monPayloads(${JSON.stringify(x)})`);
    const blocks = [...root.querySelectorAll('details')];
    expect(blocks.map(d => d.querySelector('summary').textContent)).toEqual([
      expect.stringContaining('Input · the full prompt sent'), expect.stringContaining('Output · the model’s response')]);
    expect(blocks.every(d => d.open)).toBe(true);  // a failed request opens both
    expect(root.querySelectorAll('[data-act="mon-copy"]')).toHaveLength(2);
    expect(blocks[1].querySelector('pre').textContent).toContain('"skill": "GCP"');
  });

  it('offers to start capturing when a request has no text', () => {
    ui.D.monCapture = { enabled: false, server_wide: false, retention_days: 7 };
    const root = html('monPayloads({ status: "SUCCEEDED", debug: null })');
    expect(root.textContent).toContain('were not captured');
    expect(root.querySelector('[data-act="mon-capture"]').dataset.on).toBe('1');
    expect(html('monCaptureSwitch()').querySelector('input').checked).toBe(false);
  });

  it('opens the Logs tab filtered to the request’s trace', () => {
    const m = runInContext('monState()', ui.ctx);
    runInContext('globalThis.__load = load; load = () => {}', ui.ctx);
    try { runInContext(`MONITORING_ACTIONS['mon-logs-trace']({ dataset: { id: 'abc123' } })`, ui.ctx); }
    finally { runInContext('load = globalThis.__load', ui.ctx); }
    expect(m.tab).toBe('logs');
    expect(m.logs.trace_id).toBe('abc123');
    expect(runInContext('monLogQuery()', ui.ctx)).toContain('trace_id=abc123');
  });
});

describe('Monitoring · an LLM log line', () => {
  it('shows the prompt and response of the call it describes', () => {
    const m = runInContext('monState()', ui.ctx);
    m.logs.entry = 5;
    ui.D.monCapture = { enabled: true, server_wide: false, retention_days: 7 };
    ui.D.monLog = { id: 5, message: 'Ranker call succeeded · gpt-oss:20b · 1,200 ms', level: 'INFO', category: 'LLM',
      created_at: '2026-09-27T10:30:00', details: {}, related: [],
      llm_requests: [{ id: 9, stage: 'RANKER', model: 'gpt-oss:20b', status: 'SUCCEEDED', captured: true,
        payload: { prompt: 'SYSTEM PROMPT\n\nJOB: Lead engineer', response: '{"overall_fit_score": 82}' } }] };
    const div = document.createElement('div');
    div.innerHTML = runInContext('renderMonLogModal()', ui.ctx);
    const pres = [...div.querySelectorAll('pre.mon-debug')].map(p => p.textContent);
    expect(pres).toEqual(['SYSTEM PROMPT\n\nJOB: Lead engineer', '{"overall_fit_score": 82}']);
    expect(div.querySelector('[data-act="mon-log-request"]').dataset.id).toBe('9');
    m.logs.entry = null;
  });
});
