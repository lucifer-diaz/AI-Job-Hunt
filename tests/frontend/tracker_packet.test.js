import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render'];
let ui, context, fetchMock;

const application = (over = {}) => ({
  id: 3, job_id: 11, title: 'Lead Software Engineer', company: 'Epsilon', location: 'Bengaluru, Karnataka, India',
  url: 'https://www.linkedin.com/jobs/view/4465457931', fit: 85, stage: 'applied', age: '1d', tag: '', tone: 'muted',
  meta: 'applied 22 Sep · follow-up 27 Sep', recruiter_name: '', recruiter_email: '', notes: '', recruiters: [],
  docs: 'resume v1 · cover letter', resume_id: 7, draft: '', draft_id: null, next: 'Send the follow-up',
  next_date: 'due 27 Sep', timeline: [{ id: 1, kind: 'applied', title: 'Applied', detail: '', at: '2026-09-22T13:49:00' }],
  ...over });

const tracker = (over = {}) => ({
  applications: [application()], stages: [{ id: 'applied', label: 'Applied' }, { id: 'closed', label: 'Closed' }],
  available_stages: [], extra_stages: [],
  stats: { live: 1, replies: 0, due: 1, response_rate: '0%' }, ...over });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  // Opening the packet navigates, which reloads the shell: answer every endpoint it touches.
  fetchMock = vi.fn(async url => ({ ok: true, status: 200, text: async () => JSON.stringify(
    url.includes('/workspace/summary') ? ui.D.summary
      : url.includes('/api/tracker') ? ui.D.tracker
      : url.includes('/api/tailor/queue') ? { items: [], total: 0, total_all: 0, boards: [] }
      : url.includes('/api/tailor/') ? { job: { id: 11, status: 'applied' }, resume: null, min_review_score: 55 }
      : url.includes('/api/knowledge') ? { answers: [] }
      : url.includes('/api/tailor-tasks') ? [] : {}) }));  // the endpoint returns a list
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'tracker'; ui.S.selApp = 3; ui.S.trackerView = 'board';
  ui.S.jobOpen = null; ui.S.tailorJob = null; ui.S.scheduling = false;
  Object.assign(ui.D, { tracker: tracker(), jobModal: null,
    summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
      auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } } });
  ui.render();
});

const view = () => document.getElementById('view');
const packetLinks = () => [...view().querySelectorAll('[data-act="go"][data-screen="tailor"]')];

describe('opening the packet from the tracker', () => {
  it('offers the packet next to the job', () => {
    const buttons = [...view().querySelectorAll('[data-act]')].map(b => b.textContent.trim());
    expect(buttons).toContain('Open packet');
    const link = packetLinks()[0];
    expect(link.dataset.job).toBe('11');
  });

  it('opens what was submitted from the submitted box', () => {
    const submitted = packetLinks().find(a => a.textContent.includes('resume v1'));
    expect(submitted.textContent).toBe('resume v1 · cover letter');
    expect(submitted.dataset.job).toBe('11');
  });

  it('goes to the packet, not back to the tracker', () => {
    ui.A.go(packetLinks()[0]);
    expect(ui.S.screen).toBe('tailor');
    expect(ui.S.tailorJob).toBe(11);
    expect(ui.S.tailorMode).toBe('single');
  });

  it('offers nothing to open when no packet was recorded', () => {
    ui.D.tracker = tracker({ applications: [application({ resume_id: null, docs: 'not recorded' })] });
    ui.render();
    expect([...view().querySelectorAll('[data-act]')].map(b => b.textContent.trim())).not.toContain('Open packet');
    expect(packetLinks()).toHaveLength(0);
    expect(view().textContent).toContain('not recorded');
  });

  it('opens the packet from an applied job’s detail instead of bouncing back', async () => {
    // The job modal's primary button says "Open packet" whenever a packet exists — including after applying.
    ui.D.jobModal = { id: 11, status: 'applied', resume_id: 7, application_id: 3, title: 'Lead Software Engineer',
      company: 'Epsilon', wizard: [], matches: [], gaps: [], missing: [], keywords: [], fit_analysis: {},
      description: 'x', notes: '', rejection: null, board: 'Manual', fit: 85 };
    await ui.A['modal-tailor']({ dataset: { id: '11' } });
    expect(ui.S.screen).toBe('tailor');
    expect(ui.S.tailorJob).toBe(11);
    expect(ui.S.jobOpen).toBeNull();
  });

  it('still sends a job with no packet where it belongs', async () => {
    ui.D.jobModal = { id: 11, status: 'applied', resume_id: null, application_id: 3, wizard: [], matches: [],
      gaps: [], missing: [], keywords: [], fit_analysis: {}, description: 'x', notes: '', rejection: null };
    await ui.A['modal-tailor']({ dataset: { id: '11' } });
    expect(ui.S.screen).toBe('tracker');
    expect(ui.S.selApp).toBe(3);
  });
});

describe('landing on the packet', () => {
  it('scrolls straight to the opened job’s packet once it has loaded, and only once', async () => {
    const scrolled = [];
    window.HTMLElement.prototype.scrollIntoView = function scrollIntoView() { scrolled.push(this.id); };
    ui.D.jobModal = { id: 11, status: 'applied', resume_id: 7, application_id: 3, title: 'Lead Software Engineer',
      company: 'Epsilon', wizard: [], matches: [], gaps: [], missing: [], keywords: [], fit_analysis: {},
      description: 'x', notes: '', rejection: null, board: 'Manual', fit: 85 };
    await ui.A['modal-tailor']({ dataset: { id: '11' } });
    for (let i = 0; i < 20 && !scrolled.length; i += 1) await new Promise(r => setTimeout(r, 0));
    expect(scrolled).toEqual(['tailor-packet']);
    expect(ui.S.tailorFocus).toBe(false);
    ui.render();  // later re-renders (task polling) leave the page where the reader put it
    expect(scrolled).toEqual(['tailor-packet']);
  });
});
