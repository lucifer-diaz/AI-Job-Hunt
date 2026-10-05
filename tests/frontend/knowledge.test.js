import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const scripts = ['settings', 'resume_data', 'job_data', 'monitoring', 'orchestration', 'workers', 'knowledge', 'inbox', 'guide', 'app'].map(name => ({ source: readFileSync(`app/static/${name}.js`, 'utf8').split('/* ───────────── boot ───────────── */')[0], filename: resolve(`app/static/${name}.js`) }));
const names = ['S', 'D', 'A', 'render', 'readHash', 'setHash', 'knowledgeNavigate', 'knowledgeState', 'knowledgeFiltered', 'knowledgeOpen', 'knowledgeSave', 'loadKnowledge', 'knowledgeCancel', 'knowledgeCopy', 'setF'];
let ui, context, fetchMock, writeText, confirm;
const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const answers = () => [
  { id: 1, question: 'What is your notice period?', answer: '30 days', category: 'notice_period', answer_type: 'text', verified: true, sensitive: false, use_count: 4 },
  { id: 2, question: 'What compensation do you expect?', answer: 'Discuss with recruiter', category: 'compensation', answer_type: 'text', verified: true, sensitive: true, use_count: 2 },
  { id: 3, question: 'Are you willing to relocate?', answer: '', category: 'relocation', answer_type: 'boolean', verified: false, sensitive: false, use_count: 0,
    answer_options: ['Yes', 'No'], asked_by: { job_id: 9, company: 'Globex', title: 'Staff Engineer', required: true } },
];
const evidence = () => [
  { id: 4, claim: 'Reduced cloud spend by 35%', company: 'Acme', role: 'Platform engineer', skills: ['AWS', 'Python'], verified: false },
  { id: 5, claim: 'Led a team of eight engineers', company: '', role: '', skills: [], verified: true },
];
function input(id, value, event = 'input') {
  const el = document.getElementById(id); el.value = value;
  el.dispatchEvent(new Event(event, { bubbles: true }));
}
function filter(key, value) { input(`knowledge-${key}`, value, key === 'search' ? 'input' : 'change'); }
function action(name, id) { return ui.A[name]({ dataset: { id: String(id) } }); }
const text = () => document.getElementById('view').textContent;

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"test@example.test"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast" hidden></div>';
  history.replaceState(null, '', '#knowledge/answers');
  writeText = vi.fn(async () => {}); confirm = vi.fn(() => true);
  fetchMock = vi.fn(async url => response(url === '/api/workspace/summary' ? ui.D.summary : url === '/api/knowledge' ? ui.D.knowledge : url === '/api/evidence' ? ui.D.evidence : {}));
  if (!ui) {
    context = createContext({ document, window, location, history, URLSearchParams, FormData, File, setTimeout, clearTimeout, setInterval, clearInterval, console });
    scripts.forEach(({ source, filename }) => runInContext(source, context, { filename }));
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirm; context.navigator = { clipboard: { writeText } };
  window.scrollTo = vi.fn();
  Object.assign(ui.S, { screen: 'knowledge', knowledgeSection: 'answers', knowledgeUI: undefined, forms: {}, busy: {}, evidencePrefill: '', jobOpen: null });
  Object.assign(ui.D, { summary: { profile: {}, counts: {}, auto_search: { schedule: 'daily' }, application_mode: 'manual_approval' }, knowledge: answers(), evidence: evidence(), knowledgeLoading: false, knowledgeLoadError: '' });
  ui.render();
});

describe('Knowledge bank workspace', () => {
  it('shows which application asked a waiting question and the choices its form offered', () => {
    expect(text()).toContain('Asked by Globex · Staff Engineer · required — Options: Yes / No');
    expect(text()).not.toContain('Asked by Acme');
  });
  it('defaults legacy and invalid links to answers and supports evidence deep links', () => {
    for (const hash of ['#knowledge', '#knowledge/unknown', '#knowledge/answers']) {
      history.replaceState(null, '', hash); ui.readHash(); expect(ui.S.knowledgeSection).toBe('answers');
    }
    history.replaceState(null, '', '#knowledge/evidence'); ui.readHash(); ui.setHash(); ui.render();
    expect(ui.S.knowledgeSection).toBe('evidence');
    expect(document.querySelector('[aria-current="page"]').textContent).toContain('Evidence bank');
  });
  it('navigates through real links with heading focus and no network calls', () => {
    document.querySelector('.knowledge-nav a[href="#knowledge/evidence"]').click();
    expect(location.hash).toBe('#knowledge/evidence');
    expect(document.activeElement.id).toBe('knowledge-heading');
    expect(fetchMock).not.toHaveBeenCalled();
    expect(text()).toContain('Reduced cloud spend');
  });
  it('shows verified and sensitive statuses independently with readable categories', () => {
    const item = document.querySelectorAll('.knowledge-item')[1];
    expect(item.textContent).toContain('Verified'); expect(item.textContent).toContain('Sensitive');
    expect(text()).toContain('Notice period'); expect(text()).not.toContain('notice_period');
    expect(document.querySelector('[data-act="kn-copy"][data-id="3"]').disabled).toBe(true);
  });
  it('combines search and status, category, sensitivity filters locally', () => {
    filter('search', ' recruiter '); filter('category', 'compensation'); filter('status', 'verified'); filter('sensitive', 'sensitive');
    expect(ui.knowledgeFiltered('answers').map(a => a.id)).toEqual([2]);
    expect(document.querySelector('.knowledge-results').textContent).toBe('1 of 3 answers');
    filter('status', 'review'); expect(text()).toContain('No matches found');
    expect(fetchMock).not.toHaveBeenCalled();
    ui.A['kn-clear'](); expect(ui.knowledgeFiltered('answers')).toHaveLength(3);
  });
  it('searches evidence skills and context while preserving independent filters', () => {
    filter('search', 'notice'); ui.knowledgeNavigate('evidence'); filter('search', 'python'); filter('status', 'review');
    expect(ui.knowledgeFiltered('evidence').map(a => a.id)).toEqual([4]);
    ui.knowledgeNavigate('answers'); expect(document.getElementById('knowledge-search').value).toBe('notice');
    ui.knowledgeNavigate('evidence'); expect(document.getElementById('knowledge-search').value).toBe('python');
  });
  it('distinguishes initial loading, errors, empty collections and zero search results', async () => {
    ui.D.knowledge = undefined; ui.D.evidence = undefined; ui.render(); expect(text()).toContain('Loading your Knowledge bank');
    fetchMock.mockResolvedValueOnce(response({ detail: 'Offline' }, 503));
    await ui.loadKnowledge(); ui.render(); expect(document.querySelector('[role="alert"]').textContent).toContain('Offline');
    fetchMock.mockImplementation(async () => response([])); await ui.A['kn-retry']();
    expect(text()).toContain('Your next application starts here'); expect(document.querySelector('[role="alert"]')).toBeNull();
    ui.knowledgeNavigate('evidence'); expect(text()).toContain('Manage or re-parse your resume');
  });
  it('labels every editor field, focuses question, and avoids accidental cancel on Enter', () => {
    ui.knowledgeOpen(); expect(document.activeElement.id).toBe('knnew-question');
    for (const el of document.querySelectorAll('#knowledge-editor-form input, #knowledge-editor-form textarea, #knowledge-editor-form select')) expect(el.labels.length).toBeGreaterThan(0);
    expect(document.querySelector('[data-act="kn-cancel"]').type).toBe('button');
    expect(document.querySelector('#knowledge-editor-form [type="submit"]').textContent).toBe('Save answer');
  });
  it('preserves drafts across sections, filtering, and switching between records', () => {
    ui.knowledgeOpen(1); input('kn1-answer', '60 days'); filter('search', 'compensation');
    ui.knowledgeNavigate('evidence'); ui.knowledgeOpen(); input('ev-claim', 'Evidence draft');
    ui.knowledgeNavigate('answers'); expect(document.getElementById('kn1-answer').value).toBe('60 days');
    ui.knowledgeOpen(2); input('kn2-answer', 'Another draft'); ui.knowledgeOpen(1);
    expect(document.getElementById('kn1-answer').value).toBe('60 days');
    ui.knowledgeNavigate('evidence'); expect(document.getElementById('ev-claim').value).toBe('Evidence draft');
  });
  it('cancels only the current draft and restores focus', () => {
    ui.knowledgeOpen(1); input('kn1-answer', 'Draft'); ui.knowledgeNavigate('evidence'); ui.knowledgeOpen(); input('ev-claim', 'Keep me');
    ui.knowledgeNavigate('answers'); ui.knowledgeCancel();
    expect(ui.S.forms.kn1).toBeUndefined(); expect(ui.S.forms.ev.claim).toBe('Keep me');
    expect(document.activeElement.dataset.act).toBe('kn-add');
  });
  it('retains failed save drafts and displays an inline error, then saves with existing API defaults', async () => {
    ui.knowledgeOpen(); input('knnew-question', 'My question?'); input('knnew-answer', 'My answer');
    fetchMock.mockResolvedValueOnce(response({ detail: 'Try again' }, 503)); await ui.knowledgeSave();
    expect(document.querySelector('[role="alert"]').textContent).toContain('Try again'); expect(ui.S.forms.knnew.answer).toBe('My answer');
    await ui.knowledgeSave();
    const [url, opts] = fetchMock.mock.calls[1]; expect(url).toBe('/api/knowledge'); expect(opts.method).toBe('POST');
    expect(JSON.parse(opts.body)).toEqual({ question: 'My question?', answer: 'My answer', category: 'general', answer_type: 'text', verified: true, sensitive: false });
    expect(ui.S.forms.knnew).toBeUndefined(); expect(document.activeElement.dataset.act).toBe('kn-add');
  });
  it('updates the selected answer without overwriting its flags or format', async () => {
    ui.knowledgeOpen(2); input('kn2-answer', 'Negotiable'); await ui.knowledgeSave();
    expect(fetchMock.mock.calls[0][0]).toBe('/api/knowledge/2'); expect(fetchMock.mock.calls[0][1].method).toBe('PUT');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toMatchObject({ answer: 'Negotiable', verified: true, sensitive: true, category: 'compensation' });
  });
  it('validates questions and evidence before any request', async () => {
    ui.knowledgeOpen(); input('knnew-question', 'x'); await ui.knowledgeSave(); expect(fetchMock).not.toHaveBeenCalled();
    expect(text()).toContain('at least 2 characters'); ui.knowledgeNavigate('evidence'); ui.knowledgeOpen(); await ui.knowledgeSave();
    expect(text()).toContain('Describe the factual claim'); expect(fetchMock).not.toHaveBeenCalled();
  });
  it('saves evidence with trimmed skills and verified semantics', async () => {
    ui.knowledgeNavigate('evidence'); ui.knowledgeOpen(); input('ev-claim', ' Built reliable APIs '); input('ev-skills', ' Python, AWS, '); input('ev-company', 'Acme');
    await ui.knowledgeSave(); expect(fetchMock.mock.calls[0][0]).toBe('/api/evidence');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ claim: 'Built reliable APIs', company: 'Acme', role: '', skills: ['Python', 'AWS'], verified: true });
    expect(ui.S.forms.ev).toBeUndefined();
  });
  it('opens evidence for a job gap, without discarding existing claim input', async () => {
    ui.setF('ev', 'claim', 'Existing draft'); ui.A['add-evidence-for']({ dataset: { gap: 'Distributed systems' } });
    expect(location.hash).toBe('#knowledge/evidence'); expect(document.activeElement.id).toBe('ev-claim');
    expect(document.getElementById('ev-claim').value).toBe('Existing draft\nDistributed systems: ');
    await vi.waitFor(() => expect(ui.D.knowledgeLoading).toBe(false));
  });
  it('does not delete when confirmation is cancelled', async () => {
    confirm.mockReturnValue(false); await action('kn-delete', 1); await action('ev-delete', 4);
    expect(fetchMock).not.toHaveBeenCalled();
  });
  it.each([['kn-delete', 'answers', 1, 'Answer deleted'], ['ev-delete', 'evidence', 4, 'Evidence removed']])('handles %s failure without a success message or loss of data', async (act, section, id, success) => {
    ui.knowledgeNavigate(section); fetchMock.mockResolvedValueOnce(response({ detail: 'Delete denied' }, 403));
    await action(act, id); expect(text()).toContain('Delete denied'); expect(document.getElementById('toast').textContent).not.toContain(success);
    expect(ui.knowledgeFiltered(section).some(r => r.id === id)).toBe(true);
    await action(act, id); expect(document.getElementById('toast').textContent).toBe(success);
    expect(fetchMock.mock.calls[1][1].method).toBe('DELETE');
  });
  it('clears an editor only after its record is successfully deleted', async () => {
    ui.knowledgeOpen(1); input('kn1-answer', 'Changed'); await action('kn-delete', 1);
    expect(ui.S.forms.kn1).toBeUndefined(); expect(ui.knowledgeState().editors.answers).toBeUndefined();
  });
  it('verifies evidence through the existing endpoint and surfaces failures', async () => {
    ui.knowledgeNavigate('evidence'); fetchMock.mockResolvedValueOnce(response({ detail: 'Cannot verify' }, 503)); await action('ev-verify', 4);
    expect(text()).toContain('Cannot verify'); await action('ev-verify', 4);
    expect(fetchMock.mock.calls[1][0]).toBe('/api/evidence/4'); expect(fetchMock.mock.calls[1][1].method).toBe('PATCH');
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ verified: true });
  });
  it('copies answers before recording usage', async () => {
    await ui.knowledgeCopy(1); expect(writeText).toHaveBeenCalledWith('30 days'); expect(fetchMock.mock.calls[0][0]).toBe('/api/knowledge/1/used');
    expect(ui.D.knowledge[0].use_count).toBe(5); expect(document.getElementById('toast').textContent).toBe('Answer copied');
  });
  it('does not count failed clipboard copies and reports usage tracking failures accurately', async () => {
    writeText.mockRejectedValueOnce(new Error('Denied')); document.execCommand = vi.fn(() => false); await ui.knowledgeCopy(1);
    expect(fetchMock).not.toHaveBeenCalled(); expect(text()).toContain('Could not copy');
    fetchMock.mockResolvedValueOnce(response({}, 503)); await ui.knowledgeCopy(1);
    expect(text()).toContain('Answer copied, but its usage count'); expect(ui.D.knowledge[0].use_count).toBe(4);
  });
  it('supports clipboard fallback and skips empty or missing answers', async () => {
    await ui.knowledgeCopy(3); await ui.knowledgeCopy(99); expect(writeText).not.toHaveBeenCalled();
    writeText.mockRejectedValueOnce(new Error('Denied')); document.execCommand = vi.fn(() => true); await ui.knowledgeCopy(1);
    expect(document.execCommand).toHaveBeenCalledWith('copy'); expect(ui.D.knowledge[0].use_count).toBe(5);
  });
  it('blocks duplicate writes while preserving fields during a pending save', async () => {
    ui.knowledgeOpen(1); let resolve; fetchMock.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const pending = ui.knowledgeSave(); await ui.knowledgeSave(); expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(document.querySelector('fieldset').disabled).toBe(true); resolve(response({})); await pending;
  });
  it('escapes user-controlled content in answers, evidence, and errors', () => {
    ui.D.knowledge[0].question = '<img src=x onerror=alert(1)>'; ui.D.evidence[0].claim = '<script>alert(1)</script>';
    ui.knowledgeState().errors.answers = '<iframe src=x>'; ui.render(); expect(document.querySelector('#view img, #view iframe')).toBeNull();
    ui.knowledgeNavigate('evidence'); expect(document.querySelector('#view script')).toBeNull();
  });
});
