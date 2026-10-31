import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Discovery quality in the UI: hidden (not matching) jobs can be listed and kept, each job and each search setup can
// choose its resume, and Settings → Domain filter shows the lists and what was learned.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'resumeSelect', 'chooseResume', 'renderDomainSettings', 'searchPayload', 'resumeTags', 'resumeTagBody', 'resumeInUseNote'];
let ui, context, fetchMock;

const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const choices = [{ id: 3, name: 'Backend resume', version: 2, is_active: true }, { id: 5, name: 'ML resume', version: 3, is_active: false }];
const domain = () => ({ enabled: true, learn_from_feedback: true, exclude_terms: ['sales', 'marketing'], skill_excludes: ['java'],
  include_terms: [], server_enabled: true, derived: { roles: ['backend engineer'], skills: ['python'], role_words: ['backend'] },
  overridden: [], defaults: {}, has_hidden_jobs: true,
  learned: [{ term: 'underwriter', kind: 'title', evidence: 4, positive: 0, examples: ['Senior Underwriter'], status: 'active' },
            { term: 'claims', kind: 'title', evidence: 3, positive: 0, examples: [], status: 'disabled', user_disabled: true, forgotten: true }] });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  fetchMock = vi.fn(async (url, opts = {}) => response(
    url.includes('/bulk-resume') || url.match(/\/api\/jobs\/\d+\/resume/) ? { changed: [7], ranking_queued: [], ranking_skipped: [], resume_id: 5 }
      : url.includes('domain-keep') ? { kept: [7], skipped: [] }
      : url.includes('/workspace/summary') ? ui.D.summary : { items: [], total: 0 }));
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
    // These tests drive single actions; reloading and redrawing the whole screen is covered elsewhere.
    runInContext('load = async () => {}; render = () => {};', context);
  }
  context.fetch = fetchMock;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.checked = new Set(); ui.S.jobOpen = null;
  ui.D.resumeChoices = choices;
});

const el = html => { const div = document.createElement('div'); div.innerHTML = html; return div.firstElementChild; };

describe('resume per job', () => {
  it('offers the active resume and every named version', () => {
    const select = el(ui.resumeSelect('job-resume', 5, { id: 7, choice: 'USER' }));
    expect([...select.options].map(o => o.textContent)).toEqual(['Auto — best match by tags', 'Active resume', 'Backend resume (active)', 'ML resume']);
    expect(select.value).toBe('5');
    expect(el(ui.resumeSelect('job-resume', null, { id: 7 })).value).toBe('auto');
    expect(el(ui.resumeSelect('job-resume', null, { id: 7, choice: 'ACTIVE' })).value).toBe('active');
  });

  it('draws nothing when the account has no resume versions', () => {
    ui.D.resumeChoices = [];
    expect(ui.resumeSelect('job-resume', null, { id: 7 })).toBe('');
  });

  it('sets one job, or the selection, and never sends an empty choice', async () => {
    await ui.chooseResume({ value: '5', dataset: { resumeChoice: 'job-resume', id: '7' } });
    const one = fetchMock.mock.calls.find(([url]) => url === '/api/jobs/7/resume');
    expect(one[1].method).toBe('PUT');
    expect(JSON.parse(one[1].body)).toEqual({ mode: 'fixed', resume_id: 5 });
    ui.S.checked = new Set([7, 8]);
    await ui.chooseResume({ value: 'active', dataset: { resumeChoice: 'bulk-resume', id: '' } });
    const bulk = fetchMock.mock.calls.find(([url]) => url === '/api/jobs/bulk-resume');
    expect(JSON.parse(bulk[1].body)).toEqual({ ids: [7, 8], mode: 'active' });
    const before = fetchMock.mock.calls.length;
    ui.chooseResume({ value: '', dataset: { resumeChoice: 'bulk-resume' } });
    expect(fetchMock.mock.calls.length).toBe(before);
  });

  it('a search setup saves auto, active or a pinned resume', () => {
    ui.D.searchProfile = { profile: { roles: ['ML Engineer'] } };
    expect(ui.searchPayload()).toMatchObject({ resume_mode: 'auto', resume_id: null });
    ui.S.forms.sp = { resume_select: '5' };
    expect(ui.searchPayload()).toMatchObject({ resume_mode: 'fixed', resume_id: 5 });
    ui.S.forms.sp = { resume_select: 'active' };
    expect(ui.searchPayload()).toMatchObject({ resume_mode: 'active', resume_id: null });
    ui.S.forms.sp = {};
    ui.D.searchProfile = { profile: { resume_id: 3 } };  // saved before modes existed: a pinned resume
    expect(ui.searchPayload()).toMatchObject({ resume_mode: 'fixed', resume_id: 3 });
  });
});

describe('a job matched before its resume was tagged', () => {
  const inUse = rematch => ({ id: 3, name: 'Backend resume', version: 2, is_active: true, how: 'the active resume is used', rematch });

  it('names the resume its tags match now, with a button, only when re-matching would change it', () => {
    expect(ui.resumeInUseNote(inUse(null), '', 7)).not.toContain('job-rematch');
    const html = ui.resumeInUseNote(inUse({ resume: { id: 5, name: 'ML resume', version: 3, is_active: false }, reason: "role 'Staff Software Engineer'" }), '', 7);
    expect(html).toContain('ML resume · v3');
    expect(html).toContain("role &#39;Staff Software Engineer&#39;");
    expect(html).toContain('data-act="job-rematch"');
    expect(ui.resumeInUseNote(inUse({ resume: null, reason: 'x' }), '', 7)).toContain('no resume clearly');
    expect(ui.resumeInUseNote(inUse({ resume: null, reason: 'x' }))).not.toContain('job-rematch');
  });

  it('re-matches the job as auto, asking first when it was ranked', async () => {
    ui.D.jobModal = { id: 7, fit: 82 };
    vi.stubGlobal('confirm', vi.fn(() => false)); context.confirm = confirm;
    await ui.A['job-rematch']({ dataset: { id: '7' } });
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/jobs/7/resume')).toBe(false);
    vi.stubGlobal('confirm', vi.fn(() => true)); context.confirm = confirm;
    await ui.A['job-rematch']({ dataset: { id: '7' } });
    const call = fetchMock.mock.calls.find(([url]) => url === '/api/jobs/7/resume');
    expect(call[1].method).toBe('PUT');
    expect(JSON.parse(call[1].body)).toEqual({ mode: 'auto' });
  });

  it('watches the ranking it queued, so the open job shows the new fit when it is done', async () => {
    const watched = [], original = context.watchRanking;
    context.watchRanking = ids => { watched.push(ids); return Promise.resolve(); };
    ui.D.jobModal = { id: 7, fit: null };
    fetchMock.mockImplementation(async () => response({ changed: [7], ranking_queued: [7], ranking_skipped: [] }));
    await ui.A['job-rematch']({ dataset: { id: '7' } });
    expect(watched).toEqual([[7]]);
    fetchMock.mockImplementation(async () => response({ changed: [], ranking_queued: [], ranking_skipped: [] }));
    await ui.A['job-rematch']({ dataset: { id: '7' } });
    expect(watched).toEqual([[7]]);
    context.watchRanking = original;
  });
});

describe('hidden jobs', () => {
  it('keeps one job or the selection', async () => {
    await ui.A['domain-keep']({ dataset: { id: '7' } });
    expect(fetchMock.mock.calls.some(([url, o]) => url === '/api/jobs/7/domain-keep' && o.method === 'POST')).toBe(true);
    ui.S.checked = new Set([7]);
    await ui.A['bulk-domain-keep']();
    const bulk = fetchMock.mock.calls.find(([url]) => url === '/api/jobs/bulk-domain-keep');
    expect(JSON.parse(bulk[1].body)).toEqual({ ids: [7] });
  });
});

describe('Settings → Domain filter', () => {
  it('shows the lists, the inferred terms and the learned rules', () => {
    ui.D.domain = domain();
    const div = document.createElement('div');
    div.innerHTML = ui.renderDomainSettings();
    const text = div.textContent;
    expect(text).toContain('Hide jobs outside my domain at discovery');
    expect(text).toContain('sales');
    expect(text).toContain('underwriter');
    expect(text).toContain('Dismissed 4×');
    expect(text).not.toContain('claims'), 'a forgotten rule is not listed';
    expect([...div.querySelectorAll('[data-act="domain-learned"]')].map(b => b.dataset.action)).toEqual(['disable', 'promote', 'delete']);
    expect(div.querySelector('[data-act="domain-reapply"]')).not.toBeNull();
  });

  it('acts on one learned rule', async () => {
    fetchMock.mockImplementationOnce(async () => response(domain()));
    await ui.A['domain-learned']({ dataset: { term: 'underwriter', kind: 'title', action: 'disable' } });
    const call = fetchMock.mock.calls.find(([url]) => url === '/api/settings/domain/learned');
    expect(JSON.parse(call[1].body)).toEqual({ term: 'underwriter', kind: 'title', action: 'disable' });
  });
});

describe('deleting a resume version', () => {
  it('asks first, then deletes and refreshes the versions', async () => {
    vi.stubGlobal('confirm', vi.fn(() => false));
    context.confirm = confirm;
    await ui.A['rd-delete-version']({ dataset: { id: '9', name: 'Founder.pdf · v4' } });
    expect(fetchMock.mock.calls.some(([, o]) => o?.method === 'DELETE')).toBe(false);
    vi.stubGlobal('confirm', vi.fn(() => true));
    context.confirm = confirm;
    fetchMock.mockImplementation(async (url, o = {}) => response(o.method === 'DELETE'
      ? { deleted: 9, jobs: 2, saved_searches: 0, evidence_relinked: 0, evidence_retired: 1 }
      : { versions: [], policy: {} }));
    await ui.A['rd-delete-version']({ dataset: { id: '9', name: 'Founder.pdf · v4' } });
    const call = fetchMock.mock.calls.find(([, o]) => o?.method === 'DELETE');
    expect(call[0]).toBe('/api/resumes/9');
    expect(ui.D.resumeChoices).toBe(null);
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/settings/resume')).toBe(true);
  });

  it('tags a resume with several domains, each with its own roles', () => {
    ui.S.tagEdit = null;
    const shown = ui.resumeTags('version', 9, 'AI / ML', ['AI Engineer', 'Data Engineer'],
      [{ domain: 'AI / ML', roles: ['AI Engineer'] }, { domain: 'Data', roles: ['Data Engineer'] }]);
    expect(shown).toContain('AI / ML'); expect(shown).toContain('Data Engineer'); expect(shown).toContain('Edit tags');
    ui.A['rtag-edit']({ dataset: { kind: 'version', id: '9', groups: JSON.stringify([{ domain: 'AI / ML', roles: ['AI Engineer'] }]) } });
    expect(ui.S.forms.rtag.groups).toEqual([0]);
    ui.A['rtag-add-group']();
    ui.S.forms.rtag.domain_1 = 'Data';
    ui.A['rtag-add']({ dataset: { role: 'Data Engineer', index: '1' } });
    ui.D.resumeCatalog = [{ domain: 'Data', roles: ['Data Engineer', 'Analytics Engineer'] }];
    const editor = ui.resumeTags('version', 9);
    expect(editor).toContain('Remove domain'); expect(editor).toContain('+ Analytics Engineer');
    expect(ui.resumeTagBody()).toEqual({ domains: [{ domain: 'AI / ML', roles: ['AI Engineer'] }, { domain: 'Data', roles: ['Data Engineer'] }] });
    ui.A['rtag-remove-group']({ dataset: { index: '0' } });
    expect(ui.resumeTagBody()).toEqual({ domains: [{ domain: 'Data', roles: ['Data Engineer'] }] });
  });
});
