from copy import deepcopy
from uuid import UUID, uuid4

from app.database import SessionLocal
from app.enemies import link_legacy_enemies, seed_enemies
from app.models import Encounter, EncounterEnemy, Enemy


def start(client, slug):
    hero = client.post('/api/adventurers', json={'name': str(uuid4())}).json()
    return client.post('/api/encounters', json={'adventurer_ids': [hero['id']], 'enemy_slug': slug})


def test_catalog_and_referenced_rolls(client):
    catalog = client.get('/api/enemies').json()
    from app.enemies import STARTER_ENEMIES
    assert {e['name'] for e in catalog} == {e['name'] for e in STARTER_ENEMIES}
    for definition in catalog:
        response = start(client, definition['slug'])
        assert response.status_code == 201
        encounter = response.json()
        enemy = encounter['enemies'][0]
        assert enemy['attributes'] == definition['attributes']
        assert enemy['enemy_type'] == definition['enemy_type']
        for stat, limits in definition['stat_ranges'].items():
            assert limits['min'] <= enemy[stat] <= limits['max']
        with SessionLocal() as db:
            instance = db.get(EncounterEnemy, UUID(enemy['id']))
            assert instance.encounter_id == UUID(encounter['id'])
            assert instance.enemy.slug == definition['slug']
        assert client.get(f"/api/encounters/{encounter['id']}").json() == encounter
    assert start(client, 'missing').status_code == 404


def test_combat_and_catalog_edits_are_isolated(client):
    first = start(client, 'goblin').json()
    second = start(client, 'goblin').json()
    definition = client.get('/api/enemies/goblin').json()
    response = client.post(f"/api/encounters/{first['id']}/actions", json={
        'actor_id': first['participants'][0]['id'], 'expected_turn': 1, 'action': 'attack',
    })
    assert response.status_code == 200
    assert response.json()['enemies'][0]['hp'] == first['enemies'][0]['hp'] - 10
    assert client.get(f"/api/encounters/{second['id']}").json() == second
    assert client.get('/api/enemies/goblin').json() == definition
    try:
        with SessionLocal.begin() as db:
            enemy = db.get(Enemy, 'goblin')
            enemy.stat_ranges = {**enemy.stat_ranges, 'hp': {'min': 88, 'max': 88}}
        seed_enemies()
        assert start(client, 'goblin').json()['enemies'][0]['max_hp'] == 88
        assert client.get(f"/api/encounters/{second['id']}").json() == second
    finally:
        with SessionLocal.begin() as db:
            db.get(Enemy, 'goblin').stat_ranges = definition['stat_ranges']


def test_legacy_linking_preserves_health_and_is_idempotent(client):
    created = start(client, 'roadside-bandit').json()
    enemy = deepcopy(created['enemies'][0])
    enemy['hp'] = 17
    with SessionLocal.begin() as db:
        encounter = db.get(Encounter, UUID(created['id']))
        encounter.enemy_instances.clear()
        encounter.enemies = [enemy]
    link_legacy_enemies()
    link_legacy_enemies()
    with SessionLocal() as db:
        encounter = db.get(Encounter, UUID(created['id']))
        assert encounter.enemies == []
        assert len(encounter.enemy_instances) == 1
        assert encounter.enemy_instances[0].state['hp'] == 17
        assert encounter.enemy_instances[0].enemy_slug == 'roadside-bandit'
