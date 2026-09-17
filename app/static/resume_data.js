/* Settings → Resume Data: review, edit and verify the structured resume. Loaded after settings.js, before app.js.
   The server owns verification state; this editor only sends typed structured data and verify/reject requests. */
'use strict';
const RD_SECTIONS = [
  ['personal_information', 'Personal information'], ['summary', 'Professional summary'], ['skills', 'Skills'],
  ['experience', 'Work experience'], ['projects', 'Projects'], ['education', 'Education'],
  ['certifications', 'Certifications'], ['awards', 'Awards'], ['languages', 'Languages'],
  ['additional_information', 'Additional information'],
];
// Editable fields per section: [key, label, kind]. kind: text | textarea | list (comma separated) | bool | month
const RD_FIELDS = {
  personal_information: [['full_name', 'Full name'], ['headline', 'Headline'], ['location', 'Location'], ['email', 'Email'], ['phone', 'Phone'], ['linkedin', 'LinkedIn'], ['github', 'GitHub'], ['portfolio', 'Portfolio']],
  summary: [['text', 'Summary', 'textarea']],
  skills: [['name', 'Skill'], ['category', 'Category']],
  experience: [['job_title', 'Job title'], ['company', 'Company'], ['location', 'Location'], ['employment_type', 'Employment type'], ['start_date', 'Start (YYYY-MM)', 'month'], ['end_date', 'End (YYYY-MM)', 'month'], ['is_current', 'Current role', 'bool'], ['technologies', 'Technologies', 'list']],
  projects: [['name', 'Name'], ['url', 'URL'], ['description', 'Description', 'textarea'], ['technologies', 'Technologies', 'list']],
  education: [['degree', 'Degree'], ['field_of_study', 'Field of study'], ['institution', 'Institution'], ['location', 'Location'], ['start_date', 'Start (YYYY-MM)', 'month'], ['end_date', 'End (YYYY-MM)', 'month']],
  certifications: [['name', 'Name'], ['issuer', 'Issuer'], ['issue_date', 'Issued'], ['expiration_date', 'Expires'], ['credential_id', 'Credential ID'], ['url', 'URL']],
  awards: [['title', 'Title'], ['issuer', 'Issuer'], ['date', 'Date'], ['description', 'Description', 'textarea']],
  languages: [['name', 'Language'], ['proficiency', 'Proficiency']],
  additional_information: [['text', 'Detail', 'textarea']],
};
const RD_BULLETS = { experience: [['achievements', 'Achievements'], ['responsibilities', 'Responsibilities']], projects: [['achievements', 'Achievements']] };
const RD_STATUS = { VERIFIED: ['✓', 'Verified', 'good'], REJECTED: ['✕', 'Rejected', 'bad'], EDITED: ['!', 'Edited · needs review', 'warn'], UNVERIFIED: ['?', 'Unverified', 'muted'] };
const RD_STATE = { VERIFIED: ['✓', 'good'], NEEDS_REVIEW: ['!', 'warn'], PARTIAL: ['…', 'brand'], UNVERIFIED: ['?', 'muted'], EMPTY: ['–', 'muted'] };
const RD_RESUME = { VERIFIED: ['Resume verified', 'good'], NEEDS_REVIEW: ['Needs review', 'warn'], PARTIALLY_VERIFIED: ['Partially verified', 'brand'], UNVERIFIED: ['Unverified', 'muted'] };
const RD = { tab: 'structured', editing: null, draft: null, decisions: {}, blocking: null, changes: null, raw: null, loading: false };

const rdActive = () => D.resumeData?.active || null;
const rdClone = value => JSON.parse(JSON.stringify(value ?? null));
const rdStatus = node => node?.verification?.status || 'UNVERIFIED';
function rdDate(iso) { return iso ? new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).toLocaleString() : ''; }
function rdReset() { RD.editing = null; RD.draft = null; RD.blocking = null; }
function resumeDataDirty() {
  const active = rdActive();
  return !!(RD.editing && RD.draft && active && JSON.stringify(RD.draft) !== JSON.stringify(active.structured));
}

async function resumeDataLoad(message) {
  RD.loading = true;
  try { D.resumeData = await api('/api/settings/resume'); RD.changes = null; RD.raw = null; if (message) toast(message); }
  catch (err) { D.resumeData = { error: err.message }; }
  finally { RD.loading = false; render(); }
}
function rdApply(detail) {
  const versions = (D.resumeData?.versions || []).map(v => v.id === detail.id ? { ...v, ...detail } : v);
  D.resumeData = { ...D.resumeData, active: detail.is_active ? detail : rdActive(), versions };
  RD.changes = null;
}
async function rdRun(action, success) {
  await settingsRun('resume-data', async () => { await action(); }, success);
}
function rdPost(path, body, success) {
  const active = rdActive();
  return rdRun(async () => { rdApply(await api(`/api/resumes/${active.id}/${path}`, { method: 'POST', body })); }, success);
}

/* ───────────── draft editing ───────────── */
function rdGet(path) { return path.split('.').reduce((node, key) => node?.[key], RD.draft); }
function rdSet(path, value) {
  const keys = path.split('.'), last = keys.pop();
  const parent = keys.reduce((node, key) => node[key], RD.draft);
  parent[last] = value;
}
function resumeDataInput(el) {
  if (!RD.draft) return;
  const kind = el.dataset.rdKind;
  const value = kind === 'bool' ? el.checked : kind === 'list' ? el.value.split(',').map(s => s.trim()).filter(Boolean) : el.value;
  rdSet(el.dataset.rdPath, kind === 'month' && !value ? null : value);
  settingsTouched('resume-data');
}
function rdTemplate(section) {
  const blank = Object.fromEntries((RD_FIELDS[section] || []).map(([key, , kind]) => [key, kind === 'list' ? [] : kind === 'bool' ? false : kind === 'month' ? null : '']));
  for (const [key] of RD_BULLETS[section] || []) blank[key] = [];
  return blank;
}
async function resumeDataSave() {
  const active = rdActive();
  if (!active || !RD.draft) return;
  await settingsRun('resume-data', async () => {
    try { rdApply(await api(`/api/resumes/${active.id}/structured`, { method: 'PUT', body: { revision: active.revision, data: RD.draft } })); }
    catch (err) {
      if (err.status === 409) { await resumeDataLoad(); throw new Error(`${err.message} Your edits were not saved.`); }
      throw err;
    }
    rdReset();
  }, 'Resume data saved');
}

/* ───────────── rendering ───────────── */
function rdBadge(node) {
  const [icon, label, tone] = RD_STATUS[rdStatus(node)] || RD_STATUS.UNVERIFIED;
  return `<span class="pill ${tone} rd-badge" title="${attr(label)}" data-rd-status="${rdStatus(node)}"><span aria-hidden="true">${icon}</span><span class="pt">${esc(label)}</span></span>`;
}
function rdItemActions(node) {
  if (!node?.id) return '';
  const status = rdStatus(node), busy = isBusy('settings-resume-data') || !!RD.editing;
  return `<span class="row gap6 wrap rd-item-actions">${status !== 'VERIFIED' ? btn('Verify', 'rd-verify-item', { id: node.id, status: 'VERIFIED' }, 'sm', busy) : ''}${status !== 'REJECTED' ? btn('Reject', 'rd-verify-item', { id: node.id, status: 'REJECTED' }, 'sm', busy) : ''}${status !== 'UNVERIFIED' ? btn('Unverify', 'rd-unverify', { id: node.id }, 'sm', busy) : ''}</span>`;
}
function rdDates(item) {
  const start = item.start_date || item.raw_start_date || '', end = item.is_current ? 'Present' : item.end_date || item.raw_end_date || '';
  return [start, end].filter(Boolean).join(' – ');
}
function rdTitle(section, item) {
  if (section === 'experience') return [item.job_title, item.company].filter(Boolean).join(' · ');
  if (section === 'education') return [item.degree, item.field_of_study, item.institution].filter(Boolean).join(' · ');
  if (section === 'languages') return [item.name, item.proficiency].filter(Boolean).join(' · ');
  if (section === 'skills') return item.category ? `${item.name} (${item.category})` : item.name;
  return item.name || item.title || item.text || '';
}
function rdViewItem(section, item) {
  const meta = [section === 'experience' || section === 'education' ? rdDates(item) : '', item.location, item.issuer, item.issue_date || item.date,
    (item.technologies || []).join(', '), item.description].filter(Boolean);
  const bullets = (RD_BULLETS[section] || []).map(([key, label]) => (item[key] || []).length ? `<div class="rd-bullets"><div class="caps-sm">${label}</div>${item[key].map(b => `<div class="rd-bullet" data-rd-item="${attr(b.id)}">${rdBadge(b)}<span class="grow">${esc(b.text)}</span>${rdItemActions(b)}</div>`).join('')}</div>` : '').join('');
  return `<article class="rd-item" data-rd-item="${attr(item.id)}"><div class="rd-item-head">${rdBadge(item)}<div class="grow"><strong>${esc(rdTitle(section, item) || 'Untitled')}</strong>${meta.length ? `<p>${esc(meta.join(' · '))}</p>` : ''}</div>${rdItemActions(item)}</div>${bullets}</article>`;
}
function rdInput(path, label, kind, value) {
  const id = `rd-${path.replace(/\./g, '-')}`;
  if (kind === 'bool') return `<label class="settings-check" for="${id}"><input type="checkbox" id="${id}" data-rd-path="${path}" data-rd-kind="bool" ${value ? 'checked' : ''}> ${esc(label)}</label>`;
  if (kind === 'textarea') return `<label class="field" for="${id}"><span>${esc(label)}</span><textarea class="textarea" id="${id}" data-rd-path="${path}">${esc(value ?? '')}</textarea></label>`;
  const shown = kind === 'list' ? (value || []).join(', ') : value ?? '';
  return `<label class="field" for="${id}"><span>${esc(label)}</span><input class="input" id="${id}" data-rd-path="${path}" ${kind ? `data-rd-kind="${kind}"` : ''} ${kind === 'month' ? 'placeholder="YYYY-MM" pattern="\\d{4}-\\d{2}"' : ''} value="${attr(shown)}"></label>`;
}
function rdListControls(path, index, length) {
  return `<span class="row gap6">${btn('↑', 'rd-move', { path, index, dir: -1 }, 'sm', index === 0)}${btn('↓', 'rd-move', { path, index, dir: 1 }, 'sm', index === length - 1)}${btn('Remove', 'rd-remove', { path, index }, 'sm')}</span>`;
}
function rdEditItem(section, path, item, index, length) {
  const fields = (RD_FIELDS[section] || []).map(([key, label, kind]) => rdInput(`${path}.${key}`, label, kind, item[key])).join('');
  const bullets = (RD_BULLETS[section] || []).map(([key, label]) => `<div class="rd-bullets"><div class="caps-sm">${label}</div>${(item[key] || []).map((b, i) => `<div class="rd-bullet-edit">${rdInput(`${path}.${key}.${i}.text`, `${label.slice(0, -1)} ${i + 1}`, 'textarea', b.text)}${rdListControls(`${path}.${key}`, i, item[key].length)}</div>`).join('')}${btn(`+ Add ${label.toLowerCase().slice(0, -1)}`, 'rd-add', { path: `${path}.${key}`, bullet: 1 }, 'sm')}</div>`).join('');
  const controls = index == null ? '' : `<div class="row between gap8"><span class="xs muted">${item.id ? `Changing a verified value marks it for review.` : 'New · starts unverified'}</span>${rdListControls(path.split('.').slice(0, -1).join('.'), index, length)}</div>`;
  return `<article class="rd-item rd-editing">${controls}<div class="settings-grid">${fields}</div>${bullets}</article>`;
}
function rdSectionCard(section, label, data, progress) {
  const state = progress.sections.find(s => s.section === section) || { state: 'EMPTY', counts: {} };
  const [icon, tone] = RD_STATE[state.state] || RD_STATE.EMPTY;
  const editing = RD.editing === section, value = editing ? RD.draft[section] : data[section];
  const list = Array.isArray(value);
  let body;
  if (editing) {
    body = list ? value.map((item, i) => rdEditItem(section, `${section}.${i}`, item, i, value.length)).join('') + btn(`+ Add ${label.toLowerCase()}`, 'rd-add', { path: section, section }, 'sm')
      : rdEditItem(section, section, value || {}, null, 0);
  } else if (list) {
    body = value.length ? value.map(item => rdViewItem(section, item)).join('') : '<p class="settings-help">Nothing parsed for this section.</p>';
  } else {
    const text = section === 'summary' ? value?.text : RD_FIELDS[section].map(([key]) => value?.[key]).filter(Boolean).join(' · ');
    body = text ? `<article class="rd-item"><div class="rd-item-head">${rdBadge(value)}<div class="grow"><p class="rd-text">${esc(text)}</p></div>${rdItemActions(value)}</div></article>` : '<p class="settings-help">Nothing parsed for this section.</p>';
  }
  const locked = isBusy('settings-resume-data') || (RD.editing && !editing);
  const buttons = editing ? `${btn('Cancel', 'rd-cancel', {}, 'sm')}${btn('Save section', 'settings-save', { form: 'resume-data' }, 'sm primary', !resumeDataDirty())}`
    : `${state.counts && (state.counts.UNVERIFIED || state.counts.EDITED) ? btn('Verify section', 'rd-verify-section', { section }, 'sm', locked) : ''}${btn('Edit', 'rd-edit', { section }, 'sm', locked)}`;
  return `<section class="settings-group rd-section" id="rd-section-${section}" data-rd-section="${section}"><div class="row between gap10 wrap mb12"><div class="row gap8"><span class="pill ${tone}" aria-label="${attr(state.state)}">${icon}</span><h3>${esc(label)}</h3>${state.required ? '<span class="xs muted">required</span>' : ''}</div><div class="row gap6 wrap">${buttons}</div></div>${body}</section>`;
}
function rdComparison(pending) {
  if (!pending) return '';
  const rows = pending.changes.map(c => {
    const choice = RD.decisions[c.id] || 'keep';
    const show = v => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);
    return `<div class="rd-compare" data-rd-change="${c.id}"><div class="small b">${esc(c.label)}${c.current_status ? ` · currently ${esc(c.current_status.toLowerCase())}` : ''}</div><div class="rd-compare-values"><div><span class="xs muted">Current value</span><p>${esc(show(c.current))}</p></div><div><span class="xs muted">New parser result</span><p>${esc(show(c.proposed))}</p></div></div><div class="row gap6">${btn('Keep current', 'rd-decide', { change: c.id, choice: 'keep' }, `sm ${choice === 'keep' ? 'primary' : ''}`)}${btn('Use new', 'rd-decide', { change: c.id, choice: 'use_new' }, `sm ${choice === 'use_new' ? 'primary' : ''}`)}</div></div>`;
  }).join('');
  return settingsGroup('Re-parse comparison', `The ${esc(pending.parser)} parser found ${pending.changes.length} difference${pending.changes.length === 1 ? '' : 's'}. Nothing changes until you choose. Values you take from the parser start unverified.`,
    `${rows || '<p class="settings-help">The new parse matches your current data.</p>'}<div class="row gap8 mt12">${btn('Apply choices', 'rd-resolve', {}, 'primary sm', isBusy('settings-resume-data'))}${btn('Keep everything', 'rd-resolve', { all: 'keep' }, 'sm')}</div>`);
}
function rdHeader(data) {
  const active = rdActive(), p = active.progress, [statusLabel, tone] = RD_RESUME[active.verification_status] || RD_RESUME.UNVERIFIED;
  const blocking = RD.blocking || p.blocking;
  const versions = D.resumeData.versions || [];
  const policy = D.resumeData.policy || {};
  const locked = isBusy('settings-resume-data') || !!RD.editing;
  return `<div class="settings-identity rd-header"><div class="grow"><strong>${esc(active.name || 'Resume')} · v${active.version}</strong><p>${pill(statusLabel, tone)} ${active.verified_at ? `Last verified ${esc(rdDate(active.verified_at))}` : 'Not verified yet'}</p></div><span class="settings-complete" data-rd-percent>${p.percent}% reviewed</span></div>` +
    settingsGroup('Verification', 'Verify facts one by one or a section at a time. Only verified facts are used for tailored applications.',
      `<div class="rd-counts row gap16 wrap"><span><strong>${p.counts.VERIFIED}</strong> verified</span><span><strong>${p.needs_review}</strong> require review</span><span><strong>${p.counts.EDITED}</strong> edited</span><span><strong>${p.counts.REJECTED}</strong> rejected</span></div>
      <div class="rd-overview">${p.sections.filter(s => s.state !== 'EMPTY').map(s => `<a href="#settings/resume-data" data-act="rd-jump" data-section="${s.section}" class="rd-overview-row"><span>${esc(s.label)}</span><span class="pill ${RD_STATE[s.state][1]}">${RD_STATE[s.state][0]}</span></a>`).join('')}${p.entries.filter(e => e.section === 'experience').map(e => `<div class="rd-overview-row rd-overview-entry"><span>${esc(e.label)}</span><span class="pill ${RD_STATE[e.state][1]}">${RD_STATE[e.state][0]}</span></div>`).join('')}</div>
      ${blocking.length ? `<div class="settings-notice"><strong>${blocking.length} required item${blocking.length === 1 ? '' : 's'} still need review</strong><p>${blocking.slice(0, 5).map(b => esc(b.label)).join(' · ')}${blocking.length > 5 ? ' …' : ''}</p></div>` : ''}
      <div class="row gap8 wrap mt12">${btn('Verify Resume', 'rd-verify-resume', {}, 'primary', locked || blocking.length > 0)}${btn('Re-parse resume', 'rd-reparse', { ai: 0 }, '', locked)}${btn('Re-parse with AI', 'rd-reparse', { ai: 1 }, '', locked)}</div>`) +
    settingsGroup('Versions', 'Each upload is a new version. The active version is what AI workflows use; older versions stay unchanged.',
      `<div class="settings-resume-list">${versions.map(v => `<article class="settings-resume"><div class="grow"><strong>${esc(v.name || 'Resume')} · v${v.version}</strong><p>${esc((RD_RESUME[v.verification_status] || RD_RESUME.UNVERIFIED)[0])}${v.is_active ? ' · Active' : ''}${v.is_default ? ' · Default' : ''}</p></div><div class="row gap8 wrap">${v.is_active ? '' : btn('Make active', 'rd-activate', { id: v.id }, 'sm', locked)}${v.is_default ? '' : btn('Make default', 'rd-default', { id: v.id }, 'sm', locked)}</div></article>`).join('')}</div>`) +
    settingsGroup('Candidate policy', 'How AI may use resume facts you have not verified.',
      `<div class="settings-checks"><label class="settings-check" for="rd-policy-allow"><input type="checkbox" id="rd-policy-allow" data-act-change="rd-policy" data-key="allow_unverified_resume_facts" ${policy.allow_unverified_resume_facts ? 'checked' : ''}> Allow unverified facts in tailored resumes and follow-ups</label><label class="settings-check" for="rd-policy-rank"><input type="checkbox" id="rd-policy-rank" data-act-change="rd-policy" data-key="ranking_uses_unverified" ${policy.ranking_uses_unverified ? 'checked' : ''}> Let ranking read unverified facts as lower-confidence context</label></div>`) +
    rdComparison(active.pending_reparse);
}
function rdTabs() {
  const tabs = [['structured', 'Structured Data'], ['raw', 'Raw Text'], ['original', 'Original Resume'], ['history', 'Change History']];
  return `<div class="rd-tabs row gap6 wrap" role="tablist">${tabs.map(([id, label]) => `<button class="btn sm ${RD.tab === id ? 'primary' : ''}" role="tab" aria-selected="${RD.tab === id}" data-act="rd-tab" data-tab="${id}">${label}</button>`).join('')}</div>`;
}
function rdTabBody(data) {
  const active = rdActive();
  if (RD.tab === 'raw') {
    if (RD.raw == null) { rdFetch('raw'); return loading(); }
    return settingsGroup('Raw text', 'Read-only. Correct facts in Structured Data; the parsed text itself is never edited.', `<pre class="rd-raw" data-rd-raw tabindex="0">${esc(RD.raw)}</pre>`);
  }
  if (RD.tab === 'original') {
    const version = (D.resumeData.versions || []).find(v => v.id === active.id) || active;
    return settingsGroup('Original resume', 'The uploaded file, exactly as it was uploaded.', version.has_original
      ? `<a class="btn" href="/api/resumes/${active.id}/original">Download ${esc(active.source_filename || 'original file')}</a>`
      : '<p class="settings-help">This version was created from text, or its original file is no longer stored.</p>');
  }
  if (RD.tab === 'history') {
    if (RD.changes == null) { rdFetch('changes'); return loading(); }
    const show = v => v == null ? '—' : typeof v === 'object' ? JSON.stringify(v) : String(v);
    return settingsGroup('Change history', `${RD.changes.total} change${RD.changes.total === 1 ? '' : 's'}, newest first.`, RD.changes.items.length
      ? `<div class="rd-history">${RD.changes.items.map(c => `<div class="rd-history-row" data-rd-change-type="${c.change_type}"><div class="row between gap8"><span class="small b">${esc(c.change_type.replace(/_/g, ' '))} · ${esc(c.source)}</span><span class="xs muted">${esc(rdDate(c.changed_at))}</span></div><div class="xs mono ellipsis">${esc(c.field_path)}</div><div class="xs muted">${esc(show(c.old_value))} → ${esc(show(c.new_value))}</div></div>`).join('')}</div>`
      : '<p class="settings-help">No changes yet.</p>');
  }
  return RD_SECTIONS.map(([section, label]) => rdSectionCard(section, label, data, active.progress)).join('');
}
async function rdFetch(kind) {
  const active = rdActive();
  if (RD.loading || !active) return;
  RD.loading = true;
  try {
    if (kind === 'raw') RD.raw = (await api(`/api/resumes/${active.id}/raw`)).raw_text || '';
    else RD.changes = await api(`/api/resumes/${active.id}/changes?limit=200`);
  } catch (err) { SETTING_ERRORS['resume-data'] = err.message; if (kind === 'raw') RD.raw = ''; else RD.changes = { total: 0, items: [] }; }
  finally { RD.loading = false; render(); }
}
function renderResumeDataSettings() {
  if (D.resumeData === undefined || D.resumeData === null) { if (!RD.loading) resumeDataLoad(); return loading(); }
  if (D.resumeData.error) return `<div class="settings-group"><div class="settings-error" role="alert">Resume data could not be loaded: ${esc(D.resumeData.error)}</div>${btn('Try again', 'rd-reload', {}, 'mt16')}</div>`;
  const active = rdActive();
  if (!active) return `<div class="settings-empty"><strong>No resume yet</strong><p>Upload a resume in <a href="#settings/resumes">Resumes</a>. It is parsed into structured data you can review and verify here.</p></div>`;
  const data = active.structured || {};
  return rdHeader(data) + `<div class="settings-group">${rdTabs()}</div>` + rdTabBody(data) + (RD.editing ? settingsFooter('resume-data', 'Save section') : '');
}

const RESUME_DATA_ACTIONS = {
  'rd-reload': () => resumeDataLoad(),
  'rd-tab': el => { if (RD.editing && resumeDataDirty() && !confirm('Discard unsaved resume data edits?')) return; rdReset(); RD.tab = el.dataset.tab; render(); },
  'rd-jump': (el, e) => { e?.preventDefault?.(); RD.tab = 'structured'; render(); document.getElementById(`rd-section-${el.dataset.section}`)?.scrollIntoView?.({ block: 'start' }); },
  'rd-edit': el => { RD.editing = el.dataset.section; RD.draft = rdClone(rdActive().structured); if (!RD.draft[RD.editing] && RD.editing === 'summary') RD.draft.summary = { text: '' }; render(); },
  'rd-cancel': () => { if (resumeDataDirty() && !confirm('Discard unsaved resume data edits?')) return; rdReset(); delete SETTING_ERRORS['resume-data']; render(); },
  'rd-add': el => {
    const list = rdGet(el.dataset.path);
    list.push(el.dataset.bullet ? { text: '' } : rdTemplate(el.dataset.section));
    render(); settingsTouched('resume-data');
  },
  'rd-remove': el => { rdGet(el.dataset.path).splice(Number(el.dataset.index), 1); render(); settingsTouched('resume-data'); },
  'rd-move': el => {
    const list = rdGet(el.dataset.path), from = Number(el.dataset.index), to = from + Number(el.dataset.dir);
    if (to < 0 || to >= list.length) return;
    [list[from], list[to]] = [list[to], list[from]]; render(); settingsTouched('resume-data');
  },
  'rd-verify-item': el => rdPost('verify-item', { item_id: el.dataset.id, status: el.dataset.status }, el.dataset.status === 'VERIFIED' ? 'Verified' : 'Rejected'),
  'rd-unverify': el => rdPost('unverify-item', { item_id: el.dataset.id }, 'Marked unverified'),
  'rd-verify-section': el => rdPost('verify-section', { section: el.dataset.section }, 'Section verified'),
  'rd-verify-resume': () => rdRun(async () => {
    try { rdApply(await api(`/api/resumes/${rdActive().id}/verify`, { method: 'POST' })); RD.blocking = null; }
    catch (err) { if (err.detail?.blocking) RD.blocking = err.detail.blocking; throw err; }
  }, 'Resume verified'),
  'rd-reparse': el => { RD.decisions = {}; return rdPost('reparse', { use_ai: el.dataset.ai === '1' }, 'Re-parse ready to compare'); },
  'rd-decide': el => { RD.decisions[el.dataset.change] = el.dataset.choice; render(); },
  'rd-resolve': el => {
    const decisions = el.dataset.all ? {} : { ...RD.decisions };
    RD.decisions = {};
    return rdPost('reparse/resolve', { decisions }, 'Re-parse choices applied');
  },
  'rd-activate': el => rdRun(async () => { await api(`/api/resumes/${el.dataset.id}/activate`, { method: 'POST' }); D.resumeData = await api('/api/settings/resume'); D.profile = await api('/api/profile'); RD.changes = null; RD.raw = null; }, 'Active resume changed'),
  'rd-default': el => rdRun(async () => { await api(`/api/resumes/${el.dataset.id}/default`, { method: 'POST' }); D.resumeData = await api('/api/settings/resume'); }, 'Default resume changed'),
  'rd-policy': el => rdRun(async () => { D.resumeData = { ...D.resumeData, policy: await api('/api/settings/resume/policy', { method: 'PUT', body: { [el.dataset.key]: el.checked } }) }; }, 'Candidate policy saved'),
};
