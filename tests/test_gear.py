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


def test_two_distinct_rings_stack_and_moving_one_never_duplicates_bonuses(client):
    hero = create(client)
    with SessionLocal.begin() as db:
        rings = [Gear(adventurer_id=UUID(hero), definition_slug='lucky-ring') for _ in range(2)]
        db.add_all(rings); db.flush(); ids = [str(r.id) for r in rings]
    url = '/api/adventurers/'+hero
    for slot, gear_id in zip(('Ring 1', 'Ring 2'), ids):
        result = client.post(url+'/equipment', json={'slot':slot, 'gear_id':gear_id})
        assert result.status_code == 200, result.text
    sheet = client.get(url).json()
    assert sheet['effective_attributes']['Luck'] == 6
    assert sheet['equipment']['Ring 1']['id'] != sheet['equipment']['Ring 2']['id']
    # Moving the same ring replaces the destination without equipping it twice.
    result = client.post(url+'/equipment', json={'slot':'Ring 2', 'gear_id':ids[0]})
    assert result.status_code == 200, result.text
    assert result.json()['equipment']['Ring 1'] is None
    assert result.json()['equipment']['Ring 2']['id'] == ids[0]
    assert client.get(url).json()['effective_attributes']['Luck'] == 3
    assert client.post(url+'/equipment', json={'slot':'Amulet', 'gear_id':ids[0]}).status_code == 422
    assert client.post(url+'/equipment', json={'slot':'Ring 2', 'gear_id':None}).status_code == 200
    assert client.get(url).json()['effective_attributes']['Luck'] == 0


def test_new_armor_slots_and_amulet_feed_existing_combat_snapshot(client):
    hero = create(client)
    url = '/api/adventurers/'+hero
    slots = ('Shoulders','Bracers','Belt','Cape','Amulet')
    definitions = ('alpha-wolf-shoulders','alpha-wolf-bracers','alpha-wolf-belt','alpha-wolf-cape','focus-amulet')
    with SessionLocal.begin() as db:
        gear = [Gear(adventurer_id=UUID(hero), definition_slug=slug) for slug in definitions]
        db.add_all(gear);db.flush();ids = [str(g.id) for g in gear]
    for slot, gear_id in zip(slots, ids):
        response = client.post(url+'/equipment', json={'slot':slot, 'gear_id':gear_id})
        assert response.status_code == 200, response.text
    sheet = client.get(url).json()
    assert all(sheet['equipment'][slot] for slot in slots)
    assert sheet['effective_attributes']['Vitality'] == 15
    assert sheet['effective_attributes']['Affinity'] == 3
    encounter = client.post('/api/encounters', json={'adventurer_ids':[hero]}).json()
    actor = encounter['participants'][0]
    assert actor['derived_stats'] == sheet['derived_stats']
    assert actor['max_hp'] == sheet['derived_stats']['max_hp']
    assert client.post(url+'/equipment', json={'slot':'Cape', 'gear_id':None}).status_code == 409


def test_full_equipment_migration_preserves_ring_and_extends_claimed_armor_once():
    from datetime import datetime, timedelta
    from sqlalchemy import create_engine, select, func
    from sqlalchemy.orm import sessionmaker
    from app.database import Base
    from app.models import User, EquippedGear, WorldBossEvent, WorldBossReward
    from app.migrations.v041_alpha_finale import upgrade as seed_old
    from app.migrations.v043_full_equipment import upgrade
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        seed_old(conn)
    sessions = sessionmaker(bind=engine)
    with sessions.begin() as db:
        user = User(username='legacy-equipment', email='legacy-equipment@example.test');db.add(user);db.flush()
        hero = Adventurer(name='Legacy set wearer', owner=user.id);db.add(hero);db.flush()
        db.add(GearDefinition(slug='legacy-ring', name='Legacy Ring', slot='Ring', bonuses={'Luck':3}, required_rank='iron', price=1))
        db.flush()
        ring = Gear(adventurer_id=hero.id, definition_slug='legacy-ring');db.add(ring);db.flush()
        db.add(EquippedGear(adventurer_id=hero.id, slot='Ring', gear_id=ring.id))
        original = [Gear(adventurer_id=hero.id, definition_slug='alpha-wolf-'+piece, rarity='epic', bound_account_id=user.id)
                    for piece in ('head','chest','hands','legs','feet')]
        db.add_all(original);db.flush()
        now = datetime.utcnow()
        event = WorldBossEvent(boss_slug='alpha-wolf', period='old-reward', status='defeated', health=0,
                              max_health=100, starts_at=now, ends_at=now+timedelta(minutes=10))
        db.add(event);db.flush()
        db.add(WorldBossReward(event_id=event.id, account_id=user.id, claimed=True, gear_ids=[str(g.id) for g in original]))
        hero_id, ring_id, account_id = hero.id, ring.id, user.id
    with engine.begin() as conn:
        upgrade(conn)
        upgrade(conn)
    with sessions() as db:
        assert db.get(EquippedGear, (hero_id,'Ring')) is None
        assert db.get(EquippedGear, (hero_id,'Ring 1')).gear_id == ring_id
        reward = db.scalar(select(WorldBossReward))
        assert reward.claimed and len(reward.gear_ids) == len(set(reward.gear_ids)) == 9
        pieces = list(db.scalars(select(Gear).where(Gear.bound_account_id == account_id)))
        assert len(pieces) == 9
        assert all(g.rarity == 'epic' and g.definition.slot not in ('Ring','Amulet') for g in pieces)
        assert db.scalar(select(func.count()).select_from(Gear)) == 10
    engine.dispose()
