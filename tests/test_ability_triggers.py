from copy import deepcopy
from dataclasses import asdict
from random import Random
from uuid import UUID

import pytest
from sqlalchemy import select

from app.combat import CombatAbility, InvalidCombatAction, execute_cast
from app.database import SessionLocal, engine
from app.models import Ability, EnemyAbility, WeaponEffect


def fighter(identifier, team, hp=200):
    return dict(id=identifier, name=identifier, team=team, hp=hp, max_hp=hp, power=10)


def passive(**overrides):
    return CombatAbility(**dict(dict(slug='reaction', name='Reaction', trigger_mode='on_hit', damage=7), **overrides))


def test_fixed_and_chance_strikes_charge_once_and_stop_on_failed_roll():
    actor, target = fighter('a', 'heroes'), fighter('b', 'enemies')
    ability = CombatAbility('multi', 'Multi', damage=10, strike_count=3, extra_strike_chance=100, max_extra_strikes=2, cooldown_turns=3)
    results = execute_cast(actor, ability, [target], turn=1, rng=Random(1))
    assert len(results) == 5 and target['hp'] == 150
    assert [r['strike'] for r in results] == [1,2,3,4,5]
    assert actor['ability_ready_turns'] == {'multi':4}
    ability = CombatAbility('again', 'Again', damage=10, strike_count=2, extra_strike_chance=0, max_extra_strikes=10)
    assert len(execute_cast(actor, ability, [target], turn=2)) == 2
    with pytest.raises(InvalidCombatAction):
        execute_cast(actor, CombatAbility('bad', 'Bad', strike_count=100), [target], turn=2)


def test_multistrike_dodge_consumes_only_one_hit_and_stops_on_death():
    actor, target = fighter('a', 'heroes'), fighter('b', 'enemies', 10)
    target['evasion'] = dict(chance_percent=100, until_turn=3)
    results = execute_cast(actor, CombatAbility('multi', 'Multi', damage=10, strike_count=4), [target], turn=1)
    assert len(results) == 2 and results[0]['dodged'] and target['hp'] == 0


def test_extra_strikes_stop_after_first_failed_chance_roll():
    actor, target = fighter('a', 'heroes'), fighter('b', 'enemies')
    class Rolls:
        values = iter([0.2, 0.8])
        def random(self):
            return next(self.values)
    results = execute_cast(actor, CombatAbility('repeat', 'Repeat', damage=10, strike_count=2,
                           extra_strike_chance=50, max_extra_strikes=5), [target], turn=1, rng=Rolls())
    assert len(results) == 3 and target['hp'] == 170


def test_passive_temporary_modifier_is_applied_once():
    actor, target = fighter('a', 'heroes'), fighter('b', 'enemies')
    target['equipped_abilities'] = [asdict(passive(damage=10))]
    target['ability_modifiers'] = [dict(stat='power', operation='percent', value=50,
                                       until_turn=3, ability_slug='reaction')]
    execute_cast(actor, CombatAbility('hit', 'Hit', damage=10), [target], turn=1)
    assert actor['hp'] == 185


def test_retaliation_stops_remaining_strikes_without_reaction_loops():
    actor, target = fighter('a', 'heroes', 6), fighter('b', 'enemies')
    actor['equipped_abilities'] = target['equipped_abilities'] = [asdict(passive())]
    results = execute_cast(actor, CombatAbility('multi', 'Multi', damage=10, strike_count=5, cooldown_turns=3), [target], turn=1)
    assert len(results) == 2 and results[-1]['reaction']
    assert actor['hp'] == 0 and target['hp'] == 190
    assert actor['ability_ready_turns']['multi'] == 4


def test_passive_proc_cooldown_support_and_manual_rejection():
    actor, target = fighter('a', 'heroes'), fighter('b', 'enemies')
    reaction = passive(effect='affliction', target_type='self', damage=0, cooldown_turns=2,
                       effect_chain=[dict(id='recover', effect='heal', recipient='self', source='fixed', value=5, when='always')])
    target['equipped_abilities'] = [asdict(reaction)]
    results = execute_cast(actor, CombatAbility('multi', 'Multi', damage=10, strike_count=3), [target], turn=1)
    assert target['hp'] == 175
    assert len([r for r in results if r.get('reaction') and r['effect']=='heal']) == 1
    with pytest.raises(InvalidCombatAction, match='manually'):
        execute_cast(target, reaction, [target], turn=3)
    target['equipped_abilities'][0]['proc_chance_percent'] = 0
    results = execute_cast(actor, CombatAbility('hit','Hit',damage=10), [target], turn=3)
    assert not any(r.get('reaction') for r in results)


def test_dodged_and_lethal_hits_do_not_react_and_invalid_reaction_is_atomic():
    actor, target = fighter('a', 'heroes'), fighter('b', 'enemies')
    target['equipped_abilities'] = [asdict(passive())]
    target['evasion'] = dict(chance_percent=100, until_turn=3)
    results = execute_cast(actor, CombatAbility('hit','Hit',damage=10), [target], turn=1)
    assert len(results)==1 and actor['hp']==200
    target['equipped_abilities'][0]['effect_chain']=[dict(id='bad',effect='damage',recipient='self')]
    original = deepcopy((actor,target))
    with pytest.raises(ValueError):
        execute_cast(actor, CombatAbility('hit','Hit',damage=10), [target], turn=1)
    assert (actor,target)==original
    target['hp']=1
    assert len(execute_cast(actor, CombatAbility('hit','Hit',damage=10), [target], turn=1))==1


def test_armor_requires_equipping_and_is_snapshotted_in_gauntlets(client):
    from app.models import Adventurer, ArmorEffect, Gear
    from app.armor_effects import combat_armor_effects
    hero = client.post('/api/adventurers',json={'name':'Armored reactor'}).json()['id']
    sheet = client.get('/api/adventurers/'+hero).json()
    abilities = {a['slug']:a for a in sheet['abilities']}
    assert not {'riposte','recovery_reflex','venom_strike','kindle','flashpoint'} & abilities.keys()
    with SessionLocal.begin() as db:
        armor = Gear(adventurer_id=UUID(hero),definition_slug='reaction-riposte')
        db.add(armor);db.flush();armor_id=str(armor.id)
        assert combat_armor_effects(db,db.get(Adventurer,UUID(hero))) == []
        effect=db.get(ArmorEffect,'riposte');old=deepcopy(effect.definition)
        effect.definition={**old,'proc_chance_percent':100}
    try:
        equipped=client.post('/api/adventurers/'+hero+'/equipment',json={'slot':'Hands','gear_id':armor_id})
        assert equipped.status_code==200,equipped.text
        assert equipped.json()['equipment']['Hands']['effects'][0]['slug']=='riposte'
        unequipped=client.post('/api/adventurers/'+hero+'/equipment',json={'slot':'Hands','gear_id':None})
        assert unequipped.status_code==200
        with SessionLocal() as db:
            assert combat_armor_effects(db,db.get(Adventurer,UUID(hero))) == []
        client.post('/api/adventurers/'+hero+'/equipment',json={'slot':'Hands','gear_id':armor_id})
        response=client.post('/api/gauntlet-runs',json={'adventurer_ids':[hero]})
        assert response.status_code==201,response.text
        encounter=response.json()['encounter']
        assert len(encounter['participants'][0]['armor_effects'])==1
        assert not any(a['trigger_mode']=='on_hit' for a in encounter['participants'][0]['equipped_abilities'])
        with SessionLocal.begin() as db:
            db.get(ArmorEffect,'riposte').definition={**old,'proc_chance_percent':0}
        response=client.post('/api/encounters/'+encounter['id']+'/actions',json={'actor_id':hero,'expected_turn':1,'action':'wait'})
        assert response.status_code==200,response.text
        assert any(r.get('reaction') for r in response.json()['action_results'])
    finally:
        with SessionLocal.begin() as db: db.get(ArmorEffect,'riposte').definition=old


def test_removed_application_abilities_stay_removed_after_bootstrap():
    from app.seed_abilities import seed_enemy_abilities, seed_status_abilities
    from app.migrations.v018_affliction_grammar import seed_grammar
    from app.migrations.v032_weapon_applications import migrate_seeded_content as convert
    from app.migrations.v034_remove_application_abilities import migrate_seeded_content as remove
    seed_enemy_abilities();seed_status_abilities();seed_grammar(engine);convert(engine);remove(engine)
    with SessionLocal() as db:
        sources={s.removeprefix('application-') for s in db.scalars(select(WeaponEffect.slug)) if s.startswith('application-')}
        assert not sources & set(db.scalars(select(Ability.slug)))
        assert db.get(WeaponEffect,'application-venom_strike')
        assert db.scalar(select(EnemyAbility).join(Ability).where(Ability.slug=='weapon_strike'))


def test_old_ability_schema_upgrades_without_changing_existing_values():
    from sqlalchemy import create_engine, text
    from app.migrations.v033_ability_triggers import upgrade
    legacy = create_engine('sqlite:///:memory:')
    with legacy.begin() as conn:
        conn.execute(text('CREATE TABLE abilities (slug VARCHAR PRIMARY KEY, power INTEGER)'))
        conn.execute(text("INSERT INTO abilities VALUES ('existing', 17)"))
        upgrade(conn); upgrade(conn)
        assert conn.execute(text('SELECT power, strike_count, extra_strike_chance, max_extra_strikes, trigger_mode, proc_chance_percent FROM abilities')).one() == (17, 1, 0, 1, 'active', 100)
    legacy.dispose()


def test_existing_equipped_passive_migrates_to_owned_armor_without_equipping_it():
    from uuid import uuid4
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import (Adventurer, AdventurerAbility, ArmorEffect, EquippedAbility,
                            EquippedGear, Gear, GearDefinition, RankDefinition, User)
    from app.migrations.v037_move_passives_to_armor import upgrade
    legacy = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(legacy)
    sessions = sessionmaker(bind=legacy)
    owner_id, hero_id, passive_id = uuid4(), uuid4(), uuid4()
    with sessions.begin() as db:
        db.add(User(id=owner_id, username='armor-migration-user', email='armor@example.com'))
        db.add(RankDefinition(slug='iron', name='Iron', min_level=1, ability_slots=3, passive_slots=1, unlocks=[]))
        db.add(Adventurer(id=hero_id, name='Existing hero', owner=owner_id))
        db.add(Ability(id=passive_id, slug='custom_reaction', name='Custom Reaction', trigger_mode='on_hit',
                       effect_type='heal', target_type='self', power=10))
        db.add(GearDefinition(slug='existing-armor', name='Existing Armor', slot='Chest',
                              bonuses={'Defense':2}, required_rank='iron', price=10))
        db.flush()
        existing = Gear(adventurer_id=hero_id, definition_slug='existing-armor')
        db.add(existing); db.flush()
        db.add(EquippedGear(adventurer_id=hero_id, slot='Chest', gear_id=existing.id))
        db.add(AdventurerAbility(adventurer_id=hero_id, ability_id=passive_id))
        db.add(EquippedAbility(adventurer_id=hero_id, slot=0, ability_id=passive_id))
    with legacy.begin() as conn:
        upgrade(conn)
        upgrade(conn)
    with sessions() as db:
        assert db.get(Ability, passive_id) is None
        assert db.get(ArmorEffect, 'custom_reaction').definition['trigger_mode'] == 'on_hit'
        assert db.scalar(select(Gear).where(Gear.adventurer_id == hero_id,
                                          Gear.definition_slug == 'reaction-custom_reaction'))
        assert db.get(EquippedGear, (hero_id, 'Chest')).gear.definition_slug == 'existing-armor'
    legacy.dispose()


def test_armor_reactions_are_catalog_content_and_abilities_cannot_be_passive():
    from app.ability_design import validate_template
    from app.catalog_editor import validate_definition
    from app.models import ArmorEffect, GearDefinition
    with pytest.raises(ValueError, match='armor effects'):
        validate_template({'trigger_mode':'on_hit'})
    with SessionLocal() as db:
        armor_effect = ArmorEffect(slug='probe-reaction', name='Probe', description='',
                                   definition={'trigger_mode':'on_hit', 'effect_type':'heal',
                                               'target_type':'self', 'power':10,
                                               'proc_chance_percent':40})
        validate_definition(db, armor_effect)
        assert armor_effect.definition['trigger_mode'] == 'on_hit'
        armor_effect.definition['affliction_ops'] = [{'op':'apply', 'affliction':'poison', 'stacks':1}]
        with pytest.raises(ValueError, match='weapon effects'):
            validate_definition(db, armor_effect)
        gear = GearDefinition(slug='probe-gear', name='Probe armor', slot='Chest', bonuses={},
                              required_rank='iron', price=10, effect_slugs=['missing'])
        with pytest.raises(ValueError, match='Unknown armor effect'):
            validate_definition(db, gear)


def test_armor_migration_handles_gear_tables_created_before_effect_slugs():
    from sqlalchemy import create_engine, text
    from app.migrations.v023_equipment import upgrade as seed_gear
    from app.migrations.v036_armor_effects import upgrade as add_effects
    legacy = create_engine('sqlite:///:memory:')
    with legacy.begin() as conn:
        conn.execute(text('CREATE TABLE gear_definitions (slug VARCHAR PRIMARY KEY, name VARCHAR, slot VARCHAR, bonuses JSON, required_rank VARCHAR, price INTEGER)'))
        conn.execute(text('CREATE TABLE enemies (slug VARCHAR PRIMARY KEY)'))
        seed_gear(conn)
        add_effects(conn)
        add_effects(conn)
        assert conn.execute(text("SELECT effect_slugs FROM gear_definitions WHERE slug='leather-coat'")).scalar_one() == '[]'
    legacy.dispose()
