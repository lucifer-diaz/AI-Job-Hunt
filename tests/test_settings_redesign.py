"""Independent settings editors and secure password rotation."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import auth
from app.db import SessionLocal
from app.main import app
from app.models import User, UserSession
from app.security import verify_password

OLD = 'old password for testing'
NEW = 'new password for testing'


@pytest.fixture
def account(anonymous):
    client = TestClient(app)
    email = f'settings-{uuid.uuid4().hex}@example.com'
    result = client.post('/api/auth/signup', json={
        'email': email, 'password': OLD, 'confirm_password': OLD})
    assert result.status_code == 201
    return client, email, result.json()['user']['id']


def password_body(**overrides):
    return {'current_password': OLD, 'new_password': NEW,
            'confirm_password': NEW, **overrides}


def test_profile_patch_preserves_other_sections(account):
    client, _, _ = account
    initial = {'name': 'Before', 'headline': 'Engineer', 'master_resume': 'Newest resume',
               'target_roles': ['Lead'], 'search_preferences': {
                   'primary_skills': ['Python'], 'notice_period': '30',
                   'search_profile': {'schedule': 'daily'}}}
    assert client.put('/api/profile', json=initial).status_code == 200
    assert client.patch('/api/profile', json={'name': 'After'}).status_code == 200
    result = client.patch('/api/profile', json={'search_preferences': {'notice_period': '0'}}).json()
    assert result['name'] == 'After' and result['headline'] == 'Engineer'
    assert result['master_resume'] == 'Newest resume'
    assert result['target_roles'] == ['Lead']
    assert result['search_preferences'] == {**initial['search_preferences'], 'notice_period': '0'}
    # Blank text is not a resume and never clears one: refused, and nothing in the request is saved.
    refused = client.patch('/api/profile', json={'target_roles': [], 'master_resume': ''})
    assert refused.status_code == 422 and 'delete it' in refused.text
    kept = client.get('/api/profile').json()
    assert kept['target_roles'] == ['Lead'] and kept['master_resume'] == 'Newest resume'
    cleared = client.patch('/api/profile', json={'target_roles': []}).json()
    assert cleared['target_roles'] == [] and 'created_resume_id' not in cleared


@pytest.mark.parametrize('body', [{'name': None}, {'master_resume': None},
                                 {'user_id': 100}, {'search_preferences': None}])
def test_profile_patch_rejects_invalid_fields(account, body):
    assert account[0].patch('/api/profile', json=body).status_code == 422


def test_settings_patch_merges_stages_and_preserves_workflow(account):
    client, _, _ = account
    client.put('/api/preferences', json={'weekly_target': 15, 'warn_closing': False})
    client.put('/api/settings', json={'llm_provider': 'openrouter', 'model': 'default:free',
                                     'stage_models': {'rank': 'rank:free', 'tailor': 'tailor:free'},
                                     'application_mode': 'automatic', 'min_review_score': 85})
    result = client.patch('/api/settings', json={'stage_models': {'rank': ''}}).json()
    assert result['ai']['saved']['stage_models'] == {'tailor': 'tailor:free'}
    assert result['application_mode'] == 'automatic' and result['min_review_score'] == 85
    result = client.patch('/api/settings', json={'min_review_score': 0}).json()
    assert result['ai']['saved']['model'] == 'default:free' and result['min_review_score'] == 0
    result = client.patch('/api/settings', json={'llm_provider': '', 'model': '', 'require_free': None}).json()
    assert result['ai']['saved']['model'] == ''
    assert result['ai']['saved']['require_free'] is None
    prefs = client.put('/api/preferences', json={'followup_days': 12}).json()
    assert prefs['weekly_target'] == 15 and prefs['warn_closing'] is False
    assert client.get('/api/settings').json()['ai']['saved']['stage_models'] == {'tailor': 'tailor:free'}


@pytest.mark.parametrize('body', [{'application_mode': 'auto_submit'}, {'llm_provider': 'bad'},
                                 {'stage_models': {'unknown': 'model'}}, {'min_review_score': 101},
                                 {'model': None}, {'other': True}])
def test_settings_patch_validation(account, body):
    assert account[0].patch('/api/settings', json=body).status_code == 422


def test_patch_requires_signin_and_isolates_accounts(account):
    client, _, _ = account
    anon = TestClient(app)
    assert anon.patch('/api/profile', json={'name': 'Bad'}).status_code == 401
    assert anon.patch('/api/settings', json={'model': 'Bad'}).status_code == 401
    email = f'other-{uuid.uuid4().hex}@example.com'
    other = TestClient(app)
    other.post('/api/auth/signup', json={'email': email, 'password': OLD, 'confirm_password': OLD})
    client.patch('/api/profile', json={'name': 'Private'})
    client.patch('/api/settings', json={'model': 'private-model'})
    assert other.get('/api/profile').json()['name'] == ''
    assert other.get('/api/settings').json()['ai']['saved']['model'] == ''


def test_password_rotation_revokes_all_old_sessions(account):
    client, email, user_id = account
    other = TestClient(app)
    assert other.post('/api/auth/login', json={'email': email, 'password': OLD}).status_code == 200
    old_token = client.cookies.get(auth.SESSION_COOKIE)
    result = client.post('/api/auth/password', json=password_body())
    assert result.status_code == 200 and result.json() == {'ok': True}
    assert 'httponly' in result.headers['set-cookie'].lower()
    assert client.cookies.get(auth.SESSION_COOKIE) != old_token
    assert client.get('/api/auth/me').status_code == 200
    assert other.get('/api/auth/me').status_code == 401
    replay = TestClient(app)
    replay.cookies.set(auth.SESSION_COOKIE, old_token)
    assert replay.get('/api/auth/me').status_code == 401
    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert verify_password(NEW, user.password_hash)
        assert not verify_password(OLD, user.password_hash)
        assert len(db.scalars(select(UserSession).where(UserSession.user_id == user_id)).all()) == 1
    assert other.post('/api/auth/login', json={'email': email, 'password': OLD}).status_code == 401
    assert other.post('/api/auth/login', json={'email': email, 'password': NEW}).status_code == 200


@pytest.mark.parametrize('overrides', [
    {'new_password': 'short', 'confirm_password': 'short'},
    {'confirm_password': 'not a matching password'},
    {'new_password': OLD, 'confirm_password': OLD},
    {'new_password': 'x' * 257, 'confirm_password': 'x' * 257},
    {'current_password': ''}, {'extra': 'secret value'},
])
def test_password_validation_does_not_echo_secrets(account, overrides):
    result = account[0].post('/api/auth/password', json=password_body(**overrides))
    assert result.status_code == 422
    assert OLD not in result.text and NEW not in result.text and 'secret value' not in result.text


def test_password_throttle_and_auth(account, monkeypatch):
    client, _, user_id = account
    assert TestClient(app).post('/api/auth/password', json=password_body()).status_code == 401
    assert client.post('/api/auth/password', json=password_body(),
                       headers={'Origin': 'https://attacker.example'}).status_code == 403
    for _ in range(auth.LOGIN_FAILURE_LIMIT):
        assert client.post('/api/auth/password', json=password_body(current_password='wrong')).status_code == 403
    assert client.post('/api/auth/password', json=password_body()).status_code == 429
    # Expiry of the existing process-local throttle restores access.
    auth._failures[f'password:{user_id}'].clear()
    assert client.post('/api/auth/password', json=password_body()).status_code == 200


def test_password_transaction_rolls_back_on_failure(account, monkeypatch):
    client, _, user_id = account
    from sqlalchemy.orm import Session
    def fail_commit(self):
        raise RuntimeError('simulated commit failure')
    monkeypatch.setattr(Session, 'commit', fail_commit)
    with pytest.raises(RuntimeError, match='simulated commit failure'):
        client.post('/api/auth/password', json=password_body())
    with SessionLocal() as db:
        assert verify_password(OLD, db.get(User, user_id).password_hash)
    assert client.get('/api/auth/me').status_code == 200


def test_compare_and_swap_rejects_a_stale_password_change(account):
    _, _, user_id = account
    with SessionLocal() as stale, SessionLocal() as fresh:
        old_user = stale.get(User, user_id)
        current_user = fresh.get(User, user_id)
        auth._replace_password_session(fresh, current_user, NEW)
        fresh.commit()
        with pytest.raises(Exception) as error:
            auth._replace_password_session(stale, old_user, 'another new password')
        assert error.value.status_code == 409


@pytest.mark.parametrize('kind', ['profile', 'settings'])
def test_concurrent_nested_edits_take_a_write_lock(account, kind):
    """A second editor must merge after the first editor has committed."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from app.services.user_settings import lock_settings_row, profile_for, settings_for

    _, _, user_id = account
    factory = profile_for if kind == 'profile' else settings_for
    field = 'search_preferences' if kind == 'profile' else 'preferences'
    held, attempting, release, acquired = Event(), Event(), Event(), Event()
    with SessionLocal() as db:
        factory(db, user_id)

    def first():
        with SessionLocal() as db:
            db.info['user_id'] = user_id
            row = lock_settings_row(db, factory(db, user_id))
            held.set()
            assert release.wait(3)
            setattr(row, field, {**(getattr(row, field) or {}), 'first_editor': True})
            db.commit()

    def second():
        assert held.wait(3)
        with SessionLocal() as db:
            db.info['user_id'] = user_id
            row = factory(db, user_id)
            attempting.set()
            row = lock_settings_row(db, row)
            acquired.set()
            assert getattr(row, field)['first_editor'] is True
            setattr(row, field, {**getattr(row, field), 'second_editor': True})
            db.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(first), pool.submit(second)
        try:
            assert attempting.wait(3)
            assert not acquired.wait(0.1), 'second writer must wait for the first transaction'
        finally:
            release.set()
        a.result(timeout=5)
        b.result(timeout=5)
    with SessionLocal() as db:
        value = getattr(factory(db, user_id), field)
        assert value['first_editor'] and value['second_editor']


def test_login_verified_before_rotation_cannot_create_a_session_afterward(account, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    client, email, user_id = account
    attacker = TestClient(app)
    verified, release = Event(), Event()
    original_verify = auth.verify_password

    def pause_after_verification(password, stored):
        valid = original_verify(password, stored)
        if password == OLD and not verified.is_set():
            verified.set()
            assert release.wait(5)
        return valid

    monkeypatch.setattr(auth, 'verify_password', pause_after_verification)
    with ThreadPoolExecutor(max_workers=1) as pool:
        login = pool.submit(attacker.post, '/api/auth/login', json={'email': email, 'password': OLD})
        try:
            assert verified.wait(3)
            assert client.post('/api/auth/password', json=password_body()).status_code == 200
        finally:
            release.set()
        result = login.result(timeout=5)
    assert result.status_code == 401
    assert 'set-cookie' not in result.headers
    assert attacker.get('/api/auth/me').status_code == 401
    assert client.get('/api/auth/me').status_code == 200
    with SessionLocal() as db:
        assert len(db.scalars(select(UserSession).where(UserSession.user_id == user_id)).all()) == 1


def test_rotation_waits_for_an_inflight_login_and_revokes_its_session(account, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    client, email, _ = account
    attacker = TestClient(app)
    creating_session, release, rotating = Event(), Event(), Event()
    original_start = auth.start_session

    def pause_session_creation(db, user_id):
        creating_session.set()
        assert release.wait(5)
        return original_start(db, user_id)

    def rotate():
        rotating.set()
        return client.post('/api/auth/password', json=password_body())

    monkeypatch.setattr(auth, 'start_session', pause_session_creation)
    with ThreadPoolExecutor(max_workers=2) as pool:
        login = pool.submit(attacker.post, '/api/auth/login', json={'email': email, 'password': OLD})
        try:
            assert creating_session.wait(3)
            rotation = pool.submit(rotate)
            assert rotating.wait(3)
            # The account lock must span session insertion, not only last_login_at.
            from concurrent.futures import TimeoutError
            with pytest.raises(TimeoutError):
                rotation.result(timeout=0.15)
        finally:
            release.set()
        assert login.result(timeout=5).status_code == 200
        assert rotation.result(timeout=5).status_code == 200
    assert attacker.get('/api/auth/me').status_code == 401
    assert client.get('/api/auth/me').status_code == 200


def test_password_scrypt_does_not_block_other_accounts_login(account, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    _, _, user_id = account
    other = TestClient(app)
    email = f'independent-{uuid.uuid4().hex}@example.com'
    assert other.post('/api/auth/signup', json={
        'email': email, 'password': OLD, 'confirm_password': OLD}).status_code == 201
    with SessionLocal() as db:
        user = db.get(User, user_id)
    verifying, release = Event(), Event()
    original_verify = auth.verify_password

    def slow_password_check(password, stored):
        if stored == user.password_hash:
            verifying.set()
            assert release.wait(5)
        return original_verify(password, stored)

    monkeypatch.setattr(auth, 'verify_password', slow_password_check)
    with ThreadPoolExecutor(max_workers=2) as pool:
        check = pool.submit(auth._check_current_password, user, OLD)
        try:
            assert verifying.wait(3)
            login = pool.submit(other.post, '/api/auth/login', json={'email': email, 'password': OLD})
            assert login.result(timeout=2).status_code == 200
        finally:
            release.set()
        check.result(timeout=5)


def test_login_session_failure_rolls_back_last_login_update(account, monkeypatch):
    _, email, user_id = account
    with SessionLocal() as db:
        before = db.get(User, user_id).last_login_at

    def fail_session(db, user_id):
        raise RuntimeError('session insert failed')

    monkeypatch.setattr(auth, 'start_session', fail_session)
    with pytest.raises(RuntimeError, match='session insert failed'):
        TestClient(app).post('/api/auth/login', json={'email': email, 'password': OLD})
    with SessionLocal() as db:
        assert db.get(User, user_id).last_login_at == before
        assert len(db.scalars(select(UserSession).where(UserSession.user_id == user_id)).all()) == 1


def test_deleting_the_only_file_keeps_the_active_resume_and_the_saved_text(account):
    client, _, _ = account
    uploaded = client.post('/api/profile/resume', files={
        'file': ('only.txt', b'Uploaded text', 'text/plain')})
    assert uploaded.status_code == 200 and uploaded.json()['is_active'] is True, 'the first resume is used'
    edited = client.patch('/api/profile', json={'master_resume': 'Edited saved text'}).json()
    assert edited['created_resume_active'] is False and edited['master_resume'] == 'Uploaded text'
    deleted = client.delete(f"/api/profile/resumes/{uploaded.json()['id']}")
    assert deleted.status_code == 200
    assert deleted.json()['promoted'] is None
    assert deleted.json()['resumes'] == []
    # The file is gone; its resume version (still the active one) and the edited text's version are not.
    assert client.get('/api/profile').json()['master_resume'] == 'Uploaded text'
    versions = client.get('/api/settings/resume').json()['versions']
    assert [(v['source'], v['is_active']) for v in versions] == [('manual_text', False), ('upload', True)]
