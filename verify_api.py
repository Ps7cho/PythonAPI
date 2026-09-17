"""Exercise the state API with an existing login account."""
from getpass import getpass

from fastapi.testclient import TestClient
from app.main import app


def main():
    username = input('Username: ')
    password = getpass('Password: ')
    with TestClient(app) as client:
        response = client.post('/api/auth/login', json={'username': username, 'password': password})
        if response.status_code != 200:
            raise SystemExit('Login failed.')
        user_id = response.json()['user']['id']
        print(client.get('/health').status_code)
        print(client.post('/states', json={'user_id': user_id, 'payload': {
            'hp': 100, 'inventory': [], 'location': 'spawn', 'xp': 0,
        }}).json())
        print(client.post(f'/states/{user_id}/events', json={
            'event_type': 'move', 'payload': {'location': 'forest'},
        }).json())
        print(client.get(f'/states/{user_id}/version').json())
        client.post('/api/auth/logout')


if __name__ == '__main__':
    main()
