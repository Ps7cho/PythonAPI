from datetime import datetime, timedelta
from uuid import UUID, uuid4

from app.database import SessionLocal
from app.models import PartyInvite


def account(client):
    token = client.post('/api/auth/register', json={'username': 'party_' + uuid4().hex[:16],
                                                   'password': 'party-test-password'}).json()['access_token']
    headers = {'Authorization': 'Bearer ' + token}
    hero = client.post('/api/adventurers', headers=headers, json={'name': 'Party hero'}).json()
    return headers, hero['id']


def choose(client, url, leader, hero, **selection):
    response = client.post(url + '/selection', headers=leader, json=selection)
    assert response.status_code == 200, response.text
    revision = response.json()['selection_revision']
    assert client.post(url + '/ready', headers=leader, json={'adventurer_id': hero, 'ready': True, 'selection_revision': revision}).status_code == 200
    return revision


def test_invite_join_and_shared_quest_preserve_actor_ownership(client):
    leader, hero = account(client)
    guest, other = account(client)
    response = client.post('/api/parties', headers=leader, json={'adventurer_id': hero, 'name': 'Friends'})
    assert response.status_code == 201
    party = response.json()
    url = '/api/parties/' + party['id']
    code = party['invite']['code']
    assert response.headers['cache-control'] == 'no-store'
    assert client.post('/api/parties/join', headers=guest, json={'adventurer_id': hero, 'code': code}).status_code == 404
    for _ in range(2):
        joined = client.post('/api/parties/join', headers=guest, json={'adventurer_id': other, 'code': ' ' + code.lower() + ' '})
        assert joined.status_code == 200
        assert len(joined.json()['members']) == 2
    listing = client.get('/api/parties', headers=guest).json()
    assert listing[0]['id'] == party['id'] and 'invite' not in listing[0]
    assert listing[0]['members'][1]['is_ready'] is False
    revision = choose(client, url, leader, hero, encounter_count=3)
    ready = client.post(url + '/ready?ready=true', headers=guest, json={'selection_revision': revision})
    assert ready.status_code == 200 and ready.json()['is_ready'] is True
    assert client.post(url + '/invite', headers=guest).status_code == 403
    assert client.post(url + '/encounters', headers=guest, json={}).status_code == 403
    response = client.post(url + '/encounters', headers=leader, json={'selection_revision': revision})
    assert response.status_code == 201
    encounter = response.json()
    action_url = '/api/encounters/' + encounter['id'] + '/actions'
    assert client.get('/api/encounters/' + encounter['id'], headers=guest).status_code == 200
    assert client.get('/api/adventurers', headers=guest).json()[0]['active_encounter_id'] == encounter['id']
    assert client.post(action_url, headers=guest, json={'actor_id': hero, 'expected_turn': 1, 'action': 'attack'}).status_code == 404
    late_hero = client.post('/api/adventurers', headers=guest, json={'name': 'Late join'}).json()['id']
    assert client.post('/api/parties/join', headers=guest, json={'adventurer_id': late_hero, 'code': code}).status_code == 409
    for turn in range(1, 7):
        for headers, actor in ((leader, hero), (guest, other)):
            response = client.post(action_url, headers=headers, json={'actor_id': actor, 'expected_turn': turn, 'action': 'attack'})
            assert response.status_code == 200, response.text
    assert response.json()['state'] == 'victory'
    next_url = '/api/encounters/' + encounter['id'] + '/continue'
    assert client.post(next_url, headers=guest).status_code == 403
    assert client.post(next_url, headers=leader).status_code == 200


def test_rotation_expiry_capacity_and_authentication(client):
    leader, hero = account(client)
    guest, other = account(client)
    party = client.post('/api/parties', headers=leader, json={'adventurer_id': hero, 'name': 'Codes'}).json()
    old = party['invite']['code']
    invite_url = '/api/parties/' + party['id'] + '/invite'
    new = client.post(invite_url, headers=leader).json()['code']
    assert old != new
    assert client.post('/api/parties/join', headers=guest, json={'adventurer_id': other, 'code': old}).status_code == 404
    with SessionLocal.begin() as db:
        invite = db.get(PartyInvite, UUID(party['id']))
        assert invite.code_hash != new and len(invite.code_hash) == 64
        invite.expires_at = datetime.utcnow() - timedelta(seconds=1)
    assert client.post('/api/parties/join', headers=guest, json={'adventurer_id': other, 'code': new}).status_code == 404
    code = client.post(invite_url, headers=leader).json()['code']
    for i in range(6):
        actor = client.post('/api/adventurers', headers=guest, json={'name': 'Member ' + str(i)}).json()['id']
        response = client.post('/api/parties/join', headers=guest, json={'adventurer_id': actor, 'code': code})
        assert response.status_code == (200 if i < 5 else 409)
    client.post('/api/auth/logout')
    assert client.get('/api/parties').status_code == 401
    assert client.post('/api/parties/join', json={'adventurer_id': other, 'code': code}).status_code == 401


def test_roster_readiness_and_targeted_character_changes(client):
    leader, hero = account(client)
    guest, other = account(client)
    party = client.post('/api/parties', headers=leader, json={'adventurer_id': hero, 'name': 'Together'}).json()
    url = '/api/parties/' + party['id']
    revision = choose(client, url, leader, hero)
    joined = client.post('/api/parties/join', headers=guest, json={'adventurer_id': other, 'code': party['invite']['code']}).json()
    assert not joined['all_ready']
    members = {m['id']: m for m in joined['members']}
    assert members[other]['is_yours'] and not members[hero]['is_yours']
    assert members[other]['player'].startswith('party_')
    assert members[other]['owner_id'] != members[hero]['owner_id']
    assert members[other]['is_online'] is False
    assert members[hero]['is_ready'] and members[other]['is_alive']
    assert members[other]['health'] > 0
    assert client.post(url + '/encounters', headers=leader, json={}).status_code == 409
    assert client.post(url + '/ready', headers=guest, json={'adventurer_id': hero, 'ready': True}).status_code == 403
    ready = client.post(url + '/ready', headers=guest, json={'adventurer_id': other, 'ready': True, 'selection_revision': revision})
    assert ready.status_code == 200 and ready.json()['all_ready']
    assert not client.post(url + '/ready', headers=leader, json={'adventurer_id': hero, 'ready': False, 'selection_revision': revision}).json()['all_ready']
    assert client.post(url + '/encounters', headers=leader, json={}).status_code == 409
    assert client.post(url + '/ready?ready=true', headers=leader, json={'selection_revision': revision}).json()['all_ready']
    encounter = client.post(url + '/encounters', headers=leader, json={'selection_revision': revision})
    assert encounter.status_code == 201
    assert client.post(url + '/ready', headers=guest, json={'adventurer_id': other, 'ready': False}).status_code == 409
    listing_response = client.get('/api/parties', headers=guest)
    assert listing_response.headers['cache-control'] == 'no-store'
    listing = listing_response.json()
    assert listing[0]['active_encounter_id'] == encounter.json()['id']


def test_same_name_and_members_do_not_make_parties_identical(client):
    leader, hero = account(client)
    guest, other = account(client)
    parties = []
    for _ in range(2):
        party = client.post('/api/parties', headers=leader,
                            json={'adventurer_id': hero, 'name': 'Same name'}).json()
        client.post('/api/parties/join', headers=guest,
                    json={'adventurer_id': other, 'code': party['invite']['code']})
        parties.append(party)
        party['selection_revision'] = choose(client, '/api/parties/' + party['id'], leader, hero)
    client.post('/api/parties/' + parties[1]['id'] + '/ready', headers=guest,
                json={'adventurer_id': other, 'ready': True, 'selection_revision': parties[1]['selection_revision']})
    for headers in (leader, guest):
        rows = {p['id']: p for p in client.get('/api/parties', headers=headers).json()}
        assert not rows[parties[0]['id']]['all_ready']
        assert rows[parties[1]['id']]['all_ready']


def test_selection_requires_fresh_consent_and_departure_uses_approved_plan(client):
    leader, hero = account(client)
    guest, other = account(client)
    party = client.post('/api/parties', headers=leader, json={'adventurer_id': hero, 'name': 'Consent'}).json()
    url = '/api/parties/' + party['id']
    client.post('/api/parties/join', headers=guest, json={'adventurer_id': other, 'code': party['invite']['code']})
    assert client.post(url + '/ready', headers=guest, json={'ready': True}).status_code == 409
    assert client.post(url + '/encounters', headers=leader, json={}).status_code == 409
    assert client.post(url + '/selection', headers=guest, json={}).status_code == 403
    assert client.post(url + '/selection', headers=leader, json={'enemy_slug': 'missing'}).status_code == 404
    old = choose(client, url, leader, hero)
    client.post(url + '/ready', headers=guest, json={'adventurer_id': other, 'ready': True, 'selection_revision': old})
    changed = client.post(url + '/selection', headers=leader, json={'encounter_count': 3}).json()
    assert changed['selection_revision'] != old
    assert not any(member['is_ready'] for member in changed['members'])
    assert client.post(url + '/ready', headers=guest, json={'ready': True, 'selection_revision': old}).status_code == 409
    assert client.post(url + '/encounters', headers=leader, json={'selection_revision': old}).status_code == 409
    revision = changed['selection_revision']
    assert client.post(url + '/encounters', headers=leader, json={'selection_revision': revision}).status_code == 409
    for headers, actor in ((leader, hero), (guest, other)):
        assert client.post(url + '/ready', headers=headers, json={'adventurer_id': actor, 'ready': True, 'selection_revision': revision}).status_code == 200
    unchanged = client.post(url + '/selection', headers=leader, json={'encounter_count': 3}).json()
    assert unchanged['all_ready'] and unchanged['selection_revision'] == revision
    result = client.post(url + '/encounters', headers=leader, json={'selection_revision': revision, 'encounter_count': 1})
    assert result.status_code == 201, result.text
    assert len(result.json()['participants']) == 2
    with SessionLocal() as db:
        from app.models import QuestRun, PartyMember
        from sqlalchemy import select
        run = db.scalar(select(QuestRun).where(QuestRun.party_id == UUID(party['id'])))
        assert run is not None
        assert not any(m.is_ready for m in db.scalars(select(PartyMember).where(PartyMember.party_id == UUID(party['id']))))
    assert client.post(url + '/selection', headers=leader, json={}).status_code == 409
