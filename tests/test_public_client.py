from uuid import uuid4

from fastapi.testclient import TestClient
from app.main import app


ORIGIN = 'http://127.0.0.1:5173'


def test_public_origin_login_uses_bearer_without_cookie():
    with TestClient(app) as browser:
        preflight = browser.options('/api/auth/register', headers={
            'Origin': ORIGIN, 'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'content-type,x-client-auth',
        })
        assert preflight.status_code == 200
        assert preflight.headers['access-control-allow-origin'] == ORIGIN
        assert 'access-control-allow-credentials' not in preflight.headers
        credentials = {'username': 'public_' + uuid4().hex[:16], 'password': 'test-public-password'}
        registration = browser.post('/api/auth/register', json=credentials,
                                    headers={'Origin': ORIGIN, 'X-Client-Auth': 'bearer'})
        assert registration.status_code == 201
        assert 'set-cookie' not in registration.headers
        token = registration.json()['access_token']
        headers = {'Origin': ORIGIN, 'Authorization': 'Bearer ' + token}
        assert browser.get('/api/auth/me', headers=headers).status_code == 200
        assert browser.post('/api/adventurers', json={'name':'Public hero'}, headers=headers).status_code == 200
        assert browser.post('/api/auth/logout', headers=headers).status_code == 204
        assert browser.get('/api/auth/me', headers=headers).status_code == 401
        login = browser.post('/api/auth/login', json=credentials, headers={'Origin': ORIGIN, 'X-Client-Auth':'bearer'})
        assert login.status_code == 200


def test_foreign_origins_and_cookie_mutations_remain_rejected(client):
    denied = client.options('/api/auth/login', headers={
        'Origin':'https://unlisted.example', 'Access-Control-Request-Method':'POST',
    })
    assert 'access-control-allow-origin' not in denied.headers
    assert client.post('/api/auth/login', json={'username':'nobody', 'password':'test-password-123'},
                       headers={'Origin':'https://unlisted.example'}).status_code == 403
    assert client.post('/api/adventurers', json={'name':'Cookie attempt'},
                       headers={'Origin':ORIGIN}).status_code == 403
