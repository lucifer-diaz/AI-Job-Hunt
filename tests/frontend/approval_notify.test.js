import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'announceApprovals', 'setNotifyApproved', 'refreshApprovals', 'notifyWanted', 'NOTIFY_KEYS'];
let ui, context, store, shown, permission;

class FakeNotification {
  constructor(title, options) { this.title = title; this.options = options; shown.push(this); }
  close() {}
  static get permission() { return permission; }
  static async requestPermission() { permission = 'granted'; return permission; }
}
const summary = (latest, count = 1) => ({ approved: { count, latest_at: latest }, counts: {} });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  store = new Map(); shown = []; permission = 'granted';
  const localStorage = { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
  context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console, localStorage, Notification: FakeNotification });
  for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js', 'job_insights.js']) {
    runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
  }
  runInContext(appSource, context, { filename: resolve('app/static/app.js') });
  ui = runInContext(`({${names.join(',')}})`, context);
});

describe('desktop notification for approvals', () => {
  it('stays quiet on the first visit, then announces each new approval once', () => {
    expect(ui.announceApprovals(summary('2026-10-09T10:00:00'))).toBe(false);  // what is already approved is not news
    expect(shown).toHaveLength(0);
    expect(ui.announceApprovals(summary('2026-10-09T10:00:00'))).toBe(false);  // nothing new
    expect(ui.announceApprovals(summary('2026-10-09T11:30:00', 3))).toBe(true);
    expect(shown).toHaveLength(1);
    expect(shown[0].title).toBe('Job approved');
    expect(shown[0].options.body).toContain('3 approved packets');
    expect(ui.announceApprovals(summary('2026-10-09T11:30:00', 3))).toBe(false);  // a reload repeats nothing
    expect(shown).toHaveLength(1);
  });

  it('does nothing without any approval, and announces the very first one', () => {
    expect(ui.announceApprovals(summary(null, 0))).toBe(false);
    expect(ui.announceApprovals(undefined)).toBe(false);
    expect(ui.announceApprovals(summary(null, 0))).toBe(false);
    expect(ui.announceApprovals(summary('2026-10-09T10:00:00'))).toBe(true);  // an empty account's first approval is news
    expect(shown).toHaveLength(1);
  });

  it('keeps its state in memory when localStorage cannot be used', () => {
    const broken = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
    context.localStorage = broken;
    expect(ui.announceApprovals(summary('2026-10-09T10:00:00'))).toBe(false);
    expect(ui.announceApprovals(summary('2026-10-09T11:00:00'))).toBe(true);
    expect(ui.announceApprovals(summary('2026-10-09T11:00:00'))).toBe(false);
    expect(shown).toHaveLength(1);
  });

  it('respects the switch and the browser permission, falling back to a toast', () => {
    ui.announceApprovals(summary('2026-10-09T10:00:00'));
    store.set(ui.NOTIFY_KEYS.on, 'off');
    expect(ui.announceApprovals(summary('2026-10-09T11:00:00'))).toBe(false);
    expect(shown).toHaveLength(0);
    expect(store.get(ui.NOTIFY_KEYS.seen)).toBe('2026-10-09T11:00:00');  // switching it back on does not replay that one

    store.set(ui.NOTIFY_KEYS.on, 'on'); permission = 'denied';
    expect(ui.announceApprovals(summary('2026-10-09T12:00:00'))).toBe(true);
    expect(shown).toHaveLength(0);
    expect(document.getElementById('toast').textContent).toContain('Job approved');
  });

  it('asks the browser for permission when it is turned on', async () => {
    permission = 'default';
    expect(await ui.setNotifyApproved(true)).toBe('granted');
    expect(store.get(ui.NOTIFY_KEYS.on)).toBe('on');
    await ui.setNotifyApproved(false);
    expect(ui.notifyWanted()).toBe(false);
  });
});
