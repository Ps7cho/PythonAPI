from copy import deepcopy
from datetime import datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app import world_boss
from app.database import Base, SessionLocal
from app.models import (Adventurer, Encounter, EntityType, GameEvent, Gear, Inventory, LoginSession, Party, Quest, QuestRun, User,
                        WorldBossEntry, WorldBossEvent, WorldBossReward, WorldBossState)


@pytest.fixture
def finale(monkeypatch):
    now = datetime(2030, 1, 7, 3)
    monkeypatch.setattr(world_boss, 'now_utc', lambda: now)
    with SessionLocal.begin() as db:
        state = db.get(WorldBossState, 'alpha-wolf')
        saved = {c.name: deepcopy(getattr(state, c.name)) for c in state.__table__.columns}
        state.phase = 'alpha'
        state.enabled = True
        state.delete_adventurers_on_failure = True
        state.next_start_at = now
        state.max_health = 100_000
    world_boss.tick(now)
    yield now
    with SessionLocal.begin() as db:
        db.query(WorldBossReward).delete()
        db.query(WorldBossEntry).delete()
        db.query(WorldBossEvent).delete()
        state = db.get(WorldBossState, 'alpha-wolf')
        for key, value in saved.items():
            setattr(state, key, value)


def join(client):
    hero_response = client.post('/api/adventurers', json={'name': 'Wolf challenger'})
    assert hero_response.status_code == 200, hero_response.text
    hero = hero_response.json()
    response = client.post('/api/encounters', json={'adventurer_ids': [hero['id']], 'template_slug': 'alpha-wolf-finale'})
    assert response.status_code == 201, response.text
    return hero, response.json()


def test_ten_rats_share_one_iron_encounter(client):
    hero = client.post('/api/adventurers', json={'name': 'Ten rat probe'}).json()
    response = client.post('/api/encounters', json={'adventurer_ids': [hero['id']], 'template_slug': 'iron-rat-swarm'})
    assert response.status_code == 201, response.text
    data = response.json()
    assert len(data['enemies']) == 10
    assert len({e['id'] for e in data['enemies']}) == 10
    assert all(e['enemy_slug'] == 'rat' for e in data['enemies'])
    assert data['quest']['journey']['required_rank'] == 'iron'


def test_party_fights_are_separate_but_health_is_shared(client, finale):
    first_hero, first = join(client)
    second_hero, second = join(client)
    assert first['enemies'][0]['id'] != second['enemies'][0]['id']
    assert first['world_boss']['id'] == second['world_boss']['id']
    response = client.post('/api/encounters/'+first['id']+'/actions', json={
        'actor_id': first_hero['id'], 'expected_turn': 1, 'action': 'attack'})
    assert response.status_code == 200, response.text
    damaged = response.json()
    assert damaged['world_boss']['health'] < first['world_boss']['health']
    other = client.get('/api/encounters/'+second['id']).json()
    assert other['enemies'][0]['hp'] == damaged['world_boss']['health']
    assert other['participants'][0]['hp'] == second['participants'][0]['hp']
    assert other['turn'] == 1
    assert damaged['participants'][0]['hp'] < first['participants'][0]['hp']
    # A stale action cannot damage the shared pool twice.
    repeated = client.post('/api/encounters/'+first['id']+'/actions', json={
        'actor_id': first_hero['id'], 'expected_turn': 1, 'action': 'attack'})
    assert repeated.status_code == 409
    assert client.get('/api/encounters/'+second['id']).json()['enemies'][0]['hp'] == other['enemies'][0]['hp']


def test_other_party_receives_shared_health_push_without_local_damage(client, finale, monkeypatch):
    import threading
    from app import live
    lock = threading.Lock()
    original_snapshot = live.authorized_snapshot
    original_post = client.post
    def serialized_snapshot(*args):
        with lock:
            return original_snapshot(*args)
    def serialized_post(*args, **kwargs):
        with lock:
            return original_post(*args, **kwargs)
    monkeypatch.setattr(live, 'authorized_snapshot', serialized_snapshot)
    monkeypatch.setattr(client, 'post', serialized_post)
    hero, first = join(client)
    other, second = join(client)
    token = client.cookies.get('game_session')
    with client.websocket_connect('/api/encounters/'+second['id']+'/live') as stream:
        stream.send_json({'token': token})
        before = stream.receive_json()['data']
        result = client.post('/api/encounters/'+first['id']+'/actions', json={
            'actor_id':hero['id'], 'expected_turn':1, 'action':'attack'})
        assert result.status_code == 200, result.text
        pushed = stream.receive_json()['data']
        assert pushed['world_boss']['health'] == result.json()['world_boss']['health']
        assert pushed['revision'] != before['revision']
        assert pushed['turn'] == 1
        assert pushed['participants'][0]['id'] == other['id']
        assert pushed['participants'][0]['hp'] == before['participants'][0]['hp']


def test_defeat_unlocks_beta_and_grants_one_bound_epic_set(client, finale):
    hero, battle = join(client)
    with SessionLocal.begin() as db:
        world_boss.latest(db).health = 1
    result = client.post('/api/encounters/'+battle['id']+'/actions', json={
        'actor_id': hero['id'], 'expected_turn': 1, 'action': 'attack'})
    assert result.status_code == 200, result.text
    assert result.json()['world_boss']['status'] == 'defeated'
    status = client.get('/api/world-boss').json()
    assert status['phase'] == 'beta'
    assert len(status['rewards']) == 1
    reward = {'adventurer_id': hero['id'], 'event_id': status['rewards'][0]}
    claim = client.post('/api/world-boss/rewards/claim', json=reward)
    assert claim.status_code == 200, claim.text
    assert len(claim.json()['gear_ids']) == 9
    assert client.post('/api/world-boss/rewards/claim', json=reward).status_code == 409
    items = client.get('/api/adventurers/'+hero['id']).json()['inventory']
    rewarded = [g for g in items if g.get('definition_slug', '').startswith('alpha-wolf-')]
    assert len(rewarded) == 9
    assert all(g['rarity'] == 'epic' and g['account_bound'] for g in rewarded)
    assert {g['slot'] for g in rewarded} == {'Head','Shoulders','Chest','Bracers','Hands','Belt','Legs','Feet','Cape'}
    auction = client.post('/api/auction-house', json={
        'adventurer_id': hero['id'], 'gear_id': rewarded[0]['id'], 'mode': 'fixed', 'price': 1})
    assert auction.status_code == 409
    assert 'Account-bound' in auction.json()['detail']
    world_boss.tick(finale + timedelta(days=7))
    assert client.get('/api/adventurers/'+hero['id']).status_code == 200
    assert client.get('/api/world-boss').json()['phase'] == 'beta'


def test_event_requires_active_window(client, finale, monkeypatch):
    monkeypatch.setattr(world_boss, 'now_utc', lambda: finale + timedelta(minutes=10))
    hero = client.post('/api/adventurers', json={'name': 'Late wolf challenger'}).json()
    response = client.post('/api/encounters', json={'adventurer_ids': [hero['id']], 'template_slug': 'alpha-wolf-finale'})
    assert response.status_code == 409


def test_action_crossing_deadline_rolls_back_damage(client, finale, monkeypatch):
    hero, battle = join(client)
    times = iter([finale + timedelta(seconds=599), finale + timedelta(seconds=600)])
    monkeypatch.setattr(world_boss, 'now_utc', lambda: next(times))
    response = client.post('/api/encounters/'+battle['id']+'/actions', json={
        'actor_id':hero['id'], 'expected_turn':1, 'action':'attack'})
    assert response.status_code == 409
    with SessionLocal() as db:
        assert world_boss.latest(db).health == battle['world_boss']['health']
        assert db.get(Encounter,UUID(battle['id'])).turn == 1


@pytest.mark.parametrize('armed', [True, False])
def test_failure_deletion_requires_armed_switch_and_runs_once(monkeypatch, armed):
    # The destructive lifecycle runs only in a separate disposable database.
    engine = create_engine('sqlite:///:memory:')
    @event.listens_for(engine, 'connect')
    def enable_fk(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(world_boss, 'SessionLocal', sessions)
    now = datetime(2030, 1, 7, 3, 10)
    with sessions.begin() as db:
        account = User(username='surviving_account', email='alpha@example.test')
        db.add(account)
        db.flush()
        hero = Adventurer(name='Wiped adventurer', owner=account.id, level=10, experience=1000)
        db.add(hero)
        db.flush()
        db.add(Inventory(adventurer_id=hero.id, items=['Old alpha gear']))
        second_account = User(username='idle_account', email='idle@example.test')
        db.add(second_account)
        db.flush()
        db.add(Adventurer(name='Not in the boss fight', owner=second_account.id, level=5))
        db.add(LoginSession(token_hash='a'*64, user_id=account.id, expires_at=now+timedelta(days=1)))
        party = Party(name='Historical party', leader=hero.id)
        quest = Quest(location='Mosswood', rewards={})
        db.add_all([party, quest])
        db.flush()
        run = QuestRun(party_id=party.id, quest_id=quest.id)
        db.add(run)
        db.flush()
        db.add(Encounter(quest_run_id=run.id, state='player_turn', turn=1, participants=[], enemies=[]))
        db.add(GameEvent(quest_run_id=run.id, event_type='combat_action', payload={}))
        db.add(EntityType(slug='preserved-catalog', status_resistances={}))
        db.add(WorldBossState(slug='alpha-wolf', phase='alpha', enabled=True, max_health=1000,
            delete_adventurers_on_failure=armed, duration_seconds=600, next_start_at=now+timedelta(days=7), settings={'timezone':'America/Denver','weekday':6,'hour':20}))
        db.flush()
        db.add(WorldBossEvent(boss_slug='alpha-wolf', period='test-wipe', status='active',
            health=900, max_health=1000, starts_at=now-timedelta(minutes=10), ends_at=now))
    world_boss.tick(now)
    world_boss.tick(now + timedelta(seconds=1))
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Adventurer)) == (0 if armed else 2)
        assert db.scalar(select(func.count()).select_from(Inventory)) == (0 if armed else 1)
        assert db.scalar(select(func.count()).select_from(User)) == 2
        assert db.scalar(select(func.count()).select_from(LoginSession)) == 1
        for model in (Party, Quest, QuestRun, Encounter):
            assert db.scalar(select(func.count()).select_from(model)) == (0 if armed else 1)
        assert db.get(EntityType, 'preserved-catalog') is not None
        event_row = world_boss.latest(db)
        assert event_row.status == 'failed' and event_row.wiped_adventurers == (2 if armed else 0)
        assert db.scalar(select(func.count()).select_from(GameEvent).where(GameEvent.event_type == 'world_boss_failed')) == 1
    engine.dispose()


def test_schedule_accounts_for_daylight_saving_time():
    settings = {'timezone':'America/Denver','weekday':6,'hour':20}
    assert world_boss.next_start(datetime(2030, 1, 1), settings) == datetime(2030, 1, 7, 3)
    assert world_boss.next_start(datetime(2030, 7, 1), settings) == datetime(2030, 7, 1, 2)


def test_nightly_schedule_accounts_for_dst_and_rolls_to_next_evening():
    settings = {'timezone':'America/Denver','cadence':'daily','hour':20}
    assert world_boss.next_start(datetime(2030, 1, 1), settings) == datetime(2030, 1, 1, 3)
    assert world_boss.next_start(datetime(2030, 1, 1, 3), settings) == datetime(2030, 1, 2, 3)
    assert world_boss.next_start(datetime(2030, 7, 1), settings) == datetime(2030, 7, 1, 2)
    # Spring forward and fall back preserve 8 PM local time.
    assert world_boss.next_start(datetime(2030, 3, 10, 3), settings) == datetime(2030, 3, 11, 2)
    assert world_boss.next_start(datetime(2030, 11, 3, 2), settings) == datetime(2030, 11, 4, 3)


def test_lifecycle_worker_waits_until_the_returned_deadline(monkeypatch):
    from threading import Event

    now = datetime(2030, 1, 1, 12)
    waits = []
    stop = Event()

    class WakeOnce:
        def clear(self):
            pass

        def wait(self, seconds):
            waits.append(seconds)
            stop.set()

    monkeypatch.setattr(world_boss, 'now_utc', lambda: now)
    monkeypatch.setattr(world_boss, 'tick', lambda: now + timedelta(hours=4))

    world_boss.lifecycle_worker(stop, WakeOnce())

    assert waits == [14400]


def test_lifecycle_worker_waits_without_polling_when_disabled(monkeypatch):
    from threading import Event

    waits = []
    stop = Event()

    class WakeOnce:
        def clear(self):
            pass

        def wait(self, seconds):
            waits.append(seconds)
            stop.set()

    monkeypatch.setattr(world_boss, 'tick', lambda: None)

    world_boss.lifecycle_worker(stop, WakeOnce())

    assert waits == [None]


def test_lifecycle_worker_retries_quickly_only_after_failure(monkeypatch):
    from threading import Event

    waits = []
    stop = Event()

    class WakeOnce:
        def clear(self):
            pass

        def wait(self, seconds):
            waits.append(seconds)
            stop.set()

    def fail():
        raise RuntimeError('database unavailable')

    monkeypatch.setattr(world_boss, 'tick', fail)

    world_boss.lifecycle_worker(stop, WakeOnce(), retry_seconds=7)

    assert waits == [7]


def test_switch_is_developer_only_strict_and_persisted(client, finale):
    from app.models import User
    payload = {'delete_adventurers_on_failure':False}
    assert client.patch('/api/world-boss', json=payload).status_code == 403
    assert client.get('/api/world-boss').json()['can_manage'] is False
    with SessionLocal.begin() as db:
        user_id = UUID(client.get('/api/auth/me').json()['id'])
        db.get(User, user_id).account_type = 'developer'
    assert client.patch('/api/world-boss', json={'delete_adventurers_on_failure':'false'}).status_code == 422
    assert client.patch('/api/world-boss', json=payload).status_code == 200
    assert client.get('/api/world-boss').json()['delete_adventurers_on_failure'] is False
    assert client.get('/api/world-boss').json()['can_manage'] is True
    assert client.patch('/api/world-boss', json={'delete_adventurers_on_failure':True}).status_code == 200
    with SessionLocal() as db:
        assert db.get(WorldBossState, 'alpha-wolf').delete_adventurers_on_failure is True


def test_testing_timeout_retains_heroes_ends_runs_and_reopens_next_night(client, finale, monkeypatch):
    hero, battle = join(client)
    with SessionLocal.begin() as db:
        state = db.get(WorldBossState, 'alpha-wolf')
        state.delete_adventurers_on_failure = False
        state.settings = {**state.settings, 'cadence':'daily'}
        state.next_start_at = finale + timedelta(days=1)
    world_boss.tick(finale + timedelta(minutes=10))
    with SessionLocal() as db:
        assert db.get(Adventurer, UUID(hero['id'])) is not None
        assert db.get(Encounter, UUID(battle['id'])).quest_run.status == 'defeat'
        assert world_boss.latest(db).wiped_adventurers == 0
    tomorrow = finale + timedelta(days=1)
    monkeypatch.setattr(world_boss, 'now_utc', lambda: tomorrow)
    world_boss.tick(tomorrow)
    response = client.post('/api/encounters', json={'adventurer_ids':[hero['id']], 'template_slug':'alpha-wolf-finale'})
    assert response.status_code == 201, response.text
    assert response.json()['world_boss']['id'] != battle['world_boss']['id']


def test_testing_victory_retains_alpha_and_next_nightly_event(client, finale):
    hero, battle = join(client)
    with SessionLocal.begin() as db:
        state = db.get(WorldBossState, 'alpha-wolf')
        state.delete_adventurers_on_failure = False
        state.settings = {**state.settings, 'cadence':'daily'}
        state.next_start_at = finale + timedelta(days=1)
        world_boss.latest(db).health = 1
    response = client.post('/api/encounters/'+battle['id']+'/actions', json={
        'actor_id':hero['id'], 'expected_turn':1, 'action':'attack'})
    assert response.status_code == 200, response.text
    assert client.get('/api/world-boss').json()['phase'] == 'alpha'
    world_boss.tick(finale + timedelta(days=1))
    with SessionLocal() as db:
        assert world_boss.latest(db).status == 'active'
        assert db.scalar(select(func.count()).select_from(WorldBossEvent)) == 2


def test_migration_upgrades_existing_weekly_state_with_deletion_off():
    from app.migrations.v042_world_boss_testing import upgrade
    from sqlalchemy import inspect, text
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text('ALTER TABLE world_boss_state DROP COLUMN delete_adventurers_on_failure'))
        conn.execute(text("INSERT INTO world_boss_state (slug,phase,enabled,max_health,duration_seconds,next_start_at,settings) VALUES ('alpha-wolf','alpha',TRUE,10000000,600,'2030-01-07 03:00:00',:settings)"),
                     {'settings':'{"timezone":"America/Denver","weekday":6,"hour":20}'})
        upgrade(conn)
        assert 'delete_adventurers_on_failure' in {c['name'] for c in inspect(conn).get_columns('world_boss_state')}
        row = conn.execute(select(WorldBossState.__table__)).mappings().one()
        assert row['delete_adventurers_on_failure'] is False
        assert row['settings']['cadence'] == 'daily'
        assert row['max_health'] == 10_000_000
        assert row['duration_seconds'] == 600
        conn.execute(WorldBossState.__table__.update().values(delete_adventurers_on_failure=True))
        upgrade(conn)
        assert conn.scalar(select(WorldBossState.delete_adventurers_on_failure)) is True
    engine.dispose()


def test_turning_off_after_beta_resumes_testing(client, finale):
    user_id = UUID(client.get('/api/auth/me').json()['id'])
    with SessionLocal.begin() as db:
        db.get(User, user_id).account_type = 'developer'
        state = db.get(WorldBossState, 'alpha-wolf')
        state.phase = 'beta'
        state.settings = {**state.settings, 'cadence':'daily'}
    result = client.patch('/api/world-boss', json={'delete_adventurers_on_failure':False})
    assert result.status_code == 200, result.text
    data = client.get('/api/world-boss').json()
    assert data['phase'] == 'alpha'
    assert data['next_start_at'] == (finale + timedelta(days=1)).isoformat()+'Z'
