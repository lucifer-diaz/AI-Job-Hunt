import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { createContext, runInContext } from 'node:vm';
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Execute the production scripts together, omitting only automatic network boot.
const settingsSource = readFileSync('app/static/settings.js', 'utf8');
const appSource = readFileSync('app/static/app.js', 'utf8').split('/* ───────────── boot ───────────── */')[0];
const names = ['S', 'D', 'A', 'setF', 'render', 'readHash', 'setHash', 'settingsNavigate', 'settingsChanges', 'settingsPayload', 'settingsDirty', 'settingsCanLeave', 'settingsSave', 'settingsUploadResume', 'settingsUseResume', 'profileCompleteness', 'addTags', 'commitTag', 'SETTING_ERRORS', 'SETTINGS_SECTIONS', 'SETTINGS_ACTIONS'];
let ui, fetchMock, context;
function response(data, status = 200) { return { ok: status < 400, status, text: async () => JSON.stringify(data) }; }
function data() {
  return { profile: { name: 'Alex Morgan', headline: 'Engineering leader', email: 'alex@example.com', phone: '123', location: 'Mumbai', linkedin_url: '', github_url: '', master_resume: 'My resume', target_roles: ['Lead'], preferred_locations: ['Mumbai, India'], work_authorization: 'Citizen', compensation_notes: 'INR 75 lakh', search_preferences: { total_experience: 0, notice_period: 0, primary_skills: ['Python'] } },
    summary: { profile: { name: 'Alex Morgan', headline: 'Engineering leader' }, counts: {}, model: 'default-model', llm_provider: 'openai', application_mode: 'manual_approval', auto_search: { schedule: 'daily', roles: 1, locations: 1, last_run: null } },
    resumes: [], prefs: { weekly_target: 10, followup_days: 7, warn_closing: false },
    settings: { secret_storage: true, keys: Object.fromEntries(['openai', 'openrouter', 'anthropic', 'apify'].map(k => [k, { configured: k === 'openai', hint: k === 'openai' ? '…1234' : '' }])), configured: { openai: true, mock: true }, application_mode: 'manual_approval', min_review_score: 70,
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
    runInContext(readFileSync('app/static/knowledge.js', 'utf8'), context, { filename: 'knowledge.js' });
    runInContext(appSource, context, { filename: resolve('app/static/app.js') });
    ui = runInContext(`({${names.join(',')}})`, context);
  }
  context.fetch = fetchMock; context.confirm = confirm;
  ui.S.forms = {}; ui.S.busy = {}; ui.S.screen = 'settings'; ui.S.settingsSection = 'profile';
  Object.keys(ui.SETTING_ERRORS).forEach(k => delete ui.SETTING_ERRORS[k]);
  Object.assign(ui.D, data(), { settingsLoadError: '' }); ui.render();
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
    const summary = { ...ui.D.summary, auto_search: { ...ui.D.summary.auto_search, roles: 2, locations: 3 } };
    fetchMock.mockResolvedValueOnce(response({ ...ui.D.profile, target_roles: ['Staff', 'Lead'] })).mockResolvedValueOnce(response(summary));
    await ui.settingsSave('preferences');
    expect(document.getElementById('side-foot').textContent).toContain('2 roles × 3 locations');
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
