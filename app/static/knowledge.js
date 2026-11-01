/* Focused Knowledge bank workspace. Loaded before app.js; uses shared UI helpers. */
'use strict';
const KNOWLEDGE_SECTIONS = [['answers', 'Answer library'], ['evidence', 'Evidence bank']];
const KNOWLEDGE_CATEGORIES = ['general', 'work_authorization', 'compensation', 'notice_period', 'relocation', 'demographic'];
const knowledgeSection = value => value === 'evidence' ? 'evidence' : 'answers';
const knowledgeLabel = value => String(value).replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
function knowledgeState() {
  return S.knowledgeUI ||= { editors: {}, errors: {}, tagging: null, selected: {}, filters: { answers: { q: '', status: 'all', category: 'all', sensitive: 'all' }, evidence: { q: '', status: 'all', resume: 'all' } } };
}
function knowledgeRows(section) { return (section === 'answers' ? D.knowledge : D.evidence) || []; }
function knowledgeNavigate(section) {
  S.knowledgeSection = knowledgeSection(section); setHash(); render();
  document.getElementById('knowledge-heading')?.focus();
}
async function loadKnowledge() {
  D.knowledgeLoading = true; D.knowledgeLoadError = ''; render();
  try { [D.knowledge, D.evidence, D.evidenceResumes] = await Promise.all([api('/api/knowledge'), api('/api/evidence'), api('/api/evidence/resumes').catch(() => [])]); }
  catch (error) { D.knowledgeLoadError = error.message || 'Could not load your Knowledge bank.'; }
  finally { D.knowledgeLoading = false; }
}
function knowledgeFiltered(section) {
  const f = knowledgeState().filters[section], query = f.q.trim().toLowerCase();
  return knowledgeRows(section).filter(row => {
    const text = section === 'answers' ? [row.question, row.answer, knowledgeLabel(row.category)] : [row.claim, row.company, row.role, ...(row.skills || [])];
    return text.join(' ').toLowerCase().includes(query)
      && (section !== 'answers' || !f.ids?.length || f.ids.includes(row.id))
      && (f.status === 'all' || row.verified === (f.status === 'verified'))
      && (section !== 'evidence' || evidenceMatchesResume(row, f.resume || 'all'))
      && (section !== 'answers' || ((f.category === 'all' || row.category === f.category)
        && (f.sensitive === 'all' || !!row.sensitive === (f.sensitive === 'sensitive'))));
  });
}
function knowledgeFilterEvent(el) {
  if (evidenceTagEvent(el)) return true;
  if (!el.dataset.knowledgeFilter) return false;
  knowledgeState().filters[S.knowledgeSection || 'answers'][el.dataset.knowledgeFilter] = el.value;
  render(); return true;
}
function knowledgeSelect(key, title, options, filters) {
  return `<label class="knowledge-filter-label" for="knowledge-${key}">${title}<select class="select" id="knowledge-${key}" data-knowledge-filter="${key}">${options.map(([value, text]) => `<option value="${attr(value)}" ${filters[key] === value ? 'selected' : ''}>${esc(text)}</option>`).join('')}</select></label>`;
}
function knowledgeToolbar(section) {
  const f = knowledgeState().filters[section];
  const categories = [...new Set([...KNOWLEDGE_CATEGORIES, ...knowledgeRows('answers').map(a => a.category)])];
  return `<div class="knowledge-toolbar"><label class="knowledge-filter-label knowledge-search" for="knowledge-search">Search ${section === 'answers' ? 'answers' : 'evidence'}<input class="input" type="search" id="knowledge-search" data-knowledge-filter="q" value="${attr(f.q)}" placeholder="${section === 'answers' ? 'Find a question or answer…' : 'Find a claim, company, role, or skill…'}"></label>
    ${knowledgeSelect('status', 'Verification', [['all', 'All statuses'], ['verified', 'Verified'], ['review', 'Needs review']], f)}
    ${section === 'evidence' && evidenceResumes().length ? knowledgeSelect('resume', 'Resume', [['all', 'All resumes'], ...evidenceResumes().map(r => [String(r.id), `${evidenceResumeName(r)} · ${r.evidence_in_scope ?? 0} facts in scope`]), ['untagged', 'Untagged (all resumes)']], f) : ''}
    ${section === 'answers' ? knowledgeSelect('category', 'Category', [['all', 'All categories'], ...categories.map(c => [c, knowledgeLabel(c)])], f) + knowledgeSelect('sensitive', 'Sensitivity', [['all', 'All answers'], ['sensitive', 'Sensitive'], ['standard', 'Not sensitive']], f) : ''}</div>`;
}
// Shown while the list is limited to the questions an application asked (from the Apply kit).
function knowledgeIdsBanner(section) {
  const ids = knowledgeState().filters.answers.ids;
  if (section !== 'answers' || !ids?.length) return '';
  return `<div class="banner mb12" role="status"><span class="grow">Showing the ${ids.length} question${ids.length === 1 ? '' : 's'} an application is waiting for. Edit each one, add your answer and mark it verified: the application continues by itself.</span>${btn('Show all answers', 'kn-clear-ids', {}, 'sm')}</div>`;
}
function knowledgeBadges(row) {
  return `${pill(row.verified ? 'Verified' : 'Needs review', row.verified ? 'good' : 'warn')}${row.sensitive ? pill('Sensitive · confirm each time', 'bad') : ''}`;
}
function knowledgeAsked(row) {
  // A question an application form asked while applying: which job, and the choices the form offered.
  const asked = row.asked_by, options = row.answer_options || [];
  if (!asked && !options.length) return '';
  const job = asked ? `Asked by ${esc([asked.company, asked.title].filter(Boolean).join(' · ') || `job ${asked.job_id}`)}${asked.required ? ' · required' : ''}` : '';
  return `<p class="xs muted">${[job, options.length ? `Options: ${options.map(esc).join(' / ')}` : ''].filter(Boolean).join(' — ')}</p>`;
}
function knowledgeAnswer(row) {
  return `<article class="knowledge-item" aria-labelledby="knowledge-title-${row.id}"><div class="knowledge-item-body"><div class="row gap6 wrap mb8">${knowledgeBadges(row)}${pill(knowledgeLabel(row.category))}</div>
    <h3 id="knowledge-title-${row.id}">${esc(row.question)}</h3>${knowledgeAsked(row)}<p class="knowledge-answer">${esc(row.answer || 'Awaiting answer')}</p><span class="xs muted">Used ${row.use_count || 0} times</span></div>
    <div class="knowledge-actions">${btn('Copy', 'kn-copy', { id: row.id }, 'sm', !row.answer || isBusy('knowledge'))}${btn('Edit', 'kn-edit', { id: row.id }, 'sm', isBusy('knowledge'))}${btn('Delete', 'kn-delete', { id: row.id }, 'sm ghost danger', isBusy('knowledge'))}</div></article>`;
}
/* ───── evidence ↔ resume tags: which resume a fact belongs to; a job only draws on its own resume's facts ───── */
const evidenceResumes = () => Array.isArray(D.evidenceResumes) ? D.evidenceResumes : [];
const evidenceResumeName = r => `${r.name || 'Resume'} · v${r.version}${r.is_active ? ' (active)' : ''}`;
function evidenceMatchesResume(row, choice) {
  if (choice === 'all') return true;
  const tags = row.resumes || [];
  if (choice === 'untagged') return !!row.all_resumes;
  return !!row.all_resumes || tags.some(t => String(t.id) === String(choice));
}
function evidenceTagChips(row) {
  const tags = row.resumes || [];
  if (row.all_resumes) return `<span class="xs muted">Resumes:</span>${pill('All resumes', 'plain')}`;
  if (!tags.length) return `<span class="xs muted">Resumes:</span>${pill('No resume — not used for jobs', 'warn')}`;
  return `<span class="xs muted">Resumes:</span>${tags.map(t => pill(`${t.name} · v${t.version}${t.origin === 'AUTO' ? ' · auto' : ''}`, t.origin === 'AUTO' ? 'plain' : 'good')).join('')}`;
}
function evidenceTagEditor(row) {
  const tagging = knowledgeState().tagging;
  if (!tagging || tagging.id !== row.id) return '';
  const choices = evidenceResumes();
  if (!choices.length) return '<p class="small muted mt8">Add a resume in Settings → Resumes to tag evidence to it.</p>';
  return `<fieldset class="knowledge-tags mt8" aria-label="Resumes this evidence belongs to"><legend class="xs muted">Belongs to (none selected = ${row.source === 'resume' ? 'not used for any resume' : 'all resumes'})</legend>
    ${choices.map(r => `<label class="row gap6"><input type="checkbox" data-ev-tag="${r.id}" ${tagging.selected.includes(r.id) ? 'checked' : ''}> ${esc(evidenceResumeName(r))}</label>`).join('')}
    <div class="row gap6 mt8">${btn('Save tags', 'ev-tags-save', { id: row.id }, 'sm primary', isBusy('knowledge'))}${btn('Cancel', 'ev-tags-cancel', { id: row.id }, 'sm', isBusy('knowledge'))}</div></fieldset>`;
}
function evidenceTagEvent(el) {
  if (el.dataset.evTag !== undefined) {
    const tagging = knowledgeState().tagging, id = Number(el.dataset.evTag);
    if (tagging) tagging.selected = el.checked ? [...new Set([...tagging.selected, id])] : tagging.selected.filter(r => r !== id);
    return true;
  }
  if (el.dataset.evSelect !== undefined) {
    const selected = knowledgeState().selected;
    if (el.checked) selected[el.dataset.evSelect] = true; else delete selected[el.dataset.evSelect];
    render(); return true;
  }
  return false;
}
function evidenceBulkBar() {
  const ids = Object.keys(knowledgeState().selected);
  if (!ids.length || !evidenceResumes().length) return '';
  return `<div class="knowledge-toolbar" role="group" aria-label="Tag selected evidence"><span class="small">${ids.length} selected</span>
    <label class="knowledge-filter-label" for="ev-bulk-resume">Resume<select class="select" id="ev-bulk-resume">${evidenceResumes().map(r => `<option value="${r.id}">${esc(evidenceResumeName(r))}</option>`).join('')}</select></label>
    ${btn('Add tag', 'ev-bulk', { mode: 'add' }, 'sm', isBusy('knowledge'))}${btn('Remove tag', 'ev-bulk', { mode: 'remove' }, 'sm', isBusy('knowledge'))}${btn('Clear selection', 'ev-bulk-clear', {}, 'sm ghost')}</div>`;
}
async function evidenceTagsSave(id) {
  const tagging = knowledgeState().tagging;
  if (!tagging) return;
  return knowledgeRun('evidence', async () => {
    await api(`/api/evidence/${id}/resumes`, { method: 'PUT', body: { resume_ids: tagging.selected } });
    knowledgeState().tagging = null;
    await loadKnowledge();
  }, 'Resume tags saved');
}
async function evidenceBulk(mode) {
  const ids = Object.keys(knowledgeState().selected).map(Number), resume = Number(document.getElementById('ev-bulk-resume')?.value);
  if (!ids.length || !resume) return;
  return knowledgeRun('evidence', async () => {
    await api('/api/evidence/bulk-resumes', { method: 'POST', body: { evidence_ids: ids, add: mode === 'add' ? [resume] : [], remove: mode === 'remove' ? [resume] : [] } });
    knowledgeState().selected = {};
    await loadKnowledge();
  }, mode === 'add' ? 'Tag added' : 'Tag removed');
}
function knowledgeEvidence(row) {
  const checked = !!knowledgeState().selected[row.id];
  return `<article class="knowledge-item"><div class="knowledge-item-body"><div class="row gap6 wrap mb8"><label class="row gap6 xs"><input type="checkbox" data-ev-select="${row.id}" ${checked ? 'checked' : ''} aria-label="Select this evidence"></label>${knowledgeBadges(row)}</div><h3>${esc(row.claim)}</h3>
    <p class="small muted">${esc([row.company, row.role].filter(Boolean).join(' · ') || 'No company or role added')}</p><div class="row gap6 wrap">${(row.skills || []).map(skill => pill(skill, 'plain')).join('')}</div>
    <div class="row gap6 wrap mt8 evidence-tags">${evidenceTagChips(row)}</div>${evidenceTagEditor(row)}</div>
    <div class="knowledge-actions">${!row.verified ? btn('Verify', 'ev-verify', { id: row.id }, 'sm soft', isBusy('knowledge')) : ''}${btn('Resumes', 'ev-tags', { id: row.id }, 'sm', isBusy('knowledge'))}${btn('Remove', 'ev-delete', { id: row.id }, 'sm ghost danger', isBusy('knowledge'))}</div></article>`;
}
function knowledgeList(section) {
  const all = knowledgeRows(section), rows = knowledgeFiltered(section);
  if (!all.length) return `<div class="knowledge-empty"><span class="knowledge-empty-icon" aria-hidden="true">${section === 'answers' ? '?' : '✓'}</span><h3>${section === 'answers' ? 'Your next application starts here' : 'Give your experience a place to shine'}</h3><p>${section === 'answers' ? 'Save answers to recurring questions about work authorization, notice period, and compensation.' : 'Add factual achievements and verify imported claims so tailoring can use them.'}</p>${btn(section === 'answers' ? 'Add your first answer' : 'Add your first evidence', 'kn-add', {}, 'primary')}${section === 'evidence' ? '<a class="knowledge-settings-link" href="#settings/resumes">Manage or re-parse your resume in Settings ↗</a>' : ''}</div>`;
  return `${section === 'evidence' ? evidenceBulkBar() : ''}<p class="knowledge-results" role="status">${rows.length} of ${all.length} ${section === 'answers' ? 'answers' : 'claims'}</p>${rows.length ? rows.map(section === 'answers' ? knowledgeAnswer : knowledgeEvidence).join('') : `<div class="knowledge-empty"><h3>No matches found</h3><p>Try another search or clear your filters.</p>${btn('Clear filters', 'kn-clear')}</div>`}`;
}
function knowledgeField(form, key, title, value, hint = '', multiline = false) {
  const extra = `${hint ? `aria-describedby="${form}-${key}-hint"` : ''}`;
  return `<div class="knowledge-field"><label for="${form}-${key}">${title}</label>${multiline ? textareaF(form, key, value, extra) : inputF(form, key, value, extra)}${hint ? `<p id="${form}-${key}-hint" class="xs muted">${esc(hint)}</p>` : ''}</div>`;
}
function knowledgeAnswerFields(editor) {
  const row = knowledgeRows('answers').find(a => a.id === editor.id) || {}, form = editor.form;
  return `${knowledgeField(form, 'question', 'Question', row.question, 'For example: Are you authorized to work in India?', true)}
    ${knowledgeField(form, 'answer', 'Your answer', row.answer, 'Write the answer you want to reuse in applications.', true)}
    <div class="knowledge-field-grid"><div class="knowledge-field"><label for="${form}-category">Category</label>${selectF(form, 'category', row.category || 'general', [...new Set([...KNOWLEDGE_CATEGORIES, row.category || 'general'])].map(c => [c, knowledgeLabel(c)]))}</div>
    <div class="knowledge-field"><label for="${form}-answer_type">Answer format</label>${selectF(form, 'answer_type', row.answer_type || 'text', [['text', 'Text'], ['boolean', 'Yes / No'], ['choice', 'Choice'], ['number', 'Number']])}</div></div>
    <div class="knowledge-verification">${checkF(form, 'verified', editor.id ? row.verified : true, 'Human verified — eligible for reuse')}${checkF(form, 'sensitive', row.sensitive, 'Sensitive — always ask me first')}</div>`;
}
function knowledgeEvidenceFields() {
  return `${knowledgeField('ev', 'claim', 'Achievement or factual claim', S.evidencePrefill || '', 'For example: Cut cloud spend by 35% in 2024.', true)}
    <div class="knowledge-field-grid">${knowledgeField('ev', 'company', 'Company', '')}${knowledgeField('ev', 'role', 'Role', '')}</div>
    ${knowledgeField('ev', 'skills', 'Skills', '', 'Separate skills with commas, for example: Python, AWS.')}
    ${evidenceResumes().length ? `<fieldset class="knowledge-field"><legend>Belongs to</legend>${evidenceResumes().map(r => checkF('ev', `resume_${r.id}`, evidenceDefaultResume(r), esc(evidenceResumeName(r)))).join('')}<p class="xs muted">Jobs only use evidence of the resume they are matched to. Leave all unchecked for evidence that applies to every resume.</p></fieldset>` : ''}
    <p class="knowledge-verification small">By saving, you confirm this claim is accurate. Verified evidence can be cited in tailored resumes.</p>`;
}
function knowledgeEditor(section) {
  const editor = knowledgeState().editors[section];
  if (!editor) return '';
  return `<section class="card knowledge-editor" aria-labelledby="knowledge-editor-heading"><div class="knowledge-editor-head"><span class="caps">${editor.id ? 'REFINE YOUR ANSWER' : 'BUILD YOUR LIBRARY'}</span><h3 id="knowledge-editor-heading">${section === 'evidence' ? 'Add evidence' : editor.id ? 'Edit answer' : 'Add an answer'}</h3></div>
    <form id="knowledge-editor-form"><fieldset ${isBusy('knowledge') ? 'disabled' : ''}>${section === 'answers' ? knowledgeAnswerFields(editor) : knowledgeEvidenceFields()}
    <div class="knowledge-editor-footer">${btn('Cancel', 'kn-cancel').replace('<button', '<button type="button"')}<button class="btn primary" type="submit">${label('knowledge', section === 'answers' ? 'Save answer' : 'Add verified evidence', 'Saving…')}</button></div></fieldset></form></section>`;
}
function renderKnowledge() {
  const section = knowledgeSection(S.knowledgeSection), rows = knowledgeRows(section), ui = knowledgeState();
  const verified = rows.filter(row => row.verified).length;
  return `<div class="knowledge-workspace"><nav class="knowledge-nav" aria-label="Knowledge bank sections">${KNOWLEDGE_SECTIONS.map(([id, title]) => `<a href="#knowledge/${id}" ${id === section ? 'aria-current="page"' : ''}>${title}<span>${knowledgeRows(id).length}</span></a>`).join('')}</nav>
    <div class="knowledge-header"><div><span class="caps">YOUR APPLICATION TOOLKIT</span><h2 id="knowledge-heading" tabindex="-1">${section === 'answers' ? 'Answer once. Apply with confidence.' : 'Real experience. Stronger applications.'}</h2><p>${section === 'answers' ? 'Keep your recurring application answers ready to review and reuse.' : 'Build a collection of achievements. Tailoring may only cite verified claims.'}</p></div>${btn(section === 'answers' ? '+ Add answer' : '+ Add evidence', 'kn-add', {}, 'primary', isBusy('knowledge') || D.knowledgeLoading || !!D.knowledgeLoadError)}</div>
    <div class="knowledge-summary"><span><strong>${rows.length}</strong> ${section === 'answers' ? 'saved answers' : 'evidence claims'}</span><span><i class="dot" style="background:var(--good)"></i><strong>${verified}</strong> verified</span><span><i class="dot" style="background:var(--warn)"></i><strong>${rows.length - verified}</strong> need review</span></div>
    ${D.knowledgeLoadError ? `<div class="knowledge-notice" role="alert">${esc(D.knowledgeLoadError)} ${btn('Retry loading', 'kn-retry', {}, 'sm')}</div>` : ''}
    ${ui.errors[section] ? `<div class="knowledge-notice" role="alert">${esc(ui.errors[section])}</div>` : ''}
    ${knowledgeEditor(section)}
    ${D.knowledgeLoading ? '<div class="card knowledge-empty" role="status">Loading your Knowledge bank…</div>' : D.knowledgeLoadError ? '' : !D.knowledge || !D.evidence ? '<div class="card knowledge-empty" role="status">Loading your Knowledge bank…</div>' : `<section class="card knowledge-library" aria-label="${section === 'answers' ? 'Saved answers' : 'Evidence claims'}">${knowledgeIdsBanner(section)}${knowledgeToolbar(section)}${knowledgeList(section)}</section>`}</div>`;
}
function knowledgeOpen(id) {
  const section = S.knowledgeSection || 'answers', ui = knowledgeState();
  ui.editors[section] = { id: id || null, form: section === 'evidence' ? 'ev' : 'kn' + (id || 'new') };
  delete ui.errors[section]; render();
  document.getElementById(`${ui.editors[section].form}-${section === 'answers' ? 'question' : 'claim'}`)?.focus();
}
function knowledgeCancel() {
  const section = S.knowledgeSection || 'answers', ui = knowledgeState(), editor = ui.editors[section];
  if (editor) delete S.forms[editor.form];
  delete ui.editors[section]; delete ui.errors[section];
  if (section === 'evidence') S.evidencePrefill = '';
  render(); document.querySelector('[data-act="kn-add"]')?.focus();
}
async function knowledgeRun(section, fn, message) {
  if (isBusy('knowledge')) return;
  S.busy.knowledge = true; delete knowledgeState().errors[section]; render();
  let succeeded = false;
  try { await fn(); succeeded = true; toast(message); }
  catch (error) { knowledgeState().errors[section] = error.message || 'Something went wrong. Please try again.'; }
  finally { delete S.busy.knowledge; render(); }
  if (S.screen === 'knowledge' && knowledgeSection(S.knowledgeSection) === section) {
    if (succeeded) document.querySelector('[data-act="kn-add"]')?.focus();
    else document.querySelector('[role="alert"]')?.setAttribute('tabindex', '-1');
    if (!succeeded) document.querySelector('[role="alert"]')?.focus();
  }
}
function knowledgeAnswerPayload(editor) {
  const row = knowledgeRows('answers').find(a => a.id === editor.id) || {}, f = editor.form;
  const body = Object.fromEntries(['question', 'answer', 'category', 'answer_type'].map(key => [key, fv(f, key, row[key] || ({ category: 'general', answer_type: 'text' }[key] || ''))]));
  body.question = body.question.trim();
  if (body.question.length < 2) throw new Error('Write a question with at least 2 characters.');
  return { ...body, verified: fv(f, 'verified', editor.id ? !!row.verified : true), sensitive: fv(f, 'sensitive', !!row.sensitive) };
}
function evidenceDefaultResume(r) { return !!r.is_active; }
function knowledgeEvidencePayload() {
  const f = S.forms.ev || {}, claim = (f.claim ?? S.evidencePrefill ?? '').trim();
  if (!claim) throw new Error('Describe the factual claim first.');
  const resumeIds = evidenceResumes().filter(r => fv('ev', `resume_${r.id}`, evidenceDefaultResume(r))).map(r => r.id);
  return { claim, company: f.company || '', role: f.role || '', skills: (f.skills || '').split(',').map(s => s.trim()).filter(Boolean), verified: true, resume_ids: resumeIds };
}
async function knowledgeSave() {
  const section = S.knowledgeSection || 'answers', editor = knowledgeState().editors[section];
  if (!editor) return;
  return knowledgeRun(section, async () => {
    const body = section === 'answers' ? knowledgeAnswerPayload(editor) : knowledgeEvidencePayload();
    const url = section === 'answers' ? '/api/knowledge' + (editor.id ? '/' + editor.id : '') : '/api/evidence';
    await api(url, { method: editor.id ? 'PUT' : 'POST', body });
    delete S.forms[editor.form]; delete knowledgeState().editors[section];
    if (section === 'evidence') S.evidencePrefill = '';
    await loadKnowledge();
  }, section === 'answers' ? 'Answer saved' : 'Evidence added');
}
async function knowledgeDelete(section, id) {
  if (!confirm(section === 'answers' ? 'Permanently delete this answer? It will no longer be offered in apply kits. This cannot be undone.' : 'Permanently remove this evidence claim? This cannot be undone.')) return;
  return knowledgeRun(section, async () => {
    await api(`${section === 'answers' ? '/api/knowledge' : '/api/evidence'}/${id}`, { method: 'DELETE' });
    delete S.forms['kn' + id];
    if (knowledgeState().editors[section]?.id === Number(id)) delete knowledgeState().editors[section];
    await loadKnowledge();
  }, section === 'answers' ? 'Answer deleted' : 'Evidence removed');
}
async function knowledgeCopy(id) {
  const row = knowledgeRows('answers').find(a => a.id === Number(id));
  if (!row?.answer) return;
  return knowledgeRun('answers', async () => {
    try { await navigator.clipboard.writeText(row.answer); }
    catch {
      const field = document.createElement('textarea'); field.value = row.answer; document.body.appendChild(field); field.select();
      try { if (!document.execCommand('copy')) throw new Error('Could not copy. Select and copy the answer manually.'); }
      finally { field.remove(); }
    }
    try { await api(`/api/knowledge/${id}/used`, { method: 'POST' }); row.use_count = (row.use_count || 0) + 1; }
    catch { knowledgeState().errors.answers = 'Answer copied, but its usage count could not be updated.'; }
  }, 'Answer copied');
}
const KNOWLEDGE_ACTIONS = {
  'kn-clear-ids': () => { delete knowledgeState().filters.answers.ids; render(); },
  'kn-add': () => knowledgeOpen(),
  'kn-edit': el => knowledgeOpen(Number(el.dataset.id)),
  'kn-cancel': knowledgeCancel,
  'kn-save': knowledgeSave,
  'kn-copy': el => knowledgeCopy(el.dataset.id),
  'kn-delete': el => knowledgeDelete('answers', el.dataset.id),
  'ev-add': knowledgeSave,
  'ev-delete': el => knowledgeDelete('evidence', el.dataset.id),
  'ev-verify': el => knowledgeRun('evidence', async () => { await api(`/api/evidence/${el.dataset.id}`, { method: 'PATCH', body: { verified: true } }); await loadKnowledge(); }, 'Verified — tailoring may now cite it'),
  'ev-tags': el => { const row = knowledgeRows('evidence').find(e => e.id === Number(el.dataset.id)); knowledgeState().tagging = row ? { id: row.id, selected: (row.resumes || []).map(t => t.id) } : null; render(); },
  'ev-tags-cancel': () => { knowledgeState().tagging = null; render(); },
  'ev-tags-save': el => evidenceTagsSave(Number(el.dataset.id)),
  'ev-bulk': el => evidenceBulk(el.dataset.mode),
  'ev-bulk-clear': () => { knowledgeState().selected = {}; render(); },
  'kn-retry': async () => { await loadKnowledge(); render(); },
  'kn-clear': () => { const f = knowledgeState().filters[S.knowledgeSection || 'answers']; Object.keys(f).forEach(key => { f[key] = key === 'q' ? '' : 'all'; }); render(); document.getElementById('knowledge-search')?.focus(); },
};
