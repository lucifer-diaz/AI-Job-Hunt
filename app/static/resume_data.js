/* Settings → Resumes: every resume with its file, its structured data and the roles it is for, and the review,
   editing and verification of one resume's data. Loaded after settings.js, before app.js.
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
  education: [['degree', 'Degree'], ['field_of_study', 'Field of study'], ['institution', 'Institution'], ['university', 'University'], ['location', 'Location'], ['start_date', 'Start (YYYY-MM)', 'month'], ['end_date', 'End (YYYY-MM)', 'month'], ['grade', 'Grade (GPA / %)']],
  certifications: [['name', 'Name'], ['issuer', 'Issuer'], ['issue_date', 'Issued'], ['expiration_date', 'Expires'], ['credential_id', 'Credential ID'], ['url', 'URL']],
  awards: [['title', 'Title'], ['issuer', 'Issuer'], ['date', 'Date'], ['description', 'Description', 'textarea']],
  languages: [['name', 'Language'], ['proficiency', 'Proficiency']],
  additional_information: [['text', 'Detail', 'textarea']],
};
const RD_BULLETS = { experience: [['achievements', 'Achievements'], ['responsibilities', 'Responsibilities']], projects: [['achievements', 'Achievements']] };
const RD_STATUS = { VERIFIED: ['✓', 'Verified', 'good'], REJECTED: ['✕', 'Rejected', 'bad'], EDITED: ['!', 'Edited · needs review', 'warn'], UNVERIFIED: ['?', 'Unverified', 'muted'] };
const RD_STATE = { VERIFIED: ['✓', 'good'], NEEDS_REVIEW: ['!', 'warn'], PARTIAL: ['…', 'brand'], UNVERIFIED: ['?', 'muted'], EMPTY: ['–', 'muted'] };
const RD_RESUME = { VERIFIED: ['Resume verified', 'good'], NEEDS_REVIEW: ['Needs review', 'warn'], PARTIALLY_VERIFIED: ['Partially verified', 'brand'], UNVERIFIED: ['Unverified', 'muted'] };
const RD = { tab: 'structured', renaming: null, editing: null, draft: null, decisions: {}, blocking: null, changes: null, raw: null, loading: false, viewId: null };

// The version shown and edited here. Viewing or editing a version never makes it active: only "Make active" does.
const rdActive = () => D.resumeData?.current || D.resumeData?.active || null;
const rdResumeUrl = () => RD.viewId ? `/api/settings/resume?resume_id=${RD.viewId}` : '/api/settings/resume';
const rdClone = value => JSON.parse(JSON.stringify(value ?? null));
const rdStatus = node => node?.verification?.status || 'UNVERIFIED';
function rdDate(iso) { return iso ? new Date(iso + (iso.endsWith('Z') ? '' : 'Z')).toLocaleString() : ''; }
function rdReset() { RD.editing = null; RD.draft = null; RD.blocking = null; }
// Before the screen shows another resume (view, activate, delete, a new upload or text): an open section edit
// belongs to the resume it was opened on, so it is discarded — after asking when it has changes. False: stay.
function rdLeaveDraft() {
  if (RD.editing && resumeDataDirty() && !confirm('Discard unsaved resume data edits?')) return false;
  rdReset(); return true;
}
function resumeDataDirty() {
  const active = rdActive();
  return !!(RD.editing && RD.draft && active && JSON.stringify(RD.draft) !== JSON.stringify(active.structured));
}

async function resumeDataLoad(message) {
  RD.loading = true;
  try { D.resumeData = await api(rdResumeUrl()); RD.changes = null; RD.raw = null; if (message) toast(message); }
  catch (err) { D.resumeData = { error: err.message }; }
  finally { RD.loading = false; render(); }
}
function rdApply(detail) {
  const versions = (D.resumeData?.versions || []).map(v => v.id === detail.id ? { ...v, ...detail } : v);
  const current = !D.resumeData?.current || D.resumeData.current.id === detail.id ? detail : D.resumeData.current;
  D.resumeData = { ...D.resumeData, current, active: detail.is_active ? detail : D.resumeData?.active, versions };
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
  const meta = [section === 'experience' || section === 'education' ? rdDates(item) : '', item.university, item.location, item.grade, item.issuer, item.issue_date || item.date,
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
const RD_POLICY_SOURCES = [
  ['structured', 'Structured data', 'The resume is built from its data: your corrections are used everywhere (titles, employers, dates, education). Rejected facts are left out; unverified facts are claimed only as allowed below.'],
  ['master_text', 'Resume text as written', 'The resume’s own text: its role headings and bullets, used as-is. Corrections to its data are not applied.'],
];
function rdLocked() { return isBusy('settings-resume-data') || !!RD.editing; }
// One resume in the list: its file, how far its data is reviewed, the roles it is for and what uses it.
function rdResumeCard(v, current) {
  const locked = rdLocked(), viewing = current && v.id === current.id;
  const kind = v.file ? esc((v.file.filename.split('.').pop() || 'file').toUpperCase().slice(0, 4)) : 'TXT';
  const source = v.file ? `${esc(v.file.filename)} · ${Math.max(1, Math.round(v.file.size / 1024))} KB` : 'Created from text';
  const status = (RD_RESUME[v.verification_status] || RD_RESUME.UNVERIFIED)[0];
  return `<article class="settings-resume rd-resume${viewing ? ' is-viewing' : ''}" data-rd-resume="${v.id}"${viewing ? ' aria-current="true"' : ''}>
    <div class="settings-file-icon" aria-hidden="true">${kind}</div>
    <div class="grow">${RD.renaming === v.id ? rdRenameEditor(v) : `<div class="row gap8 wrap" style="align-items:center"><strong>${esc(v.name || 'Resume')} · v${v.version}</strong>${v.is_active ? pill('Active', 'good') : ''}${viewing ? pill('Reviewing below', 'brand') : ''}${btn('Rename', 'rd-rename', { id: v.id, name: v.name || '' }, 'sm ghost', locked)}</div>`}
      <p>${source} · ${esc(status)} · ${v.reviewed_percent ?? 0}% reviewed${v.setups?.length ? ` · Used by ${esc(v.setups.map(x => x.name).join(', '))}` : ''}</p>
      <div class="rd-roles"><span class="xs muted">Target roles</span> <span class="xs faint">· jobs whose title names one are ranked and tailored with this resume</span>${resumeTags('version', v.id, v.domain, v.roles || [])}</div>
      <div class="row gap8 wrap mt10">${viewing ? '' : btn('Review data', 'rd-view', { id: v.id }, 'sm', locked)}${v.is_active ? '' : btn('Make active', 'rd-activate', { id: v.id }, 'sm', locked)}${v.has_original ? `<a class="btn sm" href="/api/resumes/${v.id}/original">Download</a>` : ''}${v.is_active ? '' : btn('Delete', 'rd-delete-version', { id: v.id, name: `${v.name || 'Resume'} · v${v.version}` }, 'sm danger', locked)}</div>
    </div>
  </article>`;
}
// Renaming changes only the label: the file, data, tags, verification and the active resume stay as they are.
function rdRenameEditor(v) {
  return `<div class="row gap8 wrap" style="align-items:center" data-rd-rename="${v.id}">${inputF('rdname', 'name', v.name || '', `maxlength="300" aria-label="Resume name" style="margin:0;flex:0 1 320px;min-width:0" data-enter="rd-rename-save" data-id="${v.id}"`)}<span class="small muted">· v${v.version}</span>${btn(label('rdRename', 'Save name', 'Saving…'), 'rd-rename-save', { id: v.id }, 'sm primary', isBusy('rdRename'))}${btn('Cancel', 'rd-rename-cancel', {}, 'sm')}</div>`;
}
function rdResumeList(current) {
  const versions = D.resumeData.versions || [];
  return settingsGroup('Your resumes', 'Each resume has its own data and the roles it is for. The active resume is used for ranking, tailoring and review, unless a job’s title matches another resume’s target roles, or its search setup or you chose one. Uploading or editing never changes which resume is active.',
    `<div class="settings-resume-list">${versions.map(v => rdResumeCard(v, current)).join('') || '<div class="settings-empty"><strong>Your experience starts here</strong><p>Upload a resume to make your applications more personal. It is parsed into data you can review and verify here.</p></div>'}</div>
    <label class="settings-upload" for="resume-file"><strong>＋ Upload ${versions.length ? 'another resume' : 'your resume'}</strong><span>PDF, DOCX or TXT · up to ${BOOT.max_upload_mb} MB</span><input type="file" id="resume-file" accept=".txt,.pdf,.docx" aria-label="Upload resume"></label>
    ${versions.some(v => v.domain || v.roles?.length) ? `<div class="row gap8 mt12" style="align-items:center">${btn(label('resumeRematch', 'Re-match existing jobs', 'Checking…'), 'resume-rematch', {}, 'sm soft', isBusy('resumeRematch'))}<span class="settings-help">New jobs use the target roles right away; this re-matches jobs found earlier (not ones you or a search pinned).</span></div>` : ''}`);
}
function rdPolicy() {
  const policy = D.resumeData.policy || {};
  return settingsGroup('Candidate policy', 'Which form of a resume AI works from for ranking, tailoring and review, and how it may use facts you have not verified. It applies to every resume.',
    `<fieldset class="settings-options" style="border:0;padding:0;margin:0 0 14px"><legend class="settings-help" style="margin-bottom:8px">Resume source for AI</legend>${RD_POLICY_SOURCES.map(([value, title, help]) => `<label class="settings-check" style="align-items:flex-start"><input type="radio" name="rd-resume-source" data-act-change="rd-policy-source" value="${value}" ${(policy.resume_source || 'structured') === value ? 'checked' : ''}><span><b>${title}</b><br><span class="settings-help">${help}</span></span></label>`).join('')}</fieldset>
    <div class="settings-checks"><label class="settings-check" for="rd-policy-allow"><input type="checkbox" id="rd-policy-allow" data-act-change="rd-policy" data-key="allow_unverified_resume_facts" ${policy.allow_unverified_resume_facts ? 'checked' : ''}> Allow unverified facts in tailored resumes and follow-ups</label><label class="settings-check" for="rd-policy-rank"><input type="checkbox" id="rd-policy-rank" data-act-change="rd-policy" data-key="ranking_uses_unverified" ${policy.ranking_uses_unverified ? 'checked' : ''}> Let ranking read unverified facts as lower-confidence context</label></div>`);
}
// The AI structuring of this version (resume_parsing on the server): running, or how it ended. While it runs the
// screen re-reads the resume every few seconds, so the filled data or the comparison shows up without a reload.
const RD_AI_TONE = { filled: 'good', compare: 'brand', kept: 'muted', not_configured: 'warn', gone: 'muted' };
let rdAiTimer = null;
function rdAiParse(ai) {
  if (!ai) return '';
  const tone = ai.active ? 'brand' : ai.status === 'FAILED' ? 'bad' : RD_AI_TONE[ai.outcome] || 'muted';
  const dropped = !ai.active && ai.dropped ? ` ${ai.dropped} value${ai.dropped === 1 ? '' : 's'} the resume text does not contain ${ai.dropped === 1 ? 'was' : 'were'} left out.` : '';
  const detail = !ai.active && ai.detail && ai.outcome !== 'filled' && ai.outcome !== 'compare' ? ` ${esc(ai.detail)}` : '';
  rdAiPoll(ai);
  return `<p class="settings-help mt8" data-rd-ai-parse="${ai.active ? 'active' : esc(ai.outcome || ai.status.toLowerCase())}">${pill(ai.note || 'AI parse', tone)}${dropped}${detail}</p>`;
}
function rdAiPoll(ai) {
  if (rdAiTimer || !ai?.active) return;
  rdAiTimer = setTimeout(async () => {
    rdAiTimer = null;
    if (!document.querySelector('[data-rd-ai-parse="active"]')) return; // the screen moved on
    if (RD.editing) return rdAiPoll(ai); // never replace data under an open edit
    try {
      const fresh = await api(rdResumeUrl());
      if (RD.editing) return rdAiPoll(ai);
      D.resumeData = fresh; RD.changes = null;
      const now = rdActive()?.ai_parse;
      if (now && !now.active && now.note) toast(now.note);
      render();
    } catch { rdAiPoll(ai); }
  }, 4000);
}
// The data of the resume being reviewed: verification, then its sections.
function rdHeader(data) {
  const active = rdActive(), p = active.progress, [statusLabel, tone] = RD_RESUME[active.verification_status] || RD_RESUME.UNVERIFIED;
  const blocking = RD.blocking || p.blocking;
  const locked = rdLocked();
  // Already verified: nothing to verify until a change (an edit, a re-parse choice) sends it back to review.
  const verified = active.verification_status === 'VERIFIED';
  return `<div class="settings-identity rd-header" id="rd-detail"><div class="grow"><span class="settings-eyebrow">RESUME DATA</span><strong>${esc(active.name || 'Resume')} · v${active.version}</strong> ${active.is_active ? pill('Active', 'good') : pill('Not active', 'muted')}<p>${pill(statusLabel, tone)} ${active.verified_at ? `Last verified ${esc(rdDate(active.verified_at))}` : 'Not verified yet'}</p></div><span class="settings-complete" data-rd-percent>${p.percent}% reviewed</span></div>` +
    (active.is_active ? '' : `<div class="settings-notice"><strong>This resume is not active</strong><p>Editing and verifying it does not change which resume is used. It is used for jobs that match its target roles, or that you or a search setup chose it for.</p><div class="row gap8 mt8">${btn('Make active', 'rd-activate', { id: active.id }, 'sm primary', locked)}</div></div>`) +
    settingsGroup('Verification', 'Verify facts one by one or a section at a time. Only verified facts are used for tailored applications.',
      `<div class="rd-counts row gap16 wrap"><span><strong>${p.counts.VERIFIED}</strong> verified</span><span><strong>${p.needs_review}</strong> require review</span><span><strong>${p.counts.EDITED}</strong> edited</span><span><strong>${p.counts.REJECTED}</strong> rejected</span></div>
      <div class="rd-overview">${p.sections.filter(s => s.state !== 'EMPTY').map(s => `<a href="#settings/resumes" data-act="rd-jump" data-section="${s.section}" class="rd-overview-row"><span>${esc(s.label)}</span><span class="pill ${RD_STATE[s.state][1]}">${RD_STATE[s.state][0]}</span></a>`).join('')}${p.entries.filter(e => e.section === 'experience').map(e => `<div class="rd-overview-row rd-overview-entry"><span>${esc(e.label)}</span><span class="pill ${RD_STATE[e.state][1]}">${RD_STATE[e.state][0]}</span></div>`).join('')}</div>
      ${blocking.length ? `<div class="settings-notice"><strong>${blocking.length} required item${blocking.length === 1 ? '' : 's'} still need review</strong><p>${blocking.slice(0, 5).map(b => esc(b.label)).join(' · ')}${blocking.length > 5 ? ' …' : ''}</p></div>` : ''}
      <div class="row gap8 wrap mt12">${verified ? btn('✓ Resume verified', 'rd-verify-resume', {}, 'primary', true) : btn('Verify Resume', 'rd-verify-resume', {}, 'primary', locked || blocking.length > 0)}${btn('Re-parse resume', 'rd-reparse', { ai: 0 }, '', locked)}${btn('Re-parse with AI', 'rd-reparse', { ai: 1 }, '', locked || !!active.ai_parse?.active)}</div>${rdAiParse(active.ai_parse)}`) +
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
// Settings → Resumes: the resumes, then the data of the one being reviewed, then the policy and the text tools.
function renderResumesScreen() {
  if (D.resumeData === undefined || D.resumeData === null) { if (!RD.loading) resumeDataLoad(); return loading(); }
  if (D.resumeData.error) return `<div class="settings-group"><div class="settings-error" role="alert">Your resumes could not be loaded: ${esc(D.resumeData.error)}</div>${btn('Try again', 'rd-reload', {}, 'mt16')}</div>`;
  const current = rdActive(), data = current?.structured || {};
  const detail = current ? rdHeader(data) + `<div class="settings-group">${rdTabs()}</div>` + rdTabBody(data) + (RD.editing ? settingsFooter('resume-data', 'Save section') : '') : '';
  return rdResumeList(current) + detail + rdPolicy() + resumeTextTools();
}
const renderResumeDataSettings = renderResumesScreen;

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
  'rd-verify-resume': () => rdActive()?.verification_status === 'VERIFIED' ? undefined : rdRun(async () => {
    try { rdApply(await api(`/api/resumes/${rdActive().id}/verify`, { method: 'POST' })); RD.blocking = null; }
    catch (err) { if (err.detail?.blocking) RD.blocking = err.detail.blocking; throw err; }
  }, 'Resume verified'),
  // The AI parse runs in the background: it fills the data while nothing is reviewed, else it arrives as a comparison.
  'rd-reparse': el => { RD.decisions = {}; const ai = el.dataset.ai === '1'; return rdPost('reparse', { use_ai: ai }, ai ? 'Structuring with AI…' : 'Re-parse ready to compare'); },
  'rd-decide': el => { RD.decisions[el.dataset.change] = el.dataset.choice; render(); },
  'rd-resolve': el => {
    const decisions = el.dataset.all ? {} : { ...RD.decisions };
    RD.decisions = {};
    return rdPost('reparse/resolve', { decisions }, 'Re-parse choices applied');
  },
  'rd-activate': el => settingsResumeGuard() && rdLeaveDraft() && rdRun(async () => { await api(`/api/resumes/${el.dataset.id}/activate`, { method: 'POST' }); RD.viewId = null; D.resumeData = await api(rdResumeUrl()); D.profile = await api('/api/profile'); delete S.forms.resumes; D.resumeChoices = null; RD.changes = null; RD.raw = null; }, 'Active resume changed: ranking, tailoring and review now use it'),
  'rd-rename': el => { RD.renaming = Number(el.dataset.id); S.forms.rdname = { name: el.dataset.name || '' }; render(); document.getElementById('rdname-name')?.focus(); },
  'rd-rename-cancel': () => { RD.renaming = null; delete S.forms.rdname; render(); },
  'rd-rename-save': el => {
    const name = String(fv('rdname', 'name', '') || '').trim();
    if (!name) return toast('A resume needs a name', true);
    return busy('rdRename', async () => {
      const out = await api(`/api/resumes/${el.dataset.id}`, { method: 'PUT', body: { name } });
      const rename = r => r && r.id === out.id ? { ...r, name: out.name } : r;
      D.resumeData = { ...D.resumeData, versions: (D.resumeData.versions || []).map(rename), active: rename(D.resumeData.active), current: rename(D.resumeData.current) };
      RD.renaming = null; delete S.forms.rdname; D.resumeChoices = null;
      return out;
    }, 'Resume renamed');
  },
  'rd-view': el => {
    if (!rdLeaveDraft()) return;
    RD.viewId = Number(el.dataset.id); RD.tab = 'structured';
    return rdRun(async () => { D.resumeData = await api(rdResumeUrl()); RD.changes = null; RD.raw = null; })
      .then(() => document.getElementById('rd-detail')?.scrollIntoView?.({ block: 'start' }));
  },
  // The active resume cannot be deleted (the server refuses too): make another one active first.
  'rd-delete-version': el => {
    if (!confirm(`Delete ${el.dataset.name}? Its file, data and verification are removed. Jobs or searches that used it go back to matching by target roles (else the active resume), and facts only it verified stop being used.`)) return;
    if (RD.viewId === Number(el.dataset.id) && !rdLeaveDraft()) return;
    return rdRun(async () => {
      const out = await api(`/api/resumes/${el.dataset.id}`, { method: 'DELETE' });
      if (RD.viewId === Number(el.dataset.id)) RD.viewId = null;
      D.resumeData = await api(rdResumeUrl()); D.resumeChoices = null; RD.changes = null; RD.raw = null;
     
      toast(`Resume deleted${out.jobs ? ` · ${out.jobs} job${out.jobs === 1 ? '' : 's'} now matched by tags` : ''}${out.evidence_retired ? ` · ${out.evidence_retired} fact${out.evidence_retired === 1 ? '' : 's'} no longer verified` : ''}`);
    });
  },
  'rd-policy-source': el => rdRun(async () => { D.resumeData = { ...D.resumeData, policy: await api('/api/settings/resume/policy', { method: 'PUT', body: { resume_source: el.value } }) }; }, el.value === 'master_text' ? 'AI now works from the resume text as written' : 'AI now works from your structured resume data'),
  'rd-policy': el => rdRun(async () => { D.resumeData = { ...D.resumeData, policy: await api('/api/settings/resume/policy', { method: 'PUT', body: { [el.dataset.key]: el.checked } }) }; }, 'Candidate policy saved'),
};
