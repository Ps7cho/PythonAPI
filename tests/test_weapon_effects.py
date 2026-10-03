from copy import deepcopy
from random import Random
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from app.combat import CombatAbility, execute_cast
from app.database import SessionLocal, engine
from app.models import Ability, Weapon, WeaponEffect, WeaponEffectPool, WeaponType
from app.weapon_effects import (create_weapon, pool_snapshot, roll_effects, snapshot_effect,
                                validate_ability_role, validate_effect, validate_pool)


def effect(slug='venom'):
    return dict(slug=slug, name='Venom edge', description='', proc_chance_percent=100, recipient='targets',
                affliction_ops=[dict(op='apply', affliction='poison', stacks=2, definition={
                    'slug': 'poison', 'name': 'Poison', 'damage': 3, 'duration': 3, 'max_stacks': 5})],
                effect_chain=[])


def combatants():
    actor = dict(id='hero', name='Hero', team='players', hp=80, max_hp=100, power=10,
                 equipped_weapon={'id': 'sword', 'weapon_type': 'sword', 'tags': ['melee'], 'base_damage': 10, 'effects': [effect()]})
    enemy = dict(id='enemy', name='Enemy', team='enemies', hp=100, max_hp=100, armor=0)
    return actor, enemy


def test_weapon_application_precedes_ability_exploitation_and_cooldown_is_charged_once():
    actor, enemy = combatants()
    ability = CombatAbility(slug='strike', name='Strike', requires_weapon=True, cooldown_turns=3,
                            affliction_ops=[dict(op='exploit', affliction='poison', stacks=2, power=4)])
    result = execute_cast(actor, ability, [enemy], turn=1, rng=Random(1))
    assert enemy['statuses'][0]['stacks'] == 2
    assert enemy['hp'] == 82  # 10 from the weapon and 8 from exploiting its two stacks.
    assert actor['ability_ready_turns'] == {'strike': 4}
    assert next(r for r in result if r.get('interaction') == 'apply')['ability'].startswith('weapon:')


def test_dodge_and_nonweapon_damage_do_not_proc_weapon_effects():
    actor, enemy = combatants()
    enemy['evasion'] = {'chance_percent': 100, 'until_turn': 3}
    execute_cast(actor, CombatAbility(slug='hit', name='Hit', requires_weapon=True), [enemy], turn=1)
    assert enemy['hp'] == 100 and not enemy.get('statuses')
    execute_cast(actor, CombatAbility(slug='spell', name='Spell', damage=5), [enemy], turn=2)
    assert enemy['hp'] == 95 and not enemy.get('statuses')


def test_weapon_followups_reuse_damage_results_and_modifiers():
    actor, enemy = combatants()
    actor['equipped_weapon']['effects'][0]['effect_chain'] = [
        dict(id='drain', effect='heal', recipient='self', source='damage_dealt', value=50),
        dict(id='empower', effect='modifier', recipient='self', source='fixed', stat='damage_multiplier',
             operation='percent', modifier=50, duration=2, ability_slug='strike'),
    ]
    strike = CombatAbility(slug='strike', name='Strike', requires_weapon=True, damage_multiplier=1)
    execute_cast(actor, strike, [enemy], turn=1)
    assert actor['hp'] == 85
    assert len(actor['ability_modifiers']) == 1
    execute_cast(actor, strike, [enemy], turn=2)
    assert enemy['hp'] == 75  # The modifier changes the next strike from 10 to 15.
    assert len(actor['ability_modifiers']) == 1  # The same weapon source refreshes.


def test_invalid_followup_rolls_back_weapon_application_and_primary_damage():
    actor, enemy = combatants()
    actor['equipped_weapon']['effects'][0]['effect_chain'] = [dict(id='invalid', effect='damage', recipient='self')]
    original = deepcopy((actor, enemy))
    with pytest.raises(ValueError):
        execute_cast(actor, CombatAbility(slug='strike', name='Strike', requires_weapon=True), [enemy], turn=1)
    assert (actor, enemy) == original


def test_optional_pool_roll_is_unique_and_saved_weapon_is_independent_of_catalog(client):
    hero_id = UUID(client.post('/api/adventurers', json={'name': 'Effects'}).json()['id'])
    slug = uuid4().hex
    with SessionLocal.begin() as db:
        source = WeaponEffect(slug=slug, name='Test edge', description='', proc_chance_percent=100,
                              allowed_weapon_tags=['melee'], recipient='targets',
                              affliction_ops=[dict(op='apply', affliction='poison', stacks=1)], effect_chain=[])
        db.add(source); db.flush(); validate_effect(db, source)
        pool = WeaponEffectPool(slug=slug, name='Test pool', description='', chance_percent=100,
                                min_effects=1, max_effects=4, entries=[dict(effect_slug=slug, weight=1)])
        db.add(pool); db.flush(); validate_pool(db, pool)
        saved_pool = pool_snapshot(db, 'sword', pool_slug=slug)
        assert len(roll_effects(saved_pool, Random(2))) == 1
        assert roll_effects({**saved_pool, 'chance_percent': 0}, Random(2)) == []
        assert roll_effects(pool_snapshot(db, 'bow', pool_slug=slug), Random(2)) == []
        weapon = create_weapon(db, adventurer_id=hero_id, weapon_type_slug='sword', name='Test blade', base_damage=10, effect_pool_slug=slug)
        db.flush(); weapon_id = weapon.id
        source.name = 'Changed later'; source.affliction_ops = []
        source.effect_chain = [dict(id='heal', effect='heal', recipient='self', source='fixed', value=1)]
    with SessionLocal() as db:
        saved = db.get(Weapon, weapon_id)
        assert saved.effects[0]['name'] == 'Test edge'
        assert saved.effects[0]['affliction_ops'][0]['definition']['slug'] == 'poison'


def test_loot_roll_and_auction_delivery_keep_exact_effects(client):
    from app.group_journeys import roll_loot
    from app.auction_house import deliver
    hero_id = client.post('/api/adventurers', json={'name': 'Effect buyer'}).json()['id']
    pool = dict(chance_percent=100, min_effects=1, max_effects=1, entries=[dict(weight=1, effect=effect())])
    drop = roll_loot({'drops': [dict(name='Blade', weapon_type_slug='sword', base_damage=10, weight=1, weapon_effect_pool=pool)]}, Random(2))
    assert drop['effects'] == [effect()] and 'weapon_effect_pool' not in drop
    identifier = uuid4()
    item = dict(id=str(identifier), item_type='weapon', name='Blade', weapon_type='sword', base_damage=10, required_rank='iron', effects=drop['effects'])
    with SessionLocal.begin() as db:
        from app.models import Adventurer
        deliver(db, SimpleNamespace(item=item, quantity=1), db.get(Adventurer, UUID(hero_id)))
    with SessionLocal() as db:
        assert db.get(Weapon, identifier).effects == drop['effects']


def test_migrated_catalog_and_role_validation():
    from app.migrations.v032_weapon_applications import migrate_seeded_content
    with SessionLocal() as db:
        assert db.get(WeaponEffectPool, 'affliction-weapons').entries
        assert db.get(WeaponType, 'sword').effect_pool_slug == 'affliction-weapons'
        assert db.get(WeaponEffect, 'application-venom_strike') is not None
        for ability in db.scalars(select(Ability)):
            validate_ability_role(ability.status_effect_slug, ability.affliction_ops)
        ids = list(db.scalars(select(WeaponEffect.slug)))
    migrate_seeded_content(engine)
    with SessionLocal() as db:
        assert list(db.scalars(select(WeaponEffect.slug))) == ids
    with pytest.raises(ValueError, match='weapon effects'):
        validate_ability_role(None, [dict(op='apply', affliction='poison')])


def test_pool_validation_rejects_duplicate_and_missing_references():
    with SessionLocal() as db:
        slug = 'application-venom_strike'
        pool = WeaponEffectPool(chance_percent=100, min_effects=1, max_effects=2,
                                entries=[dict(effect_slug=slug, weight=1)] * 2)
        with pytest.raises(ValueError, match='cannot be repeated'):
            validate_pool(db, pool)
        pool.entries = [dict(effect_slug='does-not-exist', weight=1)]
        with pytest.raises(ValueError, match='must exist'):
            validate_pool(db, pool)


def test_multiple_targets_only_apply_to_landed_hits_and_self_recipient():
    actor, enemy = combatants()
    dodger = {**deepcopy(enemy), 'id': 'dodger', 'evasion': {'chance_percent': 100, 'until_turn': 3}}
    strike = CombatAbility(slug='sweep', name='Sweep', requires_weapon=True, max_targets=None)
    execute_cast(actor, strike, [enemy, dodger], turn=1)
    assert enemy['statuses'][0]['stacks'] == 2
    assert not dodger.get('statuses')
    actor['equipped_weapon']['effects'][0]['recipient'] = 'self'
    execute_cast(actor, strike, [enemy], turn=2)
    assert actor['statuses'][0]['stacks'] == 2
    assert enemy['statuses'][0]['stacks'] == 2


def test_schema_upgrade_preserves_legacy_inventory_and_is_repeatable():
    from sqlalchemy import create_engine, inspect, text
    from app.migrations.v031_weapon_effects import upgrade
    legacy = create_engine('sqlite:///:memory:')
    with legacy.begin() as conn:
        for table in ('weapon_types', 'weapon_definitions', 'weapons', 'enemy_weapons'):
            conn.execute(text(f'CREATE TABLE {table} (id VARCHAR PRIMARY KEY)'))
        conn.execute(text("INSERT INTO weapons VALUES ('existing')"))
        upgrade(conn)
        upgrade(conn)
        assert conn.execute(text('SELECT id, effects FROM weapons')).one() == ('existing', '[]')
        assert 'effect_pool_slug' in {c['name'] for c in inspect(conn).get_columns('weapon_types')}
    legacy.dispose()
