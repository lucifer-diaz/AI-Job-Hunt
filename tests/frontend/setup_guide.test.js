import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The production scripts, minus app.js's automatic network boot.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'GUIDE', 'render', 'guideBoot', 'guideRefresh', 'renderNav'];
let ui, context, fetchMock, guide;

const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const QUESTS = [
  ['profile', 'Who you are', 50, 'settings', 'profile'], ['preferences', 'Who you are', 50, 'settings', 'preferences'],
  ['resume', 'Your story', 100, 'settings', 'resumes'], ['ai', 'Power up', 100, 'settings', 'ai'],
  ['search', 'The hunt', 100, 'discovery', 'setup'],
  ['answers', 'Ready to apply', 75, 'knowledge', 'answers'], ['gmail', 'Bonus', 50, 'settings', 'gmail', true],
];
function progress({ done = [], celebrated = done, welcomed = true, dismissed = false, level = 1 } = {}) {
  const quests = QUESTS.map(([id, chapter, xp, screen, section, optional = false]) => ({
    id, chapter, title: `Quest ${id}`, summary: `Do ${id}`, why: `Why ${id}`, steps: [`Step one of ${id}`, 'Step two'],
    xp, screen, section, optional, recommended: id === 'ai' ? 'Ollama account + LiteLLM · nemotron-3-nano:30b-cloud' : '',
    snippet: id === 'ai' ? 'litellm --config integrations/litellm/config.ollama-cloud.yaml' : '',
    links: id === 'ai' ? [{ label: 'Ollama API keys', url: 'https://ollama.com/settings/keys' }] : [], done: done.includes(id), detail: done.includes(id) ? `${id} done` : `${id} to do` }));
  const required = quests.filter(q => !q.optional);
  const xp = quests.filter(q => q.done).reduce((n, q) => n + q.xp, 0);
  return { quests, xp, xp_total: 425, level: { number: level, title: level === 1 ? 'Rookie' : 'Job Seeker', floor: level === 1 ? 0 : 150, next: level === 1 ? 150 : 400, next_title: level === 1 ? 'Job Seeker' : 'Pathfinder' },
    done: required.filter(q => q.done).length, total: required.length, complete: required.every(q => q.done),
    next: (required.find(q => !q.done) || {}).id || null, dismissed, welcomed, celebrated };
}
const flush = async () => { for (let i = 0; i < 4; i++) await new Promise(r => setTimeout(r, 0)); };
const root = () => document.getElementById('guide-root');
const click = sel => { const el = document.querySelector(sel); expect(el, sel).toBeTruthy(); el.click(); };
const puts = () => fetchMock.mock.calls.filter(([u, o]) => u === '/api/setup/guide' && o?.method === 'PUT').map(([, o]) => JSON.parse(o.body));

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"me@example.com"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="guide-root"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  guide = progress();
  fetchMock = vi.fn(async (url, opt = {}) => {
    if (url === '/api/setup/guide') return response(opt.method === 'PUT' ? { ...guide, ...JSON.parse(opt.body) } : guide);
    if (url.startsWith('/api/search/apify/runs')) return response([]);
    if (url.startsWith('/api/search/setups')) return response({ setups: [], board_yield: {} });
    return response({});
  });
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console, requestAnimationFrame: cb => setTimeout(cb, 0) });
    for (const file of ['monitoring.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock;
  clearTimeout(ui.GUIDE.timer);
  Object.assign(ui.GUIDE, { data: null, open: false, view: 'map', sel: null, active: null, booted: false, shownXp: 0, timer: null, loading: false, lastFetch: 0, queue: [] });
  Object.assign(ui.S, { screen: 'overview', busy: {}, forms: {} });
  ui.D.summary = { counts: {}, profile: {} };
});

describe('setup quest', () => {
  it('stays silent until the workspace boots it', async () => {
    await ui.guideRefresh(true);
    expect(fetchMock).not.toHaveBeenCalled();
    expect(ui.renderNav()).toContain('Setup quest');
  });

  it('welcomes a new account once, then shows the quest map', async () => {
    guide = progress({ welcomed: false });
    await ui.guideBoot();
    expect(root().textContent).toContain('Set up your hunt in 6 quests');
    click('[data-act="guide-begin"]'); await flush();
    expect(puts()).toContainEqual({ welcomed: true, dismissed: false });
    const nodes = [...root().querySelectorAll('.gq-node')];
    expect(nodes.map(n => n.dataset.id)).toEqual(['profile', 'preferences', 'resume', 'ai', 'search', 'answers', 'gmail']);
    expect(root().querySelector('.gq-node.next').dataset.id).toBe('profile');
    expect(root().querySelector('.gq-detail').textContent).toContain('Step one of profile');
  });

  it('closing the welcome screen remembers it and leaves the tracker', async () => {
    guide = progress({ welcomed: false });
    await ui.guideBoot();
    click('.gq-x'); await flush();
    expect(puts()).toContainEqual({ welcomed: true });
    expect(root().querySelector('.gq-modal')).toBeNull();
    expect(root().querySelector('.gq-tracker').textContent).toContain('Quest profile');
  });

  it('does not reopen by itself for an account that has seen it', async () => {
    guide = progress({ done: ['profile'] });
    await ui.guideBoot();
    expect(root().querySelector('.gq-modal')).toBeNull();
    expect(document.getElementById('nav').textContent).toContain('1/6');
  });

  it('quests completed before the guide existed are recorded without a celebration', async () => {
    guide = progress({ done: ['profile', 'resume'], celebrated: [] });
    await ui.guideBoot(); await flush();
    expect(puts()).toContainEqual({ celebrated: ['profile', 'resume'] });
    expect(root().querySelector('.gq-celebrate')).toBeNull();
  });

  it('celebrates a quest once when the workspace shows it done, with a level up', async () => {
    await ui.guideBoot();
    guide = progress({ done: ['profile', 'preferences', 'resume'], celebrated: ['profile', 'preferences'], level: 2 });
    await ui.guideRefresh(true);
    const layer = root().querySelector('.gq-celebrate');
    expect(layer.textContent).toContain('QUEST COMPLETE');
    expect(layer.textContent).toContain('Quest resume');
    expect(layer.textContent).toContain('+100 XP');
    expect(layer.textContent).toContain('LEVEL UP');
    expect(puts()).toContainEqual({ celebrated: ['resume'] });
  });

  it('starting a quest goes to where the work is done and tracks it', async () => {
    await ui.guideBoot();
    ui.A['guide-open']({ dataset: {} }); await flush();
    click('.gq-node[data-id="search"]');
    expect(root().querySelector('.gq-detail h3').textContent).toBe('Quest search');
    click('.gq-detail [data-act="guide-go"]'); await flush();
    expect(ui.S.screen).toBe('discovery');
    expect(ui.S.discMode).toBe('setup');
    expect(root().querySelector('.gq-tracker').textContent).toContain('IN PROGRESS');
    expect(root().querySelector('.gq-tracker').textContent).toContain('Quest search');
  });

  it('opens Settings on the quest’s section', async () => {
    await ui.guideBoot();
    ui.A['guide-open']({ dataset: { id: 'gmail' } }); await flush();
    click('.gq-detail [data-act="guide-go"]'); await flush();
    expect([ui.S.screen, ui.S.settingsSection]).toEqual(['settings', 'gmail']);
  });

  it('hiding the tracker is saved, and the sidebar still opens the guide', async () => {
    await ui.guideBoot();
    click('[data-act="guide-hide"]'); await flush();
    expect(puts()).toContainEqual({ dismissed: true });
    expect(root().querySelector('.gq-tracker')).toBeNull();
    click('#nav [data-act="guide-open"]'); await flush();
    expect(root().querySelector('.gq-modal')).toBeTruthy();
  });

  it('opens with ? and closes with Escape', async () => {
    await ui.guideBoot();
    document.body.dispatchEvent(new window.KeyboardEvent('keydown', { key: '?', bubbles: true }));
    expect(root().querySelector('.gq-modal')).toBeTruthy();
    document.body.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(root().querySelector('.gq-modal')).toBeNull();
  });

  it('shows the trophy when every required quest is done, and no tracker', async () => {
    guide = progress({ done: ['profile', 'preferences', 'resume', 'ai', 'search', 'answers'], level: 2 });
    await ui.guideBoot();
    expect(root().querySelector('.gq-tracker')).toBeNull();
    ui.A['guide-open']({ dataset: {} });
    expect(root().textContent).toContain('ALL 6 QUESTS DONE');
    expect(root().textContent).toContain('Level 2 · Job Seeker');
    expect(document.getElementById('nav').textContent).toContain('✓');
  });

  it('recommends Ollama Cloud through LiteLLM, with commands to copy', async () => {
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    await ui.guideBoot();
    ui.A['guide-open']({ dataset: { id: 'ai' } }); await flush();
    const detail = root().querySelector('.gq-detail');
    expect(detail.querySelector('.gq-recommend').textContent).toContain('nemotron-3-nano:30b-cloud');
    expect(detail.querySelector('.gq-snippet pre').textContent).toContain('config.ollama-cloud.yaml');
    const link = detail.querySelector('.gq-links a');
    expect([link.href, link.target, link.rel]).toEqual(['https://ollama.com/settings/keys', '_blank', 'noopener noreferrer']);
    click('.gq-snippet [data-act="guide-copy"]'); await flush();
    expect(writeText).toHaveBeenCalledWith('litellm --config integrations/litellm/config.ollama-cloud.yaml');
    expect(root().querySelector('.gq-node[data-id="search"] .gq-recommend')).toBeNull();
  });

  it('on a phone shows the list or one quest, with a way back', async () => {
    await ui.guideBoot();
    ui.A['guide-open']({ dataset: {} }); await flush();
    const body = () => root().querySelector('.gq-body');
    expect(body().classList.contains('pane-detail')).toBe(false);
    click('.gq-node[data-id="search"]');
    expect(body().classList.contains('pane-detail')).toBe(true);
    click('.gq-detail [data-act="guide-back"]');
    expect(body().classList.contains('pane-detail')).toBe(false);
    expect(root().querySelector('.gq-node.sel').dataset.id).toBe('search');
    // Opening a quest from the tracker goes straight to its details.
    ui.A['guide-close'](); ui.A['guide-open']({ dataset: { id: 'ai' } });
    expect(body().classList.contains('pane-detail')).toBe(true);
  });

  it('a background refresh keeps the scroll position and does not replay the entrance', async () => {
    await ui.guideBoot();
    ui.A['guide-open']({ dataset: {} });
    expect(root().querySelector('.gq-overlay').classList.contains('gq-enter')).toBe(true);
    await flush();
    root().querySelector('.gq-trail').scrollTop = 120;
    await ui.guideRefresh(true);
    expect(root().querySelector('.gq-overlay').classList.contains('gq-enter')).toBe(false);
    expect(root().querySelector('.gq-trail').scrollTop).toBe(120);
    expect(document.documentElement.classList.contains('gq-lock')).toBe(true);
    ui.A['guide-close']();
    expect(document.documentElement.classList.contains('gq-lock')).toBe(false);
  });
});
