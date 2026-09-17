from uuid import UUID, uuid4

from app.database import SessionLocal
from app.models import Adventurer


def create(client, count=1):
    heroes = [client.post('/api/adventurers', json={'name': str(uuid4())}).json() for _ in range(count)]
    response = client.post('/api/encounters', json={'adventurer_ids': [h['id'] for h in heroes]})
    assert response.status_code == 201
    return response.json()


def act(client, encounter, index=0, action='attack'):
    return client.post(f"/api/encounters/{encounter['id']}/actions", json={
        'actor_id': encounter['participants'][index]['id'],
        'expected_turn': encounter['turn'], 'action': action,
    })


def test_solo_takes_six_rounds_and_reads_do_not_advance(client):
    encounter = create(client)
    for turn in range(1, 7):
        assert encounter['turn'] == turn
        for _ in range(3):
            snapshot = client.get(f"/api/encounters/{encounter['id']}").json()
            assert snapshot == {k: v for k, v in encounter.items() if k not in ('events', 'action_results')}
        response = act(client, encounter)
        assert response.status_code == 200
        encounter = response.json()
        assert encounter['state'] == ('victory' if turn == 6 else 'player_turn')
    assert encounter['participants'][0]['hp'] == 70
    with SessionLocal() as db:
        hero = db.get(Adventurer, UUID(encounter['participants'][0]['id']))
        assert hero.gold == 10
    assert act(client, encounter).status_code == 409


def test_group_waits_for_every_player_and_rejects_duplicates(client):
    encounter = create(client, 2)
    first = act(client, encounter).json()
    assert first['turn'] == 1
    assert all(p['hp'] == 100 for p in first['participants'])
    assert len(first['pending_actor_ids']) == 1
    assert act(client, first).status_code == 409
    second = act(client, first, 1).json()
    assert second['turn'] == 2
    assert all(p['hp'] == 94 for p in second['participants'])
    assert act(client, first, 1).status_code == 409


def test_guard_and_turn_cooldown(client):
    encounter = create(client)
    encounter = act(client, encounter, action='power_strike').json()
    assert encounter['turn'] == 2
    assert act(client, encounter, action='power_strike').status_code == 409
    before = encounter['participants'][0]['hp']
    encounter = act(client, encounter, action='guard').json()
    assert encounter['participants'][0]['hp'] == before - 2
    encounter = act(client, encounter).json()
    assert encounter['turn'] == 4
    assert act(client, encounter, action='power_strike').status_code == 200


def test_invalid_action_has_no_effect(client):
    encounter = create(client)
    assert act(client, encounter, action='tick').status_code == 422
    response = client.post(f"/api/encounters/{encounter['id']}/actions", json={
        'actor_id': str(uuid4()), 'expected_turn': 1, 'action': 'attack',
    })
    assert response.status_code == 404
    assert client.get(f"/api/encounters/{encounter['id']}").json() == encounter


def test_no_parallel_encounters_for_same_hero(client):
    encounter = create(client)
    response = client.post('/api/encounters', json={
        'adventurer_ids': [encounter['participants'][0]['id']],
    })
    assert response.status_code == 409


def test_quest_starts_instead_of_auto_resolving(client):
    hero = client.post('/api/adventurers', json={'name': str(uuid4())}).json()
    response = client.post(f"/api/adventurers/{hero['id']}/quest")
    assert response.status_code == 200
    assert response.json()['state'] == 'player_turn'
    assert response.json()['participants'][0]['hp'] == 100


def test_defeat_stops_future_actions(client):
    encounter = create(client)
    for _ in range(50):
        encounter = act(client, encounter, action='guard').json()
    assert encounter['state'] == 'defeat'
    assert encounter['participants'][0]['hp'] == 0
    assert act(client, encounter).status_code == 409
