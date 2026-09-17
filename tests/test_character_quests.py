from uuid import UUID

from app.database import SessionLocal
from app.models import Adventurer


def hero(client):
    return client.post('/api/adventurers', json={'name': 'Mosswood Adventurer'}).json()


def test_character_sheet_uses_saved_data(client):
    character = hero(client)
    with SessionLocal.begin() as db:
        db.get(Adventurer, UUID(character['id'])).inventory.items = [{'name': 'Iron Ore', 'quantity': 3}]
    response = client.get('/api/adventurers/' + character['id'])
    assert response.status_code == 200
    sheet = response.json()
    assert sheet['attributes'] == character['attributes']
    assert sheet['inventory'][0] == {'name': 'Iron Ore', 'quantity': 3}
    assert sheet['inventory'][1]['name'] == 'Training Sword'
    assert len(sheet['equipment']) == 9
    assert sheet['equipment']['Main Hand']['weapon_type'] == 'sword'
    assert sheet['abilities']
    assert len(sheet['equipped_ability_ids']) == 3
    client.post('/api/auth/logout')
    assert client.get('/api/adventurers/' + character['id']).status_code == 401


def test_three_encounters_wait_continue_and_carry_health(client):
    character = hero(client)
    response = client.post('/api/encounters', json={'adventurer_ids': [character['id']], 'encounter_count': 3})
    assert response.status_code == 201
    encounter = response.json()
    assert client.post(f"/api/encounters/{encounter['id']}/continue").status_code == 409
    for number in range(1, 4):
        assert encounter['quest']['encounter_number'] == number
        assert encounter['quest']['encounter_count'] == 3
        for _ in range(6):
            response = client.post(f"/api/encounters/{encounter['id']}/actions", json={
                'actor_id': character['id'], 'expected_turn': encounter['turn'], 'action': 'attack',
            })
            assert response.status_code == 200
            encounter = response.json()
        assert encounter['state'] == 'victory'
        sheet = client.get('/api/adventurers/' + character['id']).json()
        assert sheet['health'] == 100 - 30 * number
        assert sheet['gold'] == 10 * number
        if number < 3:
            assert encounter['quest']['status'] == 'awaiting_continue'
            assert sheet['active_encounter_id'] == encounter['id']
            assert client.post('/api/encounters', json={'adventurer_ids': [character['id']]}).status_code == 409
            saved = client.get('/api/encounters/' + encounter['id']).json()
            assert saved['quest']['can_continue']
            old_id = encounter['id']
            encounter = client.post(f'/api/encounters/{old_id}/continue').json()
            assert encounter['participants'][0]['hp'] == sheet['health']
            assert client.post(f'/api/encounters/{old_id}/continue').status_code == 409
        else:
            assert encounter['quest']['status'] == 'victory'
            assert not encounter['quest']['can_continue']
            assert sheet['active_encounter_id'] is None
            assert client.post(f"/api/encounters/{encounter['id']}/continue").status_code == 409


def test_only_supported_quest_lengths(client):
    character = hero(client)
    assert client.post('/api/encounters', json={'adventurer_ids': [character['id']], 'encounter_count': 7}).status_code == 422
