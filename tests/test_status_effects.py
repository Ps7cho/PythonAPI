from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import select

from app.combat import CombatAbility, InvalidCombatAction, execute_cast, apply_status, tick_statuses
from app.database import SessionLocal
from app.models import Ability, Adventurer, Encounter, Enemy
from app.loadouts import executable
from app.enemies import roll_enemy
from app.journeys import apply_rest, RestRules
from app.status_content import EFFECTS
from tests.test_encounters import create, act


def fighter(id='p', team='players', **kwargs):
    return dict(id=id, team=team, name=id, hp=100, max_hp=100, power=10, **kwargs)


@pytest.mark.parametrize('definition', EFFECTS, ids=lambda d: d['slug'])
def test_stack_cap_refresh_and_exact_expiry(definition):
    target = fighter()
    apply_status(target, definition, 'source')
    tick_statuses([target], turn=1)
    for _ in range(5):
        apply_status(target, definition, 'other-source')
    effect = target['statuses'][0]
    assert effect['stacks'] == definition['max_stacks']
    assert effect['remaining_rounds'] == definition['duration']
    assert effect['source_id'] == 'other-source'
    target['hp'] = 100
    for turn in range(definition['duration']):
        results = tick_statuses([target], turn=turn + 2)
        assert results[0]['amount'] == definition['damage'] * definition['max_stacks']
    assert target['statuses'] == []
    assert tick_statuses([target], turn=10) == []


def test_immunity_resistance_guard_and_shield():
    target = fighter(status_resistances={'bleed': 100, 'poison': 50}, guarding=True, guard_power=99)
    assert 'immune' in apply_status(target, EFFECTS[0], 'p')
    assert not target.get('statuses')
    apply_status(target, EFFECTS[1], 'p')
    assert tick_statuses([target], turn=1)[0]['amount'] == 2  # 3 at 50%, rounded up
    target.update(hp=2, shield={'until_turn': 3})
    assert tick_statuses([target], turn=2)[0]['amount'] == 1
    assert tick_statuses([target], turn=3)[0]['amount'] == 1
    assert target['hp'] == 0


def test_failed_multitarget_cast_does_not_leak_damage_status_or_cooldown():
    actor, first, invalid = fighter(), fighter('e', 'enemies'), fighter('ally')
    ability = CombatAbility('bleeding', 'Bleeding', damage=1, status_effect=EFFECTS[0], max_targets=None, cooldown_turns=3)
    before = deepcopy([actor, first, invalid])
    with pytest.raises(InvalidCombatAction):
        execute_cast(actor, ability, [first, invalid], turn=1)
    assert [actor, first, invalid] == before
    results = execute_cast(actor, ability, [first], turn=1)
    assert first['statuses'][0]['slug'] == 'bleed'
    assert actor['ability_ready_turns'] == {'bleeding': 4}
    assert results[0]['amount'] == 1


def test_database_definitions_profiles_and_snapshot_stability():
    with SessionLocal() as db:
        ability = db.scalar(select(Ability).where(Ability.slug == 'shadow_hex'))
        snapshot = executable(ability)
        assert snapshot.status_effect['slug'] == 'necrosis'
        original = ability.status_effect.damage
        ability.status_effect.damage = 99
        assert snapshot.status_effect['damage'] == original
        db.rollback()
        enemy = roll_enemy(db.get(Enemy, 'undead'), 1).state
        assert enemy['status_resistances']['bleed'] == 100
        assert enemy['status_resistances']['poison'] == 100


def infect(encounter, *, enemies=False, hp=None):
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(encounter['id']))
        targets = [deepcopy(i.state) for i in saved.enemy_instances] if enemies else deepcopy(saved.participants)
        for target in targets:
            apply_status(target, EFFECTS[0], encounter['participants'][0]['id'])
            if hp is not None:
                target['hp'] = hp
        if enemies:
            for instance, target in zip(saved.enemy_instances, targets):
                instance.state = target
        else:
            saved.participants = targets


def test_round_boundary_persistence_and_retry(client):
    encounter = create(client, 2)
    infect(encounter)
    first = act(client, encounter, action='wait').json()
    assert first['participants'][0]['statuses'][0]['remaining_rounds'] == 3
    second = act(client, first, index=1, action='wait').json()
    assert second['turn'] == 2
    assert len([r for r in second['action_results'] if r['effect'] == 'damage_over_time']) == 2
    assert all(p['hp'] == 92 for p in second['participants'])
    assert act(client, first, index=1, action='wait').status_code == 409
    current = client.get('/api/encounters/' + encounter['id']).json()
    assert current['participants'] == second['participants']
    with SessionLocal() as db:
        hero = db.get(Adventurer, UUID(current['participants'][0]['id']))
        assert hero.combat_statuses == current['participants'][0]['statuses']


def test_status_kill_settles_victory_once(client):
    encounter = create(client)
    infect(encounter, enemies=True, hp=2)
    response = act(client, encounter, action='wait').json()
    assert response['state'] == 'victory'
    assert response['enemies'][0]['hp'] == 0
    with SessionLocal() as db:
        assert db.get(Adventurer, UUID(encounter['participants'][0]['id'])).gold == 10
    assert act(client, encounter, action='wait').status_code == 409


def test_simultaneous_status_wipe_is_defeat(client):
    encounter = create(client)
    infect(encounter, enemies=True, hp=2)
    infect(encounter, hp=2)
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(encounter['id']))
        for enemy in saved.enemy_instances:
            enemy.state = {**enemy.state, 'abilities': []}
    response = act(client, encounter, action='wait').json()
    assert response['state'] == 'defeat'
    with SessionLocal() as db:
        hero = db.get(Adventurer, UUID(encounter['participants'][0]['id']))
        assert not hero.is_alive and hero.gold == 0 and hero.health == 0


def test_statuses_carry_to_departure_and_village_rest_cures_at_full_hp(client):
    hero = client.post('/api/adventurers', json={'name': 'Afflicted'}).json()['id']
    actor = fighter()
    apply_status(actor, EFFECTS[1], 'enemy')
    with SessionLocal.begin() as db:
        db.get(Adventurer, UUID(hero)).combat_statuses = actor['statuses']
    assert client.post(f'/api/adventurers/{hero}/rest').status_code == 200
    with SessionLocal.begin() as db:
        saved = db.get(Adventurer, UUID(hero))
        assert saved.combat_statuses == []
        saved.combat_statuses = actor['statuses']
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero]}).json()
    assert encounter['participants'][0]['statuses'] == actor['statuses']
    assert client.post(f'/api/adventurers/{hero}/rest').status_code == 409


def test_camp_rest_cures():
    actor = fighter()
    apply_status(actor, EFFECTS[3], 'enemy')
    apply_rest(actor, RestRules(heal_percent=20, turns=1, seconds=0, clear_cooldowns=False))
    assert actor['statuses'] == []


def test_player_can_equip_and_apply_database_status(client):
    hero = client.post('/api/adventurers', json={'name': 'Venom fighter'}).json()['id']
    abilities = client.get('/api/adventurers/' + hero).json()['abilities']
    venom = next(a for a in abilities if a['name'] == 'Venom Strike')
    assert venom['status_effect']['max_stacks'] == 3
    equipped = client.post('/api/adventurers/' + hero + '/loadout', json={'ability_ids': [venom['id']]})
    assert equipped.status_code == 200
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero]}).json()
    result = client.post('/api/encounters/' + encounter['id'] + '/actions', json={
        'actor_id': hero, 'expected_turn': 1, 'ability_id': venom['id']})
    assert result.status_code == 200
    state = result.json()
    assert state['enemies'][0]['statuses'][0]['slug'] == 'poison'
    assert state['enemies'][0]['statuses'][0]['remaining_rounds'] == 2
    assert any(r['effect'] == 'damage_over_time' and r['amount'] == 3 for r in state['action_results'])


def test_enemy_applies_catalog_status(client):
    hero = client.post('/api/adventurers', json={'name': 'Leech victim'}).json()['id']
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero], 'enemy_slug': 'leech'}).json()
    state = act(client, encounter, action='wait').json()
    assert state['participants'][0]['statuses'][0]['slug'] == 'bleed'
    assert any(r['effect'] == 'damage_over_time' and r['amount'] == 2 for r in state['action_results'])


def test_catalog_unlocks_existing_characters_once_and_preserves_edits(monkeypatch):
    from uuid import uuid4
    from sqlalchemy import create_engine, func
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import StatusEffect, AdventurerAbility, User
    from app import seed_abilities as seeds
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(seeds, 'SessionLocal', sessions)
    try:
        with sessions.begin() as db:
            owner = User(id=uuid4(), email='test@example.com', username='test')
            db.add(owner)
            db.add_all([StatusEffect(**s) for s in EFFECTS])
            db.add_all([Adventurer(name=f'Existing {i}', owner=owner.id) for i in range(2)])
        seeds.seed_status_abilities()
        with sessions.begin() as db:
            assert db.scalar(select(func.count()).select_from(AdventurerAbility)) == 8
            ability = db.scalar(select(Ability).where(Ability.slug == 'rending_strike'))
            ability.power = 77
            ability.status_effect_slug = None
        seeds.seed_status_abilities()
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(AdventurerAbility)) == 8
            ability = db.scalar(select(Ability).where(Ability.slug == 'rending_strike'))
            assert ability.power == 77 and ability.status_effect_slug is None
    finally:
        engine.dispose()
