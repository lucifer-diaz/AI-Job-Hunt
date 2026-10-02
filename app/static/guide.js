/* Setup quest: the interactive, gamified setup guide. Loaded before app.js; uses the workspace's shared helpers.
   Progress comes from GET /api/setup/guide, which checks the account's real data — the guide never ticks a quest
   off by itself. It renders into its own #guide-root, outside the workspace's render(), so its animations are not
   restarted by every workspace re-render. */
'use strict';

const GUIDE = {
  data: null, open: false, view: 'map', sel: null, active: null, booted: false,
  shownXp: 0, timer: null, loading: false, lastFetch: 0, queue: [],
};
const GUIDE_POLL_MS = 15000;
const GUIDE_CONFETTI = ['#5b4ae8', '#a79bf0', '#0f9c86', '#e0a136', '#c22f4e', '#7c69ee'];
const guideReduced = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
const guideRoot = () => document.getElementById('guide-root');
const guideQuest = id => GUIDE.data?.quests?.find(q => q.id === id);

/* ───────────── data ───────────── */
async function guideFetch(method = 'GET', body) {
  const data = await api('/api/setup/guide', method === 'GET' ? {} : { method, body });
  return data && Array.isArray(data.quests) ? data : null;
}
// First load: remember what is already done (nothing to celebrate) and open the welcome screen for a new account.
async function guideBoot() {
  GUIDE.booted = true;
  try { GUIDE.data = await guideFetch(); } catch { return; }
  if (!GUIDE.data) return;
  GUIDE.shownXp = GUIDE.data.xp;
  const fresh = GUIDE.data.quests.filter(q => q.done && !GUIDE.data.celebrated.includes(q.id)).map(q => q.id);
  if (fresh.length) guideSave({ celebrated: fresh });  // done before the guide existed: no confetti for those
  if (!GUIDE.data.welcomed && !GUIDE.data.complete) { GUIDE.open = true; GUIDE.view = 'welcome'; }
  guideSchedule(); guideRender(); render();
}
// Later loads: anything newly done is celebrated once (per account, on any device).
async function guideRefresh(force = false) {
  if (!GUIDE.booted || GUIDE.loading || (!force && Date.now() - GUIDE.lastFetch < 2500)) return;
  GUIDE.loading = true; GUIDE.lastFetch = Date.now();
  try {
    const before = GUIDE.data, next = await guideFetch();
    if (!next) return;
    GUIDE.data = next;
    const fresh = next.quests.filter(q => q.done && !next.celebrated.includes(q.id));
    if (fresh.length) {
      const levelUp = before && next.level.number > before.level.number ? next.level : null;
      fresh.forEach((q, i) => GUIDE.queue.push({ quest: q, levelUp: i === fresh.length - 1 ? levelUp : null }));
      guideSave({ celebrated: fresh.map(q => q.id) });
      if (GUIDE.active && fresh.some(q => q.id === GUIDE.active)) GUIDE.active = null;
      guideNextCelebration();
    }
    guideSchedule(); guideRender(); render();
  } catch { /* the guide is optional: never let it break the workspace */ }
  finally { GUIDE.loading = false; }
}
function guideSave(state) { api('/api/setup/guide', { method: 'PUT', body: state }).catch(() => {}); }
// Poll while there is something to watch for, and only while the tab is visible.
function guideSchedule() {
  clearTimeout(GUIDE.timer);
  if (!GUIDE.data || GUIDE.data.complete) return;
  GUIDE.timer = setTimeout(() => { if (!document.hidden) guideRefresh(); guideSchedule(); }, GUIDE_POLL_MS);
}

/* ───────────── celebration ───────────── */
function guideNextCelebration() {
  const item = GUIDE.queue.shift(); if (!item) return;
  const root = guideRoot(); if (!root) return;
  const { quest, levelUp } = item;
  const layer = document.createElement('div');
  layer.className = 'gq-celebrate'; layer.setAttribute('role', 'status'); layer.setAttribute('aria-live', 'polite');
  const confetti = guideReduced() ? '' : Array.from({ length: 48 }, (_, i) => {
    const left = Math.random() * 100, delay = Math.random() * 0.35, dur = 1.6 + Math.random() * 1.1, drift = (Math.random() - 0.5) * 220;
    return `<i style="left:${left.toFixed(1)}%;background:${GUIDE_CONFETTI[i % GUIDE_CONFETTI.length]};animation-delay:${delay.toFixed(2)}s;animation-duration:${dur.toFixed(2)}s;--drift:${drift.toFixed(0)}px;${i % 3 ? '' : 'border-radius:99px;'}"></i>`;
  }).join('');
  layer.innerHTML = `<div class="gq-confetti" aria-hidden="true">${confetti}</div>
    <div class="gq-banner"><div class="gq-banner-badge" aria-hidden="true">✓</div><div><div class="gq-banner-caps">QUEST COMPLETE</div>
    <div class="gq-banner-title">${esc(quest.title)}</div></div><div class="gq-xp-pop">+${quest.xp} XP</div></div>
    ${levelUp ? `<div class="gq-levelup"><div class="gq-levelup-caps">LEVEL UP</div><div class="gq-levelup-n">${levelUp.number}</div><div class="gq-levelup-title">${esc(levelUp.title)}</div></div>` : ''}`;
  root.appendChild(layer);
  setTimeout(() => { layer.remove(); guideNextCelebration(); }, levelUp ? 3600 : 2600);
}

/* ───────────── rendering ───────────── */
function guideRing(pct, size = 34) {
  const r = (size - 6) / 2, c = 2 * Math.PI * r;
  return `<svg class="gq-ring" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" class="gq-ring-bg"/><circle cx="${size / 2}" cy="${size / 2}" r="${r}" class="gq-ring-fg" stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${(c * (1 - pct)).toFixed(2)}"/></svg>`;
}
function guideHeader(g) {
  const lv = g.level, span = lv.next ? lv.next - lv.floor : 1;
  const pct = lv.next ? Math.min(1, (g.xp - lv.floor) / span) : 1;
  const shownPct = lv.next ? Math.max(0, Math.min(1, (GUIDE.shownXp - lv.floor) / span)) : 1;
  return `<header class="gq-head"><div class="gq-shield" aria-hidden="true"><span>${lv.number}</span></div>
    <div class="grow"><div class="gq-caps">LEVEL ${lv.number} · ${esc(lv.title.toUpperCase())}</div>
      <h2 class="gq-title" id="gq-title" tabindex="-1">${g.complete ? 'Setup complete' : 'Setup quest'}</h2>
      <div class="gq-xpbar" role="progressbar" aria-label="Experience" aria-valuemin="${lv.floor}" aria-valuemax="${lv.next ?? g.xp}" aria-valuenow="${g.xp}"><i data-target="${(pct * 100).toFixed(1)}" style="width:${(shownPct * 100).toFixed(1)}%"></i></div>
      <div class="gq-xpmeta"><span class="mono">${g.xp} XP</span><span>${lv.next ? `${lv.next - g.xp} XP to ${esc(lv.next_title)}` : 'Max level'}</span></div></div>
    <div class="gq-count"><div class="mono gq-count-n">${g.done}<span>/${g.total}</span></div><div class="gq-caps">QUESTS</div></div>
    <button class="gq-x" data-act="guide-close" aria-label="Close setup guide">×</button></header>`;
}
function guideTrail(g) {
  let chapter = '', html = '';
  g.quests.forEach((q, i) => {
    if (q.chapter !== chapter) { chapter = q.chapter; html += `<div class="gq-chapter">${esc(chapter)}</div>`; }
    const state = q.done ? 'done' : q.id === g.next ? 'next' : 'todo';
    html += `<button class="gq-node ${state} ${q.id === GUIDE.sel ? 'sel' : ''} ${q.optional ? 'bonus' : ''}" data-act="guide-select" data-id="${attr(q.id)}" style="--i:${i}" aria-pressed="${q.id === GUIDE.sel}">
      <span class="gq-dot" aria-hidden="true">${q.done ? '✓' : q.optional ? '★' : i + 1}</span>
      <span class="grow"><span class="gq-node-title">${esc(q.title)}</span><span class="gq-node-sub">${esc(q.done ? q.detail : q.summary)}</span></span>
      <span class="gq-node-xp mono">${q.done ? '' : '+'}${q.xp}</span>
      <span class="sr-only">${q.done ? 'completed' : state === 'next' ? 'next quest' : 'not done'}</span></button>`;
  });
  return `<nav class="gq-trail" aria-label="Quests">${html}</nav>`;
}
function guideDetail(g) {
  const q = guideQuest(GUIDE.sel) || guideQuest(g.next) || g.quests[0];
  return `<section class="gq-detail" aria-labelledby="gq-detail-title">
    <div class="row between base gap8"><span class="gq-caps">${esc(q.chapter.toUpperCase())}${q.optional ? ' · OPTIONAL' : ''}</span><span class="gq-reward ${q.done ? 'won' : ''}">${q.done ? 'Earned' : 'Reward'} · ${q.xp} XP</span></div>
    <h3 id="gq-detail-title">${esc(q.title)}</h3>
    <p class="gq-why">${esc(q.why)}</p>
    <div class="gq-status ${q.done ? 'done' : ''}"><span class="dot"></span>${esc(q.detail || (q.done ? 'Done' : 'Not started'))}</div>
    <ol class="gq-steps">${q.steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol>
    <div class="row gap8 wrap mt14">${q.done ? btn('Revisit', 'guide-go', { id: q.id }) : btn(`Start quest →`, 'guide-go', { id: q.id }, 'primary')}
      ${btn(label('guide-check', 'Check progress', 'Checking…'), 'guide-check', {}, 'ghost', isBusy('guide-check'))}</div>
    <p class="gq-hint">Quests tick off by themselves when the work is done — the guide checks your workspace, not this button.</p></section>`;
}
function guideWelcome(g) {
  return `<div class="gq-welcome">
    <div class="gq-emblem" aria-hidden="true"><span class="gq-orbit"></span><span class="gq-orbit two"></span><span class="gq-emblem-core">J</span></div>
    <div class="gq-caps">WELCOME TO JOB HUNT OS</div>
    <h2 class="gq-title big" id="gq-title" tabindex="-1">Set up your hunt in ${g.total} quests</h2>
    <p class="gq-why">Each quest is one small task in your workspace. Finish them to earn XP, level up from <b>Rookie</b> to <b>Offer Magnet</b>, and have new jobs ranked and waiting for you every morning.</p>
    <div class="gq-chapters">${[...new Set(g.quests.map(q => q.chapter))].map((c, i) => `<span style="--i:${i}">${esc(c)}</span>`).join('')}</div>
    <div class="row gap10 center wrap mt18">${btn('Start the quest', 'guide-begin', {}, 'primary lg')}${btn('I’ll explore on my own', 'guide-later', {}, 'ghost')}</div>
    <p class="gq-hint">You can reopen this guide any time from <b>Setup quest</b> in the sidebar, or press <kbd>?</kbd>.</p></div>`;
}
function guideComplete(g) {
  return `<div class="gq-welcome">
    <div class="gq-trophy" aria-hidden="true">🏆</div>
    <div class="gq-caps">ALL ${g.total} QUESTS DONE</div>
    <h2 class="gq-title big" id="gq-title" tabindex="-1">Your hunt is ready</h2>
    <div class="gq-reward won">Level ${g.level.number} · ${esc(g.level.title)} · ${g.xp} XP</div>
    <p class="gq-why mt14">Searches run on schedule, new jobs are ranked for you, and every tailored claim is backed by a fact you verified.</p>
    <div class="row gap10 center wrap mt18">${btn('Go to Today', 'guide-finish', {}, 'primary lg')}${btn('See the quest map', 'guide-map', {}, 'ghost')}</div></div>`;
}
function guideTracker(g) {
  if (g.complete || g.dismissed) return '';
  const q = guideQuest(GUIDE.active) || guideQuest(g.next); if (!q) return '';
  return `<div class="gq-tracker" role="complementary" aria-label="Setup quest progress">
    <button class="gq-tracker-main" data-act="guide-open" data-id="${attr(q.id)}">${guideRing(g.done / g.total)}
      <span class="grow"><span class="gq-caps">${GUIDE.active === q.id ? 'IN PROGRESS' : 'NEXT QUEST'} · ${g.done}/${g.total}</span><span class="gq-tracker-title">${esc(q.title)}</span></span>
      <span class="gq-node-xp mono">+${q.xp}</span></button>
    <button class="gq-tracker-x" data-act="guide-hide" aria-label="Hide setup quest tracker">×</button></div>`;
}
function guideRender() {
  const root = guideRoot(), g = GUIDE.data; if (!root) return;
  const layers = [...root.querySelectorAll('.gq-celebrate')];  // keep a running celebration
  if (!g) { root.innerHTML = ''; layers.forEach(l => root.appendChild(l)); return; }
  let modal = '';
  if (GUIDE.open) {
    const view = g.complete && GUIDE.view !== 'map' ? guideComplete(g) : GUIDE.view === 'welcome' ? guideWelcome(g) : null;
    modal = `<div class="gq-overlay" data-act="guide-backdrop"><div class="gq-modal ${view ? 'narrow' : ''}" role="dialog" aria-modal="true" aria-labelledby="gq-title">
      ${view ? `<button class="gq-x abs" data-act="guide-close" aria-label="Close setup guide">×</button>${view}` : `${guideHeader(g)}<div class="gq-body">${guideTrail(g)}${guideDetail(g)}</div>`}</div></div>`;
  }
  root.innerHTML = modal + (GUIDE.open ? '' : guideTracker(g));
  layers.forEach(l => root.appendChild(l));
  // Animate the XP bar from what was last shown to the current value.
  const fill = root.querySelector('.gq-xpbar i');
  if (fill) requestAnimationFrame(() => requestAnimationFrame(() => { fill.style.width = fill.dataset.target + '%'; }));
  if (GUIDE.open) GUIDE.shownXp = g.xp;
}
function guideFocus() { requestAnimationFrame(() => document.getElementById('gq-title')?.focus({ preventScroll: true })); }
// The sidebar entry, rendered by renderNav() in app.js.
function guideNavItem() {
  const g = GUIDE.data;
  const badge = g ? (g.complete ? '✓' : `${g.done}/${g.total}`) : '·';
  return `<a class="nav-btn gq-nav ${g && !g.complete ? 'pending' : ''}" href="#" data-act="guide-open"><span>Setup quest</span><span class="nav-count">${badge}</span></a>`;
}

/* ───────────── actions ───────────── */
function guideOpen(view = 'map', sel = null) {
  if (!GUIDE.data) return;
  const kept = guideQuest(GUIDE.sel)?.done ? null : GUIDE.sel;  // reopen on unfinished work, not a ticked quest
  Object.assign(GUIDE, { open: true, view, sel: sel || kept || GUIDE.data.next });
  guideRender(); guideFocus(); guideRefresh(true);
}
function guideClose() {
  if (GUIDE.view === 'welcome' && GUIDE.data && !GUIDE.data.welcomed) { GUIDE.data.welcomed = true; guideSave({ welcomed: true }); }
  GUIDE.open = false; GUIDE.view = 'map'; guideRender();
  document.querySelector('.gq-nav')?.focus({ preventScroll: true });
}
const GUIDE_TARGETS = {
  settings: section => ['settings', { settingsSection: section }],
  discovery: section => ['discovery', { discMode: section }],
  knowledge: section => ['knowledge', { knowledgeSection: section }],
};
const GUIDE_ACTIONS = {
  'guide-open': el => guideOpen(GUIDE.data?.complete ? 'complete' : 'map', el.dataset.id || null),
  'guide-close': () => guideClose(),
  'guide-backdrop': (el, e) => { if (e.target === el) guideClose(); },
  'guide-begin': () => { Object.assign(GUIDE.data, { welcomed: true, dismissed: false }); guideSave({ welcomed: true, dismissed: false }); GUIDE.view = 'map'; GUIDE.sel = GUIDE.data.next; guideRender(); guideFocus(); },
  'guide-later': () => guideClose(),
  'guide-map': () => { GUIDE.view = 'map'; guideRender(); guideFocus(); },
  'guide-finish': () => { guideClose(); go('today'); },
  'guide-select': el => {
    GUIDE.sel = el.dataset.id; guideRender();
    [...document.querySelectorAll('.gq-node')].find(n => n.dataset.id === el.dataset.id)?.focus({ preventScroll: true });
    // On a phone the details sit below the trail.
    if (window.innerWidth <= 760) document.querySelector('.gq-detail')?.scrollIntoView?.({ block: 'start', behavior: guideReduced() ? 'auto' : 'smooth' });
  },
  'guide-check': () => busy('guide-check', () => guideRefresh(true)),
  'guide-hide': () => { GUIDE.data.dismissed = true; guideSave({ dismissed: true }); guideRender(); render(); toast('Setup quest hidden — reopen it from the sidebar any time.'); },
  'guide-go': el => {
    const q = guideQuest(el.dataset.id); if (!q) return;
    GUIDE.active = q.done ? null : q.id; GUIDE.open = false;
    if (GUIDE.data.dismissed && !q.done) { GUIDE.data.dismissed = false; guideSave({ dismissed: false }); }
    guideRender();
    const [screen, extra] = (GUIDE_TARGETS[q.screen] || (() => [q.screen, {}]))(q.section);
    go(screen, extra);
  },
};

document.addEventListener('keydown', e => {
  if (!GUIDE.booted) return;
  if (e.key === 'Escape' && GUIDE.open) { e.stopImmediatePropagation(); guideClose(); return; }
  const typing = ['INPUT', 'TEXTAREA', 'SELECT'].includes(e.target.tagName) || e.target.isContentEditable;
  if (e.key === '?' && !typing && !e.metaKey && !e.ctrlKey && !e.altKey) { e.preventDefault(); GUIDE.open ? guideClose() : guideOpen(); }
  // Keep focus inside the dialog.
  if (e.key === 'Tab' && GUIDE.open) {
    const items = [...document.querySelectorAll('.gq-modal button:not([disabled]), .gq-modal [tabindex]')];
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
}, true);
window.addEventListener('focus', () => guideRefresh());
