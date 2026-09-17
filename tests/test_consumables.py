from copy import deepcopy
from uuid import UUID

import pytest
from app.database import SessionLocal
from app.models import Adventurer, Encounter, OwnedConsumable
from app.consumables import grant
from app.combat import apply_status
from app.status_content import EFFECTS
from test_encounters import create, act
from test_encounter_groups import start, clear_group, advance


def stock(encounter, slug, quantity=2):
    hero = UUID(encounter['participants'][0]['id'])
    with SessionLocal.begin() as db:
        grant(db, hero, slug, quantity)


def quantity(encounter, slug):
    with SessionLocal() as db:
        row = db.get(OwnedConsumable, (UUID(encounter['participants'][0]['id']), slug))
        return row.quantity if row else 0


def use(client, encounter, slug, **extra):
    return client.post('/api/encounters/' + encounter['id'] + '/actions', json={
        'actor_id': encounter['participants'][0]['id'], 'expected_turn': encounter['turn'],
        'consumable_slug': slug, **extra})


def hurt(encounter, index=0, hp=40, status=False):
    with SessionLocal.begin() as db:
        row = db.get(Encounter, UUID(encounter['id']))
        players = deepcopy(row.participants)
        players[index]['hp'] = hp
        if status:
            for effect in EFFECTS:
                apply_status(players[index], effect, 'enemy')
        row.participants = players


@pytest.mark.parametrize('target_index', [0, 1])
def test_heal_self_or_ally_spends_one_and_rejects_retry(client, target_index):
    encounter = create(client, 2)
    stock(encounter, 'healing-potion')
    hurt(encounter, target_index, 80)
    extra = {'target_id': encounter['participants'][1]['id']} if target_index else {}
    response = use(client, encounter, 'healing-potion', **extra)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state['action_results'][0]['amount'] == 20
    assert state['participants'][target_index]['hp'] == 100
    assert state['participants'][0]['acted']
    assert quantity(encounter, 'healing-potion') == 1
    assert use(client, encounter, 'healing-potion', **extra).status_code == 409
    assert quantity(encounter, 'healing-potion') == 1
    with SessionLocal() as db:
        assert db.get(Adventurer, UUID(state['participants'][target_index]['id'])).health == 100


def test_cleanse_removes_all_statuses_from_ally(client):
    encounter = create(client, 2)
    stock(encounter, 'cleansing-draught')
    hurt(encounter, 1, status=True)
    state = use(client, encounter, 'cleansing-draught', target_id=encounter['participants'][1]['id']).json()
    assert state['action_results'][0]['amount'] == 4
    assert state['participants'][1]['statuses'] == []
    assert quantity(encounter, 'cleansing-draught') == 1


def test_boost_uses_existing_damage_resolver_and_expires(client):
    encounter = create(client)
    stock(encounter, 'might-tonic')
    boosted = use(client, encounter, 'might-tonic').json()
    assert boosted['participants'][0]['buff'] == {'power': 25, 'until_turn': 4}
    hit = act(client, boosted).json()
    assert hit['action_results'][0]['amount'] == 12
    hit = act(client, hit, action='wait').json()
    hit = act(client, hit).json()
    assert hit['action_results'][0]['amount'] == 10


@pytest.mark.parametrize('invalid', ['full', 'clean', 'enemy', 'dead', 'unknown', 'mixed', 'multiple'])
def test_invalid_use_does_not_spend_or_mutate(client, invalid):
    encounter = create(client, 2)
    slug = 'cleansing-draught' if invalid == 'clean' else 'healing-potion'
    stock(encounter, slug)
    extra = {}
    if invalid == 'enemy': extra['target_id'] = encounter['enemies'][0]['id']
    if invalid == 'dead':
        hurt(encounter, 1, 0)
        extra['target_id'] = encounter['participants'][1]['id']
    if invalid == 'mixed': extra['action'] = 'wait'
    if invalid == 'multiple': extra['target_ids'] = [encounter['participants'][0]['id']]
    before = client.get('/api/encounters/' + encounter['id']).json()
    response = use(client, encounter, 'missing' if invalid == 'unknown' else slug, **extra)
    assert response.status_code in (400, 409, 422)
    assert quantity(encounter, slug) == 2
    assert client.get('/api/encounters/' + encounter['id']).json() == before


def test_empty_stock_and_stale_turn_rejected(client):
    encounter = create(client)
    stock(encounter, 'might-tonic', 1)
    state = use(client, encounter, 'might-tonic').json()
    assert quantity(encounter, 'might-tonic') == 0
    assert use(client, encounter, 'might-tonic').status_code == 409
    assert use(client, state, 'might-tonic').status_code == 409


def test_consumable_loot_claims_once_per_survivor_and_carries_into_combat(client, monkeypatch):
    monkeypatch.setattr('app.group_journeys.roll_loot', lambda tier, rng:
        deepcopy(next(d for d in tier['drops'] if d.get('consumable_slug') == 'healing-potion')))
    ids, encounter = start(client, count=2)
    encounter = clear_group(client, encounter)
    assert quantity(encounter, 'healing-potion') == 0
    returned = advance(client, encounter, {'return_to_village': True})
    assert returned['quest']['group']['loot_claimed']
    for hero in ids:
        items = client.get('/api/adventurers/' + hero).json()['consumables']
        assert items[0]['slug'] == 'healing-potion' and items[0]['quantity'] == 2
    assert client.post('/api/encounters/' + encounter['id'] + '/continue', json={'return_to_village': True}).status_code == 409
    new = client.post('/api/encounters', json={'adventurer_ids': ids}).json()
    assert new['participants'][0]['consumables'][0]['quantity'] == 2


def test_loot_drop_validation():
    from app.group_journeys import LootDrop
    for data in [dict(name='Missing'), dict(name='Bad', consumable_slug='healing-potion', quantity=-1),
                 dict(name='Mixed', consumable_slug='healing-potion', weapon_type_slug='sword', base_damage=1)]:
        with pytest.raises(ValueError):
            LootDrop.model_validate(data)


def test_migration_preserves_saved_loot_and_catalog_edits():
    from sqlalchemy import create_engine, MetaData, Table, Column, String, JSON, select
    from app.migrations.v011_consumables import upgrade
    from app.models import Consumable
    engine = create_engine('sqlite:///:memory:')
    metadata = MetaData()
    templates = Table('quest_templates', metadata, Column('slug', String, primary_key=True), Column('journey', JSON))
    runs = Table('quests', metadata, Column('id', String, primary_key=True), Column('rewards', JSON))
    rotations = Table('raid_rotations', metadata, Column('key', String, primary_key=True), Column('snapshot', JSON))
    metadata.create_all(engine)
    original = {'encounter_groups': {'loot_tiers': [{'name': 'Test', 'drop_chance_percent': 60,
                'drops': [{'name': 'Sword', 'weapon_type_slug': 'sword', 'base_damage': 10, 'weight': 1}]}]}}
    try:
        with engine.begin() as conn:
            conn.execute(templates.insert().values(slug='test', journey=original))
            conn.execute(runs.insert().values(id='saved', rewards=original))
            conn.execute(rotations.insert().values(key='daily', snapshot=original))
            upgrade(conn)
            conn.execute(Consumable.__table__.update().where(Consumable.slug == 'healing-potion').values(power=50))
            upgrade(conn)
            assert len(conn.scalar(select(templates.c.journey))['encounter_groups']['loot_tiers'][0]['drops']) == 4
            assert conn.scalar(select(runs.c.rewards)) == original
            assert conn.scalar(select(rotations.c.snapshot)) == original
            assert conn.scalar(select(Consumable.power).where(Consumable.slug == 'healing-potion')) == 50
    finally:
        engine.dispose()
