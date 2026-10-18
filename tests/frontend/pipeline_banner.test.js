import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// A job held in the workflow (ranker.pipeline_status) shows why, and the one action that puts it back, on the job
// modal and on the approval it would otherwise fail to approve.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'pipelineBanner'];
let ui, context, fetchMock, calls;

const FAILED = { state: 'FAILED', previous_state: 'RANK_PENDING', since: '2026-10-04T18:39:29', stage: 'ranking',
  title: 'Ranking failed', action: 'rank', action_label: 'Put back in ranking', approval_blocked: true,
  message: 'Its last ranking gave up after its attempts.', reason: 'Ranking gave up after 3 attempt(s): EXTERNAL_SERVICE' };
const job = (over = {}) => ({ id: 574, title: 'Software Engineering Lead', company: 'MetaTech', location: 'Remote',
  url: '', source: 'manual', board: 'Manual', fit: 84, status: 'pending_approval', discovered_at: '2026-09-18T09:00:00',
  posted: '3d', matches: [], gaps: [], missing: [], keywords: [], summary: '', snippet: '', agency: false,
  search_run_id: null, application_id: null, application_status: null, stage: null, rejection: null,
  wizard: [], description: 'Lead platform teams.', fit_analysis: {}, gap_notes: [], notes: '', resume_id: 9, ...over });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('scrollTo', vi.fn());
  calls = [];
  const answer = url => url.endsWith('/rank') ? { queued: [574], skipped: [], blocked: null, tasks: [] }
    : url.includes('/api/ranking/tasks') ? { active: 0 }
    : url.includes('/api/approvals/574') ? { job: job(), pipeline: { ...FAILED, title: 'Being ranked', action: null, action_label: null } }
    : url.includes('/api/jobs/574') ? { ...job(), pipeline: { ...FAILED, title: 'Being ranked', action: null, action_label: null } }
    : url.includes('/workspace/summary') ? ui.D.summary : {};
  fetchMock = vi.fn(async (url, opts) => { calls.push([url, opts?.method || 'GET']); return { ok: true, status: 200, text: async () => JSON.stringify(answer(url)) }; });
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
  ui.S.busy = {};
});

describe('pipeline banner', () => {
  it('names the failure, says approval is on hold and offers the way back', () => {
    const html = ui.pipelineBanner({ id: 574, pipeline: FAILED });
    expect(html).toContain('Ranking failed');
    expect(html).toContain('approval is on hold');
    expect(html).toContain('gave up after 3 attempt');
    expect(html).toContain('data-act="pipeline-rank"');
    expect(html).toContain('Put back in ranking');
  });

  it('shows nothing for a job that is moving normally', () => {
    expect(ui.pipelineBanner({ id: 1, pipeline: null })).toBe('');
  });

  it('sends a job without a description to add one instead of ranking it', () => {
    const html = ui.pipelineBanner({ id: 574, pipeline: { ...FAILED, action: 'description', action_label: 'Add description' } });
    expect(html).toContain('data-act="open-job"');
    expect(html).not.toContain('pipeline-rank');
  });

  it('a waiting job has no button', () => {
    const html = ui.pipelineBanner({ id: 574, pipeline: { ...FAILED, title: 'Being ranked', action: null } });
    expect(html).toContain('Being ranked');
    expect(html).not.toContain('<button');
  });

  it('putting it back queues the ranking and refreshes the open job and approval', async () => {
    ui.S.jobOpen = 574; ui.S.approvalJob = 574;
    const el = document.createElement('button');
    el.dataset.id = '574';
    await ui.A['pipeline-rank'](el);
    await new Promise(r => setTimeout(r, 0));
    expect(calls).toContainEqual(['/api/jobs/574/rank', 'POST']);
    expect(calls.some(([u]) => u === '/api/jobs/574')).toBe(true);
    expect(calls.some(([u]) => u === '/api/approvals/574')).toBe(true);
    expect(ui.D.jobModal.pipeline.title).toBe('Being ranked');
    ui.S.jobOpen = null; ui.S.approvalJob = null;
  });
});
