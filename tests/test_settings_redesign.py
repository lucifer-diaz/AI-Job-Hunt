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
    cleared = client.patch('/api/profile', json={'target_roles': [], 'master_resume': ''}).json()
    assert cleared['target_roles'] == [] and cleared['master_resume'] == ''


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
