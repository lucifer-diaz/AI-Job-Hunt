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
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, D, renderTracker })', context);
    ui.ctx = context;
  }
  ui.D.tracker = tracker(); ui.S.selApp = null; ui.S.trackerView = 'board'; ui.S.busy = {};
  ui.S.tf = { q: '', location: 'all', min_fit: 0 }; ui.S.trackerArchive = null; ui.S.showArchived = false; runInContext('openStages.clear()', ui.ctx);
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

  it('never keeps a card the filters hide in the detail panel', () => {
    ui.S.selApp = 4; ui.S.tf = { q: '', location: 'Bengaluru', min_fit: 0 };
    const root = view();
    expect(cards(root)).toEqual([1, 2]);
    expect(root.querySelector('.tr-detail .card-title').textContent).toBe('Lead AI/ML Engineer');
    expect(root.querySelector('[data-act="move-stage"]').dataset.app).toBe('1');
  });

  it('never shows a hidden archived card in the detail panel', () => {
    const t = tracker(); t.applications[0] = { ...t.applications[0], archived: true };
    ui.D.tracker = t; ui.S.selApp = 1;
    const root = view();
    expect(cards(root)).not.toContain(1);
    expect(root.querySelector('.tr-detail .card-title').textContent).toBe('Senior AI platform Engineer');
  });

  it('reveals an application opened from elsewhere: archived cards shown, hiding filters cleared', () => {
    const t = tracker(); t.applications[3] = { ...t.applications[3], archived: true };
    ui.D.tracker = t; ui.S.selApp = 4; ui.S.tf = { q: 'optum', location: 'all', min_fit: 0 };
    runInContext('S.trackerReveal = 4; revealTrackerApp()', ui.ctx);
    expect(ui.S.showArchived).toBe(true);
    expect(ui.S.tf).toEqual({ q: '', location: 'all', min_fit: 0 });
    const root = view();
    expect(cards(root)).toContain(4);
    expect(root.querySelector('.tr-detail .card-title').textContent).toBe('AI Forward Deployed Engineer');
    expect(ui.S.trackerReveal).toBeNull();
  });

  it('leaves the filters alone when the opened application already matches', () => {
    ui.S.tf = { q: 'optum', location: 'all', min_fit: 0 };
    runInContext('S.trackerReveal = 1; revealTrackerApp()', ui.ctx);
    expect(ui.S.tf.q).toBe('optum');
    expect(ui.S.showArchived).toBe(false);
  });

  it('says so when nothing matches instead of showing an empty board', () => {
    ui.S.tf = { q: 'nothing like this', location: 'all', min_fit: 0 };
    const root = view();
    expect(cards(root)).toEqual([]);
    expect(root.textContent).toContain('No applications match');
  });
});

describe('Tracker · archive old applications', () => {
  const withClosed = () => {
    const t = tracker();
    t.stages.push({ id: 'rejected', label: 'Rejected', dot: '#c22f4e' }, { id: 'closed', label: 'Closed', dot: '#c6cad8' });
    t.applications.push(app(5, 'Old role', 'Initech', 'Remote', 60, 'closed'), app(6, 'Older role', 'Hooli', 'Remote', 50, 'closed'),
      app(7, 'Turned down', 'Umbrella', 'Remote', 70, 'rejected'));
    return t;
  };
  const openStagesOpen = () => { runInContext("openStages.add('closed')", ui.ctx); };
  const folded = root => [...root.querySelectorAll('.kcol.folded')].map(c => [c.dataset.id, c.querySelector('.mono').textContent, c.querySelector('.kfold-label').textContent]);

  it('folds Rejected and Closed to a strip with their counts, and never selects a folded card by default', () => {
    ui.D.tracker = withClosed();
    const root = view();
    expect(cards(root)).toEqual([1, 2, 4, 3]);
    expect(folded(root)).toEqual([['rejected', '1', 'Rejected'], ['closed', '2', 'Closed']]);
    expect(root.querySelector('.kcard.sel').dataset.id).toBe('1');
    expect(root.querySelector('.kcol.folded[data-id="closed"]').dataset.dropStage).toBe('closed');  // still a drop target
  });

  it('opens a folded column on a click, remembers it, and folds it again', () => {
    ui.D.tracker = withClosed();
    const toggle = id => runInContext(`toggleStage(${JSON.stringify(id)})`, ui.ctx);  // what the tracker-fold action does
    expect(view().querySelector('.kcol.folded[data-id="closed"]').dataset.act).toBe('tracker-fold');
    toggle('closed');
    let root = view();
    expect(cards(root)).toEqual([1, 2, 4, 3, 5, 6]);
    expect(folded(root).map(([id]) => id)).toEqual(['rejected']);
    expect(runInContext('[...openStages]', ui.ctx)).toEqual(['closed']);  // saved to localStorage where it is allowed
    expect(root.querySelector('button[data-act="tracker-fold"][data-id="closed"]')).not.toBeNull();
    toggle('closed');
    root = view();
    expect(folded(root).map(([id]) => id)).toEqual(['rejected', 'closed']);
  });

  it('still shows the details of a folded card opened from elsewhere', () => {
    ui.D.tracker = withClosed(); ui.S.selApp = 5;
    const root = view();
    expect(cards(root)).not.toContain(5);
    expect(root.querySelector('.tr-detail .card-title').textContent).toBe('Old role');
    expect(root.querySelector('.tr-detail').textContent).toContain('Initech');
  });

  it('offers the periods, and names how many move and from where before anything moves', () => {
    ui.S.trackerArchive = { days: 45, scope: 'no_reply', loading: false,
      preview: { count: 3, by_stage: [{ stage: 'applied', label: 'Applied', count: 2 }, { stage: 'acknowledged', label: 'Acknowledged', count: 1 }] } };
    const root = view();
    const presets = [...root.querySelectorAll('[data-act="tracker-archive-days"]')];
    expect(presets.map(b => b.textContent)).toEqual(['15 days', '30 days', '45 days', '60 days', '90 days', 'Any age']);
    expect(presets[2].classList.contains('primary')).toBe(true);
    expect(root.querySelector('#archive-days').value).toBe('45');
    expect(root.querySelector('[data-act="tracker-archive-days"][data-days="0"]').textContent).toBe('Any age');
    expect([...root.querySelectorAll('#archive-scope option')].map(o => o.value)).toEqual(['no_reply', 'live', 'finished', 'all']);
    const run = root.querySelector('[data-act="tracker-archive-run"]');
    expect(run.textContent).toBe('Archive 3 applications');
    expect(run.disabled).toBe(false);
    expect([...root.querySelectorAll('.tr-tally')].map(x => x.textContent)).toEqual(['2 Applied', '1 Acknowledged']);
  });

  it('hides archived cards until asked, and offers Unarchive on one', () => {
    const t = withClosed();
    t.applications[0] = { ...t.applications[0], archived: true, stage: 'closed' };
    ui.D.tracker = t; openStagesOpen();
    let root = view();
    expect(cards(root)).not.toContain(1);
    const toggle = root.querySelector('[data-act="tracker-show-archived"]');
    expect(toggle.textContent).toBe('Show archived (1)');
    ui.S.showArchived = true; ui.S.selApp = 1;
    root = view();
    expect(cards(root)).toContain(1);
    expect(root.querySelector('[data-act="tracker-show-archived"]').textContent).toBe('Hide archived');
    expect(root.querySelector('.kcard[data-id="1"]').textContent).toContain('archived');
    expect(root.querySelector('[data-act="tracker-unarchive"]').dataset.app).toBe('1');
  });

  it('says so when everything is archived instead of showing an empty board', () => {
    const t = tracker();
    t.applications = t.applications.map(a => ({ ...a, archived: true }));
    ui.D.tracker = t;
    const root = view();
    expect(root.textContent).toContain('Every application is archived');
    expect(root.querySelector('[data-act="tracker-show-archived"]').textContent).toBe('Show archived (4)');
  });

  it('will not archive while counting or when nothing is that old', () => {
    ui.S.trackerArchive = { days: 90, scope: 'live', loading: false, preview: { count: 0, by_stage: [] } };
    let run = view().querySelector('[data-act="tracker-archive-run"]');
    expect(run.textContent).toBe('Nothing to archive');
    expect(run.disabled).toBe(true);
    ui.S.trackerArchive = { days: 90, scope: 'live', loading: true, preview: null };
    run = view().querySelector('[data-act="tracker-archive-run"]');
    expect(run.textContent).toBe('Counting…');
    expect(run.disabled).toBe(true);
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
