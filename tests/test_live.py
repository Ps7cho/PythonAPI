from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect


def account(client):
    data = client.post('/api/auth/register', json={'username': 'live_' + uuid4().hex[:16],
                                                'password': 'live-test-password'}).json()
    token = data['access_token']
    headers = {'Authorization': 'Bearer ' + token}
    hero = client.post('/api/adventurers', headers=headers, json={'name': 'Live hero'}).json()['id']
    return token, headers, hero


def test_push_both_players_and_reconnect_snapshot(client, monkeypatch):
    # StaticPool shares one SQLite connection; PostgreSQL uses independent sessions.
    import threading
    from app import live
    original = live.authorized_snapshot
    lock = threading.Lock()
    def serialized(*args):
        with lock:
            return original(*args)
    monkeypatch.setattr(live, 'authorized_snapshot', serialized)
    original_post = client.post
    def post(*args, **kwargs):
        with lock:
            return original_post(*args, **kwargs)
    monkeypatch.setattr(client, 'post', post)
    token, leader, hero = account(client)
    guest_token, guest, other = account(client)
    party = client.post('/api/parties', headers=leader, json={'adventurer_id': hero, 'name': 'Live'}).json()
    path = '/api/parties/' + party['id']
    client.post('/api/parties/join', headers=guest, json={'adventurer_id': other, 'code': party['invite']['code']})
    revision = client.post(path + '/selection', headers=leader, json={'encounter_count': 3}).json()['selection_revision']
    for headers, actor in ((leader, hero), (guest, other)):
        client.post(path + '/ready', headers=headers, json={'adventurer_id': actor, 'ready': True, 'selection_revision': revision})
    encounter = client.post(path + '/encounters', headers=leader, json={'selection_revision': revision}).json()
    url = '/api/encounters/' + encounter['id']
    with client.websocket_connect(url + '/live') as first, client.websocket_connect(url + '/live') as second:
        first.send_json({'token': token})
        second.send_json({'token': guest_token})
        assert first.receive_json()['data']['turn'] == 1
        assert second.receive_json()['data']['turn'] == 1
        for headers, actor in ((leader, hero), (guest, other)):
            result = client.post(url + '/actions', headers=headers, json={'actor_id': actor, 'expected_turn': 1, 'action': 'wait'})
            assert result.status_code == 200
            for stream in (first, second):
                update = stream.receive_json()['data']
                assert update['revision'] == result.json()['revision']
                assert update['pending_actor_ids'] == result.json()['pending_actor_ids']
        assert update['turn'] == 2 and set(update['pending_actor_ids']) == {hero, other}
    with client.websocket_connect(url + '/live') as stream:
        stream.send_json({'token': guest_token})
        data = stream.receive_json()['data']
        assert data['turn'] == 2
        for _ in range(30):
            if data['state'] != 'player_turn':
                break
            actor = data['pending_actor_ids'][0]
            result = client.post(url + '/actions', headers=leader if actor == hero else guest,
                                 json={'actor_id': actor, 'expected_turn': data['turn'], 'action': 'attack'})
            assert result.status_code == 200
            data = stream.receive_json()['data']
        assert data['state'] == 'victory'
        result = client.post(url + '/continue', headers=leader, json={})
        assert result.status_code == 200
        data = stream.receive_json()['data']
        assert data['id'] == result.json()['id'] and data['id'] != encounter['id']
        assert data['quest']['encounter_number'] == 2
    stranger, _, _ = account(client)
    with client.websocket_connect(url + '/live') as stream:
        stream.send_json({'token': stranger})
        with pytest.raises(WebSocketDisconnect):
            stream.receive_json()


def test_live_rejects_cross_origin_cookie_access(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/api/encounters/' + str(uuid4()) + '/live', headers={'origin': 'https://untrusted.example'}):
            pass


def test_village_updates_are_private_and_follow_party_changes(client, monkeypatch):
    import threading
    from app import live
    original = live.village_snapshot
    lock = threading.Lock()
    def serialized(*args):
        with lock:
            return original(*args)
    monkeypatch.setattr(live, 'village_snapshot', serialized)
    original_post = client.post
    def post(*args, **kwargs):
        with lock:
            return original_post(*args, **kwargs)
    monkeypatch.setattr(client, 'post', post)
    token, leader, hero = account(client)
    guest_token, guest, other = account(client)
    party = client.post('/api/parties', headers=leader, json={'adventurer_id': hero, 'name': 'Village live'}).json()
    path = '/api/parties/' + party['id']
    with client.websocket_connect('/api/village/live') as first, client.websocket_connect('/api/village/live') as second:
        first.send_json({'token': token})
        second.send_json({'token': guest_token})
        data = first.receive_json()['data']
        assert {h['id'] for h in data['adventurers']} == {hero}
        data = second.receive_json()['data']
        assert {h['id'] for h in data['adventurers']} == {other} and not data['parties']
        client.post('/api/parties/join', headers=guest, json={'adventurer_id': other, 'code': party['invite']['code']})
        for stream in (first, second):
            data = stream.receive_json()['data']
            assert len(data['parties'][0]['members']) == 2
            assert 'invite' not in data['parties'][0]
        selected = client.post(path + '/selection', headers=leader, json={'encounter_count': 3}).json()
        for stream in (first, second):
            assert stream.receive_json()['data']['parties'][0]['selection_revision'] == selected['selection_revision']
        client.post(path + '/ready', headers=guest, json={'adventurer_id': other, 'ready': True, 'selection_revision': selected['selection_revision']})
        for stream in (first, second):
            members = stream.receive_json()['data']['parties'][0]['members']
            assert next(m for m in members if m['id'] == other)['is_ready']
    with client.websocket_connect('/api/village/live?adventurer_id=' + hero) as stream:
        stream.send_json({'token': guest_token})
        with pytest.raises(WebSocketDisconnect):
            stream.receive_json()
    with client.websocket_connect('/api/village/live?adventurer_id=' + hero) as stream:
        stream.send_json({'token': token})
        assert stream.receive_json()['data']['adventurer']['id'] == hero


def test_village_notices_require_committed_changes(client, monkeypatch):
    from uuid import UUID
    from app import live
    from app.database import SessionLocal
    from app.models import Adventurer
    _, headers, hero = account(client)
    topics = []
    monkeypatch.setattr(live.hub, 'publish', topics.append)
    assert client.get('/api/adventurers', headers=headers).status_code == 200
    assert topics == []
    with SessionLocal() as db:
        adventurer = db.get(Adventurer, UUID(hero))
        adventurer.gold += 1
        db.flush()
        db.rollback()
    assert topics == []
    with SessionLocal.begin() as db:
        adventurer = db.get(Adventurer, UUID(hero))
        adventurer.gold += 1
        owner = str(adventurer.owner)
    assert topics == ['village:' + owner]
