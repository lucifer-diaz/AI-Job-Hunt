import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Job Insights in the UI: every item says what kind of statement it is, generated questions are never shown as
// reported, a market estimate is never the employer's band, conflicting estimates are not averaged, official values and
// employee-reported themes are separate lists, and the Sources tab explains what is missing.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'renderJobInsights', 'renderInsightsCard', 'renderStagePrep', 'insState', 'insRange'];
let ui, context, fetchMock;

const response = (data, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(data) });
const sources = [
  { name: 'job_posting', label: 'Job posting', status: 'OK', symbol: '✓', observations: 1 },
  { name: 'glassdoor_compensation', label: 'Glassdoor salaries', status: 'OK', symbol: '✓', observations: 2, cached: true },
  { name: 'payscale', label: 'PayScale', status: 'FAILED', symbol: '!', detail: 'Apify answered 500' },
  { name: 'public_web', label: 'Public web', status: 'NOT_CONFIGURED', symbol: '○', detail: 'No public web search provider is configured' }];
const insights = (extra = {}) => ({
  job: { id: 7, title: 'Lead Software Engineer', company: 'Grab' }, eligible: true, status: 'PARTIAL', researching: false,
  refresh_failed: false, last_successful_at: '2026-10-07T08:00:00', sources, legend: { '✓': 'available', '○': 'not configured' },
  kinds: { FACT: 'Stated by a source', DERIVED: 'Interpreted', GENERATED: 'Preparation written by a model', UNKNOWN: 'Not known' },
  identity: { website: 'https://grab.com', website_source: 'POSTING' },
  overview: { compensation: { status: 'MULTI_SOURCE_CONSENSUS', confidence: 'MEDIUM', range: { low: 185000, high: 220000, currency: 'SGD', period: 'year', label: 'Market estimate — ask the recruiter for the approved band' } },
    interview: { reported_questions: 1, role_derived_questions: 1, candidate_specific_questions: 1, stages: 2, focus_areas: ['System design'] },
    company: { summary: 'Mobility and payments platform.', official_values: 2, culture_themes: 1 } },
  compensation: { status: 'MULTI_SOURCE_CONSENSUS', confidence: 'MEDIUM', employer_posted_range: null, recruiter_budget: null,
    market_range: { low: 185000, high: 220000, currency: 'SGD', period: 'year', method: 'Weighted median', label: 'Market estimate — not the employer\'s approved band' },
    recommended_discussion_range: { low: 185000, high: 220000, currency: 'SGD', period: 'year', label: 'Market estimate — ask the recruiter for the approved band' },
    observations: [{ source_type: 'GLASSDOOR', source_name: 'glassdoor_compensation', original: { low: 190000, high: 230000, currency: 'SGD', period: 'year' }, basis: 'BASE', sample_size: 12, included: true, annual_low: 190000, annual_high: 230000, currency: 'SGD' },
      { source_type: 'PAYSCALE', source_name: 'payscale', original: { low: 140000, high: 160000, currency: 'USD', period: 'year' }, basis: 'BASE', included: false, excluded_reason: 'In USD; not converted' }],
    conflict: null, annualization: '2,080 hours a year' },
  compensation_prep: { questions: [{ question: 'Could you share the approved band?', kind: 'GENERATED' }], note: 'Market figures are estimates.' },
  company: { summary: { text: 'Mobility and payments platform.', kind: 'DERIVED', source_ids: [3] },
    official_values: [{ value: 'Heart', kind: 'FACT', source_ids: [5], sources: [{ url: 'https://grab.com/values', title: 'Our values' }] },
      { value: 'Hunger', kind: 'FACT', source_ids: [6] }],
    culture_themes: [{ theme: 'Fast paced', sentiment: 'MIXED', kind: 'DERIVED', label: 'Employee-reported' }],
    sentiment: { overall_rating: 4.1, sample_size: 800, label: 'Employee-reported (not objective fact)' } },
  interview: { process: { stages: [{ name: 'Recruiter screen', order: 1, origin: 'REPORTED' }, { name: 'System design', order: 2, origin: 'INFERRED', rationale: 'Staff-level backend role' }] },
    reported_questions: [{ question: 'Design a ride matching service', kind: 'FACT', origin: 'REPORTED', source_type: 'GLASSDOOR', reported_role: 'Software Engineer · Singapore' }],
    role_derived_questions: [{ id: 'q1', question: 'How would you design an idempotent payment workflow?', origin: 'ROLE_DERIVED', kind: 'GENERATED', why_likely: 'The JD requires reliability.', category: 'SYSTEM_DESIGN', stage: 'TECHNICAL' }],
    candidate_specific_questions: [{ id: 'q2', question: 'How did you balance speed and reliability?', origin: 'CANDIDATE_SPECIFIC', kind: 'GENERATED', evidence_ids: [1, 2, 3] }],
    focus_areas: [], value_prep: [] },
  questions_to_ask: [{ stage: 'HIRING_MANAGER', question: 'How is reliability ownership split today?', kind: 'GENERATED', context: 'platform reliability' }],
  risks: [], unknowns: [{ text: 'The posting does not state a salary.', kind: 'UNKNOWN' }], recruiter_input: null, ...extra });
const panel = (tab, data = insights()) => {
  ui.insState().tab = tab;
  ui.D.insights = { 7: data };
  const div = document.createElement('div');
  div.innerHTML = ui.renderJobInsights(7);
  return div;
};

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  fetchMock = vi.fn(async url => response(url.includes('/refresh') ? { ...insights(), researching: true, task: { id: 3, status: 'PENDING' } } : {}));
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
  ui.S.forms = {}; ui.S.busy = {};
});

describe('job insights', () => {
  it('offers Find insights before anything was researched', () => {
    const div = panel('overview', insights({ status: 'NONE', last_successful_at: null, eligible: false }));
    expect(div.querySelector('[data-act="ins-refresh"]').textContent).toBe('Find insights');
    expect(div.textContent).toContain('Research starts automatically when the job is approved');
  });

  it('says a refresh failed and keeps showing the last good data', () => {
    const div = panel('overview', insights({ refresh_failed: true, last_error: 'payscale: Apify answered 500' }));
    expect(div.textContent).toContain('Refresh failed.');
    expect(div.textContent).toContain('Last successful data');
    expect(div.textContent).toContain('SGD 185K–220K');
  });

  it('never presents a market estimate as the employer band', () => {
    const div = panel('compensation');
    expect(div.textContent).toContain('Employer posting');
    expect(div.textContent).toContain('Not listed');
    expect(div.textContent).toContain('Estimate, not the employer band');
    expect(div.textContent).toContain('How was this estimated?');
    expect(div.textContent).toContain('In USD; not converted');
  });

  it('lists conflicting estimates instead of a midpoint', () => {
    const data = insights();
    data.compensation = { ...data.compensation, status: 'CONFLICTING_ESTIMATES', market_range: null,
      conflict: { spread: 0.6, between: 'market sources', alternatives: [{ label: 'glassdoor', low: 210000, high: 260000, currency: 'SGD' }, { label: 'payscale', low: 120000, high: 140000, currency: 'SGD' }] } };
    const div = panel('compensation', data);
    expect(div.textContent).toContain('Conflicting estimates');
    expect(div.textContent).toContain('SGD 210K–260K');
    expect(div.textContent).toContain('SGD 120K–140K');
  });

  it('keeps official values apart from employee-reported themes', () => {
    const div = panel('company');
    expect(div.textContent).toContain('OFFICIAL VALUES');
    expect(div.textContent).toContain('EMPLOYEE-REPORTED THEMES');
    const values = [...div.querySelectorAll('.ins-item .small.b')].map(e => e.textContent);
    expect(values).toEqual(['Heart', 'Hunger']);
    expect(div.textContent).toContain('Employee-reported (not objective fact)');
  });

  it('labels reported questions as facts and generated ones as generated preparation', () => {
    const div = panel('interview');
    expect(div.textContent).toContain('REPORTED BY CANDIDATES');
    expect(div.textContent).toContain('LIKELY FOR THIS ROLE');
    expect(div.textContent).toContain('BASED ON YOUR PROFILE');
    const reported = [...div.querySelectorAll('.ins-q')].find(q => q.textContent.includes('ride matching'));
    expect(reported.textContent).toContain('Source fact');
    expect(reported.textContent).not.toContain('Generated prep');
    const generated = [...div.querySelectorAll('.ins-q')].find(q => q.textContent.includes('idempotent'));
    expect(generated.textContent).toContain('Generated prep');
    expect(generated.querySelector('[data-act="ins-prepare"]')).not.toBeNull();
    expect(div.textContent).toContain('Inferred');
    expect(div.textContent).toContain('Evidence available: 3 stories');
  });

  it('explains every source status in the Sources tab', () => {
    const div = panel('sources');
    const rows = [...div.querySelectorAll('tbody tr')].map(r => r.textContent);
    expect(rows[0]).toContain('✓');
    expect(rows.find(r => r.includes('PayScale'))).toContain('failed');
    expect(rows.find(r => r.includes('Public web'))).toContain('not configured');
    expect(rows.find(r => r.includes('Glassdoor'))).toContain('reused');
    expect(div.querySelector('#insin7-company_website')).not.toBeNull();
  });

  it('queues a refresh and shows research in progress', async () => {
    ui.D.insights = { 7: insights() };
    await ui.A['ins-refresh']({ dataset: { id: '7' } });
    expect(fetchMock.mock.calls.some(([url, opt]) => url === '/api/jobs/7/insights/refresh' && opt.method === 'POST')).toBe(true);
    expect(ui.D.insights[7].researching).toBe(true);
  });

  it('shows each form question as a library answer, a draft or a question only the person can answer', () => {
    const form = { pending: false, max_questions: 20, note: 'Check and edit each one.', from_form: [
      { id: 'f1', question: 'What is your notice period?', origin: 'FORM', status: 'LIBRARY', kind: 'FACT', answer: 'One month' },
      { id: 'f2', question: 'Do you require visa sponsorship?', origin: 'FORM', status: 'NEEDS_INPUT', kind: 'UNKNOWN', answer: null, reason: 'Only you can answer this' },
      { id: 'f3', question: 'Why Grab?', origin: 'FORM', status: 'PENDING' }],
      likely: [{ id: 'f4', question: 'Describe a reliable system you built.', origin: 'PREDICTED', status: 'DRAFTED', kind: 'GENERATED',
        answer: 'I built reliable payment services.', evidence: ['Built reliable payment services'], rationale: 'The JD centres on reliability.' }] };
    const div = panel('form', insights({ application_form: form }));
    const text = div.textContent;
    expect(text).toContain('From your library');
    expect(text).toContain('Needs your answer');
    expect(text).toContain('Drafting…');
    expect(text).toContain('Draft');
    expect(text).toContain('Only you can answer this');
    expect(text).toContain('From your evidence: Built reliable payment services');
    // Only pasted questions can be removed; a library answer is not offered for saving again.
    expect(div.querySelectorAll('[data-act="ins-form-remove"]').length).toBe(3);
    expect([...div.querySelectorAll('[data-act="ins-form-save"]')].map(b => b.dataset.q)).toEqual(['f2', 'f4']);
    expect([...div.querySelectorAll('[data-act="ins-form-edit"]')].map(b => b.textContent)).toEqual(['Answer here', 'Review & save']);
  });

  it('saves a reviewed draft to the library as verified without leaving the job', async () => {
    const draft = { id: 'f4', question: 'Describe a reliable system you built.', origin: 'PREDICTED', status: 'DRAFTED', kind: 'GENERATED', answer: 'I built reliable payment services.' };
    const saved = insights({ application_form: { from_form: [], likely: [{ ...draft, status: 'LIBRARY', kind: 'FACT', answer: 'I built and ran reliable payment services.' }] } });
    fetchMock.mockImplementation(async () => response(saved));
    ui.D.knowledge = [];
    let div = panel('form', insights({ application_form: { from_form: [], likely: [draft] } }));
    ui.A['ins-form-edit']({ dataset: { id: '7', q: 'f4' } });
    div.innerHTML = ui.renderJobInsights(7);
    expect(div.querySelector('#insans7_f4-answer').value).toBe('I built reliable payment services.');
    expect(div.querySelector('[data-act="ins-form-verify"]')).not.toBeNull();

    ui.S.forms.insans7_f4 = { answer: 'I built and ran reliable payment services.' };
    await ui.A['ins-form-verify']({ dataset: { id: '7', q: 'f4' } });
    expect(fetchMock.mock.calls.some(([url]) => url.includes('/save')), 'not saved until the person ticks reviewed').toBe(false);

    ui.S.forms.insans7_f4.reviewed = true;
    await ui.A['ins-form-verify']({ dataset: { id: '7', q: 'f4' } });
    const [url, opt] = fetchMock.mock.calls.find(([u]) => u.includes('/save'));
    expect(url).toBe('/api/jobs/7/insights/application-questions/f4/save');
    expect(JSON.parse(opt.body)).toEqual({ answer: 'I built and ran reliable payment services.', sensitive: false, reviewed: true });
    expect(ui.S.forms.insans7_f4).toBeUndefined();
    expect(ui.D.knowledge).toBeNull();
    div = panel('form', ui.D.insights[7]);
    expect(div.textContent).toContain('From your library');
    expect(div.querySelector('[data-act="ins-form-verify"]')).toBeNull();
  });

  it('posts pasted form questions and opens the library editor unverified', async () => {
    fetchMock.mockImplementation(async url => response(url.includes('/insights/') ? { ...insights(), researching: true, application_form: { from_form: [], likely: [], pending: true } } : []));
    ui.D.insights = { 7: insights() };
    ui.S.forms.insform7 = { questions: 'Why do you want to join Grab?\nok\nDescribe a team you led.' };
    await ui.A['ins-form-add']({ dataset: { id: '7' } });
    const [, opt] = fetchMock.mock.calls.find(([url]) => url === '/api/jobs/7/insights/application-questions');
    expect(JSON.parse(opt.body)).toEqual({ questions: ['Why do you want to join Grab?', 'Describe a team you led.'] });
    expect(ui.S.forms.insform7).toBeUndefined();

    ui.D.insights = { 7: insights({ application_form: { from_form: [], likely: [{ id: 'f4', question: 'Why Grab?', origin: 'PREDICTED', status: 'DRAFTED', answer: 'Draft text' }] } }) };
    await ui.A['ins-form-save']({ dataset: { id: '7', q: 'f4' } });
    expect(ui.S.forms.knnew).toEqual({ question: 'Why Grab?', answer: 'Draft text', verified: false });
    expect(ui.S.screen).toBe('knowledge');
  });

  it('formats ranges in their own currency and period', () => {
    expect(ui.insRange({ low: 90, high: 110, currency: 'SGD', period: 'hour' })).toBe('SGD 90–110 / hour');
    expect(ui.insRange(null)).toBe('Not listed');
  });
});
