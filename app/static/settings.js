/* Focused settings editors. Loaded before app.js; functions use the workspace's shared helpers. */
'use strict';
const SETTINGS_SECTIONS = [
  ['profile', 'Profile', 'Your professional identity', '01'],
  ['preferences', 'Job preferences', 'What your next role looks like', '02'],
  ['resumes', 'Resumes', 'The experience behind your applications', '03'],
  ['resume-data', 'Resume Data', 'Review and verify the facts AI may use', '04'],
  ['account', 'Account & security', 'Manage access to your workspace', '05'],
  ['api-keys', 'API keys', 'Connect your own providers', '06'],
  ['ai', 'AI models', 'Choose how your assistant works', '07'],
  ['workflow', 'Workflow & cadence', 'Set a pace that works for you', '08'],
];
const SETTINGS_FORMS = ['profile', 'preferences', 'resumes', 'resume-data', 'account', 'keys', 'ai', 'workflow'];
const API_KEYS = [['openrouter', 'OpenRouter', 'AI model marketplace'], ['openai', 'OpenAI', 'GPT models'], ['anthropic', 'Anthropic', 'Claude models'], ['gemini', 'Gemini', 'Gemini models (Google AI Studio key)'], ['apify', 'Apify', 'Job discovery']];
const CHIP_FIELDS = [
  ['target_roles', 'Target roles', 'Choose up to 5 roles', 'Add a role…', '', 'top'],
  ['preferred_locations', 'Target locations', 'Cities, regions or Remote', 'Add a location…', '', 'top'],
  ['countries', 'Countries', 'Where you would consider working', 'Add a country…', '', 'sp'],
  ['primary_skills', 'Primary skills', 'Must-have matches', 'Add a skill…', '', 'sp'],
  ['secondary_skills', 'Secondary skills', 'Nice-to-have matches', 'Add a skill…', '', 'sp'],
  ['industries', 'Industries', 'Preferred domains', 'Add an industry…', '', 'sp'],
  ['exclude_companies', 'Excluded companies', 'Keep these out of your results', 'Add a company…', '', 'sp'],
  ['languages', 'Languages', 'Used in application answers', 'Add a language…', '', 'sp'],
];
const PROFILE_FIELDS = ['name', 'headline', 'email', 'phone', 'location', 'linkedin_url', 'github_url'];
const PREFERENCE_TOP = ['target_roles', 'preferred_locations', 'work_authorization', 'compensation_notes'];
const PREFERENCE_NESTED = ['total_experience', 'notice_period', 'open_to_relocation', 'open_to_hybrid', 'open_to_contract', ...CHIP_FIELDS.filter(c => c[5] === 'sp').map(c => c[0])];
const SETTING_ERRORS = {};
const SETTINGS_SUCCESS = {};
const SETTING_DETAILS = new Set();
let editingKey = null;
let authorizationDraft = null;
const AUTH_COUNTRIES = ['India', 'Singapore', 'Dubai (UAE)', 'United States', 'United Kingdom', 'Canada', 'Australia', 'Other'];
const AUTH_STATUSES = ['Citizen', 'Permanent resident', 'EP (Employment Pass)', 'Work permit', 'Foreigner / visa required', 'Other'];

function authorizationRows(value) {
  const lines = String(value || '').split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const notes = lines.filter(line => /^notes?:/i.test(line)).map(line => line.replace(/^notes?:\s*/i, '')).join('\n');
  const rows = lines.filter(line => !/^notes?:/i.test(line)).map(line => {
    const match = line.match(/^(.+?)\s*(?:—|-|,)\s*(.+)$/);
    if (match) return { country: match[1].trim(), status: match[2].trim() };
    return AUTH_STATUSES.includes(line) ? { country: '', status: line } : { country: line, status: '' };
  });
  return { rows: rows.length ? rows : [{ country: '', status: '' }], notes };
}
function authorizationValue() {
  const rows = [...document.querySelectorAll('[data-auth-row]')].map(row => ({
    country: row.querySelector('[data-auth-country]')?.value.trim() || '',
    status: row.querySelector('[data-auth-status]')?.value.trim() || '',
  })).filter(row => row.country || row.status);
  const notes = document.getElementById('preferences-authorization-notes')?.value.trim() || '';
  return [...rows.map(row => [row.country || 'Other', row.status || 'Other'].join(' — ')), notes ? `Notes: ${notes}` : ''].filter(Boolean).join('\n');
}
function settingsAuthUpdate() {
  const rows = [...document.querySelectorAll('[data-auth-row]')].map(row => ({
    country: row.querySelector('[data-auth-country]')?.value.trim() || '',
    status: row.querySelector('[data-auth-status]')?.value.trim() || '',
  }));
  const notes = document.getElementById('preferences-authorization-notes')?.value.trim() || '';
  authorizationDraft = { rows, notes };
  setF('preferences', 'work_authorization', authorizationValue());
}
function authorizationOptions(options, selected) {
  return options.map(option => `<option value="${attr(option)}" ${option === selected ? 'selected' : ''}>${esc(option)}</option>`).join('');
}
function authorizationEditor(value) {
  const parsed = authorizationRows(value), { rows, notes } = authorizationDraft || parsed;
  return `<div class="authorization-editor"><p class="settings-help">Add one entry for each country or market. This helps match roles to your actual right to work.</p><div class="authorization-rows">${rows.map((row, index) => `<div class="authorization-row" data-auth-row><label class="field" for="preferences-auth-country-${index}"><span>Country / market</span><select class="select" id="preferences-auth-country-${index}" data-auth-country data-auth-entry="1" data-auth-index="${index}"><option value="">Select a country</option>${authorizationOptions(AUTH_COUNTRIES, row.country)}</select></label><label class="field" for="preferences-auth-status-${index}"><span>Authorization</span><select class="select" id="preferences-auth-status-${index}" data-auth-status data-auth-entry="1" data-auth-index="${index}"><option value="">Select status</option>${authorizationOptions(AUTH_STATUSES, row.status)}</select></label>${rows.length > 1 ? btn('Remove', 'auth-remove', { index }, 'sm') : ''}</div>`).join('')}</div>${btn('+ Add country', 'auth-add', {}, 'sm')}<label class="field authorization-notes" for="preferences-authorization-notes"><span>Visa notes (optional)</span><textarea class="textarea" id="preferences-authorization-notes" data-auth-notes placeholder="e.g. Need sponsorship after EP expiry">${esc(notes)}</textarea></label></div>`;
}

function settingsSection(value) { return SETTINGS_SECTIONS.some(([id]) => id === value) ? value : 'profile'; }
function settingsForm() { return S.settingsSection === 'api-keys' ? 'keys' : S.settingsSection; }
function settingsBaseline(form) {
  const p = D.profile || {}, sp = p.search_preferences || {}, st = D.settings || {}, ai = st.ai?.saved || {};
  if (form === 'profile') return Object.fromEntries(PROFILE_FIELDS.map(k => [k, p[k] ?? '']));
  if (form === 'resumes') return { master_resume: p.master_resume || '' };
  if (form === 'preferences') return { ...Object.fromEntries(PREFERENCE_TOP.map(k => [k, p[k] ?? (k === 'target_roles' || k === 'preferred_locations' ? [] : '')])), ...Object.fromEntries(PREFERENCE_NESTED.map(k => [k, sp[k] ?? (k.startsWith('open_') ? false : ['total_experience', 'notice_period'].includes(k) ? '' : [])])) };
  if (form === 'ai') return { llm_provider: ai.llm_provider || '', model: ai.model || '', require_free: ai.require_free == null ? '' : String(ai.require_free), ...Object.fromEntries(['rank', 'tailor', 'review', 'followup'].map(k => [`stage_${k}`, ai.stage_models?.[k] || ''])) };
  if (form === 'workflow') return { application_mode: st.application_mode, min_review_score: st.min_review_score, execution_mode: st.execution_mode || 'backend', weekly_target: D.prefs?.weekly_target, followup_days: D.prefs?.followup_days };
  return {};
}
function sameSetting(a, b) {
  if (Array.isArray(a) || Array.isArray(b)) return JSON.stringify(a) === JSON.stringify(b);
  return String(a ?? '') === String(b ?? '');
}
function settingsChanges(form) {
  const baseline = settingsBaseline(form);
  return Object.fromEntries(Object.entries(S.forms[form] || {}).filter(([k, v]) => !sameSetting(v, baseline[k])));
}
function settingsPendingTags(form) {
  return [...document.querySelectorAll(`input[data-tag][data-form="${form}"]`)].filter(el => el.value.trim());
}
function settingsDirty(form) {
  if (form === 'resume-data') return resumeDataDirty();
  return Object.keys(settingsChanges(form)).length > 0 || settingsPendingTags(form).length > 0;
}
function settingsAnyDirty() { return SETTINGS_FORMS.some(settingsDirty); }
function settingsSaving() { return SETTINGS_FORMS.some(f => isBusy(`settings-${f}`)); }
function settingsPayload(form) {
  const changes = settingsChanges(form);
  if (form === 'preferences') {
    const search_preferences = Object.fromEntries(Object.entries(changes).filter(([k]) => PREFERENCE_NESTED.includes(k)));
    return { ...Object.fromEntries(Object.entries(changes).filter(([k]) => PREFERENCE_TOP.includes(k))), ...(Object.keys(search_preferences).length ? { search_preferences } : {}) };
  }
  if (form === 'ai') {
    const body = {}, stages = {};
    for (const [k, v] of Object.entries(changes)) {
      if (k.startsWith('stage_')) stages[k.slice(6)] = v.trim();
      else body[k] = k === 'require_free' ? (v === '' ? null : v === 'true') : v.trim();
    }
    return { ...body, ...(Object.keys(stages).length ? { stage_models: stages } : {}) };
  }
  return changes;
}
function profileCompleteness() {
  const p = { ...D.profile, ...S.forms.profile }, sp = { ...D.profile?.search_preferences, ...S.forms.preferences };
  const values = [p.name, p.headline, p.email, p.phone, p.location, sp.total_experience, sp.notice_period,
    fv('preferences', 'compensation_notes', p.compensation_notes), fv('preferences', 'target_roles', p.target_roles), sp.primary_skills,
    fv('preferences', 'work_authorization', p.work_authorization), fv('resumes', 'master_resume', p.master_resume)];
  return Math.round(100 * values.filter(v => Array.isArray(v) ? v.length : v !== null && v !== undefined && String(v).trim() !== '').length / values.length);
}
function settingsClearSecrets() {
  delete S.forms.keys; delete S.forms.account; editingKey = null;
  delete SETTING_ERRORS.keys; delete SETTING_ERRORS.account;
}
function settingsCanLeave(nextScreen, nextSection) {
  if (S.screen !== 'settings') return true;
  if (settingsSaving()) { toast('Wait for this change to finish.'); return false; }
  if (nextScreen !== 'settings' && settingsAnyDirty()) {
    if (!confirm('Leave Settings and discard unsaved changes?')) return false;
    SETTINGS_FORMS.forEach(f => { delete S.forms[f]; delete SETTING_ERRORS[f]; });
    authorizationDraft = null; rdReset();
  }
  if (nextScreen !== 'settings' || nextSection !== S.settingsSection) settingsClearSecrets();
  return true;
}
function settingsNavigate(section) {
  const next = settingsSection(section);
  if (!settingsCanLeave('settings', next)) { render(); return; }
  S.settingsSection = next;
  history.pushState(null, '', `#settings/${next}`);
  render(); document.getElementById('settings-heading')?.focus();
}
function settingsTouched(form) {
  if (S.screen !== 'settings' || !SETTINGS_FORMS.includes(form)) return;
  delete SETTINGS_SUCCESS[form];
  document.querySelectorAll('[data-settings-save]').forEach(el => { el.disabled = !settingsDirty(el.dataset.settingsSave) || isBusy(`settings-${el.dataset.settingsSave}`); });
  document.querySelectorAll('[data-settings-discard]').forEach(el => { el.disabled = !settingsDirty(el.dataset.settingsDiscard); });
  const status = document.getElementById('settings-save-status');
  if (status) status.textContent = settingsDirty(form) ? 'Unsaved changes' : 'All changes saved';
}
function settingsField(form, key, labelText, extra = '', help = '') {
  return `<label class="field" for="${form}-${key}"><span>${labelText}</span>${inputF(form, key, settingsBaseline(form)[key], extra)}${help ? `<span class="settings-help">${help}</span>` : ''}</label>`;
}
function settingsGroup(title, description, content) {
  return `<section class="settings-group"><div class="settings-group-title"><h3>${title}</h3>${description ? `<p>${description}</p>` : ''}</div>${content}</section>`;
}
function settingsDetails(id, title, body) {
  return `<details class="settings-details" data-settings-details="${id}" ${SETTING_DETAILS.has(id) ? 'open' : ''}><summary>${title}<span aria-hidden="true">+</span></summary><div class="settings-details-body">${body}</div></details>`;
}
function settingsFooter(form, text = 'Save changes') {
  return `<footer class="settings-footer"><span id="settings-save-status" role="status">${isBusy(`settings-${form}`) ? 'Saving…' : SETTINGS_SUCCESS[form] || (settingsDirty(form) ? 'Unsaved changes' : 'All changes saved')}</span><div class="row gap8"><button class="btn" data-act="settings-discard" data-form="${form}" data-settings-discard="${form}" ${settingsDirty(form) ? '' : 'disabled'}>Discard</button><button class="btn primary" data-act="settings-save" data-form="${form}" data-settings-save="${form}" ${settingsDirty(form) && !isBusy(`settings-${form}`) ? '' : 'disabled'}>${isBusy(`settings-${form}`) ? 'Saving…' : text}</button></div></footer>`;
}
function renderProfileSettings() {
  const p = D.profile;
  return `<div class="settings-identity"><div class="settings-avatar" aria-hidden="true">${esc(initials(p.name))}</div><div class="grow"><strong>${esc(p.name || 'Make it yours')}</strong><p>${esc(p.headline || 'Add your professional details to get started.')}</p></div><span class="settings-complete">${profileCompleteness()}% complete</span></div>` +
    settingsGroup('Personal details', 'Help employers put a name to your experience.', `<div class="settings-grid">${settingsField('profile', 'name', 'Full name', 'autocomplete="name" maxlength="200"')}${settingsField('profile', 'headline', 'Professional headline', 'maxlength="500"')}${settingsField('profile', 'email', 'Contact email', 'type="email" autocomplete="email"', 'For applications. This does not change your login email.')}${settingsField('profile', 'phone', 'Phone', 'type="tel" autocomplete="tel"')}${settingsField('profile', 'location', 'Current location', 'autocomplete="address-level2"')}</div>`) +
    settingsGroup('Around the web', 'Give your experience a little more context.', `<div class="settings-grid">${settingsField('profile', 'linkedin_url', 'LinkedIn', 'type="url" placeholder="https://linkedin.com/in/…"')}${settingsField('profile', 'github_url', 'GitHub', 'type="url" placeholder="https://github.com/…"')}</div>`) + settingsFooter('profile');
}
function settingsChips(keys) {
  return `<div class="settings-grid">${CHIP_FIELDS.filter(([k]) => keys.includes(k)).map(([k, title, hint, placeholder]) => `<div class="settings-chip-field"><label for="preferences-${k}">${title}</label><p id="help-${k}" class="settings-help">${hint}</p>${tagBox('preferences', k, settingsBaseline('preferences')[k], placeholder, k === 'target_roles' ? 5 : 25)}</div>`).join('')}</div>`;
}
function renderPreferenceSettings() {
  const sp = settingsBaseline('preferences');
  return settingsGroup('Your next role', 'These preferences guide discovery and ranking.', settingsChips(['target_roles', 'preferred_locations', 'countries'])) +
    settingsGroup('Skills & interests', '', settingsChips(['primary_skills', 'secondary_skills', 'industries', 'exclude_companies', 'languages'])) +
    settingsGroup('Availability & expectations', '', `<div class="settings-grid">${settingsField('preferences', 'total_experience', 'Experience (years)', 'type="number" min="0" step="0.5"')}${settingsField('preferences', 'notice_period', 'Notice period (days)', 'type="number" min="0" step="1"', 'Use 0 if you are available immediately.')}${settingsField('preferences', 'compensation_notes', 'Expected compensation', 'placeholder="e.g. INR 75 lakh / year"', 'Include currency and pay period.')}</div>`) +
    settingsGroup('Work arrangements', '', `<div class="field"><span>Work authorization by country</span>${authorizationEditor(sp.work_authorization)}</div><div class="settings-checks">${checkF('preferences', 'open_to_relocation', sp.open_to_relocation, 'Open to relocation')}${checkF('preferences', 'open_to_hybrid', sp.open_to_hybrid, 'Open to hybrid')}${checkF('preferences', 'open_to_contract', sp.open_to_contract, 'Open to contract')}</div>`) + settingsFooter('preferences');
}
function renderResumeSettings() {
  const docs = D.resumes || [], stale = docs.find(d => d.active && !d.in_use);
  return settingsGroup('Resume library', 'Your primary resume is the starting point for tailored applications.',
    `${stale ? `<div class="settings-notice"><strong>Your edited text is being used for tailoring</strong><p>It differs from ${esc(stale.filename)}. Keep your edits or restore the file’s text.</p>${btn('Use file text', 'resume-use', { id: stale.id }, 'sm')}</div>` : ''}
    <div class="settings-resume-list">${docs.map(d => `<article class="settings-resume"><div class="settings-file-icon" aria-hidden="true">${esc(d.filename.split('.').pop().toUpperCase().slice(0, 4))}</div><div class="grow"><strong>${esc(d.filename)}</strong><p>${Math.max(1, Math.round(d.size / 1024))} KB · ${d.words} words${d.active ? ' · Primary' : ''}${d.in_use ? ' · Used for tailoring' : ''}</p></div><div class="row gap8 wrap">${d.active ? '<a class="btn sm" href="/api/profile/resume">Download</a>' : btn('Set primary', 'resume-primary', { id: d.id }, 'sm')}${btn('Delete', 'resume-delete', { id: d.id, name: d.filename }, 'sm')}</div></article>`).join('') || '<div class="settings-empty"><strong>Your experience starts here</strong><p>Upload a resume to make your applications more personal.</p></div>'}</div>
    <label class="settings-upload" for="resume-file"><strong>＋ Upload ${docs.length ? 'another resume' : 'your resume'}</strong><span>PDF, DOCX or TXT · up to ${BOOT.max_upload_mb} MB</span><input type="file" id="resume-file" accept=".txt,.pdf,.docx" aria-label="Upload resume"></label>`) +
    settingsDetails('resume-text', 'Edit master resume text', `<p class="settings-help">Saving new text creates a new resume version. To correct parsed facts, edit them in <a href="#settings/resume-data">Resume Data</a> instead.</p><label class="field" for="resumes-master_resume">Master resume text${textareaF('resumes', 'master_resume', D.profile.master_resume)}</label>${settingsDetails('resume-format', 'Formatting tips', '<p>Use Summary, Skills, and Experience headings, a role heading for each position, and short bullet points. Include title, company, location and dates for each role.</p>')}`) +
    settingsGroup('Evidence for your applications', 'Extract claims from your saved resume, then verify them in the Knowledge bank.', btn('Re-parse resume into evidence', 'parse-evidence', {}, '', isBusy('settings-resumes'))) + settingsFooter('resumes', 'Save resume text');
}
function renderAccountSettings() {
  return settingsGroup('Signed-in account', 'Your login is separate from the contact email on your profile.', `<div class="settings-account-row"><div><span class="settings-help">Login email</span><strong>${esc(BOOT.user.email)}</strong></div>${btn('Sign out', 'logout', {}, '')}</div>`) +
    settingsGroup('Change password', 'You’ll stay signed in here. Your other sessions will be signed out.', `<div class="settings-password-fields">${settingsField('account', 'current_password', 'Current password', 'type="password" autocomplete="current-password" maxlength="256" required')}${settingsField('account', 'new_password', 'New password', 'type="password" autocomplete="new-password" minlength="10" maxlength="256" required', 'Use 10–256 characters. Choose a password you do not use elsewhere.')}${settingsField('account', 'confirm_password', 'Confirm new password', 'type="password" autocomplete="new-password" maxlength="256" required')}</div>`) + settingsFooter('account', 'Update password');
}
function renderKeySettings() {
  const st = D.settings;
  return `<p class="settings-intro">Keys are encrypted and used only for your workspace. Saved keys are never shown again.</p>${st.secret_storage ? '' : '<div class="settings-notice">Key storage is unavailable. Ask your server administrator to enable encrypted key storage.</div>'}<div class="settings-providers">${API_KEYS.map(([id, name, purpose]) => {
    const key = st.keys[id] || { configured: false, hint: '' };
    return `<section class="settings-provider"><div class="settings-provider-row"><div class="settings-provider-mark" aria-hidden="true">${name.slice(0, 1)}</div><div class="grow"><h3>${name}</h3><p>${purpose}</p></div><span class="settings-key-status ${key.configured ? 'saved' : ''}">${key.configured ? `Saved ${esc(key.hint)}` : 'Not configured'}</span>${btn(editingKey === id ? 'Cancel' : key.configured ? 'Replace' : 'Add key', 'settings-key-edit', { id }, 'sm', !st.secret_storage)}</div>${editingKey === id ? `<div class="settings-key-editor"><label class="field" for="keys-${id}">${name} API key${inputF('keys', id, '', 'type="password" autocomplete="off" spellcheck="false" maxlength="500" required')}</label><p class="settings-help">Saving replaces only this provider’s key.</p><div class="row gap8">${btn('Save key', 'settings-save', { form: 'keys', id }, 'primary', isBusy('settings-keys'))}${key.configured ? btn('Remove saved key', 'key-remove', { id }, '') : ''}</div></div>` : ''}</section>`;
  }).join('')}</div>`;
}
function renderAISettings() {
  const ai = D.settings.ai, base = settingsBaseline('ai'), provider = fv('ai', 'llm_provider', base.llm_provider) || ai.env.llm_provider;
  const defaultModel = String(fv('ai', 'model', base.model)).trim() || ai.env.models[provider] || '';
  const stages = ['rank', 'tailor', 'review', 'followup'].map(k => {
    const fallback = provider === 'mock' ? 'Mock (no API calls)' : (provider === 'openrouter' && !String(fv('ai', 'model', base.model)).trim() && ai.env.stage_models[k]) || defaultModel;
    return settingsField('ai', `stage_${k}`, { rank: 'Ranking', tailor: 'Tailoring', review: 'Review', followup: 'Follow-up' }[k], `placeholder="${attr(fallback)}" ${provider === 'mock' ? 'disabled' : ''}`, `Default: ${esc(fallback || 'Not set')}`);
  }).join('');
  return settingsGroup('Model defaults', 'Leave an override blank to use the server default.', `<div class="settings-grid"><label class="field" for="ai-llm_provider">Provider${selectF('ai', 'llm_provider', base.llm_provider, [['', `Server default (${ai.env.llm_provider})`], ['openrouter', 'OpenRouter'], ['openai', 'OpenAI'], ['anthropic', 'Anthropic'], ['gemini', 'Gemini'], ['mock', 'Mock / testing']], 'data-live="1"')}</label>${settingsField('ai', 'model', 'Default model', `placeholder="${attr(ai.env.models[provider] || '')}" data-live="1"`)}</div><p class="settings-help">${provider === 'mock' ? 'Mock mode makes no provider calls.' : `${esc(provider)}: ${D.settings.configured[provider] ? 'API key saved' : 'API key needed'}.`} <a href="#settings/api-keys">Manage API keys</a></p>`) +
    settingsDetails('ai-stages', 'Advanced · models per stage', `<div class="settings-grid">${stages}</div>`) +
    settingsGroup('Cost control', 'The free-only guard applies to OpenRouter.', `<label class="field" for="ai-require_free">Free-only guard${selectF('ai', 'require_free', base.require_free, [['', `Server default (${ai.env.require_free ? 'on' : 'off'})`], ['true', 'On — free models only'], ['false', 'Off — allow paid models']])}</label>`) + settingsFooter('ai');
}
function renderWorkflowSettings() {
  const b = settingsBaseline('workflow'), mode = fv('workflow', 'application_mode', b.application_mode), execution = fv('workflow', 'execution_mode', b.execution_mode);
  return settingsGroup('Approval preferences', 'You stay in control of every application.', `<div class="settings-options">${[['manual_approval', 'Review every packet', 'Approve each tailored packet before it reaches your apply kit.'], ['automatic', 'Auto-approve reviewed packets', 'Packets that meet your minimum score can skip the approval queue.']].map(([id, title, desc]) => `<label class="settings-option ${mode === id ? 'selected' : ''}"><input type="radio" name="settings-mode" data-form="workflow" data-field="application_mode" value="${id}" ${mode === id ? 'checked' : ''}><span><strong>${title}</strong><small>${desc}</small></span></label>`).join('')}</div><p class="settings-help">Submitting applications always stays your action.</p>${settingsField('workflow', 'min_review_score', 'Minimum reviewer score', 'type="number" min="0" max="100" required')}`) +
    settingsGroup('Tailoring & review', 'Choose who does the tailoring and review reasoning.', `<div class="settings-options">${[['backend', 'Job Hunt OS workers', 'Your configured model API tailors and reviews in the background.'], ['claude_desktop', 'Claude Desktop', 'You tailor and review in bulk from Claude Desktop over MCP; the workers make no model calls for it.']].map(([id, title, desc]) => `<label class="settings-option ${execution === id ? 'selected' : ''}"><input type="radio" name="settings-execution" data-form="workflow" data-field="execution_mode" value="${id}" ${execution === id ? 'checked' : ''}><span><strong>${title}</strong><small>${desc}</small></span></label>`).join('')}</div><p class="settings-help">Every saved result is validated by Job Hunt OS in both modes. See the README section "Claude Desktop + MCP" to connect.</p>`) +
    settingsGroup('Your rhythm', 'Set realistic targets and a helpful follow-up cadence.', `<div class="settings-grid">${settingsField('workflow', 'weekly_target', 'Weekly application target', 'type="number" min="1" max="50" required')}${settingsField('workflow', 'followup_days', 'Follow-up after (days)', 'type="number" min="1" max="60" required')}</div><p class="settings-help">Follow-up drafts are never sent automatically.</p>`) + settingsFooter('workflow');
}
const SETTINGS_RENDERERS = { profile: renderProfileSettings, preferences: renderPreferenceSettings, resumes: renderResumeSettings, 'resume-data': () => renderResumeDataSettings(), account: renderAccountSettings, 'api-keys': renderKeySettings, ai: renderAISettings, workflow: renderWorkflowSettings };
function renderSettings() {
  if (D.settingsLoadError) return `<div class="settings-content"><div class="settings-error" role="alert">Settings could not be loaded: ${esc(D.settingsLoadError)}</div>${btn('Try again', 'settings-retry', {}, 'mt16')}</div>`;
  if (!D.profile || !D.settings || !D.prefs) return loading();
  const section = settingsSection(S.settingsSection), form = section === 'api-keys' ? 'keys' : section;
  const [, title, description] = SETTINGS_SECTIONS.find(([id]) => id === section);
  return `<div class="settings-shell"><nav class="settings-nav" aria-label="Settings sections"><div class="settings-nav-caption">YOUR WORKSPACE</div>${SETTINGS_SECTIONS.map(([id, name, , number]) => `<a href="#settings/${id}" ${id === section ? 'aria-current="page"' : ''}><span class="settings-nav-number" aria-hidden="true">${number}</span>${name}</a>`).join('')}<p>Fine-tune your next chapter.</p></nav><div class="settings-content"><label class="settings-mobile-nav" for="settings-section">Settings section<select class="select" id="settings-section" data-act-change="settings-section">${SETTINGS_SECTIONS.map(([id, name]) => `<option value="${id}" ${id === section ? 'selected' : ''}>${name}</option>`).join('')}</select></label><header class="settings-section-heading"><span class="settings-eyebrow">SETTINGS / ${esc(title.toUpperCase())}</span><h2 id="settings-heading" tabindex="-1">${title}</h2><p>${description}</p></header><form id="settings-editor" novalidate><fieldset ${isBusy(`settings-${form}`) ? 'disabled' : ''}><div id="settings-error" class="settings-error" role="alert" ${SETTING_ERRORS[form] ? '' : 'hidden'}>${esc(SETTING_ERRORS[form] || '')}</div>${SETTINGS_RENDERERS[section]()}</fieldset></form></div></div>`;
}
function settingsValidate(form) {
  if (!document.getElementById('settings-editor')?.reportValidity()) return false;
  if (form === 'account') {
    const p = S.forms.account || {};
    if (p.new_password !== p.confirm_password) throw new Error('The new passwords do not match.');
    if (p.current_password === p.new_password) throw new Error('Choose a different new password.');
  }
  return true;
}
async function settingsRun(form, action, success) {
  const key = `settings-${form}`;
  if (isBusy(key)) return;
  S.busy[key] = true; delete SETTING_ERRORS[form]; delete SETTINGS_SUCCESS[form]; render();
  try { await action(); if (success) { SETTINGS_SUCCESS[form] = success; toast(success); } }
  catch (err) { SETTING_ERRORS[form] = err.message; }
  finally { delete S.busy[key]; render(); if (SETTING_ERRORS[form]) document.getElementById('settings-error')?.scrollIntoView?.({ block: 'nearest' }); }
}
async function refreshSettingsSummary(form) {
  try { D.summary = await api('/api/workspace/summary'); }
  catch (err) { SETTING_ERRORS[form] = `Changes saved, but the sidebar could not refresh: ${err.message}. Reload to refresh it.`; }
}
async function settingsSave(form) {
  if (form === 'resume-data') return resumeDataSave();
  settingsPendingTags(form).forEach(el => commitTag(el, false));
  try { if (!settingsValidate(form)) return; }
  catch (err) { SETTING_ERRORS[form] = err.message; render(); return; }
  if (form === 'workflow' && fv(form, 'application_mode', D.settings.application_mode) === 'automatic' && D.settings.application_mode !== 'automatic' && !confirm('Auto-approval skips the manual queue once a packet passes review. Submitting stays your action. Continue?')) return;
  await settingsRun(form, async () => {
    if (['profile', 'preferences', 'resumes'].includes(form)) D.profile = await api('/api/profile', { method: 'PATCH', body: settingsPayload(form) });
    if (form === 'ai') D.settings = await api('/api/settings', { method: 'PATCH', body: settingsPayload(form) });
    if (form === 'keys') {
      const value = String(S.forms.keys?.[editingKey] || '').trim();
      if (!value) throw new Error('Enter a key before saving.');
      D.settings = await api('/api/settings/keys', { method: 'PUT', body: { [editingKey]: value } }); editingKey = null;
    }
    if (form === 'account') await api('/api/auth/password', { method: 'POST', body: S.forms.account });
    if (form === 'workflow') await saveWorkflowSettings();
    delete S.forms[form];
    if (form === 'preferences') authorizationDraft = null;
    if (form === 'resumes') { D.resumes = await api('/api/profile/resumes'); D.resumeData = undefined; }
    if (['profile', 'preferences', 'ai'].includes(form)) await refreshSettingsSummary(form);
  }, form === 'account' ? 'Password updated. Other sessions are signed out.' : 'Changes saved');
}
async function saveWorkflowSettings() {
  const changes = settingsChanges('workflow');
  const approval = Object.fromEntries(Object.entries(changes).filter(([k]) => ['application_mode', 'min_review_score', 'execution_mode'].includes(k)).map(([k, v]) => [k, k === 'min_review_score' ? Number(v) : v]));
  const cadence = Object.fromEntries(Object.entries(changes).filter(([k]) => ['weekly_target', 'followup_days'].includes(k)).map(([k, v]) => [k, Number(v)]));
  let saved = false;
  try {
    if (Object.keys(approval).length) { D.settings = await api('/api/settings', { method: 'PATCH', body: approval }); saved = true; }
    if (Object.keys(cadence).length) {
      try { D.prefs = await api('/api/preferences', { method: 'PUT', body: cadence }); saved = true; }
      catch (err) { throw new Error(`${Object.keys(approval).length ? 'Approval settings saved. ' : ''}Cadence could not be saved: ${err.message}`); }
    }
  } finally {
    if (saved) await refreshSettingsSummary('workflow');
  }
}
function settingsResumeGuard() {
  return !settingsDirty('resumes') || confirm('This action replaces your unsaved resume text. Continue?');
}
async function settingsUseResume(id) {
  if (!settingsResumeGuard()) return;
  await settingsRun('resumes', async () => {
    D.resumes = await api(`/api/profile/resumes/${id}/primary`, { method: 'POST' });
    delete S.forms.resumes; D.profile = await api('/api/profile'); D.resumeData = undefined;
  }, 'Primary resume updated');
}
async function settingsUploadResume(file) {
  if (!settingsResumeGuard()) return;
  await settingsRun('resumes', async () => {
    const form = new FormData(); form.append('file', file);
    await api('/api/profile/resume', { method: 'POST', form });
    delete S.forms.resumes;
    [D.profile, D.resumes] = await Promise.all([api('/api/profile'), api('/api/profile/resumes')]);
    D.resumeData = undefined;
  }, 'Resume uploaded and set as primary — review its data in Resume Data');
}
const SETTINGS_ACTIONS = {
  'settings-retry': async () => { D.settingsLoadError = ''; render(); await load(); },
  'settings-section': el => settingsNavigate(el.value),
  'settings-save': el => settingsSave(el.dataset.form),
  'settings-discard': el => { const f = el.dataset.form; delete S.forms[f]; if (f === 'preferences') authorizationDraft = null; if (f === 'resume-data') rdReset(); delete SETTING_ERRORS[f]; delete SETTINGS_SUCCESS[f]; render(); },
  'auth-add': () => { settingsAuthUpdate(); authorizationDraft.rows.push({ country: '', status: '' }); render(); document.querySelector('[data-auth-row]:last-child [data-auth-country]')?.focus(); },
  'auth-remove': el => { settingsAuthUpdate(); authorizationDraft.rows.splice(Number(el.dataset.index), 1); render(); },
  'settings-key-edit': el => { delete S.forms.keys; delete SETTING_ERRORS.keys; editingKey = editingKey === el.dataset.id ? null : el.dataset.id; render(); document.getElementById(`keys-${editingKey}`)?.focus(); },
  'key-remove': el => {
    if (!confirm('Remove this saved key? Work needing it pauses until a replacement is added.')) return;
    return settingsRun('keys', async () => { D.settings = await api('/api/settings/keys', { method: 'PUT', body: { [el.dataset.id]: '' } }); delete S.forms.keys; editingKey = null; }, 'Key removed');
  },
  'resume-primary': el => settingsUseResume(el.dataset.id),
  'resume-use': el => settingsUseResume(el.dataset.id),
  'resume-delete': el => {
    if (!confirm(`Delete ${el.dataset.name}? This cannot be undone.`)) return;
    const replacesText = D.resumes.length > 1 && D.resumes.some(d => d.id === Number(el.dataset.id) && d.active);
    if (replacesText && !settingsResumeGuard()) return;
    return settingsRun('resumes', async () => { const out = await api(`/api/profile/resumes/${el.dataset.id}`, { method: 'DELETE' }); D.resumes = out.resumes; if (replacesText && out.promoted != null) delete S.forms.resumes; D.profile = await api('/api/profile'); D.resumeData = undefined; }, 'Resume deleted');
  },
  'parse-evidence': () => {
    if (settingsDirty('resumes')) { SETTING_ERRORS.resumes = 'Save your resume text before extracting evidence.'; render(); return; }
    return settingsRun('resumes', async () => { const out = await api('/api/profile/resume/parse-evidence', { method: 'POST' }); toast(`${out.added} unverified claims added to the Knowledge bank`); });
  },
  logout: () => {
    if (!settingsCanLeave('logout')) return;
    settingsClearSecrets();
    return busy('logout', async () => { await api('/api/auth/logout', { method: 'POST' }); window.location.href = '/login'; });
  },
};
