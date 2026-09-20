from copy import deepcopy
from dataclasses import asdict
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select, text

from app.ability_design import effective_ability, validate_template
from app.combat import CombatAbility, InvalidCombatAction, execute_cast
from app.database import SessionLocal
from app.enemy_intents import execute_planned, preview_moves
from app.loadouts import combat_loadout
from app.models import Ability, AbilityArchetype, Adventurer, AdventurerAbility, Encounter, User


def fighter(id, team='party', hp=100):
    return dict(id=id, name=id, team=team, hp=hp, max_hp=100, power=20)


def step(id='drain', **kwargs):
    return dict(id=id, effect='heal', recipient='self', source='damage_dealt', value=50, **kwargs)


def test_drain_uses_actual_damage_after_guard_and_overkill():
    actor, enemy = fighter('a', hp=50), fighter('e', 'enemy', hp=5)
    enemy.update(guarding=True, guard_turn=1, guard_reduction_percent=60)
    ability = CombatAbility('drain', 'Drain', damage=100, effect_chain=[step()])
    results = execute_cast(actor, ability, [enemy], turn=1)
    assert [r['amount'] for r in results] == [5, 2]
    assert actor['hp'] == 52


def test_party_split_conserves_total_and_previous_uses_actual_healing():
    actor, ally, dead, enemy = fighter('a', hp=99), fighter('b', hp=30), fighter('c', hp=0), fighter('e', 'enemy')
    ability = CombatAbility('siphon', 'Siphon', damage=11, effect_chain=[
        dict(id='share', effect='heal', recipient='party', source='damage_dealt', value=100, split=True),
        dict(id='mana', effect='resource', recipient='self', source='previous', value=100),
    ])
    results = execute_cast(actor, ability, [enemy], turn=1, combatants=[actor, ally, dead, enemy])
    assert [r['amount'] for r in results] == [11, 1, 5, 6]
    assert actor['resources']['mana'] == 6 and dead['hp'] == 0


def test_dodge_does_not_generate_damage_healing_or_mana():
    actor, enemy = fighter('a', hp=50), fighter('e', 'enemy')
    enemy['evasion'] = dict(chance_percent=100, until_turn=3)
    ability = CombatAbility('drain', 'Drain', damage=20, effect_chain=[step()])
    results = execute_cast(actor, ability, [enemy], turn=1)
    assert len(results) == 1 and results[0]['dodged']
    assert actor['hp'] == 50


def test_chained_damage_uses_shared_resolver_and_charges_one_cooldown():
    actor, enemy = fighter('a'), fighter('e', 'enemy')
    enemy['derived_stats'] = {'damage_reduction_percent': 50}
    ability = CombatAbility('combo', 'Combo', damage=20, cooldown_turns=3, effect_chain=[
        dict(id='echo', effect='damage', recipient='targets', source='damage_dealt', value=100),
        dict(id='mana', effect='resource', recipient='self', source='previous', value=100),
    ])
    results = execute_cast(actor, ability, [enemy], turn=1)
    assert [r['amount'] for r in results] == [10, 5, 5]
    assert actor['ability_ready_turns'] == {'combo': 4}


def test_live_modifiers_refresh_expire_and_do_not_rewrite_base_values():
    caster, ally, enemy = fighter('a'), fighter('b'), fighter('e', 'enemy')
    buff = CombatAbility('buff', 'Empower', effect='affliction', target_type='ally', effect_chain=[
        dict(id='boost', effect='modifier', recipient='targets', source='fixed', when='always',
             stat='damage_multiplier', operation='percent', modifier=50, duration=1),
    ])
    strike = CombatAbility('strike', 'Strike', damage_multiplier=1.0)
    for _ in range(2): execute_cast(caster, buff, [ally], turn=1)
    assert len(ally['ability_modifiers']) == 1
    assert execute_cast(ally, strike, [enemy], turn=1)[0]['amount'] == 30
    assert execute_cast(ally, strike, [enemy], turn=2)[0]['amount'] == 20
    assert strike.damage_multiplier == 1


def test_nerf_changes_announced_enemy_damage_and_preview_is_read_only():
    actor, enemy = fighter('a'), fighter('e', 'enemy')
    attack = CombatAbility('hit', 'Hit', damage=20)
    enemy['intent'] = dict(turn=1, ability=asdict(attack), target_ids=['a'])
    nerf = CombatAbility('nerf', 'Weaken', effect='affliction', effect_chain=[
        dict(id='weak', effect='modifier', recipient='targets', source='fixed', when='always',
             stat='power', operation='percent', modifier=-50, duration=1),
    ])
    execute_cast(actor, nerf, [enemy], turn=1)
    before = deepcopy([actor, enemy])
    preview = preview_moves([actor], [enemy], 1, lambda _: None)
    assert preview['e']['targets'][0]['amount'] == 10
    assert [actor, enemy] == before
    assert execute_planned(enemy, [actor], [enemy], 1, None)[0]['amount'] == 10


def test_invalid_followup_rolls_back_entire_cast():
    actor, enemy = fighter('a'), fighter('e', 'enemy')
    ability = CombatAbility('bad', 'Bad', damage=20, cooldown_turns=2,
                            effect_chain=[dict(id='bad', effect='heal', recipient='targets')])
    before = deepcopy([actor, enemy])
    with pytest.raises(InvalidCombatAction): execute_cast(actor, ability, [enemy], turn=1)
    assert [actor, enemy] == before


def test_rank_upgrades_are_saved_values_and_departure_snapshot_is_stable(client):
    hero_id = UUID(client.post('/api/adventurers', json={'name': 'Rank test'}).json()['id'])
    slug = 'rank-test-' + uuid4().hex
    with SessionLocal.begin() as db:
        ability = Ability(slug=slug, name=slug, power=10, effect_chain=[step()],
                          rank_upgrades={'bronze': {'power': 30, 'step:drain': 75}})
        db.add(ability); db.flush(); ability_id = ability.id
        db.add(AdventurerAbility(adventurer_id=hero_id, ability_id=ability_id))
    try:
        with SessionLocal.begin() as db:
            hero = db.get(Adventurer, hero_id)
            base = next(a for a in combat_loadout(db, hero) if a['catalog_slug'] == slug)
            hero.level = 10
            upgraded = next(a for a in combat_loadout(db, hero) if a['catalog_slug'] == slug)
            assert base['damage'] == 10 and upgraded['damage'] == 30
            assert upgraded['effect_chain'][0]['value'] == 75
            db.get(Ability, ability_id).power = 999
            assert upgraded['damage'] == 30
        sheet = client.get('/api/adventurers/' + str(hero_id)).json()
        skill = next(a for a in sheet['abilities'] if a['slug'] == slug)
        assert skill['rank_values']['power'] == 30
    finally:
        with SessionLocal.begin() as db:
            for entry in db.scalars(select(AdventurerAbility).where(AdventurerAbility.ability_id == ability_id)): db.delete(entry)
            db.delete(db.get(Ability, ability_id))


def test_catalog_roundtrip_template_and_invalid_program_rejection(client):
    username = client.get('/api/auth/me').json()['username']
    with SessionLocal.begin() as db:
        db.scalar(select(User).where(User.username == username)).account_type = 'developer'
    catalogs = client.get('/api/abilities?inspect=true').json()['editor']['catalogs']
    template = next(r for r in catalogs['ability_archetypes']['records'] if r['values']['slug'] == 'party_siphon')
    assert template['values']['definition']['effect_chain'][0]['split']
    record = catalogs['abilities']['records'][0]
    values = {**record['values'], **template['values']['definition'], 'archetype_slug': 'party_siphon'}
    payload = dict(catalog='abilities', key=record['key'], values=values, expected_revision=record['revision'], validate_only=True)
    response = client.post('/api/catalog-editor', json=payload)
    assert response.status_code == 200, response.text
    for chain in [[dict(id='unsafe', effect='run_python')], [dict(id='bad', effect='heal', recipient='enemies')], [step(), step()]]:
        invalid = client.post('/api/catalog-editor', json={**payload, 'values': {**values, 'effect_chain': chain}})
        assert invalid.status_code == 422
    assert client.post('/api/catalog-editor', json={**payload, 'values': {**values, 'rank_upgrades': {'missing': {'power': 20}}}}).status_code == 422


def test_migration_preserves_legacy_content_and_is_repeatable():
    from app.migrations.v027_ability_design import upgrade
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE abilities (slug VARCHAR, effect_type VARCHAR, power INTEGER)'))
        conn.execute(text("INSERT INTO abilities VALUES ('custom', 'evade', 73)"))
        upgrade(conn); upgrade(conn)
        row = conn.execute(text('SELECT * FROM abilities')).mappings().one()
        assert row['power'] == 73 and row['duration_turns'] == 2 and row['effect_chain'] == '[]'
        templates = list(conn.execute(select(AbilityArchetype.__table__)).mappings())
        assert len(templates) == 10
        for item in templates: validate_template(item['definition'])
    engine.dispose()


def test_saved_ability_executes_chain_and_keeps_snapshot_after_catalog_edit(client):
    hero = client.post('/api/adventurers', json={'name': 'Chain runner'}).json()
    hero_id = UUID(hero['id'])
    username = client.get('/api/auth/me').json()['username']
    with SessionLocal.begin() as db:
        db.scalar(select(User).where(User.username == username)).account_type = 'developer'
    record = client.get('/api/abilities?inspect=true').json()['editor']['catalogs']['abilities']['records'][0]
    ability_id = uuid4()
    values = {**record['values'], 'id':str(ability_id), 'slug':ability_id.hex, 'name':'Drain '+ability_id.hex,
              'effect_type':'damage', 'target_type':'enemy', 'power':10, 'damage_multiplier':None,
              'requires_weapon':False, 'status_effect_slug':None, 'affliction_ops':[], 'rank_upgrades':{},
              'cooldown_value':0, 'max_targets':1,
              'effect_chain':[step(), dict(id='mana',effect='resource',recipient='self',source='previous',value=100)]}
    response = client.post('/api/catalog-editor', json=dict(catalog='abilities', key={'id':str(ability_id)}, values=values, create=True))
    assert response.status_code == 200, response.text
    try:
        with SessionLocal.begin() as db:
            db.add(AdventurerAbility(adventurer_id=hero_id, ability_id=ability_id))
            db.get(Adventurer, hero_id).health = 50
        encounter = client.post('/api/encounters', json={'adventurer_ids':[hero['id']]}).json()
        with SessionLocal.begin() as db:
            db.get(Ability, ability_id).effect_chain = []
        url = '/api/encounters/' + encounter['id']
        command = dict(actor_id=hero['id'], expected_turn=1, ability_id=str(ability_id))
        response = client.post(url+'/actions', json=command)
        assert response.status_code == 200, response.text
        state = response.json()
        assert [r['amount'] for r in state['action_results'][:3]] == [10, 5, 5]
        assert state['participants'][0]['resources']['mana'] == 5
        assert client.post(url+'/actions', json=command).status_code == 409
        loaded = client.get(url).json()
        assert loaded['participants'][0]['resources']['mana'] == 5
        assert loaded['combat_log'] == state['combat_log']
        with SessionLocal() as db:
            saved = db.get(Encounter, UUID(encounter['id']))
            assert saved.participants[0]['resources']['mana'] == 5
    finally:
        with SessionLocal.begin() as db:
            for entry in db.scalars(select(AdventurerAbility).where(AdventurerAbility.ability_id == ability_id)): db.delete(entry)
            db.delete(db.get(Ability, ability_id))


def test_modifier_can_tune_a_followup_value_and_specific_ability_only():
    actor, enemy = fighter('a', hp=20), fighter('e', 'enemy')
    actor['ability_modifiers'] = [dict(key='boost', stat='step:drain', operation='percent', value=100,
                                      until_turn=2, ability_slug='drain')]
    drain = CombatAbility('id', 'Drain', catalog_slug='drain', damage=20, effect_chain=[step()])
    assert effective_ability(actor, drain, 1).effect_chain[0]['value'] == 100
    results = execute_cast(actor, drain, [enemy], turn=1)
    assert results[1]['amount'] == 20
    assert drain.effect_chain[0]['value'] == 50
    other = CombatAbility('other', 'Other', damage=20, effect_chain=[step()])
    assert effective_ability(actor, other, 1).effect_chain[0]['value'] == 50


def test_timed_cooldown_modifier_uses_catalog_minutes():
    actor = fighter('a')
    actor['ability_modifiers'] = [dict(key='quick', stat='cooldown_value', operation='add', value=-2,
                                      until_turn=2, ability_slug=None)]
    ability = CombatAbility('id', 'Spell', cooldown_seconds=600, cooldown_unit_seconds=60)
    assert effective_ability(actor, ability, 1).cooldown_seconds == 480
