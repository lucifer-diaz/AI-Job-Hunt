/* Job Hunt OS workspace — client for the redesigned UI. Every value comes from the API. */
'use strict';

const BOOT = JSON.parse(document.getElementById('boot').textContent);
const $ = sel => document.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const attr = esc;

/* ───────────── state ───────────── */
const S = {
  screen: 'overview', todayVariant: 'focus', discMode: 'triage',
  filters: { q: '', min_fit: 0, board: 'all', stage: 'all', sort: 'fit', page: 1, per_page: 25 },
  triage: { min_fit: 0, hide_agencies: true, hide_reposts: true, keywords: [] }, triagedThisSession: 0,
  checked: new Set(), selJob: null, jobOpen: null, jobTab: 'description',
  paletteOpen: false, paletteQuery: '', paletteJobs: [],
  kitJob: null, tailorJob: null, tailorTab: 'diff', editing: null,
  approvalJob: null, selApp: null, trackerView: 'board', runSel: null,
  editKnowledge: null, scheduling: false, editingRecruiter: false, manualMissing: null,
  polling: null, busy: {}, forms: {},
};
const D = {};
const SCREENS = ['overview', 'today', 'discovery', 'tailor', 'approvals', 'tracker', 'knowledge', 'settings'];

/* ───────────── helpers ───────────── */
async function api(url, { method = 'GET', body, form } = {}) {
  const opt = { method, headers: {} };
  if (form) opt.body = form;
  else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const res = await fetch(url, opt);
  const text = await res.text();
  let data; try { data = JSON.parse(text); } catch { data = text; }
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === 'string' ? d : d?.message || (Array.isArray(d) ? d.map(x => x.msg).join('; ') : '') || res.statusText;
    const err = new Error(msg); err.detail = d; err.status = res.status; throw err;
  }
  return data;
}
const qs = obj => new URLSearchParams(Object.entries(obj).filter(([, v]) => v !== '' && v !== null && v !== undefined)).toString();
let toastTimer;
function toast(msg, bad = false) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (bad ? ' bad' : ''); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 4000);
}
async function busy(key, fn, okMsg) {
  if (S.busy[key]) return;
  S.busy[key] = true; render();
  try {
    const out = await fn();
    const message = typeof okMsg === 'function' ? okMsg(out) : okMsg;
    if (message) toast(message);
    return out;
  } catch (e) { toast(e.message, true); }
  finally { delete S.busy[key]; render(); }
}
const isBusy = key => !!S.busy[key];
const label = (key, idle, working) => isBusy(key) ? working : idle;
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch { const t = document.createElement('textarea'); t.value = text; document.body.appendChild(t); t.select(); document.execCommand('copy'); t.remove(); }
  toast('Copied to clipboard');
}
const pill = (text, tone = 'muted', extra = '') => `<span class="pill ${tone} ${extra}">${esc(text)}</span>`;
const bar = (pct, color, cls = '') => `<div class="bar ${cls}"><i style="width:${attr(pct)};background:${attr(color)}"></i></div>`;
const fitCls = n => n == null ? 'fit-none' : n >= 80 ? 'fit-high' : n >= 65 ? 'fit-mid' : 'fit-low';
const fitTxt = n => n == null ? '—' : String(Math.round(n));
const initials = s => (s || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase();
const STATUS_TONE = { discovered: 'muted', shortlisted: 'brand', maybe: 'muted', archived: 'muted', dismissed: 'bad', tailored: 'warn', pending_approval: 'warn', rejected: 'bad', approved: 'good', applied: 'good' };
const statusPill = s => pill((s || '').replace(/_/g, ' '), STATUS_TONE[s] || 'muted');
const toDate = iso => iso ? new Date(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z') : null;
const fmtDay = iso => iso ? toDate(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '';
const fmtDateTime = iso => iso ? toDate(iso).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
const loading = () => '<div class="loading">Loading…</div>';
const empty = (title, body, actions = '') => `<div class="empty"><div class="empty-title">${esc(title)}</div><div class="muted small">${esc(body)}</div>${actions ? `<div class="row gap8 center wrap mt14">${actions}</div>` : ''}</div>`;
const btn = (text, act, data = {}, cls = '', disabled = false) =>
  `<button class="btn ${cls}" data-act="${act}" ${Object.entries(data).map(([k, v]) => `data-${k}="${attr(v)}"`).join(' ')} ${disabled ? 'disabled' : ''}>${text}</button>`;

/* form drafts survive re-renders */
const fv = (form, key, fallback) => (S.forms[form] && key in S.forms[form]) ? S.forms[form][key] : fallback;
const setF = (form, key, value) => { (S.forms[form] = S.forms[form] || {})[key] = value; if (form === 'sp') schedulePreview(); };
function inputF(form, key, value, extra = '') { return `<input class="input" id="${form}-${key}" data-form="${form}" data-field="${key}" value="${attr(fv(form, key, value ?? ''))}" ${extra}>`; }
function textareaF(form, key, value, extra = '') { return `<textarea class="textarea" id="${form}-${key}" data-form="${form}" data-field="${key}" ${extra}>${esc(fv(form, key, value ?? ''))}</textarea>`; }
function checkF(form, key, value, text, extra = '') { const on = fv(form, key, !!value); return `<label class="check"><input type="checkbox" id="${form}-${key}" data-form="${form}" data-field="${key}" ${on ? 'checked' : ''} ${extra}>${text}</label>`; }
function selectF(form, key, value, options, extra = '') {
  const cur = String(fv(form, key, value));
  return `<select class="select" id="${form}-${key}" data-form="${form}" data-field="${key}" ${extra}>${options.map(([v, t]) => `<option value="${attr(v)}" ${String(v) === cur ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`;
}
function chips(form, key, values, tone = '') {
  return (fv(form, key, values) || []).map((v, i) => `<span class="chip ${tone}">${esc(v)}<button type="button" aria-label="Remove ${attr(v)}" data-act="tag-remove" data-form="${form}" data-key="${key}" data-index="${i}">×</button></span>`).join('');
}
function tagBox(form, key, values, placeholder, max = 20, tone = '') {
  return `<div class="tagbox" data-tag-box="${key}" data-act="focus-tag" data-target="${form}-${key}">${chips(form, key, values, tone)}<input id="${form}-${key}" data-tag="${key}" data-form="${form}" data-max="${max}" ${LOCATION_KEYS.includes(key) ? 'data-split="newline"' : ''} autocomplete="off" placeholder="${attr(placeholder)}"></div>`;
}
// Locations keep their commas ("New York, NY"), so those inputs split on new lines only.
const LOCATION_KEYS = ['locations', 'preferred_locations'];
function addTags(form, key, raw, max, fallback, commas = !LOCATION_KEYS.includes(key)) {
  const list = [...(fv(form, key, fallback) || [])];
  for (const part of raw.split(commas ? /[,\n]/ : /\n/)) {
    const v = part.trim();
    if (!v || list.some(x => x.toLowerCase() === v.toLowerCase())) continue;
    if (list.length >= max) { toast(`You can add up to ${max}.`, true); break; }
    list.push(v);
  }
  setF(form, key, list);
}
function highlight(text, good, gaps) {
  const terms = [...good.map(t => [t, 'm-good']), ...gaps.map(t => [t, 'm-gap'])].filter(([t]) => t && t.length >= 3);
  const safe = esc(text);
  if (!terms.length) return safe;
  const cls = {}; terms.forEach(([t, c]) => { cls[esc(t).toLowerCase()] = cls[esc(t).toLowerCase()] || c; });
  const re = new RegExp('(' + terms.map(([t]) => esc(t).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).sort((a, b) => b.length - a.length).join('|') + ')', 'gi');
  return safe.replace(re, m => `<mark class="${cls[m.toLowerCase()] || 'm-good'}">${m}</mark>`);
}
const paragraphs = (text, good = [], gaps = []) => (text || '').split(/\n{1,}/).filter(l => l.trim()).map(l => `<p>${highlight(l, good, gaps)}</p>`).join('') || '<p class="faint">No description captured.</p>';
function miniMarkdown(md) {
  const body = (md || '').split('## Source Resume')[0];
  let html = '', inList = false;
  for (const line of body.split('\n')) {
    if (line.startsWith('- ')) { if (!inList) { html += '<ul>'; inList = true; } html += `<li>${esc(line.slice(2))}</li>`; continue; }
    if (inList) { html += '</ul>'; inList = false; }
    if (line.startsWith('### ')) {
      const [title, ...rest] = line.slice(4).split('|').map(part => part.trim());
      html += `<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap;justify-content:space-between"><span><b style="color:var(--ink)">${esc(title)}</b>${rest.length > 1 ? ` <span class="muted">· ${esc(rest.slice(0, -1).join(' · '))}</span>` : ''}</span><span class="muted">${esc(rest.length ? rest[rest.length - 1] : '')}</span></div>`;
    } else if (line.startsWith('# ')) html += `<h3>${esc(line.slice(2))}</h3>`;
    else if (line.startsWith('## ')) html += `<h4>${esc(line.slice(3))}</h4>`;
    else if (line.trim()) html += `<div>${esc(line)}</div>`;
  }
  return html + (inList ? '</ul>' : '');
}

/* ───────────── routing + loading ───────────── */
function setHash() {
  const sub = S.screen === 'discovery' ? '/' + S.discMode : S.screen === 'today' ? '/' + S.todayVariant : '';
  const next = '#' + S.screen + sub;
  if (location.hash !== next) history.replaceState(null, '', next);
}
function readHash() {
  const [screen, sub] = location.hash.replace(/^#/, '').split('/');
  if (SCREENS.includes(screen)) S.screen = screen;
  if (screen === 'discovery' && ['triage', 'table', 'setup'].includes(sub)) S.discMode = sub;
  if (screen === 'today' && ['focus', 'run'].includes(sub)) S.todayVariant = sub;
}
function go(screen, extra = {}) {
  Object.assign(S, { screen, editing: null, scheduling: false, editingRecruiter: false }, extra);
  setHash(); window.scrollTo(0, 0); render(); load();
}

const LOADERS = {
  async overview() { D.overview = await api('/api/workspace/overview'); },
  async today() { D.today = await api('/api/workspace/today'); },
  async discovery() {
    if (S.discMode === 'triage') await loadQueue();
    else if (S.discMode === 'table') await loadTable();
    else await loadSetup();
  },
  async tailor() {
    D.tailorQueue = await api('/api/tailor/queue');
    if (!S.tailorJob || !D.tailorQueue.some(j => j.id === S.tailorJob)) {
      S.tailorJob = (D.tailorQueue.find(j => j.has_resume) || D.tailorQueue[0] || {}).id || S.tailorJob;
    }
    D.packet = S.tailorJob ? await api(`/api/tailor/${S.tailorJob}`) : null;
    if (!D.knowledge) D.knowledge = await api('/api/knowledge');
  },
  async approvals() {
    D.approvals = await api('/api/approvals');
    if (!S.approvalJob || !D.approvals.some(j => j.id === S.approvalJob)) S.approvalJob = (D.approvals[0] || {}).id || null;
    D.approval = S.approvalJob ? await api(`/api/approvals/${S.approvalJob}`) : null;
  },
  async tracker() {
    D.tracker = await api('/api/tracker');
    const apps = D.tracker.applications;
    if (!apps.some(a => a.id === S.selApp)) S.selApp = (apps[0] || {}).id || null;
  },
  async knowledge() { [D.knowledge, D.evidence] = await Promise.all([api('/api/knowledge'), api('/api/evidence')]); },
  async settings() {
    [D.profile, D.settings, D.prefs, D.resumes] = await Promise.all([
      api('/api/profile'), api('/api/settings'), api('/api/preferences'), api('/api/profile/resumes')]);
  },
};
async function loadQueue() {
  const t = S.triage;
  D.queue = await api('/api/jobs/search?' + qs({ queue: true, per_page: 60, min_fit: t.min_fit || '', hide_agencies: t.hide_agencies, hide_reposts: t.hide_reposts, keywords: t.keywords.join(',') }));
  D.queueTotal = D.queue.total;
  if (!D.searchProfile) D.searchProfile = await api('/api/search/profile');
  if (!D.runs) D.runs = await api('/api/search/apify/runs');
}
async function loadTable() {
  const f = S.filters;
  D.jobs = await api('/api/jobs/search?' + qs({ ...f, min_fit: f.min_fit || '' }));
  if (!D.jobs.items.some(j => j.id === S.selJob)) S.selJob = (D.jobs.items[0] || {}).id || null;
  D.selDetail = S.selJob ? await api(`/api/jobs/${S.selJob}`) : null;
}
async function loadSetup() {
  [D.searchProfile, D.runs, D.actors] = await Promise.all([api('/api/search/profile'), api('/api/search/apify/runs'), api('/api/search/actors')]);
  schedulePreview();
  if (!D.runs.some(r => r.id === S.runSel)) S.runSel = (D.runs[0] || {}).id || null;
  D.runDetail = S.runSel ? await api(`/api/search/apify/runs/${S.runSel}/detail`) : null;
}
let loadSeq = 0;
async function load() {
  const seq = ++loadSeq;
  try {
    await Promise.all([LOADERS[S.screen](), api('/api/workspace/summary').then(s => { D.summary = s; })]);
  } catch (e) { toast(e.message, true); }
  if (seq === loadSeq) render();
}

/* ───────────── shell ───────────── */
const NAV = [['overview', 'Overview', null], ['today', 'Today', 'today'], ['discovery', 'Discovery', 'discovery'], ['tailor', 'Tailor', 'tailor'], ['approvals', 'Approvals', 'approvals'], ['tracker', 'Tracker', 'tracker']];
const NAV2 = [['knowledge', 'Knowledge bank'], ['settings', 'Settings & profile']];
function renderNav() {
  const c = D.summary?.counts || {};
  const item = ([id, text, key]) => `<a class="nav-btn ${S.screen === id ? 'on' : ''}" href="#${id}" data-act="go" data-screen="${id}"><span>${text}</span>${key ? `<span class="nav-count">${c[key] ?? '·'}</span>` : `<span class="nav-count">—</span>`}</a>`;
  return `<div class="nav-group"><div class="nav-caps">WORKSPACE</div>${NAV.map(item).join('')}</div>
    <div class="nav-group"><div class="nav-caps">SETUP</div>${NAV2.map(([id, text]) => `<a class="nav-btn ${S.screen === id ? 'on' : ''}" href="#${id}" data-act="go" data-screen="${id}"><span>${text}</span></a>`).join('')}</div>`;
}
function renderSideFoot() {
  const s = D.summary; if (!s) return '';
  const a = s.auto_search;
  const on = a.schedule === 'daily';
  const last = a.last_run ? `Last run ${fmtDay(a.last_run)} · ${a.last_run_status.toLowerCase()}.` : 'No searches have run yet.';
  return `<div class="side-card"><div class="side-card-title"><span class="dot" style="background:${on ? '#4ade80' : '#6a7388'}"></span>Auto-search ${on ? 'on' : 'off'}</div>
    <div class="side-card-body">${on ? `${a.roles} role${a.roles === 1 ? '' : 's'} × ${Math.max(1, a.locations)} location${a.locations === 1 ? '' : 's'} run daily via the cron job.` : 'Searches run when you start them.'} ${esc(last)}</div></div>`;
}
function screenMeta() {
  const s = D.summary, c = s?.counts || {};
  const now = new Date();
  return {
    overview: ['Overview', D.overview ? `The whole hunt on one screen · ${D.overview.total} discovered in 30 days, ${c.tracker ?? 0} live` : 'The whole hunt on one screen'],
    today: ['Today', `${now.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })} · everything that needs you, in order`],
    discovery: ['Discovery', `${s?.total_jobs ?? 0} in pipeline · ${c.discovery ?? 0} waiting for triage`],
    tailor: ['Tailor & review', 'AI drafts, you approve every claim'],
    approvals: ['Approvals', 'Nothing leaves this queue without your sign-off'],
    tracker: ['Tracker', D.tracker ? `${D.tracker.stats.live} live applications · ${D.tracker.stats.due} follow-ups due` : 'Every submitted application'],
    knowledge: ['Knowledge bank', 'Answer once, reuse in every ATS form'],
    settings: ['Settings & profile', 'Models, cadence and what tailoring may cite'],
  }[S.screen];
}
function renderTop() {
  const [title, sub] = screenMeta();
  let seg = '';
  if (S.screen === 'today') seg = [['focus', 'Command centre'], ['run', 'Run sheet']].map(([id, t]) => `<button class="${S.todayVariant === id ? 'on' : ''}" data-act="variant" data-kind="today" data-id="${id}">${t}</button>`).join('');
  if (S.screen === 'discovery') seg = [['triage', 'Triage queue'], ['table', 'Table + detail'], ['setup', 'Search setup']].map(([id, t]) => `<button class="${S.discMode === id ? 'on' : ''}" data-act="variant" data-kind="discovery" data-id="${id}">${t}</button>`).join('');
  return `<div class="top-row"><div><h1>${esc(title)}</h1><div class="sub">${esc(sub)}</div></div>
    <div class="row gap8 wrap">${seg ? `<div class="seg">${seg}</div>` : ''}
      <button class="btn" data-act="palette">Search · ⌘K</button>
      <button class="btn primary" data-act="go" data-screen="discovery" data-mode="setup">New search</button></div></div>`;
}

/* ───────────── overview ───────────── */
function renderOverview() {
  const o = D.overview; if (!o) return loading();
  const perf = o.source_perf;
  const withApplied = perf.filter(b => b.applied);
  const best = [...withApplied].sort((a, b) => parseFloat(b.rate) - parseFloat(a.rate))[0];
  const dead = perf.find(b => b.found >= 10 && b.applied >= 2 && b.replies === 0);
  const insight = !perf.length ? 'Run a search to see which boards pay off.' : best
    ? `${best.board} replies ${best.rate} of the time.${dead ? ` ${dead.board} has returned ${dead.found} jobs and zero replies — worth pausing.` : ''}`
    : 'No applications yet — reply rates appear once you apply.';
  const t = o.time_in_stage;
  const days = v => v == null ? '—' : `${v} days`;
  const bottleneck = o.stale_approved ? `${o.stale_approved} approved packet${o.stale_approved === 1 ? ' has' : 's have'} sat unsubmitted for over 2 days.`
    : o.approvals ? `${o.approvals} packet${o.approvals === 1 ? ' is' : 's are'} waiting for your approval.` : 'Nothing is stuck right now.';
  return `<div class="stack">
  <section class="card"><div class="row base between wrap mb16"><div><div class="card-title">Pipeline health</div><div class="card-sub">Where ${o.total} discovered job${o.total === 1 ? '' : 's'} actually ended up. Click a stage to work it.</div></div><span class="small muted">last 30 days</span></div>
    <div class="row wrap gap10">${o.funnel.map(f => `<button class="stage-btn" data-act="go" data-screen="${f.screen}"><div class="stage-label">${esc(f.label)}</div><div class="mono big">${f.n}</div>${bar(f.pct, f.color)}<div class="tiny faint mt6">${esc(f.conv) || '&nbsp;'}</div></button>`).join('')}</div></section>
  <div class="split gap16">
    <section class="card" style="flex:1 1 380px;min-width:0"><div class="card-title mb4">Where each board earns its keep</div><div class="card-sub mb14">Reply rate is what matters — not how many jobs a board returns.</div>
      <div class="tgrid boards thead"><span>BOARD</span><span class="tr">FOUND</span><span class="tr">APPLIED</span><span class="tr">REPLIES</span><span class="tr">REPLY RATE</span></div>
      ${perf.length ? perf.map(b => `<div class="tgrid boards trow"><span class="b base">${esc(b.board)}</span><span class="tr mono small muted">${b.found}</span><span class="tr mono small muted">${b.applied}</span><span class="tr mono small muted">${b.replies}</span><span class="tr mono base bb">${esc(b.rate)}</span></div>`).join('') : `<div class="small muted mt12">No jobs discovered in the last 30 days.</div>`}
      <div class="small muted mt12">${esc(insight)}</div></section>
    <section class="card" style="flex:1 1 280px;min-width:0"><div class="card-title mb14">Next 10 days</div>
      <div class="col gap9">${o.upcoming.length ? o.upcoming.map(u => `<div class="box row gap12"><span class="mono small b muted none">${fmtDay(u.date)}</span><span class="small grow pretty" style="font-weight:500">${esc(u.label)}</span>${pill(u.kind, u.kind === 'interview' ? 'good' : 'warn', 'none')}</div>`).join('') : '<div class="small muted">No follow-ups or interviews scheduled.</div>'}</div>
      <button class="btn block mt12" data-act="go" data-screen="tracker">Open tracker</button></section>
  </div>
  <div class="split gap16">
    <section class="card" style="flex:1 1 300px;min-width:0"><div class="card-title mb4">Applications per week</div><div class="card-sub mb16">Target is ${o.weekly_target} · six-week trend</div>
      <div class="vbars">${o.weeks.map(w => `<div class="vbar" title="${w.n} applied"><span class="tiny faint tc">${w.n || ''}</span><i style="height:${w.pct}"></i><div class="tiny faint tc mt6">${esc(w.label)}</div></div>`).join('')}</div></section>
    <section class="card" style="flex:1 1 280px;min-width:0"><div class="card-title mb14">What the searches are returning</div>
      <div class="col gap12">${o.role_mix.length ? o.role_mix.map((r, i) => `<div><div class="row between small mb4"><span style="font-weight:500">${esc(r.label)}</span><span class="mono muted">${r.n}</span></div>${bar(r.pct, ['#5b4ae8', '#a79bf0', '#c6cad8', '#e0a136'][i], 'thick')}</div>`).join('') : '<div class="small muted">Nothing discovered yet.</div>'}</div>
      <div class="small muted mt14" style="padding-top:14px;border-top:1px solid var(--line2)">Median fit of new jobs: <b>${o.median_fit ?? '—'}</b>.${o.median_fit != null && o.median_fit < 60 ? ' Tighten the search profile if that keeps falling.' : ''}</div></section>
    <section class="card dark" style="flex:1 1 260px;min-width:0"><div class="card-title mb14" style="color:#cfd5e4">Time in stage</div>
      <div class="col gap12">
        <div><div class="small" style="color:#8a92a8">Discovered → triaged</div><div class="mono bb" style="font-size:19px">${days(t.triage)}</div></div>
        <div><div class="small" style="color:#8a92a8">Shortlisted → applied</div><div class="mono bb" style="font-size:19px">${days(t.apply)}</div></div>
        <div><div class="small" style="color:#8a92a8">Applied → first reply</div><div class="mono bb" style="font-size:19px">${days(t.reply)}</div></div></div>
      <div class="small mt16" style="color:#8a92a8;line-height:1.5;padding-top:14px;border-top:1px solid #252c42">${esc(bottleneck)}</div>
      <button class="btn primary block mt12" data-act="go" data-screen="approvals">Clear the approval queue</button></section>
  </div></div>`;
}

/* ───────────── today ───────────── */
const ACTION_META = {
  approve: { tag: 'APPROVE', tone: 'warn', cta: 'Review', av: '' },
  tailor: { tag: 'TAILOR', tone: 'brand', cta: 'Tailor', av: 'good' },
  follow_up: { tag: 'FOLLOW UP', tone: 'good', cta: 'Open', av: 'warn' },
};
function renderToday() {
  const t = D.today; if (!t) return loading();
  if (S.todayVariant === 'run') return renderRunSheet(t);
  const minutes = t.actions.slice(0, 3).reduce((a, x) => a + x.minutes, 0);
  const w = t.week;
  return `<div class="split">
  <div class="c-main stack" style="gap:18px">
    <section class="card flush"><div class="card-head row between"><div><div class="card-title">Do this next</div><div class="card-sub">Ordered by stage, then fit.${t.actions.length ? ` The top ${Math.min(3, t.actions.length)} take ≈ ${minutes} minutes.` : ''}</div></div>${pill('ranked', 'brand')}</div>
      ${t.actions.length ? t.actions.map(a => { const m = ACTION_META[a.kind]; return `<div class="lrow" style="padding:15px 18px;gap:14px"><div class="avatar ${m.av}">${esc(initials(a.company))}</div>
        <div class="grow"><div class="row gap8 wrap"><span class="b" style="font-size:14px">${esc(a.company)}</span>${pill(m.tag, m.tone)}</div><div class="small muted mt4 pretty">${esc(a.title)} — ${esc(a.reason)}</div></div>
        <div class="tr none"><div class="mono bb ${fitCls(a.fit)}" style="font-size:19px">${fitTxt(a.fit)}</div><div class="caps-sm faint">FIT</div></div>
        ${btn(m.cta, 'do-action', { kind: a.kind, job: a.job_id, app: a.application_id || '' }, 'primary none')}</div>`; }).join('')
        : empty('Nothing needs you right now', 'Shortlist jobs in Discovery and they will show up here.')}
      <button class="linkbtn" data-act="go" data-screen="discovery" data-mode="triage">${t.pending_triage ? `Triage ${t.pending_triage} new job${t.pending_triage === 1 ? '' : 's'} →` : 'Start a new search →'}</button></section>
    <section class="card pad18"><div class="row base between mb14"><div class="card-title">Follow-ups due</div><a href="#tracker" data-act="go" data-screen="tracker">Open tracker</a></div>
      <div class="col gap9">${t.followups.length ? t.followups.map(f => {
        const c = { overdue: ['#c22f4e', '#fffafb', 'var(--bad)'], today: ['#e0a136', '#fffdf9', 'var(--warn)'], soon: ['#c6cad8', '#fff', 'var(--muted)'] }[f.tone];
        return `<div class="box row gap12" style="background:${c[1]};padding:12px 14px"><div style="width:3px;align-self:stretch;border-radius:99px;background:${c[0]}"></div>
        <div class="grow"><div class="b" style="font-size:13.5px">${esc(f.company)} · ${esc(f.role)}</div><div class="small muted mt4">${esc(f.detail)}</div></div>
        <span class="xs b none" style="color:${c[2]}">${esc(f.due)}</span>${btn(label('nudge' + f.application_id, 'Draft nudge', 'Drafting…'), 'draft-nudge', { app: f.application_id }, 'sm none', isBusy('nudge' + f.application_id))}</div>`; }).join('') : '<div class="small muted">No follow-ups due in the next 3 days.</div>'}</div></section>
  </div>
  <div class="c-side stack" style="gap:18px">
    <section class="card dark pad18"><div class="base b" style="color:#cfd5e4">This week</div>
      <div class="row gap10" style="align-items:flex-end;margin:10px 0 4px"><span class="mono bb" style="font-size:40px;line-height:1">${w.applied}</span><span class="base" style="color:#8a92a8;padding-bottom:6px">of ${w.target} applications</span></div>
      <div class="bar fat" style="margin:12px 0 14px"><i style="width:${Math.min(100, 100 * w.applied / w.target)}%;background:var(--brand)"></i></div>
      <div class="grid-auto" style="grid-template-columns:repeat(3,1fr);gap:10px">
        <div><div class="mono bb" style="font-size:17px">${w.replies}</div><div class="tiny" style="color:#8a92a8">Replies</div></div>
        <div><div class="mono bb" style="font-size:17px">${w.interviews}</div><div class="tiny" style="color:#8a92a8">Interviews</div></div>
        <div><div class="mono bb" style="font-size:17px">${esc(w.response_rate)}</div><div class="tiny" style="color:#8a92a8">Response</div></div></div></section>
    <section class="card pad18"><div class="card-title mb4">Funnel</div><div class="small muted mb14">Last 30 days</div>
      <div class="col gap9">${t.funnel.map(f => `<div><div class="row between small mb4"><span style="font-weight:500">${esc(f.label)}</span><span class="mono muted">${f.n}</span></div>${bar(f.pct, f.color, 'thick')}</div>`).join('')}</div></section>
    <section class="card pad18"><div class="card-title mb12">Needs your attention</div><div class="col gap9">
      <button class="box warn row gap12" style="cursor:pointer;text-align:left;width:100%" data-act="go" data-screen="approvals"><span class="mono bb" style="font-size:18px;color:var(--warn)">${t.approvals}</span><span class="small" style="color:var(--warn-deep);line-height:1.4">tailored packet${t.approvals === 1 ? '' : 's'} waiting for your approval</span></button>
      <button class="box row gap12" style="cursor:pointer;text-align:left;width:100%" data-act="go" data-screen="knowledge"><span class="mono bb muted" style="font-size:18px">${t.unverified_answers}</span><span class="small muted" style="line-height:1.4">ATS answers unverified — verify once, reuse everywhere</span></button>
    </div></section>
  </div></div>`;
}
function renderRunSheet(t) {
  const now = new Date();
  const triageDone = t.pending_triage === 0 && t.triaged_today > 0;
  const items = [{ done: triageDone, title: t.pending_triage ? `Triage ${t.pending_triage} new job${t.pending_triage === 1 ? '' : 's'}` : 'Triage the new jobs', sub: triageDone ? `Done · ${t.triaged_today} triaged today` : t.pending_triage ? 'Shortlist, park or skip each one' : 'Queue is empty — run a search to fill it', minutes: Math.max(2, Math.ceil(t.pending_triage * 0.3)), cta: triageDone ? 'Review' : 'Start', act: 'go-triage' }];
  t.actions.slice(0, 3).forEach(a => items.push({ done: false, title: `${ACTION_META[a.kind].cta === 'Tailor' ? 'Tailor' : a.kind === 'approve' ? 'Approve' : 'Nudge'} ${a.company}`, sub: a.reason, minutes: a.minutes, cta: ACTION_META[a.kind].cta, act: 'do-action', data: { kind: a.kind, job: a.job_id, app: a.application_id || '' } }));
  const open = items.filter(i => !i.done);
  const total = open.reduce((a, i) => a + i.minutes, 0);
  let n = 0;
  return `<div class="col gap14" style="max-width:820px;margin:0 auto">
    <div class="card" style="padding:22px 24px"><div class="caps" style="letter-spacing:.08em;color:var(--faint)">${esc(now.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'short' }).toUpperCase())} · RUN SHEET</div>
      <div class="pretty" style="font-size:26px;font-weight:600;letter-spacing:-.02em;margin:8px 0 6px">${open.length ? `${['Zero', 'One', 'Two', 'Three', 'Four', 'Five'][open.length]} thing${open.length === 1 ? '' : 's'}, then you're done for the day.` : "You're done for the day."}</div>
      <div class="muted" style="font-size:13.5px">${open.length ? `Estimated ${total} minutes. Nothing else on this screen needs you.` : 'Come back tomorrow, or start a new search.'}</div></div>
    ${items.map(i => { const first = !i.done && n++ === 0; return `<div class="card row gap16" style="padding:18px 20px;${first ? 'border-color:var(--brand-line)' : ''}${i.done ? ';background:#fbfcfe' : ''}">
      <div style="width:26px;height:26px;flex:none;border-radius:99px;border:2px solid ${i.done ? '#0f9c86' : first ? '#5b4ae8' : '#9aa1b2'};display:grid;place-items:center;font-size:12px;font-weight:700;color:${i.done ? '#0f9c86' : first ? '#5b4ae8' : '#9aa1b2'}">${i.done ? '✓' : n}</div>
      <div class="grow"><div class="b" style="font-size:15px;${i.done ? 'text-decoration:line-through' : ''}">${esc(i.title)}</div><div class="small muted mt4 pretty">${esc(i.sub)}</div></div>
      <span class="xs faint mono none">${i.minutes} min</span>${btn(i.cta, i.act, i.data || {}, first ? 'primary none' : 'none')}</div>`; }).join('')}
    <div class="card row gap14 wrap" style="padding:18px 20px"><div class="grow"><div class="b" style="font-size:13.5px">Weekly pace</div><div class="small muted mt4">${t.week.applied} of ${t.week.target} applications · ${esc(t.week.response_rate)} response rate</div></div>
      <div class="row" style="gap:5px">${t.week.days.map(d => { const max = Math.max(2, ...t.week.days.map(x => x.n)); return `<div style="width:26px" class="tc"><div style="height:34px;border-radius:6px;background:#f0f1f7;display:flex;align-items:flex-end;overflow:hidden"><div style="width:100%;height:${100 * d.n / max}%;background:${d.future ? '#a79bf0' : '#5b4ae8'}"></div></div><div class="tiny faint mt4">${d.label}</div></div>`; }).join('')}</div></div>
  </div>`;
}

/* ───────────── discovery ───────────── */
function renderDiscovery() {
  const banner = S.polling ? `<div class="banner mb14"><span class="dot" style="background:var(--brand)"></span><span class="grow">${esc(S.polling.text)}</span></div>` : '';
  if (S.discMode === 'table') return banner + renderTable();
  if (S.discMode === 'setup') return banner + renderSetup();
  return banner + renderTriage();
}
function fitSummary(j) {
  return `<div class="row wrap gap10 mt16">
    <div class="box good grow" style="min-width:220px"><div class="xs b mb4" style="color:var(--good)">MATCHED · ${j.matches.length}</div><div class="small ink2" style="line-height:1.5">${j.matches.length ? esc(j.matches.join(', ')) : 'Rank this job to see matches'}</div></div>
    <div class="box warn grow" style="min-width:220px"><div class="xs b mb4" style="color:var(--warn)">GAPS · ${j.gaps.length + j.missing.length}</div><div class="small" style="color:var(--warn-deep);line-height:1.5">${[...j.missing, ...j.gaps].length ? esc([...j.missing, ...j.gaps].join(' · ')) : j.fit == null ? '—' : 'No gaps flagged'}</div></div></div>`;
}
function renderTriage() {
  const q = D.queue; if (!q) return loading();
  const job = q.items[0];
  const done = S.triagedThisSession;
  const total = q.total + done;
  const sp = D.searchProfile?.profile;
  const side = `<div class="c-narrow col gap14">
    <section class="card pad16"><div class="card-title sm mb10">Up next</div><div class="col gap4">${q.items.slice(1, 6).map((u, i) => `<div class="row gap10" style="padding:9px;border-radius:9px;cursor:pointer;background:${i === 0 ? 'var(--softer)' : 'transparent'}" data-act="open-job" data-id="${u.id}"><span class="mono bb ${fitCls(u.fit)}" style="width:24px">${fitTxt(u.fit)}</span><div class="grow"><div class="small ellipsis" style="font-weight:500">${esc(u.title)}</div><div class="tiny muted ellipsis">${esc(u.company)}</div></div></div>`).join('') || '<div class="small muted">Nothing else in the queue.</div>'}</div></section>
    <section class="card pad16"><div class="card-title sm mb4">Filters for this batch</div><div class="small muted mb12">Narrowing re-ranks the queue instantly.</div>
      <div class="col gap12"><label class="field strong">Minimum fit · <span class="mono">${S.triage.min_fit}</span><input type="range" min="0" max="100" step="5" id="triage-min" data-triage="min_fit" value="${S.triage.min_fit}"></label>
        <div><div class="xs muted mb4">Must mention (use / for either)</div>${tagBox('triage', 'keywords', S.triage.keywords, 'Hybrid, Lead/Principal…', 6)}</div>
        <label class="check"><input type="checkbox" data-triage="hide_agencies" ${S.triage.hide_agencies ? 'checked' : ''}>Hide staffing agencies</label>
        <label class="check"><input type="checkbox" data-triage="hide_reposts" ${S.triage.hide_reposts ? 'checked' : ''}>Hide reposts I've seen</label>
        ${q.items.some(i => i.fit == null) ? btn(label('rankBatch', 'Rank every unranked job', 'Ranking…'), 'rank-batch', {}, 'soft block', isBusy('rankBatch')) : ''}</div></section>
    <section class="card pad16"><div class="row between mb10"><div class="card-title sm">Saved search</div>${sp ? pill(sp.schedule === 'daily' ? 'daily' : 'manual', sp.schedule === 'daily' ? 'good' : 'muted') : ''}</div>
      ${sp ? `<div class="box"><div class="small b">${esc(sp.roles.join(' · ') || 'No roles yet')}</div><div class="xs muted mt4">${esc(sp.locations.join(' · ') || 'Any location')} · ${sp.sites.length} board${sp.sites.length === 1 ? '' : 's'}${sp.auto_rank ? ' · auto-rank on' : ''}</div></div>` : ''}
      <div class="col gap6 mt10">${(D.runs || []).slice(0, 3).map(r => `<div class="xs muted row gap8"><span class="dot" style="background:${r.status === 'SUCCEEDED' ? '#0f9c86' : r.completed_at ? '#c22f4e' : '#e0a136'}"></span>Run #${r.id} · ${r.created_jobs} new from ${r.item_count} · ${fmtDay(r.created_at)}</div>`).join('')}</div>
      <button class="btn block mt12" data-act="go" data-screen="discovery" data-mode="setup">Edit search setup</button></section></div>`;
  if (!job) {
    return `<div class="split"><div class="c-main">${`<section class="card">${empty('Inbox zero', done ? `You triaged ${done} job${done === 1 ? '' : 's'} this session. Nothing else is waiting.` : 'No untriaged jobs match these filters.', `${btn('Run the saved search', 'run-search', {}, 'primary')}${btn('Browse all jobs', 'go', { screen: 'discovery', mode: 'table' })}${btn('Add a job manually', 'go', { screen: 'discovery', mode: 'setup' })}`)}</section>`}</div>${side}</div>`;
  }
  const d = D.triageDetail && D.triageDetail.id === job.id ? D.triageDetail : job;
  return `<div class="split">
  <div class="c-main col gap14">
    <div class="card row gap12" style="padding:12px 16px;border-radius:12px"><span class="base b">Job ${done + 1} of ${total}</span><div class="bar grow">${''}<i style="width:${total ? (100 * done / total) : 0}%;background:var(--brand)"></i></div><span class="small muted">${q.total - 1} left · ~${Math.max(1, Math.ceil((q.total) * 0.3))} min</span></div>
    <div class="card flush" style="border-radius:16px">
      <div class="row top-align gap16 wrap" style="padding:22px 24px;border-bottom:1px solid var(--line2)">
        <div class="avatar lg">${esc(initials(job.company))}</div>
        <div style="flex:1 1 210px;min-width:0"><div style="font-size:20px;font-weight:600;letter-spacing:-.01em">${esc(job.title)}</div><div class="muted mt4" style="font-size:13.5px">${esc(job.company)}${job.location ? ' · ' + esc(job.location) : ''}</div>
          <div class="row gap6 wrap mt10">${pill(`${job.board} · ${job.posted}`, 'plain', 'lg')}${job.agency ? pill('Likely agency', 'warn', 'lg') : ''}${job.url ? `<a class="pill plain lg" href="${attr(job.url)}" target="_blank" rel="noopener">Posting ↗</a>` : ''}</div></div>
        ${job.fit == null ? `<div class="scorebox"><div class="n" style="color:var(--grey)">—</div><div class="l">NOT RANKED</div>${btn(label('rank' + job.id, 'Rank now', 'Ranking…'), 'rank-job', { id: job.id }, 'sm soft mt8', isBusy('rank' + job.id))}</div>` : `<div class="scorebox"><div class="n">${fitTxt(job.fit)}</div><div class="l">FIT SCORE</div></div>`}
      </div>
      <div style="padding:18px 24px;border-bottom:1px solid var(--line2)">
        <div class="row gap8 mb10">${pill('AI', 'brand')}<span class="base b">${job.fit == null ? 'Not ranked yet' : `Why this scores ${fitTxt(job.fit)}`}</span></div>
        <div class="small ink2 pretty" style="line-height:1.55">${esc(job.summary || (job.fit == null ? 'Ranking compares the posting against your profile and verified evidence.' : ''))}</div>
        ${job.strategy ? `<div class="small muted mt8 pretty"><b>Strategy:</b> ${esc(job.strategy)}</div>` : ''}
        ${fitSummary(job)}
      </div>
      <div style="padding:18px 24px"><div class="base b mb8">Job description</div><div class="desc fade">${paragraphs(d.description || job.snippet, [...job.matches, ...job.keywords], [...job.gaps, ...job.missing])}</div></div>
      <button class="linkbtn" style="border-top:1px solid var(--line2)" data-act="open-job" data-id="${job.id}">Open full job description</button>
    </div>
    <div class="actionbar">
      ${btn('Skip <span class="kbd">X</span>', 'triage', { id: job.id, action: 'skip' }, 'lg')}
      ${btn('Maybe later <span class="kbd">M</span>', 'triage', { id: job.id, action: 'maybe' }, 'lg')}
      <div class="grow"></div>
      ${btn('Shortlist <span class="kbd">S</span>', 'triage', { id: job.id, action: 'shortlist' }, 'lg soft')}
      ${btn('Shortlist &amp; tailor now <span class="kbd">⏎</span>', 'triage', { id: job.id, action: 'shortlist', tailor: 1 }, 'lg primary')}
    </div>
  </div>${side}</div>`;
}
function renderTable() {
  const j = D.jobs; if (!j) return loading();
  const f = S.filters;
  const sel = D.selDetail;
  const opts = (list, cur) => list.map(([v, t]) => `<option value="${attr(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`).join('');
  const pages = []; for (let p = Math.max(1, j.page - 2); p <= Math.min(j.pages, Math.max(1, j.page - 2) + 4); p++) pages.push(p);
  const start = j.total ? (j.page - 1) * j.per_page + 1 : 0;
  const allOnPage = j.items.length && j.items.every(r => S.checked.has(r.id));
  return `<div class="split">
  <section class="card flush c-main">
    <div class="toolbar">
      <input class="input grow" style="min-width:180px" id="flt-q" data-filter="q" value="${attr(f.q)}" placeholder="Search title, company or location…">
      <select class="select" id="flt-min" data-filter="min_fit">${opts([[0, 'Any fit'], [50, 'Fit ≥ 50'], [70, 'Fit ≥ 70'], [80, 'Fit ≥ 80']], f.min_fit)}</select>
      <select class="select" id="flt-board" data-filter="board">${opts([['all', 'All boards'], ...j.boards.map(b => [b, b])], f.board)}</select>
      <select class="select" id="flt-stage" data-filter="stage">${opts([['all', 'All stages'], ...['discovered', 'shortlisted', 'maybe', 'archived', 'dismissed', 'tailored', 'approved', 'applied'].map(s => [s, s[0].toUpperCase() + s.slice(1)])], f.stage)}</select>
      <select class="select" id="flt-sort" data-filter="sort">${opts([['fit', 'Sort: best fit'], ['new', 'Sort: newest'], ['company', 'Sort: company']], f.sort)}</select>
      ${btn('Reset', 'reset-filters', {}, 'sm')}
      ${btn(label('rankBatch', 'Rank unranked', 'Ranking…'), 'rank-batch', {}, 'sm', isBusy('rankBatch'))}
      ${btn(`Bulk shortlist${S.checked.size ? ` (${S.checked.size})` : ''}`, 'bulk', { action: 'shortlist' }, 'sm soft', !S.checked.size)}
      ${S.checked.size ? btn('Dismiss', 'bulk', { action: 'dismiss' }, 'sm danger') : ''}
    </div>
    <div class="row between wrap" style="padding:9px 14px;border-bottom:1px solid var(--line2);background:#fbfbfe"><span class="small muted">${j.total ? `Showing ${start}–${Math.min(j.page * j.per_page, j.total)} of ${j.total} jobs` : 'No matches'}</span><span class="small muted">Page ${j.page} of ${j.pages} · ${j.total === j.total_all ? 'no filters applied' : `${j.total_all - j.total} hidden by filters`}</span></div>
    <div class="jgrid jhead"><input type="checkbox" data-act="check-all" ${allOnPage ? 'checked' : ''} aria-label="Select page"><span>OPPORTUNITY</span><span class="tr">FIT</span><span>STAGE</span><span>SOURCE</span><span class="tr">POSTED</span></div>
    ${j.items.map(r => `<div class="jgrid jrow ${r.id === S.selJob ? 'sel' : ''}" data-act="select-job" data-id="${r.id}">
      <input type="checkbox" data-act="check" data-id="${r.id}" ${S.checked.has(r.id) ? 'checked' : ''} aria-label="Select">
      <div style="min-width:0"><div class="b ellipsis" style="font-size:13.5px">${esc(r.title)}</div><div class="xs muted ellipsis">${esc(r.company)}${r.location ? ' · ' + esc(r.location) : ''}</div></div>
      <div class="tr mono bb ${fitCls(r.fit)}" style="font-size:13.5px">${fitTxt(r.fit)}</div><div>${statusPill(r.status)}</div><div class="xs muted">${esc(r.board)}</div><div class="tr xs faint">${esc(r.posted)}</div></div>`).join('')}
    ${!j.total ? `<div class="empty small muted">No jobs match these filters. <a href="#" data-act="reset-filters">Reset</a> to see all ${j.total_all}.</div>` : ''}
    <div class="row wrap gap10 pager" style="padding:12px 14px;background:#fbfbfe"><div class="row gap6"><span class="small muted">Rows</span><select class="select" style="margin:0;width:auto;padding:6px 8px" id="flt-per" data-filter="per_page">${opts([[10, 10], [25, 25], [50, 50]], f.per_page)}</select></div><div class="grow"></div>
      ${btn('← Prev', 'page', { page: j.page - 1 }, 'sm', j.page <= 1)}${pages.map(p => btn(String(p), 'page', { page: p }, 'sm ' + (p === j.page ? 'on' : ''))).join('')}${btn('Next →', 'page', { page: j.page + 1 }, 'sm', j.page >= j.pages)}</div>
  </section>
  <section class="card pad18 c-side sticky-panel">${sel ? `
    <div class="row top-align between gap10"><div class="grow"><div style="font-size:17px;font-weight:600">${esc(sel.title)}</div><div class="base muted mt4">${esc(sel.company)}${sel.location ? ' · ' + esc(sel.location) : ''}</div></div><div class="mono bb ${fitCls(sel.fit)}" style="font-size:24px">${fitTxt(sel.fit)}</div></div>
    <div class="row gap6 wrap" style="margin:14px 0">${statusPill(sel.status)}${pill(sel.board + ' · ' + sel.posted, 'plain', 'lg')}${pill(`${sel.matches.length} matched`, 'good', 'lg')}${pill(`${sel.gaps.length + sel.missing.length} gaps`, 'warn', 'lg')}</div>
    <div class="base pretty" style="color:var(--ink3);line-height:1.6;border-top:1px solid var(--line2);padding-top:14px">${esc(sel.summary || (sel.description || '').slice(0, 420) || 'No description captured.')}</div>
    <div class="row gap8 mt16">${['tailored', 'approved', 'applied', 'pending_approval'].includes(sel.status) ? btn('Open packet', 'go', { screen: sel.status === 'applied' ? 'tracker' : sel.status === 'approved' ? 'approvals' : 'tailor', job: sel.id }, 'primary grow') : btn('Tailor', 'triage', { id: sel.id, action: 'shortlist', tailor: 1 }, 'primary grow')}${btn('Full description', 'open-job', { id: sel.id }, 'grow')}</div>` : empty('Select a job', 'Pick a row to see its details.')}
  </section></div>`;
}
function renderSetup() {
  const data = D.searchProfile; if (!data) return loading();
  const p = data.profile, F = 'sp';
  const roles = fv(F, 'roles', p.roles), sites = fv(F, 'sites', p.sites);
  const suggestions = (D.summary && D.profileRoles || []).filter(r => !roles.includes(r)).slice(0, 3);
  const actors = fv(F, 'actors', p.actors || []), customs = fv(F, 'custom_actors', p.custom_actors || []);
  const catalogList = [...(D.actors?.actors || []).filter(a => !a.custom),
    ...customs.map(c => ({ ...c, name: c.name || c.id, summary: 'Added by you', cost: "see the actor's pricing", custom: true }))];
  const on = id => actors.includes(id);
  const platforms = fv(F, 'remote_platforms', p.remote_platforms || []), seniority = fv(F, 'seniority', p.seniority || []);
  const hint = catalogList.filter(a => on(a.id) && a.location_hint).map(a => a.location_hint).join(' ');
  const preview = D.spPreview;
  const run = D.runDetail;
  const boardName = { linkedin: 'LinkedIn', indeed: 'Indeed', glassdoor: 'Glassdoor', naukri: 'Naukri', bayt: 'Bayt', zip_recruiter: 'ZipRecruiter' };
  return `<div class="split">
  <section class="card flush c-main">
    <div style="padding:18px 22px;border-bottom:1px solid var(--line2)"><div class="card-title" style="font-size:16px">Search profile</div><div class="card-sub">Save it once and let it run on a schedule — you triage, not configure.</div></div>
    ${data.apify_configured ? '' : `<div class="banner warn" style="margin:16px 22px 0">APIFY_API_TOKEN is not set on the server, so searches cannot start. You can still add jobs manually.</div>`}
    <div class="col gap18" style="padding:20px 22px">
      <div><div class="small b mb8">Roles <span class="faint" style="font-weight:400">· up to 5</span></div>
        <div class="tagbox" data-tag-input="roles" data-act="focus-tag" data-target="sp-roles">${chips(F, 'roles', p.roles)}<input id="sp-roles" data-tag="roles" data-form="sp" data-max="5" autocomplete="off" placeholder="Type a role, then press Enter"></div>
        <div class="helper">Press Enter to add each role · Backspace removes the last one${suggestions.length ? ` · Suggested from your profile: ${suggestions.map(s => `<a href="#" data-act="suggest-role" data-role="${attr(s)}">${esc(s)}</a>`).join(' · ')}` : ''}</div></div>
      <div><div class="small b mb8">Locations <span class="faint" style="font-weight:400">· up to 5, one search each</span></div>
        <div class="tagbox" data-tag-input="locations" data-act="focus-tag" data-target="sp-locations">${chips(F, 'locations', p.locations)}<input id="sp-locations" data-tag="locations" data-form="sp" data-max="5" data-split="newline" autocomplete="off" placeholder="Type a city or Remote, then press Enter"></div>
        <div class="helper">Press Enter to add each location — commas stay part of it (New York, NY) · leave empty to search anywhere</div></div>
      <div><div class="row base between mb8"><span class="small b">Apify actors <span class="faint" style="font-weight:400">· pick one or more</span></span><a href="#" class="xs" data-act="toggle-add-actor">${S.addActor ? 'Close' : '+ Add your own actor'}</a></div>
        <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:8px">${catalogList.map(a => `<label class="board-opt ${on(a.id) ? 'on' : ''}" style="align-items:flex-start"><input type="checkbox" data-act="toggle-actor" data-actor="${attr(a.id)}" ${on(a.id) ? 'checked' : ''} style="margin-top:3px"><span class="grow" style="min-width:0"><span class="small b" style="display:block">${esc(a.name)}</span><span class="xs muted" style="display:block">${esc(a.summary)}</span><span class="tiny faint" style="display:block;margin-top:4px"><span class="mono">${esc(a.id)}</span> · ${esc(a.cost)} · ${esc(FANOUT[a.fan_out] || a.fan_out)}</span></span>${a.custom ? `<button type="button" class="btn ghost sm danger none" data-act="remove-actor" data-actor="${attr(a.id)}">Remove</button>` : ''}</label>`).join('') || '<span class="small muted">Loading actors…</span>'}</div>
        ${hint ? `<div class="helper" style="color:var(--warn)">${esc(hint)}</div>` : ''}
        ${S.addActor ? addActorForm() : ''}</div>
      ${on('openclawai/job-board-scraper') ? `<div class="box soft" style="padding:14px"><div class="small b mb8">Multi Job Board settings</div>
        <div class="grid-auto w140">${Object.keys(boardName).map(b => `<label class="board-opt ${sites.includes(b) ? 'on' : ''}"><input type="checkbox" data-act="toggle-site" data-site="${b}" ${sites.includes(b) ? 'checked' : ''}><span class="grow">${boardName[b]}</span><span class="tiny mono faint" title="kept per 100 found, last 30 days">${data.board_yield[b] ?? '—'}</span></label>`).join('')}</div>
        <div class="helper">Numbers show jobs kept per 100 found over the last 30 days.</div>
        <div class="grid-auto mt12"><label class="field">Indeed &amp; Glassdoor country${inputF(F, 'country_indeed', p.country_indeed, 'list="indeed-countries" placeholder="usa, uk, india…"')}<datalist id="indeed-countries">${COUNTRIES.map(c => `<option value="${c}">`).join('')}</datalist></label>
          <label class="field">Search radius (miles)${inputF(F, 'distance', p.distance, 'type="number" min="0" max="500"')}</label>
          <label class="field">Skip first N results${inputF(F, 'offset', p.offset, 'type="number" min="0" max="1000"')}</label></div>
        <div class="mt12"><div class="xs muted mb4">LinkedIn company IDs <span class="faint">· optional, restricts LinkedIn to these companies</span></div>${tagBox(F, 'linkedin_company_ids', p.linkedin_company_ids || [], 'e.g. 1441, then Enter', 20)}</div>
        <div class="row gap16 wrap mt12">${checkF(F, 'fetch_description', p.fetch_description, 'Fetch full LinkedIn descriptions')}${checkF(F, 'enforce_annual_salary', p.enforce_annual_salary, 'Normalize salaries to annual')}${checkF(F, 'residential_proxy', p.residential_proxy, 'Residential proxy (recommended for LinkedIn)')}</div></div>` : ''}
      ${on('benthepythondev/remote-jobs-aggregator') ? `<div class="box soft" style="padding:14px"><div class="small b mb8">Remote Jobs Aggregator boards</div>
        <div class="grid-auto w140">${(D.actors?.remote_platforms || []).map(pl => `<label class="board-opt ${platforms.includes(pl) ? 'on' : ''}"><input type="checkbox" data-act="toggle-list" data-key="remote_platforms" data-value="${pl}" ${platforms.includes(pl) ? 'checked' : ''}><span class="grow">${pl}</span></label>`).join('')}</div></div>` : ''}
      ${on('shahidirfan/Remoteok-Job-Scraper') ? `<div class="box soft" style="padding:14px"><label class="field strong">RemoteOK start page${inputF(F, 'remoteok_url', p.remoteok_url, 'placeholder="https://remoteok.com/remote-dev-jobs"')}</label>
        <div class="helper">Use a category page such as https://remoteok.com/remote-dev-jobs to narrow results before the keyword filter.</div></div>` : ''}
      <div><div class="small b mb8">Filters <span class="faint" style="font-weight:400">· each actor receives the filters it supports</span></div><div class="grid-auto">
        <label class="field">Posted within${selectF(F, 'hours_old', p.hours_old, [[1, 'Past hour'], [24, 'Past 24 hours'], [72, 'Past 3 days'], [168, 'Past 7 days'], [720, 'Past 30 days'], [0, 'Any time']])}</label>
        <label class="field">Job type${selectF(F, 'job_type', p.job_type, [['', 'Any job type'], ['fulltime', 'Full-time'], ['parttime', 'Part-time'], ['contract', 'Contract'], ['internship', 'Internship'], ['temporary', 'Temporary']])}</label>
        <label class="field">Results per search${inputF(F, 'max_results', p.max_results, 'type="number" min="1" max="100"')}</label>
        <label class="field">Run schedule${selectF(F, 'schedule', p.schedule, [['daily', 'Daily (cron)'], ['manual', 'Manual only']])}</label></div>
        <div class="mt12"><div class="xs muted mb4">Seniority <span class="faint">· Advanced LinkedIn and Remote Jobs Aggregator</span></div><div class="row gap6 wrap">${(D.actors?.seniority || []).map(s => `<button type="button" class="btn sm ${seniority.includes(s) ? 'soft' : ''}" data-act="toggle-list" data-key="seniority" data-value="${s}">${seniority.includes(s) ? '✓ ' : ''}${s}</button>`).join('')}</div></div>
        <div class="mt12"><div class="xs muted mb4">Exclude titles or keywords</div>${tagBox(F, 'exclude_keywords', p.exclude_keywords || [], 'e.g. intern, then Enter', 20)}</div>
        <div class="row gap16 wrap mt12">${checkF(F, 'is_remote', p.is_remote, 'Remote only')}${checkF(F, 'easy_apply', p.easy_apply, 'Easy Apply only')}${checkF(F, 'salary_only', p.salary_only, 'Salary listed only')}${checkF(F, 'visa_sponsorship', p.visa_sponsorship, 'Offers visa sponsorship')}${checkF(F, 'skip_agencies', p.skip_agencies, 'Skip agencies &amp; reposts')}${checkF(F, 'auto_rank', p.auto_rank, 'Auto-rank on import')}</div></div>
    </div>
    <div class="card-foot row gap12 wrap">
      ${btn(label('run', 'Run now &amp; triage', 'Starting…'), 'run-search', {}, 'primary lg', isBusy('run') || !!S.polling)}
      ${btn(label('saveSp', 'Save profile', 'Saving…'), 'save-search', {}, 'lg', isBusy('saveSp'))}
      <span class="small muted grow">${!preview ? 'Planning runs…' : preview.error ? esc(preview.error)
        : `${preview.runs.length} run${preview.runs.length === 1 ? '' : 's'} across ${new Set(preview.runs.map(r => r.actor_id)).size} actor${new Set(preview.runs.map(r => r.actor_id)).size === 1 ? '' : 's'}`}</span>
      ${preview?.runs?.length ? btn(S.showInputs ? 'Hide inputs' : 'Show exact inputs', 'toggle-inputs', {}, 'sm ghost') : ''}</div>
    ${preview?.warnings?.length ? `<div class="banner warn" style="margin:0 22px 16px;display:block">${preview.warnings.map(w => `<div>• ${esc(w)}</div>`).join('')}</div>` : ''}
    ${S.showInputs && preview?.runs?.length ? `<div style="margin:0 22px 18px" class="doc clip"><pre class="plain mono" style="font-size:11px">${esc(JSON.stringify(preview.runs.map(r => ({ actor: r.actor_id, location: r.location, input: r.input })), null, 2))}</pre></div>` : ''}
  </section>
  <div class="c-narrow col gap14">
    <section class="card flush"><div class="card-head"><div class="card-title sm">Recent runs</div><div class="card-sub">Pick a run to see exactly what it searched and everything it pulled.</div></div>
      ${D.runs.length ? D.runs.slice(0, 6).map(r => `<div class="lrow click ${r.id === S.runSel ? 'sel' : ''}" data-act="select-run" data-id="${r.id}" style="align-items:flex-start"><span class="dot" style="width:7px;height:7px;margin-top:6px;background:${r.status === 'SUCCEEDED' ? '#0f9c86' : r.completed_at ? '#c22f4e' : '#e0a136'}"></span><div class="grow"><div class="small b">Run #${r.id} · ${esc(r.status.toLowerCase())}</div><div class="xs muted mt4"><span class="mono">${esc((r.actor_id || '').split('/').pop())}</span> · ${r.created_jobs} new from ${r.item_count} results · ${fmtDateTime(r.created_at)}</div></div></div>`).join('') : `<div class="small muted" style="padding:16px">No runs yet.</div>`}
      ${run ? `<div style="padding:16px;background:#fbfbfe">
        <div class="row base between wrap mb12"><div class="base b">Run #${run.id}</div>${pill(run.status, run.status === 'SUCCEEDED' ? 'good' : run.completed ? 'bad' : 'warn')}</div>
        <div class="grid-auto w96 mb14">${[['pulled', 'Pulled', 'var(--ink)'], ['new', 'New', 'var(--good)'], ['duplicates', 'Duplicates', 'var(--muted)'], ['filtered', 'Filtered out', 'var(--warn)']].map(([k, l, c]) => `<div class="box" style="padding:9px 10px"><div class="mono bb" style="font-size:17px;color:${c}">${run.counts[k]}</div><div class="tiny muted">${l}</div></div>`).join('')}</div>
        <div class="box mb12"><div class="caps mb8" style="font-size:11px">WHAT IT SEARCHED</div><div class="col gap6">${run.context.map(([k, v]) => `<div class="row top-align small" style="gap:10px"><span class="faint none" style="width:72px">${esc(k)}</span><span class="ink2 grow pretty">${esc(v)}</span></div>`).join('')}</div></div>
        <div class="box" style="padding:0;overflow:hidden"><div class="row between" style="padding:10px 12px;border-bottom:1px solid var(--line3)"><span class="caps" style="font-size:11px">EVERYTHING IT PULLED</span><span class="tiny faint">showing ${Math.min(6, run.results.length)} of ${run.counts.new}</span></div>
          ${run.results.slice(0, 6).map(r => `<div class="row gap9" style="padding:9px 12px;border-bottom:1px solid #f8f9fd;cursor:pointer" data-act="open-job" data-id="${r.id}"><span class="mono bb small ${fitCls(r.fit)}" style="width:22px">${fitTxt(r.fit)}</span><div class="grow"><div class="small b ellipsis">${esc(r.title)}</div><div class="tiny muted ellipsis">${esc(r.company)} · ${esc(r.board)}</div></div>${pill(r.verdict, r.verdict === 'new' ? 'good' : 'warn')}</div>`).join('') || '<div class="small muted" style="padding:12px">No new jobs were imported by this run.</div>'}
          <button class="linkbtn" data-act="go" data-screen="discovery" data-mode="table">See every job in the table →</button></div></div>` : ''}
    </section>
    <section class="card pad16"><div class="card-title sm mb4">Add one manually</div><div class="small muted mb12">Paste any job URL — we'll pull the title, company and description.</div>
      ${inputF('manual', 'url', '', 'placeholder="https://…"')}
      ${S.manualMissing ? `<div class="col gap8 mt8"><div class="xs" style="color:var(--warn)">${esc(S.manualMissing)}</div>${inputF('manual', 'title', '', 'placeholder="Job title"')}${inputF('manual', 'company', '', 'placeholder="Company"')}${inputF('manual', 'location', '', 'placeholder="Location (optional)"')}${textareaF('manual', 'description', '', 'placeholder="Paste the description"')}</div>` : ''}
      ${btn(label('manual', 'Add to pipeline', 'Reading posting…'), 'add-manual', {}, 'block mt8', isBusy('manual'))}</section>
    <section class="card pad16"><div class="card-title sm mb4">Import a company board</div><div class="small muted mb12">Pull every open role from a company's Greenhouse or Lever board. No Apify token needed.</div>
      <div class="row gap8">${selectF('board', 'kind', 'greenhouse', [['greenhouse', 'Greenhouse'], ['lever', 'Lever']], 'style="width:auto"')}${inputF('board', 'token', '', 'placeholder="Board token, e.g. stripe"')}</div>
      <div class="helper">The token is the company slug in boards.greenhouse.io/<b>stripe</b> or jobs.lever.co/<b>stripe</b>.</div>
      ${btn(label('board', 'Import board', 'Importing…'), 'import-board', {}, 'block mt8', isBusy('board'))}</section>
  </div></div>`;
}

/* ───────────── tailor ───────────── */
function renderTailor() {
  const queue = D.tailorQueue; if (!queue) return loading();
  if (!queue.length) return `<section class="card">${empty('Nothing to tailor yet', 'Shortlist a job in Discovery and it will appear here.', btn('Go to triage', 'go', { screen: 'discovery', mode: 'triage' }, 'primary'))}</section>`;
  const p = D.packet; if (!p) return loading();
  const job = p.job, r = p.resume;
  const picker = `<select class="select" style="margin:0;width:auto;max-width:320px" id="tailor-pick" data-act-change="pick-tailor">${queue.map(j => `<option value="${j.id}" ${j.id === S.tailorJob ? 'selected' : ''}>${esc(j.company)} — ${esc(j.title)}${j.has_resume ? '' : ' (not tailored)'}</option>`).join('')}</select>`;
  const generating = isBusy('tailor' + job.id), skipping = isBusy('skip' + job.id), working = generating || skipping;
  if (!r) {
    return `<div class="stack"><section class="card row gap16 wrap"><div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">Tailoring for ${esc(job.title)} · ${esc(job.company)}</div><div class="card-sub">Fit ${fitTxt(job.fit)} · no packet generated yet</div></div>${picker}</section>
      <section class="card">${empty(generating ? 'Drafting your packet…' : skipping ? 'Reviewing your master resume…' : 'No packet yet', generating ? 'Ranking, tailoring and an independent review run back to back. This can take a minute.' : skipping ? 'Your master resume is used unchanged; the reviewer is scoring it against this posting.' : 'Generate a tailored resume, cover letter and reviewer score. Tailoring may only cite your master resume and verified evidence. If this posting needs no tailoring, skip it and send your master resume as-is.', btn(generating ? 'Generating…' : 'Generate tailored packet', 'generate', { id: job.id }, 'primary lg', working) + btn(skipping ? 'Skipping…' : 'Skip tailoring', 'skip-tailor', { id: job.id }, 'lg', working) + btn('Read the posting', 'open-job', { id: job.id }))}</section></div>`;
  }
  const accepted = r.edits.filter(e => e.status === 'accepted').length;
  const threshold = p.application_mode === 'automatic'
    ? `Auto-approval threshold is ${p.min_review_score}. ${r.reviewer_score >= p.min_review_score ? 'This packet cleared it.' : 'This packet needs your sign-off.'}`
    : `Manual approval mode · minimum review score ${p.min_review_score}.`;
  let body = '';
  if (S.tailorTab === 'diff' && r.untailored) {
    body = `<div style="padding:18px">${empty('Tailoring skipped', 'This packet uses your master resume unchanged, so there are no edits to review. Use “Tailor instead” to generate a tailored version.')}</div>`;
  } else if (S.tailorTab === 'diff') {
    body = r.edits.map(e => {
      const tone = { accepted: 'good', review: 'warn', reverted: 'muted' }[e.status];
      const editing = S.editing === e.index;
      return `<div class="diff ${e.status}"><div class="row between mb10"><span class="caps">${esc(e.section)}</span>${pill(e.status === 'review' ? 'review this' : e.status, tone)}</div>
        <div class="diff-before pretty">${esc(e.before)}</div>
        ${editing ? `<textarea class="textarea" id="edit-text-${e.index}">${esc(e.after)}</textarea>` : `<div class="diff-after pretty">${esc(e.after)}</div>`}
        <div class="row wrap gap10 mt10">${e.evidence ? pill('evidence · ' + e.evidence, 'good', 'lg') : pill('no verified evidence matched', 'warn', 'lg')}<div class="grow"></div>
          ${editing ? btn('Cancel', 'edit-cancel', {}, 'sm') + btn('Save edit', 'edit-save', { index: e.index, resume: r.id }, 'sm primary')
            : `${btn('Edit', 'edit-start', { index: e.index }, 'sm')}${e.status !== 'accepted' ? btn(e.status === 'reverted' ? 'Restore' : 'Accept', 'edit-status', { index: e.index, resume: r.id, status: 'accepted' }, 'sm soft') : ''}${e.status !== 'reverted' ? btn('Revert', 'edit-status', { index: e.index, resume: r.id, status: 'reverted' }, 'sm') : ''}`}</div></div>`;
    }).join('');
  } else if (S.tailorTab === 'resume') {
    body = `<div style="padding:18px"><div class="doc">${miniMarkdown(r.markdown)}</div></div>`;
  } else if (S.tailorTab === 'cover') {
    body = `<div style="padding:18px">${textareaF('cover' + r.id, 'text', r.cover_letter, 'style="min-height:260px;font-size:13px"')}<div class="row gap8 mt10"><span class="small muted grow">${r.cover_words} words</span>${btn(label('cover', 'Save cover letter', 'Saving…'), 'save-cover', { resume: r.id }, 'primary sm', isBusy('cover'))}</div></div>`;
  } else {
    const ans = D.knowledge || [];
    body = `<div style="padding:18px" class="col gap9">${ans.length ? ans.slice(0, 12).map(a => `<div class="box"><div class="small muted">${esc(a.question)}</div><div class="row gap8 mt4"><span class="small b grow">${esc(a.answer || 'Awaiting answer')}</span>${pill(a.sensitive ? 'confirm each time' : a.verified ? 'verified' : 'needs review', a.sensitive || !a.verified ? 'warn' : 'good')}</div></div>`).join('') : '<div class="small muted">No saved ATS answers yet. Add them in the Knowledge bank.</div>'}</div>`;
  }
  return `<div class="stack">
  <section class="card row gap16 wrap" style="padding:16px 20px">
    <div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">Tailoring for ${esc(job.title)} · ${esc(job.company)}</div><div class="card-sub">v${r.version} · generated ${esc(r.generated)} · ${r.untailored ? 'master resume, not tailored' : 'edits must trace to verified evidence'}</div></div>
    ${picker}
    <div class="row gap14"><div class="metric"><div class="n" style="color:${r.reviewer_score >= p.min_review_score ? 'var(--good)' : 'var(--warn)'}">${fitTxt(r.reviewer_score)}</div><div class="l">REVIEWER</div></div>
      <div class="metric"><div class="n">${r.ats_pct == null ? '—' : r.ats_pct + '%'}</div><div class="l">ATS KEYWORDS</div></div>
      ${btn(generating ? (r.untailored ? 'Tailoring…' : 'Regenerating…') : (r.untailored ? 'Tailor instead' : 'Regenerate'), 'generate', { id: job.id }, '', working)}
      ${r.untailored ? '' : btn(skipping ? 'Skipping…' : 'Skip tailoring', 'skip-tailor', { id: job.id }, '', working)}
      ${btn('Send to approval', 'go', { screen: 'approvals', approval: job.id }, 'primary')}</div>
  </section>
  <div class="split gap16">
    <section class="card flush c-main"><div class="row between wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div class="tabs">${[['diff', 'Diff'], ['resume', 'Resume'], ['cover', 'Cover letter'], ['answers', 'ATS answers']].map(([id, t]) => `<button class="${S.tailorTab === id ? 'on' : ''}" data-act="tailor-tab" data-id="${id}">${t}</button>`).join('')}</div>
      <span class="small muted">${r.edits.length} edits · ${accepted} accepted</span></div>${body}</section>
    <div class="c-side col gap14">
      <section class="card pad18"><div class="row gap8 mb12">${pill('AI', 'brand')}<span class="card-title sm">Reviewer notes</span><span class="mono bb base" style="margin-left:auto;color:var(--good)">${fitTxt(r.reviewer_score)} / 100</span></div>
        <div class="col gap10">${r.review.length ? r.review.map(n => `<div class="row top-align gap10"><span class="note-dot" style="background:${{ good: '#0f9c86', warn: '#e0a136', bad: '#c22f4e' }[n.tone]}"></span><div class="small ink2 pretty" style="line-height:1.55">${esc(n.text)}</div></div>`).join('') : '<div class="small muted">The reviewer left no notes.</div>'}</div>
        <div class="xs faint mt12" style="padding-top:12px;border-top:1px solid var(--line2)">${esc(threshold)}</div></section>
      <section class="card pad18"><div class="card-title sm mb12">Keyword coverage</div><div class="row wrap gap6">${r.keywords.length ? r.keywords.map(k => `<span class="kw ${k.status}" title="${k.status}">${esc(k.word)}</span>`).join('') : '<span class="small muted">Rank the job to extract keywords.</span>'}</div></section>
      <section class="card pad18"><div class="card-title sm mb10">Exports</div><div class="col gap8">
        <a class="btn between" href="/api/jobs/${job.id}/resume.pdf">Resume PDF <span class="faint">plain text</span></a>
        <a class="btn between" href="/api/jobs/${job.id}/resume.docx">Resume DOCX <span class="faint">ATS-safe</span></a>
        <a class="btn between" href="/api/jobs/${job.id}/cover-letter.txt">Cover letter <span class="faint">${r.cover_words} words</span></a></div></section>
    </div></div></div>`;
}

/* ───────────── approvals ───────────── */
function renderApprovals() {
  const list = D.approvals; if (!list) return loading();
  if (!list.length) return `<section class="card">${empty('The queue is clear', 'Tailored packets land here for your sign-off.', btn('Go to Tailor', 'go', { screen: 'tailor' }, 'primary'))}</section>`;
  const a = D.approval;
  const waiting = list.filter(x => x.waiting).length;
  const queue = `<section class="card flush c-narrow"><div class="card-head card-title sm">Queue · ${waiting} waiting</div>
    ${list.map(q => `<div class="lrow click ${q.id === S.approvalJob ? 'sel' : ''}" data-act="select-approval" data-id="${q.id}"><div class="grow"><div class="base b ellipsis">${esc(q.title)}</div><div class="xs muted">${esc(q.company)} · ${esc(q.age)}</div></div><span class="mono bb base" style="color:${q.status === 'approved' ? 'var(--faint)' : (q.reviewer_score || 0) >= 75 ? 'var(--good)' : (q.reviewer_score || 0) >= 65 ? 'var(--warn)' : 'var(--bad)'}">${fitTxt(q.reviewer_score)}</span></div>`).join('')}</section>`;
  if (!a) return `<div class="split">${queue}<section class="card c-main">${loading()}</section></div>`;
  const r = a.resume, job = a.job;
  const approved = job.status === 'approved';
  return `<div class="split">${queue}
  <section class="card flush c-main">
    <div class="row between wrap gap12" style="padding:18px 22px;border-bottom:1px solid var(--line2)"><div><div style="font-size:17px;font-weight:600">${esc(job.title)} · ${esc(job.company)}</div><div class="small muted mt4">Fit ${fitTxt(job.fit)} · reviewer ${fitTxt(r?.reviewer_score)} · tailored ${esc(r?.generated || '')} · ${statusPill(job.status)}</div></div>
      <div class="row gap8">${approved ? btn('Open apply kit', 'open-kit', { id: job.id }, 'good') : `${btn(label('revise', 'Needs revision', 'Sending back…'), 'revise', { id: job.id }, '', isBusy('revise'))}${btn(label('approve', 'Approve → apply kit', 'Approving…'), 'approve', { id: job.id }, 'good', isBusy('approve') || !r)}`}</div></div>
    ${r ? `<div style="padding:18px 22px;border-bottom:1px solid var(--line2)"><div class="base b mb12">Pre-submit checklist</div>
      <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:10px">${a.checklist.map(c => `<div class="check-item ${c.state}"><span class="mark">${c.state === 'ok' ? '✓' : '!'}</span><div><div class="small b lbl">${esc(c.label)}</div><div class="xs muted mt4">${esc(c.detail)}</div></div></div>`).join('')}</div></div>
    <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:0">
      <div style="padding:18px 22px;border-right:1px solid var(--line2)"><div class="row between mb10"><div class="base b">Tailored resume</div><a class="small" href="#" data-act="go" data-screen="tailor" data-job="${job.id}">Edit in Tailor</a></div><div class="doc clip">${miniMarkdown(r.markdown)}</div></div>
      <div style="padding:18px 22px"><div class="base b mb10">ATS answers to reuse</div>
        <div class="col gap9">${a.answers.length ? a.answers.map(x => `<div class="box"><div class="small muted">${esc(x.question)}</div><div class="row gap8 mt4"><span class="small b grow">${esc(x.answer || 'Awaiting answer')}</span>${pill(x.sensitive ? 'confirm' : x.verified ? 'verified' : 'unverified', x.sensitive || !x.verified ? 'warn' : 'good')}</div></div>`).join('') : '<div class="small muted">No saved answers. Add them in the Knowledge bank.</div>'}</div>
        <textarea class="textarea mt12" id="approval-notes" data-act-blur="save-notes" data-id="${job.id}" placeholder="Notes for yourself — saved when you click away">${esc(fv('approvalNotes', job.id, a.notes))}</textarea></div></div>` : empty('No packet', 'This job has no tailored resume.')}
  </section></div>`;
}
function renderKit() {
  const k = D.kit; if (!k) return `<div class="overlay center" data-act="overlay-close"><div class="modal white" style="padding:40px">${loading()}</div></div>`;
  const job = k.job;
  return `<div class="overlay center" data-act="overlay-close"><div class="modal white" role="dialog" aria-label="Apply kit">
    <div class="row top-align between gap16" style="padding:22px 24px;border-bottom:1px solid var(--line2)"><div>
      <div class="row gap9">${pill(k.status === 'applied' ? 'APPLIED' : 'APPROVED', 'good')}<span class="small muted">Fit ${fitTxt(job.fit)} · reviewer ${fitTxt(k.reviewer_score)}</span></div>
      <div style="font-size:21px;font-weight:600;letter-spacing:-.02em;margin-top:8px">Apply kit · ${esc(job.title)} at ${esc(job.company)}</div>
      <div class="base muted mt4 pretty">We never submit for you. Everything the form will ask for is on this screen — open the posting, paste, come back and confirm.</div></div>
      ${btn('Close', 'close-kit', {}, 'none')}</div>
    <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:0">
      <div style="padding:20px 24px;border-right:1px solid var(--line2)">
        <div class="caps mb10">1 · TAKE THESE</div><div class="col gap9">${k.files.map(f => `<div class="box row gap12"><div class="avatar" style="width:32px;height:32px;border-radius:8px;font-size:10px;background:var(--soft);color:var(--muted)">${f.kind}</div><div class="grow"><div class="small b ellipsis">${esc(f.name)}</div><div class="xs muted">${esc(f.meta)}</div></div><a class="btn sm soft" href="${attr(f.href)}" download="${attr(f.name)}">Download</a></div>`).join('')}</div>
        <div class="caps" style="margin:20px 0 11px">2 · PASTE THESE</div><div class="col gap8">${k.answers.length ? k.answers.map(x => `<div class="box row gap10"><div class="grow"><div class="xs muted">${esc(x.question)}</div><div class="small b mt4">${esc(x.answer)}</div></div>${btn('Copy', 'copy-answer', { id: x.id, text: x.answer }, 'ghost sm none')}</div>`).join('') : '<div class="small muted">No verified answers yet — anything you type into their form can be saved to the Knowledge bank afterwards.</div>'}</div>
      </div>
      <div style="padding:20px 24px;background:#fbfbfe">
        <div class="caps mb10">3 · SUBMIT ON THEIR SITE</div>
        ${job.url ? `<a class="btn primary block lg" style="border-radius:11px;padding:14px" href="${attr(job.url)}" target="_blank" rel="noopener">Open ${esc(job.company)} application ↗</a>` : `<div class="banner warn">No posting URL was captured for this job.</div>`}
        <div class="small muted mt10 pretty" style="line-height:1.55">Opens in a new tab. Job Hunt OS does not bypass logins or CAPTCHAs — the submit click stays yours.</div>
        <div class="hr"></div>
        <div class="caps mb10">4 · CONFIRM BACK HERE</div>
        ${k.status === 'applied' ? `<div class="box good"><div class="b" style="color:var(--good)">Already marked applied</div><div class="small ink2 mt4">Track replies and follow-ups in the Tracker.</div>${btn('Open tracker', 'go', { screen: 'tracker', app: job.application_id }, 'block mt12')}</div>` : `
        <div class="box good" style="padding:15px;border-radius:13px"><div class="b" style="font-size:13.5px;color:var(--good)">Did you submit it?</div>
          <div class="small ink2 mt4" style="line-height:1.55">Confirming moves this to <b>Applied</b>, timestamps it, and schedules a follow-up for <b>${fmtDay(k.followup_date)}</b>.</div>
          <div class="grid-auto mt12" style="grid-template-columns:1fr 1fr;gap:8px">${inputF('kit', 'recruiter_name', '', 'placeholder="Recruiter name (optional)"')}${inputF('kit', 'recruiter_email', '', 'placeholder="Recruiter email (optional)"')}</div>
          ${btn(label('applied', 'Yes — mark applied', 'Saving…'), 'mark-applied', { id: job.id }, 'good block lg mt12', isBusy('applied'))}
          ${btn('Not yet — keep it in approvals', 'close-kit', {}, 'block mt8')}</div>`}
      </div></div></div></div>`;
}

/* ───────────── tracker ───────────── */
function renderTracker() {
  const t = D.tracker; if (!t) return loading();
  const apps = t.applications;
  const stats = [[t.stats.live, 'Live applications', 'var(--ink)'], [t.stats.replies, 'Replies', 'var(--good)'], [t.stats.due, 'Follow-ups due', 'var(--warn)'], [t.stats.response_rate, 'Response rate', 'var(--brand-ink)']];
  const head = `<div class="row wrap gap10"><div class="row wrap gap10 grow">${stats.map(([n, l, c]) => `<div class="card" style="padding:11px 15px;min-width:130px;border-radius:11px"><div class="mono bb" style="font-size:20px;color:${c}">${esc(n)}</div><div class="xs muted mt4">${l}</div></div>`).join('')}</div>
    ${btn('Board', 'tracker-view', { id: 'board' }, S.trackerView === 'board' ? '' : 'ghost')}${btn('Timeline', 'tracker-view', { id: 'timeline' }, S.trackerView === 'timeline' ? '' : 'ghost')}</div>`;
  if (!apps.length) return `<div class="stack">${head}<section class="card">${empty('No submitted applications yet', 'Approve a packet and confirm you applied from the apply kit — it lands here.', btn('Open approvals', 'go', { screen: 'approvals' }, 'primary'))}</section></div>`;
  if (S.trackerView === 'timeline') {
    const events = apps.flatMap(a => a.timeline.map(e => ({ ...e, app: a }))).sort((x, y) => y.at.localeCompare(x.at));
    return `<div class="stack">${head}<section class="card">${events.map(e => `<div class="tl"><div class="tl-rail"><span class="d" style="background:${KIND_DOT[e.kind] || '#c6cad8'}"></span><span class="l"></span></div><div style="padding-bottom:16px" class="grow"><div class="small b">${esc(e.app.company)} · ${esc(e.title)}</div><div class="xs muted mt4">${fmtDateTime(e.at)}${e.detail && e.kind !== 'stage' ? ' · ' + esc(e.detail) : ''}</div></div>${btn('Open', 'select-app', { id: e.app.id, board: 1 }, 'sm none')}</div>`).join('')}</section></div>`;
  }
  const sel = apps.find(a => a.id === S.selApp) || apps[0];
  const stageLabel = (t.stages.find(s => s.id === sel.stage) || { label: sel.stage }).label;
  return `<div class="stack" style="gap:14px">${head}
  <div class="kanban">${t.stages.map(st => { const cards = apps.filter(a => a.stage === st.id); return `<div class="kcol" data-drop-stage="${st.id}">
      <div class="row between" style="padding:2px 4px"><div class="row gap6"><span class="dot" style="width:7px;height:7px;background:${st.dot}"></span><span class="small b">${esc(st.label)}</span></div><span class="mono xs muted">${cards.length}</span></div>
      ${cards.map(k => `<div class="kcard ${k.id === sel.id ? 'sel' : ''}" draggable="true" data-drag-app="${k.id}" data-act="select-app" data-id="${k.id}">
        <div class="row top-align between gap8"><div class="small b pretty" style="line-height:1.35">${esc(k.title)}</div><span class="mono bb small none ${fitCls(k.fit)}">${fitTxt(k.fit)}</span></div>
        <div class="xs muted mt4">${esc(k.company)}</div>
        <div class="row gap6 wrap mt8">${k.tag ? pill(k.tag, k.tone === 'warn' ? 'warn' : k.tone === 'good' ? 'good' : 'muted') : ''}${pill(k.age, 'muted')}</div></div>`).join('')}</div>`; }).join('')}
    <div class="kadd"><div class="small b ink2">Add a stage</div><div class="xs muted" style="line-height:1.45">Track the parts of your process this board is missing.</div>
      <div class="row gap6 wrap">${t.available_stages.map(s => btn('+ ' + esc(s.label), 'add-stage', { id: s.id }, 'sm')).join('')}</div>
      ${t.extra_stages.length ? `<button class="btn ghost sm danger" style="margin-top:auto;justify-content:flex-start" data-act="remove-stage">Remove the last added stage</button>` : ''}</div>
  </div>
  <section class="card">
    <div class="row top-align between wrap gap14 mb16"><div><div class="row gap9 wrap"><span class="card-title">${esc(sel.company)} · ${esc(sel.title)}</span>${pill(stageLabel, 'brand')}</div><div class="small muted mt4">${esc(sel.meta)}${sel.location ? ' · ' + esc(sel.location) : ''} · fit ${fitTxt(sel.fit)}</div></div>
      <div class="row gap8 wrap">${btn('Open job', 'open-job', { id: sel.job_id }, 'sm')}${sel.url ? `<a class="btn sm" href="${attr(sel.url)}" target="_blank" rel="noopener">Open posting ↗</a>` : ''}</div></div>
    <div class="row wrap gap10 mb16" style="background:#f8f9fd;border:1px solid #eaecf5;border-radius:12px;padding:12px 14px"><span class="caps none">MOVE TO</span>
      <div class="row wrap gap6 grow">${t.stages.map(st => btn(esc(st.label), 'move-stage', { app: sel.id, stage: st.id }, 'sm ' + (st.id === sel.stage ? 'primary' : ''))).join('')}</div><span class="xs muted none">or drag the card between columns</span></div>
    <div class="split gap18">
      <div style="flex:1 1 320px;min-width:0"><div class="caps mb12">HISTORY</div>
        <div class="col">${sel.timeline.map(e => `<div class="tl"><div class="tl-rail"><span class="d" style="background:${KIND_DOT[e.kind] || '#c6cad8'}"></span><span class="l"></span></div><div style="padding-bottom:16px"><div class="small b">${esc(e.title)}</div><div class="xs muted mt4 pretty">${fmtDateTime(e.at)}${e.detail && e.kind !== 'stage' ? ' · ' + esc(e.detail) : ''}</div></div></div>`).join('')}</div>
        <div class="logbox"><select class="select" style="width:auto;padding:6px 8px" id="log-kind">${['note', 'call', 'email', 'referral'].map(k => `<option value="${k}">${k[0].toUpperCase() + k.slice(1)}</option>`).join('')}</select>
          <input class="input bare grow" id="log-text" placeholder="Log something — call, email, referral…" data-enter="log-event" data-app="${sel.id}">${btn(label('log', 'Add', 'Adding…'), 'log-event', { app: sel.id }, 'sm', isBusy('log'))}</div>
      </div>
      <div style="flex:1 1 240px;min-width:0" class="col gap12">
        <div class="box brand" style="padding:13px"><div class="caps mb8">NEXT STEP</div><div class="base b">${esc(sel.next)}</div><div class="small muted mt4">${esc(sel.next_date)}</div></div>
        <div class="box" style="padding:13px"><div class="row between mb8"><span class="caps">RECRUITER</span>${S.editingRecruiter ? '' : `<a href="#" class="xs" data-act="edit-recruiter">Edit</a>`}</div>
          ${S.editingRecruiter ? `${inputF('rec' + sel.id, 'recruiter_name', sel.recruiter_name, 'placeholder="Name"')}${inputF('rec' + sel.id, 'recruiter_email', sel.recruiter_email, 'placeholder="Email"')}<div class="row gap8 mt8">${btn('Cancel', 'edit-recruiter-cancel', {}, 'sm')}${btn('Save', 'save-recruiter', { app: sel.id }, 'sm primary')}</div>`
            : `<div class="base b">${esc(sel.recruiter_name || 'No contact captured')}</div><div class="small muted mt4">${sel.recruiter_email ? `<a href="mailto:${attr(sel.recruiter_email)}">${esc(sel.recruiter_email)}</a>` : 'Add a recruiter to personalise nudges'}</div>`}</div>
        <div class="box" style="padding:13px"><div class="caps mb8">SUBMITTED</div><div class="small ink2" style="line-height:1.5">${esc(sel.docs)}</div>${sel.notes ? `<div class="xs muted mt8 pretty">${esc(sel.notes)}</div>` : ''}</div>
        <div class="box" style="padding:13px;border-color:#e2ddff;background:var(--brand-softer)"><div class="row gap6 mb8">${pill('AI', 'brand')}<span class="xs b" style="color:var(--brand-ink)">Follow-up draft</span></div>
          <div class="small ink2 pretty" style="line-height:1.55;white-space:pre-wrap">${esc(sel.draft || 'No draft yet. Generate one — nothing is ever sent for you.')}</div>
          <div class="row gap8 mt10">${sel.draft ? btn('Review &amp; copy', 'copy-draft', { app: sel.id }, 'sm primary') : ''}${btn(label('nudge' + sel.id, sel.draft ? 'Regenerate' : 'Draft follow-up', 'Drafting…'), 'draft-nudge', { app: sel.id, stay: 1 }, 'sm' + (sel.draft ? '' : ' primary'), isBusy('nudge' + sel.id))}</div></div>
        ${S.scheduling ? `<div class="box" style="padding:13px"><div class="caps mb8">SCHEDULE AN INTERVIEW</div><input class="input" id="iv-title" placeholder="e.g. Panel interview"><input class="input" id="iv-at" type="datetime-local"><div class="row gap8 mt8">${btn('Cancel', 'schedule-cancel', {}, 'sm')}${btn('Save', 'schedule-save', { app: sel.id }, 'sm primary')}</div></div>`
          : btn('Schedule an interview', 'schedule-start', {}, 'block')}
      </div></div>
  </section></div>`;
}
const KIND_DOT = { applied: '#5b4ae8', stage: '#0f9c86', note: '#c6cad8', call: '#0f9c86', email: '#a79bf0', referral: '#a79bf0', interview: '#e0a136' };

/* ───────────── knowledge ───────────── */
const CATEGORIES = ['general', 'work_authorization', 'compensation', 'notice_period', 'relocation', 'demographic'];
function renderKnowledge() {
  const k = D.knowledge; if (!k || !D.evidence) return loading();
  const verified = k.filter(a => a.verified).length;
  const editing = k.find(a => a.id === S.editKnowledge);
  const F = 'kn' + (editing ? editing.id : 'new');
  const ev = D.evidence;
  return `<div class="split">
  <section class="card flush c-main"><div class="row gap8 wrap" style="padding:14px 18px;border-bottom:1px solid var(--line2)"><div class="card-title grow">Answer library</div>${pill(`${verified} verified`, 'good', 'lg')}${pill(`${k.length - verified} need review`, 'warn', 'lg')}</div>
    ${k.length ? k.map(a => `<div style="padding:15px 18px;border-bottom:1px solid var(--line3);${a.id === S.editKnowledge ? 'background:var(--brand-softer)' : ''}"><div class="row top-align gap10"><div class="grow">
      <div class="base b pretty">${esc(a.question)}</div><div class="small ink2 mt4 pretty">${esc(a.answer || 'Awaiting answer')}</div>
      <div class="row gap6 wrap mt8">${a.sensitive ? pill('sensitive', 'bad') : a.verified ? pill('verified', 'good') : pill('needs review', 'warn')}${pill(a.category, 'muted')}${pill(`used ${a.use_count}×`, 'muted')}</div></div>
      <div class="col gap6 none">${btn('Edit', 'kn-edit', { id: a.id }, 'sm')}${btn('Archive', 'kn-delete', { id: a.id }, 'sm danger')}</div></div></div>`).join('') : empty('No answers yet', 'Save the questions ATS forms keep asking — work authorization, notice period, compensation.')}
  </section>
  <div class="c-side col gap14">
    <section class="card pad18"><div class="card-title sm mb4">${editing ? 'Edit answer' : 'Add an answer'}</div><div class="small muted mb12">Answer once, reuse in every ATS form.</div>
      ${textareaF(F, 'question', editing?.question, 'placeholder="Question, e.g. Are you authorized to work in India?" style="min-height:60px"')}
      ${textareaF(F, 'answer', editing?.answer, 'placeholder="Your answer"')}
      <div class="row gap9">${selectF(F, 'category', editing?.category || 'general', CATEGORIES.map(c => [c, c]))}${selectF(F, 'answer_type', editing?.answer_type || 'text', [['text', 'text'], ['boolean', 'boolean'], ['choice', 'choice'], ['number', 'number']])}</div>
      <div class="col gap6 mt10">${checkF(F, 'verified', editing ? editing.verified : true, 'Human verified — eligible for reuse')}${checkF(F, 'sensitive', editing?.sensitive, 'Sensitive — always ask me first')}</div>
      <div class="row gap8 mt12">${editing ? btn('Cancel', 'kn-cancel', {}, 'grow') : ''}${btn(label('kn', 'Save answer', 'Saving…'), 'kn-save', { id: editing?.id || '' }, 'primary grow', isBusy('kn'))}</div></section>
    <section class="card pad18"><div class="card-title sm mb4">Evidence bank</div><div class="small muted mb12">Tailoring may only cite verified claims. ${ev.filter(e => e.verified).length} verified.</div>
      <div class="col gap8">${ev.length ? ev.map(e => `<div class="box"><div class="row top-align gap8"><div class="grow"><div class="small b pretty">${esc(e.claim)}</div><div class="xs muted mt4">${esc([e.company, e.role, (e.skills || []).join(', ')].filter(Boolean).join(' · ') || 'no context')}</div></div>
        <div class="col gap4 none">${e.verified ? pill('verified', 'good') : btn('Verify', 'ev-verify', { id: e.id }, 'sm soft')}<a href="#" class="xs danger tr" style="color:var(--bad)" data-act="ev-delete" data-id="${e.id}">Remove</a></div></div></div>`).join('') : '<div class="small muted">No evidence yet. Add claims below or re-parse your resume from Settings.</div>'}</div>
      <div class="hr" style="margin:14px 0"></div>
      ${textareaF('ev', 'claim', S.evidencePrefill || '', 'placeholder="A factual achievement, e.g. Cut cloud spend 35% in 2024" style="min-height:56px"')}
      <div class="row gap8">${inputF('ev', 'company', '', 'placeholder="Company"')}${inputF('ev', 'role', '', 'placeholder="Role"')}</div>
      ${inputF('ev', 'skills', '', 'placeholder="Skills, comma separated"')}
      ${btn(label('ev', 'Add verified evidence', 'Adding…'), 'ev-add', {}, 'block mt10', isBusy('ev'))}</section>
  </div></div>`;
}

/* ───────────── settings ───────────── */
const CHIP_FIELDS = [
  ['target_roles', 'Target roles', 'up to 5 · drives every search', 'Add a role…', '', 'top'],
  ['preferred_locations', 'Target locations', 'cities, regions or Remote', 'Add a location…', '', 'top'],
  ['countries', 'Countries', 'where you will consider working', 'Add a country…', 'good', 'sp'],
  ['primary_skills', 'Primary skills', 'must appear in the job to rank well', 'Add a skill…', '', 'sp'],
  ['secondary_skills', 'Secondary skills', 'nice to have', 'Add a skill…', 'grey', 'sp'],
  ['industries', 'Industries', 'preferred domains', 'Add an industry…', 'grey', 'sp'],
  ['exclude_companies', 'Exclude companies', 'never show these again', 'Add a company…', 'bad', 'sp'],
  ['languages', 'Languages', 'for ATS answers', 'Add a language…', 'grey', 'sp'],
];
function renderSettings() {
  const p = D.profile, st = D.settings, pr = D.prefs; if (!p || !st || !pr) return loading();
  const sp = p.search_preferences || {}, F = 'profile';
  const val = (key, where) => where === 'sp' ? sp[key] : p[key];
  const filled = [fv(F, 'name', p.name), fv(F, 'headline', p.headline), fv(F, 'email', p.email), fv(F, 'phone', p.phone), fv(F, 'location', p.location), fv(F, 'total_experience', sp.total_experience), fv(F, 'notice_period', sp.notice_period), fv(F, 'compensation_notes', p.compensation_notes), (fv(F, 'target_roles', p.target_roles) || []).length, (fv(F, 'primary_skills', sp.primary_skills) || []).length, fv(F, 'work_authorization', p.work_authorization), p.master_resume];
  const pct = Math.round(100 * filled.filter(Boolean).length / filled.length);
  const mode = fv('ai', 'application_mode', st.application_mode);
  const fields = [['name', 'Full name', p.name], ['headline', 'Headline', p.headline], ['email', 'Email', p.email], ['phone', 'Phone', p.phone], ['location', 'Current location', p.location], ['total_experience', 'Total experience', sp.total_experience], ['notice_period', 'Notice period', sp.notice_period], ['compensation_notes', 'Expected compensation', p.compensation_notes]];
  return `<div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;align-items:start">
  <section class="card" style="grid-column:1/-1;padding:20px">
    <div class="row base between wrap gap12 mb16"><div><div class="card-title">Search profile &amp; candidate details</div><div class="card-sub">This drives discovery, ranking and what tailoring may claim. Everything with chips accepts several values.</div></div>${pill(`${pct}% complete`, pct >= 80 ? 'good' : 'warn', 'lg')}</div>
    <div class="grid-auto w240">${fields.map(([k, l, v]) => `<label class="field">${l}${inputF(F, k, v)}</label>`).join('')}</div>
    <div class="hr"></div>
    <div class="grid-auto w300">${CHIP_FIELDS.map(([k, l, hint, ph, tone, where]) => `<div><div class="row base gap8 mb8"><span class="small b">${l}</span><span class="tiny faint">${hint}</span></div>${tagBox(F, k, val(k, where) || [], ph, k === 'target_roles' ? 5 : 25, tone)}</div>`).join('')}</div>
    <div class="hr"></div>
    <div class="grid-auto" style="grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;align-items:start">
      <div><div class="small b mb8">Work authorization &amp; visa notes</div>${textareaF(F, 'work_authorization', p.work_authorization, 'style="margin-top:0"')}
        <div class="row gap16 wrap mt10">${checkF(F, 'open_to_relocation', sp.open_to_relocation, 'Open to relocation')}${checkF(F, 'open_to_hybrid', sp.open_to_hybrid, 'Open to hybrid')}${checkF(F, 'open_to_contract', sp.open_to_contract, 'Open to contract')}</div>
        <div class="small b mb8 mt16">Master resume text</div>${textareaF(F, 'master_resume', p.master_resume, 'style="margin-top:0;min-height:120px;font-family:var(--mono);font-size:11.5px"')}
        <div class="helper">Uploading a file replaces this text with what we extract. For clean tailoring use the agent-friendly format: <span class="mono">## Summary</span>, <span class="mono">## Skills</span>, <span class="mono">## Experience</span>, one <span class="mono">### Title | Company | Location | Mon YYYY - Mon YYYY</span> line per role, <span class="mono">- </span> bullets, plain punctuation.</div></div>
      <div><div class="small b mb8">Master resume &amp; variants</div><div class="col gap8">
        ${D.resumes.map(d => `<div class="box row gap12 ${d.active ? 'brand' : ''}"><div class="avatar" style="width:34px;height:34px;border-radius:8px;font-size:10px;${d.active ? '' : 'background:var(--soft);color:var(--muted)'}">${esc((d.filename.split('.').pop() || '').toUpperCase().slice(0, 4))}</div><div class="grow"><div class="small b ellipsis">${esc(d.filename)}</div><div class="xs muted">${Math.max(1, Math.round(d.size / 1024))} KB · ${d.words} words${d.active ? ' · primary' : ''}</div></div>${d.active ? `<a class="btn sm" href="/api/profile/resume">Download</a>` : btn('Set primary', 'resume-primary', { id: d.id }, 'sm')}</div>`).join('')}
        <label class="btn block" style="border-style:dashed;color:var(--brand);border-radius:11px;padding:11px">${isBusy('upload') ? 'Uploading…' : `+ Upload ${D.resumes.length ? 'another' : 'a'} resume (PDF, DOCX, TXT · max ${BOOT.max_upload_mb} MB)`}<input type="file" id="resume-file" accept=".txt,.pdf,.docx" hidden></label></div>
        <div class="grid-auto mt12" style="grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px"><label class="field">LinkedIn${inputF(F, 'linkedin_url', p.linkedin_url)}</label><label class="field">GitHub${inputF(F, 'github_url', p.github_url)}</label></div></div>
    </div>
    <div class="row gap9 wrap mt20">${btn(label('profile', 'Save profile', 'Saving…'), 'save-profile', {}, 'primary', isBusy('profile'))}${btn(label('parse', 'Re-parse resume into evidence', 'Parsing…'), 'parse-evidence', {}, '', isBusy('parse'))}</div>
  </section>
  <section class="card" style="padding:20px"><div class="card-title mb4">AI models per stage</div><div class="card-sub mb16">Keys stay server-side.${pr.require_free ? ' Free-only guard is on for OpenRouter.' : ''}</div>
    <div class="grid-auto" style="grid-template-columns:1fr 1fr;gap:10px">
      <label class="field">Provider${selectF('ai', 'llm_provider', st.llm_provider, [['openrouter', 'OpenRouter'], ['openai', 'OpenAI'], ['anthropic', 'Anthropic'], ['mock', 'Mock / testing']])}</label>
      <label class="field">Default model${inputF('ai', 'model', st.model)}</label></div>
    <div class="col gap9 mt12">${pr.stage_models.map(s => `<div class="box row gap12" style="padding:10px 12px"><span class="small b none" style="width:74px">${esc(s.stage)}</span><span class="mono xs ink2 grow ellipsis" style="background:var(--line3);border-radius:6px;padding:4px 7px">${esc(s.model)}</span>${pill(s.badge, s.badge === 'free' ? 'good' : 'warn')}</div>`).join('')}</div>
    <div class="xs faint mt8">${Object.entries(st.configured).filter(([k]) => k !== 'mock').map(([k, v]) => `${k}: ${v ? 'key set' : 'no key'}`).join(' · ')}</div>
    <div class="hr" style="margin:16px 0"></div>
    <div class="base b mb4">Approval &amp; submission mode</div><div class="small muted mb10">How far the workflow may run without you.</div>
    <div class="col gap8">
      ${[['manual_approval', 'I approve every packet', 'Nothing reaches the apply kit without you.'], ['automatic', `Auto-approve above reviewer score ${fv('ai', 'min_review_score', st.min_review_score)}`, 'Packets skip the queue; submission stays your action.']].map(([id, l, d]) => `<label class="radio ${mode === id ? 'on' : ''}"><input type="radio" name="mode" data-form="ai" data-field="application_mode" value="${id}" ${mode === id ? 'checked' : ''}><span><span class="small b" style="display:block">${l}</span><span class="xs muted">${d}</span></span></label>`).join('')}
      <label class="radio disabled"><input type="radio" disabled><span><span class="small b" style="display:block">Auto-submit on supported portals</span><span class="xs muted">Not available by design — Job Hunt OS never submits, bypasses logins or solves CAPTCHAs. Approved packets become an apply kit.</span></span></label>
    </div>
    <label class="field mt12">Minimum reviewer score${inputF('ai', 'min_review_score', st.min_review_score, 'type="number" min="0" max="100"')}</label>
    ${btn(label('ai', 'Save AI settings', 'Saving…'), 'save-ai', {}, 'primary mt12', isBusy('ai'))}
  </section>
  <section class="card" style="padding:20px"><div class="card-title mb4">Cadence &amp; nudges</div><div class="card-sub mb16">What the run sheet asks of you each day.</div>
    <div class="col gap14">
      <label class="field strong">Weekly application target<input type="range" min="1" max="25" id="cad-weekly_target" data-form="cadence" data-field="weekly_target" data-live="1" value="${fv('cadence', 'weekly_target', pr.weekly_target)}" style="margin-top:8px"><span class="xs muted" style="font-weight:400">${fv('cadence', 'weekly_target', pr.weekly_target)} per week · about ${Math.max(1, Math.round(fv('cadence', 'weekly_target', pr.weekly_target) / 5))} a working day</span></label>
      <label class="field strong">Follow-up after${selectF('cadence', 'followup_days', pr.followup_days, [[5, '5 days of silence'], [7, '7 days of silence'], [10, '10 days of silence'], [14, '14 days of silence']])}</label>
      <div class="xs muted">Follow-up drafts are generated by <span class="mono">python -m app.cron</span> (or the Draft buttons) and never sent automatically. Daily searches run with <span class="mono">python -m app.cron searches</span>.</div>
      ${btn(label('cadence', 'Save cadence', 'Saving…'), 'save-cadence', {}, 'primary', isBusy('cadence'))}
    </div></section>
  </div>`;
}

/* ───────────── job modal + palette ───────────── */
function renderJobModal() {
  const j = D.jobModal;
  if (!j || j.id !== S.jobOpen) return `<div class="overlay" data-act="overlay-close"><div class="modal" style="padding:40px">${loading()}</div></div>`;
  const hereIdx = j.wizard.findIndex(w => !w.done);
  const actionable = ['discovered', 'shortlisted', 'maybe', 'archived', 'dismissed'].includes(j.status);
  let tab = '';
  if (S.jobTab === 'description') tab = `<div class="desc" style="font-size:13.5px;line-height:1.7;padding:20px 22px">${paragraphs(j.description, [...j.matches, ...j.keywords], [...j.gaps, ...j.missing])}
      <div class="row gap12 wrap xs faint" style="margin-top:20px;padding-top:16px;border-top:1px solid var(--line2)"><span class="row gap6"><span style="width:9px;height:9px;border-radius:3px;background:#9fe0d2"></span>matched to your evidence</span><span class="row gap6"><span style="width:9px;height:9px;border-radius:3px;background:#ffd79a"></span>gap — needs a real answer</span><span>source: ${esc(j.board)}${j.search_run_id ? ` · pulled in run #${j.search_run_id}` : ''}</span></div></div>`;
  else if (S.jobTab === 'requirements') tab = `<div style="padding:20px 22px" class="col gap14">${fitSummary(j)}${j.fit_analysis.must_have_missing?.length ? `<div class="box bad"><div class="xs b mb4" style="color:var(--bad)">MUST-HAVES MISSING</div><div class="small">${esc(j.fit_analysis.must_have_missing.join(' · '))}</div></div>` : ''}<div><div class="caps mb8">KEYWORDS</div><div class="row wrap gap6">${j.keywords.map(k => pill(k, 'plain', 'lg')).join('') || '<span class="small muted">Rank this job to extract keywords.</span>'}</div></div></div>`;
  else tab = `<div style="padding:20px 22px">${textareaF('jobnotes', j.id, j.notes, 'placeholder="Anything worth remembering about this role…" style="min-height:160px"')}${btn(label('jobnotes', 'Save notes', 'Saving…'), 'save-job-notes', { id: j.id }, 'primary sm mt10', isBusy('jobnotes'))}</div>`;
  return `<div class="overlay" data-act="overlay-close"><div class="modal" role="dialog" aria-label="Job detail">
    <div class="modal-head"><div class="row top-align gap16 wrap"><div class="avatar lg">${esc(initials(j.company))}</div>
      <div style="flex:1 1 240px;min-width:0"><div style="font-size:21px;font-weight:600;letter-spacing:-.02em">${esc(j.title)}</div><div class="muted mt4" style="font-size:13.5px">${esc(j.company)}${j.location ? ' · ' + esc(j.location) : ''} · ${esc(j.board)} · found ${esc(j.posted)}</div></div>
      <div class="row gap8 wrap"><div class="scorebox sm"><div class="n">${fitTxt(j.fit)}</div><div class="l">FIT</div></div>
        ${j.url ? `<a class="btn" href="${attr(j.url)}" target="_blank" rel="noopener">Open posting ↗</a>` : ''}
        ${btn(j.resume_id ? 'Open packet' : 'Tailor resume', 'modal-tailor', { id: j.id }, 'primary')}${btn('Close', 'close-job')}</div></div>
      <div class="wizard">${j.wizard.map((w, i) => `<div class="${w.done ? 'done' : i === hereIdx ? 'here' : ''}"><div class="row gap6"><span class="m">${w.done ? '✓' : i + 1}</span><span class="small b" style="color:${w.done ? 'var(--ink)' : i === hereIdx ? 'var(--brand-ink)' : 'var(--faint)'}">${w.label}</span></div><div class="xs muted mt4">${esc(i === hereIdx && !w.done ? 'next step' : w.meta)}</div></div>`).join('')}</div></div>
    <div class="split gap16" style="padding:18px 24px 24px">
      <section class="card flush" style="flex:1 1 420px;min-width:0"><div class="tabs under">${[['description', 'Description'], ['requirements', 'Requirements map'], ['notes', 'Notes']].map(([id, t]) => `<button class="${S.jobTab === id ? 'on' : ''}" data-act="job-tab" data-id="${id}">${t}</button>`).join('')}</div>${tab}</section>
      <div class="col gap14" style="flex:1 1 280px;min-width:0">
        <section class="card pad18"><div class="row gap8 mb14">${pill('AI', 'brand')}<span class="card-title sm">Fit breakdown</span></div>
          ${j.fit == null ? `<div class="small muted mb12">Not ranked yet.</div>${btn(label('rank' + j.id, 'Rank this job', 'Ranking…'), 'rank-job', { id: j.id }, 'soft', isBusy('rank' + j.id))}` : `
          <div class="row base gap8 mb14"><span class="mono bb" style="font-size:34px;color:var(--brand-ink);line-height:1">${fitTxt(j.fit)}</span><span class="small muted">${j.rank_pct != null ? `top ${j.rank_pct}% of your ranked jobs` : ''}</span></div>
          <div class="col gap10"><div><div class="row between small mb4"><span class="muted">Strong matches</span><span class="mono b">${j.matches.length}</span></div>${bar(Math.min(100, j.matches.length * 15) + '%', '#5b4ae8')}</div>
            <div><div class="row between small mb4"><span class="muted">Gaps flagged</span><span class="mono b">${j.gaps.length + j.missing.length}</span></div>${bar(Math.min(100, (j.gaps.length + j.missing.length) * 20) + '%', '#e0a136')}</div>
            <div><div class="row between small mb4"><span class="muted">Model confidence</span><span class="mono b">${Math.round((j.fit_analysis.confidence || 0) * 100)}</span></div>${bar(Math.round((j.fit_analysis.confidence || 0) * 100) + '%', '#a79bf0')}</div></div>
          ${j.summary ? `<div class="small ink2 mt12 pretty">${esc(j.summary)}</div>` : ''}`}</section>
        <section class="card pad18"><div class="card-title sm mb12">Close the gaps</div><div class="col gap10">${j.gap_notes.length ? j.gap_notes.map(g => g.partial
          ? `<div class="box"><div class="small b">${esc(g.gap)}</div><div class="small muted mt4" style="line-height:1.5">Partial: ${esc(g.partial)}</div></div>`
          : `<div class="box warn"><div class="small b" style="color:var(--warn-deep)">${esc(g.gap)}</div><div class="small mt4" style="color:var(--warn-deep);line-height:1.5">No verified evidence. Add a claim or leave it out — tailoring never invents experience.</div>${btn('Add evidence', 'add-evidence-for', { gap: g.gap }, 'sm mt8')}</div>`).join('') : '<div class="small muted">No gaps flagged.</div>'}</div></section>
        <section class="card pad18"><div class="card-title sm mb10">Move this job</div><div class="row gap6 wrap">${actionable
          ? `${btn('Shortlist', 'modal-triage', { id: j.id, action: 'shortlist' }, 'sm soft')}${btn('Maybe later', 'modal-triage', { id: j.id, action: 'maybe' }, 'sm')}${btn('Dismiss', 'modal-triage', { id: j.id, action: 'dismiss' }, 'sm danger')}`
          : `<span class="small muted">This job is ${esc(j.status.replace('_', ' '))} — manage it from ${j.status === 'applied' ? 'the Tracker' : 'Approvals'}.</span>`}</div></section>
      </div></div></div></div>`;
}
function renderPalette() {
  const q = S.paletteQuery;
  const item = (key, text, meta, act, data = {}) => `<button class="pal-item" data-act="${act}" ${Object.entries(data).map(([k, v]) => `data-${k}="${attr(v)}"`).join(' ')}><span class="k">${key}</span><span class="grow ellipsis">${esc(text)}</span><span class="xs faint none">${esc(meta)}</span></button>`;
  const c = D.summary?.counts || {};
  return `<div class="overlay" data-act="overlay-close"><div class="palette" role="dialog" aria-label="Command palette">
    <div class="row gap10" style="padding:15px 18px;border-bottom:1px solid var(--line2)"><span class="mono xs faint">⌘K</span><input class="input bare grow" style="font-size:15px" id="palette-q" data-palette="1" value="${attr(q)}" placeholder="Jump to a job, company or action…" autocomplete="off">${btn('esc', 'close-palette', {}, 'sm')}</div>
    <div style="padding:10px 0 12px;max-height:55vh;overflow:auto">
      ${q ? `<div class="row between" style="padding:8px 18px"><span class="caps-sm faint">JOBS</span><span class="tiny faint">${S.paletteTotal ?? 0} match</span></div>${S.paletteJobs.map(j => item('↳', `${j.company} — ${j.title}`, `fit ${fitTxt(j.fit)} · ${j.location || j.board}`, 'palette-job', { id: j.id })).join('') || '<div class="small muted" style="padding:6px 18px">No jobs match.</div>'}` : ''}
      <div class="caps-sm faint" style="padding:8px 18px">JUMP TO</div>
      ${item('O', 'Overview', 'pipeline health', 'palette-go', { screen: 'overview' })}${item('T', 'Today · run sheet', `${c.today ?? 0} actions`, 'palette-go', { screen: 'today', variant: 'run' })}${item('D', 'Discovery · triage queue', `${c.discovery ?? 0} new`, 'palette-go', { screen: 'discovery', mode: 'triage' })}${item('A', 'Approvals', `${c.approvals ?? 0} waiting`, 'palette-go', { screen: 'approvals' })}${item('R', 'Tracker', `${c.tracker ?? 0} live`, 'palette-go', { screen: 'tracker' })}
      <div class="caps-sm faint" style="padding:14px 18px 8px;border-top:1px solid var(--line3);margin-top:6px">ACTIONS</div>
      ${item('N', 'Run the saved search now', 'search profile', 'palette-run')}${item('K', 'Open the apply kit for the latest approved packet', '', 'palette-kit')}${item('F', 'Work the follow-ups that are due', `${D.today?.followups?.length ?? ''}`, 'palette-go', { screen: 'today' })}${item('+', 'Add a job from a URL', 'manual', 'palette-go', { screen: 'discovery', mode: 'setup' })}
    </div><div class="xs muted" style="padding:10px 18px;border-top:1px solid var(--line2);background:#fbfbfe">Search every job in the pipeline, jump between screens, or fire an action without leaving the keyboard.</div></div></div>`;
}

/* ───────────── render ───────────── */
const RENDER = { overview: renderOverview, today: renderToday, discovery: renderDiscovery, tailor: renderTailor, approvals: renderApprovals, tracker: renderTracker, knowledge: renderKnowledge, settings: renderSettings };
function render() {
  const active = document.activeElement;
  const fid = active && active.id;
  let selRange = null;
  try { if (fid && typeof active.selectionStart === 'number') selRange = [active.selectionStart, active.selectionEnd]; } catch { /* not a text field */ }
  $('#nav').innerHTML = renderNav();
  $('#side-foot').innerHTML = renderSideFoot();
  if (D.summary) $('#side-meta').textContent = `${D.summary.model || D.summary.llm_provider} · ${D.summary.application_mode.replace('_', ' ')}`;
  if (D.summary?.profile) $('#brand-sub').textContent = [D.summary.profile.name, D.summary.profile.headline].filter(Boolean).join(' · ') || 'Set up your profile';
  $('#top').innerHTML = renderTop();
  $('#view').innerHTML = RENDER[S.screen]();
  $('#overlays').innerHTML = (S.jobOpen ? renderJobModal() : '') + (S.kitJob ? renderKit() : '') + (S.paletteOpen ? renderPalette() : '');
  document.body.style.overflow = (S.jobOpen || S.kitJob || S.paletteOpen) ? 'hidden' : '';
  if (fid) {
    const el = document.getElementById(fid);
    if (el) { el.focus({ preventScroll: true }); if (selRange) { try { el.setSelectionRange(...selRange); } catch { /* ignore */ } } }
  }
}

/* ───────────── actions ───────────── */
async function openJob(id) {
  S.jobOpen = Number(id); S.jobTab = 'description'; render();
  try { D.jobModal = await api(`/api/jobs/${id}`); } catch (e) { toast(e.message, true); S.jobOpen = null; }
  render();
}
async function openKit(id) {
  S.kitJob = Number(id); D.kit = null; render();
  try { D.kit = await api(`/api/jobs/${id}/apply-kit`); } catch (e) { toast(e.message, true); S.kitJob = null; }
  render();
}
async function triageJob(id, action, thenTailor) {
  try {
    await api(`/api/jobs/${id}/triage`, { method: 'POST', body: { action } });
    if (S.screen === 'discovery' && S.discMode === 'triage') S.triagedThisSession++;
    if (thenTailor) return go('tailor', { tailorJob: Number(id), tailorTab: 'diff' });
    toast({ shortlist: 'Shortlisted', maybe: 'Parked for later', skip: 'Skipped', dismiss: 'Dismissed' }[action]);
    await load();
  } catch (e) { toast(e.message, true); }
}
async function pollRuns() {
  const p = S.polling; if (!p) return;
  try {
    const runs = await Promise.all(p.ids.map(id => api(`/api/search/apify/runs/${id}`)));
    const done = runs.filter(r => r.completed).length;
    const created = runs.reduce((a, r) => a + (r.created_jobs || 0), 0);
    p.text = `${done} of ${runs.length} searches complete · ${created} new jobs so far`;
    if (done < runs.length) { render(); setTimeout(pollRuns, 4000); return; }
    const failed = runs.filter(r => r.status !== 'SUCCEEDED');
    if (D.searchProfile?.profile?.auto_rank) {
      for (const r of runs.filter(x => x.status === 'SUCCEEDED')) {
        for (let guard = 0; guard < 20; guard++) {
          p.text = `Ranking new jobs from run #${r.id}…`; render();
          const out = await api(`/api/search/apify/runs/${r.id}/rank`, { method: 'POST' });
          if (out.errors.length && !out.ranked) { toast(`Ranking stopped: ${out.errors[0]}`, true); break; }
          if (!out.remaining || !out.ranked) break;
        }
      }
    }
    S.polling = null; D.runs = null;
    toast(failed.length ? `${failed.length} search${failed.length === 1 ? '' : 'es'} failed. Completed results were kept.` : `Discovery complete · ${created} new jobs`, !!failed.length);
    go('discovery', { discMode: 'triage', triagedThisSession: 0 });
  } catch (e) { toast(`Could not refresh search status: ${e.message}`, true); S.polling = null; render(); }
}
const FANOUT = { role_location: 'one run per role × location', location: 'one run per location', role: 'one run per role', once: 'one run for everything' };
const COUNTRIES = ['usa', 'uk', 'canada', 'australia', 'india', 'singapore', 'uae', 'germany', 'france', 'netherlands', 'ireland', 'spain', 'italy', 'japan', 'brazil', 'mexico', 'south africa', 'new zealand'];
const DEFAULT_TEMPLATE = JSON.stringify({ searchTerm: '{{role}}', location: '{{location}}', maxItems: '{{max_results}}' }, null, 2);
function addActorForm() {
  return `<div class="box brand mt10" style="padding:14px"><div class="small b mb4">Add an Apify actor</div>
    <div class="xs muted mb8">Any Store actor that returns job listings. Items are normalized automatically — title, company, location, URL and description.</div>
    <div class="row gap8 wrap">${inputF('ca', 'id', '', 'placeholder="username/actor-name" style="flex:1 1 220px;margin-top:0"')}${btn(label('lookup', 'Look up on Apify', 'Looking up…'), 'lookup-actor', {}, 'sm', isBusy('lookup'))}</div>
    ${inputF('ca', 'name', '', 'placeholder="Display name"')}
    <label class="field mt8">Runs${selectF('ca', 'fan_out', 'role_location', Object.entries(FANOUT).map(([k, v]) => [k, v[0].toUpperCase() + v.slice(1)]))}</label>
    <label class="field mt8">Input template (JSON)${textareaF('ca', 'input_template', DEFAULT_TEMPLATE, 'style="font-family:var(--mono);font-size:11.5px;min-height:130px"')}</label>
    <div class="helper">Placeholders: ${(D.actors?.placeholders || []).map(x => `<span class="mono">{{${x}}}</span>`).join(' ')}. A value that is only a placeholder keeps its type (number, list, true/false).</div>
    ${S.caFields ? `<div class="xs muted mt8">This actor accepts: ${S.caFields.map(f => `<span class="mono">${esc(f.name)}${f.required ? '*' : ''}</span>`).join(', ') || 'no documented inputs'}</div>` : ''}
    ${btn('Add actor', 'add-actor', {}, 'primary sm mt10')}</div>`;
}
function searchPayload() {
  const p = D.searchProfile.profile, F = 'sp', v = (k, d) => fv(F, k, p[k] ?? d);
  return {
    roles: v('roles', []), locations: v('locations', []), actors: v('actors', []), custom_actors: v('custom_actors', []),
    sites: v('sites', []), remote_platforms: v('remote_platforms', []), remoteok_url: v('remoteok_url', ''),
    hours_old: Number(v('hours_old', 168)) || 0, max_results: Number(v('max_results', 20)) || 20, offset: Number(v('offset', 0)) || 0,
    job_type: v('job_type', ''), seniority: v('seniority', []), country_indeed: v('country_indeed', 'usa'),
    distance: Number(v('distance', 50)) || 0, is_remote: !!v('is_remote', false), easy_apply: !!v('easy_apply', false),
    fetch_description: !!v('fetch_description', true), enforce_annual_salary: !!v('enforce_annual_salary', false),
    residential_proxy: !!v('residential_proxy', true), salary_only: !!v('salary_only', false), visa_sponsorship: !!v('visa_sponsorship', false),
    exclude_keywords: v('exclude_keywords', []), linkedin_company_ids: v('linkedin_company_ids', []),
    skip_agencies: !!v('skip_agencies', true), auto_rank: !!v('auto_rank', true), schedule: v('schedule', 'manual'),
  };
}
let previewTimer;
function schedulePreview() {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(async () => {
    if (!D.searchProfile) return;
    try { D.spPreview = await api('/api/search/profile/preview', { method: 'POST', body: searchPayload() }); }
    catch (e) { D.spPreview = { runs: [], warnings: [], error: e.message }; }
    if (S.screen === 'discovery' && S.discMode === 'setup') render();
  }, 350);
}
function toggleIn(key, value) {
  const list = [...(fv('sp', key, D.searchProfile.profile[key] || []) || [])];
  const i = list.indexOf(value); i >= 0 ? list.splice(i, 1) : list.push(value);
  setF('sp', key, list); render();
}
async function saveSearchProfile() {
  const pending = document.getElementById('sp-roles')?.value.trim(); if (pending) addTags('sp', 'roles', pending, 5, D.searchProfile.profile.roles);
  const pendingLoc = document.getElementById('sp-locations')?.value.trim(); if (pendingLoc) addTags('sp', 'locations', pendingLoc, 5, D.searchProfile.profile.locations);
  const out = await api('/api/search/profile', { method: 'PUT', body: searchPayload() });
  D.searchProfile.profile = out.profile; delete S.forms.sp;
  S.triage.hide_agencies = out.profile.skip_agencies; S.triage.hide_reposts = out.profile.skip_agencies;
  return out;
}
async function refreshAfter(msg) { if (msg) toast(msg); await load(); }

const A = {
  go: el => go(el.dataset.screen, {
    ...(el.dataset.mode ? { discMode: el.dataset.mode } : {}), ...(el.dataset.job ? { tailorJob: Number(el.dataset.job) } : {}),
    ...(el.dataset.approval ? { approvalJob: Number(el.dataset.approval) } : {}), ...(el.dataset.app ? { selApp: Number(el.dataset.app), trackerView: 'board' } : {}),
    jobOpen: null, kitJob: null, paletteOpen: false,
  }),
  variant: el => { if (el.dataset.kind === 'today') S.todayVariant = el.dataset.id; else { S.discMode = el.dataset.id; S.manualMissing = null; } go(S.screen); },
  'go-triage': () => go('discovery', { discMode: 'triage' }),
  'do-action': el => {
    const { kind, job, app } = el.dataset;
    if (kind === 'approve') go('approvals', { approvalJob: Number(job) });
    else if (kind === 'tailor') go('tailor', { tailorJob: Number(job) });
    else go('tracker', { selApp: Number(app), trackerView: 'board' });
  },
  'draft-nudge': el => busy('nudge' + el.dataset.app, async () => {
    await api(`/api/applications/${el.dataset.app}/followup-draft`, { method: 'POST' });
    if (el.dataset.stay) await load(); else go('tracker', { selApp: Number(el.dataset.app), trackerView: 'board' });
  }, 'Follow-up draft ready — review it before sending'),
  palette: () => { S.paletteOpen = true; S.paletteQuery = ''; S.paletteJobs = []; render(); setTimeout(() => document.getElementById('palette-q')?.focus(), 0); },
  'close-palette': () => { S.paletteOpen = false; render(); },
  'palette-go': el => { S.paletteOpen = false; go(el.dataset.screen, { ...(el.dataset.mode ? { discMode: el.dataset.mode } : {}), ...(el.dataset.variant ? { todayVariant: el.dataset.variant } : {}) }); },
  'palette-job': el => { S.paletteOpen = false; openJob(el.dataset.id); },
  'palette-run': () => { S.paletteOpen = false; go('discovery', { discMode: 'setup' }); setTimeout(() => A['run-search'](), 300); },
  'palette-kit': async () => {
    S.paletteOpen = false;
    const list = await api('/api/approvals').catch(() => []);
    const approved = list.find(a => a.status === 'approved');
    if (approved) openKit(approved.id); else { toast('No approved packet is waiting for submission', true); render(); }
  },
  'overlay-close': (el, e) => { if (e.target !== el) return; S.jobOpen = null; S.kitJob = null; S.paletteOpen = false; render(); },
  'open-job': el => openJob(el.dataset.id),
  'close-job': () => { S.jobOpen = null; render(); },
  'job-tab': el => { S.jobTab = el.dataset.id; render(); },
  'modal-tailor': async el => {
    const id = Number(el.dataset.id), j = D.jobModal;
    if (!j.resume_id && ['discovered', 'shortlisted', 'maybe', 'archived', 'dismissed'].includes(j.status)) await api(`/api/jobs/${id}/triage`, { method: 'POST', body: { action: 'shortlist' } }).catch(e => toast(e.message, true));
    S.jobOpen = null;
    if (j.status === 'approved') go('approvals', { approvalJob: id });
    else if (j.status === 'applied') go('tracker', { selApp: j.application_id });
    else go('tailor', { tailorJob: id });
  },
  'modal-triage': async el => { await triageJob(el.dataset.id, el.dataset.action); if (S.jobOpen) openJob(S.jobOpen); },
  'save-job-notes': el => busy('jobnotes', () => api(`/api/jobs/${el.dataset.id}/notes`, { method: 'PUT', body: { notes: fv('jobnotes', el.dataset.id, D.jobModal.notes) } }), 'Notes saved'),
  'add-evidence-for': el => { S.jobOpen = null; S.evidencePrefill = `${el.dataset.gap}: `; go('knowledge'); },
  'rank-job': el => busy('rank' + el.dataset.id, async () => {
    const out = await api(`/api/jobs/${el.dataset.id}/rank`, { method: 'POST' });
    if (S.jobOpen) D.jobModal = await api(`/api/jobs/${S.jobOpen}`);
    await load(); return out;
  }, out => `Ranked · fit ${Math.round(out.score)}`),
  triage: el => triageJob(el.dataset.id, el.dataset.action, !!el.dataset.tailor),
  // table
  'select-job': async (el, e) => {
    if (e.target.matches('input[type=checkbox]')) return;
    S.selJob = Number(el.dataset.id); render();
    D.selDetail = await api(`/api/jobs/${S.selJob}`).catch(() => null); render();
  },
  check: (el, e) => { e.stopPropagation(); const id = Number(el.dataset.id); S.checked.has(id) ? S.checked.delete(id) : S.checked.add(id); render(); },
  'check-all': el => { const ids = D.jobs.items.map(r => r.id); const all = ids.every(id => S.checked.has(id)); ids.forEach(id => all ? S.checked.delete(id) : S.checked.add(id)); render(); },
  bulk: el => busy('bulk', async () => {
    const out = await api('/api/jobs/bulk-triage', { method: 'POST', body: { ids: [...S.checked], action: el.dataset.action } });
    S.checked.clear(); await load(); return out;
  }, out => `${out.changed} job${out.changed === 1 ? '' : 's'} updated`),
  'reset-filters': (el, e) => { e.preventDefault(); S.filters = { q: '', min_fit: 0, board: 'all', stage: 'all', sort: 'fit', page: 1, per_page: S.filters.per_page }; load(); },
  page: el => { S.filters.page = Number(el.dataset.page); load(); },
  // setup
  'focus-tag': (el, e) => { if (e.target === el) document.getElementById(el.dataset.target)?.focus(); },
  'tag-remove': (el, e) => {
    e.stopPropagation();
    const { form, key } = el.dataset;
    if (form === 'triage') { S.triage.keywords.splice(Number(el.dataset.index), 1); load(); return; }
    const list = [...(fv(form, key, tagFallback(form, key)) || [])]; list.splice(Number(el.dataset.index), 1); setF(form, key, list); render();
  },
  'suggest-role': (el, e) => { e.preventDefault(); addTags('sp', 'roles', el.dataset.role, 5, D.searchProfile.profile.roles); render(); },
  'toggle-site': el => toggleIn('sites', el.dataset.site),
  'toggle-actor': el => toggleIn('actors', el.dataset.actor),
  'toggle-list': el => toggleIn(el.dataset.key, el.dataset.value),
  'toggle-inputs': () => { S.showInputs = !S.showInputs; render(); },
  'toggle-add-actor': (el, e) => { e.preventDefault(); S.addActor = !S.addActor; S.caFields = null; render(); },
  'remove-actor': (el, e) => {
    e.preventDefault(); e.stopPropagation();
    const p = D.searchProfile.profile, id = el.dataset.actor;
    setF('sp', 'custom_actors', fv('sp', 'custom_actors', p.custom_actors || []).filter(c => c.id !== id));
    setF('sp', 'actors', fv('sp', 'actors', p.actors || []).filter(a => a !== id));
    render(); toast('Actor removed — save the profile to keep the change');
  },
  'lookup-actor': () => busy('lookup', async () => {
    const id = (fv('ca', 'id', '') || '').trim();
    if (!/^[\w.-]+[/~][\w.-]+$/.test(id)) throw new Error('Enter the actor as username/actor-name');
    const out = await api('/api/search/actors/lookup', { method: 'POST', body: { id } });
    setF('ca', 'id', out.id); setF('ca', 'name', out.name);
    if (Object.keys(out.input_template).length) setF('ca', 'input_template', JSON.stringify(out.input_template, null, 2));
    S.caFields = out.fields;
    return out;
  }, out => `Found ${out.name} · ${out.fields.length} documented inputs`),
  'add-actor': () => {
    const f = S.forms.ca || {}, p = D.searchProfile.profile, id = (f.id || '').trim();
    if (!/^[\w.-]+[/~][\w.-]+$/.test(id)) return toast('Enter the actor as username/actor-name', true);
    if ((D.actors?.actors || []).some(a => !a.custom && a.id === id)) return toast('That actor is already built in — just tick it', true);
    let template;
    try { template = JSON.parse(f.input_template ?? DEFAULT_TEMPLATE); } catch { return toast('The input template is not valid JSON', true); }
    if (!template || typeof template !== 'object' || Array.isArray(template)) return toast('The input template must be a JSON object', true);
    const customs = fv('sp', 'custom_actors', p.custom_actors || []).filter(c => c.id !== id);
    customs.push({ id, name: (f.name || '').trim() || id, fan_out: f.fan_out || 'role_location', input_template: template });
    setF('sp', 'custom_actors', customs);
    const selected = fv('sp', 'actors', p.actors || []);
    if (!selected.includes(id)) setF('sp', 'actors', [...selected, id]);
    delete S.forms.ca; S.addActor = false; S.caFields = null;
    render(); toast('Actor added — save the profile to keep it');
  },
  'save-search': () => busy('saveSp', saveSearchProfile, 'Search profile saved'),
  'run-search': () => busy('run', async () => {
    if (!D.searchProfile) D.searchProfile = await api('/api/search/profile');
    if (S.screen === 'discovery' && S.discMode === 'setup') await saveSearchProfile();
    const out = await api('/api/search/profile/run', { method: 'POST' });
    const actorCount = new Set(out.runs.map(r => r.actor_id)).size;
    S.polling = { ids: out.runs.map(r => r.id), text: `Started ${out.runs.length} run${out.runs.length === 1 ? '' : 's'} on ${actorCount} actor${actorCount === 1 ? '' : 's'}. Waiting for results…` };
    setTimeout(pollRuns, 3000);
    return out;
  }, out => out.warnings?.length ? `Started · note: ${out.warnings[0]}` : null),
  'select-run': async el => { S.runSel = Number(el.dataset.id); render(); D.runDetail = await api(`/api/search/apify/runs/${S.runSel}/detail`).catch(e => { toast(e.message, true); return null; }); render(); },
  'add-manual': () => busy('manual', async () => {
    const m = S.forms.manual || {};
    try {
      const out = await api('/api/jobs/from-url', { method: 'POST', body: { url: (m.url || '').trim(), title: m.title || '', company: m.company || '', location: m.location || '', description: m.description || '' } });
      delete S.forms.manual; S.manualMissing = null;
      toast(out.created ? 'Added to your triage queue' : 'That job was already in the pipeline — details refreshed');
      openJob(out.job.id);
    } catch (e) {
      if (e.status === 422 && e.detail?.extracted) {
        S.manualMissing = e.detail.message;
        for (const [k, v] of Object.entries(e.detail.extracted)) if (v && !m[k]) setF('manual', k, v);
        return;
      }
      throw e;
    }
  }),
  'import-board': () => busy('board', async () => {
    const kind = fv('board', 'kind', 'greenhouse'), token = (fv('board', 'token', '') || '').trim();
    if (!/^[\w.-]+$/.test(token)) throw new Error('Enter the board token — letters, numbers, dots or dashes');
    const out = await api(`/api/import/${kind}/${encodeURIComponent(token)}`, { method: 'POST' });
    delete S.forms.board; await load(); return out;
  }, out => `Imported ${out.created} new of ${out.seen} open roles — they are in your triage queue`),
  'rank-batch': () => busy('rankBatch', async () => {
    const out = await api('/api/jobs/rank-batch', { method: 'POST' });
    const failed = out.filter(r => r.error);
    if (!out.length) throw new Error('Every job is already ranked');
    if (failed.length === out.length) throw new Error(`Ranking failed: ${failed[0].error}`);
    await load(); return { ranked: out.length - failed.length, failed: failed.length };
  }, r => `Ranked ${r.ranked} job${r.ranked === 1 ? '' : 's'}${r.failed ? ` · ${r.failed} failed` : ''}`),
  // tailor
  'pick-tailor': el => go('tailor', { tailorJob: Number(el.value) }),
  'tailor-tab': el => { S.tailorTab = el.dataset.id; S.editing = null; render(); },
  generate: el => busy('tailor' + el.dataset.id, async () => {
    const out = await api(`/api/jobs/${el.dataset.id}/tailor`, { method: 'POST' });
    await load(); return out;
  }, out => out.auto_approved ? `Packet scored ${Math.round(out.reviewer_score)} and was auto-approved` : `Packet ready · reviewer ${Math.round(out.reviewer_score)}`),
  'skip-tailor': el => busy('skip' + el.dataset.id, async () => {
    const out = await api(`/api/jobs/${el.dataset.id}/skip-tailoring`, { method: 'POST' });
    S.tailorTab = 'resume';
    await load(); return out;
  }, out => out.auto_approved ? `Master resume scored ${Math.round(out.reviewer_score)} and was auto-approved` : `Tailoring skipped · master resume reviewed at ${Math.round(out.reviewer_score)}`),
  'edit-start': el => { S.editing = Number(el.dataset.index); render(); document.getElementById(`edit-text-${el.dataset.index}`)?.focus(); },
  'edit-cancel': () => { S.editing = null; render(); },
  // Read typed values before busy() re-renders and replaces the inputs.
  'edit-save': el => { const text = document.getElementById(`edit-text-${el.dataset.index}`).value; return busy('edit', async () => {
    D.packet = await api(`/api/resumes/${el.dataset.resume}/edits/${el.dataset.index}`, { method: 'PATCH', body: { text, status: 'accepted' } });
    S.editing = null;
  }, 'Edit saved'); },
  'edit-status': el => busy('edit', async () => {
    D.packet = await api(`/api/resumes/${el.dataset.resume}/edits/${el.dataset.index}`, { method: 'PATCH', body: { status: el.dataset.status } });
  }),
  'save-cover': el => busy('cover', async () => {
    await api(`/api/resumes/${el.dataset.resume}/cover-letter`, { method: 'PUT', body: { text: fv('cover' + el.dataset.resume, 'text', D.packet.resume.cover_letter) } });
    delete S.forms['cover' + el.dataset.resume]; await load();
  }, 'Cover letter saved'),
  // approvals
  'select-approval': async el => { S.approvalJob = Number(el.dataset.id); render(); D.approval = await api(`/api/approvals/${S.approvalJob}`).catch(() => null); render(); },
  approve: el => busy('approve', async () => {
    const out = await api(`/api/jobs/${el.dataset.id}/approve`, { method: 'POST' });
    await load(); openKit(el.dataset.id); return out;
  }, 'Approved — here is your apply kit'),
  revise: el => busy('revise', async () => {
    await api(`/api/jobs/${el.dataset.id}/revision`, { method: 'POST', body: { notes: fv('approvalNotes', el.dataset.id, D.approval?.notes || '') } });
    await load();
  }, 'Sent back for revision'),
  'open-kit': el => openKit(el.dataset.id),
  'close-kit': () => { S.kitJob = null; render(); },
  'copy-answer': el => { copyText(el.dataset.text); api(`/api/knowledge/${el.dataset.id}/used`, { method: 'POST' }).catch(() => {}); },
  'mark-applied': el => busy('applied', async () => {
    const k = S.forms.kit || {};
    await api(`/api/jobs/${el.dataset.id}/applied`, { method: 'POST', body: { recruiter_name: k.recruiter_name || '', recruiter_email: k.recruiter_email || '', notes: '' } });
    delete S.forms.kit; S.kitJob = null;
    const tracker = await api('/api/tracker');
    const app = tracker.applications.find(a => a.job_id === Number(el.dataset.id));
    go('tracker', { selApp: app?.id, trackerView: 'board' });
  }, 'Marked applied · follow-up scheduled'),
  // tracker
  'tracker-view': el => { S.trackerView = el.dataset.id; render(); },
  'select-app': el => { S.selApp = Number(el.dataset.id); S.editingRecruiter = false; S.scheduling = false; if (el.dataset.board) S.trackerView = 'board'; render(); },
  'move-stage': el => moveStage(el.dataset.app, el.dataset.stage),
  'add-stage': el => busy('stages', async () => { D.tracker = await api('/api/tracker/stages', { method: 'PUT', body: { extra_stages: [...D.tracker.extra_stages, el.dataset.id] } }); }),
  'remove-stage': () => busy('stages', async () => {
    const extras = D.tracker.extra_stages.slice(0, -1), removed = D.tracker.extra_stages.at(-1);
    if (D.tracker.applications.some(a => a.stage === removed)) throw new Error('Move the applications out of that stage first');
    D.tracker = await api('/api/tracker/stages', { method: 'PUT', body: { extra_stages: extras } });
  }),
  'log-event': el => {
    const text = document.getElementById('log-text').value.trim(), kind = document.getElementById('log-kind').value;
    if (!text) return toast('Write something to log', true);
    return busy('log', async () => {
      await api(`/api/applications/${el.dataset.app}/events`, { method: 'POST', body: { kind, title: kind[0].toUpperCase() + kind.slice(1), detail: text } });
      await load();
    }, 'Logged');
  },
  'edit-recruiter': (el, e) => { e.preventDefault(); S.editingRecruiter = true; render(); },
  'edit-recruiter-cancel': () => { S.editingRecruiter = false; render(); },
  'save-recruiter': el => busy('recruiter', async () => {
    const sel = D.tracker.applications.find(a => a.id === Number(el.dataset.app)), F = 'rec' + el.dataset.app;
    await api(`/api/applications/${el.dataset.app}/recruiter`, { method: 'PUT', body: { recruiter_name: fv(F, 'recruiter_name', sel.recruiter_name), recruiter_email: fv(F, 'recruiter_email', sel.recruiter_email) } });
    delete S.forms[F]; S.editingRecruiter = false; await load();
  }, 'Recruiter saved'),
  'copy-draft': el => copyText(D.tracker.applications.find(a => a.id === Number(el.dataset.app)).draft),
  'schedule-start': () => { S.scheduling = true; render(); document.getElementById('iv-title')?.focus(); },
  'schedule-cancel': () => { S.scheduling = false; render(); },
  'schedule-save': el => {
    const title = document.getElementById('iv-title').value.trim() || 'Interview', at = document.getElementById('iv-at').value;
    if (!at) return toast('Pick a date and time', true);
    return busy('schedule', async () => {
      await api(`/api/applications/${el.dataset.app}/events`, { method: 'POST', body: { kind: 'interview', title, detail: 'Scheduled', occurred_at: new Date(at).toISOString() } });
      S.scheduling = false; await load();
    }, 'Interview scheduled');
  },
  // knowledge
  'kn-edit': el => { S.editKnowledge = Number(el.dataset.id); render(); },
  'kn-cancel': () => { delete S.forms['kn' + S.editKnowledge]; S.editKnowledge = null; render(); },
  'kn-save': el => busy('kn', async () => {
    const id = el.dataset.id, F = 'kn' + (id || 'new'), cur = (D.knowledge || []).find(a => String(a.id) === id) || {};
    const body = { question: fv(F, 'question', cur.question || ''), answer: fv(F, 'answer', cur.answer || ''), category: fv(F, 'category', cur.category || 'general'), answer_type: fv(F, 'answer_type', cur.answer_type || 'text'), verified: fv(F, 'verified', id ? cur.verified : true), sensitive: fv(F, 'sensitive', !!cur.sensitive) };
    if (!body.question.trim()) throw new Error('Write the question first');
    await api(id ? `/api/knowledge/${id}` : '/api/knowledge', { method: id ? 'PUT' : 'POST', body });
    delete S.forms[F]; S.editKnowledge = null; await load();
  }, 'Answer saved'),
  'kn-delete': async el => { if (!confirm('Archive this answer? It will no longer be offered in apply kits.')) return; await api(`/api/knowledge/${el.dataset.id}`, { method: 'DELETE' }).catch(e => toast(e.message, true)); refreshAfter('Answer archived'); },
  'ev-add': () => busy('ev', async () => {
    const f = S.forms.ev || {};
    const claim = (f.claim ?? S.evidencePrefill ?? '').trim();
    if (!claim) throw new Error('Describe the claim first');
    await api('/api/evidence', { method: 'POST', body: { claim, company: f.company || '', role: f.role || '', skills: (f.skills || '').split(',').map(s => s.trim()).filter(Boolean), verified: true } });
    delete S.forms.ev; S.evidencePrefill = ''; await load();
  }, 'Evidence added'),
  'ev-verify': el => busy('evv', async () => { await api(`/api/evidence/${el.dataset.id}`, { method: 'PATCH', body: { verified: true } }); await load(); }, 'Verified — tailoring may now cite it'),
  'ev-delete': async (el, e) => { e.preventDefault(); if (!confirm('Remove this evidence claim?')) return; await api(`/api/evidence/${el.dataset.id}`, { method: 'DELETE' }).catch(err => toast(err.message, true)); refreshAfter('Evidence removed'); },
  // settings
  'save-profile': () => busy('profile', async () => {
    const p = D.profile, sp = p.search_preferences || {}, F = 'profile';
    for (const [key, , , , , where] of CHIP_FIELDS) {
      const pending = document.getElementById(`${F}-${key}`)?.value.trim();
      if (pending) addTags(F, key, pending, key === 'target_roles' ? 5 : 25, where === 'sp' ? sp[key] || [] : p[key] || []);
    }
    const spKeys = ['total_experience', 'notice_period', 'open_to_relocation', 'open_to_hybrid', 'open_to_contract', ...CHIP_FIELDS.filter(c => c[5] === 'sp').map(c => c[0])];
    const nextSp = { ...sp }; spKeys.forEach(k => { nextSp[k] = fv(F, k, sp[k] ?? (k.startsWith('open_') ? false : k.endsWith('_period') || k === 'total_experience' ? '' : [])); });
    const top = ['name', 'headline', 'email', 'phone', 'location', 'compensation_notes', 'work_authorization', 'master_resume', 'linkedin_url', 'github_url', 'target_roles', 'preferred_locations'];
    const body = { ...p, search_preferences: nextSp }; top.forEach(k => { body[k] = fv(F, k, p[k]); });
    await api('/api/profile', { method: 'PUT', body });
    delete S.forms[F]; await load();
  }, 'Profile saved'),
  'parse-evidence': () => busy('parse', () => api('/api/profile/resume/parse-evidence', { method: 'POST' }), out => `${out.added} new claim${out.added === 1 ? '' : 's'} added as unverified — verify them in the Knowledge bank`),
  'resume-primary': el => busy('primary', async () => { D.resumes = await api(`/api/profile/resumes/${el.dataset.id}/primary`, { method: 'POST' }); await load(); }, 'Primary resume updated'),
  'save-ai': () => busy('ai', async () => {
    const st = D.settings;
    const body = { llm_provider: fv('ai', 'llm_provider', st.llm_provider), model: fv('ai', 'model', st.model), application_mode: fv('ai', 'application_mode', st.application_mode), min_review_score: Number(fv('ai', 'min_review_score', st.min_review_score)) };
    if (body.application_mode === 'automatic' && st.application_mode !== 'automatic' && !confirm('Auto-approval skips the manual queue once a resume passes review. Submitting stays your action. Continue?')) return;
    await api('/api/settings', { method: 'PUT', body }); delete S.forms.ai; await load();
  }, 'AI settings saved'),
  'save-cadence': () => busy('cadence', async () => {
    const pr = D.prefs;
    await api('/api/preferences', { method: 'PUT', body: { weekly_target: Number(fv('cadence', 'weekly_target', pr.weekly_target)), followup_days: Number(fv('cadence', 'followup_days', pr.followup_days)), email_run_sheet: !!pr.email_run_sheet, warn_closing: pr.warn_closing !== false } });
    delete S.forms.cadence; await load();
  }, 'Cadence saved'),
};
function tagFallback(form, key) {
  if (form === 'sp') return D.searchProfile?.profile?.[key] || [];
  if (form === 'profile') { const c = CHIP_FIELDS.find(x => x[0] === key); return (c && c[5] === 'sp' ? D.profile?.search_preferences?.[key] : D.profile?.[key]) || []; }
  return [];
}
async function moveStage(appId, stage) {
  try { await api(`/api/applications/${appId}/stage`, { method: 'POST', body: { stage } }); S.selApp = Number(appId); await load(); }
  catch (e) { toast(e.message, true); }
}

/* ───────────── events ───────────── */
document.addEventListener('click', e => {
  const el = e.target.closest('[data-act]');
  if (!el || el.disabled) return;
  const fn = A[el.dataset.act]; if (!fn) return;
  if (el.tagName === 'A' || (el.tagName === 'BUTTON' && el.closest('form'))) e.preventDefault();
  if (el.tagName === 'LABEL' || (el.tagName === 'INPUT' && el.type === 'checkbox' && el.dataset.act === 'toggle-site')) { /* let the checkbox toggle */ }
  fn(el, e);
});
let filterTimer, paletteTimer;
document.addEventListener('input', e => {
  const el = e.target;
  if (el.dataset.form && el.dataset.field) {
    setF(el.dataset.form, el.dataset.field, el.type === 'checkbox' ? el.checked : el.value);
    if (el.dataset.live || el.type === 'radio') render();
  }
  if (el.dataset.filter === 'q') { S.filters.q = el.value; S.filters.page = 1; clearTimeout(filterTimer); filterTimer = setTimeout(load, 250); }
  if (el.dataset.triage === 'min_fit') { S.triage.min_fit = Number(el.value); clearTimeout(filterTimer); filterTimer = setTimeout(load, 250); render(); }
  if (el.dataset.palette) {
    S.paletteQuery = el.value; clearTimeout(paletteTimer);
    paletteTimer = setTimeout(async () => {
      if (!S.paletteQuery) { S.paletteJobs = []; render(); return; }
      const out = await api('/api/jobs/search?' + qs({ q: S.paletteQuery, per_page: 6 })).catch(() => ({ items: [], total: 0 }));
      S.paletteJobs = out.items; S.paletteTotal = out.total; render();
    }, 180);
  }
});
document.addEventListener('change', e => {
  const el = e.target;
  if (el.dataset.form && el.dataset.field && (el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'radio')) {
    setF(el.dataset.form, el.dataset.field, el.type === 'checkbox' ? el.checked : el.value);
    if (el.type === 'radio') render();
  }
  if (el.dataset.filter && el.dataset.filter !== 'q') { S.filters[el.dataset.filter] = el.dataset.filter === 'per_page' || el.dataset.filter === 'min_fit' ? Number(el.value) : el.value; S.filters.page = 1; load(); }
  if (el.dataset.triage && el.dataset.triage !== 'min_fit') { S.triage[el.dataset.triage] = el.checked; load(); }
  if (el.dataset.actChange) A[el.dataset.actChange](el, e);
  if (el.id === 'resume-file' && el.files[0]) {
    const form = new FormData(); form.append('file', el.files[0]);
    busy('upload', async () => { await api('/api/profile/resume', { method: 'POST', form }); delete S.forms.profile?.master_resume; await load(); }, 'Resume uploaded and set as primary');
  }
});
document.addEventListener('focusout', e => {
  const el = e.target;
  if (el.dataset && el.dataset.tag && el.value.trim()) commitTag(el);
  if (el.dataset && el.dataset.actBlur === 'save-notes') {
    const id = el.dataset.id; if (el.value === (D.approval?.notes || '')) return;
    setF('approvalNotes', id, el.value);
    api(`/api/jobs/${id}/notes`, { method: 'PUT', body: { notes: el.value } }).then(() => { if (D.approval) D.approval.notes = el.value; toast('Notes saved'); }).catch(err => toast(err.message, true));
  }
});
function commitTag(el) {
  const { form, tag } = el.dataset, max = Number(el.dataset.max || 20);
  if (form === 'triage') { const tmp = { keywords: S.triage.keywords }; S.forms.__t = tmp; addTags('__t', 'keywords', el.value, max, S.triage.keywords); S.triage.keywords = S.forms.__t.keywords; delete S.forms.__t; el.value = ''; load(); return; }
  addTags(form, tag, el.value, max, tagFallback(form, tag)); el.value = ''; render();
}
document.addEventListener('keydown', e => {
  const el = e.target;
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); A.palette(); return; }
  if (e.key === 'Escape') { if (S.paletteOpen || S.jobOpen || S.kitJob) { S.paletteOpen = false; S.jobOpen = null; S.kitJob = null; render(); } return; }
  if (el.dataset && el.dataset.tag) {
    if (e.key === 'Enter' || (e.key === ',' && el.dataset.split !== 'newline')) { e.preventDefault(); if (el.value.trim()) commitTag(el); return; }
    if (e.key === 'Backspace' && !el.value) {
      const { form, tag } = el.dataset;
      if (form === 'triage') { S.triage.keywords.pop(); load(); return; }
      const list = [...(fv(form, tag, tagFallback(form, tag)) || [])]; list.pop(); setF(form, tag, list); render(); return;
    }
  }
  if (el.dataset && el.dataset.enter && e.key === 'Enter') { e.preventDefault(); A[el.dataset.enter](el, e); return; }
  if (el.dataset && el.dataset.palette && e.key === 'Enter') { const first = S.paletteJobs[0]; if (first) { S.paletteOpen = false; openJob(first.id); } return; }
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName);
  if (typing || e.metaKey || e.ctrlKey || e.altKey || S.jobOpen || S.kitJob || S.paletteOpen) return;
  if (S.screen === 'discovery' && S.discMode === 'triage' && D.queue?.items[0]) {
    const id = D.queue.items[0].id;
    const map = { x: 'skip', m: 'maybe', s: 'shortlist' };
    if (map[e.key.toLowerCase()]) { e.preventDefault(); triageJob(id, map[e.key.toLowerCase()]); }
    else if (e.key === 'Enter') { e.preventDefault(); triageJob(id, 'shortlist', true); }
  }
});
document.addEventListener('paste', e => {
  const el = e.target;
  if (!el.dataset || !el.dataset.tag) return;
  const text = e.clipboardData.getData('text');
  if ((el.dataset.split === 'newline' ? /\n/ : /[,\n]/).test(text)) { e.preventDefault(); el.value = text; commitTag(el); }
});
// Tracker drag and drop
document.addEventListener('dragstart', e => { const card = e.target.closest?.('[data-drag-app]'); if (card) { e.dataTransfer.setData('text/plain', card.dataset.dragApp); e.dataTransfer.effectAllowed = 'move'; } });
document.addEventListener('dragover', e => { const col = e.target.closest?.('[data-drop-stage]'); if (col) { e.preventDefault(); document.querySelectorAll('.kcol.drop').forEach(c => c !== col && c.classList.remove('drop')); col.classList.add('drop'); } });
document.addEventListener('dragleave', e => { const col = e.target.closest?.('[data-drop-stage]'); if (col && !col.contains(e.relatedTarget)) col.classList.remove('drop'); });
document.addEventListener('drop', e => { const col = e.target.closest?.('[data-drop-stage]'); if (!col) return; e.preventDefault(); col.classList.remove('drop'); const id = e.dataTransfer.getData('text/plain'); if (id) moveStage(id, col.dataset.dropStage); });
window.addEventListener('hashchange', () => { const before = S.screen + S.discMode + S.todayVariant; readHash(); if (before !== S.screen + S.discMode + S.todayVariant) { render(); load(); } });

/* ───────────── boot ───────────── */
readHash();
setHash();
render();
load().then(async () => {
  try { const prof = await api('/api/profile'); D.profileRoles = prof.target_roles || []; } catch { /* optional */ }
});
