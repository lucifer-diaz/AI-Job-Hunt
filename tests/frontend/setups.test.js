import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The production script, minus the automatic network boot at the bottom.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'setF', 'render', 'curSetup', 'setupPayload', 'nextSetupName', 'useSetup', 'adoptSetup'];
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
    for (const file of ['monitoring.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js']) {
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
