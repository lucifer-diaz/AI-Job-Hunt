import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The production script, minus the automatic network boot at the bottom.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'setF', 'render', 'curSetup', 'setupPayload', 'searchPayload', 'nextSetupName', 'useSetup', 'adoptSetup'];
let ui, context, fetchMock;

function response(data, status = 200) { return { ok: status < 400, status, text: async () => JSON.stringify(data) }; }
function setup(id, name, overrides = {}) {
  return { id, name, settings: { roles: [`Role ${id}`], locations: ['Remote'], sites: ['linkedin'], actors: [], custom_actors: [], remote_platforms: [], seniority: [], exclude_keywords: [], linkedin_company_ids: [], skip_agencies: true, auto_rank: true, hours_old: 168, max_results: 20, offset: 0, distance: 50, job_type: '', country_indeed: 'usa', remoteok_url: '' },
    schedule_kind: 'manual', every_hours: 24, time_of_day: '07:00', weekdays: [], timezone: 'UTC', enabled: true,
    schedule_label: 'Manual only', last_run_at: null, next_run_at: null, ...overrides };
}
const setupCard = () => document.querySelector('#view');
const buttonFor = act => [...document.querySelectorAll(`[data-act="${act}"]`)];

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"login@example.com"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  fetchMock = vi.fn(async () => response({}));
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirm;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'discovery'; ui.S.discMode = 'setup'; ui.S.polling = null;
  ui.D.setups = [setup(1, 'Remote AI'), setup(2, 'Mumbai leadership', { schedule_kind: 'daily', schedule_label: 'Daily at 07:00 UTC', next_run_at: '2026-09-21T07:00:00' })];
  ui.S.setupSel = 1;
  ui.D.searchProfile = { profile: ui.D.setups[0].settings, board_yield: {}, apify_configured: true };
  ui.D.runs = []; ui.D.actors = { actors: [], remote_platforms: [], seniority: [] };
  ui.render();
});

describe('saved search setups', () => {
  it('lists every setup and marks the selected one', () => {
    const chips = buttonFor('select-setup');
    expect(chips.map(b => b.textContent.trim())).toEqual(['Remote AI', 'Mumbai leadership']);
    expect(chips[0].className).toContain('soft');
    expect(document.getElementById('sp-name').value).toBe('Remote AI');
  });

  it('switching setups loads that setup into the form and drops the old draft', () => {
    ui.setF('sp', 'name', 'Half-typed name');
    ui.useSetup(2);
    expect(ui.S.forms.sp).toBeUndefined();
    expect(ui.curSetup().name).toBe('Mumbai leadership');
    expect(document.getElementById('sp-name').value).toBe('Mumbai leadership');
    expect(setupCard().textContent).toContain('Next run');
  });

  it('fills the schedule controls from the saved setup, not from the defaults', () => {
    ui.D.setups[0] = setup(1, 'Remote AI', { schedule_kind: 'weekly', weekdays: [0, 2, 4], time_of_day: '08:00',
      timezone: 'Asia/Kolkata', schedule_label: 'Mon, Wed, Fri at 08:00 Asia/Kolkata', next_run_at: '2026-09-21T02:30:00' });
    ui.render();
    expect(document.getElementById('sp-schedule_kind').value).toBe('weekly');
    expect(document.getElementById('sp-time_of_day').value).toBe('08:00');
    expect(document.getElementById('sp-timezone').value).toBe('Asia/Kolkata');
    expect(buttonFor('toggle-day').filter(b => b.textContent.startsWith('✓')).map(b => b.textContent.trim()))
      .toEqual(['✓ Mon', '✓ Wed', '✓ Fri']);
    expect(ui.setupPayload().weekdays).toEqual([0, 2, 4]);

    ui.D.setups[0] = setup(1, 'Remote AI', { schedule_kind: 'interval', every_hours: 6, schedule_label: 'Every 6 hours' });
    ui.render();
    expect(document.getElementById('sp-every_hours').value).toBe('6');
    expect(ui.setupPayload().every_hours).toBe(6);
  });

  it('shows only the schedule controls that the chosen cadence needs', () => {
    expect(document.getElementById('sp-time_of_day')).toBeNull();
    expect(setupCard().textContent).toContain('This setup only runs when you press Run now');

    ui.setF('sp', 'schedule_kind', 'interval'); ui.render();
    expect(document.getElementById('sp-every_hours')).not.toBeNull();
    expect(document.getElementById('sp-time_of_day')).toBeNull();

    ui.setF('sp', 'schedule_kind', 'weekly'); ui.render();
    expect(document.getElementById('sp-time_of_day')).not.toBeNull();
    expect(document.getElementById('sp-timezone')).not.toBeNull();
    expect(buttonFor('toggle-day').map(b => b.textContent.trim())).toEqual(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']);
  });

  it('picks weekdays and sends them, sorted, with the rest of the setup', () => {
    ui.setF('sp', 'schedule_kind', 'weekly'); ui.render();
    ui.A['toggle-day']({ dataset: { day: '4' } });
    ui.A['toggle-day']({ dataset: { day: '0' } });
    const payload = ui.setupPayload();
    expect(payload.weekdays).toEqual([0, 4]);
    expect(payload.schedule_kind).toBe('weekly');
    expect(payload.name).toBe('Remote AI');
    expect(payload.settings.roles).toEqual(['Role 1']);
  });

  it('never proposes a name another setup already has', () => {
    expect(ui.nextSetupName()).toBe('Remote AI 2');
    expect(ui.nextSetupName('New search')).toBe('New search');
  });

  it('saves the selected setup and keeps the returned copy', async () => {
    ui.setF('sp', 'name', 'Remote AI · EU');
    const saved = setup(1, 'Remote AI · EU');
    fetchMock.mockImplementation(async (url, opts) => {
      expect(url).toBe('/api/search/setups/1');
      expect(opts.method).toBe('PUT');
      expect(JSON.parse(opts.body).name).toBe('Remote AI · EU');
      return response({ setup: saved });
    });
    await ui.A['save-search']();
    expect(ui.curSetup().name).toBe('Remote AI · EU');
    expect(ui.S.forms.sp).toBeUndefined();
  });

  it('a saved setup shares its own actors with the other setups, unticking one it removed', async () => {
    const mine = { id: 'me/jobs', name: 'Mine', fan_out: 'role', input_template: {} };
    const gone = { id: 'me/old', name: 'Old', fan_out: 'role', input_template: {} };
    ui.D.setups[1].settings = { ...ui.D.setups[1].settings, custom_actors: [gone], actors: ['me/old', 'openclawai/job-board-scraper'] };
    const saved = setup(1, 'Remote AI');
    saved.settings = { ...saved.settings, custom_actors: [mine], actors: ['me/jobs'] };
    fetchMock.mockImplementation(async () => response({ setup: saved }));
    await ui.A['save-search']();
    const other = ui.D.setups.find(x => x.id === 2).settings;
    expect(other.custom_actors).toEqual([mine]);
    expect(other.actors).toEqual(['openclawai/job-board-scraper']);
  });

  it('runs the selected setup after saving it', async () => {
    const calls = [];
    fetchMock.mockImplementation(async (url, opts) => {
      calls.push(`${opts?.method || 'GET'} ${url}`);
      if (url.endsWith('/run')) return response({ runs: [{ id: 9, actor_id: 'a/b' }], warnings: [], setup: setup(1, 'Remote AI') });
      return response({ setup: setup(1, 'Remote AI') });
    });
    await ui.A['run-search']();
    expect(calls).toEqual(['PUT /api/search/setups/1', 'POST /api/search/setups/1/run']);
    expect(ui.S.polling.ids).toEqual([9]);
  });
});

describe('actor picker', () => {
  const catalog = [
    { id: 'openclawai/job-board-scraper', name: 'Multi Job Board', summary: 'Many boards', cost: '$0.005 per job', fan_out: 'location', group: 'Job boards', supports: ['boards', 'hours', 'job_type', 'remote'], custom: false },
    { id: 'blackfalcondata/wellfound-scraper', name: 'Wellfound', summary: 'Startup jobs', cost: '$0.003 per job', fan_out: 'role', group: 'Startups', supports: ['hours', 'seniority', 'min_salary', 'remote'], custom: false },
    { id: 'parseforge/hn-whoishiring-scraper', name: "HN Who's Hiring", summary: 'HN thread', cost: '$0.02 per job', fan_out: 'role', group: 'Startups', supports: ['remote', 'hn_month', 'min_salary'], custom: false },
  ];
  beforeEach(() => {
    ui.D.actors = { actors: catalog, groups: ['Job boards', 'LinkedIn', 'Startups', 'Your actors'], remote_platforms: [], seniority: ['entry', 'senior'] };
  });

  it('groups actors under labelled fieldsets and counts the selection', () => {
    ui.setF('sp', 'actors', ['blackfalcondata/wellfound-scraper']);
    ui.render();
    const legends = [...document.querySelectorAll('.actor-group legend')].map(l => l.textContent.trim());
    expect(legends).toEqual(['JOB BOARDS', 'STARTUPS · 1 selected']);
    const box = document.getElementById('actor-blackfalcondata-wellfound-scraper');
    expect(box.checked).toBe(true);
    expect(document.getElementById(box.getAttribute('aria-describedby')).textContent).toContain('Startup jobs');
    expect(document.getElementById('actors-heading').textContent).toContain('1 selected');
  });

  it('shows startup settings only for the startup actors picked, and says which actor reads each filter', () => {
    ui.setF('sp', 'actors', ['openclawai/job-board-scraper']);
    ui.render();
    expect(document.querySelector('.actor-settings')).toBeNull();
    expect(document.getElementById('use-seniority').textContent).toBe('Not used by the selected actors');

    ui.setF('sp', 'actors', ['openclawai/job-board-scraper', 'parseforge/hn-whoishiring-scraper']);
    ui.render();
    expect(document.getElementById('sp-hn_month').value).toBe('current');
    expect(document.getElementById('sp-min_salary')).not.toBeNull();
    expect(document.getElementById('sp-yc_batches')).toBeNull();  // only Work at a Startup and YC Jobs read batches
    expect(document.getElementById('use-hours').textContent).toBe('Used by Multi Job Board');
    expect(document.getElementById('use-min_salary').textContent).toBe("Used by HN Who's Hiring");
  });

  it('offers primary skills and sends them with the search', () => {
    ui.render();
    expect(document.getElementById('sp-primary_skills').dataset.max).toBe('3');
    ui.setF('sp', 'primary_skills', ['Python', 'Django']);
    ui.render();
    expect([...document.querySelectorAll('[data-tag-box="primary_skills"] .chip')].map(c => c.firstChild.textContent)).toEqual(['Python', 'Django']);
    expect(ui.searchPayload().primary_skills).toEqual(['Python', 'Django']);
  });

  it('edits a user-added actor in place, keeping its id and position', () => {
    const mine = { id: 'me/jobs', name: 'Mine', fan_out: 'role', input_template: { q: '{{role}}' } };
    const other = { id: 'me/other', name: 'Other', fan_out: 'role_location', input_template: {} };
    ui.setF('sp', 'custom_actors', [mine, other]);
    ui.setF('sp', 'actors', ['me/jobs']);
    ui.D.actors.actors = [...catalog, ...[mine, other].map(c => ({ ...c, summary: 'Added by you', cost: '', custom: true, group: 'Your actors' }))];
    ui.render();
    document.querySelector('[data-act="edit-actor"][data-actor="me/jobs"]').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(document.getElementById('ca-id').readOnly).toBe(true);
    expect(JSON.parse(document.getElementById('ca-input_template').value)).toEqual({ q: '{{role}}' });
    ui.setF('ca', 'name', 'Renamed');
    ui.setF('ca', 'input_template', JSON.stringify({ q: '{{role}}', limit: '{{max_results}}' }));
    ui.A['add-actor']();
    const saved = ui.S.forms.sp.custom_actors;
    expect(saved.map(c => c.id)).toEqual(['me/jobs', 'me/other']);
    expect(saved[0]).toMatchObject({ name: 'Renamed', fan_out: 'role', input_template: { q: '{{role}}', limit: '{{max_results}}' } });
    expect(ui.S.editActor).toBeNull();
  });

  it('inserts a clicked placeholder over the selected value, and fills a schema field with its suggestion', () => {
    ui.D.actors.placeholders = ['role', 'is_remote', 'seniority'];
    ui.S.addActor = true;
    ui.setF('ca', 'input_template', '{\n  "remote": false\n}');
    ui.S.caFields = [{ name: 'seniority', type: 'array', suggested: '{{seniority}}' }, { name: 'note', type: 'string', suggested: '' }];
    ui.render();
    const box = document.getElementById('ca-input_template');
    box.selectionStart = box.value.indexOf('false'); box.selectionEnd = box.selectionStart + 5;
    document.querySelector('[data-act="insert-placeholder"][data-name="is_remote"]').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(JSON.parse(document.getElementById('ca-input_template').value)).toEqual({ remote: '{{is_remote}}' });
    document.querySelector('[data-act="insert-actor-field"][data-name="seniority"]').dispatchEvent(new MouseEvent('click', { bubbles: true }));
    expect(JSON.parse(document.getElementById('ca-input_template').value)).toEqual({ remote: '{{is_remote}}', seniority: '{{seniority}}' });
  });

  it('labels the primary skill placeholders and shows a template that reads them on its card', () => {
    ui.D.actors.placeholders = ['role', 'skill', 'skills'];
    ui.S.addActor = true;
    ui.setF('sp', 'custom_actors', [{ id: 'me/jobs', name: 'Mine', fan_out: 'role', input_template: { q: '{{role}} {{skill}}' } }]);
    ui.render();
    const button = document.querySelector('[data-act="insert-placeholder"][data-name="skill"]');
    expect(button.textContent).toContain('primary skill');
    expect(button.title).toBe('First primary skill');
    expect(document.querySelector('label[for="actor-me-jobs"] .actor-uses').textContent).toContain('Primary skills');
  });

  it('sends the startup settings with the search', () => {
    ui.setF('sp', 'yc_batches', ['W24']);
    ui.setF('sp', 'hn_month', ' 2026-08 ');
    ui.setF('sp', 'min_salary', '150000');
    const body = ui.searchPayload();
    expect([body.yc_batches, body.hn_month, body.min_salary, body.startup_details]).toEqual([['W24'], '2026-08', 150000, true]);
  });
});

describe('import a company board', () => {
  it('imports only the chosen teams and role keywords', async () => {
    const calls = [];
    fetchMock.mockImplementation(async (url, opts) => {
      calls.push([opts?.method || 'GET', String(url), opts?.body]);
      if (opts?.method === 'POST') return response({ seen: 2, created: 2, total: 9 });
      return response(String(url).endsWith('/teams') ? { teams: [{ name: 'Data', count: 3 }] } : []);
    });
    ui.setF('board', 'token', 'acme');
    ui.setF('board', 'roles', ' engineer, scientist ,');
    await ui.A['board-teams']();
    expect(ui.S.boardTeams).toEqual([{ name: 'Data', count: 3 }]);
    ui.A['toggle-board-team']({ dataset: { team: 'Data' } });
    await ui.A['import-board']();
    const post = calls.find(c => c[0] === 'POST');
    expect(post[1]).toBe('/api/import/greenhouse/acme');
    expect(JSON.parse(post[2])).toEqual({ teams: ['Data'], roles: ['engineer', 'scientist'] });
  });
});

describe('review a board before importing', () => {
  it('previews the roles, then imports only the ticked ones', async () => {
    const calls = [];
    const jobs = [{ source_ref: 'a', title: 'Eng', team: 'T', location: '', url: '', in_pipeline: false },
                  { source_ref: 'b', title: 'PM', team: 'T', location: '', url: '', in_pipeline: false },
                  { source_ref: 'c', title: 'Old', team: 'T', location: '', url: '', in_pipeline: true }];
    fetchMock.mockImplementation(async (url, opts) => {
      calls.push([String(url), opts?.body]);
      if (String(url).endsWith('/preview')) return response({ jobs, total: 9 });
      return response(opts?.method === 'POST' ? { seen: 1, created: 1, total: 9 } : []);
    });
    ui.setF('board', 'token', 'acme');
    await ui.A['board-review']();
    expect([...ui.S.boardReview.selected]).toEqual(['a', 'b']);  // already-added roles start unticked
    expect(document.querySelector('[aria-label="Review board roles"]')).not.toBeNull();
    ui.A['toggle-board-job']({ dataset: { ref: 'b' } });
    await ui.A['board-review-import']();
    const post = calls.find(c => !c[0].endsWith('/preview') && c[1]);
    expect(post[0]).toBe('/api/import/greenhouse/acme');
    expect(JSON.parse(post[1]).source_refs).toEqual(['a']);
    expect(ui.S.boardReview).toBeNull();
  });
});

describe('add one manually', () => {
  const click = el => el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  const mode = id => click(buttonFor('manual-mode').find(b => b.dataset.id === id));
  const flush = () => new Promise(r => setTimeout(r, 0));

  it('offers a link, a file or pasted text', () => {
    expect(buttonFor('manual-mode').map(b => b.textContent)).toEqual(['Link', 'PDF file', 'Paste text']);
    expect(document.getElementById('manual-url')).not.toBeNull();
    mode('text');
    expect(document.getElementById('manual-description')).not.toBeNull();
    expect(buttonFor('add-manual-text')).toHaveLength(1);
  });

  it('adds pasted text through the text endpoint', async () => {
    mode('text');
    for (const [k, v] of [['title', 'Lead Engineer'], ['company', 'Acme'], ['description', 'We are hiring']]) ui.setF('manual', k, v);
    fetchMock.mockImplementation(async () => response({ created: true, job: { id: 7 } }));
    click(buttonFor('add-manual-text')[0]);
    await flush();
    const [url, opt] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/jobs/from-text');
    expect(JSON.parse(opt.body)).toMatchObject({ title: 'Lead Engineer', company: 'Acme', description: 'We are hiring', url: '' });
  });

  it('reads an uploaded file into a draft the person checks before adding', async () => {
    mode('file');
    expect(buttonFor('add-manual-text')).toHaveLength(0);
    fetchMock.mockImplementation(async () => response({ title: 'Staff Engineer', company: '', location: '', description: 'Build things' }));
    const input = document.getElementById('manual-jd-file');
    Object.defineProperty(input, 'files', { value: [new File(['%PDF'], 'jd.pdf', { type: 'application/pdf' })] });
    input.dispatchEvent(new Event('change', { bubbles: true }));
    await flush(); await flush();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/jobs/read-file');
    expect(document.getElementById('manual-title').value).toBe('Staff Engineer');
    expect(document.getElementById('manual-description').value).toBe('Build things');
    expect(buttonFor('add-manual-text')).toHaveLength(1);
  });
});

