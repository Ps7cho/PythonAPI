import pytest

@pytest.fixture(autouse=True)
def successful_loot_for_settlement_tests(monkeypatch):
    from copy import deepcopy
    monkeypatch.setattr('app.group_journeys.roll_loot', lambda tier, rng: deepcopy(tier['drops'][0]))

from copy import deepcopy
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from app.database import SessionLocal
from app.models import Adventurer, Encounter, Enemy, QuestTemplate, RaidRotation, Weapon
from app.raids import rotation_info, build_raid_plan
from app.journeys import JourneyRules
from app.progression import award_experience
from test_encounter_groups import start, win, advance, clear_group, weapons


def saved_plan(encounter):
    with SessionLocal() as db:
        return deepcopy(db.get(Encounter, UUID(encounter['id'])).quest_run.quest.encounter_pool)


def test_rotation_boundaries_are_utc_daily_and_monday_weekly():
    def at(day, hour=0):
        return datetime(2026, 9, day, hour, tzinfo=timezone.utc)
    assert rotation_info('daily-raid', 'daily', at(12))['seed'] == rotation_info('daily-raid', 'daily', at(12, 23))['seed']
    assert rotation_info('daily-raid', 'daily', at(12))['seed'] != rotation_info('daily-raid', 'daily', at(13))['seed']
    weekly = rotation_info('weekly-raid', 'weekly', at(13, 23))
    assert weekly['period'] == '2026-09-07'
    assert weekly['resets_at'] == '2026-09-14T00:00:00+00:00'
    assert weekly['seed'] == rotation_info('weekly-raid', 'weekly', at(7))['seed']
    assert weekly['seed'] != rotation_info('weekly-raid', 'weekly', at(14))['seed']


def test_shared_raid_seed_freezes_enemies_and_has_exactly_three_bosses(client):
    ids, first = start(client, 'daily-raid')
    first_plan = saved_plan(first)
    preview = client.get('/api/quest-templates/daily-raid').json()['journey']['raid']['rotation']
    assert preview['encounter_count'] == len(first_plan)
    assert preview['seed'] == first['quest']['raid']['seed']
    assert 10 <= len(first_plan) <= 15
    assert len([e for e in first_plan if e['boss']]) == 3
    assert first_plan[-1]['boss']
    assert all(e['group_end'] == e['boss'] for e in first_plan)
    assert all(e['seeded_enemies'][0]['enemy_type'] == 'boss' and e['seeded_enemies'][0]['abilities'] for e in first_plan if e['boss'])
    with SessionLocal.begin() as db:
        original = {e.slug: deepcopy(e.stat_ranges) for e in db.scalars(select(Enemy))}
        for enemy in db.scalars(select(Enemy)):
            enemy.stat_ranges = {**enemy.stat_ranges, 'hp': {'min': 1000, 'max': 1000}}
    try:
        _, second = start(client, 'daily-raid')
        second_plan = saved_plan(second)
        assert first['quest']['raid'] == second['quest']['raid']
        assert first['id'] != second['id']
        assert first['enemies'][0]['id'] != second['enemies'][0]['id']
        assert first['enemies'][0]['max_hp'] == second['enemies'][0]['max_hp']
        assert [{k:v for k,v in e.items() if k != 'encounter_id'} for e in first_plan] == [{k:v for k,v in e.items() if k != 'encounter_id'} for e in second_plan]
    finally:
        with SessionLocal.begin() as db:
            for slug, stats in original.items():
                db.get(Enemy, slug).stat_ranges = stats
    with SessionLocal() as db:
        info = first['quest']['raid']
        assert db.get(RaidRotation, 'daily-raid:' + info['period']).seed == info['seed']


def test_weekly_seed_and_daily_snapshot_roll_over_independently(client):
    _, weekly = start(client, 'weekly-raid')
    assert 13 <= weekly['quest']['encounter_count'] <= 15
    assert sum(e['boss'] for e in saved_plan(weekly)) == 3
    with SessionLocal.begin() as db:
        settings = JourneyRules.model_validate(db.get(QuestTemplate, 'daily-raid').journey).model_dump()
        a, sa = build_raid_plan(db, 'daily-raid', settings, datetime(2026, 1, 1, tzinfo=timezone.utc))
        b, sb = build_raid_plan(db, 'daily-raid', settings, datetime(2026, 1, 2, tzinfo=timezone.utc))
        assert sa['raid_seed'] != sb['raid_seed']
        assert db.get(RaidRotation, 'daily-raid:2026-01-01')
        assert db.get(RaidRotation, 'daily-raid:2026-01-02')
        assert 10 <= len(a) <= 15 and 10 <= len(b) <= 15


def test_raid_is_finite_and_final_boss_claims_rewards_once(client):
    ids, e = start(client, 'daily-raid')
    total_encounters = e['quest']['encounter_count']
    for group in range(1, 4):
        e = clear_group(client, e)
        assert e['quest']['is_boss']
        if group < 3:
            assert e['quest']['can_continue']
            e = advance(client, e, {'choice': 'rest'})
    assert e['quest']['encounter_number'] == total_encounters
    assert e['quest']['status'] == 'victory'
    assert not e['quest']['can_continue']
    assert e['quest']['group']['loot_claimed']
    assert weapons(ids[0]) == 4
    hero = client.get('/api/adventurers/' + ids[0]).json()
    assert hero['gold'] == total_encounters * 30 + 150
    assert hero['experience'] == total_encounters * 60 + 300
    assert client.post('/api/encounters/' + e['id'] + '/continue', json={'choice':'push'}).status_code == 409
    assert client.post('/api/encounters/' + e['id'] + '/continue', json={'return_to_village':True}).status_code == 409
    assert weapons(ids[0]) == 4


def down_member(encounter, hero_id):
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(encounter['id']))
        saved.participants = [{**p, 'hp': 0} if p['id'] == hero_id else p for p in saved.participants]


def test_survivor_rescues_downed_ally_preserving_pre_run_wealth(client):
    ids, e = start(client, count=2)
    with SessionLocal.begin() as db:
        hero = db.get(Adventurer, UUID(ids[1]))
        award_experience(db, hero, 250)
        hero.gold = 77
    before = client.get('/api/adventurers/' + ids[1]).json()
    e = clear_group(client, e)
    assert e['quest']['run_gains'][ids[1]]['experience'] > 0
    e = advance(client, e, {'choice':'push'})
    down_member(e, ids[1])
    e = client.get('/api/encounters/' + e['id']).json()
    e = clear_group(client, e)
    downed = client.get('/api/adventurers/' + ids[1]).json()
    assert downed['is_alive'] and downed['health'] == 0
    assert client.post('/api/adventurers/' + ids[1] + '/rest').status_code == 409
    result = advance(client, e, {'return_to_village':True})
    restored = client.get('/api/adventurers/' + ids[1]).json()
    assert restored['is_alive'] and restored['health'] == 1
    for field in ('gold','experience','level','attributes','inventory'):
        assert restored[field] == before[field]
    assert restored['progression']['attribute_points'] == before['progression']['attribute_points']
    assert weapons(ids[1]) == 1
    assert any('rescued' in m for m in result['events'])
    assert client.post('/api/adventurers/' + ids[1] + '/rest').status_code == 200


def test_raid_death_stays_permanent_even_when_ally_returns(client):
    ids, e = start(client, 'daily-raid', count=2)
    down_member(e, ids[1])
    e = client.get('/api/encounters/' + e['id']).json()
    e = clear_group(client, e)
    dead = client.get('/api/adventurers/' + ids[1]).json()
    assert not dead['is_alive'] and dead['health'] == 0
    advance(client, e, {'return_to_village':True})
    dead = client.get('/api/adventurers/' + ids[1]).json()
    assert not dead['is_alive'] and dead['health'] == 0
    assert weapons(ids[1]) == 1
    assert client.post('/api/adventurers/' + ids[1] + '/rest').status_code == 409
    assert client.post('/api/encounters', json={'adventurer_ids':[ids[1]],'template_slug':'village-patrol'}).status_code == 409


def test_group_ranges_cover_initial_routes_and_extensions(client):
    from app.group_journeys import bounded_lengths
    for low, high, counts in [(2, 3, range(2, 21)), (3, 5, range(3, 21))]:
        for count in counts:
            lengths = bounded_lengths(count, low, high)
            assert sum(lengths) == count
            assert all(low <= n <= high for n in lengths)
    templates = client.get('/api/quest-templates').json()
    for template in templates:
        rules = template['journey']
        if rules.get('kind') not in ('quest', 'epic'):
            continue
        expected = (2, 3) if rules['kind'] == 'quest' else (3, 5)
        assert (rules['encounter_groups']['min_size'], rules['encounter_groups']['max_size']) == expected
        assert rules['death_policy'] == 'rescue_on_return'


def test_recovery_migration_keeps_running_adventures_unchanged():
    from sqlalchemy import create_engine, MetaData, Table, Column, String, JSON, inspect
    from app.migrations.v007_raids_recovery import upgrade
    engine = create_engine('sqlite:///:memory:')
    metadata = MetaData()
    templates = Table('quest_templates', metadata, Column('slug', String, primary_key=True), Column('journey', JSON))
    quests = Table('quests', metadata, Column('id', String, primary_key=True), Column('rewards', JSON))
    metadata.create_all(engine)
    old = {'kind':'epic', 'encounter_groups':{'size':2}}
    with engine.begin() as conn:
        conn.execute(templates.insert(), [{'slug':'epic','journey':old}, {'slug':'quest','journey':{**old,'kind':'quest'}}])
        conn.execute(quests.insert().values(id='running',rewards={'journey':old}))
        upgrade(conn); upgrade(conn)
        rows = dict(conn.execute(select(templates.c.slug,templates.c.journey)).all())
        assert rows['epic']['encounter_groups']['min_size'] == 3
        assert rows['epic']['encounter_groups']['max_size'] == 5
        assert rows['quest']['encounter_groups']['max_size'] == 3
        assert rows['epic']['death_policy'] == 'rescue_on_return'
        assert conn.scalar(select(quests.c.rewards)) == {'journey':old}
        assert inspect(conn).has_table('raid_rotations')
    engine.dispose()
