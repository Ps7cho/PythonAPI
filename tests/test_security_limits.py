from fastapi.testclient import TestClient
from sqlalchemy import select
from app.main import app
from app.config import get_settings
from app.database import SessionLocal
from app.models import User
from app.request_limits import limiter


def test_editor_requires_developer_role(client):
    assert client.get('/api/abilities?inspect=true').json()['editor'] == {'can_edit': False}
    assert client.post('/api/catalog-editor', json={'catalog':'abilities','key':{},'values':{}}).status_code == 403
    username = client.get('/api/auth/me').json()['username']
    with SessionLocal.begin() as db:
        db.scalar(select(User).where(User.username == username)).account_type = 'developer'
    assert client.get('/api/abilities?inspect=true').json()['editor']['can_edit']


def test_login_throttle_does_not_lock_other_source(client):
    from app.auth import hasher
    from app.models import LoginAccount
    username = client.get('/api/auth/me').json()['username']
    with SessionLocal.begin() as db:
        db.get(LoginAccount, username).password_hash = hasher.hash('correct-password-123')
    with TestClient(app, client=('attacker', 123)) as attacker:
        for _ in range(5):
            assert attacker.post('/api/auth/login', json={'username':username,'password':'wrong-password-123'}).status_code == 401
        response = attacker.post('/api/auth/login', json={'username':username,'password':'wrong-password-123'})
        assert response.status_code == 429 and 'retry-after' in response.headers
    assert client.post('/api/auth/login', json={'username':username,'password':'correct-password-123'}).status_code == 200


def test_character_quota_and_body_limit(client, monkeypatch):
    monkeypatch.setattr(get_settings(), 'max_characters_per_account', 1)
    assert client.post('/api/adventurers', json={'name':'First'}).status_code == 200
    assert client.post('/api/adventurers', json={'name':'Second'}).status_code == 409
    assert client.post('/api/auth/register', content=b'x'*4097).status_code == 413
    assert client.post('/api/auth/login', content=iter([b'x'*3000,b'x'*3000])).status_code == 413


def test_registration_throttled_before_hashing(client, monkeypatch):
    from app.auth import hasher
    from app.request_limits import limiter
    limiter.clear()
    for _ in range(5):
        limiter.hit(('register', 'testclient'), 5, 3600)
    monkeypatch.setattr(type(hasher), 'hash', lambda *_: (_ for _ in ()).throw(AssertionError('Hashing should not run')))
    assert client.post('/api/auth/register', json={'username':'blocked_user','password':'correct-password-123'}).status_code == 429
