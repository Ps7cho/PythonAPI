from copy import deepcopy
from uuid import UUID
from app.database import SessionLocal
from app.models import Adventurer, Gear, GearDefinition
from app.combat import CombatAbility, execute_action


def create(client):
    hero = client.post('/api/adventurers', json={'name': 'Armored hero'}).json()['id']
    with SessionLocal.begin() as db:
        db.get(Adventurer, UUID(hero)).gold = 1000
    return hero


def buy(client, hero, slug='leather-coat'):
    response = client.post('/api/shop/purchase', json={'adventurer_id': hero, 'item_type': 'gear', 'item_slug': slug})
    assert response.status_code == 200, response.text
    return next(i for i in client.get('/api/adventurers/'+hero).json()['inventory'] if i.get('definition_slug') == slug)


def test_gear_hp_and_shared_combat_snapshot(client):
    hero = create(client)
    item = buy(client, hero)
    url = '/api/adventurers/'+hero
    result = client.post(url+'/equipment', json={'slot': 'Chest', 'gear_id': item['id']})
    assert result.status_code == 200
    assert result.json()['derived_stats']['max_hp'] == 106
    assert result.json()['health'] == 100
    sheet = client.get(url).json()
    assert sheet['attributes']['Defense'] == 0
    assert sheet['effective_attributes']['Defense'] == 4
    assert sheet['equipment']['Chest']['id'] == item['id']
    assert client.post(url+'/rest').json()['health'] == 106
    assert client.post(url+'/equipment', json={'slot': 'Chest', 'gear_id': None}).json()['health'] == 100
    assert client.post(url+'/equipment', json={'slot': 'Chest', 'gear_id': item['id']}).status_code == 200
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero]}).json()
    actor = encounter['participants'][0]
    assert actor['max_hp'] == 106
    assert actor['derived_stats']['damage_reduction_percent'] == 2.4
    target = deepcopy(actor)
    target['team'] = 'heroes'
    enemy = {'id': 'foe', 'team': 'enemies', 'name': 'Foe', 'hp': 100, 'power': 100}
    assert execute_action(enemy, CombatAbility('hit', 'Hit', damage=100), target, turn=1)['amount'] == 98
    assert client.post(url+'/equipment', json={'slot': 'Chest', 'gear_id': None}).status_code == 409
    with SessionLocal.begin() as db:
        original = dict(db.get(GearDefinition, 'leather-coat').bonuses)
        db.get(GearDefinition, 'leather-coat').bonuses = {'Defense': 100}
    try:
        assert client.get('/api/encounters/'+encounter['id']).json()['participants'][0]['derived_stats']['damage_reduction_percent'] == 2.4
    finally:
        with SessionLocal.begin() as db:
            db.get(GearDefinition, 'leather-coat').bonuses = original


def test_gear_ownership_slot_and_rank_checks(client):
    hero, other = create(client), create(client)
    item = buy(client, hero)
    assert client.post('/api/adventurers/'+other+'/equipment', json={'slot': 'Chest', 'gear_id': item['id']}).status_code == 422
    assert client.post('/api/adventurers/'+hero+'/equipment', json={'slot': 'Ring', 'gear_id': item['id']}).status_code == 422
    with SessionLocal.begin() as db:
        db.add(GearDefinition(slug='test-high-gear', name='High rank coat', slot='Chest', bonuses={}, required_rank='gold', price=1))
        db.flush()
        gear = Gear(adventurer_id=UUID(hero), definition_slug='test-high-gear')
        db.add(gear); db.flush(); gear_id = str(gear.id)
    assert client.post('/api/adventurers/'+hero+'/equipment', json={'slot': 'Chest', 'gear_id': gear_id}).status_code == 409


def test_gear_auction_escrow_and_return(client):
    hero = create(client)
    item = buy(client, hero, 'lucky-ring')
    equip = '/api/adventurers/'+hero+'/equipment'
    assert client.post(equip, json={'slot': 'Ring', 'gear_id': item['id']}).status_code == 200
    body = {'adventurer_id': hero, 'mode': 'fixed', 'gear_id': item['id'], 'price': 50}
    assert client.post('/api/auction-house', json=body).status_code == 409
    assert client.post(equip, json={'slot': 'Ring', 'gear_id': None}).status_code == 200
    response = client.post('/api/auction-house', json=body)
    assert response.status_code == 201
    with SessionLocal() as db:
        assert db.get(Gear, UUID(item['id'])) is None
    assert client.post('/api/auction-house/'+response.json()['id']+'/cancel').status_code == 200
    assert client.post(equip, json={'slot': 'Ring', 'gear_id': item['id']}).status_code == 200
