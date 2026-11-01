import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The Apply kit's pre-submit claim (Phase 5): start the submission before submitting on the employer's site, report
// with the claim's token, release it when nothing was sent, and say what happened when its outcome is unknown.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'renderKit'];
let ui, context, fetchMock, calls;

const kit = (over = {}) => ({
  job: { id: 11, title: 'Senior Backend Engineer', company: 'Examplecorp', fit: 80, url: 'https://boards.greenhouse.io/x/jobs/1' },
  status: 'approved', reviewer_score: 90, followup_date: '2026-10-26T00:00:00', files: [], answers: [],
  claim: null, execution: null, execution_mode: 'off', ...over });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  calls = [];
  fetchMock = vi.fn(async (url, options = {}) => {
    calls.push([options.method || 'GET', url, options.body ? JSON.parse(options.body) : null]);
    const body = url.includes('/apply-kit') ? ui.D.kit : url.includes('/api/tracker') ? { applications: [] } : {};
    return { ok: true, status: 200, text: async () => JSON.stringify(body) };
  });
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock;
  context.render = () => {};  // the actions re-render; the markup is checked through renderKit itself
  ui.S.forms = {}; ui.S.busy = {}; ui.S.kitJob = 11;
});

const html = k => { ui.D.kit = k; const el = document.createElement('div'); el.innerHTML = ui.renderKit(); return el; };
const acts = el => [...el.querySelectorAll('[data-act]')].map(b => b.dataset.act);

describe('the Apply kit claim', () => {
  it('has nothing to start before submitting: the duplicate check is automatic', () => {
    const el = html(kit());
    expect(acts(el)).not.toContain('kit-start');
    expect(el.textContent).not.toContain('Start submission');
    expect(acts(el)).toContain('mark-applied');
  });

  it('reports with the claim token of a submission already in progress', async () => {
    ui.D.kit = kit({ claim: { id: 4, job_id: 11, status: 'CLAIMED', client: 'apply_kit', claim_token: 'tok-123',
      claimed_at: '2026-10-19T10:00:00', lease_expires_at: '2026-10-19T11:00:00', started_by: 'you' } });
    expect(acts(html(ui.D.kit))).toContain('kit-release');
    await ui.A['mark-applied']({ dataset: { id: '11' } });
    const applied = calls.find(([m, u]) => m === 'POST' && u.endsWith('/api/jobs/11/applied'));
    expect(applied[2].claim_token).toBe('tok-123');
  });

  it('shows another client is submitting, without a way to submit over it', () => {
    const el = html(kit({ claim: { id: 5, job_id: 12, status: 'CLAIMED', client: 'mcp', started_by: 'Claude Desktop',
      claimed_at: '2026-10-19T10:00:00' } }));
    expect(el.textContent).toContain('Claude Desktop is submitting this posting (job #12)');
    expect(acts(el)).not.toContain('kit-start');
  });

  it('asks the person to resolve an uncertain submission', async () => {
    const el = html(kit({ claim: { id: 6, job_id: 11, status: 'UNCERTAIN', client: 'apply_kit', claimed_at: '2026-10-19T10:00:00' } }));
    expect(acts(el).filter(a => a === 'kit-resolve')).toHaveLength(2);
    await ui.A['kit-resolve']({ dataset: { id: '11', claim: '6', submitted: '' } });
    const resolved = calls.find(([m, u]) => m === 'POST' && u.endsWith('/submission/resolve'));
    expect(resolved[2]).toEqual({ claim_id: 6, submitted: false });
  });

  it('asks the questions the worker is waiting for, saves each as a verified answer, and links to them', async () => {
    const waiting = { id: 9, mode: 'ASSIST', status: 'PENDING', stage: 'NEEDS_INPUT', interrupt: { kind: 'NEEDS_INPUT', question_ids: [21, 22], confirm_question_ids: [] } };
    const questions = [
      { id: 21, question: 'Why Examplecorp?', answer: '', verified: false, answered: false, answer_type: 'text', answer_options: [], category: 'general', sensitive: false },
      { id: 22, question: 'Do you require visa sponsorship?', answer: '', verified: false, answered: false, answer_type: 'boolean', answer_options: [], category: 'general', sensitive: true }];
    const el = html(kit({ execution_mode: 'assist', execution: waiting, execution_questions: questions }));
    expect(el.textContent).toContain('2 of 2 questions to answer');
    expect(el.textContent).toContain('Why Examplecorp?');
    expect(el.querySelector('#kitq-q21').tagName).toBe('TEXTAREA');
    expect([...el.querySelector('#kitq-q22').options].map(o => o.value)).toEqual(['', 'Yes', 'No']);
    ui.S.forms.kitq = { q21: 'I build payment systems like yours.' };
    await ui.A['kit-answer']({ dataset: { qid: '21' } });
    const saved = calls.find(([m, u]) => m === 'PUT' && u === '/api/knowledge/21');
    expect(saved[2]).toEqual({ question: 'Why Examplecorp?', answer: 'I build payment systems like yours.', category: 'general', answer_type: 'text', verified: true, sensitive: false });
    ui.S.forms.kitq = {};
    await ui.A['kit-answer']({ dataset: { qid: '22' } });  // nothing chosen: nothing is saved
    expect(calls.some(([m, u]) => m === 'PUT' && u === '/api/knowledge/22')).toBe(false);
    expect(acts(el)).toContain('kit-open-questions');
  });

  it('runs a job on browser-use when the backend is set up, and never offers it otherwise', async () => {
    const done = { id: 9, mode: 'ASSIST', status: 'FAILED', stage: 'FAILED', interrupt: null, result_code: null };
    expect(acts(html(kit({ execution_mode: 'assist', execution: done, browser_use_ready: false })))).not.toContain('kit-browser-use');
    const el = html(kit({ execution_mode: 'assist', execution: done, browser_use_ready: true }));
    expect(acts(el)).toContain('kit-browser-use');
    await ui.A['kit-browser-use']({ dataset: { id: '11' } });
    const queued = calls.find(([m, u]) => m === 'POST' && u === '/api/jobs/11/application-execution');
    expect(queued[2]).toEqual({ backend: 'browser_use_webui' });
  });

  it('shows the application worker only when it is on, and offers Apply for me only with Auto submit on', async () => {
    expect(acts(html(kit()))).not.toContain('kit-prepare');
    const done = { id: 9, mode: 'ASSIST', status: 'COMPLETED', stage: 'READY_TO_SUBMIT', interrupt: null, result_code: 'READY_TO_SUBMIT' };
    const el = html(kit({ execution_mode: 'assist', execution: done }));
    expect(el.textContent).toContain('You submit');
    // The private page is gone: the kit says so instead of implying a filled form is waiting somewhere.
    expect(el.textContent).toContain('which was then closed');
    expect(acts(el)).not.toContain('kit-guarded');
    const window = html(kit({ execution_mode: 'assist', execution: { ...done, backend: 'browser_use_webui' } }));
    expect(window.textContent).toContain('filled in the open browser window');
    expect(acts(html(kit({ execution_mode: 'guarded_auto', guarded_consent: false, execution: done })))).not.toContain('kit-guarded');
    const auto = html(kit({ execution_mode: 'guarded_auto', guarded_consent: true, execution: done }));
    expect(auto.textContent).toContain('submits it for you');
    expect(acts(auto)).toContain('kit-guarded');
    await ui.A['kit-prepare']({ dataset: { id: '11', mode: 'ASSIST' } });
    expect(calls.find(([m, u]) => m === 'POST' && u.endsWith('/api/jobs/11/application-execution'))[2]).toEqual({ mode: 'ASSIST' });
  });

  it('links a queued run to its task in Running workers, and says why it is retrying', async () => {
    const queued = { id: 9, mode: 'GUARDED_AUTO', status: 'PENDING', stage: 'RETRYING', interrupt: null, result_code: null,
      attempts: 1, last_error: 'The application page did not load in time (TimeoutError)' };
    const el = html(kit({ execution_mode: 'guarded_auto', guarded_consent: true, execution: queued }));
    const link = el.querySelector('[data-act="wk-open-task"]');
    expect(link.textContent).toContain('Watch it in Running workers');
    expect(link.dataset.id).toBe('9');
    expect(el.textContent).toContain('Last try: The application page did not load in time');
    await ui.A['wk-open-task'](link);
    expect([ui.S.screen, ui.S.kitJob, ui.S.wk.tab, ui.S.wk.history]).toEqual(['workers', null, 'application', { kind: 'application', id: 9 }]);
  });

  it('shows each sensitive question and its saved answer before they are confirmed, and sends only those', async () => {
    const execution = { id: 9, status: 'PENDING', stage: 'NEEDS_INPUT', interrupt: { question_ids: [], confirm_question_ids: [3] } };
    // Nothing to show (no confirmations sent): nothing to confirm blindly.
    expect(acts(html(kit({ execution_mode: 'assist', execution })))).not.toContain('kit-confirm');
    const el = html(kit({ execution_mode: 'assist', execution, execution_confirmations: [
      { id: 3, question: 'Are you authorized to work in the United States?', answer: 'Yes' }] }));
    expect(el.textContent).toContain('Are you authorized to work in the United States?');
    expect(el.textContent).toContain('Yes');
    expect(acts(el)).toContain('kit-confirm');
    const button = [...el.querySelectorAll('[data-act="kit-confirm"]')][0];
    expect(button.dataset.ids).toBe('3');
  });

  it('lists the fields highlighted in the open window, continues without reloading, and offers to save what was typed', async () => {
    const waiting = { id: 9, mode: 'ASSIST', status: 'QUEUED', stage: 'NEEDS_HUMAN', interrupt: { reason: 'FIELDS_NEED_YOU', question_ids: [3] }, fields: [] };
    const takeover = { reason: 'FIELDS_NEED_YOU', expires_at: '2026-10-26T12:00:00', fields: [{ id: 'bu_py', label: 'Years of Python?' }] };
    const el = html(kit({ execution_mode: 'assist', execution: waiting, execution_takeover: takeover,
      execution_questions: [{ id: 3, question: 'Years of Python?', answered: false, answer_type: 'text', answer_options: [] }] }));
    expect(el.textContent).toContain('1 field needs you');
    expect(el.textContent).toContain('Years of Python?');
    expect(el.textContent).toContain('the page is not reloaded');
    expect(acts(el)).toContain('kit-continue');
    expect(acts(el)).not.toContain('kit-save-login');
    expect(acts(el)).not.toContain('kit-save-window');
    const done = { id: 9, mode: 'ASSIST', status: 'COMPLETED', stage: 'READY_TO_SUBMIT', interrupt: null, result_code: 'READY_TO_SUBMIT',
      fields: [{ id: 'bu_py', label: 'Years of Python?', status: 'PERSON' }, { id: 'bu_name', label: 'Name', status: 'RESOLVED' }] };
    const ready = html(kit({ execution_mode: 'assist', execution: done }));
    expect(ready.textContent).toContain('You filled 1 field yourself in the window (Years of Python?)');
    expect(acts(ready)).toContain('kit-save-window');
    ui.D.kit = kit({ execution_mode: 'assist', execution: done });
    await ui.A['kit-save-window']({ dataset: { exec: '9' } });
    expect(calls.some(([m, u]) => m === 'POST' && u.endsWith('/api/application-executions/9/save-window-answers'))).toBe(true);
  });
  it('looks up the code the site emailed after Submit only when asked, for the person to copy', async () => {
    const execution = { id: 9, mode: 'GUARDED_AUTO', status: 'COMPLETED', stage: 'SUBMISSION_UNCERTAIN', interrupt: null, result_code: 'SUBMISSION_UNCERTAIN' };
    const intent = { id: 3, status: 'UNCERTAIN', reason: 'The site emailed you a code to finish the application', evidence: { email_code: true } };
    ui.S.kitEmailCode = null;
    let el = html(kit({ execution_mode: 'guarded_auto', guarded_consent: true, execution, execution_intent: intent }));
    expect(el.textContent).toContain('The site emailed you a code to finish the application');
    expect(acts(el)).toContain('kit-email-code');
    expect(acts(el)).not.toContain('kit-copy-email-code');
    expect(calls.some(([, u]) => u.includes('/email-code'))).toBe(false);  // nothing read until the person asks
    fetchMock.mockImplementationOnce(async (url) => { calls.push(['GET', url, null]); return { ok: true, status: 200, text: async () => JSON.stringify({ status: 'FOUND', code: 'Wz3K9pQa' }) }; });
    await ui.A['kit-email-code']({ dataset: { exec: '9' } });
    expect(calls.find(([, u]) => u === '/api/application-executions/9/email-code')).toBeTruthy();
    el = html(ui.D.kit);
    expect(el.textContent).toContain('Wz3K9pQa');
    expect(acts(el)).toContain('kit-copy-email-code');
    ui.A['close-kit']();
    expect(ui.S.kitEmailCode).toBeNull();  // the code is gone with the kit
    // Any other unknown outcome has no code to look up.
    expect(acts(html(kit({ execution_mode: 'guarded_auto', execution, execution_intent: { ...intent, evidence: {} } })))).not.toContain('kit-email-code');
  });
});
