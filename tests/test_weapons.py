from copy import deepcopy
from uuid import UUID

import pytest

from app.combat import CombatAbility, InvalidCombatAction, execute_action
from app.database import SessionLocal
from app.models import Weapon


def test_weapon_damage_and_nonweapon_damage_are_distinct():
    actor = {'id': 'hero', 'name': 'Hero', 'team': 'players', 'hp': 100, 'power': 5,
             'equipped_weapon': {'base_damage': 20, 'tags': ['sword', 'melee']}}
    target = {'id': 'enemy', 'name': 'Enemy', 'team': 'enemies', 'hp': 100}
    strike = CombatAbility('strike', 'Strike', requires_weapon=True,
                           allowed_weapon_tags=['axe', 'sword'], damage_multiplier=1.6)
    assert execute_action(actor, strike, target, turn=1)['amount'] == 32
    spell = CombatAbility('spell', 'Spell', damage_multiplier=1.6)
    assert execute_action(actor, spell, target, turn=1)['amount'] == 8
    actor['equipped_weapon'] = None
    assert execute_action(actor, spell, target, turn=2)['amount'] == 8
    before = deepcopy((actor, target))
    with pytest.raises(InvalidCombatAction, match='equipped weapon'):
        execute_action(actor, strike, target, turn=2)
    assert (actor, target) == before
    actor['equipped_weapon'] = {'base_damage': 90, 'tags': ['ranged', 'bow']}
    before = deepcopy((actor, target))
    with pytest.raises(InvalidCombatAction, match='weapon tags'):
        execute_action(actor, strike, target, turn=2)
    assert (actor, target) == before


def test_equipment_ownership_requirements_and_encounter_snapshot(client):
    hero = client.post('/api/adventurers', json={'name': 'Weapon test'}).json()
    other = client.post('/api/adventurers', json={'name': 'Other owner'}).json()
    url = '/api/adventurers/' + hero['id'] + '/weapon'
    owned = client.get('/api/adventurers/' + hero['id'] + '/weapons').json()
    sword_id = owned['equipped_weapon']['id']
    other_weapon = client.get('/api/adventurers/' + other['id'] + '/weapons').json()['equipped_weapon']['id']
    assert client.post(url, json={'weapon_id': other_weapon}).status_code == 422
    with SessionLocal.begin() as db:
        bow = Weapon(adventurer_id=UUID(hero['id']), weapon_type_slug='bow', name='Test bow', base_damage=50)
        db.add(bow)
        db.flush()
        bow_id = str(bow.id)
    assert client.post(url, json={'weapon_id': bow_id}).status_code == 200
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    action_url = '/api/encounters/' + encounter['id'] + '/actions'
    assert client.post(url, json={'weapon_id': sword_id}).status_code == 409
    before = client.get('/api/encounters/' + encounter['id']).json()
    response = client.post(action_url, json={'actor_id': hero['id'], 'expected_turn': 1, 'action': 'power_strike'})
    assert response.status_code == 409
    assert 'weapon tags' in response.json()['detail']
    assert client.get('/api/encounters/' + encounter['id']).json() == before
    # Editing a weapon record cannot change an already-created encounter.
    with SessionLocal.begin() as db:
        db.get(Weapon, UUID(bow_id)).base_damage = 99
    assert client.get('/api/encounters/' + encounter['id']).json()['participants'][0]['equipped_weapon']['base_damage'] == 50
    client.post('/api/auth/logout')
    assert client.post(url, json={'weapon_id': sword_id}).status_code == 401


def test_unarmed_can_guard_but_cannot_use_weapon_attack(client):
    hero = client.post('/api/adventurers', json={'name': 'Unarmed'}).json()
    assert client.post('/api/adventurers/' + hero['id'] + '/weapon', json={'weapon_id': None}).status_code == 200
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    url = '/api/encounters/' + encounter['id'] + '/actions'
    action = {'actor_id': hero['id'], 'expected_turn': 1}
    assert client.post(url, json={**action, 'action': 'attack'}).status_code == 409
    assert client.post(url, json={**action, 'action': 'guard'}).status_code == 200
    types = client.get('/api/weapon-types').json()
    assert next(t for t in types if t['slug'] == 'bow')['tags'] == ['weapon', 'ranged', 'bow', 'two_handed', 'piercing']


def test_pocket_dimension_switches_owned_weapons_atomically(client):
    hero = client.post('/api/adventurers', json={'name': 'Pocket arsenal'}).json()
    other = client.post('/api/adventurers', json={'name': 'Other arsenal'}).json()
    sword = client.get('/api/adventurers/' + hero['id'] + '/weapons').json()['equipped_weapon']
    foreign = client.get('/api/adventurers/' + other['id'] + '/weapons').json()['equipped_weapon']
    with SessionLocal.begin() as db:
        bow = Weapon(adventurer_id=UUID(hero['id']), weapon_type_slug='bow', name='Pocket bow',
                     base_damage=2, required_rank='Gold')
        db.add(bow)
        db.flush()
        bow_id = str(bow.id)
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    url = '/api/encounters/' + encounter['id'] + '/actions'
    assert {sword['id'], bow_id} == {w['id'] for w in encounter['participants'][0]['weapons']}
    with SessionLocal.begin() as db:
        db.get(Weapon, UUID(bow_id)).base_damage = 999
    action = {'actor_id': hero['id'], 'expected_turn': 1, 'action': 'power_strike'}
    before = client.get('/api/encounters/' + encounter['id']).json()
    for weapon_id in [foreign['id'], bow_id]:
        assert client.post(url, json={**action, 'weapon_id': weapon_id}).status_code == 409
        assert client.get('/api/encounters/' + encounter['id']).json() == before
    result = client.post(url, json={**action, 'action': 'attack', 'weapon_id': bow_id})
    assert result.status_code == 200
    assert result.json()['action_results'][0]['amount'] == 2
    assert result.json()['participants'][0]['equipped_weapon']['id'] == bow_id
    result = client.post(url, json={**action, 'expected_turn': 2, 'weapon_id': sword['id']})
    assert result.status_code == 200
    assert result.json()['action_results'][0]['amount'] == 16
    before = client.get('/api/encounters/' + encounter['id']).json()
    retry = client.post(url, json={**action, 'expected_turn': 3, 'weapon_id': sword['id']})
    assert retry.status_code == 409
    assert 'cooldown' in retry.json()['detail']
    assert client.get('/api/encounters/' + encounter['id']).json() == before
    assert client.get('/api/adventurers/' + hero['id'] + '/weapons').json()['equipped_weapon']['id'] == sword['id']
