from uuid import UUID, uuid4

from app.database import SessionLocal
from app.models import Party


def test_leaving_transfers_leadership_and_allows_solo(client):
    heroes = [client.post('/api/adventurers', json={'name': uuid4().hex}).json() for _ in range(2)]
    party = client.post('/api/parties', json={'name': 'Departure', 'adventurer_id': heroes[0]['id']}).json()
    assert client.post('/api/parties/join', json={'adventurer_id': heroes[1]['id'], 'code': party['invite']['code']}).status_code == 200
    assert client.post('/api/parties/'+party['id']+'/leave', json={'adventurer_id': heroes[0]['id']}).status_code == 200
    with SessionLocal() as db:
        assert str(db.get(Party, UUID(party['id'])).leader) == heroes[1]['id']
    solo = client.post('/api/encounters', json={'adventurer_ids': [heroes[0]['id']]}).json()
    assert len(solo['participants']) == 1
    parties = client.get('/api/parties').json()
    active = next(p for p in parties if p['active_encounter_id'] == solo['id'])
    assert client.post('/api/parties/'+active['id']+'/leave', json={'adventurer_id': heroes[0]['id']}).status_code == 409


def test_log_survives_reads_and_includes_guard_and_enemy_damage(client):
    hero = client.post('/api/adventurers', json={'name': uuid4().hex}).json()
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    url = '/api/encounters/'+encounter['id']
    result = client.post(url+'/actions', json={'actor_id': hero['id'], 'expected_turn': 1, 'action': 'guard'}).json()
    log = result['combat_log']
    assert len(log) == 1 and log[0]['turn'] == 1
    assert any('blocking the next 4' in message for message in log[0]['messages'])
    assert any('Guard blocks 4' in message for message in log[0]['messages'])
    assert client.get(url).json()['combat_log'] == log
