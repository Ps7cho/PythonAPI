from uuid import uuid4

from app.combat import CombatAbility, execute_action


def character(client):
    hero = client.post('/api/adventurers', json={'name':'Loadout Test'}).json()
    sheet = client.get('/api/adventurers/' + hero['id']).json()
    return hero, {a['name']: a['id'] for a in sheet['abilities']}


def test_favorites_persist_and_all_learned_abilities_can_execute(client):
    hero, abilities = character(client)
    url = '/api/adventurers/' + hero['id'] + '/loadout'
    selected = [abilities['Strike'], abilities['Second Wind']]
    assert client.post(url, json={'ability_ids': selected}).status_code == 200
    sheet = client.get('/api/adventurers/' + hero['id']).json()
    assert sheet['equipped_ability_ids'] == selected
    encounter = client.post('/api/encounters', json={'adventurer_ids':[hero['id']]}).json()
    assert set(selected + [abilities['Power Strike']]) <= {a['slug'] for a in encounter['participants'][0]['equipped_abilities']}
    action_url = '/api/encounters/' + encounter['id'] + '/actions'
    assert client.post(action_url, json={'actor_id':hero['id'], 'expected_turn':1, 'ability_id':str(uuid4())}).status_code == 409
    assert client.post(url, json={'ability_ids':[abilities['Strike']]}).status_code == 409
    result = client.post(action_url, json={'actor_id':hero['id'], 'expected_turn':1, 'ability_id':abilities['Power Strike']})
    assert result.status_code == 200
    heal = client.post(action_url, json={'actor_id':hero['id'], 'expected_turn':2, 'ability_id':abilities['Second Wind']})
    assert heal.status_code == 200
    assert heal.json()['action_results'][0]['effect'] == 'heal'
    assert heal.json()['action_results'][0]['amount'] == 6
    assert client.post(action_url, json={'actor_id':hero['id'], 'expected_turn':3, 'ability_id':abilities['Second Wind']}).status_code == 409


def test_invalid_loadouts_and_ownership(client):
    hero, abilities = character(client)
    url = '/api/adventurers/' + hero['id'] + '/loadout'
    for ids in ([], [abilities['Strike']] * 2, [str(uuid4())], [abilities['Second Wind']]):
        assert client.post(url, json={'ability_ids':ids}).status_code == 422
    client.post('/api/auth/logout')
    assert client.post(url, json={'ability_ids':[abilities['Strike']]}).status_code == 401


def test_legacy_encounter_gains_learned_skills_but_excludes_locked_skills(client):
    from copy import deepcopy
    from uuid import UUID
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import AdventurerAbility, Encounter

    hero, abilities = character(client)
    with SessionLocal.begin() as db:
        entry = db.scalar(select(AdventurerAbility).where(
            AdventurerAbility.adventurer_id == UUID(hero['id']),
            AdventurerAbility.ability_id == UUID(abilities['Second Wind'])))
        entry.unlocked = False
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(encounter['id']))
        participants = deepcopy(saved.participants)
        actor = participants[0]
        actor.pop('weapons')
        actor['equipped_abilities'] = [a for a in actor['equipped_abilities'] if a['slug'] == abilities['Strike']]
        actor['equipped_weapon']['base_damage'] = 3
        saved.participants = participants
    url = '/api/encounters/' + encounter['id']
    actor = client.get(url).json()['participants'][0]
    assert abilities['Power Strike'] in {a['slug'] for a in actor['equipped_abilities']}
    assert abilities['Second Wind'] not in {a['slug'] for a in actor['equipped_abilities']}
    weapon_id = actor['equipped_weapon']['id']
    assert next(w for w in actor['weapons'] if w['id'] == weapon_id)['base_damage'] == 3
    action = {'actor_id': hero['id'], 'expected_turn': 1}
    assert client.post(url + '/actions', json={**action, 'ability_id': abilities['Second Wind']}).status_code == 409
    result = client.post(url + '/actions', json={**action, 'action': 'power_strike', 'weapon_id': weapon_id})
    assert result.status_code == 200
    assert result.json()['action_results'][0]['amount'] == 5


def test_party_buff_executes_once_and_database_power_drives_combat(client):
    hero, abilities = character(client)
    other = client.post('/api/adventurers', json={'name':'Other'}).json()
    client.post('/api/adventurers/' + hero['id'] + '/loadout', json={'ability_ids':[abilities['Strike'], abilities['Battle Cry']]})
    encounter = client.post('/api/encounters', json={'adventurer_ids':[hero['id'],other['id']]}).json()
    result = client.post('/api/encounters/' + encounter['id'] + '/actions', json={
        'actor_id':hero['id'], 'expected_turn':1, 'ability_id':abilities['Battle Cry']})
    assert result.status_code == 200
    assert len(result.json()['action_results']) == 2
    assert all(p['buff']['power'] == 20 for p in result.json()['participants'])


def test_shield_and_heal_effects():
    actor={'id':'a','name':'A','team':'players','hp':5,'max_hp':100}
    foe={'id':'b','name':'B','team':'enemies','hp':10,'power':40}
    execute_action(actor, CombatAbility('shield','Shield',effect='shield'), actor, turn=1)
    execute_action(foe, CombatAbility('hit','Hit'), actor, turn=1)
    assert actor['hp'] == 1
    execute_action(actor, CombatAbility('heal','Heal',effect='heal',damage=30), actor, turn=2)
    assert actor['hp'] == 31
    execute_action(foe, CombatAbility('hit','Hit'), actor, turn=4)
    assert actor['hp'] == 0
