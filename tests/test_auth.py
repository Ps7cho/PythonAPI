import hashlib
from datetime import datetime, timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
from app.main import app
from app.database import SessionLocal
from app.models import LoginAccount, LoginSession


def credentials():
    return {'username': 'player_' + uuid4().hex[:16], 'password': 'correct-password-123'}


def test_login_logout_expiry_and_password_storage():
    payload = credentials()
    with TestClient(app) as client:
        assert client.get('/api/auth/me').status_code == 401
        registered = client.post('/api/auth/register', json=payload)
        assert registered.status_code == 201
        assert 'HttpOnly' in registered.headers['set-cookie']
        assert 'SameSite=strict' in registered.headers['set-cookie']
        token = registered.json()['access_token']
        assert client.get('/api/auth/me').json()['username'] == payload['username']
        with SessionLocal() as db:
            stored = db.get(LoginAccount, payload['username'])
            assert stored.password_hash.startswith('$argon2id$')
            assert stored.password_hash != payload['password']
            assert db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
        assert client.post('/api/auth/logout').status_code == 204
        assert client.get('/api/auth/me', headers={'Authorization': 'Bearer ' + token}).status_code == 401
        assert client.post('/api/auth/login', json={**payload, 'password': 'wrong-password-123'}).status_code == 401
        login = client.post('/api/auth/login', json={**payload, 'username': payload['username'].upper()})
        assert login.status_code == 200
        token_hash = hashlib.sha256(login.json()['access_token'].encode()).hexdigest()
        with SessionLocal.begin() as db:
            db.get(LoginSession, token_hash).expires_at = datetime.utcnow() - timedelta(seconds=1)
        assert client.get('/api/auth/me').status_code == 401


def test_registration_validation_duplicates_and_origin():
    payload = credentials()
    with TestClient(app) as client:
        assert client.post('/api/auth/register', json={**payload, 'password': 'short'}).status_code == 422
        assert client.post('/api/auth/register', json=payload, headers={'Origin': 'https://foreign.example'}).status_code == 403
        assert client.post('/api/auth/register', json=payload).status_code == 201
        assert client.post('/api/auth/register', json={**payload, 'username': payload['username'].upper()}).status_code == 409
        assert client.post('/api/auth/logout', headers={'Origin': 'https://foreign.example'}).status_code == 403


def test_ownership_and_bearer_access():
    with TestClient(app) as first, TestClient(app) as second, TestClient(app) as anonymous:
        account = first.post('/api/auth/register', json=credentials()).json()
        second.post('/api/auth/register', json=credentials())
        hero = first.post('/api/adventurers', json={'name': 'My hero'}).json()
        encounter = first.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
        assert hero['owner'] == account['user']['id']
        assert second.get('/api/adventurers').json() == []
        assert anonymous.post('/api/adventurers', json={'name': 'No session'}).status_code == 401
        assert anonymous.get('/api/adventurers', headers={'Authorization': 'Bearer ' + account['access_token']}).status_code == 200
        assert second.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).status_code == 404
        assert second.get('/api/encounters/' + encounter['id']).status_code == 404
        assert second.post('/api/encounters/' + encounter['id'] + '/actions', json={
            'actor_id': hero['id'], 'expected_turn': 1, 'action': 'attack',
        }).status_code == 404


def test_failed_login_throttle():
    payload = credentials()
    with TestClient(app) as client:
        client.post('/api/auth/register', json=payload)
        client.post('/api/auth/logout')
        for _ in range(5):
            assert client.post('/api/auth/login', json={**payload, 'password': 'wrong-password-123'}).status_code == 401
        assert client.post('/api/auth/login', json=payload).status_code == 429
