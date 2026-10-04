from uuid import UUID, uuid4

from app.database import SessionLocal
from app.models import Adventurer, EquippedWeapon, Gear, GearDefinition
from app.gear import serialize as serialize_gear
from app.item_rarity import RARITIES


def test_completed_run_results_are_event_backed_and_private(client):
    hero = client.post('/api/adventurers', json={'name': 'Results Hero'}).json()
    with SessionLocal.begin() as db:
        weapon = db.get(EquippedWeapon, UUID(hero['id'])).weapon
        weapon.base_damage = 90
        weapon.rarity = 'epic'
    sheet = client.get('/api/adventurers/' + hero['id']).json()
    assert sheet['equipment']['Main Hand']['rarity'] == 'epic'
    assert sheet['equipment']['Main Hand']['effect_slots'] == 3
    assert sheet['equipment']['Main Hand']['slotted_effects'] == []

    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero['id']], 'enemy_slug': 'goblin'}).json()
    run_id = encounter['quest']['id']
    assert client.get(f'/api/quest-runs/{run_id}/results').status_code == 404
    response = client.post(f"/api/encounters/{encounter['id']}/actions", json={
        'actor_id': hero['id'], 'expected_turn': encounter['turn'], 'action': 'attack',
        'target_id': encounter['enemies'][0]['id']})
    assert response.status_code == 200, response.text
    final = response.json()
    assert final['state'] == 'victory'
    result = client.get(f'/api/quest-runs/{run_id}/results')
    assert result.status_code == 200, result.text
    summary = result.json()
    expected = sum(row['amount'] for row in final['action_results'] if row['effect'] in ('damage', 'damage_over_time'))
    assert summary['totals']['damage_dealt'] == expected
    assert summary['damage_by_enemy_type'][0]['damage'] == expected
    assert summary['individual_loot'][0]['gains']['gold'] > 0
    assert summary['party'][0]['name'] == 'Results Hero'
    assert client.post('/api/auth/logout').status_code == 204
    other = client.post('/api/auth/register', json={'username': 'results_other_' + uuid4().hex[:8], 'password': 'test-password-1234'})
    assert other.status_code == 201
    assert client.get(f'/api/quest-runs/{run_id}/results').status_code == 404


def test_rarity_slots_are_empty_and_gear_serialization_keeps_rarity(client):
    hero = client.post('/api/adventurers', json={'name': 'Rarity Hero'}).json()
    with SessionLocal.begin() as db:
        from app.item_rarity import item_rarity
        assert [item_rarity(name)['effect_slots'] for name in RARITIES] == [0, 1, 2, 3, 4]
        definition = GearDefinition(slug='results-test-helm', name='Results Helm', slot='Head',
                                    bonuses={}, required_rank='iron', price=1)
        db.add(definition)
        db.flush()
        gear = Gear(adventurer_id=UUID(hero['id']), definition_slug=definition.slug, rarity='legendary')
        db.add(gear)
        db.flush()
        result = serialize_gear(gear)
        assert result['rarity'] == 'legendary'
        assert result['effect_slots'] == 4
        assert result['slotted_effects'] == []
    with SessionLocal.begin() as db:
        db.get(Adventurer, UUID(hero['id'])).gold = 1000
    bought = client.post('/api/shop/purchase', json={'adventurer_id': hero['id'],
                         'item_type': 'weapon', 'item_slug': 'axe'})
    assert bought.status_code == 200, bought.text
    item = next(item for item in client.get('/api/adventurers/' + hero['id']).json()['inventory']
                if item.get('weapon_type') == 'axe')
    assert item['rarity'] in RARITIES
    assert item['effect_slots'] == RARITIES.index(item['rarity'])
