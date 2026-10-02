import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Tailoring works from the Ranker's analysis: the Jobs screen offers it only for ranked jobs.
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
let ui;

const row = (id, fit) => ({ id, title: `Product Manager ${id}`, company: 'Paytm', location: 'Bengaluru, India', fit,
  status: 'discovered', board: 'Naukri', posted: '5d ago' });
const jobs = items => ({ items, page: 1, pages: 1, per_page: 25, total: items.length, total_all: items.length,
  boards: ['Naukri'] });
const view = () => { document.getElementById('view').innerHTML = ui.renderTable(); return document.getElementById('view'); };
const tailorButton = root => [...root.querySelectorAll('[data-act="bulk-to-tailor"]')][0];

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c","is_admin":true}}</script><div id="view"></div><div id="toast"></div>';
  if (!ui) {
    const context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl,
      setTimeout, clearTimeout, setInterval, clearInterval, console, fetch: vi.fn() });
    for (const file of ['monitoring.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext('({ S, D, renderTable })', context);
  }
  ui.S.checked = new Set(); ui.S.rankedIds = new Set(); ui.S.busy = {}; ui.S.selJob = null; ui.D.selDetail = null;
  ui.S.filters = { q: '', min_fit: 0, board: 'all', stage: 'discovered', sort: 'fit', per_page: 25 };
});

describe('Jobs · Bulk tailor needs a ranking', () => {
  it('is disabled when only unranked jobs are selected, and says why', () => {
    ui.D.jobs = jobs([row(1, null), row(2, null)]);
    ui.S.checked = new Set([1]);
    const button = tailorButton(view());
    expect(button.disabled).toBe(true);
    expect(button.textContent.trim()).toBe('Bulk tailor');
    expect(button.parentElement.title).toContain('1 selected job is not ranked yet');
  });

  it('counts only the ranked jobs in a mixed selection', () => {
    ui.D.jobs = jobs([row(1, null), row(2, 78), row(3, 64)]);
    ui.S.checked = new Set([1, 2, 3]);
    const button = tailorButton(view());
    expect(button.disabled).toBe(false);
    expect(button.textContent.trim()).toBe('Bulk tailor 2');
    expect(button.parentElement.title).toContain('1 selected job is not ranked yet');
  });

  it('offers to rank an unranked job instead of tailoring it', () => {
    ui.D.jobs = jobs([row(1, null)]);
    ui.S.selJob = 1;
    ui.D.selDetail = { ...row(1, null), matches: [], gaps: [], missing: [], summary: '' };
    const root = view();
    expect(root.querySelector('[data-act="rank-job"]').textContent.trim()).toBe('Rank first');
    expect(root.querySelector('[data-act="triage"][data-tailor]')).toBeNull();
  });
});

describe('Jobs · date filter and applied jobs', () => {
  it('offers found/posted date windows, a newest-posted sort and a Show applied box', () => {
    ui.D.jobs = { ...jobs([row(1, 80)]), applied_hidden: 3, total_all: 4 };
    const root = view();
    expect([...root.querySelectorAll('#flt-date-field option')].map(o => o.value)).toEqual(['found', 'posted']);
    expect([...root.querySelectorAll('#flt-within option')].map(o => o.textContent))
      .toEqual(['Any time', 'Last 24 hours', 'Last 3 days', 'Last 7 days', 'Last 15 days', 'Last 30 days']);
    expect(root.querySelector('#flt-sort option[value="posted"]').textContent).toBe('Sort: newest posted');
    expect(root.querySelector('#flt-applied').checked).toBe(false);
    expect(root.textContent).toContain('3 applied jobs hidden');
    expect(root.textContent).toContain('no filters applied');
  });
});
