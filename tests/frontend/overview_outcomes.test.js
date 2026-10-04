import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// The Overview's outcome cards: replies by location and role, how applications ended, fit vs replies, company
// emails from the Application Inbox and reasons for passing on jobs (app/workspace.py overview()).
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui;

const row = (label, applied, replies, interviews = 0) => ({ label, applied, replies, interviews, rejected: 0,
  rate: applied ? `${Math.round(100 * replies / applied)}%` : '—' });
const overview = (extra = {}) => ({
  total: 0, funnel: [], source_perf: [], upcoming: [], weeks: [], weekly_target: 10, role_mix: [], median_fit: null,
  time_in_stage: { triage: null, apply: null, reply: null }, stale_approved: 0, approvals: 0, outcome_days: 90,
  by_location: [], by_role: [], fit_vs_outcome: [], inbox: null, pass_reasons: [],
  outcomes: { total: 0, response_rate: '—', answered: 0, buckets: [] }, ...extra });
const view = () => { document.getElementById('view').innerHTML = ui.renderOverview(); return document.getElementById('view'); };

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script><div id="view"></div><div id="toast"></div>';
  if (!ui) {
    const context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl,
      setTimeout, clearTimeout, setInterval, clearInterval, console, fetch: vi.fn() });
    for (const file of ['monitoring.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, D, renderOverview })', context);
  }
  ui.D.gateway = null;
});

describe('overview outcome cards', () => {
  it('shows replies by location and role, and names the best and the quiet group', () => {
    ui.D.overview = overview({
      by_location: [row('Bengaluru', 6, 3, 1), row('Remote', 5, 0), row('Pune', 1, 1)],
      by_role: [row('Lead AI Engineer', 8, 3, 1), row('Other roles', 4, 1)],
      fit_vs_outcome: [row('90+', 3, 2), row('Under 60', 4, 0)],
      outcomes: { total: 12, response_rate: '42%', answered: 5, buckets: [
        { id: 'awaiting', label: 'Awaiting reply', n: 7, pct: '58%', color: '#c6cad8' },
        { id: 'rejected', label: 'Rejected', n: 5, pct: '42%', color: '#c22f4e' }] },
      inbox: { total: 4, needs_action: 1, groups: [{ id: 'interviews', label: 'Interview invites', n: 2 },
        { id: 'rejections', label: 'Rejections', n: 2 }] },
      pass_reasons: [{ id: 'compensation', label: 'Pay too low', n: 3, pct: '75%' }],
    });
    const root = view();
    const text = root.textContent;
    expect(text).toContain('Where companies reply');
    expect(text).toContain('Bengaluru replies most (50% of 6).');
    expect(text).toContain('Remote: 5 applications, no replies yet.');
    expect(text).toContain('Which roles get replies');
    expect(text).toContain('42% heard back from the company — 5 of 12');
    expect(root.querySelectorAll('.stackbar i')).toHaveLength(2);
    expect(text).toContain('1 email needs a reply or action');
    // It opens the tracker's Inbox insights, where those emails are, not the kanban.
    expect(root.querySelector('[data-view="inbox"]')?.dataset.screen).toBe('tracker');
    expect(text).toContain('Pay too low');
  });

  it('has quiet empty states with no applications and no Gmail connection', () => {
    ui.D.overview = overview();
    const text = view().textContent;
    expect(text).toContain('No applications sent in this window yet.');
    expect(text).toContain('Connect Gmail in Settings');
    expect(text).not.toContain('Why you pass on jobs');
  });
});
