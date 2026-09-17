from copy import deepcopy
from uuid import UUID
import pytest
from sqlalchemy import select
from app.group_journeys import roll_loot, LootTier, earn_group_loot
from app.database import SessionLocal
from app.models import Encounter, Weapon
from test_encounter_groups import start, clear_group, advance, weapons


class ControlledRoll:
    def __init__(self, chance_roll, selected=0):
        self.chance_roll, self.selected, self.weight_calls = chance_roll, selected, 0
    def randrange(self, stop):
        assert stop == 100
        return self.chance_roll
    def choices(self, entries, weights, k):
        self.weight_calls += 1
        assert weights == [e['weight'] for e in entries] and k == 1
        return [entries[self.selected]]


def table(chance=60):
    return {'name':'Test', 'drop_chance_percent':chance, 'drops':[
        {'name':'Common','weapon_type_slug':'mace','base_damage':11,'weight':9},
        {'name':'Rare','weapon_type_slug':'bow','base_damage':15,'weight':1}]}


def test_chance_boundaries_weighted_selection_and_validation():
    missed = ControlledRoll(60)
    assert roll_loot(table(), missed) is None and missed.weight_calls == 0
    assert roll_loot(table(), ControlledRoll(59, 1))['name'] == 'Rare'
    assert roll_loot(table(0), ControlledRoll(0)) is None
    assert roll_loot(table(100), ControlledRoll(99))['name'] == 'Common'
    original = table()
    won = roll_loot(original, ControlledRoll(0)); won['name'] = 'Changed'
    assert original['drops'][0]['name'] == 'Common'
    for chance in (-1, 101, 1.5, True):
        with pytest.raises(ValueError): LootTier.model_validate(table(chance))


def test_missed_drop_persists_and_cashout_does_not_roll_again(client, monkeypatch):
    calls = []
    def miss(tier, rng):
        calls.append(tier['name']); return None
    monkeypatch.setattr('app.group_journeys.roll_loot', miss)
    ids, e = start(client)
    e = clear_group(client, e)
    assert len(calls) == 1 and e['quest']['group']['cleared'] == 1
    assert e['quest']['group']['stash'] == []
    assert e['quest']['group']['next_loot_chance_percent'] == 35
    with SessionLocal() as db:
        progress = db.get(Encounter, UUID(e['id'])).quest_run.quest.rewards['journey_progress']
        assert progress['loot_rolls'][0]['item'] is None
        assert progress['loot_rolls'][0]['drop_chance_percent'] == 35
    assert any('No item dropped' in event for event in e['events'])
    client.get('/api/encounters/' + e['id'])
    advance(client, e, {'return_to_village':True})
    assert len(calls) == 1 and weapons(ids[0]) == 1
    hero = client.get('/api/adventurers/' + ids[0]).json()
    assert hero['gold'] == 25 and hero['experience'] == 21
    assert client.post('/api/encounters/' + e['id'] + '/continue', json={'return_to_village':True}).status_code == 409
    assert len(calls) == 1


def test_preview_odds_types_tags_and_route_variation(client):
    catalog = client.get('/api/quest-templates').json()
    routes = [t for t in catalog if t['journey'].get('encounter_groups')]
    for route in routes:
        for index, tier in enumerate(route['journey']['loot_tables']):
            assert len(tier['items']) == 50
            categories = tier['category_chances_percent']
            assert categories['weapon'] == 5 + index and categories['consumable'] == 20
            assert categories['orb'] > 0 and categories['essence'] > 0
            assert categories['orb'] + categories['essence'] == pytest.approx(10 + index)
            assert tier['orb_essence_chance_percent'] == 10 + index
            assert {d['consumable_slug'] for d in tier['items'] if d['consumable_slug'] and not d['consumable_slug'].startswith('essence-')} == {'healing-potion', 'cleansing-draught', 'might-tonic', 'evasion-orb', 'strike-orb'}
            assert {d['weapon_type'] for d in tier['items'] if d['weapon_type']} == {'sword','axe','dagger','mace','bow','staff'}
            assert abs(sum(d['chance_percent'] for d in tier['items']) - tier['drop_chance_percent']) < .001
            assert tier['no_drop_chance_percent'] + tier['drop_chance_percent'] == 100
            assert all('weapon' in d['tags'] and d['weapon_type'] in d['tags'] for d in tier['items'] if d['weapon_type'])
    a = next(t for t in routes if t['slug']=='wolf-tracks')['journey']['loot_tables'][0]
    b = next(t for t in routes if t['slug']=='village-patrol')['journey']['loot_tables'][0]
    assert {d['name'] for d in a['items']} != {d['name'] for d in b['items']}


def test_raids_use_independent_loot_randomness_not_world_seed(client, monkeypatch):
    import app.group_journeys as groups
    # A deterministic fake entropy source proves that the run RNG is selected.
    rolls = iter([ControlledRoll(99), ControlledRoll(0, 1)])
    monkeypatch.setattr(groups, 'SystemRandom', lambda: next(rolls))
    _, first = start(client, 'daily-raid')
    _, second = start(client, 'daily-raid')
    assert first['quest']['raid']['seed'] == second['quest']['raid']['seed']
    with SessionLocal.begin() as db:
        a = db.get(Encounter, UUID(first['id'])).quest_run.quest
        b = db.get(Encounter, UUID(second['id'])).quest_run.quest
        earn_group_loot(a); earn_group_loot(b)
        assert a.rewards['journey_progress']['loot_rolls'][0]['item'] is None
        assert b.rewards['journey_progress']['loot_rolls'][0]['item'] is not None


def test_looted_bow_uses_tags_for_basic_attack_and_melee_restriction(client, monkeypatch):
    def bow(tier, rng):
        return deepcopy(next(d for d in tier['drops'] if d['weapon_type_slug']=='bow'))
    monkeypatch.setattr('app.group_journeys.roll_loot', bow)
    ids, e = start(client); e = clear_group(client, e); advance(client,e,{'return_to_village':True})
    gear = client.get('/api/adventurers/' + ids[0] + '/weapons').json()['weapons']
    weapon = next(w for w in gear if w['weapon_type']=='bow')
    assert 'ranged' in weapon['tags'] and 'melee' not in weapon['tags']
    assert client.post('/api/adventurers/' + ids[0] + '/weapon',json={'weapon_id':weapon['id']}).status_code == 200
    e = client.post('/api/encounters',json={'adventurer_ids':ids,'enemy_slug':'roadside-bandit'}).json()
    url = '/api/encounters/' + e['id'] + '/actions'
    assert client.post(url,json={'actor_id':ids[0],'expected_turn':1,'action':'power_strike'}).status_code == 409
    before = e['enemies'][0]['hp']
    hit = client.post(url,json={'actor_id':ids[0],'expected_turn':1,'action':'attack'})
    assert hit.status_code == 200, hit.text
    assert hit.json()['enemies'][0]['hp'] == before - weapon['base_damage']


def test_loot_migration_preserves_snapshots_custom_chances_and_is_idempotent():
    from sqlalchemy import create_engine, MetaData, Table, Column, String, JSON
    from app.migrations.v008_loot_chances import upgrade
    engine = create_engine('sqlite:///:memory:')
    metadata = MetaData()
    templates = Table('quest_templates', metadata, Column('slug',String,primary_key=True),Column('journey',JSON))
    quests = Table('quests',metadata,Column('id',String,primary_key=True),Column('rewards',JSON))
    abilities = Table('abilities',metadata,Column('id',String,primary_key=True),Column('slug',String),Column('allowed_weapon_tags',JSON))
    metadata.create_all(engine)
    tier = table();tier.pop('drop_chance_percent')
    old = {'kind':'quest','encounter_groups':{'loot_tiers':[tier]}}
    custom = {'kind':'quest','encounter_groups':{'loot_tiers':[table(25)]}}
    with engine.begin() as conn:
        conn.execute(templates.insert(),[{'slug':'old','journey':old},{'slug':'custom','journey':custom}])
        conn.execute(quests.insert().values(id='running',rewards={'journey':old}))
        conn.execute(abilities.insert(),[{'id':'a','slug':'attack','allowed_weapon_tags':['melee']},{'id':'b','slug':'power_strike','allowed_weapon_tags':['melee']}])
        upgrade(conn);upgrade(conn)
        rows=dict(conn.execute(select(templates.c.slug,templates.c.journey)).all())
        assert rows['old']['encounter_groups']['loot_tiers'][0]['drop_chance_percent']==60
        assert len(rows['old']['encounter_groups']['loot_tiers'][0]['drops'])==11
        assert rows['custom']['encounter_groups']['loot_tiers']==custom['encounter_groups']['loot_tiers']
        assert conn.scalar(select(quests.c.rewards))=={'journey':old}
        tags=dict(conn.execute(select(abilities.c.slug,abilities.c.allowed_weapon_tags)).all())
        assert tags=={'attack':['melee','ranged'],'power_strike':['melee']}
    engine.dispose()
