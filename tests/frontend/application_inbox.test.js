import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'render', 'renderInboxSettings', 'inboxPoll'];
let ui, context, fetchMock, posted, statusAnswers;

const emailEvent = (over = {}) => ({
  id: 41, application_id: 3, company: 'Epsilon', role: 'Lead Software Engineer', event_type: 'INTERVIEW_INVITATION',
  label: 'Interview invitation', sender: 'Jane Doe <jane@epsilon.io>', subject: 'Technical interview',
  received_at: '2026-09-27T09:00:00', summary: 'Jane invited you to a technical interview.', requires_action: true,
  action_items: [{ action: 'Confirm availability', deadline: '2026-09-30', confidence: 0.9 }], deadline: '2026-09-30T00:00:00',
  confidence: 0.84, needs_review: false, review_status: 'OPEN', stage_decision: 'SUGGESTED', stage_from: 'applied',
  stage_to: 'interviewing', stage_reason: 'confidence 0.84 is below 0.90', interview: null, assessment: null, recruiter: null,
  classification_method: 'LLM', user_corrected: false, gmail_thread_id: '18c2f0a1', gmail_url: 'https://mail.google.com/mail/u/0/#all/18c2f0a1',
  ...over });

const application = (over = {}) => ({
  id: 3, job_id: 11, title: 'Lead Software Engineer', company: 'Epsilon', location: 'Remote', url: '', fit: 85,
  stage: 'applied', age: '1d', tag: '', tone: 'muted', meta: 'applied 22 Sep', recruiter_name: '', recruiter_email: '',
  notes: '', recruiters: [], docs: 'resume v1', resume_id: null, draft: '', draft_id: null, next: 'Send the follow-up',
  next_date: '', timeline: [], email_activity: [emailEvent()], inbox_reply: true, ...over });

const insights = (over = {}) => ({
  sections: { needs_attention: [emailEvent()], upcoming: [], new_updates: [emailEvent()], stage_changes: [], interviews: [emailEvent()],
    assessments: [], offers: [], rejections: [],
    waiting: [{ application_id: 4, company: 'Quiet Co', role: 'Engineer', stage: 'applied', since: '2026-09-10T00:00:00',
      text: 'No new application-related email detected since 10 Sep.' }], new_response_counts: {} },
  digest: { summary: '1 new application update: 1 interview invitation. 1 needs your attention.' },
  status: { configured: true, enabled: true, interval_hours: 24, scope: 'https://www.googleapis.com/auth/gmail.readonly',
    thresholds: { auto_match: 0.9, review: 0.7, stage_auto_update: 0.9 },
    connection: { status: 'CONNECTED', email: 'me@example.com', last_successful_sync_at: '2026-09-28T08:00:00', syncing: false } },
  event_types: [{ id: 'INTERVIEW_INVITATION', label: 'Interview invitation' }, { id: 'DOCUMENT_REQUEST', label: 'Document request' }],
  ...over });

beforeEach(() => {
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"a@b.c"}}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  posted = []; statusAnswers = [];
  fetchMock = vi.fn(async (url, opt = {}) => {
    if (opt.method === 'POST') posted.push({ url, body: opt.body ? JSON.parse(opt.body) : null });
    return { ok: true, status: 200, text: async () => JSON.stringify(
      url.includes('/workspace/summary') ? ui.D.summary
        : url.includes('/api/tracker') ? ui.D.tracker
        : url.includes('/api/application-inbox/insights') ? insights()
        : url.includes('/api/application-inbox/status') ? (statusAnswers.shift() || insights().status)
        : url.includes('/api/application-inbox/sync') ? insights().status : {}) };
  });
  vi.stubGlobal('fetch', fetchMock);
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, Intl, setTimeout, clearTimeout, setInterval, clearInterval, console });
    for (const file of ['monitoring.js', 'orchestration.js', 'workers.js', 'settings.js', 'resume_data.js', 'job_data.js', 'knowledge.js', 'inbox.js', 'guide.js']) {
      runInContext(readFileSync(`app/static/${file}`, 'utf8'), context, { filename: resolve(`app/static/${file}`) });
    }
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'tracker'; ui.S.selApp = 3; ui.S.trackerView = 'board'; ui.S.inboxEdit = null; ui.S.inboxSync = null;
  Object.assign(ui.D, {
    tracker: { applications: [application(), application({ id: 4, company: 'Quiet Co', title: 'Engineer', email_activity: [], inbox_reply: false })],
      stages: [{ id: 'applied', label: 'Applied' }, { id: 'interviewing', label: 'Interviewing' }, { id: 'closed', label: 'Closed' }],
      available_stages: [], extra_stages: [], stats: { live: 2, replies: 0, due: 0, response_rate: '0%' } },
    inbox: insights(), inboxStatus: insights().status, jobModal: null,
    summary: { profile: {}, counts: {}, application_mode: 'manual_approval', model: 'm', total_jobs: 1,
      auto_search: { schedule: 'manual', setups: 0, roles: 0, locations: 0, next_run: null, last_run: null } } });
  ui.render();
});

const view = () => document.getElementById('view');
const click = el => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
const settle = () => new Promise(r => setTimeout(r, 0));

describe('email activity on an application', () => {
  it('shows each email with its type, sender, subject, date, summary, action, deadline and confidence', () => {
    const text = view().textContent;
    for (const bit of ['EMAIL ACTIVITY', 'Interview invitation', 'Jane Doe', 'Technical interview', 'Jane invited you',
      'Confirm availability', 'Action required', 'Deadline', 'confidence 84%', 'Suggests: Interviewing']) {
      expect(text).toContain(bit);
    }
    const gmail = view().querySelector('a[href^="https://mail.google.com"]');
    expect(gmail.getAttribute('rel')).toContain('noopener');
    expect(text).toContain('The employer already replied');
  });

  it('explains why a suggested stage was not applied, and accepts it on request', async () => {
    expect(view().textContent).toContain('Not applied automatically: confidence 0.84 is below 0.90');
    click(view().querySelector('[data-act="inbox-review"][data-action="accept_stage"]'));
    await settle();
    expect(posted).toContainEqual({ url: '/api/application-email-events/41/review', body: { action: 'accept_stage' } });
  });

  it('sends only the fields a person corrected', async () => {
    click(view().querySelector('[data-act="inbox-correct-open"]'));
    view().querySelector('[data-fix="type"]').value = 'DOCUMENT_REQUEST';
    click(view().querySelector('[data-act="inbox-correct-save"]'));
    await settle();
    expect(posted).toContainEqual({ url: '/api/application-email-events/41/correct', body: { event_type: 'DOCUMENT_REQUEST' } });
  });
});

describe('Tracker → Inbox insights', () => {
  it('lists every section and describes silence neutrally', () => {
    ui.S.trackerView = 'inbox'; ui.render();
    const sections = [...view().querySelectorAll('[data-inbox-section]')].map(s => s.dataset.inboxSection);
    expect(sections).toEqual(['needs_attention', 'upcoming', 'new_updates', 'stage_changes', 'interviews', 'assessments', 'offers', 'rejections', 'waiting']);
    const waiting = view().querySelector('[data-inbox-section="waiting"]').textContent;
    expect(waiting).toContain('No new application-related email detected since 10 Sep.');
    expect(waiting.toLowerCase()).not.toContain('rejected');
    expect(view().textContent).toContain('1 new application update');
  });

  it('queues a sync without waiting for it', async () => {
    ui.S.trackerView = 'inbox'; ui.render();
    click(view().querySelector('[data-act="inbox-sync"]'));
    await settle();
    expect(posted.map(p => p.url)).toContain('/api/application-inbox/sync');
  });

  it('asks to connect Gmail when there is no connection', () => {
    ui.D.inbox = insights({ status: { ...insights().status, connection: null } });
    ui.S.trackerView = 'inbox'; ui.render();
    expect(view().querySelector('[data-act="inbox-connect"]')).not.toBeNull();
  });
});

describe('Settings → Gmail inbox', () => {
  it('shows the read-only scope and the connected mailbox', () => {
    const html = ui.renderInboxSettings();
    expect(html).toContain('gmail.readonly');
    expect(html).toContain('me@example.com');
    expect(html).toContain('data-act="inbox-sync"');
    expect(html).toContain('data-act="inbox-disconnect"');
  });

  it('explains when the server has no Google OAuth client', () => {
    ui.D.inboxStatus = { ...insights().status, configured: false };
    expect(ui.renderInboxSettings()).toContain('GOOGLE_OAUTH_CLIENT_ID');
  });
});

describe('sync progress', () => {
  const syncing = (progress, over = {}) => ({ ...insights().status,
    connection: { ...insights().status.connection, syncing: true, progress, ...over } });
  const bar = () => view().querySelector('[role="progressbar"]');

  it('shows the phase and percent of a running sync, and does not offer a second one', () => {
    ui.D.inbox = insights({ status: syncing({ phase: 'reading', label: 'Reading messages · 9 of 20', percent: 45 }) });
    ui.S.trackerView = 'inbox'; ui.render();
    expect(bar().getAttribute('aria-valuenow')).toBe('45');
    expect(bar().textContent).toContain('Reading messages · 9 of 20');
    expect(bar().querySelector('.inbox-progress-fill').style.width).toBe('45%');
    const button = view().querySelector('[data-act="inbox-sync"]');
    expect(button.disabled).toBe(true);
    expect(button.textContent).toBe('Syncing…');
    ui.S.screen = 'overview';  // stop the poller this render started
  });

  it('follows a manual sync from queued to finished, then reloads the insights', async () => {
    ui.S.trackerView = 'inbox'; ui.render();
    expect(bar()).toBeNull();
    click(view().querySelector('[data-act="inbox-sync"]'));
    await settle();
    expect(posted.map(p => p.url)).toContain('/api/application-inbox/sync');
    expect(bar().textContent).toContain('Starting the sync…');  // queued: indeterminate until it reports
    statusAnswers.push(syncing({ phase: 'analysing', label: 'Analysing emails · 2 of 4', percent: 82 }));
    await ui.inboxPoll();
    expect(bar().getAttribute('aria-valuenow')).toBe('82');
    const done = { ...insights().status, last_run: { messages_new: 4, messages_matched: 3 },
      connection: { ...insights().status.connection, syncing: false, last_sync_at: '2026-10-01T09:00:00',
        progress: { phase: 'done', percent: 100, status: 'succeeded' } } };
    statusAnswers.push(done);
    const before = fetchMock.mock.calls.filter(([u]) => u.includes('/insights')).length;
    await ui.inboxPoll();
    await settle();
    expect(bar()).toBeNull();
    expect(document.getElementById('toast').textContent).toBe('Gmail synced · 4 new, 3 matched');
    expect(fetchMock.mock.calls.filter(([u]) => u.includes('/insights')).length).toBeGreaterThan(before);
    ui.S.screen = 'overview';
  });

  it('reports a sync that ended badly', async () => {
    ui.S.trackerView = 'inbox'; ui.render();
    ui.S.inboxSync = { since: null, started: Date.now(), seen: true };
    statusAnswers.push({ ...insights().status, connection: { ...insights().status.connection, syncing: false,
      last_error: 'Gmail is rate limiting this account', progress: { phase: 'done', percent: 100, status: 'rate_limited' } } });
    await ui.inboxPoll();
    expect(document.getElementById('toast').textContent).toBe('Gmail sync rate limited: Gmail is rate limiting this account');
    ui.S.screen = 'overview';
  });
});

describe('correcting an email listed in several sections', () => {
  it('opens one editor, in the copy that was clicked, and saves that copy\'s fields', async () => {
    ui.S.trackerView = 'inbox'; ui.render();
    const copies = () => [...view().querySelectorAll('[data-inbox-event="41"]')];
    expect(copies().length).toBeGreaterThan(1);  // Needs attention, New updates and Interviews all list it
    click(copies()[2].querySelector('[data-act="inbox-correct-open"]'));
    expect(view().querySelectorAll('[data-fix="type"]').length).toBe(1);
    const editor = copies().find(c => c.querySelector('[data-fix="type"]'));
    expect(editor).toBe(copies()[2]);
    expect([...editor.querySelectorAll('[data-fix="type"] option')].map(o => o.value)).toContain('DOCUMENT_REQUEST');
    editor.querySelector('[data-fix="type"]').value = 'DOCUMENT_REQUEST';
    click(editor.querySelector('[data-act="inbox-correct-save"]'));
    await settle();
    expect(posted).toContainEqual({ url: '/api/application-email-events/41/correct', body: { event_type: 'DOCUMENT_REQUEST' } });
    ui.S.screen = 'overview';
  });

  it('offers every event type from the tracker alone, before Inbox insights has loaded', () => {
    ui.D.inbox = undefined;
    ui.D.tracker.email_event_types = [{ id: 'INTERVIEW_INVITATION', label: 'Interview invitation' }, { id: 'OFFER', label: 'Offer' }];
    ui.render();
    click(view().querySelector('[data-act="inbox-correct-open"]'));
    expect([...view().querySelectorAll('[data-fix="type"] option')].map(o => o.value)).toEqual(['INTERVIEW_INVITATION', 'OFFER']);
  });
});
