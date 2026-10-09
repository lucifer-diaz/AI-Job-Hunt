import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'rankProgress', 'rankEta'];
let ui, context, fetchMock;

const task = (id, over = {}) => ({
  id, job_id: id, title: `Associate Product Manager ${id}`, company: 'Kapture Crm', status: 'PENDING',
  reason: 'new', force: false, attempt_count: 0, max_attempts: 3, last_error: null, error_category: null,
  next_attempt_at: null, created_at: '2026-09-21T09:00:00', started_at: null, completed_at: null,
  lease_expires_at: null, retried: false, ranking: null, job_state: 'RANK_PENDING',
  queue_position: id, stage: 'waiting', stage_label: 'Waiting its turn', percent: 5,
  elapsed_seconds: null, eta_seconds: 120, ...over });

const tracker = (over = {}) => ({
  generated_at: '2026-09-21T09:30:00', blocked: null, auto_rank: true,
  counts: { filtering: 0, queued: 4, retry_scheduled: 0, waiting: 168, processing: 1, completed: 12, failed: 1,
            skipped: 0, qualified: 9, archived: 3 },
  progress: { finished: 13, total: 185 }, concurrency: 5, typical_seconds: 60, in_flight: 172,
  worker: { enabled: true, running: false, next_run_at: null, interval_minutes: 2 },
  active: [
    task(1, { status: 'PROCESSING', stage: 'ranking', stage_label: 'Ranking', percent: 72, elapsed_seconds: 40,
              eta_seconds: 20, attempt_count: 1, started_at: '2026-09-21T09:29:20' }),
    task(2, { stage: 'queued', stage_label: 'Queued', percent: 25, eta_seconds: 0 }),
    task(3, { stage: 'waiting', stage_label: 'Waiting its turn', percent: 5, eta_seconds: 60 }),
  ],
  recent: [], ...over });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  fetchMock = vi.fn(async () => ({ ok: true, status: 200, text: async () => '{}' }));
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
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'workers'; ui.S.wk = { tab: 'ranking', range: '7d' }; ui.D.wk = { workers: [{ kind: 'parsing', label: 'JD parsing', counts: { running: 0, queued: 0, retrying: 0, active: 0 } }, { kind: 'recovery', label: 'JD recovery', counts: { running: 0, queued: 0, retrying: 0, active: 0 } }, { kind: 'ranking', label: 'Ranking', description: 'Scores each job', counts: { running: 1, queued: 2, retrying: 0, active: 3 }, scheduler: { enabled: true, interval_minutes: 2 } }, { kind: 'tailoring', label: 'Tailoring', counts: { running: 0, queued: 0, retrying: 0, active: 0 } }], cooldowns: [], active: 3, running: 1 }; ui.S.polling = null;
  Object.assign(ui.D, { ranking: tracker(), setups: [],
    summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
      auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } } });
  ui.render();
});

const view = () => document.getElementById('view');

describe('ranking queue progress', () => {
  it('gives every waiting job a bar, a place in line and an estimate', () => {
    const rows = [...view().querySelectorAll('.lrow')];
    expect(rows).toHaveLength(3);
    const bars = view().querySelectorAll('.lrow .bar i');
    expect(bars).toHaveLength(3);
    expect(bars[0].style.width).toBe('72%');
    expect(bars[1].style.width).toBe('25%');
    expect(rows[0].textContent).toContain('#1 of 172');
    expect(rows[0].textContent).toContain('Ranking');
    expect(rows[0].textContent).toContain('~20s left');
    expect(rows[2].textContent).toContain('#3 of 172');
    expect(rows[2].textContent).toContain('~1m');
  });

  it('says a job is about to start rather than showing a zero countdown', () => {
    expect(ui.rankEta(0)).toBe('starting next');
    expect(ui.rankEta(45)).toBe('~45s');
    expect(ui.rankEta(600)).toBe('~10m');
    expect(ui.rankEta(7200)).toBe('~2h');
    expect(ui.rankEta(null)).toBe('');
    const queued = [...view().querySelectorAll('.lrow')][1];
    expect(queued.textContent).toContain('starting next');
  });

  it('shows no bar and no estimate when the server measured nothing', () => {
    expect(ui.rankProgress({ percent: null }, 172)).toBe('');
    const unmeasured = tracker({ typical_seconds: null });
    unmeasured.active = unmeasured.active.map(t => ({ ...t, eta_seconds: null }));
    ui.D.ranking = unmeasured; ui.render();
    const row = view().querySelector('.lrow');
    expect(row.querySelector('.bar')).not.toBeNull();
    expect(row.textContent).not.toContain('~');
  });

  it('says how many rankings run at once and links to the setting', () => {
    expect(view().textContent).toContain('Up to 5 rankings at a time');
    expect(view().textContent).toContain('about 1m each');
    expect([...view().querySelectorAll('a[href="#workers/settings"]')].map(a => a.textContent)).toContain('change');
  });
});
