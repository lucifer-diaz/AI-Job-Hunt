/* AI Orchestration workspace: the pipeline as an interactive graph, live packets and one job's path.
   Loaded before app.js; uses the workspace's shared helpers (api, esc, attr, btn, pill, render, go) and Monitoring's
   formatters (monMs, monPct, monTokens, monUSD). Every stage, kind and transition comes from
   /api/orchestration/* — the backend registry (app.services.orchestration_topology) — so adding a worker there needs
   no change here. Nothing on this screen changes configuration: model edits open Settings → AI Models. */
'use strict';

const ORCH_MODES = [['system', 'System'], ['live', 'Live job'], ['history', 'History']];
const ORCH_RANGES = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days']];
// [label, tone, symbol]: status is always text plus a symbol, never colour alone.
const ORCH_STATUS = { HEALTHY: ['Healthy', 'good', '✓'], BUSY: ['Busy', 'brand', '●'], DEGRADED: ['Degraded', 'warn', '▲'],
  ERROR: ['Error', 'bad', '✕'], DISABLED: ['Disabled', 'muted', '⏸'], NO_DATA: ['No data', 'muted', '–'] };
const ORCH_STEP = { COMPLETED: ['Completed', 'good', '✓'], CURRENT: ['Current', 'brand', '●'], WAITING: ['Waiting', 'muted', '○'],
  FAILED: ['Failed', 'bad', '✕'], RETRYING: ['Retrying', 'warn', '↻'], SKIPPED: ['Skipped', 'muted', '–'], BLOCKED: ['Blocked', 'bad', '⛔'] };
const ORCH_ACTIVITY = { RUNNING: 'Running', QUEUED: 'Queued', RETRYING: 'Retrying', AWAITING_HUMAN: 'Waiting for you',
  WAITING_TO_START: 'Not started', MONITORING: 'Watching for replies', BLOCKED: 'Blocked', FAILED: 'Failed' };
const ORCH_TABS = [['overview', 'Overview'], ['performance', 'Performance'], ['routing', 'Routing'], ['history', 'History'], ['activity', 'Recent activity']];
const ORCH_GRID = { w: 204, h: 116, dx: 256, dy: 182, pad: 48 };
const ORCH_POLL_ACTIVE = 4000, ORCH_POLL_IDLE = 30000, ORCH_GRAPH_REFRESH = 60000;
let orchTimer = null, orchSearchTimer = null;

function orchState() {
  if (!S.orch) S.orch = { mode: 'system', range: '7d', all: false, node: null, edge: null, tab: 'overview', jobId: null,
    jobQuery: '', traceOpen: false, view: null, legend: false };
  return S.orch;
}
const orchQuery = (extra = {}) => qs({ range: orchState().range, all_accounts: orchState().all ? 'true' : '', ...extra });

/* ───────────── loading and polling ───────────── */
async function loadOrchestration() {
  const o = orchState();
  try {
    const [graph, live] = await Promise.all([api('/api/orchestration/graph?' + orchQuery()), api('/api/orchestration/live?' + orchQuery())]);
    Object.assign(D, { orchGraph: graph, orchLive: live, orchError: null, orchGraphAt: Date.now() });
  } catch (e) {
    D.orchError = e.message;
  }
  await Promise.all([o.jobId ? orchLoadJob() : null, o.node ? orchLoadNode() : null, o.edge ? orchLoadEdge() : null]);
  scheduleOrchPoll();
}
async function orchLoadJob(withTrace = true) {
  const o = orchState(); if (!o.jobId) return;
  try {
    const [job, trace] = await Promise.all([api(`/api/orchestration/jobs/${o.jobId}`),
      withTrace ? api(`/api/orchestration/jobs/${o.jobId}/trace`) : Promise.resolve(D.orchTrace)]);
    Object.assign(D, { orchJob: job, orchTrace: trace });
  } catch (e) { toast(e.message, true); o.jobId = null; D.orchJob = D.orchTrace = null; }
}
async function orchLoadNode() {
  const o = orchState(); if (!o.node) return;
  try {
    D.orchNode = await api(`/api/orchestration/nodes/${o.node}?` + orchQuery());
    if (o.tab === 'history') D.orchHistory = await api(`/api/orchestration/nodes/${o.node}/history?` + orchQuery());
  } catch (e) { toast(e.message, true); o.node = null; }
}
async function orchLoadEdge() {
  const o = orchState(); if (!o.edge) return;
  try { D.orchEdge = await api(`/api/orchestration/edges/${o.edge.source}/${o.edge.target}?` + orchQuery()); }
  catch (e) { toast(e.message, true); o.edge = null; }
}
// Poll fast while something runs (or the selected job is active), slowly when idle, never while the tab is hidden or
// another screen is open. The graph's historical aggregates are refreshed at most once a minute; the poll itself only
// asks for what changes (queue counts, packets, the selected job).
function orchPollDelay() {
  if (S.screen !== 'orchestration' || document.hidden) return null;
  return D.orchLive?.active || D.orchJob?.packet?.active ? ORCH_POLL_ACTIVE : ORCH_POLL_IDLE;
}
function scheduleOrchPoll() {
  stopOrchPoll();
  const delay = orchPollDelay();
  if (delay != null) orchTimer = setTimeout(orchPoll, delay);
}
function stopOrchPoll() { clearTimeout(orchTimer); orchTimer = null; }
async function orchPoll() {
  if (S.screen !== 'orchestration') return stopOrchPoll();
  const o = orchState();
  try {
    const stale = Date.now() - (D.orchGraphAt || 0) > ORCH_GRAPH_REFRESH;
    const [live, graph] = await Promise.all([api('/api/orchestration/live?' + orchQuery()),
      stale ? api('/api/orchestration/graph?' + orchQuery()) : Promise.resolve(D.orchGraph)]);
    Object.assign(D, { orchLive: live, orchGraph: graph, orchError: null }, stale ? { orchGraphAt: Date.now() } : {});
    // A finished job is not polled again: its path cannot change by itself.
    if (o.jobId && D.orchJob && !D.orchJob.packet.terminal) await orchLoadJob(D.orchJob.packet.active);
  } catch (e) { D.orchError = e.message; }
  if (S.screen === 'orchestration') { render(); scheduleOrchPoll(); }
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) stopOrchPoll();
  else if (S.screen === 'orchestration' && D.orchGraph) orchPoll();
});

/* ───────────── formatting ───────────── */
const orchAgo = iso => {
  if (!iso) return '—';
  const s = Math.max(0, (Date.now() - toDate(iso).getTime()) / 1000);
  return s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : s < 86400 ? `${Math.floor(s / 3600)}h ${Math.floor(s % 3600 / 60)}m` : `${Math.floor(s / 86400)}d`;
};
const orchSecs = s => s == null ? '—' : monMs(s * 1000);
const orchTime = iso => iso ? toDate(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—';
const orchStatusChip = (status, map = ORCH_STATUS) => { const [text, tone, sym] = map[status] || [status, 'muted', '•']; return `<span class="orch-chip ${tone}"><span aria-hidden="true">${sym}</span> ${esc(text)}</span>`; };
const orchTypeLabel = (g, t) => g.topology.node_types[t]?.label || t;
const orchSym = (g, t) => g.topology.node_types[t]?.symbol || '•';
const orchNodeDef = id => D.orchGraph?.topology.nodes.find(n => n.id === id);
// What the model line on a node says: the assignment from the active configuration. What actually answered is
// telemetry, shown separately (observed models), and is never inferred from this.
function orchModelLine(a) {
  if (!a) return ['', ''];
  if (a.executed_by === 'claude_desktop') return ['Claude Desktop (MCP)', 'tailoring in Claude Desktop'];
  if (!a.enabled) return ['Switched off', a.mode || ''];
  const model = a.resolved_model || 'No model resolved';
  const how = a.mode === 'DIRECT' ? `Direct · ${a.provider}` : a.mode === 'AUTO_ROUTER' ? `Auto router${a.router && !a.router.active ? ' (off → tier)' : ''}`
    : a.mode === 'TIER' ? `Tier · ${a.configured || '—'}` : a.mode === 'FIXED' ? 'Fixed' : (a.mode || '');
  return [model, how];
}
// Queue counts from the latest poll win over the graph's (which may be up to a minute old).
function orchLive(id) { return { ...(D.orchGraph?.nodes[id]?.live || {}), ...(D.orchLive?.counts?.[id] || {}) }; }
function orchDisplayStatus(id) {
  const rt = D.orchGraph?.nodes[id]; if (!rt) return 'NO_DATA';
  const live = orchLive(id), moving = live.running || live.queued || live.retrying;
  if (['HEALTHY', 'NO_DATA'].includes(rt.status) && moving) return 'BUSY';
  if (rt.status === 'BUSY' && !moving) return 'HEALTHY';
  return rt.status;
}
function orchLiveText(live) {
  const parts = [];
  if (live.running) parts.push(`${live.running} running`);
  if (live.queued) parts.push(`${live.queued} queued`);
  if (live.retrying) parts.push(`${live.retrying} retrying`);
  if (live.waiting) parts.push(`${live.waiting} waiting`);
  if (live.watching) parts.push(`${live.watching} watching`);
  if (live.failed_recent) parts.push(`${live.failed_recent} failed (24h)`);
  if (live.blocked_recent) parts.push(`${live.blocked_recent} blocked (24h)`);
  return parts;
}
function orchProcessed(g, id) {
  const out = g.topology.edges.filter(e => e.source === id && g.edges[e.id]);
  return out.length ? out.reduce((a, e) => a + (g.edges[e.id].count || 0), 0) : null;
}
function orchMetricLine(g, n, rt) {
  if (rt.llm && rt.llm.requests) return `p95 ${monMs(rt.llm.p95_latency_ms)} · ${monPct(rt.llm.success_rate)} ok · ${rt.llm.requests} calls`;
  if (rt.tasks && rt.tasks.tasks) return `${rt.tasks.completed} done${rt.tasks.failed ? ` · ${rt.tasks.failed} failed` : ''} · median ${orchSecs(rt.tasks.median_run_seconds)}`;
  if (rt.external && rt.external.requests) return `${rt.external.requests} calls · ${monPct(rt.external.success_rate)} ok · p95 ${monMs(rt.external.p95_latency_ms)}`;
  const done = orchProcessed(g, n.id);
  if (done != null) return `${done} passed on in range`;
  return rt.llm ? 'No calls in range' : '';
}

/* ───────────── layout ───────────── */
function orchLayout(g) {
  const nodes = g.topology.nodes, G = ORCH_GRID;
  const cols = nodes.map(n => n.position.col), rows = nodes.map(n => n.position.row);
  const c0 = Math.min(...cols), r0 = Math.min(...rows);
  const pos = Object.fromEntries(nodes.map(n => [n.id, { x: G.pad + (n.position.col - c0) * G.dx, y: G.pad + (n.position.row - r0) * G.dy }]));
  return { pos, width: G.pad * 2 + (Math.max(...cols) - c0) * G.dx + G.w, height: G.pad * 2 + (Math.max(...rows) - r0) * G.dy + G.h };
}
// A path between two node boxes: straight between neighbours, an arc over anything in between, a curve between rows.
// Two edges between the same pair (tailor ⇄ reviewer) are drawn apart.
function orchEdgeGeometry(a, b, offset) {
  const { w, h, dx } = ORCH_GRID;
  if (Math.abs(a.y - b.y) < 1) {
    const right = b.x > a.x, y = a.y + h / 2 + offset;
    const x1 = right ? a.x + w : a.x, x2 = right ? b.x : b.x + w;
    if (Math.abs(b.x - a.x) <= dx + 1) return { d: `M${x1},${y} L${x2},${y}`, lx: (x1 + x2) / 2, ly: y - 8 };
    const lift = a.y - 54;
    return { d: `M${a.x + w / 2},${a.y} C${a.x + w / 2},${lift} ${b.x + w / 2},${lift} ${b.x + w / 2},${b.y}`, lx: (a.x + b.x + w) / 2, ly: lift + 10 };
  }
  const down = b.y > a.y;
  const x1 = a.x + w / 2 + offset, y1 = down ? a.y + h : a.y, x2 = b.x + w / 2 + offset, y2 = down ? b.y : b.y + h;
  const my = (y1 + y2) / 2;
  return { d: `M${x1},${y1} C${x1},${my} ${x2},${my} ${x2},${y2}`, lx: (x1 + x2) / 2, ly: my };
}
function orchPathStatus() {
  const job = orchState().mode === 'live' ? D.orchJob : null;
  return job ? Object.fromEntries(job.path.map(s => [s.node_id, s])) : null;
}

/* ───────────── the graph ───────────── */
function orchCanvas(g) {
  const o = orchState(), L = orchLayout(g), path = orchPathStatus(), { w, h } = ORCH_GRID;
  const pairs = new Set(g.topology.edges.map(e => `${e.source}>${e.target}`));
  const maxCount = Math.max(1, ...Object.values(g.edges).map(e => e.count || 0));
  const edges = g.topology.edges.map(e => {
    const a = L.pos[e.source], b = L.pos[e.target]; if (!a || !b) return '';
    const offset = pairs.has(`${e.target}>${e.source}`) ? (e.source < e.target ? -12 : 12) : 0;
    const geo = orchEdgeGeometry(a, b, offset), stat = g.edges[e.id];
    const from = path?.[e.source], to = path?.[e.target];
    const walked = path && from?.status === 'COMPLETED' && to && !['WAITING', 'SKIPPED'].includes(to.status);
    const selected = o.edge && o.edge.source === e.source && o.edge.target === e.target;
    const width = o.mode === 'history' && stat ? 1.5 + 5 * (stat.count || 0) / maxCount : 1.6;
    const text = o.mode === 'history' ? (stat ? `${stat.count}${stat.median_seconds != null ? ` · ${orchSecs(stat.median_seconds)}` : ''}` : '')
      : o.mode === 'live' ? (walked ? e.label : '') : e.label;
    const cls = ['orch-edge', `kind-${e.kind}`, walked ? 'walked' : '', path && !walked ? 'dim' : '', selected ? 'selected' : ''].join(' ');
    const name = `${orchNodeDef(e.source)?.label} to ${orchNodeDef(e.target)?.label}: ${e.label}${stat ? `, ${stat.count} in range` : ''}`;
    return `<g class="${cls}" data-act="orch-edge" data-source="${attr(e.source)}" data-target="${attr(e.target)}" tabindex="0" role="button" aria-label="${attr(name)}">
      <path class="orch-edge-hit" d="${geo.d}"/><path class="orch-edge-line" d="${geo.d}" style="stroke-width:${width.toFixed(2)}px" marker-end="url(#orch-arrow${walked ? '-on' : ''})"/>
      ${text ? `<text class="orch-edge-label" x="${geo.lx.toFixed(1)}" y="${geo.ly.toFixed(1)}" text-anchor="middle">${esc(text)}</text>` : ''}</g>`;
  }).join('');
  const nodes = g.topology.nodes.map(n => orchNode(g, n, L.pos[n.id], path)).join('');
  const v = o.view || { x: 0, y: 0, k: 0.7 };
  return `<div class="orch-canvas" data-orch-canvas tabindex="0" aria-label="Pipeline graph. Drag to pan, scroll or press + and − to zoom, 0 to fit.">
    <div class="orch-world" data-orch-world style="width:${L.width}px;height:${L.height}px;transform:translate(${v.x}px,${v.y}px) scale(${v.k})">
      <svg class="orch-edges" width="${L.width}" height="${L.height}" viewBox="0 0 ${L.width} ${L.height}" aria-hidden="false">
        <defs><marker id="orch-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="orch-arrow"/></marker>
        <marker id="orch-arrow-on" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" class="orch-arrow on"/></marker></defs>
        ${edges}</svg>
      ${nodes}
    </div>
    <div class="orch-zoom" role="group" aria-label="Zoom">${[['in', '+', 'Zoom in'], ['out', '−', 'Zoom out'], ['fit', 'Fit', 'Fit to screen'], ['reset', 'Reset', 'Reset view']].map(([id, t, label]) => `<button class="btn sm" data-act="orch-zoom" data-id="${id}" aria-label="${label}">${t}</button>`).join('')}</div>
  </div>`;
}
function orchNode(g, n, p, path) {
  const o = orchState(), rt = g.nodes[n.id] || {}, live = orchLive(n.id), status = orchDisplayStatus(n.id);
  const [statusText, tone, statusSym] = ORCH_STATUS[status];
  const step = path?.[n.id];
  const a = (rt.assignments || [])[0];
  // An AI-first stage shows its model; anything else shows what it is, and an optional model second.
  const [model, how] = n.primary_type === 'AI' ? orchModelLine(a)
    : [orchTypeLabel(g, n.primary_type), a ? `AI: ${orchModelLine(a)[0]}` : n.types.slice(1).map(t => orchTypeLabel(g, t)).join(' · ')];
  const external = n.external?.length ? [...new Set(n.external.map(m => m.provider.replace(/_/g, ' ').toLowerCase()))].join(', ') : '';
  const liveText = orchLiveText(live);
  const cls = ['orch-node', `type-${n.primary_type}`, `status-${status}`, o.node === n.id ? 'selected' : '',
    step ? `step-${step.status}` : '', step?.current ? 'is-current' : '', path && !step ? 'dim' : '', path && step && ['WAITING', 'SKIPPED'].includes(step.status) ? 'ahead' : ''].join(' ');
  const label = `${n.label}, ${n.types.map(t => orchTypeLabel(g, t)).join(', ')}, ${statusText}${step ? `, ${ORCH_STEP[step.status]?.[0] || step.status} for the selected job` : ''}`;
  return `<button class="${cls}" style="left:${p.x}px;top:${p.y}px;width:${ORCH_GRID.w}px;height:${ORCH_GRID.h}px" data-act="orch-node" data-id="${attr(n.id)}" aria-label="${attr(label)}">
    <span class="orch-node-head"><span class="orch-sym" title="${attr(n.types.map(t => orchTypeLabel(g, t)).join(' + '))}">${n.types.map(t => orchSym(g, t)).join('')}</span>
      <span class="orch-node-title">${esc(n.label)}</span></span>
    <span class="orch-node-model ellipsis ${n.primary_type === 'AI' ? '' : 'plain'}" title="${attr(model)}">${esc(model)}</span>
    <span class="orch-node-sub ellipsis">${esc([how, external].filter(Boolean).join(' · '))}</span>
    <span class="orch-node-metric ellipsis">${esc(step?.llm?.latest ? `${step.llm.latest.actual_model || 'model unknown'} · ${monMs(step.llm.latest.latency_ms)}` : orchMetricLine(g, n, rt))}</span>
    <span class="orch-node-foot"><span class="orch-node-live ellipsis">${esc(liveText.join(' · '))}</span>
      ${step ? orchStatusChip(step.status, ORCH_STEP) : `<span class="orch-chip ${tone}" title="${attr((rt.status_reasons || []).join(' · ') || statusText)}"><span aria-hidden="true">${statusSym}</span> ${esc(statusText)}</span>`}</span>
    ${rt.recommendations ? `<span class="orch-rec" title="${rt.recommendations} AI Optimization recommendation${rt.recommendations === 1 ? '' : 's'}">${rt.recommendations}</span>` : ''}
    ${rt.llm?.fallbacks ? `<span class="orch-flag" title="Fallback models answered ${rt.llm.fallbacks} request${rt.llm.fallbacks === 1 ? '' : 's'} in this range">↪ fallback</span>` : ''}
  </button>`;
}

/* ───────────── screen ───────────── */
function renderOrchestration() {
  const o = orchState(), g = D.orchGraph;
  if (!g) return D.orchError ? `<div class="card">${empty('AI Orchestration could not load', D.orchError, btn('Try again', 'orch-retry', {}, 'primary'))}</div>` : loading();
  if (!o.view) setTimeout(orchAutoFit, 0);
  return `<div class="orch">${orchToolbar(g)}
    ${D.orchError ? `<div class="banner warn mb12" role="alert">Live updates paused: ${esc(D.orchError)} · ${btn('Retry', 'orch-retry', {}, 'sm')}</div>` : ''}
    <div class="orch-main">
      <section class="card orch-canvas-card">${orchCanvas(g)}${orchLegend(g)}</section>
      <aside class="orch-side">${o.mode === 'live' ? orchJobPanel() : ''}${orchPacketsPanel()}</aside>
    </div>
    ${o.mode === 'live' && D.orchJob ? orchTimeline() : ''}
    ${o.mode === 'history' ? orchHistoryPanel(g) : ''}
  </div>${orchDrawer(g)}${orchTraceDrawer()}`;
}
function orchToolbar(g) {
  const o = orchState(), sys = g.system;
  const gw = g.gateway?.mode === 'litellm' ? `LiteLLM gateway${g.gateway.config_version ? ` · config v${g.gateway.config_version}` : ''}` : `Direct · ${g.gateway?.provider || '—'}`;
  return `<section class="card orch-toolbar"><div class="row between wrap gap12">
    <div class="row gap10 wrap"><div class="seg" role="group" aria-label="View">${ORCH_MODES.map(([id, t]) => `<button class="${o.mode === id ? 'on' : ''}" data-act="orch-mode" data-id="${id}" aria-pressed="${o.mode === id}">${t}</button>`).join('')}</div>
      <span class="small muted">System ${orchStatusChip(sys.status)} · ${esc(gw)}</span></div>
    <div class="row gap10 wrap"><div class="seg" role="group" aria-label="Range">${ORCH_RANGES.map(([id, t]) => `<button class="${o.range === id ? 'on' : ''}" data-act="orch-range" data-id="${id}" aria-pressed="${o.range === id}">${t}</button>`).join('')}</div>
      ${BOOT.user?.is_admin ? `<label class="check"><input type="checkbox" data-act="orch-all" ${o.all ? 'checked' : ''}>All accounts</label>` : ''}
      <button class="btn sm" data-act="orch-legend" aria-expanded="${o.legend}">Legend</button>
      <button class="btn sm" data-act="orch-monitoring" data-tab="overview">Monitoring →</button></div></div>
    ${o.all ? '<div class="xs muted mt8">All accounts: node and edge numbers cover every account. Packets, job titles and traces stay your own.</div>' : ''}</section>`;
}
function orchLegend(g) {
  const o = orchState(); if (!o.legend) return '';
  const types = Object.entries(g.topology.node_types).map(([id, t]) => `<span class="row gap6"><span class="orch-sym">${esc(t.symbol)}</span>${esc(t.label)}</span>`).join('');
  const statuses = Object.keys(ORCH_STATUS).map(s => orchStatusChip(s)).join(' ');
  const steps = Object.keys(ORCH_STEP).map(s => orchStatusChip(s, ORCH_STEP)).join(' ');
  return `<div class="orch-legend"><div class="caps-sm mb6">Node kinds</div><div class="row gap14 wrap small">${types}</div>
    <div class="caps-sm mt10 mb6">Stage status</div><div class="row gap6 wrap">${statuses}</div>
    <div class="caps-sm mt10 mb6">A job at each stage</div><div class="row gap6 wrap">${steps}</div>
    <details class="mt10"><summary class="small b">How status is decided</summary><ol class="small muted orch-rules">${g.health_policy.rules.map(r => `<li><b>${esc(r.status)}</b> — ${esc(r.rule)}</li>`).join('')}</ol></details>
    ${g.topology.reserved?.length ? `<div class="xs muted mt8">Planned, not running yet: ${g.topology.reserved.map(r => esc(r.label)).join(', ')}.</div>` : ''}</div>`;
}
function orchPacketsPanel() {
  const live = D.orchLive, items = live?.packets || [];
  const waiting = live ? Object.entries(live.counts).filter(([, c]) => c.waiting).map(([id, c]) => `${c.waiting} at ${orchNodeDef(id)?.label || id}`) : [];
  return `<section class="card orch-packets"><div class="row between base mb10"><div class="card-title">Live packets</div><span class="xs muted">${live ? `${live.total_running} running · ${live.total_queued} queued` : ''}</span></div>
    ${!live ? loading() : items.length ? `<div class="orch-packet-list" data-scroll-key="orch-packets">${items.map(p => `<button class="orch-packet ${orchState().jobId === p.job_id ? 'on' : ''}" data-act="orch-packet" data-id="${p.job_id}">
      <span class="grow" style="min-width:0"><span class="small b ellipsis" style="display:block">${esc(p.title || `Job #${p.job_id}`)}</span><span class="xs muted ellipsis" style="display:block">${esc(p.company || '')}${p.model ? ` · ${esc(p.model)}` : ''}</span></span>
      <span class="col gap4" style="align-items:flex-end"><span class="xs b">${esc(p.node_label)}</span><span class="xs ${p.activity === 'RUNNING' ? 'orch-running' : 'muted'}">${esc(ORCH_ACTIVITY[p.activity] || p.activity)} · ${orchAgo(p.since)}</span></span></button>`).join('')}</div>`
      : `<div class="small muted">Nothing is running right now.${waiting.length ? ` Waiting: ${esc(waiting.join(', '))}.` : ''}</div>`}</section>`;
}
function orchJobPanel() {
  const o = orchState(), job = D.orchJob;
  const results = D.orchJobResults || [];
  const search = `<label class="field" for="orch-job-search">Live job</label><input class="input" id="orch-job-search" data-orch-search placeholder="Search by company, role or job ID" value="${attr(o.jobQuery)}" autocomplete="off">
    ${o.jobQuery && results.length ? `<div class="orch-results">${results.map(r => `<button class="orch-result" data-act="orch-pick-job" data-id="${r.job_id}"><span class="ellipsis">${esc(r.title)} · ${esc(r.company)}</span><span class="xs muted">#${r.job_id}</span></button>`).join('')}</div>` : o.jobQuery && D.orchJobResults ? '<div class="xs muted mt6">No matching job.</div>' : ''}`;
  if (!job) return `<section class="card">${search}<div class="small muted mt10">Pick a job, or a live packet below, to follow it through the graph.</div></section>`;
  const p = job.packet, model = p.model ? `${esc(p.model)} <span class="xs muted">(${p.model_source === 'telemetry' ? 'recorded' : 'assigned, not yet recorded'})</span>` : '<span class="muted">—</span>';
  const row = (k, v) => `<div class="orch-kv"><span class="xs muted">${k}</span><span class="small">${v}</span></div>`;
  return `<section class="card orch-job">${search}
    <div class="orch-packet-card mt12"><div class="small b">${esc(p.title)}</div><div class="xs muted">${esc(p.company)} · Job #${p.job_id}</div>
      <div class="orch-kvs mt10">${row('Current', p.terminal ? `Finished${p.terminal_node ? ` at ${esc(orchNodeDef(p.terminal_node)?.label || p.terminal_node)}` : ''}` : `<b>${esc(p.node_label || '—')}</b> · ${esc(ORCH_ACTIVITY[p.activity] || p.activity || '—')}`)}
        ${row('Started', p.since ? esc(orchTime(p.since)) : '—')}${row('Elapsed', p.since && !p.terminal ? orchAgo(p.since) : '—')}
        ${row('Model', model)}${row('Attempt', p.attempt != null ? `${p.attempt}${p.max_attempts ? ` / ${p.max_attempts}` : ''}` : '—')}</div>
      ${p.reason ? `<div class="xs muted mt8">${esc(p.reason)}</div>` : ''}
      <div class="row gap6 wrap mt10">${btn('View trace', 'orch-trace', {}, 'sm primary')}${btn('Open job', 'orch-open-job', { id: p.job_id }, 'sm')}${btn('Clear', 'orch-clear-job', {}, 'sm ghost')}</div></div>
    <ol class="orch-path mt12">${job.path.map(s => `<li class="orch-step step-${s.status} ${s.current ? 'current' : ''}"><button class="orch-step-btn" data-act="orch-node" data-id="${attr(s.node_id)}">
      <span class="orch-step-sym" aria-hidden="true">${ORCH_STEP[s.status]?.[2] || '•'}</span><span class="grow" style="min-width:0"><span class="small ${s.current ? 'b' : ''}">${esc(s.label)}</span>
      <span class="xs muted ellipsis" style="display:block">${esc([ORCH_STEP[s.status]?.[0], s.current ? ORCH_ACTIVITY[s.activity] : null, s.llm?.latest ? `${s.llm.latest.actual_model || 'model unknown'} · ${monMs(s.llm.latest.latency_ms)}` : null, s.detail].filter(Boolean).join(' — '))}</span></span></button></li>`).join('')}</ol></section>`;
}
function orchTimeline() {
  const t = D.orchTrace; if (!t) return '';
  const events = t.events.filter(e => e.source !== 'log').slice(-60);
  return `<section class="card mt16"><div class="row between base mb10"><div><div class="card-title">Execution timeline</div><div class="card-sub">${esc(t.title)} · ${esc(t.company)} · workflow, tasks and model calls in order</div></div>${btn('Full trace', 'orch-trace', {}, 'sm')}</div>
    <ol class="orch-timeline">${events.map(e => `<li><span class="mono xs muted">${esc(orchTime(e.at))}</span><span class="orch-src src-${e.source}">${esc(e.source)}</span>
      <span class="small">${esc(e.title)}${e.llm ? ` <span class="xs muted">· ${esc(e.llm.actual_model || 'model unknown')} · ${monMs(e.llm.latency_ms)} · ${monTokens(e.llm.input_tokens)} → ${monTokens(e.llm.output_tokens)} tokens</span>` : ''}${e.status && e.source !== 'workflow' ? ` <span class="xs ${e.status === 'FAILED' ? 'orch-bad' : 'muted'}">${esc(e.status.toLowerCase())}</span>` : ''}</span></li>`).join('')}</ol>
    ${t.truncated ? '<div class="xs muted mt8">Showing the most recent events only.</div>' : ''}</section>`;
}
function orchHistoryPanel(g) {
  const nodes = g.topology.nodes.map(n => ({ n, rt: g.nodes[n.id] || {} }));
  const rows = nodes.map(({ n, rt }) => {
    const llm = rt.llm, tasks = rt.tasks, processed = orchProcessed(g, n.id);
    return `<tr><td><button class="textbtn" data-act="orch-node" data-id="${attr(n.id)}">${esc(n.label)}</button></td><td>${orchStatusChip(rt.status)}</td>
      <td class="tr">${processed ?? '—'}</td><td class="tr">${llm ? llm.requests : tasks ? tasks.tasks : '—'}</td>
      <td class="tr">${llm?.requests ? monPct(llm.success_rate) : tasks?.finished ? monPct(1 - (tasks.failure_rate || 0)) : '—'}</td>
      <td class="tr">${llm?.requests ? monMs(llm.p50_latency_ms) : tasks?.median_run_seconds != null ? orchSecs(tasks.median_run_seconds) : '—'}</td></tr>`;
  }).join('');
  return `<section class="card mt16"><div class="card-title">How work moved in this range</div><div class="card-sub mb10">Edges on the graph show transitions counted from stored facts; nodes below show processed items, calls or tasks. Only what is recorded is shown.</div>
    <div class="mon-table-wrap"><table class="mon-table"><thead><tr><th>Stage</th><th>Status</th><th class="tr">Passed on</th><th class="tr">Calls / tasks</th><th class="tr">Success</th><th class="tr">Median latency</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
}

/* ───────────── drawers ───────────── */
function orchDrawer(g) {
  const o = orchState();
  if (o.edge) return orchEdgeDrawer();
  if (!o.node) return '';
  const n = orchNodeDef(o.node); if (!n) return '';
  const d = D.orchNode, rt = g.nodes[n.id] || {};
  const a = (rt.assignments || [])[0], [model, how] = a ? orchModelLine(a) : [orchTypeLabel(g, n.primary_type), ''];
  const body = !d ? loading() : ({ overview: orchNodeOverview, performance: orchNodePerformance, routing: orchNodeRouting, history: orchNodeHistory, activity: orchNodeActivity }[o.tab] || orchNodeOverview)(g, n, d);
  const tabs = ORCH_TABS.filter(([id]) => n.types.includes('AI') || !['routing', 'history'].includes(id));
  return `<aside class="orch-drawer" role="dialog" aria-label="${attr(n.label)} details"><div class="orch-drawer-head">
      <div class="row between gap10"><div style="min-width:0"><div class="caps-sm">${esc(n.types.map(t => orchTypeLabel(g, t)).join(' · '))}</div>
        <h2 class="orch-drawer-title">${esc(n.label)}</h2><div class="small muted ellipsis">${esc([model, how].filter(Boolean).join(' · '))} · ${orchStatusChip(orchDisplayStatus(n.id))}</div></div>
        <button class="btn sm" data-act="orch-close" aria-label="Close details">✕</button></div>
      <div class="orch-tabs" role="tablist">${tabs.map(([id, t]) => `<button role="tab" aria-selected="${o.tab === id}" class="${o.tab === id ? 'on' : ''}" data-act="orch-tab" data-id="${id}">${t}${id === 'overview' && rt.recommendations ? ` <span class="orch-rec inline">${rt.recommendations}</span>` : ''}</button>`).join('')}</div></div>
    <div class="orch-drawer-body">${body}</div></aside>`;
}
const orchKV = (k, v) => `<div class="orch-kv"><span class="xs muted">${esc(k)}</span><span class="small">${v}</span></div>`;
const orchTile = (k, v, sub = '') => `<div class="orch-tile"><div class="xs muted">${esc(k)}</div><div class="orch-tile-v">${v}</div>${sub ? `<div class="xs faint">${esc(sub)}</div>` : ''}</div>`;
function orchAssignment(a) {
  const esc2 = a.escalation ? `${a.escalation.model || a.escalation.label || '—'} on ${a.escalation.conditions.join(', ').toLowerCase().replace(/_/g, ' ')}` : '—';
  return `<div class="box soft mb10"><div class="small b mb6">${esc(a.label)}${a.has_prompt ? '' : ' <span class="xs muted">(no prompt yet)</span>'}</div><div class="orch-kvs">
    ${orchKV('Mode', esc(a.mode === 'DIRECT' ? `Direct provider (${a.provider})` : a.mode || '—'))}
    ${orchKV('Configured', esc(a.configured || '—'))}${orchKV('Requests go to', esc(a.resolved_model || 'nothing resolvable'))}
    ${orchKV('Fallback', esc(a.fallback_model || '—'))}${orchKV('Quality escalation', esc(esc2))}
    ${orchKV('Config version', a.config_version ? `v${a.config_version}` : '—')}${orchKV('Enabled', a.enabled ? 'Yes' : 'No')}</div>
    ${a.executed_by === 'claude_desktop' ? '<div class="xs muted mt6">Tailoring and review run in Claude Desktop over MCP: this assignment is not used.</div>' : ''}
    ${a.note ? `<div class="xs muted mt6">${esc(a.note)}</div>` : ''}
    ${a.router ? `<div class="xs muted mt6">Auto router ${a.router.active ? `<b>${esc(a.router.model || '')}</b> · classifier ${esc(a.router.classifier || '—')}` : 'is off: the worker uses its tier'}</div>` : ''}</div>`;
}
function orchNodeOverview(g, n, d) {
  const live = orchLiveText(orchLive(n.id));
  const recs = d.recommendations || [];
  return `<p class="small ink2 mt0">${esc(n.description)}</p>
    ${d.status_reasons?.length ? `<div class="banner ${['ERROR', 'DEGRADED'].includes(d.status) ? 'warn' : ''} mb12"><span>${orchStatusChip(d.status)} ${esc(d.status_reasons.join(' · '))}</span></div>` : ''}
    ${live.length ? `<div class="small mb12"><b>Now:</b> ${esc(live.join(' · '))}</div>` : ''}
    ${(d.assignments || []).length ? `<div class="caps-sm mb8">Model assignment (active configuration)</div>${d.assignments.map(orchAssignment).join('')}
      <div class="small mb12"><b>Actually answered (telemetry):</b> ${d.observed_models?.length ? d.observed_models.map(m => `<span class="mono">${esc(m)}</span>`).join(', ') : '<span class="muted">nothing recorded in this range</span>'}</div>` : ''}
    ${recs.length ? `<div class="caps-sm mb8">AI Optimization recommendations</div>${recs.map(r => `<div class="box soft mb8"><div class="small b">${esc(r.title || r.finding || 'Recommendation')}</div><div class="xs muted">${esc([r.priority, r.category].filter(Boolean).join(' · '))}</div>${r.recommended_change ? `<div class="small mt4">${esc(r.recommended_change)}</div>` : ''}</div>`).join('')}<div class="xs muted mb12">Recommendations only: nothing is changed automatically.</div>` : ''}
    <div class="row gap8 wrap">${n.monitoring_tab ? btn('View detailed monitoring', 'orch-monitoring', { tab: n.monitoring_tab, stage: n.llm_stages?.[0] || '' }, 'sm') : ''}
      ${n.screen ? btn(`Open ${n.screen}`, 'orch-screen', { screen: n.screen }, 'sm') : ''}
      ${n.types.includes('AI') ? btn('Edit model assignment', 'orch-settings-ai', {}, 'sm soft') : ''}</div>
    ${n.types.includes('AI') ? '<div class="xs muted mt8">Model changes go through Settings → AI Models: draft, preview, apply, version.</div>' : ''}`;
}
function orchNodePerformance(g, n, d) {
  const l = d.llm, t = d.tasks, x = d.external;
  const llm = l ? (l.requests ? `<div class="caps-sm mb8">Model calls · ${l.requests} request${l.requests === 1 ? '' : 's'}${l.low_sample ? ' · small sample' : ''}</div><div class="orch-tiles">
      ${orchTile('Success', monPct(l.success_rate), `${l.failed} failed`)}${orchTile('p50 / p95', `${monMs(l.p50_latency_ms)} / ${monMs(l.p95_latency_ms)}`, `p99 ${monMs(l.p99_latency_ms)}`)}
      ${orchTile('Avg tokens', `${monTokens(l.avg_input_tokens)} → ${monTokens(l.avg_output_tokens)}`, l.requests_without_usage ? `${l.requests_without_usage} without usage` : '')}
      ${orchTile('Context used', l.avg_context_utilization_percent == null ? '—' : `${l.avg_context_utilization_percent.toFixed(0)}%`)}
      ${orchTile('Cost', l.estimated_cost == null ? 'Unknown' : monUSD(l.estimated_cost), l.unpriced_requests ? `${l.unpriced_requests} unpriced` : l.free ? 'free' : '')}
      ${orchTile('Retry rate', monPct(l.retry_rate))}${orchTile('Timeouts', monPct(l.timeout_rate))}
      ${orchTile('Schema pass', monPct(l.schema_pass_rate), l.schema_checked ? `${l.schema_failures} of ${l.schema_checked} failed` : 'not checked')}
      ${orchTile('Fallback', monPct(l.fallback_rate), `${l.fallbacks} answered by fallback`)}${orchTile('Escalation', monPct(l.escalation_rate), `${l.escalations} escalated`)}</div>`
    : '<div class="small muted mb12">No model calls in this range.</div>') : '';
  const tasks = t ? `<div class="caps-sm mt14 mb8">Tasks · ${t.tasks}${t.truncated ? '+' : ''}</div><div class="orch-tiles">${orchTile('Completed', t.completed)}${orchTile('Failed', t.failed, monPct(t.failure_rate))}
      ${t.blocked != null ? orchTile('Blocked', t.blocked) : ''}${orchTile('Retried', t.retried, monPct(t.retry_rate))}${orchTile('Median run', orchSecs(t.median_run_seconds), `p95 ${orchSecs(t.p95_run_seconds)}`)}</div>` : '';
  const ext = x ? `<div class="caps-sm mt14 mb8">External calls · ${x.requests}</div><div class="orch-tiles">${orchTile('Success', monPct(x.success_rate))}${orchTile('Rate limited', x.rate_limited)}
      ${orchTile('Timeouts', x.timeouts)}${orchTile('p95', monMs(x.p95_latency_ms))}${orchTile('Cost', x.estimated_cost == null ? 'Unknown' : monUSD(x.estimated_cost))}</div>` : '';
  const q = d.quality, quality = q ? `<div class="caps-sm mt14 mb8">Outcomes stored for this stage${q.low_sample ? ' · small sample' : ''}</div><div class="orch-kvs">${Object.entries(q).filter(([k]) => k !== 'low_sample').map(([k, v]) => orchKV(k.replace(/_/g, ' '), esc(typeof v === 'object' ? Object.entries(v).map(([a, b]) => `${a} ${b}`).join(', ') || '—' : String(k.endsWith('rate') ? monPct(v) : v ?? '—')))).join('')}</div>` : '';
  const methods = d.fetch_methods ? `<div class="caps-sm mt14 mb8">How descriptions were fetched</div><div class="small">${esc(Object.entries(d.fetch_methods).map(([k, v]) => `${k.toLowerCase()} ${v}`).join(' · '))}${d.playwright_used != null ? ` · browser used ${d.playwright_used}×` : ''}</div>` : '';
  return llm + tasks + ext + quality + methods || '<div class="small muted">Nothing is recorded for this stage in this range.</div>';
}
function orchNodeRouting(g, n, d) {
  const r = d.routing; if (!r) return '<div class="small muted">Not an AI stage.</div>';
  const list = obj => Object.keys(obj || {}).length ? Object.entries(obj).map(([k, v]) => `<div class="row between small"><span class="mono ellipsis">${esc(k)}</span><b>${v}</b></div>`).join('') : '<div class="small muted">—</div>';
  const auto = (r.assignments || []).find(a => a.mode === 'AUTO_ROUTER' && a.router);
  const router = auto ? `<div class="caps-sm mt14 mb8">Auto router</div><div class="orch-router">
      <div class="orch-router-root">${esc(n.label)} → ${esc(auto.router.active ? auto.router.model || 'router' : 'router off')}</div>
      ${Object.entries(auto.router.tiers || {}).map(([tier, model]) => { const seen = r.tiers.find(t => t.tier === tier); return `<div class="orch-router-tier"><span class="b small">${esc(tier)}</span><span class="mono small ellipsis">${esc(model || '—')}</span><span class="xs muted">${seen ? `${monPct(seen.share, 0)} · ${seen.requests}` : ''}</span></div>`; }).join('')}
      ${r.tiers_recorded ? '' : '<div class="xs muted mt6">No historical routing metadata: the router\'s tier choice was not recorded for these requests.</div>'}</div>` : '';
  return `<div class="orch-kvs mb12">${orchKV('Primary answered', r.primary_requests)}${orchKV('Fallback answered', r.fallback_requests)}${orchKV('Fallback unknown', r.fallback_unknown)}${orchKV('Escalations', r.escalations)}</div>
    ${router}
    <div class="orch-cols mt14"><div><div class="caps-sm mb6">Configured</div>${list(r.configured)}</div><div><div class="caps-sm mb6">Requested</div>${list(r.requested)}</div>
      <div><div class="caps-sm mb6">Actually answered</div>${list(r.actual)}${r.actual_unknown ? `<div class="xs muted">${r.actual_unknown} with no model recorded (unknown)</div>` : ''}</div></div>
    ${Object.keys(r.escalation_reasons || {}).length ? `<div class="caps-sm mt14 mb6">Escalation reasons</div>${list(r.escalation_reasons)}` : ''}
    ${Object.keys(r.routing_causes || {}).length ? `<div class="caps-sm mt14 mb6">Router reasons</div>${list(r.routing_causes)}` : ''}
    <div class="xs muted mt10">Modes recorded: ${esc(Object.entries(r.modes).map(([k, v]) => `${k} ${v}`).join(' · ') || '—')}${r.config_versions.length ? ` · config versions seen ${r.config_versions.map(v => `v${v}`).join(', ')}` : ''}</div>`;
}
function orchNodeHistory(g, n, d) {
  const h = D.orchHistory; if (!h || h.node_id !== n.id) return loading();
  const fmt = iso => iso ? fmtDay(iso) : 'now';
  return `${h.mode_note ? `<div class="banner mb12">${esc(h.mode_note)}</div>` : ''}${h.workers.map(w => `<div class="mb16"><div class="small b mb8">${esc(w.label)}</div>
    ${w.periods.length ? `<ol class="orch-periods">${w.periods.map(p => `<li class="${p.current ? 'current' : ''}"><div class="row between gap10"><span class="small b mono ellipsis">${esc(p.model || 'nothing resolvable')}</span><span class="xs muted none">${esc(fmt(p.started_at))} → ${esc(p.current ? 'current' : fmt(p.ended_at))}</span></div>
      <div class="xs muted">${esc([p.mode, p.assignment && p.assignment !== p.model ? p.assignment : null, `v${p.from_version}${p.to_version ? `–v${p.to_version}` : '+'}`, p.changed_by].filter(Boolean).join(' · '))}</div>
      <div class="xs mt4"><b>${p.observed.requests}</b> request${p.observed.requests === 1 ? '' : 's'}${p.observed.requests ? ` · ${monPct(p.observed.success_rate)} success · p95 ${monMs(p.observed.p95_latency_ms)} · ${monTokens(p.observed.avg_input_tokens)} avg in` : ''}${p.observed.low_sample && p.observed.requests ? ' · small sample' : ''}</div></li>`).join('')}</ol>` : '<div class="small muted">No applied configuration version yet.</div>'}
    ${w.by_actual_model.length ? `<div class="caps-sm mt12 mb6">By the model that answered</div><div class="mon-table-wrap"><table class="mon-table"><thead><tr><th>Model</th><th class="tr">Requests</th><th class="tr">Success</th><th class="tr">p95</th><th class="tr">Avg tokens</th><th>Seen</th></tr></thead><tbody>
      ${w.by_actual_model.map(m => `<tr><td class="mono">${esc(m.model)}</td><td class="tr b">${m.requests}${m.low_sample ? '*' : ''}</td><td class="tr">${monPct(m.success_rate)}</td><td class="tr">${monMs(m.p95_latency_ms)}</td><td class="tr">${monTokens(m.avg_input_tokens)}</td><td class="xs muted">${esc(fmtDay(m.first_seen))} → ${esc(fmtDay(m.last_seen))}</td></tr>`).join('')}</tbody></table></div>` : ''}
    ${w.changes.length ? `<div class="caps-sm mt12 mb6">Configuration changes</div>${w.changes.map(c => `<div class="small">v${c.version} · ${esc(fmtDateTime(c.at))} · ${esc(c.from.model || '—')} → <b>${esc(c.to.model || '—')}</b>${c.from.mode !== c.to.mode ? ` (${esc(c.from.mode || '—')} → ${esc(c.to.mode || '—')})` : ''} <span class="xs muted">${esc(c.changed_by || c.source)}</span></div>`).join('')}` : ''}</div>`).join('')}
    <div class="xs muted">${esc(h.note)} * fewer than 20 requests.</div><div class="mt10">${btn('View configuration versions', 'orch-settings-ai', {}, 'sm')}</div>`;
}
function orchNodeActivity(g, n, d) {
  const r = d.recent || {};
  const call = c => `<div class="orch-call"><span class="xs mono muted">${esc(fmtDateTime(c.at))}</span><span class="small ellipsis">${esc(c.actual_model || 'model unknown')}${c.requested_model && c.requested_model !== c.actual_model ? ` <span class="xs muted">(asked ${esc(c.requested_model)})</span>` : ''}</span>
    <span class="xs muted">${esc([monMs(c.latency_ms), c.error_type, c.fallback_used ? 'fallback' : null, c.escalated ? `escalated: ${c.escalation_reason || ''}` : null, c.retry_count ? `retry ${c.retry_count}` : null].filter(Boolean).join(' · '))}</span>
    ${c.job_id ? btn(`Job #${c.job_id}`, 'orch-pick-job', { id: c.job_id }, 'sm ghost') : ''}</div>`;
  const block = (title, items) => items?.length ? `<div class="caps-sm mt12 mb6">${title}</div>${items.map(call).join('')}` : '';
  const tasks = r.failed_tasks?.length ? `<div class="caps-sm mt12 mb6">Failed tasks</div>${r.failed_tasks.map(t => `<div class="orch-call"><span class="xs mono muted">${esc(fmtDateTime(t.at))}</span><span class="small">${esc(t.status.toLowerCase())}${t.error_category ? ` · ${esc(t.error_category.toLowerCase())}` : ''} · ${t.attempts} attempt${t.attempts === 1 ? '' : 's'}</span><span class="xs muted ellipsis">${esc(t.error || '')}</span>${t.job_id ? btn(`Job #${t.job_id}`, 'orch-pick-job', { id: t.job_id }, 'sm ghost') : ''}</div>`).join('')}` : '';
  const html = block('Latest failures', r.failures) + block('Retries, fallbacks and escalations', r.retries) + block('Latest successes', r.successes) + tasks;
  return html || '<div class="small muted">No recent activity in this range.</div>';
}
function orchEdgeDrawer() {
  const o = orchState(), e = D.orchEdge, from = orchNodeDef(o.edge.source), to = orchNodeDef(o.edge.target);
  const body = !e ? loading() : `<p class="small ink2 mt0">${esc(e.measure || 'A supporting relationship.')}</p>
    ${e.count == null ? `<div class="small muted">${esc(e.note || '')}</div>` : `<div class="orch-tiles">${orchTile('In range', e.count, e.truncated ? 'capped' : '')}${orchTile('Jobs', e.jobs)}
      ${orchTile('Median time', orchSecs(e.median_seconds), e.timed_items ? `${e.timed_items} timed` : '')}${orchTile('Last', e.last_at ? fmtDateTime(e.last_at) : '—')}</div>
      ${e.timing_note ? `<div class="xs muted mt6">${esc(e.timing_note)}.</div>` : ''}
      ${e.split ? `<div class="caps-sm mt14 mb6">Split out of ${esc(from?.label || '')}</div>${e.split.map(s => `<div class="orch-split"><span class="small">${esc(s.label)}</span><span class="orch-split-bar"><i style="width:${(100 * (s.share || 0)).toFixed(1)}%"></i></span><span class="small b">${monPct(s.share, 0)}</span><span class="xs muted">${s.count}</span></div>`).join('')}` : ''}
      ${e.target_tasks ? `<div class="caps-sm mt14 mb6">At ${esc(to?.label || '')}</div><div class="small">${Object.entries(e.target_tasks).map(([k, v]) => `${esc(k)} ${v}`).join(' · ')}</div>` : ''}
      ${e.recent?.length ? `<div class="caps-sm mt14 mb6">Recent</div>${e.recent.map(r => `<button class="orch-result" data-act="orch-pick-job" data-id="${r.job_id}"><span class="ellipsis small">${esc(r.title || `Job #${r.job_id}`)} · ${esc(r.company || '')}</span><span class="xs muted">${esc(fmtDateTime(r.at))}</span></button>`).join('')}` : ''}`}`;
  return `<aside class="orch-drawer" role="dialog" aria-label="Transition details"><div class="orch-drawer-head"><div class="row between gap10"><div><div class="caps-sm">Transition</div>
    <h2 class="orch-drawer-title">${esc(from?.label || o.edge.source)} → ${esc(to?.label || o.edge.target)}</h2><div class="small muted">${esc(e?.label || '')}</div></div>
    <button class="btn sm" data-act="orch-close" aria-label="Close details">✕</button></div></div><div class="orch-drawer-body">${body}</div></aside>`;
}
function orchTraceDrawer() {
  const o = orchState(); if (!o.traceOpen) return '';
  const t = D.orchTrace;
  const body = !t ? loading() : `<div class="xs muted mb10">${esc(t.note)} Sources: ${esc(Object.entries(t.sources).map(([k, v]) => `${k} ${v}`).join(' · '))}</div>
    ${t.trace_ids.length ? `<div class="row gap6 wrap mb12">${t.trace_ids.slice(0, 8).map(id => btn(`Trace ${id.slice(0, 8)} in Logs`, 'orch-mon-trace', { id }, 'sm')).join('')}</div>` : ''}
    <ol class="orch-trace">${t.events.map(e => `<li class="src-${e.source}"><div class="row between gap8"><span class="small b">${esc(e.title)}</span><span class="mono xs muted none">${esc(fmtDateTime(e.at))} ${esc(orchTime(e.at))}</span></div>
      <div class="xs muted">${esc([e.source, e.node_id ? orchNodeDef(e.node_id)?.label : null, e.status, e.actor, e.detail, e.error, e.level].filter(Boolean).join(' · '))}</div>
      ${e.llm ? `<div class="orch-kvs mt4">${orchKV('Requested', esc(e.llm.requested_model || '—'))}${orchKV('Actual', esc(e.llm.actual_model || 'unknown'))}${orchKV('Mode', esc([e.llm.model_mode, e.llm.configured_model].filter(Boolean).join(' / ') || '—'))}${orchKV('Tier', esc(e.llm.routing_tier || '—'))}
        ${orchKV('Latency', monMs(e.llm.latency_ms))}${orchKV('Tokens', `${monTokens(e.llm.input_tokens)} → ${monTokens(e.llm.output_tokens)}`)}${orchKV('Schema', e.llm.schema_valid == null ? '—' : e.llm.schema_valid ? 'pass' : `fail${e.llm.validation_failure_reason ? ` (${esc(e.llm.validation_failure_reason)})` : ''}`)}
        ${orchKV('Fallback / escalation', `${e.llm.fallback_used == null ? 'unknown' : e.llm.fallback_used ? 'fallback answered' : 'primary'}${e.llm.escalated ? ` · escalated (${esc(e.llm.escalation_reason || '')})` : ''}`)}</div>` : ''}
      ${e.external ? `<div class="xs muted mt4">${esc([e.external.host, e.external.http_status_code, monMs(e.external.latency_ms), e.external.error_type, e.external.retry_count ? `${e.external.retry_count} retries` : null].filter(Boolean).join(' · '))}</div>` : ''}</li>`).join('')}</ol>`;
  return `<div class="overlay" data-act="orch-trace-close"><div class="modal white" role="dialog" aria-label="Job trace"><div class="modal-head row between"><div><div class="caps-sm">Trace</div><div class="b">${esc(t?.title || '')} · ${esc(t?.company || '')}</div></div>${btn('Close', 'orch-trace-close')}</div><div style="padding:18px 22px">${body}</div></div></div>`;
}

/* ───────────── pan and zoom ───────────── */
function orchApplyView() {
  const v = orchState().view, world = document.querySelector('[data-orch-world]');
  if (v && world) world.style.transform = `translate(${v.x}px,${v.y}px) scale(${v.k})`;
}
// Fit the whole graph. `readable` (the first view) never shrinks text below a legible scale: a wide pipeline then
// starts at Discovery and is panned, rather than shown whole and unreadable.
const ORCH_READABLE = 0.68;
function orchFit(readable = false) {
  const g = D.orchGraph, canvas = document.querySelector('[data-orch-canvas]'); if (!g || !canvas) return;
  const L = orchLayout(g), box = canvas.getBoundingClientRect();
  const cw = box.width || 900, ch = box.height || 560;
  const fit = Math.max(0.3, Math.min(1.1, (cw - 24) / L.width, (ch - 24) / L.height));
  const k = readable ? Math.max(fit, Math.min(ORCH_READABLE, (ch - 24) / L.height)) : fit;
  const x = L.width * k > cw ? 12 - (ORCH_GRID.pad - 16) * k : (cw - L.width * k) / 2;
  orchState().view = { k, x, y: Math.max(8, (ch - L.height * k) / 2) };
  orchApplyView();
}
// Bring one node into the middle of the canvas, keeping the zoom (used when a job is selected).
function orchFocusNode(id) {
  const g = D.orchGraph, canvas = document.querySelector('[data-orch-canvas]'); if (!g || !canvas || !id) return;
  const p = orchLayout(g).pos[id]; if (!p) return;
  if (!orchState().view) orchFit(true);
  const v = orchState().view, box = canvas.getBoundingClientRect();
  const cw = box.width || 900, ch = box.height || 560;
  orchState().view = { k: v.k, x: cw / 2 - (p.x + ORCH_GRID.w / 2) * v.k, y: ch / 2 - (p.y + ORCH_GRID.h / 2) * v.k };
  orchApplyView();
}
function orchAutoFit() { if (!orchState().view && S.screen === 'orchestration') orchFit(true); }
function orchZoomAt(factor, px, py) {
  const o = orchState(); if (!o.view) orchFit();
  const v = o.view, k = Math.max(0.3, Math.min(2.2, v.k * factor));
  o.view = { k, x: px - (px - v.x) * k / v.k, y: py - (py - v.y) * k / v.k };
  orchApplyView();
}
(() => {
  let drag = null;
  document.addEventListener('pointerdown', e => {
    const canvas = e.target.closest?.('[data-orch-canvas]');
    if (!canvas || e.button !== 0 || e.target.closest('[data-act]')) return;
    if (!orchState().view) orchFit();
    drag = { x: e.clientX, y: e.clientY, view: { ...orchState().view } };
    canvas.classList.add('panning');
  });
  document.addEventListener('pointermove', e => {
    if (!drag) return;
    orchState().view = { ...drag.view, x: drag.view.x + e.clientX - drag.x, y: drag.view.y + e.clientY - drag.y };
    orchApplyView();
  });
  document.addEventListener('pointerup', () => { drag = null; document.querySelector('[data-orch-canvas]')?.classList.remove('panning'); });
  document.addEventListener('wheel', e => {
    const canvas = e.target.closest?.('[data-orch-canvas]'); if (!canvas) return;
    e.preventDefault();
    const box = canvas.getBoundingClientRect();
    orchZoomAt(Math.exp(-e.deltaY * 0.0015), e.clientX - box.left, e.clientY - box.top);
  }, { passive: false });
  document.addEventListener('keydown', e => {
    if (S.screen !== 'orchestration') return;
    const el = e.target;
    if (e.key === 'Escape' && (orchState().node || orchState().edge || orchState().traceOpen)) { ORCHESTRATION_ACTIONS['orch-close'](); return; }
    // SVG edges are not buttons: Enter and Space activate them like one.
    if ((e.key === 'Enter' || e.key === ' ') && el.dataset?.act?.startsWith('orch-') && el.tagName !== 'BUTTON') { e.preventDefault(); A[el.dataset.act](el, e); return; }
    if (!el.closest?.('[data-orch-canvas]') || el.dataset?.act) return;
    const v = orchState().view || { x: 0, y: 0, k: 0.7 }, step = 60;
    const moves = { ArrowLeft: [step, 0], ArrowRight: [-step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    if (moves[e.key]) { e.preventDefault(); orchState().view = { ...v, x: v.x + moves[e.key][0], y: v.y + moves[e.key][1] }; orchApplyView(); }
    else if (e.key === '+' || e.key === '=') ORCHESTRATION_ACTIONS['orch-zoom']({ dataset: { id: 'in' } });
    else if (e.key === '-') ORCHESTRATION_ACTIONS['orch-zoom']({ dataset: { id: 'out' } });
    else if (e.key === '0') orchFit();
  });
  document.addEventListener('input', e => {
    if (e.target.dataset?.orchSearch === undefined) return;
    const o = orchState(); o.jobQuery = e.target.value;
    clearTimeout(orchSearchTimer);
    orchSearchTimer = setTimeout(async () => {
      D.orchJobResults = o.jobQuery.trim() ? (await api('/api/orchestration/jobs?' + qs({ q: o.jobQuery.trim(), limit: 8 })).catch(() => ({ items: [] }))).items : null;
      if (S.screen === 'orchestration') render();
    }, 200);
  });
})();

/* ───────────── actions ───────────── */
async function orchSelectJob(id) {
  const o = orchState();
  Object.assign(o, { jobId: Number(id), mode: 'live', jobQuery: '', node: null, edge: null });
  D.orchJobResults = null; D.orchJob = D.orchTrace = null;
  render();
  await orchLoadJob();
  render(); scheduleOrchPoll();
  const p = D.orchJob?.packet;
  orchFocusNode(p?.node_id || p?.terminal_node);
}
const ORCHESTRATION_ACTIONS = {
  'orch-mode': el => { orchState().mode = el.dataset.id; render(); },
  'orch-range': el => { orchState().range = el.dataset.id; D.orchHistory = null; load(); },
  'orch-all': el => { orchState().all = el.checked; load(); },
  'orch-retry': () => load(),
  'orch-legend': () => { orchState().legend = !orchState().legend; render(); },
  'orch-node': async el => {
    const o = orchState();
    Object.assign(o, { node: el.dataset.id, edge: null, tab: 'overview' });
    D.orchNode = null; D.orchHistory = null; render();
    await orchLoadNode(); render();
  },
  'orch-edge': async el => {
    const o = orchState();
    Object.assign(o, { edge: { source: el.dataset.source, target: el.dataset.target }, node: null });
    D.orchEdge = null; render();
    await orchLoadEdge(); render();
  },
  'orch-tab': async el => {
    const o = orchState(); o.tab = el.dataset.id; render();
    if (o.tab === 'history' && (!D.orchHistory || D.orchHistory.node_id !== o.node)) {
      try { D.orchHistory = await api(`/api/orchestration/nodes/${o.node}/history?` + orchQuery()); } catch (e) { toast(e.message, true); }
      render();
    }
  },
  'orch-close': () => { Object.assign(orchState(), { node: null, edge: null, traceOpen: false }); render(); },
  'orch-zoom': el => {
    const id = el.dataset.id, canvas = document.querySelector('[data-orch-canvas]');
    const box = canvas?.getBoundingClientRect() || { width: 900, height: 560 };
    if (id === 'fit') { orchFit(); return; }
    if (id === 'reset') { orchState().view = null; orchFit(true); return; }
    orchZoomAt(id === 'in' ? 1.2 : 1 / 1.2, (box.width || 900) / 2, (box.height || 560) / 2);
  },
  'orch-packet': el => orchSelectJob(el.dataset.id),
  'orch-pick-job': el => orchSelectJob(el.dataset.id),
  // From Monitoring (an LLM request or a log line about a job) straight to that job's path.
  'orch-view-job': el => { const o = orchState(); Object.assign(o, { jobId: Number(el.dataset.id), mode: 'live', node: null, edge: null }); D.orchJob = D.orchTrace = null; if (S.mon) Object.assign(S.mon, { request: null }); go('orchestration'); },
  'orch-clear-job': () => { orchState().jobId = null; D.orchJob = D.orchTrace = null; render(); scheduleOrchPoll(); },
  'orch-trace': async () => {
    const o = orchState(); o.traceOpen = true; render();
    if (!D.orchTrace && o.jobId) { await orchLoadJob(); render(); }
  },
  'orch-trace-close': (el, e) => { if (e && el.classList?.contains('overlay') && e.target !== el) return; orchState().traceOpen = false; render(); },
  'orch-open-job': el => openJob(el.dataset.id),
  'orch-screen': el => go(el.dataset.screen),
  'orch-settings-ai': () => go('settings', { settingsSection: 'ai' }),
  'orch-monitoring': el => {
    const m = monState();
    Object.assign(m, { tab: el.dataset.tab || 'overview', stage: el.dataset.stage || '', page: 1, all: orchState().all });
    go('monitoring');
  },
  'orch-mon-trace': el => {
    const m = monState();
    Object.assign(m.logs, { category: '', level: '', provider: '', workflow: '', search: '', trace_id: el.dataset.id, page: 1, entry: null });
    m.tab = 'logs'; orchState().traceOpen = false; go('monitoring');
  },
};
