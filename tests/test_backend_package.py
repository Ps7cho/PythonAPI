from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app


def test_backend_without_frontend_files():
    with TestClient(app) as client:
        for url in ('/', '/main', '/ui/ui.js', '/ui/ui.css', '/ui/debug.js',
                    '/journey-ui.js', '/adventurers/' + str(uuid4())):
            response = client.get(url)
            assert response.status_code == 404
            assert 'hosted separately' in response.json()['detail']
        assert client.get('/health').status_code == 200
        assert client.get('/docs').status_code == 200
        assert client.get('/api/abilities').status_code == 200
