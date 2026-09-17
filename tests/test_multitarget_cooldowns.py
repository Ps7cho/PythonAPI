from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from app.combat import CombatAbility, InvalidCombatAction, execute_cast, select_targets
from app.database import SessionLocal
from app.models import Ability, Adventurer, AdventurerAbility, Encounter, Enemy
from app.enemies import roll_enemy


def fighter(key, team='enemy', hp=100):
    return {'id': key, 'name': key, 'team': team, 'hp': hp, 'max_hp': 100, 'power': 10}


def test_multitarget_cast_is_atomic_and_charges_once():
    actor, first, second = fighter('actor', 'player'), fighter('first'), fighter('second')
    ability = CombatAbility('power_strike', 'Area strike', damage=16, max_targets=2, cooldown_turns=3)
    before = deepcopy((actor, first, second))
    second['team'] = 'player'
    with pytest.raises(InvalidCombatAction):
        execute_cast(actor, ability, [first, second], turn=1)
    second['team'] = 'enemy'
    assert (actor, first, second) == before
    results = execute_cast(actor, ability, [first, second], turn=1)
    assert [r['amount'] for r in results] == [16, 16]
    assert [r['ability'] for r in results] == ['power_strike', 'power_strike']
    assert actor['ability_ready_turns'] == {'power_strike': 4}
    assert actor['power_ready_turn'] == 4
    before = deepcopy((actor, first, second))
    with pytest.raises(InvalidCombatAction):
        execute_cast(actor, ability, [first, second], turn=2)
    assert (actor, first, second) == before


def test_target_limits_teams_dead_targets_and_duplicates():
    actor, first, second, dead = fighter('actor', 'player'), fighter('first'), fighter('second'), fighter('dead', hp=0)
    ability = CombatAbility('area', 'Area', max_targets=2)
    pool = [actor, first, second, dead]
    assert select_targets(actor, ability, pool) == [first, second]
    for ids in ([], ['first', 'first'], ['dead'], ['actor'], ['missing']):
        with pytest.raises(InvalidCombatAction):
            select_targets(actor, ability, pool, ids)
    with pytest.raises(InvalidCombatAction):
        select_targets(actor, replace(ability, max_targets=1), pool, ['first', 'second'])
    assert select_targets(actor, ability, pool, ['second', 'first']) == [second, first]


def grant_area(client, cooldown_type='turn'):
    hero = client.post('/api/adventurers', json={'name': 'Area caster'}).json()
    with SessionLocal.begin() as db:
        ability = Ability(slug='area_' + uuid4().hex, name='Area ' + uuid4().hex, power=100,
                          max_targets=2, cooldown_type=cooldown_type, cooldown_value=3)
        db.add(ability)
        db.flush()
        ability_id = str(ability.id)
        db.add(AdventurerAbility(adventurer_id=UUID(hero['id']), ability_id=ability.id))
    assert client.post('/api/adventurers/' + hero['id'] + '/loadout', json={'ability_ids': [ability_id]}).status_code == 200
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    with SessionLocal.begin() as db:
        stored = db.get(Encounter, UUID(encounter['id']))
        extra = roll_enemy(db.get(Enemy, 'roadside-bandit'), 1)
        extra.position = 1
        stored.enemy_instances.append(extra)
    encounter = client.get('/api/encounters/' + encounter['id']).json()
    return hero, ability_id, encounter


def test_api_multitarget_and_turn_cooldowns_survive_new_quest(client):
    hero, ability_id, encounter = grant_area(client)
    url = '/api/encounters/' + encounter['id'] + '/actions'
    action = {'actor_id': hero['id'], 'ability_id': ability_id, 'expected_turn': 1}
    targets = [e['id'] for e in encounter['enemies']]
    for bad in ([targets[0], targets[0]], [targets[0], str(uuid4())], targets + [str(uuid4())]):
        assert client.post(url, json={**action, 'target_ids': bad}).status_code == 400
        assert client.get('/api/encounters/' + encounter['id']).json() == encounter
    assert client.post(url, json={**action, 'target_id': targets[0], 'target_ids': targets}).status_code == 422
    response = client.post(url, json={**action, 'target_ids': targets[::-1]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['state'] == 'victory'
    assert [r['target_id'] for r in result['action_results']] == targets[::-1]
    assert result['participants'][0]['ability_ready_turns'][ability_id] == 4
    with SessionLocal() as db:
        assert db.get(Adventurer, UUID(hero['id'])).combat_cooldowns['turns'][ability_id] == 2
    next_fight = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    assert next_fight['participants'][0]['ability_ready_turns'][ability_id] == 3
    response = client.post('/api/encounters/' + next_fight['id'] + '/actions', json=action)
    assert response.status_code == 409
    assert client.get('/api/encounters/' + next_fight['id']).json() == next_fight


def test_realtime_cooldown_survives_reequipping_reload_and_expires(client, monkeypatch):
    import app.combat as combat
    monkeypatch.setattr(combat, 'time', lambda: 1000)
    hero, ability_id, encounter = grant_area(client, 'minutes')
    action = {'actor_id': hero['id'], 'ability_id': ability_id, 'expected_turn': 1}
    response = client.post('/api/encounters/' + encounter['id'] + '/actions', json=action)
    assert response.status_code == 200
    assert response.json()['state'] == 'victory'
    assert client.post('/api/adventurers/' + hero['id'] + '/loadout', json={'ability_ids': [ability_id]}).status_code == 200
    next_fight = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    assert next_fight['participants'][0]['ability_ready_at'][ability_id] == 1180
    url = '/api/encounters/' + next_fight['id'] + '/actions'
    assert client.post(url, json=action).status_code == 409
    monkeypatch.setattr(combat, 'time', lambda: 1180)
    assert client.post(url, json=action).status_code == 200


def test_versioned_migration_preserves_existing_targeting_and_cooldowns():
    from app.migrations import migrate
    engine = create_engine('sqlite:///:memory:')
    # Minimal legacy schema for the tables whose columns the migration upgrades.
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE abilities (id VARCHAR PRIMARY KEY, slug VARCHAR, target_type VARCHAR, effect_type VARCHAR, starter BOOLEAN)'))
        conn.execute(text('CREATE TABLE adventurers (id VARCHAR PRIMARY KEY)'))
        conn.execute(text('CREATE TABLE enemy_abilities (ability_id VARCHAR)'))
        conn.execute(text('CREATE TABLE adventurer_abilities (ability_id VARCHAR)'))
        conn.execute(text('CREATE TABLE encounters (id VARCHAR, participants JSON, turn INTEGER, state VARCHAR, updated_at VARCHAR, created_at VARCHAR)'))
        conn.execute(text("INSERT INTO abilities VALUES ('enemy', 'bite', 'enemy', 'damage', FALSE), ('buff', 'cry', 'party', 'buff', TRUE)"))
        conn.execute(text("INSERT INTO enemy_abilities VALUES ('enemy')"))
        conn.execute(text("INSERT INTO adventurers VALUES ('hero')"))
        import json
        conn.execute(text("INSERT INTO encounters VALUES ('enc', :participants, 2, 'victory', '2026', '2026')"),
                     {'participants': json.dumps([{'id': 'hero', 'ability_ready_turns': {'buff': 5}, 'ability_ready_at': {'buff': 5000}}])})
    migrate(engine)
    migrate(engine)
    with engine.connect() as conn:
        assert conn.scalar(text('SELECT COUNT(*) FROM schema_migrations')) == 13
        assert conn.execute(text('SELECT max_targets FROM abilities')).scalars().all() == [None, None]
        saved = json.loads(conn.scalar(text('SELECT combat_cooldowns FROM adventurers')))
        assert saved == {'turns': {'buff': 2}, 'ready_at': {'buff': 5000}}
    engine.dispose()


def test_basic_attack_reads_equipped_weapon_damage(client):
    from app.models import EquippedWeapon
    hero = client.post('/api/adventurers', json={'name': 'Weapon scaling'}).json()
    with SessionLocal.begin() as db:
        db.get(EquippedWeapon, UUID(hero['id'])).weapon.base_damage = 23
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    response = client.post('/api/encounters/' + encounter['id'] + '/actions', json={
        'actor_id': hero['id'], 'expected_turn': 1, 'action': 'attack'})
    assert response.status_code == 200
    assert response.json()['action_results'][0]['amount'] == 23
