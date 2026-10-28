import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Sending a packet whose review never ran (or failed) to review, from the Tailor screen.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui, context, fetchMock;

const packet = (over = {}) => ({
  job: { id: 11, title: 'AI Implementation Engineer', company: 'Morgan Stanley', status: 'tailored', fit: 80 },
  min_review_score: 68, application_mode: 'manual_approval',
  resume: { id: 7, version: 2, generated: '9d ago', reviewer_score: null, markdown: '# Resume', untailored: false,
    cover_letter: '', cover_words: 0, edits: [], review: [], keywords: [], ats_pct: 58, missing_supported: [],
    length: { target_pages: 2, estimated_pages: 1.8 }, ...over } });

const queueRow = (over = {}) => ({ id: 11, title: 'AI Implementation Engineer', company: 'Morgan Stanley', board: 'linkedin',
  status: 'tailored', fit: 80, has_resume: true, reviewer_score: null, ats_pct: 58, ...over });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  fetchMock = vi.fn(async (url, init) => ({ ok: true, status: 200, text: async () => JSON.stringify(
    url.includes('/api/tailor-tasks') && init?.method === 'POST'
      ? { queued: [{ id: 5, job_id: 11, mode: 'review', status: 'queued', stage: 'queued', progress: 0 }], skipped: [] }
      : url.includes('/api/tailor-tasks') ? [] : {}) }));
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({S, D, A, render})', context);
  }
  context.fetch = fetchMock;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'tailor'; ui.S.tailorMode = 'single'; ui.S.tailorJob = 11;
  ui.S.tailorTab = 'diff'; ui.S.tailorChecked = new Set();
  Object.assign(ui.D, { tailorQueue: [queueRow()], tailorFacets: { total: 1, total_all: 1, boards: [], locations: [] }, packet: packet(), tasks: [],
    summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
      auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } } });
});

const view = () => document.getElementById('view');
const buttons = act => [...view().querySelectorAll(`[data-act="${act}"]`)];

describe('a packet with no review', () => {
  it('offers Send to review instead of Send to approval', () => {
    ui.render();
    expect(buttons('send-review').length).toBeGreaterThan(0);
    expect(view().textContent).not.toContain('Send to approval');
    expect(view().textContent).toContain('has not been reviewed');
  });

  it('queues a review-only task for the job', async () => {
    ui.render();
    await ui.A['send-review'](buttons('send-review')[0]);
    const post = fetchMock.mock.calls.find(([url, init]) => url.includes('/api/tailor-tasks') && init?.method === 'POST');
    expect(JSON.parse(post[1].body)).toMatchObject({ ids: [11], mode: 'review', regenerate: false });
  });

  it('keeps Send to approval once the packet is reviewed', () => {
    ui.D.packet = packet({ reviewer_score: 82, review: [{ tone: 'good', text: 'Clear' }] });
    ui.render();
    expect(buttons('send-review')).toEqual([]);
    expect(view().textContent).toContain('Send to approval');
  });

  it('lets bulk tailor review the selected unreviewed packets', () => {
    ui.S.tailorMode = 'bulk'; ui.S.tailorChecked = new Set([11]);
    ui.render();
    const review = buttons('bulk-tailor').find(b => b.dataset.mode === 'review');
    expect(review.textContent).toContain('Review 1');
    expect(view().textContent).toContain('not reviewed');
  });
});
