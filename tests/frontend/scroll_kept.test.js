import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Polls re-render every few seconds (ranking tracker, JD recovery, workers). Every scrolled box, and the field being
// typed in, used to jump back to the top and lose its caret on each refresh; render() now keeps them.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui;

const task = id => ({ id, job_id: id, title: `Engineer ${id}`, company: 'Acme', status: 'PENDING', reason: 'new',
  attempt_count: 0, next_attempt_at: null, percent: 10, stage: 'queued', stage_label: 'Queued', queue_position: id });
const ranking = () => ({
  counts: { filtering: 0, queued: 30, processing: 0, retry_scheduled: 0, waiting: 0, no_description: 0, qualified: 0,
    archived: 0, failed: 0, completed: 0, skipped: 0 },
  worker: { enabled: true, interval_minutes: 5 }, active: Array.from({ length: 30 }, (_, i) => task(i + 1)), recent: [],
  progress: { finished: 0, total: 30 }, in_flight: 30, auto_rank: true, blocked: null,
  locations: [['Bengaluru', 20], ['Singapore', 10]], filtered: false });
const list = () => document.querySelector('[data-scroll-key="ranking-active"]');

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script>'
    + '<nav id="nav"></nav><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><header id="top"></header>'
    + '<div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  if (!ui) {
    const context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl,
      setTimeout, clearTimeout, setInterval, clearInterval, console, fetch: vi.fn() });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, D, render, renderTaskStack })', context);
    context.scrollTo = () => {};
  }
  ui.S.screen = 'workers'; ui.S.wk = { tab: 'ranking', range: '7d' }; ui.D.wk = { workers: [{ kind: 'parsing', label: 'JD parsing', counts: { running: 0, queued: 0, retrying: 0, active: 0 } }, { kind: 'recovery', label: 'JD recovery', counts: { running: 0, queued: 0, retrying: 0, active: 0 } }, { kind: 'ranking', label: 'Ranking', description: 'Scores each job', counts: { running: 1, queued: 2, retrying: 0, active: 3 }, scheduler: { enabled: true, interval_minutes: 2 } }, { kind: 'tailoring', label: 'Tailoring', counts: { running: 0, queued: 0, retrying: 0, active: 0 } }], cooldowns: [], active: 3, running: 1 }; ui.S.jobOpen = null; ui.S.busy = {};
  ui.D.ranking = ranking();
});

describe('Ranking tracker · scrolling', () => {
  it('keeps the in-progress list where you scrolled it when the tracker refreshes', () => {
    ui.render();
    list().scrollTop = 900;
    ui.D.ranking = ranking();  // what a poll brings back
    ui.render();
    expect(list().scrollTop).toBe(900);
  });
});

describe('Tailoring in the background · scrolling', () => {
  it('keeps the task list where you scrolled it when the Tailor screen polls', () => {
    ui.D.tasks = Array.from({ length: 20 }, (_, i) => ({ id: i + 1, job_id: i + 1, title: `Engineer ${i + 1}`, company: 'Acme',
      mode: 'tailor', status: i < 2 ? 'running' : 'queued', stage: 'queued', progress: 0, created_at: null }));
    document.getElementById('view').innerHTML = ui.renderTaskStack();
    expect(document.querySelector('[data-scroll-key="tailor-tasks"]')).not.toBeNull();
  });
});

describe('Ranking tracker · filters and removal', () => {
  const view = () => { ui.render(); return document.getElementById('view'); };

  it('offers a location filter and a Remove button on each waiting job', () => {
    const root = view();
    expect([...root.querySelectorAll('#rf-location option')].map(o => o.textContent))
      .toEqual(['All locations', 'Bengaluru (20)', 'Singapore (10)']);
    expect(root.querySelectorAll('[data-act="ranking-remove"]').length).toBe(30);
  });

  it('never offers to remove a job that is being ranked right now', () => {
    ui.D.ranking.active[0].status = 'PROCESSING';
    const removable = [...view().querySelectorAll('[data-act="ranking-remove"]')].map(b => b.dataset.ids);
    expect(removable).not.toContain('1');
  });

  it('offers to remove everything shown once a filter is on', () => {
    ui.D.ranking = { ...ranking(), filtered: true, active: ranking().active.slice(0, 3) };
    const all = [...view().querySelectorAll('[data-act="ranking-remove"][data-confirm]')];
    expect(all).toHaveLength(1);
    expect(all[0].dataset.ids).toBe('1,2,3');
    expect(all[0].textContent).toContain('Remove 3 shown from ranking');
  });

  it('offers to rank a removed job again', () => {
    ui.D.ranking = { ...ranking(), active: [], recent: [{ ...task(9), status: 'SKIPPED', removed: true, percent: null }] };
    const again = [...view().querySelectorAll('[data-act="ranking-retry"]')];
    expect(again.map(b => b.textContent.trim())).toEqual(['Rank again']);
  });
});

describe('Ranking tracker · clearing the finished list', () => {
  const view = () => { ui.render(); return document.getElementById('view'); };

  it('offers Clear finished in the filter bar, and Show cleared once it has been cleared', () => {
    ui.D.ranking = { ...ranking(), active: [], recent: [{ ...task(4), status: 'FAILED', percent: null }] };
    expect(view().querySelector('[data-act="wk-clear"]').textContent).toContain('Clear finished (1)');
    ui.D.ranking = { ...ranking(), active: [], recent: [], finished_cleared_at: '2026-09-27T10:30:00' };
    const root = view();
    expect(root.textContent).toContain('Finished rankings cleared at');
    expect(root.querySelector('[data-act="wk-unclear"]')).not.toBeNull();
    expect(root.querySelector('[data-act="wk-clear"]')).toBeNull();  // nothing finished left to clear
    expect(root.textContent).not.toContain('No ranking activity in the last day');
  });
});

describe('Job detail · scrolling while a poll re-renders', () => {
  const job = { id: 1, title: 'AI Implementation Engineer', company: 'Example Corp', location: 'Mumbai', url: '', source: 'manual',
    board: 'Manual', fit: 42, status: 'archived', discovered_at: '2026-10-18T10:00:00', posted: '1d ago', matches: ['Python'],
    gaps: ['Java'], missing: [], keywords: [], summary: 'Strong match on core AI implementation.', strategy: '', snippet: '',
    needs_description: false, domain_match: 'KEPT', description: Array.from({ length: 120 }, (_, i) => `Line ${i + 1}`).join('\n'),
    fit_analysis: { confidence: 0.7 }, description_state: 'PARSED', description_recovery: { state: 'NOT_NEEDED', usable: true },
    rank_pct: null, wizard: [{ label: 'Discovered', done: true, meta: '1d ago' }, { label: 'Ranked', done: true, meta: 'fit 42' }],
    gap_notes: [{ gap: 'Java' }, { gap: 'Kubernetes' }], notes: '', resume_id: null, rejection: null };
  const modal = () => document.querySelector('.modal');
  const textarea = () => document.getElementById('jobdesc-1');
  const gaps = () => [...document.querySelectorAll('.modal section.card')].find(s => s.textContent.includes('Close the gaps'));

  beforeEach(() => {
    ui.S.screen = 'overview'; ui.D.overview = undefined;
    ui.S.jobOpen = 1; ui.S.jobTab = 'description'; ui.S.descEdit = 1; ui.S.forms = {};
    ui.D.jobModal = { ...job };
  });

  it('keeps the description editor, the modal and the side cards where you scrolled them', () => {
    ui.render();
    textarea().scrollTop = 500; modal().scrollTop = 120; gaps().scrollTop = 40;
    ui.D.jobModal = { ...job, description_recovery: { state: 'NOT_NEEDED', usable: true } };  // what watchRecovery brings back
    ui.render();
    expect(textarea().scrollTop).toBe(500);
    expect(modal().scrollTop).toBe(120);
    expect(gaps().scrollTop).toBe(40);
  });

  it('keeps focus, caret and unsaved typing in the field being edited', () => {
    ui.render();
    const field = textarea();
    field.focus(); field.value = 'Pasted text the input handler has not seen yet'; field.setSelectionRange(6, 10);
    ui.render();
    expect(textarea()).not.toBe(field);  // the markup really was replaced
    expect(document.activeElement).toBe(textarea());
    expect(textarea().value).toBe('Pasted text the input handler has not seen yet');
    expect([textarea().selectionStart, textarea().selectionEnd]).toEqual([6, 10]);
  });

  it('takes a value the render brings over what was typed before it', () => {
    ui.render();
    textarea().focus(); textarea().value = 'typed';
    ui.S.forms = { jobdesc: { 1: 'Saved elsewhere' } };
    ui.render();
    expect(textarea().value).toBe('Saved elsewhere');
  });

  it('never moves focus to whatever button now sits where the clicked one was', () => {
    ui.render();
    const button = document.querySelector('.modal [data-act="modal-triage"]');
    button.focus();
    ui.render();
    expect(document.activeElement).toBe(document.body);
  });
});
