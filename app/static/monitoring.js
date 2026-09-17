/* Monitoring & AI Optimization tab. Loaded before app.js; functions use the workspace's shared helpers.
   Every number comes from /api/monitoring/*. Charts are single-series in the brand hue, except input vs output
   tokens (brand + orange, validated for colour-vision deficiency); every chart has hover details and a table view. */
'use strict';

const MON_STAGES = { RANKER: 'Ranker', TAILOR: 'Tailor', REVIEWER: 'Reviewer', RESUME_PARSER: 'Resume parser', APPLICATION: 'Application', FOLLOW_UP: 'Follow-up', INBOX: 'Inbox', COMPANY_INSIGHTS: 'Company insights', ADVISOR: 'Advisor', OTHER: 'Other' };
const MON_ERRORS = { TIMEOUT: 'Timeouts', RATE_LIMIT: 'Rate limits', INVALID_JSON: 'Invalid JSON', SCHEMA_VALIDATION: 'Schema validation', PROVIDER_ERROR: 'Provider errors', CONTEXT_OVERFLOW: 'Context overflow', CONFIGURATION: 'Configuration', OTHER: 'Other' };
const MON_RANGES = [['today', 'Today'], ['7d', '7 days'], ['30d', '30 days']];
const MON_SERIES = { input: '#5b4ae8', output: '#eb6834' };
const MON_FILTER_KEYS = ['provider', 'model', 'stage', 'prompt_version', 'status', 'job_id'];
let monTimer = null;

function monState() {
  if (!S.mon) S.mon = { range: '7d', provider: '', model: '', stage: '', prompt_version: '', status: '', job_id: '', all: false, page: 1, tables: new Set(), request: null };
  return S.mon;
}
function monQuery(extra = {}) {
  const m = monState();
  return qs({ range: m.range, ...Object.fromEntries(MON_FILTER_KEYS.map(k => [k, m[k]])), all_accounts: m.all ? 'true' : '', ...extra });
}
async function loadMonitoring() {
  const m = monState();
  const [dash, reqs, rec] = await Promise.all([
    api('/api/monitoring/dashboard?' + monQuery()),
    api('/api/monitoring/requests?' + monQuery({ page: m.page, per_page: 20 })),
    api('/api/monitoring/recommendations'),
  ]);
  const wasActive = D.monRec?.active;
  Object.assign(D, { mon: dash, monRequests: reqs, monRec: rec });
  if (wasActive && !rec.active) {
    const task = rec.tasks[0];
    toast(task?.status === 'FAILED' ? `AI usage analysis failed: ${task.error || 'unknown error'}` : 'AI usage analysis finished: recommendations updated', task?.status === 'FAILED');
  }
  scheduleMonPoll();
}
function scheduleMonPoll() {
  clearTimeout(monTimer); monTimer = null;
  if (S.screen !== 'monitoring' || !D.monRec?.active) return;
  monTimer = setTimeout(async () => {
    if (S.screen !== 'monitoring') return;
    try { await loadMonitoring(); } catch { scheduleMonPoll(); return; }
    if (S.screen === 'monitoring') render();
  }, 4000);
}

/* ───────────── formatting ───────────── */
const monUSD = v => v == null ? '—' : v === 0 ? '$0' : Math.abs(v) < 0.01 ? `$${v.toFixed(4)}` : `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
const monTokens = n => n == null ? '—' : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e4 ? `${Math.round(n / 1e3)}K` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}K` : String(Math.round(n));
const monMs = ms => ms == null ? '—' : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
const monPct = (r, digits = 1) => r == null ? '—' : `${(r * 100).toFixed(digits)}%`;
const monStage = s => MON_STAGES[s] || s;

/* ───────────── chart pieces ───────────── */
// Horizontal bars: one series, value at the bar tip, the whole row is the hover target.
function monBars(rows, valueOf, textOf, tipOf, { labelWidth = 130, valueWidth = 72 } = {}) {
  const max = Math.max(0, ...rows.map(valueOf));
  if (!rows.length || max <= 0) return '<div class="small muted" style="padding:18px 0">No data in this range.</div>';
  return `<div class="mon-bars">${rows.map(r => {
    const v = valueOf(r), w = max ? Math.max(v > 0 ? 1.5 : 0, 100 * v / max) : 0;
    return `<div class="mon-bar-row" data-tip="${attr(tipOf(r))}"><span class="mon-bar-label ellipsis" style="width:${labelWidth}px">${esc(r._label)}</span>
      <span class="mon-bar-track"><span class="mon-bar-fill"><i style="width:${w}%;background:${MON_SERIES.input}"></i></span><b style="width:${valueWidth}px">${esc(textOf(r))}</b></span></div>`;
  }).join('')}</div>`;
}
// Columns over days: stacked series with a 2px surface gap, rounded top, square baseline, one axis.
function monColumns(days, series, fmt, tipOf) {
  if (!days.length) return '<div class="small muted">No data.</div>';
  const totals = days.map(d => series.reduce((a, s) => a + (d[s.key] || 0), 0));
  const max = Math.max(0, ...totals);
  const labelEvery = Math.max(1, Math.ceil(days.length / 7));
  const cols = days.map((d, i) => {
    const segs = series.map(s => {
      const v = d[s.key] || 0; if (!v || !max) return '';
      return `<i style="height:${Math.max(1.5, 100 * v / max)}%;background:${s.color}"></i>`;
    }).join('');
    return `<div class="mon-col" data-tip="${attr(tipOf(d))}"><div class="mon-col-stack">${segs}</div><span class="mon-col-x">${(i % labelEvery === 0 && days.length - 1 - i >= labelEvery) || i === days.length - 1 ? esc(fmtDay(d.day)) : ''}</span></div>`;
  }).join('');
  const legend = series.length > 1 ? `<div class="row gap14 xs muted mb8">${series.map(s => `<span class="row gap6"><span style="width:10px;height:10px;border-radius:3px;background:${s.color}"></span>${esc(s.name)}</span>`).join('')}</div>` : '';
  return `${legend}<div class="mon-cols-wrap"><span class="mon-axis-max xs faint">${max ? esc(fmt(max)) : ''}</span><div class="mon-cols">${cols}</div></div>`;
}
function monTable(head, rows) {
  return `<div class="mon-table-wrap"><table class="mon-table"><thead><tr>${head.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
}
function monCard(key, title, sub, chart, table, extra = '') {
  const showTable = monState().tables.has(key);
  return `<section class="card mon-card"><div class="row between gap10 mb12" style="align-items:flex-start"><div><div class="card-title">${esc(title)}</div>${sub ? `<div class="card-sub">${sub}</div>` : ''}</div>
    ${btn(showTable ? 'Chart' : 'Table', 'mon-table', { key }, 'sm')}</div>${showTable ? table : chart}${extra}</section>`;
}
function monTile(label, value, sub = '', tone = '') {
  return `<div class="box mon-tile"><div class="xs muted">${esc(label)}</div><div class="mono bb mon-tile-value" style="${tone ? `color:var(--${tone})` : ''}">${esc(value)}</div>${sub ? `<div class="tiny faint">${esc(sub)}</div>` : ''}</div>`;
}

/* ───────────── page ───────────── */
function renderMonitoring() {
  const m = monState(), d = D.mon;
  if (!d) return loading();
  const s = d.summary, o = d.options;
  const selects = [['provider', 'All providers', o.providers.map(v => [v, v])], ['model', 'All models', o.models.map(v => [v, v])],
    ['stage', 'All stages', o.stages.map(v => [v, monStage(v)])], ['prompt_version', 'All prompt versions', o.prompt_versions.map(v => [v, v])],
    ['status', 'All statuses', [['SUCCEEDED', 'Succeeded'], ['FAILED', 'Failed only']]]]
    .map(([k, all, opts]) => `<select class="select mon-select" data-act-change="mon-filter" data-key="${k}" aria-label="${attr(all)}">${[['', all], ...opts].map(([v, t]) => `<option value="${attr(v)}" ${String(m[k]) === v ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select>`).join('');
  const filtered = MON_FILTER_KEYS.some(k => m[k]) || m.all;
  const filters = `<section class="card pad16"><div class="row wrap gap8" style="align-items:center">
    <div class="seg">${MON_RANGES.map(([id, t]) => `<button class="${m.range === id ? 'on' : ''}" data-act="mon-range" data-id="${id}">${t}</button>`).join('')}</div>
    ${selects}<input class="input mon-select" style="width:96px" inputmode="numeric" placeholder="Job id" value="${attr(m.job_id)}" data-act-change="mon-filter" data-key="job_id" aria-label="Job id">
    ${BOOT.user?.is_admin ? `<label class="row gap6 small"><input type="checkbox" data-act-change="mon-all" ${m.all ? 'checked' : ''}> All accounts</label>` : ''}
    ${filtered ? btn('Reset', 'mon-reset', {}, 'sm') : ''}<span class="grow"></span>${btn(label('monRefresh', 'Refresh', 'Refreshing…'), 'mon-refresh', {}, 'sm', isBusy('monRefresh'))}</div>
    ${d.truncated ? '<div class="xs muted mt8">Showing the most recent 50,000 requests in this range.</div>' : ''}</section>`;

  const rangeLabel = MON_RANGES.find(([id]) => id === m.range)?.[1].toLowerCase() || 'this range';
  const tiles = `<section class="mon-tiles">
    ${monTile(m.range === 'today' ? 'LLM requests today' : `LLM requests · ${rangeLabel}`, s.requests.toLocaleString(), `${s.succeeded.toLocaleString()} succeeded`)}
    ${monTile('Total tokens', monTokens(s.total_tokens), [s.estimated_token_requests && `${s.estimated_token_requests} estimated locally`, s.requests_without_usage && `${s.requests_without_usage} without usage`].filter(Boolean).join(' · ') || 'provider-reported')}
    ${monTile('Input tokens', monTokens(s.input_tokens))}
    ${monTile('Output tokens', monTokens(s.output_tokens))}
    ${monTile('Estimated cost', monUSD(s.estimated_cost), s.unpriced_requests ? `+ ${s.unpriced_requests} unpriced request${s.unpriced_requests === 1 ? '' : 's'}` : 'all requests priced')}
    ${monTile('Average latency', monMs(s.avg_latency_ms))}
    ${monTile('P95 latency', monMs(s.p95_latency_ms))}
    ${monTile('Failed requests', s.failed.toLocaleString(), s.requests ? `${monPct(s.failure_rate)} of requests` : '', s.failed ? 'bad' : '')}
    ${monTile('Retry rate', monPct(s.retry_rate), `${s.retried_requests} retried`)}
    ${monTile('Average context', s.avg_context_tokens == null ? '—' : `${monTokens(s.avg_context_tokens)} tokens`, s.avg_context_utilization_percent == null ? 'context window unknown' : `${s.avg_context_utilization_percent.toFixed(1)}% of window`)}
  </section>`;

  const days = d.timeseries;
  const costChart = monCard('cost', 'Cost over time', 'Estimated USD per day. Unpriced requests are counted separately, never guessed.',
    monColumns(days, [{ key: 'estimated_cost', color: MON_SERIES.input, name: 'Estimated cost' }], monUSD,
      x => `${fmtDay(x.day)} · ${monUSD(x.estimated_cost)} · ${x.requests} request${x.requests === 1 ? '' : 's'}${x.unpriced_requests ? ` (${x.unpriced_requests} unpriced)` : ''}`),
    monTable(['Day', 'Requests', 'Estimated cost', 'Unpriced'], days.map(x => [x.day, x.requests, monUSD(x.estimated_cost), x.unpriced_requests])));
  const tokenChart = monCard('tokens', 'Token usage over time', 'Input vs output tokens per day',
    monColumns(days, [{ key: 'input_tokens', color: MON_SERIES.input, name: 'Input' }, { key: 'output_tokens', color: MON_SERIES.output, name: 'Output' }], monTokens,
      x => `${fmtDay(x.day)} · input ${monTokens(x.input_tokens)} · output ${monTokens(x.output_tokens)}`),
    monTable(['Day', 'Input tokens', 'Output tokens'], days.map(x => [x.day, x.input_tokens.toLocaleString(), x.output_tokens.toLocaleString()])));

  const stages = d.stages.map(x => ({ ...x, _label: monStage(x.stage) }));
  const stageChart = monCard('stages', 'Usage by stage', 'Share of requests; hover for tokens and cost share',
    monBars(stages, x => x.requests, x => x.requests ? `${x.requests.toLocaleString()} · ${monPct(x.request_share, 0)}` : '0',
      x => `${monStage(x.stage)} · ${x.requests} requests · ${monTokens(x.input_tokens + x.output_tokens)} tokens · ${monUSD(x.estimated_cost)}${x.cost_share != null ? ` (${monPct(x.cost_share, 0)} of cost)` : ''} · avg context ${monTokens(x.avg_context_tokens)}`),
    monTable(['Stage', 'Requests', 'Share', 'Tokens', 'Estimated cost', 'Cost share', 'Failures', 'Avg context'], stages.map(x => [monStage(x.stage), x.requests, monPct(x.request_share), (x.input_tokens + x.output_tokens).toLocaleString(), monUSD(x.estimated_cost), monPct(x.cost_share), x.failed, monTokens(x.avg_context_tokens)])));

  const models = d.models.slice(0, 12).map(x => ({ ...x, _label: x.model }));
  const modelChart = monCard('models', 'Cost by model', 'Free models cost $0; unknown prices stay unknown',
    monBars(models, x => x.estimated_cost || 0, x => x.free ? 'free' : `${monUSD(x.estimated_cost)}${x.unpriced_requests ? ' + unpriced' : ''}`,
      x => `${x.provider} · ${x.model} · ${x.requests} requests · ${monUSD(x.estimated_cost)}${x.unpriced_requests ? ` · ${x.unpriced_requests} unpriced` : ''} · schema failures ${monPct(x.structured_output_failure_rate)}`, { labelWidth: 150, valueWidth: 118 })
      + (models.length && models.every(x => !x.estimated_cost) ? `<div class="xs muted mt8">${models.map(x => `${esc(x.model)}: ${x.free ? 'free' : x.unpriced_requests ? 'price unknown' : '$0'}`).join(' · ')}</div>` : ''),
    monTable(['Provider', 'Model', 'Requests', 'Estimated cost', 'Unpriced', 'Failure rate', 'Schema failures', 'Avg latency'], d.models.map(x => [x.provider, x.model, x.requests, monUSD(x.estimated_cost), x.unpriced_requests, monPct(x.failure_rate), monPct(x.structured_output_failure_rate), monMs(x.avg_latency_ms)])));

  const lat = d.latency.overall;
  const latRows = [['Average', lat.avg_latency_ms], ['p50', lat.p50_latency_ms], ['p95', lat.p95_latency_ms], ['p99', lat.p99_latency_ms]].map(([n, v]) => ({ _label: n, v }));
  const latencyChart = monCard('latency', 'Latency', 'Time from request to response',
    monBars(latRows, x => x.v || 0, x => monMs(x.v), x => `${x._label}: ${monMs(x.v)}`, { labelWidth: 70, valueWidth: 52 }),
    monTable(['Measure', 'Latency'], latRows.map(x => [x._label, monMs(x.v)]).concat(d.latency.by_stage.map(x => [`${monStage(x.stage)} p95`, monMs(x.p95_latency_ms)]))));

  const c = d.context;
  const buckets = c.buckets.map(x => ({ ...x, _label: x.label }));
  const contextChart = monCard('context', 'Context utilisation', `Average ${c.avg_utilization_percent == null ? '—' : c.avg_utilization_percent.toFixed(1) + '%'} of the context window · ${c.high_context_requests} high-context (≥50%) · ${c.near_limit_requests} near the limit (≥90%)`,
    monBars(buckets, x => x.requests, x => x.requests.toLocaleString(), x => `${x.label} of context window: ${x.requests} requests`, { labelWidth: 70, valueWidth: 44 })
      + (c.unknown_window ? `<div class="xs muted mt8">${c.unknown_window} request${c.unknown_window === 1 ? '' : 's'} used a model whose context window is not in the model catalog.</div>` : '')
      + (Object.keys(c.contributors).length ? `<div class="xs muted mt8">Context by source: ${Object.entries(c.contributors).map(([k, v]) => `${esc(k.replace(/_/g, ' '))} ${monPct(v, 0)}`).join(' · ')}</div>` : ''),
    monTable(['Utilisation', 'Requests'], buckets.map(x => [x.label, x.requests]).concat([['Window unknown', c.unknown_window]])),
    c.largest.length ? `<div class="mt10"><div class="caps-sm mb8">Largest contexts</div>${c.largest.slice(0, 5).map(x => `<div class="row between gap10 small click" data-act="mon-request" data-id="${x.id}" style="padding:4px 0;cursor:pointer" title="${attr(`${monStage(x.stage)} · ${x.model || ''}`)}"><span class="ellipsis" style="min-width:0">${esc(monStage(x.stage))} · ${esc(x.model || '')}</span><span class="mono none" style="white-space:nowrap">${monTokens(x.context_tokens)}/${monTokens(x.context_window_size)} · ${x.utilization_percent.toFixed(0)}%</span></div>`).join('')}</div>` : '');

  const errs = d.errors.by_type.map(x => ({ ...x, _label: MON_ERRORS[x.error_type] || x.error_type }));
  const errorChart = monCard('errors', 'Failures', `${d.errors.failed} failed · ${monPct(d.errors.failure_rate)} of requests`,
    monBars(errs, x => x.requests, x => x.requests.toLocaleString(), x => `${x._label}: ${x.requests}`, { valueWidth: 44 }),
    monTable(['Failure type', 'Requests'], errs.map(x => [x._label, x.requests])));

  return `<div class="stack">${filters}${tiles}
    <div class="mon-grid">${costChart}${tokenChart}</div>
    <div class="mon-grid">${stageChart}${modelChart}</div>
    <div class="mon-grid mon-grid-3">${latencyChart}${contextChart}${errorChart}</div>
    ${renderAdvisor()}${renderMonRequests()}</div>`;
}

function renderMonRequests() {
  const r = D.monRequests; if (!r) return '';
  const pages = Math.max(1, Math.ceil(r.total / r.per_page));
  const rows = r.items.map(x => `<tr class="click" data-act="mon-request" data-id="${x.id}">
    <td>${esc(fmtDateTime(x.created_at))}</td><td>${esc(monStage(x.stage))}</td><td class="ellipsis" style="max-width:200px" title="${attr(x.model || '')}">${esc(x.model || '—')}</td>
    <td>${x.status === 'FAILED' ? pill(MON_ERRORS[x.error_type] || 'failed', 'bad') : pill('ok', 'good')}</td>
    <td class="mono">${monTokens(x.input_tokens)} / ${monTokens(x.output_tokens)}${x.usage_source === 'estimated' ? ' <span class="faint" title="Estimated locally">~</span>' : ''}</td>
    <td class="mono">${x.context_utilization_percent == null ? '—' : x.context_utilization_percent.toFixed(1) + '%'}</td>
    <td class="mono">${monMs(x.latency_ms)}</td><td class="mono">${monUSD(x.estimated_cost)}</td><td class="mono">${x.retry_count}</td></tr>`).join('');
  return `<section class="card flush"><div class="row between wrap gap10" style="padding:14px 18px;border-bottom:1px solid var(--line2)">
      <div><div class="card-title">LLM requests</div><div class="card-sub">${r.total.toLocaleString()} matching · click a request for details (no prompt or resume text is shown)</div></div>
      <div class="row gap8 xs muted">${btn('‹ Prev', 'mon-page', { dir: -1 }, 'sm', r.page <= 1)}<span>Page ${r.page} of ${pages}</span>${btn('Next ›', 'mon-page', { dir: 1 }, 'sm', r.page >= pages)}</div></div>
    ${r.items.length ? `<div class="mon-table-wrap"><table class="mon-table"><thead><tr><th>Time</th><th>Stage</th><th>Model</th><th>Status</th><th>Tokens in / out</th><th>Context</th><th>Latency</th><th>Cost</th><th>Retry</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div style="padding:18px">${empty('No LLM requests', 'Requests appear here as ranking, tailoring, reviews and follow-ups call a model.')}</div>`}</section>`;
}

const PRIORITY_TONE = { HIGH: 'bad', MEDIUM: 'warn', LOW: 'muted' };
function renderAdvisor() {
  const rec = D.monRec; if (!rec) return '';
  const r = rec.report, active = rec.tasks.find(t => ['PENDING', 'PROCESSING'].includes(t.status));
  const lastFailed = !active && rec.tasks[0]?.status === 'FAILED' ? rec.tasks[0] : null;
  const schedule = `<label class="row gap6 small muted">Scheduled analysis<select class="select mon-select" data-act-change="mon-schedule" aria-label="Scheduled analysis">${[['off', 'Off'], ['daily', 'Daily'], ['weekly', 'Weekly']].map(([v, t]) => `<option value="${v}" ${rec.schedule === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label>`;
  const head = `<div class="row between wrap gap10 mb12"><div><div class="card-title">AI optimization</div><div class="card-sub">${esc(rec.notice)}</div></div>
    <div class="row gap10 wrap">${schedule}${btn(active ? (active.status === 'PENDING' ? 'Analysis queued…' : 'Analyzing…') : label('monAnalyze', 'Analyze AI usage', 'Queueing…'), 'mon-analyze', {}, 'primary', !!active || isBusy('monAnalyze'))}</div></div>`;
  const status = lastFailed ? `<div class="banner warn mb12"><span class="grow">The last analysis failed: ${esc(lastFailed.error || 'unknown error')}</span></div>` : '';
  if (!r) return `<section class="card">${head}${status}${empty('No recommendations yet', `Analyze the ${MON_RANGES.find(([id]) => id === monState().range)?.[1].toLowerCase()} of usage to get cost, latency, context and reliability recommendations.`)}</section>`;
  const recs = r.recommendations || [];
  const groups = ['HIGH', 'MEDIUM', 'LOW'].map(p => [p, recs.filter(x => x.priority === p)]).filter(([, items]) => items.length);
  const field = (name, value) => value ? `<div><div class="caps-sm">${name}</div><div class="small ink2" style="line-height:1.5">${esc(value)}</div></div>` : '';
  const body = groups.map(([p, items]) => `<div class="mt12"><div class="row gap8 mb8">${pill(p, PRIORITY_TONE[p])}<span class="xs muted">${items.length} recommendation${items.length === 1 ? '' : 's'}</span></div>
    ${items.map(x => `<details class="mon-rec"><summary><span class="b small">${esc(x.title)}</span><span class="xs muted">${esc(x.category.toLowerCase())} · ${esc(monStage(x.affected_stage) === 'ALL' ? 'all stages' : monStage(x.affected_stage))}${x.estimated_cost_saving && x.estimated_cost_saving !== 'unknown' ? ` · saving ${esc(x.estimated_cost_saving)}` : ''}</span></summary>
      <div class="mon-rec-body">${field('Finding', x.finding)}${field('Evidence', x.evidence)}${field('Recommended change', x.recommended_change)}${field('Expected benefit', x.expected_benefit)}${field('Estimated cost saving', x.estimated_cost_saving)}${field('Risk', x.risk)}
        <div class="xs faint">Recommendation only — nothing has been changed. Apply it yourself if you agree.</div></div></details>`).join('')}</div>`).join('');
  const source = r.generated_by === 'llm' ? `AI write-up by ${esc(r.model_name || r.model_provider || 'model')} · prompt ${esc(r.prompt_version || '')}` : 'Rule-based findings';
  return `<section class="card">${head}${status}
    <div class="box soft"><div class="small" style="line-height:1.55">${esc(r.summary)}</div>
      <div class="xs muted mt6">${source} · ${esc(fmtDay(r.time_range_start))} – ${esc(fmtDay(r.time_range_end))} · created ${esc(fmtDateTime(r.created_at))}${r.estimated_savings && r.estimated_savings !== 'unknown' ? ` · estimated savings ${esc(r.estimated_savings)}` : ''}</div></div>
    ${body || '<div class="small muted mt12">No recommendations: nothing in this period needs attention.</div>'}</section>`;
}

function renderMonRequestModal() {
  const x = D.monRequest;
  if (!x || x.id !== monState().request) return `<div class="overlay" data-act="mon-close"><div class="modal" style="padding:40px">${loading()}</div></div>`;
  const row = (k, v) => `<div class="mon-kv"><span class="muted">${esc(k)}</span><span>${v}</span></div>`;
  const breakdown = Object.entries(x.context_breakdown || {}).filter(([, v]) => v).map(([k, v]) => ({ _label: k.replace(/_/g, ' '), v }));
  const entity = x.entity ? (x.entity.title ? `${esc(x.entity.title)} · ${esc(x.entity.company)} (${esc(x.entity.type)} #${x.entity.id})` : `${esc(x.entity.type)} #${x.entity.id}`) : '—';
  return `<div class="overlay" data-act="mon-close"><div class="modal" role="dialog" aria-label="LLM request detail" style="max-width:720px">
    <div class="modal-head"><div class="row between gap10"><div><div style="font-size:19px;font-weight:600">${esc(monStage(x.stage))} request</div><div class="muted small mt4">${esc(fmtDateTime(x.created_at))} · ${esc(x.request_id)}</div></div>
      ${x.status === 'FAILED' ? pill(MON_ERRORS[x.error_type] || 'failed', 'bad') : pill('succeeded', 'good')}</div></div>
    <div style="padding:18px 22px" class="col gap14">
      <div class="mon-kv-grid">
        ${row('Workflow', esc(x.workflow_type || '—'))}${row('Job / application', entity)}
        ${row('Provider', esc(x.provider))}${row('Model', esc(x.model || '—') + (x.requested_model && x.requested_model !== x.model ? ` <span class="faint">(requested ${esc(x.requested_model)})</span>` : ''))}
        ${row('Prompt version', esc(x.prompt_name ? `${x.prompt_name} ${x.prompt_version || ''}` : x.prompt_version || '—'))}${row('Timestamp', esc(fmtDateTime(x.created_at)))}
        ${row('Input tokens', `${monTokens(x.input_tokens)}${x.usage_source === 'estimated' ? ' <span class="faint">(estimated)</span>' : x.usage_source === 'none' ? ' <span class="faint">(not reported)</span>' : ''}`)}${row('Output tokens', monTokens(x.output_tokens))}
        ${row('Context utilisation', x.context_utilization_percent == null ? `${monTokens(x.context_tokens)} tokens · window unknown` : `${(x.context_tokens || 0).toLocaleString()} / ${(x.context_window_size || 0).toLocaleString()} tokens · ${x.context_utilization_percent.toFixed(1)}%`)}
        ${row('Cached tokens', x.cache_read_tokens == null ? '—' : `${monTokens(x.cache_read_tokens)} read${x.cache_write_tokens ? ` · ${monTokens(x.cache_write_tokens)} written` : ''}`)}
        ${row('Latency', monMs(x.latency_ms))}${row('Estimated cost', `${monUSD(x.estimated_cost)} <span class="faint">(${esc(x.cost_source.replace(/_/g, ' '))}${x.pricing_effective_from ? `, prices from ${esc(x.pricing_effective_from)}` : ''})</span>`)}
        ${row('Retry count', x.retry_count)}${row('Structured output', x.structured_output_valid == null ? 'not checked' : x.structured_output_valid ? 'valid' : `invalid${x.structured_output_retry_count ? ` · ${x.structured_output_retry_count} earlier failure${x.structured_output_retry_count === 1 ? '' : 's'}` : ''}`)}
      </div>
      ${breakdown.length ? `<div><div class="caps-sm mb8">Estimated context by source</div>${monBars(breakdown, b => b.v, b => `${monTokens(b.v)} tokens`, b => `${b._label}: ${b.v.toLocaleString()} estimated tokens`, { labelWidth: 130, valueWidth: 96 })}</div>` : ''}
      ${x.error_message ? `<div class="box bad"><div class="xs b mb4" style="color:var(--bad)">ERROR</div><div class="small mono" style="white-space:pre-wrap;word-break:break-word">${esc(x.error_message)}</div></div>` : ''}
      ${x.debug ? `<details><summary class="small b">Prompt debugging (stored because LLM_PROMPT_DEBUG is on)</summary><pre class="mon-debug">${esc(x.debug.prompt || '')}</pre><pre class="mon-debug">${esc(x.debug.response || '')}</pre></details>` : '<div class="xs faint">Prompt and response text are not stored. Only metadata and token counts are kept.</div>'}
    </div>
    <div class="card-foot row gap8" style="justify-content:flex-end">${x.entity?.job_id ? btn('Open job', 'mon-open-job', { id: x.entity.job_id }) : ''}${btn('Close', 'mon-close')}</div></div></div>`;
}

const MONITORING_ACTIONS = {
  'mon-range': el => { Object.assign(monState(), { range: el.dataset.id, page: 1 }); load(); },
  'mon-filter': el => { monState()[el.dataset.key] = el.dataset.key === 'job_id' ? el.value.replace(/\D/g, '') : el.value; monState().page = 1; load(); },
  'mon-all': el => { Object.assign(monState(), { all: el.checked, page: 1 }); load(); },
  'mon-reset': () => { Object.assign(monState(), { provider: '', model: '', stage: '', prompt_version: '', status: '', job_id: '', all: false, page: 1 }); load(); },
  'mon-refresh': () => busy('monRefresh', () => loadMonitoring()),
  'mon-table': el => { const t = monState().tables; t.has(el.dataset.key) ? t.delete(el.dataset.key) : t.add(el.dataset.key); render(); },
  'mon-page': el => { monState().page = Math.max(1, monState().page + Number(el.dataset.dir)); load(); },
  'mon-request': async el => {
    monState().request = Number(el.dataset.id); D.monRequest = null; render();
    try { D.monRequest = await api(`/api/monitoring/requests/${el.dataset.id}?` + qs({ all_accounts: monState().all ? 'true' : '' })); }
    catch (e) { monState().request = null; toast(e.message, true); }
    render();
  },
  'mon-close': (el, e) => { if (e && el.classList.contains('overlay') && e.target !== el) return; monState().request = null; render(); },
  'mon-open-job': el => { monState().request = null; openJob(el.dataset.id); },
  'mon-analyze': () => busy('monAnalyze', async () => {
    await api('/api/monitoring/analyze', { method: 'POST', body: { range: monState().range === 'today' ? 'today' : monState().range } });
    await loadMonitoring();
  }, 'Analysis queued. Recommendations appear here when it finishes.'),
  'mon-schedule': el => busy('monSchedule', async () => {
    await api('/api/monitoring/schedule', { method: 'PUT', body: { schedule: el.value } });
    D.monRec = { ...D.monRec, schedule: el.value };
  }, `Scheduled analysis ${el.value === 'off' ? 'turned off' : `set to ${el.value}`}`),
};

// Hover details for chart marks: one tooltip element follows the pointer over any [data-tip].
(() => {
  let tip = null;
  const place = (e, text) => {
    if (!tip) { tip = document.createElement('div'); tip.className = 'viz-tip'; tip.setAttribute('role', 'tooltip'); document.body.appendChild(tip); }
    tip.textContent = text; tip.hidden = false;
    const pad = 14, w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = `${Math.min(window.innerWidth - w - 8, e.clientX + pad)}px`;
    tip.style.top = `${Math.max(8, e.clientY - h - pad)}px`;
  };
  document.addEventListener('mousemove', e => {
    const el = e.target.closest?.('[data-tip]');
    if (el) place(e, el.dataset.tip); else if (tip) tip.hidden = true;
  });
  document.addEventListener('scroll', () => { if (tip) tip.hidden = true; }, true);
})();
