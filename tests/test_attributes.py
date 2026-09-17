from copy import deepcopy

class NoProc:
    def random(self): return 0.999

from uuid import UUID

import pytest
from app.attributes import derived_stats, DESCRIPTIONS
from app.combat import CombatAbility, execute_action, apply_status, tick_statuses
from app.database import SessionLocal
from app.models import Adventurer, Encounter
from app import main


def fighter(id, team, attributes=None):
    stats = derived_stats(attributes or {})
    return dict(id=id, name=id, team=team, hp=stats['max_hp'], max_hp=stats['max_hp'],
                power=20, derived_stats=stats, equipped_weapon={'base_damage': 20, 'tags': ['melee']})


@pytest.mark.parametrize('attribute', DESCRIPTIONS)
def test_every_attribute_changes_derived_stats(attribute):
    assert derived_stats({attribute: 10}) != derived_stats({})


def test_combined_build_and_caps():
    stats = derived_stats({name: 10 for name in DESCRIPTIONS})
    assert stats == dict(critical_chance_percent=6, flinch_chance_percent=4, max_hp=140, weapon_bonus_percent=30, spell_bonus_percent=30,
                        healing_bonus_percent=20, damage_reduction_percent=9,
                        guard_bonus=4, status_resistance_percent=10)
    high = derived_stats({name: 1000 for name in DESCRIPTIONS})
    assert high['damage_reduction_percent'] == 60
    assert high['status_resistance_percent'] == 50


def test_weapon_and_nonweapon_scale_from_distinct_pairs():
    attacker = fighter('a', 'players', {'Might': 20, 'Precision': 10, 'Willpower': 10, 'Affinity': 5})
    enemy = fighter('e', 'enemies')
    weapon = CombatAbility('hit', 'Hit', damage_multiplier=1, requires_weapon=True)
    assert execute_action(attacker, weapon, enemy, turn=1, rng=NoProc())['amount'] == 30
    spell = CombatAbility('spell', 'Spell', damage_multiplier=1)
    assert execute_action(attacker, spell, enemy, turn=2, rng=NoProc())['amount'] == 24


def test_defense_agility_and_guard_share_mitigation():
    attacker = fighter('a', 'enemies')
    defender = fighter('d', 'players', {'Defense': 20, 'Agility': 10, 'Awareness': 10, 'Speed': 15})
    hit = CombatAbility('hit', 'Hit', damage=40)
    assert execute_action(attacker, hit, defender, turn=1)['amount'] == 34
    execute_action(defender, CombatAbility('guard', 'Guard', effect='guard', target_type='self', damage=4), defender, turn=2)
    assert defender['guard_power'] == 9
    assert execute_action(attacker, hit, defender, turn=2)['amount'] == 25


def test_luck_vitality_status_ward_combines_with_type_resistance():
    target = fighter('p', 'players', {'Luck': 40, 'Vitality': 60})
    target['status_resistances'] = {'poison': 50, 'bleed': 100}
    poison = dict(slug='poison', name='Poison', damage=20, duration=2, max_stacks=1)
    apply_status(target, poison, 'e')
    assert target['statuses'][0]['resistance'] == 75
    assert tick_statuses([target], turn=1)[0]['amount'] == 5
    assert 'immune' in apply_status(target, {**poison, 'slug': 'bleed'}, 'e')


def test_ability_healing_scales_but_consumables_keep_potency():
    caster = fighter('p', 'players', {'Willpower': 20, 'Affinity': 30})
    target = fighter('t', 'players', {'Vitality': 30, 'Willpower': 10})
    target['hp'] = 1
    heal = CombatAbility('heal', 'Heal', effect='heal', target_type='ally', damage=20)
    assert execute_action(caster, heal, target, turn=1)['amount'] == 60
    potion = CombatAbility('potion', 'Potion', effect='heal', target_type='ally', damage=20, scales_with_attributes=False)
    assert execute_action(caster, potion, target, turn=1)['amount'] == 40


def test_real_build_creation_allocation_rest_and_snapshot(client, monkeypatch):
    monkeypatch.setattr(main, 'generate_attribute_budget', lambda: {name: 10 for name in DESCRIPTIONS})
    hero = client.post('/api/adventurers', json={'name': 'Balanced', 'health': 99999, 'attributes': {'Might': 99999}}).json()
    id = hero['id']
    assert hero['health'] == 140
    assert sum(hero['attributes'].values()) == 100
    with SessionLocal.begin() as db:
        db.get(Adventurer, UUID(id)).attribute_points = 3
    allocated = client.post(f'/api/adventurers/{id}/attributes', json={'allocations': {'Vitality': 3}})
    assert allocated.status_code == 200
    sheet = client.get(f'/api/adventurers/{id}').json()
    assert sheet['health'] == 140 and sheet['max_health'] == 149
    assert client.post(f'/api/adventurers/{id}/rest').json()['health'] == 149
    monkeypatch.setattr('app.encounter_service.combat_rng', lambda *args: NoProc())
    e = client.post('/api/encounters', json={'adventurer_ids': [id]}).json()
    assert e['participants'][0]['max_hp'] == 149
    assert e['participants'][0]['derived_stats'] == sheet['derived_stats']
    assert client.post(f'/api/adventurers/{id}/attributes', json={'allocations': {'Might': 1}}).status_code == 409
    result = client.post('/api/encounters/' + e['id'] + '/actions', json={
        'actor_id': id, 'expected_turn': 1, 'action': 'attack', 'derived_stats': {'weapon_bonus_percent': 99999}}).json()
    assert result['action_results'][0]['amount'] == 13
    assert result['participants'][0]['hp'] == 144  # Six base enemy damage, 9% mitigation.
    with SessionLocal() as db:
        assert db.get(Adventurer, UUID(id)).health == 144


def test_legacy_snapshot_keeps_original_damage_and_health(client):
    hero = client.post('/api/adventurers', json={'name': 'Legacy'}).json()['id']
    e = client.post('/api/encounters', json={'adventurer_ids': [hero]}).json()
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(e['id']))
        players = deepcopy(saved.participants)
        players[0].pop('derived_stats')
        saved.participants = players
        db.get(Adventurer, UUID(hero)).attributes = {name: 100 for name in DESCRIPTIONS}
    sheet = client.get('/api/adventurers/' + hero).json()
    assert sheet['max_health'] == 100 and sheet['derived_stats'] == {}
    state = client.post('/api/encounters/' + e['id'] + '/actions', json={
        'actor_id': hero, 'expected_turn': 1, 'action': 'attack'}).json()
    assert state['action_results'][0]['amount'] == 10
    assert state['participants'][0]['hp'] == 94
