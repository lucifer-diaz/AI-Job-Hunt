/* Application Inbox: Gmail connection (Settings), Inbox Insights (Tracker) and each application's Email activity.
   Loaded before app.js; uses the workspace's shared helpers (esc, attr, btn, pill, api, busy, render, toast). */
'use strict';
const INBOX_SECTIONS = [
  ['needs_attention', 'Needs attention', 'Actions, deadlines, suggested stage changes and emails to confirm'],
  ['upcoming', 'Upcoming', 'Interviews and deadlines in the next 7 days'],
  ['new_updates', 'New updates', 'Recruiting email from the last 24 hours'],
  ['stage_changes', 'Stage changes', 'Moves the inbox made in the last 24 hours'],
  ['interviews', 'Interviews', ''], ['assessments', 'Assessments', ''], ['offers', 'Offers', ''],
  ['rejections', 'Rejections', ''],
  ['waiting', 'Waiting', 'No reply detected yet — that is not a rejection'],
];
const INBOX_STATUS_TEXT = { CONNECTED: ['Connected', 'good'], CONNECTION_REQUIRED: ['Reconnect needed', 'warn'], REVOKED: ['Disconnected', 'muted'], ERROR: ['Error', 'bad'] };
const inboxLabel = type => String(type || '').replace(/_/g, ' ').toLowerCase().replace(/^./, c => c.toUpperCase()).replace('Followup', 'Follow-up');
const inboxDate = iso => iso ? toDate(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' }) : '';
const inboxConfidence = value => `${Math.round((Number(value) || 0) * 100)}%`;
const inboxSender = sender => String(sender || '').replace(/\s*<[^>]*>\s*$/, '') || sender || '';

function inboxEventRow(e, { showCompany = true, gmail = true, where = 'activity' } = {}) {
  // The same email can be listed in several sections at once: the editor opens in the one copy that was clicked.
  const key = `${where}:${e.id}`;
  const stage = e.stage_decision === 'SUGGESTED' && e.stage_to ? pill(`Suggests: ${inboxLabel(e.stage_to)}`, 'warn') : e.stage_decision === 'APPLIED' && e.stage_to ? pill(`Moved to ${inboxLabel(e.stage_to)}`, 'good') : '';
  const flags = [e.requires_action ? pill('Action required', 'warn') : '', e.needs_review ? pill('Needs review', 'warn') : '', e.user_corrected ? pill('Corrected by you', 'brand') : '', stage].join('');
  const actions = (e.action_items || []).map(a => `<li>${esc(a.action)}${a.deadline ? ` <span class="muted">· due ${esc(inboxDate(a.deadline))}</span>` : ''}</li>`).join('');
  const who = showCompany && e.company ? `${esc(e.company)}${e.role ? ' · ' + esc(e.role) : ''}` : '';
  const buttons = [
    e.stage_decision === 'SUGGESTED' && e.review_status === 'OPEN' ? btn('Accept stage', 'inbox-review', { id: e.id, action: 'accept_stage' }, 'sm primary') : '',
    e.review_status === 'OPEN' && (e.needs_review || e.requires_action || e.stage_decision === 'SUGGESTED') ? btn('Mark reviewed', 'inbox-review', { id: e.id, action: 'mark_reviewed' }, 'sm') : '',
    btn('Correct', 'inbox-correct-open', { key }, 'sm ghost'),
    e.review_status === 'OPEN' ? btn('Dismiss', 'inbox-review', { id: e.id, action: 'dismiss' }, 'sm ghost') : '',
    gmail && e.gmail_url ? `<a class="btn sm ghost" href="${attr(e.gmail_url)}" target="_blank" rel="noopener noreferrer">Open in Gmail ↗</a>` : '',
  ].join('');
  return `<div class="inbox-event" data-inbox-event="${e.id}">
    <div class="row between wrap gap8"><div class="small b">${esc(e.label || inboxLabel(e.event_type))}${who ? ` <span class="muted" style="font-weight:400">· ${who}</span>` : ''}</div>
      <span class="xs muted">${esc(fmtDateTime(e.received_at))} · confidence ${inboxConfidence(e.confidence)}</span></div>
    <div class="xs muted mt4">${esc(inboxSender(e.sender))} — ${esc(e.subject || '(no subject)')}</div>
    ${e.application_id ? '' : `<div class="xs mt4" style="color:var(--warn)">Which application is this about? Choose one with Correct.</div>`}
    <div class="small ink2 mt6 pretty">${esc(e.summary || '')}</div>
    ${actions ? `<ul class="small mt6 inbox-actions">${actions}</ul>` : ''}
    ${e.deadline ? `<div class="xs mt4"><b>Deadline</b> ${esc(inboxDate(e.deadline))}</div>` : ''}
    ${e.stage_reason && e.stage_decision === 'SUGGESTED' ? `<div class="xs muted mt4">Not applied automatically: ${esc(e.stage_reason)}</div>` : ''}
    ${flags ? `<div class="row gap6 wrap mt8">${flags}</div>` : ''}
    <div class="row gap6 wrap mt8">${buttons}</div>
    ${S.inboxEdit === key ? inboxCorrectionForm(e, key) : ''}</div>`;
}

function inboxCorrectionForm(e, key) {
  const apps = (D.tracker?.applications || []);
  // Every type an email can be corrected to: from the insights, else the tracker payload (always loaded with it).
  const types = [D.inbox?.event_types, D.tracker?.email_event_types].find(list => list?.length)
    || [{ id: e.event_type, label: inboxLabel(e.event_type) }];
  const uid = key.replace(/[^a-z0-9]/gi, '-');
  const stages = D.tracker?.stages || [];
  return `<div class="box mt10" style="padding:12px"><div class="caps mb8">CORRECT THIS EMAIL</div><div class="col gap8">
    <label class="xs muted" for="inbox-fix-app-${uid}">Application</label>
    <select class="select" id="inbox-fix-app-${uid}" data-fix="app"><option value="" ${e.application_id ? '' : 'selected'}>${e.application_id ? 'Not about any of my applications' : 'Choose an application…'}</option>
      ${apps.map(a => `<option value="${a.id}" ${a.id === e.application_id ? 'selected' : ''}>${esc(a.company)} · ${esc(a.title)}</option>`).join('')}</select>
    <label class="xs muted" for="inbox-fix-type-${uid}">What the email is</label>
    <select class="select" id="inbox-fix-type-${uid}" data-fix="type">${types.map(t => `<option value="${attr(t.id)}" ${t.id === e.event_type ? 'selected' : ''}>${esc(t.label)}</option>`).join('')}</select>
    <label class="xs muted" for="inbox-fix-stage-${uid}">Stage</label>
    <select class="select" id="inbox-fix-stage-${uid}" data-fix="stage"><option value="">Leave the stage as it is</option>${stages.map(s => `<option value="${attr(s.id)}">${esc(s.label)}</option>`).join('')}</select>
    <label class="xs muted" for="inbox-fix-deadline-${uid}">Deadline</label>
    <input class="input" style="margin:0" type="date" id="inbox-fix-deadline-${uid}" data-fix="deadline" value="${attr(e.deadline ? e.deadline.slice(0, 10) : '')}"></div>
    <div class="row gap8 mt10">${btn('Cancel', 'inbox-correct-cancel', {}, 'sm')}${btn(label('inbox-fix', 'Save correction', 'Saving…'), 'inbox-correct-save', { id: e.id }, 'sm primary', isBusy('inbox-fix'))}</div></div>`;
}

/* ───────────── sync progress ───────────── */
// A sync runs in the background; while it does, the status endpoint reports its phase and percent
// (gmail_connections.sync_progress). Poll it, draw the bar, and reload the insights when it finishes.
const INBOX_POLL_MS = 1200;
const INBOX_START_GRACE_MS = 20000;  // how long to wait for a queued sync to start before giving up
let inboxPollTimer = null;
const inboxConn = () => (D.inbox?.status || D.inboxStatus)?.connection || null;
function inboxSyncActive() { return !!(S.inboxSync || inboxConn()?.syncing); }
function inboxSetStatus(status) {
  D.inboxStatus = status;
  if (D.inbox && !D.inbox.error) D.inbox = { ...D.inbox, status };
}
function inboxWatch(since) {
  S.inboxSync = { since: since ?? inboxConn()?.last_sync_at ?? null, started: Date.now(), seen: false };
  inboxSchedulePoll();
}
function inboxSchedulePoll() {
  clearTimeout(inboxPollTimer);
  inboxPollTimer = setTimeout(inboxPoll, INBOX_POLL_MS);
}
async function inboxPoll() {
  inboxPollTimer = null;
  if (!['tracker', 'settings'].includes(S.screen)) { S.inboxSync = null; return; }
  let status;
  try { status = await api('/api/application-inbox/status'); } catch { inboxSchedulePoll(); return; }
  inboxSetStatus(status);
  const conn = status.connection || {}, watch = S.inboxSync;
  if (conn.syncing) { if (watch) watch.seen = true; render(); inboxSchedulePoll(); return; }
  const finished = !watch || watch.seen || (conn.last_sync_at && conn.last_sync_at !== watch.since);
  if (!finished && Date.now() - watch.started < INBOX_START_GRACE_MS) { render(); inboxSchedulePoll(); return; }
  S.inboxSync = null;
  const outcome = conn.progress?.status;
  if (finished && outcome) {
    const run = status.last_run || {};
    const counts = `${run.messages_new || 0} new, ${run.messages_matched || 0} matched`;
    toast(outcome === 'succeeded' ? `Gmail synced · ${counts}`
      : outcome === 'partial' ? `Gmail synced · ${counts} · more mail waiting, the next sync carries on`
      : `Gmail sync ${outcome.replace(/_/g, ' ')}${conn.last_error ? `: ${conn.last_error}` : ''}`, !['succeeded', 'partial'].includes(outcome));
  }
  D.inbox = undefined;
  if (S.screen === 'tracker') await load().catch(() => {});
  render();
}
function inboxProgressBar(conn) {
  if (!inboxSyncActive()) return '';
  const p = conn?.syncing ? conn.progress : null;
  const percent = p && p.phase !== 'done' ? Math.max(2, Math.min(100, p.percent || 0)) : null;
  const text = percent == null ? 'Starting the sync…' : p.label;
  return `<div class="inbox-progress mt12" role="progressbar" aria-label="Gmail sync" aria-valuemin="0" aria-valuemax="100" ${percent == null ? '' : `aria-valuenow="${percent}"`}>
    <div class="row between xs muted" style="margin-bottom:6px"><span>${esc(text)}</span><span class="mono">${percent == null ? '' : `${percent}%`}</span></div>
    <div class="inbox-progress-track"><div class="inbox-progress-fill ${percent == null ? 'indeterminate' : ''}" style="${percent == null ? '' : `width:${percent}%`}"></div></div></div>`;
}

/* ───────────── Tracker → Inbox Insights ───────────── */
function inboxLoad() {
  D.inbox = null;
  return api('/api/application-inbox/insights').then(v => { D.inbox = v; render(); })
    .catch(err => { D.inbox = { error: err.message }; render(); });
}
function renderInboxInsights() {
  if (D.inbox === undefined) { inboxLoad(); return loading(); }
  if (!D.inbox) return loading();
  if (D.inbox.error) return `<section class="card">${empty('Inbox insights could not be loaded', D.inbox.error, btn('Try again', 'inbox-refresh', {}, 'primary'))}</section>`;
  const { sections, digest, status } = D.inbox;
  const conn = status.connection;
  if (conn?.syncing && !inboxPollTimer) inboxWatch();  // a scheduled (or other tab's) sync is running
  if (!status.configured) return `<section class="card">${empty('Gmail is not set up on this server', 'Ask the administrator to add a Google OAuth client (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET).')}</section>`;
  if (!conn || conn.status === 'REVOKED') return `<section class="card">${empty('Connect Gmail to see application updates', 'Read-only access. Only mail about applications you submitted is read.', btn('Connect Gmail', 'inbox-connect', {}, 'primary'))}</section>`;
  const head = `<section class="card pad18"><div class="row between wrap gap10"><div><div class="card-title">Inbox insights</div>
      <div class="small muted mt4">${esc(conn.email)} · ${conn.last_successful_sync_at ? `last synced ${esc(fmtDateTime(conn.last_successful_sync_at))}` : 'not synced yet'}</div>
      ${conn.status === 'CONNECTION_REQUIRED' ? `<div class="small mt4" style="color:var(--warn)">${esc(conn.last_error || 'Gmail needs to be connected again.')}</div>` : ''}</div>
    <div class="row gap8">${conn.status === 'CONNECTION_REQUIRED' ? btn('Reconnect Gmail', 'inbox-connect', {}, 'sm primary') : btn(inboxSyncActive() ? 'Syncing…' : label('inbox-sync', 'Sync now', 'Queuing…'), 'inbox-sync', {}, 'sm primary', isBusy('inbox-sync') || inboxSyncActive())}${btn('Refresh', 'inbox-refresh', {}, 'sm')}</div></div>
    ${inboxProgressBar(conn)}
    ${digest ? `<div class="small ink2 mt10 pretty">${esc(digest.summary)}</div>` : ''}</section>`;
  const body = INBOX_SECTIONS.map(([id, title, hint]) => {
    const items = sections[id] || [];
    let rows;
    if (id === 'waiting') rows = items.map(w => `<div class="inbox-event"><div class="small b">${esc(w.company)} · ${esc(w.role)}</div><div class="xs muted mt4">${esc(w.text)}</div></div>`).join('');
    else if (id === 'upcoming') rows = items.map(u => `<div class="inbox-event"><div class="small b">${esc(u.company || '')}${u.role ? ' · ' + esc(u.role) : ''}</div><div class="small mt4">${esc(u.what)}</div></div>`).join('');
    else if (id === 'stage_changes') rows = items.map(c => `<div class="inbox-event"><div class="small b">${esc(c.company || '')}</div><div class="small mt4">${esc(c.change)}</div><div class="xs muted mt4">${esc(c.summary || '')}</div></div>`).join('');
    else rows = items.map(e => inboxEventRow(e, { where: id })).join('');
    return `<section class="card pad18" data-inbox-section="${id}"><div class="row between"><div class="base b">${title} <span class="mono xs muted">${items.length}</span></div></div>${hint ? `<div class="xs muted mt4">${hint}</div>` : ''}
      <div class="col mt10">${rows || `<div class="small muted">${id === 'new_updates' ? 'No new application-related email detected.' : 'Nothing here.'}</div>`}</div></section>`;
  }).join('');
  return `${head}<div class="inbox-grid">${body}</div>`;
}

/* ───────────── an application's Email activity ───────────── */
function renderEmailActivity(sel) {
  const events = sel.email_activity || [];
  if (!events.length) return '';
  return `<div class="mt16"><div class="caps mb12">EMAIL ACTIVITY</div><div class="col">${events.map(e => inboxEventRow(e, { showCompany: false })).join('')}</div></div>`;
}

/* ───────────── Settings → Gmail ───────────── */
function renderInboxSettings() {
  const st = D.inboxStatus;
  if (st === undefined) { api('/api/application-inbox/status').then(v => { D.inboxStatus = v; render(); }).catch(err => { D.inboxStatus = { error: err.message }; render(); }); return loading(); }
  if (st.error) return `<p class="settings-help">${esc(st.error)}</p>`;
  if (!st.configured) return settingsGroup('Gmail', '', '<p class="settings-help">Gmail is not set up on this server. The administrator needs to add a Google OAuth client (GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET) — see the README, Application Inbox Agent.</p>');
  const conn = st.connection, [text, tone] = INBOX_STATUS_TEXT[conn?.status] || ['Not connected', 'muted'];
  const connected = conn && conn.status === 'CONNECTED';
  if (conn?.syncing && !inboxPollTimer) inboxWatch();
  return settingsGroup('Gmail', 'Read-only access to find replies about applications you submitted. Nothing is sent, labelled or deleted, and unrelated mail is never analysed.',
    `<div class="row wrap gap10">${pill(text, tone)}${conn?.email ? `<span class="small">${esc(conn.email)}</span>` : ''}</div>
    ${conn?.last_error && !connected ? `<p class="settings-help" style="color:var(--warn)">${esc(conn.last_error)}</p>` : ''}
    <p class="settings-help">Permission requested: <code>${esc(st.scope)}</code> (read-only). ${st.enabled ? `Checks every ${esc(st.interval_hours)} hours.` : 'Scheduled checks are off on this server; use Sync now.'}
      ${conn?.last_successful_sync_at ? ` Last synced ${esc(fmtDateTime(conn.last_successful_sync_at))}.` : ''}</p>
    ${connected ? inboxProgressBar(conn) : ''}
    <div class="row gap8 wrap mt10">${connected ? `${btn(inboxSyncActive() ? 'Syncing…' : label('inbox-sync', 'Sync now', 'Queuing…'), 'inbox-sync', {}, 'primary', isBusy('inbox-sync') || inboxSyncActive())}${btn(label('inbox-disconnect', 'Disconnect', 'Disconnecting…'), 'inbox-disconnect', {}, '', isBusy('inbox-disconnect'))}` : btn(conn ? 'Reconnect Gmail' : 'Connect Gmail', 'inbox-connect', {}, 'primary')}</div>
    <p class="settings-help mt10">Emails are linked to an application only when the evidence is strong (at least ${inboxConfidence(st.thresholds.auto_match)}); stages change automatically only for explicit, confident emails (${inboxConfidence(st.thresholds.stage_auto_update)}). Everything else waits in Tracker → Inbox insights for you.</p>`);
}

const INBOX_ACTIONS = {
  'inbox-connect': () => { window.location.href = '/api/gmail/connect'; },
  'inbox-disconnect': () => {
    if (!confirm('Disconnect Gmail? Job Hunt OS forgets the authorization; emails already analysed stay on your timeline.')) return;
    return busy('inbox-disconnect', async () => { D.inboxStatus = await api('/api/gmail/connection', { method: 'DELETE' }); D.inbox = undefined; }, 'Gmail disconnected');
  },
  'inbox-sync': () => {
    if (inboxSyncActive()) return;
    const since = inboxConn()?.last_sync_at ?? null;
    return busy('inbox-sync', async () => {
      inboxSetStatus(await api('/api/application-inbox/sync', { method: 'POST' }));
      inboxWatch(since);
    });
  },
  'inbox-refresh': () => { D.inbox = undefined; D.inboxStatus = undefined; render(); },
  'inbox-review': el => busy('inbox-review', async () => {
    await api(`/api/application-email-events/${el.dataset.id}/review`, { method: 'POST', body: { action: el.dataset.action } });
    D.inbox = undefined; await load();
  }, el.dataset.action === 'accept_stage' ? 'Stage updated' : el.dataset.action === 'dismiss' ? 'Dismissed' : 'Marked reviewed'),
  'inbox-correct-open': el => { S.inboxEdit = S.inboxEdit === el.dataset.key ? null : el.dataset.key; render(); },
  'inbox-correct-cancel': () => { S.inboxEdit = null; render(); },
  'inbox-correct-save': el => {
    const all = [...(D.inbox?.sections ? Object.values(D.inbox.sections).flat() : []), ...(D.tracker?.applications || []).flatMap(a => a.email_activity || [])];
    const current = all.find(e => e.id === Number(el.dataset.id)) || {};
    const form = el.closest('[data-inbox-event]'), field = name => form.querySelector(`[data-fix="${name}"]`).value;
    const appValue = field('app'), type = field('type'), stage = field('stage'), deadline = field('deadline');
    const body = {};
    const appId = appValue ? Number(appValue) : null;
    if (appId !== (current.application_id ?? null) && (appId !== null || current.application_id)) body.application_id = appId;
    if (type && type !== current.event_type) body.event_type = type;
    if (stage) body.stage = stage;
    if ((deadline || null) !== (current.deadline ? current.deadline.slice(0, 10) : null)) body.deadline = deadline ? `${deadline}T00:00:00` : null;
    if (!Object.keys(body).length) { S.inboxEdit = null; render(); return; }
    return busy('inbox-fix', async () => {
      const out = await api(`/api/application-email-events/${el.dataset.id}/correct`, { method: 'POST', body });
      S.inboxEdit = null; D.inbox = undefined; await load();
      // Linking an email to an application analyses it again in the background: show the result when it lands.
      if (out?.reanalysing) setTimeout(() => { D.inbox = undefined; load(); }, 3000);
    }, 'Correction saved — later syncs keep it');
  },
};
