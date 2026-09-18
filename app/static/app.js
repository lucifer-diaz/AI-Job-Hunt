/* Job Hunt OS workspace — client for the redesigned UI. Every value comes from the API. */
'use strict';

const BOOT = JSON.parse(document.getElementById('boot').textContent);
const $ = sel => document.querySelector(sel);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const attr = esc;

/* ───────────── state ───────────── */
// Filters on the Tailor and Approvals screens, sent to /api/tailor/queue and /api/approvals.
function pfDefaults(kind) { return { q: '', min_fit: 0, min_review: 0, board: 'all', status: 'all', sort: kind === 'tailor' ? 'fit' : 'default' }; }
const S = {
  screen: 'overview', knowledgeSection: 'answers', settingsSection: 'profile', todayVariant: 'focus', discMode: 'triage',
  filters: { q: '', min_fit: 0, board: 'all', stage: 'all', sort: 'fit', page: 1, per_page: 25 },
  triage: { min_fit: 0, hide_agencies: true, hide_reposts: true, keywords: [] }, triagedThisSession: 0,
  checked: new Set(), selJob: null, jobOpen: null, jobTab: 'description',
  paletteOpen: false, paletteQuery: '', paletteJobs: [],
  kitJob: null, tailorJob: null, tailorTab: 'diff', editing: null,
  tailorMode: 'single', tailorChecked: new Set(), tailorRegenerate: false,
  pf: { tailor: pfDefaults('tailor'), approvals: pfDefaults('approvals') },
  approvalJob: null, selApp: null, trackerView: 'board', runSel: null,
  scheduling: false, editingRecruiter: false, recruiterDraft: [], ivDraft: null, manualMissing: null,
  polling: null, busy: {}, forms: {}, apifyImport: false, importPick: new Set(), autoRefreshed: false,
};
const D = {};
const SCREENS = ['overview', 'today', 'discovery', 'tailor', 'approvals', 'tracker', 'monitoring', 'knowledge', 'settings'];

/* ───────────── helpers ───────────── */
async function api(url, { method = 'GET', body, form } = {}) {
  const opt = { method, headers: {} };
  if (form) opt.body = form;
  else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const res = await fetch(url, opt);
  if (res.status === 401 && !url.startsWith('/api/auth/')) {
    window.location.href = '/login'; // session expired or signed out in another tab
    throw new Error('Sign in required');
  }
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
const pill = (text, tone = 'muted', extra = '') => `<span class="pill ${tone} ${extra}" title="${attr(text)}"><span class="pt">${esc(text)}</span></span>`;
const bar = (pct, color, cls = '') => `<div class="bar ${cls}"><i style="width:${attr(pct)};background:${attr(color)}"></i></div>`;
const fitCls = n => n == null ? 'fit-none' : n >= 80 ? 'fit-high' : n >= 65 ? 'fit-mid' : 'fit-low';
const fitTxt = n => n == null ? '—' : String(Math.round(n));
const initials = s => (s || '?').split(/\s+/).filter(Boolean).slice(0, 2).map(w => w[0]).join('').toUpperCase();
const STATUS_TONE = { discovered: 'muted', shortlisted: 'brand', maybe: 'muted', archived: 'muted', dismissed: 'bad', tailored: 'warn', needs_retailor: 'warn', pending_approval: 'warn', rejected: 'bad', approved: 'good', applied: 'good' };
const STATUS_LABEL = { needs_retailor: 're-tailor' };
const statusText = s => STATUS_LABEL[s] || (s || '').replace(/_/g, ' ');
const statusTitle = s => statusText(s).charAt(0).toUpperCase() + statusText(s).slice(1);
const statusPill = s => pill(STATUS_LABEL[s] || (s || '').replace(/_/g, ' '), STATUS_TONE[s] || 'muted');
const toDate = iso => iso ? new Date(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z') : null;
const fmtDay = iso => iso ? toDate(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' }) : '';
const fmtDateTime = iso => iso ? toDate(iso).toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
const loading = () => '<div class="loading">Loading…</div>';
const empty = (title, body, actions = '') => `<div class="empty"><div class="empty-title">${esc(title)}</div><div class="muted small">${esc(body)}</div>${actions ? `<div class="row gap8 center wrap mt14">${actions}</div>` : ''}</div>`;
const btn = (text, act, data = {}, cls = '', disabled = false) =>
  `<button class="btn ${cls}" data-act="${act}" ${Object.entries(data).map(([k, v]) => `data-${k}="${attr(v)}"`).join(' ')} ${disabled ? 'disabled' : ''}>${text}</button>`;

/* form drafts survive re-renders */
const fv = (form, key, fallback) => (S.forms[form] && key in S.forms[form]) ? S.forms[form][key] : fallback;
const setF = (form, key, value) => { (S.forms[form] = S.forms[form] || {})[key] = value; if (form === 'sp') schedulePreview(); settingsTouched(form); };
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
// Scraped postings often arrive as escaped markdown (\\-, **bold**, * bullets); show them as plain text.
const unmark = line => line.replace(/\\([\\`*_{}\[\]()#+\-.!|>~])/g, '$1').replace(/^\s*[*-]\s+/, '• ');
const paragraphs = (text, good = [], gaps = []) => (text || '').split(/\n{1,}/).filter(l => l.trim()).map(l => `<p>${highlight(unmark(l), good, gaps).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>').replace(/\*\*/g, '')}</p>`).join('') || '<p class="faint">No description captured.</p>';
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
  const sub = S.screen === 'discovery' ? '/' + S.discMode : S.screen === 'today' ? '/' + S.todayVariant : S.screen === 'settings' ? '/' + S.settingsSection : S.screen === 'knowledge' ? '/' + S.knowledgeSection : '';
  const next = '#' + S.screen + sub;
  if (location.hash !== next) history.replaceState(null, '', next);
}
function readHash() {
  const [screen, sub] = location.hash.replace(/^#/, '').split('/');
  if (SCREENS.includes(screen)) S.screen = screen;
  if (screen === 'settings') S.settingsSection = settingsSection(sub);
  if (screen === 'knowledge') S.knowledgeSection = knowledgeSection(sub);
  if (screen === 'discovery' && ['triage', 'table', 'setup', 'ranking'].includes(sub)) S.discMode = sub;
  if (screen === 'today' && ['focus', 'run'].includes(sub)) S.todayVariant = sub;
}
function go(screen, extra = {}) {
  if (!settingsCanLeave(screen, extra.settingsSection || S.settingsSection)) return;
  Object.assign(S, { screen, editing: null, scheduling: false, editingRecruiter: false }, extra);
  setHash(); window.scrollTo(0, 0); render(); load();
}

const LOADERS = {
  async overview() { D.overview = await api('/api/workspace/overview'); },
  async today() { D.today = await api('/api/workspace/today'); },
  async discovery() {
    if (S.discMode === 'triage') await loadQueue();
    else if (S.discMode === 'table') await loadTable();
    else if (S.discMode === 'ranking') await loadRanking();
    else await loadSetup();
  },
  async tailor() {
    [D.tailorFacets] = await Promise.all([api('/api/tailor/queue?' + qs({ ...S.pf.tailor, with_facets: true })), refreshTasks()]);
    D.tailorQueue = D.tailorFacets.items;
    D.packet = null;
    // A job opened from elsewhere (Discovery, Today, Approvals) or hidden by the current filter is still
    // shown: asking for a specific job must never land on a different one.
    if (S.tailorJob && !D.tailorQueue.some(j => j.id === S.tailorJob)) {
      const p = await api(`/api/tailor/${S.tailorJob}`).catch(() => null);
      if (p) D.packet = p; else S.tailorJob = null;
    }
    if (!S.tailorJob) S.tailorJob = (D.tailorQueue.find(j => j.has_resume) || D.tailorQueue[0] || {}).id || null;
    if (!D.packet && S.tailorJob) D.packet = await api(`/api/tailor/${S.tailorJob}`);
    if (!D.knowledge) D.knowledge = await api('/api/knowledge');
  },
  async approvals() {
    D.approvalFacets = await api('/api/approvals?' + qs({ ...S.pf.approvals, with_facets: true }));
    D.approvals = D.approvalFacets.items;
    if (!S.approvalJob || !D.approvals.some(j => j.id === S.approvalJob)) S.approvalJob = (D.approvals[0] || {}).id || null;
    D.approval = S.approvalJob ? await api(`/api/approvals/${S.approvalJob}`) : null;
  },
  async tracker() {
    D.tracker = await api('/api/tracker');
    const apps = D.tracker.applications;
    if (!apps.some(a => a.id === S.selApp)) S.selApp = (apps[0] || {}).id || null;
  },
  knowledge: loadKnowledge,
  async monitoring() { await loadMonitoring(); },
  async settings() {
    D.settingsLoadError = '';
    try {
      [D.profile, D.settings, D.prefs, D.resumes] = await Promise.all([
        api('/api/profile'), api('/api/settings'), api('/api/preferences'), api('/api/profile/resumes')]);
    } catch (err) { D.settingsLoadError = err.message; throw err; }
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
  // Runs left "ready" after a reload are checked once per visit, quietly.
  if (!S.autoRefreshed && !S.polling && D.searchProfile.apify_configured && D.runs.some(r => !r.completed_at)) {
    S.autoRefreshed = true;
    api('/api/search/apify/runs/refresh-pending', { method: 'POST' }).then(async out => {
      if (!out.results.some(r => r.ok && r.completed)) return;
      await afterPull(out.results);
      if (S.screen === 'discovery' && S.discMode === 'setup') { render(); toast(summarizePull(out.results)); }
    }).catch(() => {});
  }
}
function renderApifyImport() {
  const remote = D.remoteRuns, chosen = S.importPick;
  if (!remote) return `<div class="small muted" style="padding:14px 16px;background:#fbfbfe;border-bottom:1px solid var(--line2)">${isBusy('remoteRuns') ? 'Loading runs from your Apify account…' : 'Could not load Apify runs.'}</div>`;
  return `<div style="padding:12px 16px;background:#fbfbfe;border-bottom:1px solid var(--line2)">
    <div class="xs muted mb8">Runs in your Apify account, newest first — including ones started outside Job Hunt OS. Imported jobs are normalized and deduplicated against everything already in your pipeline.</div>
    ${remote.length ? remote.map(r => `<label class="row gap9 top-align" style="padding:8px 0;border-bottom:1px solid var(--line3);cursor:${r.importable ? 'pointer' : 'default'}">
      <input type="checkbox" data-act="pick-remote" data-id="${attr(r.external_run_id)}" ${chosen.has(r.external_run_id) ? 'checked' : ''} ${r.importable ? '' : 'disabled'} style="margin-top:3px;accent-color:var(--brand)">
      <span class="grow" style="min-width:0"><span class="small b ellipsis" style="display:block">${esc(r.actor_title)}</span>
        <span class="tiny muted" style="display:block"><span class="mono">${esc(r.actor_id)}</span> · ${fmtDateTime(r.started_at)}${r.item_count != null ? ` · ${r.item_count} items` : ''}${r.origin ? ` · ${esc(r.origin.toLowerCase())}` : ''}</span></span>
      <span class="col gap4 none" style="align-items:flex-end">${pill(r.status.toLowerCase(), r.status === 'SUCCEEDED' ? 'good' : ['READY', 'RUNNING'].includes(r.status) ? 'warn' : 'bad')}${r.local_run_id ? `<span class="tiny faint">${r.imported ? 'imported' : 'tracking'} · #${r.local_run_id}</span>` : ''}</span></label>`).join('')
      : '<div class="small muted">No runs found in your Apify account.</div>'}
    <div class="row gap8 wrap mt10">${btn(label('importRuns', chosen.size ? `Import ${chosen.size} selected` : 'Select runs to import', 'Importing…'), 'import-remote', {}, 'sm primary', !chosen.size || isBusy('importRuns'))}${btn(label('remoteRuns', 'Reload', 'Loading…'), 'load-remote', {}, 'sm', isBusy('remoteRuns'))}
      <span class="tiny faint grow">Already imported? Open the run and use Pull again.</span></div></div>`;
}
// Ranking runs on the server: imports queue new jobs and the ranking worker scores them in the background.
// After queueing, poll the jobs' ranking tasks for a while and refresh once none are waiting or running.
async function watchRanking(ids) {
  const watched = ids.slice(0, 200);
  if (!watched.length) return;
  for (let i = 0; i < 40; i++) {
    await new Promise(r => setTimeout(r, 3000));
    const out = await api(`/api/ranking/tasks?job_ids=${watched.join(',')}`).catch(() => null);
    if (!out || !out.active) break;
  }
  if (S.jobOpen) D.jobModal = await api(`/api/jobs/${S.jobOpen}`).catch(() => D.jobModal);
  await load();
}
function rankQueuedMsg(out) {
  const n = out.queued.length;
  return `${n ? `Queued ${n} job${n === 1 ? '' : 's'} for ranking` : 'Nothing new to rank'}${out.skipped.length ? ` · ${out.skipped.length} skipped` : ''}${out.blocked ? ` · waiting: ${out.blocked}` : ''}`;
}
async function afterPull(results) {
  const ok = results.filter(r => r.ok && r.id);
  if (ok[0]) S.runSel = ok[0].id;
  await loadSetup();
  api('/api/workspace/summary').then(s => { D.summary = s; render(); }).catch(() => {});
}
function summarizePull(results) {
  const ok = results.filter(r => r.ok), failed = results.filter(r => !r.ok);
  if (!ok.length && !failed.length) return 'No runs were waiting';
  const created = ok.reduce((a, r) => a + (r.import?.created || 0), 0), dup = ok.reduce((a, r) => a + (r.import?.duplicates || 0), 0);
  const running = ok.filter(r => !r.completed).length;
  return [`${created} new job${created === 1 ? '' : 's'}`, dup ? `${dup} duplicate${dup === 1 ? '' : 's'} skipped` : '',
    running ? `${running} still running` : '', failed.length ? `${failed.length} failed: ${failed[0].error}` : ''].filter(Boolean).join(' · ');
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
const NAV = [['overview', 'Overview', null], ['today', 'Today', 'today'], ['discovery', 'Discovery', 'discovery'], ['tailor', 'Tailor', 'tailor'], ['approvals', 'Approvals', 'approvals'], ['tracker', 'Tracker', 'tracker'], ['monitoring', 'Monitoring', null]];
const NAV2 = [['knowledge', 'Knowledge bank'], ['settings', 'Settings & profile']];
function renderNav() {
  const c = D.summary?.counts || {};
  const item = ([id, text, key]) => `<a class="nav-btn ${S.screen === id ? 'on' : ''}" href="#${id}" data-act="go" data-screen="${id}"><span>${text}</span>${key ? `<span class="nav-count">${c[key] ?? '·'}</span>` : `<span class="nav-count">—</span>`}</a>`;
  return `<div class="nav-group"><div class="nav-caps">WORKSPACE</div>${NAV.map(item).join('')}</div>
    <div class="nav-group"><div class="nav-caps">SETUP</div>${NAV2.map(([id, text]) => `<a class="nav-btn ${S.screen === id ? 'on' : ''}" href="#${id}" data-act="go" data-screen="${id}"><span>${text}</span></a>`).join('')}</div>`;
}
function renderSideFoot() {
  const account = BOOT.user ? `<div class="side-card"><div class="side-card-title ellipsis" title="${attr(BOOT.user.email)}">${esc(BOOT.user.email)}</div>
    <div class="side-card-body">${btn(label('logout', 'Sign out', 'Signing out…'), 'logout', {}, 'sm', isBusy('logout'))}</div></div>` : '';
  const s = D.summary; if (!s) return account;
  const a = s.auto_search;
  const on = a.schedule === 'daily';
  const last = a.last_run ? `Last run ${fmtDay(a.last_run)} · ${a.last_run_status.toLowerCase()}.` : 'No searches have run yet.';
  return `<div class="side-card"><div class="side-card-title"><span class="dot" style="background:${on ? '#4ade80' : '#6a7388'}"></span>Auto-search ${on ? 'on' : 'off'}</div>
    <div class="side-card-body">${on ? `${a.roles} role${a.roles === 1 ? '' : 's'} × ${Math.max(1, a.locations)} location${a.locations === 1 ? '' : 's'} run on the scheduler with your Apify key.` : 'Searches run when you start them.'} ${esc(last)}</div></div>${account}`;
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
    monitoring: ['Monitoring', 'Every LLM request: tokens, context, latency, cost and failures · recommendations never change anything automatically'],
    knowledge: ['Knowledge bank', 'Answer once, reuse in every ATS form'],
    settings: ['Settings', 'A workspace that works your way'],
  }[S.screen];
}
function renderTop() {
  const [title, sub] = screenMeta();
  let seg = '';
  if (S.screen === 'today') seg = [['focus', 'Command centre'], ['run', 'Run sheet']].map(([id, t]) => `<button class="${S.todayVariant === id ? 'on' : ''}" data-act="variant" data-kind="today" data-id="${id}">${t}</button>`).join('');
  if (S.screen === 'tailor') seg = [['single', 'One job'], ['bulk', 'Bulk tailor']].map(([id, t]) => `<button class="${S.tailorMode === id ? 'on' : ''}" data-act="variant" data-kind="tailor" data-id="${id}">${t}</button>`).join('');
  if (S.screen === 'discovery') seg = [['triage', 'Triage queue'], ['table', 'Table + detail'], ['ranking', 'Ranking tracker'], ['setup', 'Search setup']].map(([id, t]) => `<button class="${S.discMode === id ? 'on' : ''}" data-act="variant" data-kind="discovery" data-id="${id}">${t}</button>`).join('');
  return `<div class="top-row"><div><h1>${esc(title)}</h1><div class="sub">${esc(sub)}</div></div>
    <div class="row gap8 wrap">${seg ? `<div class="seg">${seg}</div>` : ''}
      <button class="btn" data-act="palette">Search · ⌘K</button>
      ${S.screen === 'settings' ? '' : '<button class="btn primary" data-act="go" data-screen="discovery" data-mode="setup">New search</button>'}</div></div>`;
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
  if (S.discMode === 'ranking') return banner + renderRanking();
  return banner + renderTriage();
}

/* ───────────── ranking tracker ───────────── */
// The asynchronous ranker's work for this account (/api/ranking/tracker): waiting, running, retrying and finished.
let rankingTimer = null;
const onRankingTab = () => S.screen === 'discovery' && S.discMode === 'ranking';
const RANK_REASON = { new: 'New job', user_request: 'You asked', resume_changed: 'Resume changed', profile_changed: 'Profile changed', prompt_changed: 'Ranking prompt updated' };
const until = iso => {
  if (!iso) return '';
  const sec = Math.round((new Date(iso.slice(0, 23) + 'Z') - Date.now()) / 1000);
  return sec <= 0 ? 'now' : sec < 60 ? `in ${sec}s` : sec < 3600 ? `in ${Math.round(sec / 60)}m` : `in ${Math.round(sec / 3600)}h`;
};
const later = iso => !!iso && until(iso) !== 'now';
async function loadRanking() {
  const before = new Set((D.ranking?.active || []).map(t => t.id));
  D.ranking = await api('/api/ranking/tracker');
  const done = D.ranking.recent.filter(t => before.has(t.id));
  if (done.length) {
    const one = done.length === 1 ? done[0] : null, failed = done.filter(t => t.status === 'FAILED').length;
    toast(one ? `${one.company}: ${rankTaskDetail(one)}` : `${done.length} rankings finished${failed ? ` · ${failed} failed` : ''}`, failed === done.length);
  }
  scheduleRankingPoll();
}
// Poll while the tracker is open: every few seconds while work is in flight, slowly otherwise.
function scheduleRankingPoll() {
  clearTimeout(rankingTimer); rankingTimer = null;
  if (!onRankingTab()) return;
  rankingTimer = setTimeout(async () => {
    if (!onRankingTab()) return;
    try { await loadRanking(); } catch { scheduleRankingPoll(); return; }
    if (onRankingTab()) render();
  }, D.ranking?.active.length ? 4000 : 20000);
}
function rankTaskPill(t) {
  if (t.status === 'PROCESSING') return pill('ranking…', 'brand');
  if (t.status === 'PENDING') return later(t.next_attempt_at) ? pill(t.attempt_count ? 'retry scheduled' : 'waiting', 'warn') : pill('queued', 'muted');
  if (t.status === 'FAILED') return pill('failed', 'bad');
  if (t.status === 'SKIPPED') return pill('skipped', 'muted');
  return t.ranking?.decision === 'QUALIFIED' ? pill('qualified', 'good') : pill('archived', 'muted');
}
function rankTaskDetail(t) {
  const category = t.error_category ? ` (${t.error_category.toLowerCase().replace(/_/g, ' ')})` : '';
  if (t.status === 'PROCESSING') return `Ranking · attempt ${t.attempt_count} of ${t.max_attempts} · ${elapsed(t.started_at)}`;
  if (t.status === 'PENDING') {
    if (later(t.next_attempt_at) && t.attempt_count) return `Attempt ${t.attempt_count} of ${t.max_attempts} failed${category} · retry ${until(t.next_attempt_at)}${t.last_error ? ` · ${t.last_error}` : ''}`;
    if (later(t.next_attempt_at)) return `Waiting: ${t.last_error || 'your account is not ready'} · checks again ${until(t.next_attempt_at)}`;
    return 'Queued for the next ranking run';
  }
  if (t.status === 'FAILED') return `Gave up after ${t.attempt_count} attempt${t.attempt_count === 1 ? '' : 's'}${category}${t.last_error ? ` · ${t.last_error}` : ''}`;
  if (t.status === 'SKIPPED') return t.last_error || 'Skipped';
  const r = t.ranking;
  if (!r) return 'Ranked';
  return `${r.decision === 'QUALIFIED' ? 'Qualified' : 'Archived'} · fit ${fitTxt(r.overall_fit_score)} · candidate ${fitTxt(r.candidate_fit_score)} · resume ${fitTxt(r.resume_fit_score)} · confidence ${Math.round(r.confidence * 100)}% · v${r.version}`;
}
function renderRanking() {
  const r = D.ranking; if (!r) return loading();
  const c = r.counts, w = r.worker || {};
  const tile = ([title, n, hint, tone]) => `<div class="box"><div class="xs muted">${esc(title)}</div>
    <div class="mono bb" style="font-size:24px;line-height:1.3;color:${n ? { brand: 'var(--brand)', warn: 'var(--warn)', good: 'var(--good)', bad: 'var(--bad)' }[tone] || 'var(--ink)' : 'var(--grey)'}">${n}</div>
    <div class="tiny faint">${esc(hint)}</div></div>`;
  const tileGroup = (caption, items) => `<div class="grow" style="min-width:240px"><div class="caps-sm mb8">${esc(caption)}</div>
    <div style="display:grid;grid-template-columns:repeat(${items.length},minmax(0,1fr));gap:8px">${items.map(tile).join('')}</div></div>`;
  const tiles = tileGroup('Pipeline now', [['Filtering', c.filtering, 'hard filters', 'muted'], ['Queued', c.queued, 'next run', 'muted'],
    ['Ranking', c.processing, 'model call', 'brand'], ['Retry / wait', c.retry_scheduled + c.waiting, 'backing off', 'warn']])
    + tileGroup('Last 24 hours', [['Qualified', c.qualified, 'kept', 'good'], ['Archived', c.archived, 'by policy', 'muted'],
      ['Failed', c.failed, 'gave up', 'bad']]);
  const inFlight = r.active.length, { finished, total } = r.progress;
  const pct = total ? Math.round(100 * finished / total) : 100;
  const workerTxt = !w.enabled ? 'The ranking worker is turned off on this server'
    : w.running ? 'A ranking run is in progress'
    : w.next_run_at ? `Next ranking run ${until(w.next_run_at)} · every ${w.interval_minutes} min` : `Ranking runs every ${w.interval_minutes} min`;
  const blocked = r.blocked ? `<div class="banner warn mb14"><span class="grow">Ranking is paused for your account: ${esc(r.blocked)}</span>${btn('Open settings', 'go', { screen: 'settings' }, 'sm')}</div>` : '';
  const autoOff = r.auto_rank ? '' : `<div class="banner mb14"><span class="grow">Auto-rank on import is off, so only the jobs you ask to rank are ranked.</span>${btn('Search setup', 'go', { screen: 'discovery', mode: 'setup' }, 'sm')}</div>`;
  const row = t => {
    const retry = t.status !== 'FAILED' ? '' : t.retried ? btn('Retried', 'ranking-retry', {}, 'sm', true)
      : btn(isBusy('rankRetry' + t.job_id) ? 'Queueing…' : 'Retry', 'ranking-retry', { job: t.job_id }, 'sm', isBusy('rankRetry' + t.job_id));
    const detail = rankTaskDetail(t), why = RANK_REASON[t.reason] || t.reason;
    return `<div class="lrow ${t.status === 'PROCESSING' ? 'sel' : ''}" style="gap:12px">
      <div class="grow" style="min-width:0"><div class="small b ellipsis">${esc(t.title)} <span class="muted" style="font-weight:400">· ${esc(t.company)}</span></div>
        <div class="xs ellipsis ${t.status === 'FAILED' ? '' : 'muted'}" style="${t.status === 'FAILED' ? 'color:var(--bad)' : ''}" title="${attr(detail)}">${esc(why)} · ${esc(detail)}</div></div>
      ${retry}${btn('Job', 'open-job', { id: t.job_id }, 'sm')}${rankTaskPill(t)}</div>`;
  };
  const section = (title, sub, items) => `<section class="card flush">
    <div style="padding:14px 18px;border-bottom:1px solid var(--line2)"><div class="card-title" style="font-size:16px">${esc(title)}</div><div class="card-sub">${esc(sub)}</div></div>
    <div style="max-height:460px;overflow:auto">${items.map(row).join('')}</div></section>`;
  const nothing = !inFlight && !r.recent.length && !c.filtering;
  return `<div class="stack">${blocked}${autoOff}
    <section class="card">
      <div class="row wrap gap10 mb14"><div class="grow" style="min-width:220px"><div class="card-title">${inFlight || c.filtering ? 'Ranking in the background' : 'Ranking is idle'}</div>
        <div class="card-sub">${esc(workerTxt)}${w.enabled ? ' · runs on the server, safe to leave this page' : ''}</div>
        <div class="row base gap8 mt8"><span class="grow">${bar(pct + '%', '#5b4ae8')}</span><span class="xs mono muted none">${finished}/${total} in the last day</span></div></div>
        ${btn(label('rankBatch', 'Queue unranked jobs', 'Queueing…'), 'rank-batch', {}, 'sm', isBusy('rankBatch'))}
        ${btn(label('rankRefresh', 'Refresh', 'Refreshing…'), 'ranking-refresh', {}, 'sm', isBusy('rankRefresh'))}</div>
      <div class="row wrap gap16" style="align-items:flex-start">${tiles}</div></section>
    ${nothing ? `<section class="card">${empty('No ranking activity in the last day', 'Jobs from your searches are filtered and ranked automatically. Rankings you request from a job, the table or the triage queue show up here too.', btn('Go to triage', 'go-triage', {}, 'primary'))}</section>` : ''}
    ${inFlight ? section('In progress', `${inFlight} job${inFlight === 1 ? '' : 's'} waiting, ranking or retrying`, r.active) : ''}
    ${r.recent.length ? section('Finished in the last 24 hours', `${c.completed} ranked · ${c.failed} failed${c.skipped ? ` · ${c.skipped} skipped` : ''}`, r.recent) : ''}
  </div>`;
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
        ${q.items.some(i => i.fit == null) ? btn(label('rankBatch', 'Rank every unranked job', 'Queueing…'), 'rank-batch', {}, 'soft block', isBusy('rankBatch')) : ''}</div></section>
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
        ${job.fit == null ? `<div class="scorebox"><div class="n" style="color:var(--grey)">—</div><div class="l">NOT RANKED</div>${btn(label('rank' + job.id, 'Rank now', 'Queueing…'), 'rank-job', { id: job.id }, 'sm soft mt8', isBusy('rank' + job.id))}</div>` : `<div class="scorebox"><div class="n">${fitTxt(job.fit)}</div><div class="l">FIT SCORE</div></div>`}
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
      <select class="select" id="flt-stage" data-filter="stage">${opts([['all', 'All stages'], ['unranked', 'Unranked'], ...['discovered', 'shortlisted', 'maybe', 'archived', 'dismissed', 'tailored', 'needs_retailor', 'pending_approval', 'approved', 'applied'].map(s => [s, statusTitle(s)])], f.stage)}</select>
      <select class="select" id="flt-sort" data-filter="sort">${opts([['fit', 'Sort: best fit'], ['new', 'Sort: newest'], ['company', 'Sort: company']], f.sort)}</select>
      ${btn('Reset', 'reset-filters', {}, 'sm')}
      ${S.checked.size ? '' : btn(label('rankBatch', 'Rank unranked', 'Queueing…'), 'rank-batch', {}, 'sm', isBusy('rankBatch'))}
      ${S.checked.size ? `<span class="small b" style="color:var(--brand-ink)">${S.checked.size} selected</span>` : ''}
      ${btn(isBusy('bulkRank') ? esc(S.rankProgress || 'Queueing…') : `Rank${S.checked.size ? ` ${S.checked.size}` : ''}`, 'bulk-rank', {}, 'sm', !S.checked.size || isBusy('bulkRank'))}
      ${btn(`Shortlist${S.checked.size ? ` ${S.checked.size}` : ''}`, 'bulk', { action: 'shortlist' }, 'sm soft', !S.checked.size)}
      ${btn(label('bulkToTailor', `Bulk tailor${S.checked.size ? ` ${S.checked.size}` : ''}`, 'Queueing…'), 'bulk-to-tailor', {}, 'sm primary', !S.checked.size || isBusy('bulkToTailor'))}
      ${btn('Dismiss', 'bulk', { action: 'dismiss' }, 'sm', !S.checked.size)}
      ${btn(label('bulkDelete', `Delete${S.checked.size ? ` ${S.checked.size}` : ''}`, 'Deleting…'), 'bulk-delete', {}, 'sm danger', !S.checked.size || isBusy('bulkDelete'))}
    </div>
    <div class="row between wrap" style="padding:9px 14px;border-bottom:1px solid var(--line2);background:#fbfbfe"><span class="small muted">${j.total ? `Showing ${start}–${Math.min(j.page * j.per_page, j.total)} of ${j.total} jobs` : 'No matches'}</span><span class="small muted">Page ${j.page} of ${j.pages} · ${j.total === j.total_all ? 'no filters applied' : `${j.total_all - j.total} hidden by filters`}</span></div>
    ${allOnPage && j.total > j.items.length && S.checked.size < j.total ? `<div class="banner" style="border-radius:0;border-width:0 0 1px">All ${j.items.length} on this page are selected. <a href="#" data-act="select-all-matching">${isBusy('selectAll') ? 'Selecting…' : `Select all ${j.total} matching jobs`}</a></div>` : ''}
    ${S.checked.size && S.checked.size >= j.total && j.total > j.items.length ? `<div class="banner" style="border-radius:0;border-width:0 0 1px">All ${S.checked.size} matching jobs are selected. <a href="#" data-act="clear-selection">Clear selection</a></div>` : ''}
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
    <div class="row gap8 mt16">${['tailored', 'needs_retailor', 'approved', 'applied', 'pending_approval'].includes(sel.status) ? btn(sel.status === 'needs_retailor' ? 'Re-tailor' : 'Open packet', 'go', { screen: sel.status === 'applied' ? 'tracker' : sel.status === 'approved' ? 'approvals' : 'tailor', job: sel.id }, 'primary grow') : btn('Tailor', 'triage', { id: sel.id, action: 'shortlist', tailor: 1 }, 'primary grow')}${btn('Full description', 'open-job', { id: sel.id }, 'grow')}</div>` : empty('Select a job', 'Pick a row to see its details.')}
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
    <section class="card flush"><div class="card-head"><div class="row between wrap gap8"><div class="card-title sm">Recent runs</div>
        <div class="row gap6">${D.runs.some(r => !r.completed_at) ? btn(label('refreshPending', `Refresh ${D.runs.filter(r => !r.completed_at).length} pending`, 'Checking…'), 'refresh-pending', {}, 'sm', isBusy('refreshPending') || !data.apify_configured) : ''}${btn(S.apifyImport ? 'Close import' : 'Import from Apify', 'toggle-apify-import', {}, 'sm soft', !data.apify_configured)}</div></div>
        <div class="card-sub">Pick a run to see exactly what it searched and everything it pulled.</div></div>
      ${S.apifyImport ? renderApifyImport() : ''}
      ${D.runs.length ? D.runs.slice(0, 6).map(r => `<div class="lrow click ${r.id === S.runSel ? 'sel' : ''}" data-act="select-run" data-id="${r.id}" style="align-items:flex-start"><span class="dot" style="width:7px;height:7px;margin-top:6px;background:${r.status === 'SUCCEEDED' ? '#0f9c86' : r.completed_at ? '#c22f4e' : '#e0a136'}"></span><div class="grow"><div class="small b">Run #${r.id} · ${esc(r.status.toLowerCase())}</div><div class="xs muted mt4"><span class="mono">${esc((r.actor_id || '').split('/').pop())}</span> · ${r.created_jobs} new from ${r.item_count} results · ${fmtDateTime(r.created_at)}</div></div></div>`).join('') : `<div class="small muted" style="padding:16px">No runs yet.</div>`}
      ${run ? `<div style="padding:16px;background:#fbfbfe">
        <div class="row base between wrap gap8 mb12"><div class="base b">Run #${run.id}</div><div class="row gap6">${pill(run.status, run.status === 'SUCCEEDED' ? 'good' : run.completed ? 'bad' : 'warn')}${data.apify_configured ? btn(label('pull' + run.id, run.completed ? 'Pull again' : 'Check status &amp; pull', 'Checking…'), 'pull-run', { id: run.id, reimport: run.completed ? '1' : '' }, 'sm', isBusy('pull' + run.id)) : ''}</div></div>
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

/* ───────────── packet filters (Tailor, Approvals) ───────────── */
const PF_STATUS = {
  tailor: [['all', 'All packets'], ['not_tailored', 'Not tailored'], ['has_packet', 'Has packet'], ['needs_retailor', 'Needs re-tailoring'], ['sent_back', 'Sent back'], ['untailored', 'Master resume only']],
  approvals: [['all', 'All statuses'], ['waiting', 'Waiting'], ['sent_back', 'Sent back'], ['approved', 'Approved']],
};
const PF_SORT = {
  tailor: [['fit', 'Sort: best fit'], ['reviewer', 'Sort: reviewer score'], ['new', 'Sort: newest'], ['company', 'Sort: company']],
  approvals: [['default', 'Sort: waiting first'], ['reviewer', 'Sort: reviewer score'], ['fit', 'Sort: best fit'], ['new', 'Sort: newest'], ['company', 'Sort: company']],
};
const pfActive = kind => JSON.stringify(S.pf[kind]) !== JSON.stringify(pfDefaults(kind));
function renderPacketFilters(kind, facets) {
  const f = S.pf[kind];
  const opts = (list, cur) => list.map(([v, t]) => `<option value="${attr(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(t)}</option>`).join('');
  const sel = (key, list) => `<select class="select" style="margin:0;width:auto" id="pf-${kind}-${key}" data-pf="${kind}" data-pfkey="${key}">${opts(list, f[key])}</select>`;
  return `<section class="card flush"><div class="toolbar" style="border-bottom:0">
    <input class="input grow" style="min-width:180px;margin:0" id="pf-${kind}-q" data-pf="${kind}" data-pfkey="q" value="${attr(f.q)}" placeholder="Search title, company or location…">
    ${sel('min_fit', [[0, 'Any fit'], [50, 'Fit ≥ 50'], [70, 'Fit ≥ 70'], [80, 'Fit ≥ 80']])}
    ${sel('min_review', [[0, 'Any reviewer score'], [50, 'Reviewer ≥ 50'], [65, 'Reviewer ≥ 65'], [75, 'Reviewer ≥ 75'], [85, 'Reviewer ≥ 85']])}
    ${sel('board', [['all', 'All boards'], ...facets.boards.map(b => [b, b])])}
    ${sel('status', PF_STATUS[kind])}${sel('sort', PF_SORT[kind])}
    <span class="small muted none">${facets.total === facets.total_all ? `${facets.total_all} job${facets.total_all === 1 ? '' : 's'}` : `${facets.total} of ${facets.total_all}`}</span>
    ${btn('Reset', 'pf-reset', { kind }, 'sm', !pfActive(kind))}
  </div></section>`;
}
const pfNoMatch = kind => `<section class="card">${empty('No jobs match these filters', `${S.pf[kind].q ? `Nothing matches “${esc(S.pf[kind].q)}” with the current filters. ` : ''}Loosen or reset the filters to see all ${(kind === 'tailor' ? D.tailorFacets : D.approvalFacets).total_all}.`, btn('Reset filters', 'pf-reset', { kind }, 'primary'))}</section>`;

/* ───────────── tailor ───────────── */
const PACKET_LOCKED = ['approved', 'applied'];
// Background tailoring tasks (/api/tailor-tasks). They run on the server, so leaving or reloading the page is safe.
const STAGE_LABEL = { queued: 'Queued', ranking: 'Ranking', tailoring: 'Tailoring', reviewing: 'Reviewing', done: 'Done' };
const activeTask = jobId => (D.tasks || []).find(t => t.job_id === jobId && (t.status === 'queued' || t.status === 'running'));
const elapsed = iso => { if (!iso) return ''; const sec = Math.max(0, Math.round((Date.now() - new Date(iso.slice(0, 23) + 'Z')) / 1000)); return sec < 60 ? `${sec}s` : `${Math.floor(sec / 60)}m ${sec % 60}s`; };
// A failed task counts as retried once a newer task exists for the same job, whatever that task's outcome.
const retried = t => (D.tasks || []).some(o => o.job_id === t.job_id && o.id > t.id);
function taskPill(t) {
  if (t.status === 'running') return pill(`${STAGE_LABEL[t.stage] || 'Working'}…`, 'brand');
  if (t.status === 'queued') return pill('queued', 'muted');
  if (t.status === 'failed') return pill('failed', 'bad');
  if (t.status === 'cancelled') return pill('cancelled', 'muted');
  return pill(t.result?.auto_approved ? 'auto-approved' : 'packet ready', 'good');
}
function renderTaskStack() {
  const tasks = D.tasks || []; if (!tasks.length) return '';
  const count = st => tasks.filter(t => t.status === st).length;
  const running = count('running'), queued = count('queued'), done = count('succeeded'), failed = count('failed');
  const finished = tasks.length - running - queued, total = tasks.length;
  const overall = Math.round(tasks.reduce((sum, t) => sum + t.progress, 0) / total);
  const parts = [running && `${running} running`, queued && `${queued} queued`, done && `${done} done`, failed && `${failed} failed`].filter(Boolean).join(' · ');
  const rows = tasks.map(t => {
    const active = t.status === 'running' || t.status === 'queued';
    const detail = t.status === 'running' ? `${STAGE_LABEL[t.stage]} · ${elapsed(t.started_at)}`
      : t.status === 'queued' ? 'Waiting for a free worker'
      : t.status === 'failed' ? esc(t.error || 'Failed')
      : t.status === 'cancelled' ? 'Cancelled'
      : `Reviewer ${fitTxt(t.result?.reviewer_score)}${t.result?.untailored ? ' · master resume' : ''}`;
    const actions = t.status === 'queued' ? btn('Cancel', 'task-cancel', { id: t.id }, 'sm')
      : t.status === 'succeeded' ? btn('Open', 'task-open', { job: t.job_id }, 'sm')
      : t.status === 'failed' ? (retried(t) ? btn('Retried', 'task-retry', {}, 'sm', true) : btn(isBusy('retry' + t.id) ? 'Retrying…' : 'Retry', 'task-retry', { id: t.id, job: t.job_id, mode: t.mode }, 'sm', isBusy('retry' + t.id))) : '';
    return `<div class="lrow ${t.status === 'running' ? 'sel' : ''}" style="gap:12px">
      <div class="grow" style="min-width:0"><div class="small b ellipsis">${esc(t.title)} <span class="muted" style="font-weight:400">· ${esc(t.company)}</span></div>
        <div class="xs ${t.status === 'failed' ? '' : 'muted'} ellipsis" style="${t.status === 'failed' ? 'color:var(--bad)' : ''}">${t.mode === 'skip' ? 'Skip tailoring · ' : ''}${detail}</div>
        ${active ? `<div class="row base gap8 mt6"><span class="grow">${bar(t.progress + '%', t.status === 'running' ? '#5b4ae8' : '#c9cbe0')}</span><span class="xs mono muted none" style="width:34px;text-align:right">${t.progress}%</span></div>` : ''}</div>
      ${actions}${taskPill(t)}</div>`;
  }).join('');
  return `<section class="card flush">
    <div class="row wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">${running || queued ? 'Tailoring in the background' : 'Tailoring finished'}</div>
        <div class="card-sub">${parts}${running || queued ? ' · runs on the server, safe to leave this page' : ''}</div>
        <div class="row base gap8 mt8"><span class="grow">${bar(overall + '%', '#5b4ae8')}</span><span class="xs mono muted none">${finished}/${total}</span></div></div>
      ${queued ? btn(`Cancel queued (${queued})`, 'tasks-cancel', {}, 'sm') : ''}
      ${finished ? btn(`Clear finished (${finished})`, 'tasks-clear', {}, 'sm') : ''}
      ${done ? btn('Review in Approvals', 'go', { screen: 'approvals' }, 'sm soft') : ''}
    </div>
    <div style="max-height:420px;overflow:auto">${rows}</div></section>`;
}

function renderBulkTailor(queue) {
  const selected = queue.filter(j => S.tailorChecked.has(j.id));
  const needsWork = j => !j.has_resume || j.status === 'needs_retailor';
  const eligible = selected.filter(j => !activeTask(j.id) && (S.tailorRegenerate || needsWork(j))).length;
  const untailored = queue.filter(needsWork);
  const allOn = queue.every(j => S.tailorChecked.has(j.id));
  const allToggle = `<input type="checkbox" data-act="tailor-select" data-which="${allOn ? 'none' : 'all'}" ${allOn ? 'checked' : ''} aria-label="Select all jobs">`;
  const head = `<div class="lrow caps" style="gap:12px;padding-top:9px;padding-bottom:9px;background:#fafbfe;font-size:10.5px;letter-spacing:.06em">
      ${allToggle}<span class="none" style="width:26px" title="AI fit score">FIT</span><span class="grow">JOB</span>
      <span class="none" style="width:54px;text-align:right" title="Independent reviewer score of the latest packet">REVIEWER</span>
      <span class="none" style="width:48px;text-align:right" title="Share of the posting's ATS keywords the packet covers">ATS</span>
      <span class="none" style="width:210px;text-align:right">STATUS</span></div>`;
  const rows = queue.map(j => {
    const task = activeTask(j.id);
    const retailor = j.status === 'needs_retailor';
    const status = task ? taskPill(task)
      : retailor ? `<span title="${attr((j.review_reasons || []).join(' · ') || 'Review sent this packet back to be tailored again')}">${pill('re-tailor', 'warn')}</span>`
      : j.has_resume ? pill(j.status === 'pending_approval' ? 'sent back' : 'has packet', 'muted') : pill('not tailored', 'warn');
    return `<div class="lrow click ${task?.status === 'running' ? 'sel' : ''}" data-act="tailor-check" data-id="${j.id}" style="gap:12px">
      <input type="checkbox" data-act="tailor-check" data-id="${j.id}" ${S.tailorChecked.has(j.id) ? 'checked' : ''} aria-label="Select ${attr(j.title)}">
      <span class="mono bb ${fitCls(j.fit)}" style="width:26px" title="Fit score">${fitTxt(j.fit)}</span>
      <div class="grow" style="min-width:0"><div class="small b ellipsis">${esc(j.title)}</div><div class="xs muted ellipsis">${esc(j.company)} · ${esc(j.board)}${j.location ? ' · ' + esc(j.location) : ''}</div></div>
      <span class="mono xs none" style="width:54px;text-align:right" title="Reviewer score">${j.reviewer_score == null ? '<span class="faint">—</span>' : fitTxt(j.reviewer_score)}${j.untailored ? '<span class="faint"> ·m</span>' : ''}</span>
      <span class="mono xs none" style="width:48px;text-align:right" title="ATS keyword coverage">${j.ats_pct == null ? '<span class="faint">—</span>' : j.ats_pct + '%'}</span>
      <span class="row gap8 none" style="width:210px;justify-content:flex-end">${btn('Description', 'open-job', { id: j.id }, 'sm')}${status}</span></div>`;
  }).join('');
  const queueing = isBusy('bulkTailor');
  return `<div class="stack">${renderTaskStack()}${renderPacketFilters('tailor', D.tailorFacets)}<section class="card flush">
    <div class="row wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">Bulk tailor</div>
        <div class="card-sub">${selected.length} of ${queue.length} selected · ${untailored.length} not tailored yet</div></div>
      ${btn(`Select not tailored (${untailored.length})`, 'tailor-select', { which: 'untailored' }, 'sm', !untailored.length)}
      ${btn(allOn ? 'Clear' : 'Select all', 'tailor-select', { which: allOn ? 'none' : 'all' }, 'sm')}
      ${btn(`Skip tailoring${eligible ? ` ${eligible}` : ''}`, 'bulk-tailor', { mode: 'skip' }, 'sm', !eligible || queueing)}
      ${btn(queueing ? 'Queueing…' : `Tailor${eligible ? ` ${eligible}` : ''}`, 'bulk-tailor', { mode: 'tailor' }, 'sm primary', !eligible || queueing)}
    </div>
    <div class="row wrap gap10 xs muted" style="padding:10px 18px;border-bottom:1px solid var(--line2)">
      <label class="row gap6"><input type="checkbox" data-act="tailor-regenerate" ${S.tailorRegenerate ? 'checked' : ''}> Redo jobs that already have a packet</label>
      <span class="grow"></span><span>Each job is tailored (guided by its ranking when it has one), then independently reviewed on the server. You can leave this page.</span>
    </div>
    ${head}${rows}</section></div>`;
}

function renderTailor() {
  const queue = D.tailorQueue; if (!queue) return loading();
  const outside = S.tailorMode === 'single' && D.packet && !queue.some(j => j.id === D.packet.job.id);
  if (outside) return renderTailorSingle(queue);
  if (!queue.length && D.tailorFacets.total_all) return `<div class="stack">${renderTaskStack()}${renderPacketFilters('tailor', D.tailorFacets)}${pfNoMatch('tailor')}</div>`;
  if (!queue.length) return `<section class="card">${empty('Nothing to tailor yet', 'Shortlist a job in Discovery and it will appear here.', btn('Go to triage', 'go', { screen: 'discovery', mode: 'triage' }, 'primary'))}</section>`;
  if (S.tailorMode === 'bulk') return renderBulkTailor(queue);
  return renderTailorSingle(queue);
}
function renderTailorSingle(queue) {
  const p = D.packet; if (!p) return loading();
  const job = p.job, r = p.resume;
  const locked = PACKET_LOCKED.includes(job.status);
  // The open job keeps its place in the picker even when the queue filter hides it.
  const extra = queue.some(j => j.id === job.id) ? ''
    : `<option value="${job.id}" selected>${esc(job.company)} — ${esc(job.title)} (${esc(statusText(job.status))})</option>`;
  const picker = `<select class="select" style="margin:0;width:auto;max-width:320px" id="tailor-pick" data-act-change="pick-tailor">${extra}${queue.map(j => `<option value="${j.id}" ${j.id === S.tailorJob ? 'selected' : ''}>${esc(j.company)} — ${esc(j.title)}${j.status === 'needs_retailor' ? ' (re-tailor)' : j.has_resume ? '' : ' (not tailored)'}</option>`).join('')}</select>`;
  const task = activeTask(job.id), working = !!task || isBusy('queue' + job.id);
  const generating = working && task?.mode !== 'skip', skipping = task?.mode === 'skip';
  const stack = renderTaskStack() + renderPacketFilters('tailor', D.tailorFacets) + (locked ? `<section class="banner ${job.status === 'applied' ? '' : 'warn'} row wrap gap10" style="margin:0">
      <span class="grow" style="min-width:220px">${job.status === 'applied'
        ? '<b>Already applied.</b> Edits here no longer change what you submitted.'
        : '<b>This packet is approved.</b> Edits below change the approved resume and cover letter directly. To regenerate it or skip tailoring, send it back for revision first.'}</span>
      ${job.status === 'approved' ? btn(label('revise', 'Send back for revision', 'Sending back…'), 'revise', { id: job.id }, 'sm', isBusy('revise')) + btn('Open apply kit', 'open-kit', { id: job.id }, 'sm good') : ''}</section>` : '');
  if (!r) {
    return `<div class="stack">${stack}<section class="card row gap16 wrap"><div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">Tailoring for ${esc(job.title)} · ${esc(job.company)}</div><div class="card-sub">Fit ${fitTxt(job.fit)} · no packet generated yet</div></div>${picker}</section>
      <section class="card">${empty(generating ? 'Drafting your packet…' : skipping ? 'Reviewing your master resume…' : 'No packet yet', generating ? `${task ? `${STAGE_LABEL[task.stage]} · ${task.progress}%. ` : ''}Tailoring and an independent review run on the server. You can leave this page.` : skipping ? `${STAGE_LABEL[task.stage]}. Your master resume is used unchanged; the reviewer is scoring it against this posting.` : 'Generate a tailored resume, cover letter and reviewer score. Tailoring may only cite your master resume and verified evidence. If this posting needs no tailoring, skip it and send your master resume as-is.', btn(generating ? 'Generating…' : 'Generate tailored packet', 'generate', { id: job.id }, 'primary lg', working) + btn(skipping ? 'Skipping…' : 'Skip tailoring', 'skip-tailor', { id: job.id }, 'lg', working) + btn('Read the posting', 'open-job', { id: job.id }))}</section></div>`;
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
  return `<div class="stack">${stack}
  <section class="card row gap16 wrap" style="padding:16px 20px">
    <div class="grow" style="min-width:220px"><div class="card-title" style="font-size:16px">Tailoring for ${esc(job.title)} · ${esc(job.company)}</div><div class="card-sub">v${r.version} · generated ${esc(r.generated)} · ${r.untailored ? 'master resume, not tailored' : 'edits must trace to verified evidence'}</div></div>
    ${picker}
    <div class="row gap14"><div class="metric"><div class="n" style="color:${r.reviewer_score >= p.min_review_score ? 'var(--good)' : 'var(--warn)'}">${fitTxt(r.reviewer_score)}</div><div class="l">REVIEWER</div></div>
      <div class="metric"><div class="n">${r.ats_pct == null ? '—' : r.ats_pct + '%'}</div><div class="l">ATS KEYWORDS</div></div>
      ${btn(generating ? (r.untailored ? 'Tailoring…' : 'Regenerating…') : (r.untailored ? 'Tailor instead' : 'Regenerate'), 'generate', { id: job.id }, '', working || locked)}
      ${r.untailored ? '' : btn(skipping ? 'Skipping…' : 'Skip tailoring', 'skip-tailor', { id: job.id }, '', working || locked)}
      ${btn('Job description', 'open-job', { id: job.id })}
      ${locked ? '' : btn('Send to approval', 'go', { screen: 'approvals', approval: job.id }, 'primary')}</div>
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
  if (!D.approvalFacets.total_all) return renderApprovalsBody(list);
  return `<div class="stack">${renderPacketFilters('approvals', D.approvalFacets)}${list.length ? renderApprovalsBody(list) : pfNoMatch('approvals')}</div>`;
}
function renderApprovalsBody(list) {
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
      <div class="row gap8 wrap">${btn('Job details', 'open-job', { id: job.id })}${job.url ? `<a class="btn" href="${attr(job.url)}" target="_blank" rel="noopener">Open posting ↗</a>` : ''}${approved ? btn('Open apply kit', 'open-kit', { id: job.id }, 'good') : `${btn(label('revise', 'Needs revision', 'Sending back…'), 'revise', { id: job.id }, '', isBusy('revise'))}${btn(label('approve', 'Approve → apply kit', 'Approving…'), 'approve', { id: job.id }, 'good', isBusy('approve') || !r)}`}</div></div>
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
  const head = `<div class="row wrap gap10"><div class="row wrap gap10 grow statrow">${stats.map(([n, l, c]) => `<div class="card" style="padding:11px 15px;min-width:130px;border-radius:11px"><div class="mono bb" style="font-size:20px;color:${c}">${esc(n)}</div><div class="xs muted mt4">${l}</div></div>`).join('')}</div>
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
    <div class="row wrap gap10 mb16 movebar" style="background:#f8f9fd;border:1px solid #eaecf5;border-radius:12px;padding:12px 14px"><span class="caps none">MOVE TO</span>
      <div class="row wrap gap6 grow">${t.stages.map(st => btn(esc(st.label), 'move-stage', { app: sel.id, stage: st.id }, 'sm ' + (st.id === sel.stage ? 'primary' : ''))).join('')}</div><span class="xs muted none drag-hint">or drag the card between columns</span></div>
    <div class="split gap18">
      <div style="flex:1 1 320px;min-width:0"><div class="caps mb12">HISTORY</div>
        <div class="col">${sel.timeline.map(e => `<div class="tl"><div class="tl-rail"><span class="d" style="background:${KIND_DOT[e.kind] || '#c6cad8'}"></span><span class="l"></span></div><div style="padding-bottom:16px"><div class="small b">${esc(e.title)}</div><div class="xs muted mt4 pretty">${eventTime(e)}${e.detail && e.kind !== 'stage' ? ' · ' + esc(e.detail) : ''}</div>${eventContact(e)}</div></div>`).join('')}</div>
        <div class="logbox"><select class="select" style="width:auto;padding:6px 8px" id="log-kind">${['note', 'call', 'email', 'referral'].map(k => `<option value="${k}">${k[0].toUpperCase() + k.slice(1)}</option>`).join('')}</select>
          <input class="input bare grow" id="log-text" placeholder="Log something — call, email, referral…" data-enter="log-event" data-app="${sel.id}">${btn(label('log', 'Add', 'Adding…'), 'log-event', { app: sel.id }, 'sm', isBusy('log'))}</div>
      </div>
      <div style="flex:1 1 240px;min-width:0" class="col gap12">
        <div class="box brand" style="padding:13px"><div class="caps mb8">NEXT STEP</div><div class="base b">${esc(sel.next)}</div><div class="small muted mt4">${esc(sel.next_date)}</div></div>
        ${renderRecruiters(sel)}
        <div class="box" style="padding:13px"><div class="caps mb8">SUBMITTED</div><div class="small ink2" style="line-height:1.5">${esc(sel.docs)}</div>${sel.notes ? `<div class="xs muted mt8 pretty">${esc(sel.notes)}</div>` : ''}</div>
        <div class="box" style="padding:13px;border-color:#e2ddff;background:var(--brand-softer)"><div class="row gap6 mb8">${pill('AI', 'brand')}<span class="xs b" style="color:var(--brand-ink)">Follow-up draft</span></div>
          <div class="small ink2 pretty" style="line-height:1.55;white-space:pre-wrap">${esc(sel.draft || 'No draft yet. Generate one — nothing is ever sent for you.')}</div>
          <div class="row gap8 mt10">${sel.draft ? btn('Review &amp; copy', 'copy-draft', { app: sel.id }, 'sm primary') : ''}${btn(label('nudge' + sel.id, sel.draft ? 'Regenerate' : 'Draft follow-up', 'Drafting…'), 'draft-nudge', { app: sel.id, stay: 1 }, 'sm' + (sel.draft ? '' : ' primary'), isBusy('nudge' + sel.id))}</div></div>
        ${S.scheduling ? renderScheduleForm(sel) : btn('Schedule an interview', 'schedule-start', {}, 'block')}
      </div></div>
  </section></div>`;
}
/* recruiters and interviews */
// Some browsers still report renamed IANA zones; show and store the current names.
const TZ_RENAMED = { 'Asia/Calcutta': 'Asia/Kolkata', 'Asia/Saigon': 'Asia/Ho_Chi_Minh', 'Asia/Katmandu': 'Asia/Kathmandu', 'Asia/Rangoon': 'Asia/Yangon',
  'Europe/Kiev': 'Europe/Kyiv', 'Atlantic/Faeroe': 'Atlantic/Faroe', 'America/Buenos_Aires': 'America/Argentina/Buenos_Aires', 'Pacific/Enderbury': 'Pacific/Kanton' };
const tzName = tz => TZ_RENAMED[tz] || tz;
const BROWSER_TZ = (() => { try { return tzName(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'); } catch { return 'UTC'; } })();
let tzOptionsCache = null;
function tzOptions() {
  if (tzOptionsCache) return tzOptionsCache;
  let zones = [];
  try { zones = [...new Set(Intl.supportedValuesOf('timeZone').map(tzName))].sort(); } catch { /* older browsers */ }
  if (!zones.length) zones = ['UTC', 'Asia/Kolkata', 'Asia/Dubai', 'Asia/Singapore', 'Europe/London', 'Europe/Berlin', 'America/New_York', 'America/Chicago', 'America/Los_Angeles', 'Australia/Sydney'];
  if (!zones.includes('UTC')) zones = ['UTC', ...zones];
  if (!zones.includes(BROWSER_TZ)) zones = [BROWSER_TZ, ...zones];
  const offset = tz => { try { return new Intl.DateTimeFormat('en-US', { timeZone: tz, timeZoneName: 'shortOffset' }).formatToParts(new Date()).find(p => p.type === 'timeZoneName')?.value || ''; } catch { return ''; } };
  tzOptionsCache = zones.map(tz => [tz, `${tz.replace(/_/g, ' ')}${offset(tz) ? ` (${offset(tz)})` : ''}`]);
  return tzOptionsCache;
}
function eventTime(e) {
  if (!e.at_local) return fmtDateTime(e.at);
  return esc(e.at_local) + (e.timezone !== BROWSER_TZ ? ` <span class="faint">(${fmtDateTime(e.at)} your time)</span>` : '');
}
function eventContact(e) {
  const bits = [e.contact_name && esc(e.contact_name), e.contact_email && `<a href="mailto:${attr(e.contact_email)}">${esc(e.contact_email)}</a>`,
    e.contact_phone && `<a href="tel:${attr(e.contact_phone.replace(/[^+\d]/g, ''))}">${esc(e.contact_phone)}</a>`].filter(Boolean);
  return bits.length ? `<div class="xs muted mt4">${bits.join(' · ')}</div>` : '';
}
function renderRecruiters(sel) {
  const list = sel.recruiters || [];
  if (!S.editingRecruiter) {
    return `<div class="box" style="padding:13px"><div class="row between mb8"><span class="caps">RECRUITERS${list.length > 1 ? ` · ${list.length}` : ''}</span><a href="#" class="xs" data-act="edit-recruiter">${list.length ? 'Edit' : 'Add'}</a></div>
      ${list.length ? `<div class="col gap10">${list.map((r, i) => `<div${i ? ' style="padding-top:10px;border-top:1px solid var(--line3)"' : ''}>
        <div class="base b">${esc(r.name || r.email || r.phone)}${i === 0 && list.length > 1 ? ' <span class="xs muted" style="font-weight:400">· primary</span>' : ''}</div>
        ${r.email && r.name ? `<div class="small mt4"><a href="mailto:${attr(r.email)}">${esc(r.email)}</a></div>` : ''}
        ${r.phone && (r.name || r.email) ? `<div class="small mt4"><a href="tel:${attr(r.phone.replace(/[^+\d]/g, ''))}">${esc(r.phone)}</a></div>` : ''}</div>`).join('')}</div>`
        : `<div class="base b">No contact captured</div><div class="small muted mt4">Add recruiters to personalise nudges</div>`}</div>`;
  }
  const rows = S.recruiterDraft.map((r, i) => `<div class="col gap6"${i ? ' style="padding-top:10px;border-top:1px solid var(--line3)"' : ''}>
      <div class="row between"><span class="xs muted">${i === 0 ? 'Primary recruiter' : `Recruiter ${i + 1}`}</span>${btn('Remove', 'recruiter-remove', { index: i }, 'sm ghost')}</div>
      <input class="input" style="margin:0" id="rec-${i}-name" data-rec="${i}" data-recfield="name" value="${attr(r.name)}" placeholder="Name">
      <input class="input" style="margin:0" id="rec-${i}-email" data-rec="${i}" data-recfield="email" type="email" value="${attr(r.email)}" placeholder="Email (optional)">
      <input class="input" style="margin:0" id="rec-${i}-phone" data-rec="${i}" data-recfield="phone" type="tel" value="${attr(r.phone)}" placeholder="Phone (optional)"></div>`).join('');
  return `<div class="box" style="padding:13px"><div class="caps mb8">RECRUITERS</div>
    <div class="col gap10">${rows || '<div class="small muted">No recruiters. Add one below.</div>'}</div>
    ${S.recruiterDraft.length < 20 ? btn('+ Add recruiter', 'recruiter-add', {}, 'sm mt10') : ''}
    <div class="row gap8 mt10">${btn('Cancel', 'edit-recruiter-cancel', {}, 'sm')}${btn(label('recruiter', 'Save', 'Saving…'), 'save-recruiter', { app: sel.id }, 'sm primary', isBusy('recruiter'))}</div></div>`;
}
function renderScheduleForm(sel) {
  const d = S.ivDraft;
  const field = (key, extra) => `<input class="input" style="margin:0" id="iv-${key}" data-iv="${key}" value="${attr(d[key])}" ${extra}>`;
  return `<div class="box" style="padding:13px"><div class="caps mb8">SCHEDULE AN INTERVIEW</div><div class="col gap8">
    ${field('title', 'placeholder="e.g. Panel interview"')}
    <div class="row gap8 wrap">${field('at', 'type="datetime-local" style="margin:0;flex:1 1 180px"')}
      <select class="select" style="margin:0;flex:1 1 180px" id="iv-timezone" data-iv="timezone" aria-label="Time zone">${tzOptions().map(([v, t]) => `<option value="${attr(v)}" ${v === d.timezone ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></div>
    <div class="xs muted mt4">Contact for this interview (optional)</div>
    ${field('contact_name', 'placeholder="Name"')}
    <div class="row gap8 wrap">${field('contact_email', 'type="email" placeholder="Email" style="margin:0;flex:1 1 160px"')}${field('contact_phone', 'type="tel" placeholder="Phone" style="margin:0;flex:1 1 140px"')}</div></div>
    <div class="row gap8 mt10">${btn('Cancel', 'schedule-cancel', {}, 'sm')}${btn(label('schedule', 'Save', 'Saving…'), 'schedule-save', { app: sel.id }, 'sm primary', isBusy('schedule'))}</div></div>`;
}
const KIND_DOT = { applied: '#5b4ae8', stage: '#0f9c86', note: '#c6cad8', call: '#0f9c86', email: '#a79bf0', referral: '#a79bf0', interview: '#e0a136' };

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
  else if (S.jobTab === 'structured') tab = renderJobStructured(j);
  else tab = `<div style="padding:20px 22px">${textareaF('jobnotes', j.id, j.notes, 'placeholder="Anything worth remembering about this role…" style="min-height:160px"')}${btn(label('jobnotes', 'Save notes', 'Saving…'), 'save-job-notes', { id: j.id }, 'primary sm mt10', isBusy('jobnotes'))}</div>`;
  return `<div class="overlay" data-act="overlay-close"><div class="modal" role="dialog" aria-label="Job detail">
    <div class="modal-head"><div class="row top-align gap16 wrap"><div class="avatar lg">${esc(initials(j.company))}</div>
      <div style="flex:1 1 240px;min-width:0"><div style="font-size:21px;font-weight:600;letter-spacing:-.02em">${esc(j.title)}</div><div class="muted mt4" style="font-size:13.5px">${esc(j.company)}${j.location ? ' · ' + esc(j.location) : ''} · ${esc(j.board)} · found ${esc(j.posted)}</div></div>
      <div class="row gap8 wrap"><div class="scorebox sm"><div class="n">${fitTxt(j.fit)}</div><div class="l">FIT</div></div>
        ${j.url ? `<a class="btn" href="${attr(j.url)}" target="_blank" rel="noopener">Open posting ↗</a>` : ''}
        ${btn(j.resume_id ? 'Open packet' : 'Tailor resume', 'modal-tailor', { id: j.id }, 'primary')}${btn('Close', 'close-job')}</div></div>
      <div class="wizard">${j.wizard.map((w, i) => `<div class="${w.done ? 'done' : i === hereIdx ? 'here' : ''}"><div class="row gap6"><span class="m">${w.done ? '✓' : i + 1}</span><span class="small b" style="color:${w.done ? 'var(--ink)' : i === hereIdx ? 'var(--brand-ink)' : 'var(--faint)'}">${w.label}</span></div><div class="xs muted mt4">${esc(i === hereIdx && !w.done ? 'next step' : w.meta)}</div></div>`).join('')}</div></div>
    <div class="split gap16" style="padding:18px 24px 24px">
      <section class="card flush" style="flex:1 1 420px;min-width:0"><div class="tabs under">${[['description', 'Description'], ['structured', 'Structured JD'], ['requirements', 'Requirements map'], ['notes', 'Notes']].map(([id, t]) => `<button class="${S.jobTab === id ? 'on' : ''}" data-act="job-tab" data-id="${id}">${t}</button>`).join('')}</div>${tab}</section>
      <div class="col gap14" style="flex:1 1 280px;min-width:0">
        <section class="card pad18"><div class="row gap8 mb14">${pill('AI', 'brand')}<span class="card-title sm">Fit breakdown</span></div>
          ${j.fit == null ? `<div class="small muted mb12">Not ranked yet.</div>${btn(label('rank' + j.id, 'Rank this job', 'Queueing…'), 'rank-job', { id: j.id }, 'soft', isBusy('rank' + j.id))}` : `
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
          : `<span class="small muted">This job is ${esc(statusText(j.status))} — manage it from ${j.status === 'applied' ? 'the Tracker' : ['tailored', 'needs_retailor'].includes(j.status) ? 'Tailor' : 'Approvals'}.</span>`}</div></section>
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
const RENDER = { overview: renderOverview, today: renderToday, discovery: renderDiscovery, tailor: renderTailor, approvals: renderApprovals, tracker: renderTracker, monitoring: renderMonitoring, knowledge: renderKnowledge, settings: renderSettings };
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
  $('#overlays').innerHTML = (S.jobOpen ? renderJobModal() : '') + (S.kitJob ? renderKit() : '') + (S.paletteOpen ? renderPalette() : '') + (S.mon?.request ? renderMonRequestModal() : '');
  document.body.style.overflow = (S.jobOpen || S.kitJob || S.paletteOpen || S.mon?.request) ? 'hidden' : '';
  if (fid) {
    const el = document.getElementById(fid);
    if (el) { el.focus({ preventScroll: true }); if (selRange) { try { el.setSelectionRange(...selRange); } catch { /* ignore */ } } }
  }
}

/* ───────────── background tailoring ───────────── */
let taskTimer = null;
async function refreshTasks() {
  const before = new Map((D.tasks || []).map(t => [t.id, t.status]));
  D.tasks = await api('/api/tailor-tasks').catch(() => D.tasks || []);
  const finished = D.tasks.filter(t => ['queued', 'running'].includes(before.get(t.id)) && !['queued', 'running'].includes(t.status));
  if (finished.length) {
    const ok = finished.filter(t => t.status === 'succeeded'), bad = finished.filter(t => t.status === 'failed');
    const one = finished.length === 1 ? finished[0] : null;
    toast(one ? (one.status === 'succeeded' ? `${one.company}: packet ready · reviewer ${fitTxt(one.result?.reviewer_score)}${one.result?.auto_approved ? ' · auto-approved' : ''}` : one.status === 'failed' ? `${one.company}: ${one.error}` : `${one.company}: cancelled`)
      : `${ok.length} packet${ok.length === 1 ? '' : 's'} ready${bad.length ? ` · ${bad.length} failed` : ''}`, !ok.length && !!bad.length);
  }
  scheduleTaskPoll();
  return { finished };
}
// Poll while anything is queued or running, on every screen, so finished jobs are announced wherever you are.
function scheduleTaskPoll() {
  clearTimeout(taskTimer); taskTimer = null;
  if (!(D.tasks || []).some(t => t.status === 'queued' || t.status === 'running')) return;
  taskTimer = setTimeout(async () => {
    const { finished } = await refreshTasks();
    if (S.screen !== 'tailor') return;
    if (finished.length) await load(); else render();
  }, 2500);
}
function queueTailoring(ids, mode, regenerate, busyKey, onQueued) {
  if (!ids.length) return;
  return busy(busyKey, async () => {
    const out = await api('/api/tailor-tasks', { method: 'POST', body: { ids, mode, regenerate } });
    onQueued?.(out);
    await refreshTasks();
    return out;
  }, out => {
    const n = out.queued.length, verb = mode === 'skip' ? 'Skipping tailoring for' : 'Tailoring';
    if (!n) return `Nothing queued: ${out.skipped[0]?.reason || 'no eligible jobs'}`;
    return `${verb} ${n} job${n === 1 ? '' : 's'} in the background${out.skipped.length ? ` · ${out.skipped.length} skipped` : ''}`;
  });
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
    ...(el.dataset.mode ? { discMode: el.dataset.mode } : {}), ...(el.dataset.job ? { tailorJob: Number(el.dataset.job), tailorMode: 'single', tailorTab: 'diff' } : {}),
    ...(el.dataset.approval ? { approvalJob: Number(el.dataset.approval) } : {}), ...(el.dataset.app ? { selApp: Number(el.dataset.app), trackerView: 'board' } : {}),
    jobOpen: null, kitJob: null, paletteOpen: false,
  }),
  variant: el => {
    if (el.dataset.kind === 'today') S.todayVariant = el.dataset.id;
    else if (el.dataset.kind === 'tailor') S.tailorMode = el.dataset.id;
    else { S.discMode = el.dataset.id; S.manualMissing = null; }
    go(S.screen);
  },
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
  'add-evidence-for': el => {
    S.jobOpen = null;
    // Preserve an existing claim draft while incorporating the new job gap.
    const current = fv('ev', 'claim', S.evidencePrefill || '');
    S.evidencePrefill = current ? current + '\n' + el.dataset.gap + ': ' : el.dataset.gap + ': ';
    setF('ev', 'claim', S.evidencePrefill);
    knowledgeState().editors.evidence = { id: null, form: 'ev' };
    go('knowledge', { knowledgeSection: 'evidence' });
    document.getElementById('ev-claim')?.focus();
  },
  'rank-job': el => busy('rank' + el.dataset.id, async () => {
    const out = await api(`/api/jobs/${el.dataset.id}/rank`, { method: 'POST' });
    watchRanking(out.queued);
    return out;
  }, rankQueuedMsg),
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
  // Shortlist the selection so it reaches the Tailor queue, then open Bulk tailor with exactly those jobs ticked.
  'bulk-to-tailor': () => busy('bulkToTailor', async () => {
    const ids = [...S.checked];
    await api('/api/jobs/bulk-triage', { method: 'POST', body: { ids, action: 'shortlist' } });
    const queue = await api('/api/tailor/queue'), inQueue = new Set(queue.map(j => j.id));
    const queued = ids.filter(id => inQueue.has(id));
    S.checked.clear();
    go('tailor', { tailorMode: 'bulk', tailorChecked: new Set(queued) });
    return { queued: queued.length, left: ids.length - queued.length };
  }, r => `${r.queued} job${r.queued === 1 ? '' : 's'} ready to bulk tailor${r.left ? ` · ${r.left} already approved or applied` : ''}`),
  'select-all-matching': (el, e) => {
    e.preventDefault();
    return busy('selectAll', async () => {
      const f = S.filters;
      const out = await api('/api/jobs/search?' + qs({ ...f, min_fit: f.min_fit || '', ids_only: true }));
      out.ids.forEach(id => S.checked.add(id));
    });
  },
  'clear-selection': (el, e) => { e.preventDefault(); S.checked.clear(); render(); },
  'bulk-rank': () => busy('bulkRank', async () => {
    const out = await api('/api/jobs/bulk-rank?rerank=true', { method: 'POST', body: { ids: [...S.checked] } });
    S.checked.clear(); await load(); watchRanking(out.queued);
    return out;
  }, rankQueuedMsg),
  'bulk-delete': () => {
    const count = S.checked.size;
    if (!confirm(`Permanently delete ${count} job${count === 1 ? '' : 's'}? Tailored resumes for them are deleted too. Approved or applied jobs are kept unless you confirm again.`)) return;
    return busy('bulkDelete', async () => {
      const ids = [...S.checked];
      let out = await api('/api/jobs/bulk-delete', { method: 'POST', body: { ids } });
      if (out.protected.length && confirm(`${out.protected.length} of them are approved or applied and have tracker history. Delete those too?`)) {
        const more = await api('/api/jobs/bulk-delete', { method: 'POST', body: { ids: out.protected, include_submitted: true } });
        out = { ...out, deleted: out.deleted + more.deleted, protected: [] };
      }
      S.checked = new Set(out.protected);
      if (!S.checked.has(S.selJob)) S.selJob = null;
      await load();
      return out;
    }, out => `Deleted ${out.deleted} job${out.deleted === 1 ? '' : 's'}${out.protected.length ? ` · kept ${out.protected.length} approved or applied` : ''}`);
  },
  'pf-reset': el => { S.pf[el.dataset.kind] = pfDefaults(el.dataset.kind); load(); },
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
  'toggle-apify-import': () => { S.apifyImport = !S.apifyImport; if (S.apifyImport && !D.remoteRuns) A['load-remote'](); else render(); },
  'load-remote': () => busy('remoteRuns', async () => {
    D.remoteRuns = null; render();
    D.remoteRuns = (await api('/api/search/apify/remote-runs?limit=20')).runs;
  }),
  'pick-remote': el => { const id = el.dataset.id; S.importPick.has(id) ? S.importPick.delete(id) : S.importPick.add(id); render(); },
  'import-remote': () => busy('importRuns', async () => {
    const out = await api('/api/search/apify/import', { method: 'POST', body: { external_run_ids: [...S.importPick] } });
    if (out.results.every(r => !r.ok)) throw new Error(out.results[0].error);
    S.importPick.clear();
    D.remoteRuns = (await api('/api/search/apify/remote-runs?limit=20')).runs;
    await afterPull(out.results);
    return out;
  }, out => summarizePull(out.results)),
  'refresh-pending': () => busy('refreshPending', async () => {
    const out = await api('/api/search/apify/runs/refresh-pending', { method: 'POST' });
    if (out.results.length && out.results.every(r => !r.ok)) throw new Error(out.results[0].error);
    await afterPull(out.results);
    return out;
  }, out => summarizePull(out.results)),
  'pull-run': el => busy('pull' + el.dataset.id, async () => {
    const run = await api(`/api/search/apify/runs/${el.dataset.id}${el.dataset.reimport ? '?reimport=true' : ''}`);
    await afterPull([{ ...run, ok: true }]);
    return run;
  }, run => run.completed ? summarizePull([{ ...run, ok: true }]) : `Still ${run.status.toLowerCase()} on Apify — check again shortly`),
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
  'ranking-retry': el => busy('rankRetry' + el.dataset.job, async () => {
    const out = await api(`/api/jobs/${el.dataset.job}/rank`, { method: 'POST' });
    await loadRanking();
    return out;
  }, rankQueuedMsg),
  'ranking-refresh': () => busy('rankRefresh', () => loadRanking()),
  'rank-batch': () => busy('rankBatch', async () => {
    const out = await api('/api/jobs/rank-batch', { method: 'POST' });
    if (!out.queued.length && !out.skipped.length) throw new Error('Every job is already ranked');
    await load(); watchRanking(out.queued);
    return out;
  }, rankQueuedMsg),
  // tailor
  'pick-tailor': el => go('tailor', { tailorJob: Number(el.value) }),
  'tailor-tab': el => { S.tailorTab = el.dataset.id; S.editing = null; render(); },
  generate: el => queueTailoring([Number(el.dataset.id)], 'tailor', true, 'queue' + el.dataset.id),
  'task-cancel': el => busy('tasks', async () => { await api('/api/tailor-tasks/cancel', { method: 'POST', body: { ids: [Number(el.dataset.id)] } }); await refreshTasks(); render(); }),
  'tasks-cancel': () => busy('tasks', async () => { await api('/api/tailor-tasks/cancel', { method: 'POST', body: {} }); await refreshTasks(); render(); }, 'Queued jobs cancelled'),
  'tasks-clear': () => busy('tasks', async () => { await api('/api/tailor-tasks/clear', { method: 'POST' }); await refreshTasks(); render(); }),
  'task-retry': el => {
    const task = (D.tasks || []).find(t => t.id === Number(el.dataset.id));
    if (!task || retried(task)) return;
    return queueTailoring([task.job_id], task.mode, true, 'retry' + task.id);
  },
  'task-open': el => go('tailor', { tailorMode: 'single', tailorJob: Number(el.dataset.job), tailorTab: 'diff' }),
  'tailor-check': (el, e) => {
    e.stopPropagation();
    const id = Number(el.dataset.id);
    S.tailorChecked.has(id) ? S.tailorChecked.delete(id) : S.tailorChecked.add(id);
    render();
  },
  'tailor-select': el => {
    const queue = D.tailorQueue || [], which = el.dataset.which;
    S.tailorChecked = new Set(which === 'all' ? queue.map(j => j.id) : which === 'untailored' ? queue.filter(j => !j.has_resume || j.status === 'needs_retailor').map(j => j.id) : []);
    render();
  },
  'tailor-regenerate': (el, e) => { e.stopPropagation(); S.tailorRegenerate = !S.tailorRegenerate; render(); },
  'bulk-tailor': el => {
    const ids = (D.tailorQueue || []).filter(j => S.tailorChecked.has(j.id) && !activeTask(j.id) && (S.tailorRegenerate || !j.has_resume || j.status === 'needs_retailor')).map(j => j.id);
    return queueTailoring(ids, el.dataset.mode, S.tailorRegenerate, 'bulkTailor', () => ids.forEach(id => S.tailorChecked.delete(id)));
  },
  'skip-tailor': el => queueTailoring([Number(el.dataset.id)], 'skip', true, 'queue' + el.dataset.id),
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
    await api(`/api/jobs/${el.dataset.id}/revision`, { method: 'POST', body: { notes: fv('approvalNotes', el.dataset.id, D.approval?.job?.id === Number(el.dataset.id) ? D.approval.notes || '' : '') } });
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
  'edit-recruiter': (el, e) => {
    e.preventDefault();
    const sel = D.tracker.applications.find(a => a.id === S.selApp);
    S.recruiterDraft = (sel?.recruiters || []).map(r => ({ ...r }));
    if (!S.recruiterDraft.length) S.recruiterDraft.push({ name: '', email: '', phone: '' });
    S.editingRecruiter = true; render();
  },
  'edit-recruiter-cancel': () => { S.editingRecruiter = false; render(); },
  'recruiter-add': () => { S.recruiterDraft.push({ name: '', email: '', phone: '' }); render(); document.getElementById(`rec-${S.recruiterDraft.length - 1}-name`)?.focus(); },
  'recruiter-remove': el => { S.recruiterDraft.splice(Number(el.dataset.index), 1); render(); },
  'save-recruiter': el => busy('recruiter', async () => {
    const recruiters = S.recruiterDraft.map(r => ({ name: r.name.trim(), email: r.email.trim(), phone: r.phone.trim() })).filter(r => r.name || r.email || r.phone);
    await api(`/api/applications/${el.dataset.app}/recruiters`, { method: 'PUT', body: { recruiters } });
    S.editingRecruiter = false; await load();
  }, 'Recruiters saved'),
  'copy-draft': el => copyText(D.tracker.applications.find(a => a.id === Number(el.dataset.app)).draft),
  'schedule-start': () => {
    let tz = BROWSER_TZ; try { tz = tzName(localStorage.getItem('interviewTimezone') || tz); } catch { /* storage blocked */ }
    if (!tzOptions().some(([v]) => v === tz)) tz = BROWSER_TZ;
    S.ivDraft = { title: '', at: '', timezone: tz, contact_name: '', contact_email: '', contact_phone: '' };
    S.scheduling = true; render(); document.getElementById('iv-title')?.focus();
  },
  'schedule-cancel': () => { S.scheduling = false; render(); },
  'schedule-save': el => {
    const d = S.ivDraft;
    if (!d.at) return toast('Pick a date and time', true);
    return busy('schedule', async () => {
      await api(`/api/applications/${el.dataset.app}/events`, { method: 'POST', body: {
        kind: 'interview', title: d.title.trim() || 'Interview', detail: 'Scheduled', local_time: d.at, timezone: d.timezone,
        contact_name: d.contact_name.trim(), contact_email: d.contact_email.trim(), contact_phone: d.contact_phone.trim() } });
      try { localStorage.setItem('interviewTimezone', d.timezone); } catch { /* storage blocked */ }
      S.scheduling = false; await load();
    }, 'Interview scheduled');
  },
  ...KNOWLEDGE_ACTIONS,
  ...SETTINGS_ACTIONS,
  ...RESUME_DATA_ACTIONS,
  ...JOB_DATA_ACTIONS,
  ...MONITORING_ACTIONS,
};
function tagFallback(form, key) {
  if (form === 'sp') return D.searchProfile?.profile?.[key] || [];
  if (form === 'preferences') { const c = CHIP_FIELDS.find(x => x[0] === key); return (c && c[5] === 'sp' ? D.profile?.search_preferences?.[key] : D.profile?.[key]) || []; }
  return [];
}
async function moveStage(appId, stage) {
  try { await api(`/api/applications/${appId}/stage`, { method: 'POST', body: { stage } }); S.selApp = Number(appId); await load(); }
  catch (e) { toast(e.message, true); }
}

/* ───────────── events ───────────── */
document.addEventListener('click', e => {
  const knowledgeLink = e.target.closest('a[href^="#knowledge/"]');
  if (knowledgeLink && S.screen === 'knowledge' && !e.metaKey && !e.ctrlKey) { e.preventDefault(); knowledgeNavigate(knowledgeLink.hash.split('/')[1]); return; }
  const link = e.target.closest('a[href^="#settings/"]');
  if (link && S.screen === 'settings' && !e.metaKey && !e.ctrlKey) { e.preventDefault(); settingsNavigate(link.hash.split('/')[1]); return; }
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
  if (knowledgeFilterEvent(el)) return;
  if (el.dataset.authEntry || el.dataset.authNotes) { settingsAuthUpdate(); return; }
  if (el.dataset.rdPath) { resumeDataInput(el); return; }
  if (el.dataset.jdPath) { jobDataInput(el); return; }
  if (el.dataset.form && el.dataset.field) {
    setF(el.dataset.form, el.dataset.field, el.type === 'checkbox' ? el.checked : el.value);
    if (el.dataset.live || el.type === 'radio') render();
  }
  if (el.dataset.tag && S.screen === 'settings') settingsTouched(el.dataset.form);
  if (el.dataset.rec !== undefined && S.recruiterDraft?.[el.dataset.rec]) S.recruiterDraft[el.dataset.rec][el.dataset.recfield] = el.value;
  if (el.dataset.iv && S.ivDraft) S.ivDraft[el.dataset.iv] = el.value;
  if (el.dataset.pf && el.dataset.pfkey === 'q') { S.pf[el.dataset.pf].q = el.value; clearTimeout(filterTimer); filterTimer = setTimeout(load, 250); }
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
  if (knowledgeFilterEvent(el)) return;
  if (el.dataset.authEntry) { settingsAuthUpdate(); return; }
  if (el.dataset.rdPath) { resumeDataInput(el); return; }
  if (el.dataset.jdPath) { jobDataInput(el); return; }
  if (el.dataset.form && el.dataset.field && (el.tagName === 'SELECT' || el.type === 'checkbox' || el.type === 'radio')) {
    setF(el.dataset.form, el.dataset.field, el.type === 'checkbox' ? el.checked : el.value);
    if (el.type === 'radio') render();
  }
  if (el.dataset.iv && S.ivDraft) S.ivDraft[el.dataset.iv] = el.value;
  if (el.dataset.pf && el.dataset.pfkey !== 'q') { const k = el.dataset.pfkey; S.pf[el.dataset.pf][k] = k.startsWith('min_') ? Number(el.value) : el.value; load(); }
  if (el.dataset.filter && el.dataset.filter !== 'q') { S.filters[el.dataset.filter] = el.dataset.filter === 'per_page' || el.dataset.filter === 'min_fit' ? Number(el.value) : el.value; S.filters.page = 1; load(); }
  if (el.dataset.triage && el.dataset.triage !== 'min_fit') { S.triage[el.dataset.triage] = el.checked; load(); }
  if (el.dataset.actChange) A[el.dataset.actChange](el, e);
  if (el.id === 'resume-file' && el.files[0]) { settingsUploadResume(el.files[0]); el.value = ''; }
});
document.addEventListener('focusout', e => {
  const el = e.target;
  if (el.dataset && el.dataset.tag && el.value.trim()) commitTag(el, false);
  if (el.dataset && el.dataset.actBlur === 'save-notes') {
    const id = el.dataset.id; if (el.value === (D.approval?.notes || '')) return;
    setF('approvalNotes', id, el.value);
    api(`/api/jobs/${id}/notes`, { method: 'PUT', body: { notes: el.value } }).then(() => { if (D.approval) D.approval.notes = el.value; toast('Notes saved'); }).catch(err => toast(err.message, true));
  }
});
function commitTag(el, redraw = true) {
  const { form, tag } = el.dataset, max = Number(el.dataset.max || 20);
  if (form === 'triage') { const tmp = { keywords: S.triage.keywords }; S.forms.__t = tmp; addTags('__t', 'keywords', el.value, max, S.triage.keywords); S.triage.keywords = S.forms.__t.keywords; delete S.forms.__t; el.value = ''; load(); return; }
  addTags(form, tag, el.value, max, tagFallback(form, tag)); el.value = '';
  if (redraw) render();
  else { const box = el.closest('.tagbox'); if (box) { box.querySelectorAll('.chip').forEach(c => c.remove()); el.insertAdjacentHTML('beforebegin', chips(form, tag, tagFallback(form, tag))); } }
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
window.addEventListener('hashchange', () => {
  const [screen, section] = location.hash.slice(1).split('/');
  if (!settingsCanLeave(screen, settingsSection(section))) { setHash(); return; }
  const before = S.screen + S.discMode + S.todayVariant + S.settingsSection + S.knowledgeSection;
  readHash(); setHash();
  if (before !== S.screen + S.discMode + S.todayVariant + S.settingsSection + S.knowledgeSection) {
    render(); load();
    if (S.screen === 'settings') document.getElementById('settings-heading')?.focus();
    if (S.screen === 'knowledge') document.getElementById('knowledge-heading')?.focus();
  }
});
window.addEventListener('beforeunload', e => {
  if (S.screen === 'settings' && settingsAnyDirty()) { e.preventDefault(); e.returnValue = ''; }
});
document.addEventListener('submit', e => {
  if (e.target.id === 'knowledge-editor-form') { e.preventDefault(); knowledgeSave(); }
  if (e.target.id === 'settings-editor') { e.preventDefault(); settingsSave(settingsForm()); }
});
document.addEventListener('toggle', e => {
  const id = e.target.dataset?.settingsDetails;
  if (id && e.target.isConnected) { if (e.target.open) SETTING_DETAILS.add(id); else SETTING_DETAILS.delete(id); }
}, true);

/* ───────────── boot ───────────── */
readHash();
setHash();
render();
refreshTasks();
load().then(async () => {
  try { const prof = await api('/api/profile'); D.profileRoles = prof.target_roles || []; } catch { /* optional */ }
});
