import pytest

@pytest.fixture(autouse=True)
def successful_loot_for_settlement_tests(monkeypatch):
    from copy import deepcopy
    monkeypatch.setattr('app.group_journeys.roll_loot', lambda tier, rng: deepcopy(tier['drops'][0]))

from uuid import UUID

from sqlalchemy import select
from app.database import SessionLocal
from app.models import Adventurer, Encounter, Weapon, QuestTemplate


def start(client, slug='village-patrol', count=1):
    ids = [client.post('/api/adventurers', json={'name': 'Risk taker'}).json()['id'] for _ in range(count)]
    response = client.post('/api/encounters', json={'adventurer_ids': ids, 'template_slug': slug})
    assert response.status_code == 201, response.text
    return ids, response.json()


def win(client, encounter):
    # Keep battles deterministic while exercising the actual action/reward transaction.
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(encounter['id']))
        for enemy in saved.enemy_instances:
            enemy.state = {**enemy.state, 'hp': 1}
    for _ in range(10):
        if encounter['state'] == 'victory':
            return encounter
        target = next(e for e in encounter['enemies'] if e['hp'] > 0)
        response = client.post('/api/encounters/' + encounter['id'] + '/actions', json={
            'actor_id': encounter['pending_actor_ids'][0], 'expected_turn': encounter['turn'],
            'action': 'attack', 'target_id': target['id']})
        assert response.status_code == 200, response.text
        encounter = response.json()
    raise AssertionError('Battle did not end')


def advance(client, encounter, payload=None):
    response = client.post('/api/encounters/' + encounter['id'] + '/continue', json=payload or {})
    assert response.status_code == 200, response.text
    return response.json()


def weapons(hero_id):
    with SessionLocal() as db:
        return len(db.scalars(select(Weapon).where(Weapon.adventurer_id == UUID(hero_id))).all())


def test_group_commitment_cashout_and_replay(client):
    ids, e = start(client)
    e = win(client, e)
    assert e['quest']['can_return'] is False
    assert e['quest']['requires_choice'] is False
    path = '/api/encounters/' + e['id'] + '/continue'
    before = client.get('/api/encounters/' + e['id']).json()
    for command in ({'return_to_village': True}, {'choice': 'rest'}, {'choice': 'push'}):
        assert client.post(path, json=command).status_code == 409
    assert client.get('/api/encounters/' + e['id']).json() == before
    assert client.post('/api/adventurers/' + ids[0] + '/rest').status_code == 409
    hp = e['participants'][0]['hp']
    e = advance(client, e)
    assert e['participants'][0]['hp'] == hp
    e = win(client, e)
    assert e['quest']['can_return'] and e['quest']['can_continue']
    assert e['quest']['group']['stash'][0]['tier'] == 'Initial loot'
    assert weapons(ids[0]) == 1
    saved_stash = e['quest']['group']['stash']
    assert client.get('/api/encounters/' + e['id']).json()['quest']['group']['stash'] == saved_stash
    path = '/api/encounters/' + e['id'] + '/continue'
    assert client.post(path, json={'choice': 'rest'}).status_code == 409
    returned = advance(client, e, {'return_to_village': True})
    assert returned['quest']['status'] == 'returned'
    assert returned['quest']['group']['loot_claimed']
    assert weapons(ids[0]) == 2
    assert client.post(path, json={'return_to_village': True}).status_code == 409
    assert weapons(ids[0]) == 2
    assert client.post('/api/adventurers/' + ids[0] + '/rest').status_code == 200
    new = client.post('/api/encounters', json={'adventurer_ids': ids, 'template_slug': 'village-patrol'}).json()
    assert new['quest']['group']['cleared'] == 0 and new['quest']['group']['stash'] == []


def clear_group(client, e):
    while True:
        e = win(client, e)
        if e['quest']['group']['boundary']:
            return e
        assert not e['quest']['can_return']
        e = advance(client, e)


def test_deeper_groups_scale_tiers_and_do_not_repeat_completion_bounty(client):
    ids, e = start(client)
    battle_count = 0
    expected_xp = 5  # Initial route bounty, paid once.
    for group_number in range(1, 5):
        size = e['quest']['group']['size']
        assert 2 <= size <= 3
        battle_count += size
        expected_xp += size * ((8 * (100 + min(15, (group_number-1)*5)) + 99) // 100)
        e = clear_group(client, e)
        group = e['quest']['group']
        assert group['number'] == group_number and group['cleared'] == group_number
        assert group['stash'][-1]['tier'] == ('Initial loot' if group_number == 1 else f'Push {group_number - 1}')
        assert group['next_loot_chance_percent'] == 35 + 2 * (group_number - 1)
        assert group['after_push_loot_chance_percent'] == 35 + 2 * group_number
        assert group['threat']['health'] == 100 + 12 * (group_number - 1)
        assert group['threat']['power'] == 100 + 8 * (group_number - 1)
        if group_number < 4:
            path = '/api/encounters/' + e['id'] + '/continue'
            e = advance(client, e, {'choice': 'push', 'push_streak': 999, 'loot_stash': [{'base_damage': 999}]})
            assert e['quest']['push_streak'] == group_number
            assert client.post(path, json={'choice': 'push'}).status_code == 409
    bank = e['quest']['run_gains'][ids[0]]
    assert bank == {'gold': battle_count * 10 + 5, 'experience': expected_xp}
    assert client.get('/api/adventurers/' + ids[0]).json()['experience'] == 0
    advance(client, e, {'return_to_village': True})
    assert client.get('/api/adventurers/' + ids[0]).json()['experience'] == expected_xp
    assert weapons(ids[0]) == 5  # A successful weapon roll at each of four clears.


def test_epic_camp_only_at_boundary_budget_spans_extension(client):
    ids, e = start(client, 'mosswood-epic')
    assert 3 <= e['quest']['group']['size'] <= 5
    e = win(client, e)
    hp = e['participants'][0]['hp']
    assert client.post('/api/encounters/' + e['id'] + '/continue', json={'choice': 'rest'}).status_code == 409
    e = advance(client, e)
    assert e['participants'][0]['hp'] == hp
    e = clear_group(client, e)
    e = advance(client, e, {'choice': 'rest'})
    assert e['quest']['rests_remaining'] == 0
    assert e['quest']['group']['next_loot_chance_percent'] == 35
    assert 3 <= e['quest']['group']['size'] <= 5
    e = clear_group(client, e)
    path = '/api/encounters/' + e['id'] + '/continue'
    assert client.post(path, json={'choice': 'rest'}).status_code == 409
    e = advance(client, e, {'choice': 'push'})
    assert e['quest']['group']['number'] == 3
    assert e['quest']['xp_bonus_percent'] == 5
    assert e['quest']['group']['next_loot_chance_percent'] == 37
    e = clear_group(client, e)
    bank = e['quest']['run_gains'][ids[0]]
    result = advance(client, e, {'return_to_village': True})
    after = client.get('/api/adventurers/' + ids[0]).json()
    assert after['gold'] == bank['gold'] + 10
    assert after['experience'] == bank['experience'] + 8
    assert weapons(ids[0]) == 4
    assert any('Safe return' in event for event in result['events'])


def test_wipe_loses_stash_but_keeps_earned_xp(client):
    ids, e = start(client)
    e = win(client, e); e = advance(client, e); e = win(client, e)
    earned = client.get('/api/adventurers/' + ids[0]).json()['experience']
    e = advance(client, e, {'choice': 'push'})
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(e['id']))
        actor = {**saved.participants[0], 'hp': 1}
        saved.participants = [actor]
        enemy = saved.enemy_instances[0]
        from dataclasses import asdict
        from app.combat import CombatAbility
        enemy.state = {**enemy.state, 'abilities': [{'weight': 1, 'priority': 1, 'ability': asdict(CombatAbility('test_strike', 'Test strike', damage=1000))}]}
    response = client.post('/api/encounters/' + e['id'] + '/actions', json={'actor_id': ids[0], 'expected_turn': e['turn'], 'action': 'wait'})
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'defeat'
    assert response.json()['quest']['group']['stash'] == []
    assert weapons(ids[0]) == 1
    assert client.get('/api/adventurers/' + ids[0]).json()['experience'] == earned
    assert client.post('/api/encounters/' + e['id'] + '/continue', json={'return_to_village': True}).status_code == 409


def test_party_loot_awarded_only_to_survivors(client):
    ids, e = start(client, count=2)
    e = win(client, e); e = advance(client, e)
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(e['id']))
        saved.participants = [{**p, 'hp': 0} if p['id'] == ids[1] else p for p in saved.participants]
    e = client.get('/api/encounters/' + e['id']).json()
    e = win(client, e)
    advance(client, e, {'return_to_village': True})
    assert weapons(ids[0]) == 2 and weapons(ids[1]) == 1


def test_group_migration_is_idempotent_preserves_snapshots_and_custom_rules():
    from sqlalchemy import create_engine, MetaData, Table, Column, String, JSON
    from app.migrations.v006_encounter_groups import upgrade
    engine = create_engine('sqlite:///:memory:')
    metadata = MetaData()
    templates = Table('quest_templates', metadata, Column('slug', String, primary_key=True), Column('journey', JSON))
    quests = Table('quests', metadata, Column('id', String, primary_key=True), Column('rewards', JSON))
    metadata.create_all(engine)
    rules = {'kind': 'quest', 'stages': [{'groups': [['wolf']], 'description': 'Hunt'}]}
    custom = {**rules, 'encounter_groups': {'size': 4}}
    with engine.begin() as conn:
        conn.execute(templates.insert(), [{'slug': 'new', 'journey': rules}, {'slug': 'custom', 'journey': custom}])
        conn.execute(quests.insert().values(id='running', rewards={'journey': rules}))
        upgrade(conn); upgrade(conn)
        results = dict(conn.execute(select(templates.c.slug, templates.c.journey)).all())
        assert results['new']['encounter_groups']['size'] == 2
        assert results['new']['max_rests'] == 0
        assert results['custom'] == custom
        assert conn.scalar(select(quests.c.rewards)) == {'journey': rules}
    engine.dispose()


def test_cashout_failure_rolls_back_loot_and_run_then_retry_claims_once(client, monkeypatch):
    import app.group_journeys as groups
    ids, e = start(client)
    e = win(client, e); e = advance(client, e); e = win(client, e)
    def fail_grant(**kwargs):
        raise RuntimeError('Simulated grant failure')
    with monkeypatch.context() as patch:
        patch.setattr(groups, 'Weapon', fail_grant)
        import pytest
        with pytest.raises(RuntimeError, match='Simulated grant failure'):
            client.post('/api/encounters/' + e['id'] + '/continue', json={'return_to_village': True})
    saved = client.get('/api/encounters/' + e['id']).json()
    assert saved['quest']['status'] == 'awaiting_continue'
    assert len(saved['quest']['group']['stash']) == 1
    assert weapons(ids[0]) == 1
    advance(client, e, {'return_to_village': True})
    assert weapons(ids[0]) == 2
