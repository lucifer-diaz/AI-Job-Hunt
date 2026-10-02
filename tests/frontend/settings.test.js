import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Execute the production scripts together, omitting only automatic network boot.
const settingsSource = readFileSync('app/static/settings.js', 'utf8');
const resumeDataSource = readFileSync('app/static/resume_data.js', 'utf8');
const jobDataSource = readFileSync('app/static/job_data.js', 'utf8');
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'setF', 'render', 'readHash', 'setHash', 'settingsNavigate', 'settingsChanges', 'settingsPayload', 'settingsDirty', 'settingsCanLeave', 'settingsSave', 'settingsUploadResume', 'settingsUseResume', 'profileCompleteness', 'addTags', 'commitTag', 'SETTING_ERRORS', 'SETTINGS_SECTIONS', 'SETTINGS_ACTIONS', 'RD', 'resumeDataDirty', 'rdReset', 'JD', 'renderJobStructured', 'jobDataInput'];
let ui, fetchMock, context;
function response(data, status = 200) { return { ok: status < 400, status, text: async () => JSON.stringify(data) }; }
function data() {
  return { profile: { name: 'Alex Morgan', headline: 'Engineering leader', email: 'alex@example.com', phone: '123', location: 'Mumbai', linkedin_url: '', github_url: '', master_resume: 'My resume', target_roles: ['Lead'], preferred_locations: ['Mumbai, India'], work_authorization: 'Citizen', compensation_notes: 'INR 75 lakh', search_preferences: { total_experience: 0, notice_period: 0, primary_skills: ['Python'] } },
    summary: { profile: { name: 'Alex Morgan', headline: 'Engineering leader' }, counts: {}, model: 'default-model', llm_provider: 'openai', application_mode: 'manual_approval', auto_search: { schedule: 'daily', setups: 1, roles: 1, locations: 1, next_run: null, last_run: null } },
    resumes: [], prefs: { weekly_target: 10, followup_days: 7, warn_closing: false },
    settings: { secret_storage: true, ranking_concurrency: 5, tailoring_concurrency: 2, processing_maximums: { ranking_concurrency: 5, tailoring_concurrency: 2 }, rate_limits: { enabled: true, base_seconds: 30, multiplier: 2, max_seconds: 900, ladder: [30, 60, 120, 240, 480] }, keys: Object.fromEntries(['openai', 'openrouter', 'anthropic', 'apify'].map(k => [k, { configured: k === 'openai', hint: k === 'openai' ? '…1234' : '' }])), configured: { openai: true, mock: true }, application_mode: 'manual_approval', min_review_score: 70,
      ai: { saved: { llm_provider: '', model: '', require_free: null, stage_models: {} }, env: { llm_provider: 'openai', models: { openai: 'default-model' }, require_free: true, stage_models: {} } } } };
}
function section(id) { ui.S.settingsSection = id; ui.render(); }
function input(id, value) {
  const el = document.getElementById(id); el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
}
beforeEach(() => {
  // New document event targets prevent event handlers accumulating between test instances.
  document.body.innerHTML = '<script id="boot" type="application/json">{"user":{"email":"login@example.com"},"max_upload_mb":10}</script><div id="nav"></div><div id="side-foot"></div><div id="side-meta"></div><div id="brand-sub"></div><div id="top"></div><div id="view"></div><div id="overlays"></div><div id="toast"></div>';
  vi.stubGlobal('confirm', vi.fn(() => true));
  vi.stubGlobal('scrollTo', vi.fn());
  fetchMock = vi.fn(async url => response(url === '/api/workspace/summary' ? ui.D.summary : {})); vi.stubGlobal('fetch', fetchMock);
  // Each isolated iframe-like jsdom document needs the same production event registration just once.
  if (!ui) {
    context = createContext({ document, window, location, history, navigator, URLSearchParams, FormData, File, setTimeout, clearTimeout, setInterval, clearInterval, console });
    runInContext(readFileSync('app/static/monitoring.js', 'utf8'), context, { filename: 'monitoring.js' });
    runInContext(settingsSource, context, { filename: resolve('app/static/settings.js') });
    runInContext(resumeDataSource, context, { filename: resolve('app/static/resume_data.js') });
    runInContext(jobDataSource, context, { filename: resolve('app/static/job_data.js') });
    runInContext(readFileSync('app/static/knowledge.js', 'utf8'), context, { filename: 'knowledge.js' });
    runInContext(readFileSync('app/static/inbox.js', 'utf8'), context, { filename: 'inbox.js' });
    runInContext(readFileSync('app/static/guide.js', 'utf8'), context, { filename: 'guide.js' });
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirm;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'settings'; ui.S.settingsSection = 'profile';
  Object.keys(ui.SETTING_ERRORS).forEach(k => delete ui.SETTING_ERRORS[k]);
  ui.rdReset(); ui.RD.tab = 'structured'; ui.RD.changes = null; ui.RD.raw = null; ui.RD.decisions = {}; ui.RD.loading = false;
  Object.assign(ui.D, data(), { settingsLoadError: '', resumeData: undefined }); ui.render();
});

describe('focused settings UI', () => {
  it('renders each section with labels and no unrelated editors', () => {
    for (const [id, title] of ui.SETTINGS_SECTIONS) {
      section(id);
      expect(document.querySelector('h2').textContent).toBe(title);
      expect(document.querySelectorAll('.settings-nav [aria-current]').length).toBe(1);
      for (const el of document.querySelectorAll('#settings-editor input:not([type="radio"]), #settings-editor textarea, #settings-editor select')) {
        expect(el.getAttribute('aria-label') || el.labels?.length).toBeTruthy();
      }
    }
    section('profile');
    expect(document.querySelector('#account-new_password')).toBeNull();
    expect(document.querySelector('#resumes-master_resume')).toBeNull();
  });
  it('supports default and deep-link routes', () => {
    history.replaceState(null, '', '#settings'); ui.readHash(); expect(ui.S.settingsSection).toBe('profile');
    history.replaceState(null, '', '#settings/api-keys'); ui.readHash(); expect(ui.S.settingsSection).toBe('api-keys');
    history.replaceState(null, '', '#settings/unknown'); ui.readHash(); expect(ui.S.settingsSection).toBe('profile');
    ui.settingsNavigate('ai'); expect(location.hash).toBe('#settings/ai');
  });
  it('retains independent drafts and discards only the selected editor', () => {
    input('profile-name', 'Edited name'); ui.settingsNavigate('preferences');
    ui.setF('preferences', 'notice_period', '14'); ui.settingsNavigate('profile');
    expect(document.getElementById('profile-name').value).toBe('Edited name');
    ui.A['settings-discard']({ dataset: { form: 'profile' } });
    expect(ui.settingsDirty('profile')).toBe(false);
    expect(ui.settingsDirty('preferences')).toBe(true);
  });
  it('enables save on changes and disables it on revert', () => {
    const save = () => document.querySelector('[data-settings-save]');
    expect(save().disabled).toBe(true);
    input('profile-name', 'Changed'); expect(save().disabled).toBe(false);
    input('profile-name', 'Alex Morgan'); expect(save().disabled).toBe(true);
  });
  it('sends changed profile fields only and preserves failed drafts', async () => {
    input('profile-name', 'Edited');
    fetchMock.mockResolvedValueOnce(response({ detail: 'Try again' }, 503));
    await ui.settingsSave('profile');
    expect(ui.S.forms.profile.name).toBe('Edited');
    expect(document.querySelector('[role="alert"]').textContent).toBe('Try again');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, name: 'Edited' }));
    await ui.settingsSave('profile');
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ name: 'Edited' });
    expect(fetchMock.mock.calls[1][1].method).toBe('PATCH');
    expect(ui.settingsDirty('profile')).toBe(false);
  });
  it('keeps zero values complete and creates nested preference patches', () => {
    expect(ui.profileCompleteness()).toBe(100);
    ui.setF('preferences', 'notice_period', '0'); expect(ui.settingsDirty('preferences')).toBe(false);
    ui.setF('preferences', 'primary_skills', []); ui.setF('preferences', 'target_roles', ['Staff']);
    expect(ui.settingsPayload('preferences')).toEqual({ target_roles: ['Staff'], search_preferences: { primary_skills: [] } });
  });
  it('preserves location commas, deduplicates tags and enforces role limits', () => {
    ui.addTags('preferences', 'preferred_locations', 'New York, NY\nLondon', 25, []);
    expect(ui.S.forms.preferences.preferred_locations).toEqual(['New York, NY', 'London']);
    ui.addTags('preferences', 'target_roles', 'A,a,B,C,D,E,F', 5, []);
    expect(ui.S.forms.preferences.target_roles).toEqual(['A', 'B', 'C', 'D', 'E']);
  });
  it('commits a pending tag without replacing the save button on blur', () => {
    section('preferences'); const field = document.getElementById('preferences-primary_skills');
    const save = document.querySelector('[data-settings-save]'); field.value = 'Rust'; ui.commitTag(field, false);
    expect(ui.S.forms.preferences.primary_skills).toEqual(['Python', 'Rust']);
    expect(document.querySelector('[data-settings-save]')).toBe(save);
  });
  it('warns before leaving settings and clears secret fields between sections', () => {
    ui.setF('profile', 'name', 'Unsaved'); confirm.mockReturnValueOnce(false);
    expect(ui.settingsCanLeave('overview')).toBe(false);
    section('account'); ui.setF('account', 'new_password', 'temporary password');
    ui.settingsNavigate('ai'); expect(ui.S.forms.account).toBeUndefined(); expect(ui.S.forms.profile.name).toBe('Unsaved');
    expect(ui.settingsCanLeave('overview')).toBe(true); expect(ui.S.forms.profile).toBeUndefined();
  });
  it('saves one provider without changing or exposing another', async () => {
    section('api-keys'); ui.A['settings-key-edit']({ dataset: { id: 'openai' } });
    input('keys-openai', 'sk-test-only'); fetchMock.mockResolvedValueOnce(response(ui.D.settings));
    await ui.settingsSave('keys');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ openai: 'sk-test-only' });
    expect(ui.S.forms.keys).toBeUndefined(); expect(document.body.textContent).not.toContain('sk-test-only');
  });
  it('validates password matching and retains server errors', async () => {
    section('account');
    input('account-current_password', 'old password'); input('account-new_password', 'new password'); input('account-confirm_password', 'different password');
    await ui.settingsSave('account'); expect(fetchMock).not.toHaveBeenCalled();
    expect(ui.SETTING_ERRORS.account).toContain('do not match');
    input('account-confirm_password', 'new password');
    fetchMock.mockResolvedValueOnce(response({ detail: 'Current password is incorrect' }, 403));
    await ui.settingsSave('account'); expect(ui.SETTING_ERRORS.account).toContain('incorrect');
    fetchMock.mockResolvedValueOnce(response({ ok: true })); await ui.settingsSave('account');
    expect(ui.S.forms.account).toBeUndefined();
  });
  it('serializes clearing AI overrides as inheritance, including stage clearing', async () => {
    section('ai'); ui.D.settings.ai.saved.stage_models.rank = 'saved-model';
    ui.setF('ai', 'stage_rank', ''); ui.setF('ai', 'model', 'my-model');
    ui.setF('ai', 'require_free', 'false');
    expect(ui.settingsPayload('ai')).toEqual({ model: 'my-model', require_free: false, stage_models: { rank: '' } });
    ui.D.settings.ai.saved.require_free = true; ui.setF('ai', 'require_free', '');
    expect(ui.settingsPayload('ai').require_free).toBeNull();
    fetchMock.mockResolvedValueOnce(response(ui.D.settings)); await ui.settingsSave('ai');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/settings');
  });
  it('requires confirmation for automatic approval and reports partial workflow failure', async () => {
    section('workflow'); ui.setF('workflow', 'application_mode', 'automatic'); ui.setF('workflow', 'weekly_target', '20');
    confirm.mockReturnValueOnce(false); await ui.settingsSave('workflow'); expect(fetchMock).not.toHaveBeenCalled();
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.settings, application_mode: 'automatic' }));
    fetchMock.mockResolvedValueOnce(response({ detail: 'Unavailable' }, 503));
    await ui.settingsSave('workflow');
    expect(ui.SETTING_ERRORS.workflow).toContain('Approval settings saved');
    expect(ui.settingsChanges('workflow')).toEqual({ weekly_target: '20' });
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({ weekly_target: 20 });
  });
  it('turns on automatic tailoring with its own fit score', async () => {
    section('workflow');
    expect(document.getElementById('view').textContent).toContain('qualified jobs wait in the Tailor queue');
    expect(document.getElementById('workflow-auto_tailor_min_fit_score')).toBeNull();
    const box = document.querySelector('[data-field="auto_tailor"]'); box.checked = true;
    box.dispatchEvent(new Event('change', { bubbles: true }));  // the checkbox itself reveals the fit score field
    expect(document.getElementById('workflow-auto_tailor_min_fit_score')).not.toBeNull();
    expect(document.getElementById('view').textContent).toContain('starts at once when a tailoring slot is free (up to 2 at a time)');
    ui.setF('workflow', 'auto_tailor_min_fit_score', '80'); ui.render();
    expect(document.getElementById('view').textContent).toContain('fit score of 80 or more');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.settings, auto_tailor: { enabled: true, min_fit_score: 80, enabled_at: '2026-09-28T10:00:00' } }));
    await ui.settingsSave('workflow');
    const [url, options] = fetchMock.mock.calls[0];
    expect([url, options.method, JSON.parse(options.body)]).toEqual(['/api/settings', 'PATCH', { auto_tailor: true, auto_tailor_min_fit_score: 80 }]);
    ui.D.settings.execution_mode = 'claude_desktop'; section('workflow');
    expect(document.querySelector('[data-field="auto_tailor"]').disabled).toBe(true);
    expect(document.getElementById('view').textContent).toContain('only runs on Job Hunt OS workers');
  });
  it('protects dirty resume text before primary selection and upload', async () => {
    section('resumes'); ui.setF('resumes', 'master_resume', 'Unsaved resume');
    confirm.mockReturnValue(false); await ui.settingsUseResume(1); await ui.settingsUploadResume(new File(['test'], 'test.txt'));
    expect(fetchMock).not.toHaveBeenCalled(); expect(ui.S.forms.resumes.master_resume).toBe('Unsaved resume');
    confirm.mockReturnValue(true);
    fetchMock.mockResolvedValueOnce(response([])).mockResolvedValueOnce(response({ ...ui.D.profile, master_resume: 'From file' }));
    await ui.settingsUseResume(1); expect(ui.S.forms.resumes).toBeUndefined(); expect(ui.D.profile.master_resume).toBe('From file');
  });
  it('renders dangerous profile and filename content as text', () => {
    ui.D.profile.name = '<img src=x onerror=alert(1)>'; ui.render();
    expect(document.querySelector('.settings-identity img')).toBeNull();
    ui.D.resumes = [{ id: 1, filename: '<svg onload=alert(1)>.txt', size: 10, words: 2, active: true, in_use: false }]; section('resumes');
    expect(document.querySelector('.settings-resume svg')).toBeNull();
    expect(document.body.textContent).toContain('Your edited text is being used');
  });
  it('saves pending chip text and enables Save before blur', async () => {
    section('preferences'); input('preferences-primary_skills', 'Rust');
    expect(document.querySelector('[data-settings-save]').disabled).toBe(false);
    fetchMock.mockResolvedValueOnce(response(ui.D.profile)); await ui.settingsSave('preferences');
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ search_preferences: { primary_skills: ['Python', 'Rust'] } });
  });
  it('captures country-specific work authorization and arrangement choices', () => {
    section('preferences');
    const country = document.getElementById('preferences-auth-country-0');
    const status = document.getElementById('preferences-auth-status-0');
    country.value = 'India'; country.dispatchEvent(new Event('change', { bubbles: true }));
    status.value = 'Citizen'; status.dispatchEvent(new Event('change', { bubbles: true }));
    ui.A['auth-add']({});
    document.getElementById('preferences-auth-country-1').value = 'Singapore';
    document.getElementById('preferences-auth-country-1').dispatchEvent(new Event('change', { bubbles: true }));
    document.getElementById('preferences-auth-status-1').value = 'EP (Employment Pass)';
    document.getElementById('preferences-auth-status-1').dispatchEvent(new Event('change', { bubbles: true }));
    expect(ui.settingsPayload('preferences')).toMatchObject({ work_authorization: 'India — Citizen\nSingapore — EP (Employment Pass)' });
    expect(document.querySelectorAll('[data-auth-row]').length).toBe(2);
  });
  it('removes a saved key only after confirmation and clears cancelled editors', async () => {
    section('api-keys'); ui.A['settings-key-edit']({ dataset: { id: 'openai' } });
    input('keys-openai', 'discard-this');
    ui.A['settings-key-edit']({ dataset: { id: 'openai' } }); expect(ui.S.forms.keys).toBeUndefined();
    confirm.mockReturnValueOnce(false); await ui.A['key-remove']({ dataset: { id: 'openai' } });
    expect(fetchMock).not.toHaveBeenCalled();
    fetchMock.mockResolvedValueOnce(response(ui.D.settings));
    await ui.A['key-remove']({ dataset: { id: 'openai' } });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ openai: '' });
  });
  it('blocks navigation during saves and disables unavailable key storage', () => {
    ui.S.busy['settings-profile'] = true;
    expect(ui.settingsCanLeave('settings', 'ai')).toBe(false);
    ui.settingsNavigate('ai'); expect(ui.S.settingsSection).toBe('profile');
    ui.S.busy = {}; ui.D.settings.secret_storage = false; section('api-keys');
    expect([...document.querySelectorAll('[data-act="settings-key-edit"]')].every(el => el.disabled)).toBe(true);
    expect(document.body.textContent).toContain('Key storage is unavailable');
  });
  it('uploads resumes and saves explicit text with refreshed file provenance', async () => {
    section('resumes');
    fetchMock.mockResolvedValueOnce(response({ id: 1 })).mockResolvedValueOnce(response({ ...ui.D.profile, master_resume: 'Uploaded' })).mockResolvedValueOnce(response([]));
    await ui.settingsUploadResume(new File(['Uploaded'], 'test.txt'));
    expect(fetchMock.mock.calls[0][1].body).toBeInstanceOf(FormData);
    expect(ui.D.profile.master_resume).toBe('Uploaded');
    ui.setF('resumes', 'master_resume', 'Edited text');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, master_resume: 'Edited text' })).mockResolvedValueOnce(response([]));
    await ui.settingsSave('resumes');
    expect(JSON.parse(fetchMock.mock.calls[3][1].body)).toEqual({ master_resume: 'Edited text' });
  });
  it('protects primary deletion but keeps text drafts when deleting a secondary file', async () => {
    section('resumes'); ui.D.resumes = [{ id: 1, filename: 'primary.txt', active: true }, { id: 2, filename: 'older.txt', active: false }];
    ui.setF('resumes', 'master_resume', 'Draft');
    confirm.mockReturnValueOnce(true).mockReturnValueOnce(false);
    await ui.A['resume-delete']({ dataset: { id: '1', name: 'primary.txt' } }); expect(fetchMock).not.toHaveBeenCalled();
    fetchMock.mockResolvedValueOnce(response({ resumes: [] })).mockResolvedValueOnce(response(ui.D.profile));
    await ui.A['resume-delete']({ dataset: { id: '2', name: 'older.txt' } });
    expect(ui.S.forms.resumes.master_resume).toBe('Draft');
  });
  it('requires saved text for evidence extraction', async () => {
    section('resumes'); ui.setF('resumes', 'master_resume', 'Draft');
    await ui.A['parse-evidence'](); expect(fetchMock).not.toHaveBeenCalled();
    expect(ui.SETTING_ERRORS.resumes).toContain('Save your resume');
    delete ui.S.forms.resumes; fetchMock.mockResolvedValueOnce(response({ added: 2 }));
    await ui.A['parse-evidence'](); expect(fetchMock.mock.calls[0][0]).toContain('parse-evidence');
  });
  it('saves cadence alone and never writes untouched approval settings', async () => {
    section('workflow'); ui.setF('workflow', 'followup_days', '14');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.prefs, followup_days: 14 }));
    await ui.settingsSave('workflow'); expect(fetchMock.mock.calls.filter(([, options]) => options.method !== 'GET')).toHaveLength(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/preferences'); expect(ui.D.prefs.followup_days).toBe(14);
  });
  it('supports section selector changes and password reuse validation', async () => {
    ui.A['settings-section']({ value: 'account' });
    input('account-current_password', 'same password'); input('account-new_password', 'same password'); input('account-confirm_password', 'same password');
    await ui.settingsSave('account'); expect(ui.SETTING_ERRORS.account).toContain('different');
  });

  it('shows a recoverable initial load error and loading state', () => {
    ui.D.settingsLoadError = 'Offline'; ui.render();
    expect(document.querySelector('[role="alert"]').textContent).toContain('Offline');
    expect(document.querySelector('[data-act="settings-retry"]')).not.toBeNull();
    ui.D.settingsLoadError = ''; ui.D.profile = null; ui.render();
    expect(document.querySelector('.loading').textContent).toBe('Loading…');
  });
  it('preserves ordinary drafts on Back and Forward routes', async () => {
    const fixture = data();
    fetchMock.mockImplementation(async url => response(({ '/api/profile': fixture.profile, '/api/settings': fixture.settings, '/api/preferences': fixture.prefs, '/api/profile/resumes': [], '/api/workspace/summary': null })[url]));
    section('profile'); ui.setF('profile', 'name', 'Still editing');
    history.replaceState(null, '', '#settings/preferences');
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(ui.S.settingsSection).toBe('preferences');
    history.replaceState(null, '', '#settings/profile');
    window.dispatchEvent(new HashChangeEvent('hashchange'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(ui.S.forms.profile.name).toBe('Still editing');
  });

  it('refreshes saved identity without clearing other section drafts', async () => {
    input('profile-name', 'Updated identity');
    ui.setF('preferences', 'notice_period', '14');
    const summary = { ...ui.D.summary, profile: { name: 'Updated identity', headline: 'Updated headline' } };
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, name: 'Updated identity' })).mockResolvedValueOnce(response(summary));
    await ui.settingsSave('profile');
    expect(fetchMock.mock.calls[1][0]).toBe('/api/workspace/summary');
    expect(document.getElementById('brand-sub').textContent).toBe('Updated identity · Updated headline');
    expect(ui.S.forms.preferences.notice_period).toBe('14');
  });
  it('refreshes the effective model shown in the shell after an AI save', async () => {
    section('ai'); ui.setF('ai', 'model', 'updated-model');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.settings, model: 'updated-model' }));
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.summary, model: 'updated-model' }));
    await ui.settingsSave('ai');
    expect(document.getElementById('side-meta').textContent).toContain('updated-model');
  });
  it('refreshes auto-search counts after preference saves', async () => {
    section('preferences'); ui.setF('preferences', 'target_roles', ['Staff', 'Lead']);
    const summary = { ...ui.D.summary, auto_search: { ...ui.D.summary.auto_search, setups: 2, roles: 2, locations: 3 } };
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, target_roles: ['Staff', 'Lead'] })).mockResolvedValueOnce(response(summary));
    await ui.settingsSave('preferences');
    expect(document.getElementById('side-foot').textContent).toContain('2 scheduled setups · 2 roles × 3 locations');
  });
  it('refreshes the shell even if cadence fails after approval settings save', async () => {
    section('workflow'); ui.setF('workflow', 'application_mode', 'automatic'); ui.setF('workflow', 'followup_days', '14');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.settings, application_mode: 'automatic' }));
    fetchMock.mockResolvedValueOnce(response({ detail: 'Cadence offline' }, 503));
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.summary, application_mode: 'automatic' }));
    await ui.settingsSave('workflow');
    expect(document.getElementById('side-meta').textContent).toContain('automatic');
    expect(ui.SETTING_ERRORS.workflow).toContain('Approval settings saved');
    expect(ui.settingsChanges('workflow')).toEqual({ followup_days: '14' });
  });
  it('distinguishes summary-refresh failure from save failure', async () => {
    input('profile-name', 'Already saved');
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, name: 'Already saved' }));
    fetchMock.mockResolvedValueOnce(response({ detail: 'Offline' }, 503));
    await ui.settingsSave('profile');
    expect(ui.D.profile.name).toBe('Already saved');
    expect(ui.S.forms.profile).toBeUndefined();
    expect(ui.SETTING_ERRORS.profile).toContain('Changes saved, but the sidebar could not refresh');
    expect(document.querySelector('[data-settings-save]').disabled).toBe(true);
    expect(ui.D.summary.profile.name).toBe('Alex Morgan');
  });
  it('preserves unsaved edits when the only uploaded resume is deleted', async () => {
    section('resumes'); ui.D.resumes = [{ id: 1, filename: 'only.txt', active: true }];
    ui.setF('resumes', 'master_resume', 'Do not discard this draft');
    fetchMock.mockResolvedValueOnce(response({ promoted: null, resumes: [] })).mockResolvedValueOnce(response(ui.D.profile));
    await ui.A['resume-delete']({ dataset: { id: '1', name: 'only.txt' } });
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(ui.S.forms.resumes.master_resume).toBe('Do not discard this draft');
    expect(ui.settingsDirty('resumes')).toBe(true);
  });
  it('clears a confirmed draft only when the backend actually promotes a replacement', async () => {
    section('resumes'); ui.D.resumes = [{ id: 1, filename: 'primary.txt', active: true }, { id: 2, filename: 'replacement.txt', active: false }];
    ui.setF('resumes', 'master_resume', 'Draft');
    fetchMock.mockResolvedValueOnce(response({ promoted: 2, resumes: [] })).mockResolvedValueOnce(response({ ...ui.D.profile, master_resume: 'Replacement text' }));
    await ui.A['resume-delete']({ dataset: { id: '1', name: 'primary.txt' } });
    expect(confirm).toHaveBeenCalledTimes(2);
    expect(ui.S.forms.resumes).toBeUndefined();
    expect(ui.D.profile.master_resume).toBe('Replacement text');
  });
  it('retains a draft when an expected replacement disappeared before deletion', async () => {
    section('resumes'); ui.D.resumes = [{ id: 1, filename: 'primary.txt', active: true }, { id: 2, filename: 'replacement.txt', active: false }];
    ui.setF('resumes', 'master_resume', 'Draft');
    fetchMock.mockResolvedValueOnce(response({ promoted: null, resumes: [] })).mockResolvedValueOnce(response(ui.D.profile));
    await ui.A['resume-delete']({ dataset: { id: '1', name: 'primary.txt' } });
    expect(ui.S.forms.resumes.master_resume).toBe('Draft');
  });

});

describe('resume data', () => {
  const verification = status => ({ status, updated_at: null });
  function detail(overrides = {}) {
    const structured = {
      personal_information: { id: 'personal', full_name: 'Alex Morgan', headline: 'Lead', location: 'Mumbai', email: '', phone: '', linkedin: '', github: '', portfolio: '', other_links: [], verification: verification('VERIFIED') },
      summary: { id: 'summary', text: 'Builds <platforms>', verification: verification('UNVERIFIED') },
      skills: [{ id: 'skill_python', name: 'Python', category: 'Languages', verification: verification('VERIFIED') }],
      experience: [{ id: 'exp_zendrive_2024', company: 'Zendrive', job_title: 'Lead Engineer', location: '', employment_type: '', start_date: '2024-06', end_date: '2025-10', is_current: false, technologies: ['Python'], verification: verification('EDITED'),
        achievements: [{ id: 'ach_1', text: 'Reduced incidents by 45%', verification: verification('VERIFIED') }], responsibilities: [{ id: 'resp_1', text: 'Owned ingestion', verification: verification('REJECTED') }] }],
      projects: [], education: [], certifications: [], awards: [], languages: [], additional_information: [],
    };
    const progress = { total: 6, counts: { VERIFIED: 3, UNVERIFIED: 1, EDITED: 1, REJECTED: 1 }, reviewed: 4, percent: 67, needs_review: 2,
      sections: [['personal_information', 'VERIFIED'], ['summary', 'UNVERIFIED'], ['skills', 'VERIFIED'], ['experience', 'NEEDS_REVIEW'], ['projects', 'EMPTY']].map(([section, state]) => ({ section, label: section, state, required: true, counts: { UNVERIFIED: state === 'UNVERIFIED' ? 1 : 0, EDITED: state === 'NEEDS_REVIEW' ? 1 : 0 } })),
      entries: [{ section: 'experience', item_id: 'exp_zendrive_2024', label: 'Lead Engineer · Zendrive', state: 'NEEDS_REVIEW', counts: {} }],
      blocking: [{ item_id: 'summary', label: 'Builds platforms' }] };
    return { id: 5, name: 'resume.pdf', version: 2, is_active: true, is_default: true, verification_status: 'NEEDS_REVIEW', verified_at: '2026-09-01T10:00:00', revision: 3, source_filename: 'resume.pdf', has_original: true, structured, progress, pending_reparse: null, ...overrides };
  }
  function settle() { return new Promise(r => setTimeout(r, 0)); }
  function route(routes) {
    fetchMock.mockImplementation(async (url, opt = {}) => {
      const key = `${opt.method || 'GET'} ${url}`;
      const hit = Object.entries(routes).find(([k]) => key === k || key.startsWith(k));
      if (!hit) return response({});
      const value = typeof hit[1] === 'function' ? hit[1](JSON.parse(opt.body || 'null')) : hit[1];
      return value && value.__status ? response(value.body, value.__status) : response(value);
    });
  }
  async function open(active = detail(), extra = {}) {
    route({ 'GET /api/settings/resume': { active, versions: [active, { id: 4, name: 'old.pdf', version: 1, is_active: false, is_default: false, verification_status: 'UNVERIFIED' }], policy: { allow_unverified_resume_facts: false, ranking_uses_unverified: true } }, ...extra });
    section('resume-data'); await settle();
  }

  it('shows verification progress, statuses and blocks Verify Resume while items need review', async () => {
    await open();
    const view = document.getElementById('view').innerHTML;
    expect(document.querySelector('h2').textContent).toBe('Resume Data');
    expect(document.querySelector('[data-rd-percent]').textContent).toContain('67%');
    expect(view).toContain('require review');
    expect(view).toContain('Builds &lt;platforms&gt;');
    expect(document.querySelector('[data-rd-item="ach_1"] [data-rd-status]').dataset.rdStatus).toBe('VERIFIED');
    expect(document.querySelector('[data-rd-item="resp_1"] [data-rd-status]').dataset.rdStatus).toBe('REJECTED');
    expect(document.querySelector('[data-act="rd-verify-resume"]').disabled).toBe(true);
    for (const el of document.querySelectorAll('#settings-editor input, #settings-editor textarea')) expect(el.labels?.length).toBeTruthy();
  });
  it('verifies an item, a section and the whole resume through the API', async () => {
    const verified = detail({ verification_status: 'VERIFIED', progress: { ...detail().progress, blocking: [] } });
    await open(detail(), { 'POST /api/resumes/5/verify-item': body => { expect(body).toEqual({ item_id: 'summary', status: 'VERIFIED' }); return verified; },
      'POST /api/resumes/5/verify-section': verified, 'POST /api/resumes/5/unverify-item': detail(), 'POST /api/resumes/5/verify': verified });
    await ui.A['rd-verify-item']({ dataset: { id: 'summary', status: 'VERIFIED' } });
    expect(ui.D.resumeData.active.verification_status).toBe('VERIFIED');
    expect(document.querySelector('[data-act="rd-verify-resume"]').disabled).toBe(false);
    await ui.A['rd-verify-section']({ dataset: { section: 'experience' } });
    await ui.A['rd-unverify']({ dataset: { id: 'ach_1' } });
    await ui.A['rd-verify-resume']();
    expect(fetchMock.mock.calls.map(([url, opt]) => `${opt?.method || 'GET'} ${url}`)).toContain('POST /api/resumes/5/verify');
  });
  it('lists blocking items when the server refuses to verify the resume', async () => {
    await open(detail({ progress: { ...detail().progress, blocking: [] } }), { 'POST /api/resumes/5/verify': { __status: 422, body: { detail: { message: '1 required item(s) still need review', blocking: [{ item_id: 'x', label: 'Unreviewed degree' }] } } } });
    await ui.A['rd-verify-resume']();
    expect(ui.SETTING_ERRORS['resume-data']).toContain('still need review');
    expect(document.getElementById('view').innerHTML).toContain('Unreviewed degree');
  });
  it('edits a section as a form, tracks dirty state and saves typed data with the revision', async () => {
    let sent;
    await open(detail(), { 'PUT /api/resumes/5/structured': body => { sent = body; return detail({ revision: 4 }); } });
    ui.A['rd-edit']({ dataset: { section: 'experience' } });
    expect(document.querySelector('#rd-experience-0-company').value).toBe('Zendrive');
    expect(ui.resumeDataDirty()).toBe(false);
    input('rd-experience-0-company', 'Zendrive Inc');
    input('rd-experience-0-technologies', 'Python, Kafka');
    input('rd-experience-0-start_date', '');
    const current = document.getElementById('rd-experience-0-is_current'); current.checked = true; current.dispatchEvent(new Event('change', { bubbles: true }));
    expect(ui.resumeDataDirty()).toBe(true);
    ui.A['rd-add']({ dataset: { path: 'experience.0.achievements', bullet: '1' } });
    input('rd-experience-0-achievements-1-text', 'Cut cost 30%');
    ui.A['rd-move']({ dataset: { path: 'experience.0.achievements', index: '1', dir: '-1' } });
    ui.A['rd-move']({ dataset: { path: 'experience.0.achievements', index: '0', dir: '-1' } });
    ui.A['rd-add']({ dataset: { path: 'experience', section: 'experience' } });
    ui.A['rd-remove']({ dataset: { path: 'experience', index: '1' } });
    await ui.A['settings-save']({ dataset: { form: 'resume-data' } });
    expect(sent.revision).toBe(3);
    expect(sent.data.experience[0]).toMatchObject({ company: 'Zendrive Inc', technologies: ['Python', 'Kafka'], start_date: null, is_current: true });
    expect(sent.data.experience[0].achievements.map(a => a.text)).toEqual(['Cut cost 30%', 'Reduced incidents by 45%']);
    expect(ui.RD.editing).toBeNull();
  });
  it('reloads and keeps the user informed after an edit conflict', async () => {
    await open(detail(), { 'PUT /api/resumes/5/structured': { __status: 409, body: { detail: 'This resume changed since you opened it.' } } });
    ui.A['rd-edit']({ dataset: { section: 'summary' } });
    input('rd-summary-text', 'New summary');
    await ui.A['settings-save']({ dataset: { form: 'resume-data' } });
    expect(ui.SETTING_ERRORS['resume-data']).toContain('were not saved');
  });
  it('asks before discarding edits when cancelling, switching tabs or leaving settings', async () => {
    await open();
    ui.A['rd-edit']({ dataset: { section: 'skills' } });
    input('rd-skills-0-name', 'Go');
    confirm.mockReturnValueOnce(false);
    ui.A['rd-cancel']();
    expect(ui.RD.editing).toBe('skills');
    ui.A['rd-tab']({ dataset: { tab: 'raw' } });
    expect(ui.RD.editing).toBeNull();
    ui.A['rd-tab']({ dataset: { tab: 'structured' } });
    ui.A['rd-edit']({ dataset: { section: 'skills' } }); input('rd-skills-0-name', 'Go');
    expect(ui.settingsCanLeave('overview')).toBe(true);
    expect(ui.RD.draft).toBeNull();
    ui.A['rd-edit']({ dataset: { section: 'skills' } }); input('rd-skills-0-name', 'Go');
    ui.A['settings-discard']({ dataset: { form: 'resume-data' } });
    expect(ui.RD.editing).toBeNull();
  });
  it('shows raw text read-only, the original file link and change history', async () => {
    await open(detail(), { 'GET /api/resumes/5/raw': { raw_text: 'RAW <b>resume</b>' }, 'GET /api/resumes/5/changes': { total: 1, items: [{ change_type: 'UPDATE', source: 'USER', field_path: 'summary.text', old_value: 'a', new_value: { x: 1 }, changed_at: '2026-09-02T10:00:00' }] } });
    ui.A['rd-tab']({ dataset: { tab: 'raw' } }); await settle();
    const raw = document.querySelector('[data-rd-raw]');
    expect(raw.textContent).toBe('RAW <b>resume</b>');
    expect(document.querySelector('#settings-editor textarea')).toBeNull();
    ui.A['rd-tab']({ dataset: { tab: 'original' } });
    expect(document.querySelector('a[href="/api/resumes/5/original"]')).toBeTruthy();
    ui.A['rd-tab']({ dataset: { tab: 'history' } }); await settle();
    expect(document.querySelector('[data-rd-change-type="UPDATE"]').textContent).toContain('summary.text');
  });
  it('compares a re-parse and sends only the choices the user made', async () => {
    let decisions;
    const pending = { parser: 'deterministic', changes: [{ id: 'chg_1', label: 'Work experience · title', current: 'Lead Engineer', proposed: 'Staff Engineer', current_status: 'VERIFIED' }] };
    await open(detail(), { 'POST /api/resumes/5/reparse/resolve': body => { decisions = body.decisions; return detail(); }, 'POST /api/resumes/5/reparse': detail({ pending_reparse: pending }) });
    await ui.A['rd-reparse']({ dataset: { ai: '0' } });
    const view = document.getElementById('view').innerHTML;
    expect(view).toContain('Staff Engineer'); expect(view).toContain('Keep current');
    ui.A['rd-decide']({ dataset: { change: 'chg_1', choice: 'use_new' } });
    await ui.A['rd-resolve']({ dataset: {} });
    expect(decisions).toEqual({ chg_1: 'use_new' });
    await ui.A['rd-reparse']({ dataset: { ai: '1' } });
    ui.A['rd-decide']({ dataset: { change: 'chg_1', choice: 'use_new' } });
    await ui.A['rd-resolve']({ dataset: { all: 'keep' } });
    expect(decisions).toEqual({});
  });
  it('switches versions and saves the candidate policy', async () => {
    let policy;
    await open(detail(), { 'POST /api/resumes/4/activate': detail(), 'POST /api/resumes/4/default': detail(), 'PUT /api/settings/resume/policy': body => { policy = body; return { allow_unverified_resume_facts: true, ranking_uses_unverified: true }; } });
    await ui.A['rd-activate']({ dataset: { id: '4' } });
    await ui.A['rd-default']({ dataset: { id: '4' } });
    await ui.A['rd-policy']({ dataset: { key: 'allow_unverified_resume_facts' }, checked: true });
    expect(policy).toEqual({ allow_unverified_resume_facts: true });
    expect(ui.D.resumeData.policy.allow_unverified_resume_facts).toBe(true);
    const sources = [...document.querySelectorAll('#view input[name="rd-resume-source"]')];
    expect(sources.map(r => r.value)).toEqual(['structured', 'master_text']);
    await ui.A['rd-policy-source']({ value: 'master_text' });
    expect(policy).toEqual({ resume_source: 'master_text' });
    ui.A['rd-jump']({ dataset: { section: 'skills' } }, { preventDefault() {} });
    expect(ui.RD.tab).toBe('structured');
  });
  it('renders every section type and edits single-object sections', async () => {
    const v = verification('UNVERIFIED');
    const base = detail();
    const structured = { ...base.structured, summary: { id: 'summary', text: '', verification: v },
      experience: [{ ...base.structured.experience[0], is_current: true, start_date: null, raw_start_date: '2021', achievements: [], responsibilities: [] }],
      projects: [{ id: 'proj_a', name: 'Atlas', description: 'Search', url: '', technologies: [], achievements: [{ id: 'pach_1', text: 'Served 2M users', verification: v }], verification: v }],
      education: [{ id: 'edu_1', degree: 'BTech', field_of_study: 'CS', institution: 'IIT', end_date: '2016-05', verification: v }],
      certifications: [{ id: 'cert_1', name: 'CKA', issuer: 'CNCF', issue_date: '2022', verification: v }],
      awards: [{ id: 'award_1', title: '', description: 'Hackathon winner', date: '2020', verification: v }],
      languages: [{ id: 'lang_1', name: 'Hindi', proficiency: 'Native', verification: v }, { id: 'lang_2', name: 'German', proficiency: '', verification: v }],
      additional_information: [{ id: 'info_1', text: 'Open source maintainer', verification: v }], skills: [{ id: 'skill_go', name: 'Go', category: '', verification: v }] };
    await open(detail({ structured, verified_at: null, has_original: false, source_filename: null, verification_status: 'SOMETHING_NEW',
      progress: { ...base.progress, blocking: Array.from({ length: 7 }, (_, i) => ({ label: `item ${i}` })) } }));
    const view = document.getElementById('view').innerHTML;
    for (const text of ['Atlas', 'Served 2M users', 'BTech · CS · IIT', 'CKA', 'Hackathon winner', 'Hindi · Native', 'German', 'Open source maintainer', '2021 – Present', 'Not verified yet', '…']) expect(view).toContain(text);
    ui.A['rd-edit']({ dataset: { section: 'personal_information' } });
    input('rd-personal_information-full_name', 'Alex M');
    expect(ui.resumeDataDirty()).toBe(true);
    ui.A['rd-cancel']();
    ui.A['rd-edit']({ dataset: { section: 'summary' } });
    expect(document.getElementById('rd-summary-text').value).toBe('');
    ui.rdReset();
    ui.A['rd-tab']({ dataset: { tab: 'original' } });
    expect(document.getElementById('view').innerHTML).toContain('no longer stored');
  });
  it('handles no resume and load failures', async () => {
    route({ 'GET /api/settings/resume': { active: null, versions: [], policy: {} } });
    section('resume-data'); await settle();
    expect(document.getElementById('view').innerHTML).toContain('No resume yet');
    route({ 'GET /api/settings/resume': { __status: 500, body: { detail: 'boom' } } });
    await ui.A['rd-reload'](); 
    expect(document.getElementById('view').innerHTML).toContain('could not be loaded');
  });
});

describe('structured job description', () => {
  const requirement = (id, level, value, source) => ({ id, type: 'SKILL', value, level, confidence: 0.9, derived: false, source_text: source, source_section: 'Requirements' });
  function payload(overrides = {}) {
    return {
      job_id: 7, status: 'PARSED', version: 'v1', parsed_at: '2026-09-18T10:00:00',
      structured_description: {
        job_identity: { title: 'Data Platform Lead', company: 'Globex', seniority: 'Lead', remote_type: 'hybrid' },
        role_summary: 'Own the data platform.', responsibilities: ['Own the Kafka pipelines'],
        required_skills: ['Strong experience with Python is required.'], preferred_skills: ['Familiarity with Kubernetes is a plus.'],
        experience_requirements: { minimum_years: 8, maximum_years: null, leadership_required: true, management_required: null },
        technical_domains: ['streaming'], leadership_expectations: ['Mentor engineers'], education_requirements: [],
        location_requirements: { country: 'Singapore', city: null, relocation_supported: null, visa_sponsorship: null, work_authorization_required: null },
        compensation: { min: 180000, max: 220000, currency: 'USD', period: 'year', source: 'job_description' },
        hard_requirements: [], nice_to_have: [], keywords: [], company_context: [],
        derived_signals: { likely_interview_domains: ['distributed systems'], likely_system_design_topics: [], likely_leadership_topics: [], likely_business_topics: [] },
      },
      requirements: [requirement('req_001', 'REQUIRED', 'Strong experience with Python is required.', 'Strong experience with Python is required.'),
        requirement('req_002', 'PREFERRED', 'Familiarity with Kubernetes is a plus.', 'Familiarity with Kubernetes is a plus.')],
      counts: { REQUIRED: 1, PREFERRED: 1, INFORMATIONAL: 0, UNKNOWN: 0 },
      candidate: null, has_candidate: false, parse_task: { status: 'COMPLETED', attempt_count: 1, last_error: null },
      derived_label: 'likely / inferred preparation topics, not stated by the employer', normalized_description: 'Requirements', structured_hash: 'a'.repeat(64),
      ...overrides,
    };
  }
  function settle() { return new Promise(r => setTimeout(r, 0)); }
  function routes(map) {
    fetchMock.mockImplementation(async (url, opt = {}) => {
      const key = `${opt.method || 'GET'} ${url}`;
      const hit = Object.entries(map).find(([k]) => key === k);
      if (!hit) return response({});
      const value = typeof hit[1] === 'function' ? hit[1](JSON.parse(opt.body || 'null')) : hit[1];
      return response(value);
    });
  }
  async function open(data = payload()) {
    routes({ 'GET /api/jobs/7/structured-description': data });
    ui.S.screen = 'discovery'; ui.S.jobOpen = 7; ui.S.jobTab = 'structured';
    ui.D.jobModal = { id: 7, title: 'Data Platform Lead', company: 'Globex', location: 'Singapore', board: 'linkedin',
      posted: 'today', fit: 80, fit_analysis: {}, matches: [], gaps: [], missing: [], keywords: [], wizard: [],
      gap_notes: [], status: 'discovered', description: 'raw', notes: '', summary: '', resume_id: null, rank_pct: null };
    ui.JD.data = null; ui.JD.draft = null; ui.JD.view = 'summary'; ui.JD.jobId = null; ui.JD.error = '';
    ui.render(); await settle();
  }

  it('shows requirements by level with the employer sentence behind each one', async () => {
    await open();
    const view = document.body.innerHTML;
    expect(view).toContain('Structured JD');
    expect(view).toContain('1 required · 1 preferred');
    expect(document.querySelector('[data-jd-req="req_001"]').textContent).toContain('Required');
    expect(document.querySelector('[data-jd-req="req_002"]').textContent).toContain('Preferred');
    expect(document.querySelector('[data-jd-req="req_001"] blockquote').textContent).toContain('Strong experience with Python');
    // Derived topics are labelled as inferred, never as employer statements.
    const derived = document.querySelector('[data-jd-derived]').textContent;
    expect(derived).toContain('Inferred');
    expect(derived).toContain('not stated by the employer');
    expect(derived).toContain('distributed systems');
    // Unstated facts say so rather than guessing.
    expect(view).toContain('sponsorship not stated');
  });

  it('corrects a value and sends typed structured data', async () => {
    let sent;
    await open();
    routes({ 'PUT /api/jobs/7/structured-description': body => { sent = body; return payload({ status: 'MANUALLY_CORRECTED' }); } });
    ui.A['jd-edit']();
    input('jd-job_identity-seniority', 'Staff');
    input('jd-experience_requirements-minimum_years', '10');
    const tri = document.getElementById('jd-location_requirements-visa_sponsorship');
    tri.value = 'false'; tri.dispatchEvent(new Event('input', { bubbles: true }));
    await ui.A['jd-save']({ dataset: { id: '7' } });
    expect(sent.structured_description.job_identity.seniority).toBe('Staff');
    expect(sent.structured_description.experience_requirements.minimum_years).toBe(10);
    expect(sent.structured_description.location_requirements.visa_sponsorship).toBe(false);
    expect(sent.base_hash).toBe('a'.repeat(64));
    expect(ui.JD.data.status).toBe('MANUALLY_CORRECTED');
    expect(ui.JD.draft).toBeNull();
  });

  it('reloads instead of overwriting when a parse landed while editing', async () => {
    await open();
    const newer = payload({ structured_hash: 'b'.repeat(64) });
    newer.structured_description.job_identity.seniority = 'Principal';
    fetchMock.mockImplementation(async (url, opt = {}) => opt.method === 'PUT'
      ? response({ detail: 'This job description was parsed again while you were editing.' }, 409) : response(newer));
    ui.A['jd-edit']();
    input('jd-job_identity-seniority', 'Staff');
    await ui.A['jd-save']({ dataset: { id: '7' } });
    expect(ui.JD.draft).toBeNull();
    expect(ui.JD.data.structured_description.job_identity.seniority).toBe('Principal');
    expect(document.body.innerHTML).toContain('parsed again while you were editing');
  });

  it('tells the user when a correction could not be applied to the requirements', async () => {
    await open();
    routes({ 'PUT /api/jobs/7/structured-description': () => ({ ...payload({ status: 'MANUALLY_CORRECTED' }), unmatched_corrections: ['Python'] }) });
    ui.A['jd-edit']();
    input('jd-required_skills', 'Go');
    await ui.A['jd-save']({ dataset: { id: '7' } });
    const notice = document.querySelector('[data-jd-unmatched]');
    expect(notice.textContent).toContain('not applied to the requirements');
    expect(notice.textContent).toContain('Python');
    ui.A['jd-edit'](); ui.A['jd-cancel']();
    expect(document.querySelector('[data-jd-unmatched]')).toBeNull();
  });

  it('compares a re-parse of a corrected job and sends only the chosen values', async () => {
    let decisions;
    const candidate = { version: 'v1', changes: [{ id: 'chg_1', path: 'job_identity.seniority', current: 'Staff', proposed: 'Lead', corrected: true }] };
    await open(payload({ status: 'MANUALLY_CORRECTED' }));
    routes({ 'POST /api/jobs/7/structured-description/reparse': payload({ status: 'MANUALLY_CORRECTED', candidate, has_candidate: true }),
      'POST /api/jobs/7/structured-description/reparse/resolve': body => { decisions = body.decisions; return payload(); } });
    await ui.A['jd-reparse']({ dataset: { id: '7' } });
    expect(document.querySelector('[data-jd-compare]').textContent).toContain('your correction');
    ui.A['jd-decide']({ dataset: { change: 'chg_1', choice: 'use_new' } });
    await ui.A['jd-resolve']({ dataset: {} });
    expect(decisions).toEqual({ chg_1: 'use_new' });
  });

  it('explains an unparsed posting and shows the change history', async () => {
    await open(payload({ status: 'RAW', structured_description: null, requirements: [], parse_task: { status: 'PENDING', attempt_count: 0, last_error: null } }));
    expect(document.body.innerHTML).toContain('queued for parsing');
    expect(document.querySelector('[data-act="jd-edit"]').disabled).toBe(true);
    routes({ 'GET /api/jobs/7/structured-description/history': { items: [{ id: 1, field_path: 'job_identity.seniority', old_value: 'Lead', new_value: 'Staff', change_source: 'USER', created_at: '2026-09-18T11:00:00' }] } });
    await ui.A['jd-history']({ dataset: { id: '7' } });
    expect(document.querySelector('[data-jd-change-source="USER"]').textContent).toContain('job_identity.seniority');
  });
});
