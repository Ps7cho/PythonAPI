import pytest

@pytest.fixture(autouse=True)
def legacy_journey_catalog():
    """Exercise the pre-group rules still held by existing saved journeys."""
    with SessionLocal.begin() as db:
        originals = {t.slug: t.journey for t in db.scalars(select(QuestTemplate))}
        for template in db.scalars(select(QuestTemplate)):
            rules = {k: v for k, v in template.journey.items() if k not in ('encounter_groups', 'death_policy')}
            if rules.get('kind') == 'quest':
                rules.pop('max_rests', None)
                rules.pop('push_xp_tiers', None)
            template.journey = rules
    yield
    with SessionLocal.begin() as db:
        for slug, rules in originals.items():
            db.get(QuestTemplate, slug).journey = rules

from uuid import UUID

from app.database import SessionLocal
from app.models import Adventurer, Encounter, Enemy, GameEvent, QuestTemplate
from app.enemies import roll_enemy
from sqlalchemy import select


def create_hero(client):
    return client.post('/api/adventurers', json={'name': 'Journey hero'}).json()['id']


def win(client, encounter, hero):
    for _ in range(60):
        if encounter['state'] != 'player_turn':
            break
        actor = next(p for p in encounter['participants'] if p['id'] == hero)
        target = min((e for e in encounter['enemies'] if e['hp'] > 0), key=lambda e: e['hp'])
        ready = actor.get('ability_ready_turns', {})
        power = next(a for a in actor['equipped_abilities'] if a['catalog_slug'] == 'power_strike')
        action = 'power_strike' if target['hp'] > 10 and encounter['turn'] >= ready.get(power['slug'], 1) else 'attack'
        response = client.post('/api/encounters/' + encounter['id'] + '/actions', json={
            'actor_id': hero, 'expected_turn': encounter['turn'], 'action': action, 'target_id': target['id']})
        assert response.status_code == 200, response.text
        encounter = response.json()
    assert encounter['state'] == 'victory', encounter
    return encounter


def test_epic_full_loop_group_plan_camp_rewards_and_village_rest(client):
    hero = create_hero(client)
    from app.models import EquippedWeapon
    with SessionLocal.begin() as db:
        db.get(EquippedWeapon, UUID(hero)).weapon.base_damage = 25
    response = client.post('/api/encounters', json={'adventurer_ids': [hero], 'template_slug': 'mosswood-epic'})
    assert response.status_code == 201
    encounter = response.json()
    assert encounter['quest']['encounter_count'] == 5
    assert encounter['quest']['journey']['kind'] == 'epic'
    assert client.post('/api/adventurers/' + hero + '/rest').status_code == 409
    for number in range(1, 6):
        assert encounter['quest']['xp_bonus_percent'] == [0, 5, 10, 0, 5][number - 1]
        assert encounter['quest']['push_streak'] == [0, 1, 2, 0, 1][number - 1]
        encounter = win(client, encounter, hero)
        before_hp = encounter['participants'][0]['hp']
        saved = client.get('/api/encounters/' + encounter['id']).json()
        assert saved['enemies'] == encounter['enemies']
        if number < 5:
            if number <= 3:
                assert encounter['quest']['camp_rest']['heal_percent'] == 35
            else:
                assert encounter['quest']['camp_rest'] is None
            old_id = encounter['id']
            response = client.post('/api/encounters/' + old_id + '/continue', json={'choice': 'rest' if number == 3 else 'push'})
            assert response.status_code == 200, response.text
            encounter = response.json()
            assert encounter['participants'][0]['hp'] == min(100, before_hp + (35 if number == 3 else 0))
            assert client.post('/api/encounters/' + old_id + '/continue').status_code == 409
            assert client.get('/api/adventurers/' + hero).json()['health'] == encounter['participants'][0]['hp']
            if number in (1, 2, 4):
                assert len(encounter['enemies']) == 2
    sheet = client.get('/api/adventurers/' + hero).json()
    assert sheet['gold'] == 145 and sheet['experience'] == 133
    assert sheet['active_encounter_id'] is None
    assert client.post('/api/adventurers/' + hero + '/rest').status_code == 200
    assert client.get('/api/adventurers/' + hero).json()['health'] == 100
    with SessionLocal() as db:
        assert db.get(Adventurer, UUID(hero)).combat_cooldowns == {'turns': {}, 'ready_at': {}}
        run_id = db.get(Encounter, UUID(encounter['id'])).quest_run_id
        assert len(db.scalars(select(GameEvent).where(GameEvent.quest_run_id == run_id, GameEvent.event_type == 'camp_rest')).all()) == 1


def test_short_quest_no_camp_and_early_return_keeps_only_earned_rewards(client):
    hero = create_hero(client)
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero], 'template_slug': 'village-patrol'}).json()
    assert encounter['quest']['encounter_count'] == 2
    encounter = win(client, encounter, hero)
    assert encounter['quest']['camp_rest'] is None
    result = client.post('/api/encounters/' + encounter['id'] + '/continue', json={'return_to_village': True})
    assert result.status_code == 200
    assert result.json()['quest']['status'] == 'returned'
    assert client.post('/api/encounters/' + encounter['id'] + '/continue').status_code == 409
    sheet = client.get('/api/adventurers/' + hero).json()
    assert sheet['gold'] == 10 and sheet['experience'] == 8
    assert client.post('/api/adventurers/' + hero + '/rest').status_code == 200


def test_wait_advances_turn_and_multi_enemy_wipe_persists(client):
    hero = create_hero(client)
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero]}).json()
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(encounter['id']))
        first = saved.enemy_instances[0]
        first.state = {**first.state, 'equipped_weapon': {**first.state['equipped_weapon'], 'base_damage': 200}}
        extra = roll_enemy(db.get(Enemy, 'roadside-bandit'), 1)
        extra.position = 1
        saved.enemy_instances.append(extra)
    result = client.post('/api/encounters/' + encounter['id'] + '/actions', json={
        'actor_id': hero, 'expected_turn': 1, 'action': 'wait'})
    assert result.status_code == 200
    assert result.json()['state'] == 'defeat'
    assert client.get('/api/encounters/' + encounter['id']).json()['state'] == 'defeat'
    assert client.post('/api/adventurers/' + hero + '/rest').status_code == 409


def test_journey_is_snapshotted_and_rest_requires_ownership(client):
    hero = create_hero(client)
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero], 'template_slug': 'mosswood-epic'}).json()
    with SessionLocal.begin() as db:
        template = db.get(QuestTemplate, 'mosswood-epic')
        original = dict(template.journey)
        template.journey = {**original, 'gold': 9999}
    try:
        saved = client.get('/api/encounters/' + encounter['id']).json()
        assert saved['quest']['journey']['gold'] == 18
    finally:
        with SessionLocal.begin() as db:
            db.get(QuestTemplate, 'mosswood-epic').journey = original
    client.post('/api/auth/logout')
    assert client.post('/api/adventurers/' + hero + '/rest').status_code == 401


def test_camp_recovers_cooldowns_and_wait_allows_progress(client):
    from app.journeys import apply_rest, RestRules
    actor = {'hp': 20, 'max_hp': 100, 'ability_ready_turns': {'hit': 9},
             'ability_ready_at': {'spell': 900}, 'power_ready_turn': 9}
    assert apply_rest(actor, RestRules(heal_percent=35, turns=3, seconds=300)) == 35
    assert actor['ability_ready_turns']['hit'] == 6
    assert actor['ability_ready_at']['spell'] == 600
    assert actor['power_ready_turn'] == 6
    hero = create_hero(client)
    with SessionLocal.begin() as db:
        sheet = db.get(Adventurer, UUID(hero))
        power = next(a.ability for a in sheet.ability_inventory if a.ability.slug == 'power_strike')
        ability_id = str(power.id)
        sheet.combat_cooldowns = {'turns': {ability_id: 2}, 'ready_at': {}}
    assert client.post('/api/adventurers/' + hero + '/loadout', json={'ability_ids': [ability_id]}).status_code == 200
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero]}).json()
    url = '/api/encounters/' + encounter['id'] + '/actions'
    for turn in (1, 2):
        assert client.post(url, json={'actor_id': hero, 'expected_turn': turn, 'ability_id': ability_id}).status_code == 409
        result = client.post(url, json={'actor_id': hero, 'expected_turn': turn, 'action': 'wait'})
        assert result.status_code == 200
        assert result.json()['turn'] == turn + 1
    assert client.post(url, json={'actor_id': hero, 'expected_turn': 3, 'ability_id': ability_id}).status_code == 200


def test_journey_migration_preserves_legacy_templates():
    from sqlalchemy import create_engine, inspect, text
    from app.migrations.v002_journeys import upgrade
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE quest_templates (slug VARCHAR PRIMARY KEY, name VARCHAR)'))
        conn.execute(text("INSERT INTO quest_templates VALUES ('old', 'Existing quest')"))
        upgrade(conn)
        upgrade(conn)
        assert conn.scalar(text('SELECT name FROM quest_templates')) == 'Existing quest'
        assert conn.scalar(text('SELECT journey FROM quest_templates')) == '{}'
        assert inspect(conn).has_table('rest_policies')
    engine.dispose()


def test_one_rest_limit_push_state_and_retreat_forfeits_bonus(client):
    from app.models import EquippedWeapon
    hero = create_hero(client)
    with SessionLocal.begin() as db:
        db.get(EquippedWeapon, UUID(hero)).weapon.base_damage = 25
    e = client.post('/api/encounters', json={'adventurer_ids':[hero], 'template_slug':'mosswood-epic'}).json()
    e = win(client, e, hero)
    url = '/api/encounters/' + e['id'] + '/continue'
    assert client.post(url).status_code == 422
    assert client.post(url, json={'choice':'rest', 'return_to_village':True}).status_code == 422
    e = client.post(url, json={'choice':'rest'}).json()
    assert e['quest']['rests_remaining'] == 0
    assert client.post(url, json={'choice':'rest'}).status_code == 409
    e = win(client, e, hero)
    url = '/api/encounters/' + e['id'] + '/continue'
    before = client.get('/api/encounters/' + e['id']).json()
    assert client.post(url, json={'choice':'rest'}).status_code == 409
    assert client.get('/api/encounters/' + e['id']).json() == before
    hp = e['participants'][0]['hp']
    e = client.post(url, json={'choice':'push'}).json()
    assert e['participants'][0]['hp'] == hp
    assert e['quest']['risk_bonus'] == {'gold':10, 'experience':8}
    assert client.post(url, json={'choice':'push'}).status_code == 409
    assert client.get('/api/encounters/' + e['id']).json()['quest']['pushes'] == 1
    e = win(client, e, hero)
    assert client.post('/api/encounters/' + e['id'] + '/continue', json={'return_to_village':True}).status_code == 200
    sheet = client.get('/api/adventurers/' + hero).json()
    assert sheet['gold'] == 54 and sheet['experience'] == 46


def test_catalog_has_three_of_each_and_legacy_epics_keep_rests(client):
    templates = client.get('/api/quest-templates').json()
    for kind in ('quest', 'epic'):
        assert sum(t['journey'].get('kind') == kind for t in templates) == 6
    assert all(t['journey']['max_rests'] == 1 for t in templates if t['journey'].get('kind') == 'epic')
    hero = create_hero(client)
    e = client.post('/api/encounters', json={'adventurer_ids':[hero], 'template_slug':'mosswood-epic'}).json()
    with SessionLocal.begin() as db:
        quest = db.get(Encounter, UUID(e['id'])).quest_run.quest
        journey = {k:v for k,v in quest.rewards['journey'].items() if k not in ('max_rests','push_gold','push_experience')}
        quest.rewards = {**quest.rewards, 'journey':journey}
    e = win(client, e, hero)
    assert client.post('/api/encounters/' + e['id'] + '/continue').status_code == 200


def test_push_xp_cap_persistence_and_final_reward(client):
    from app.models import EquippedWeapon
    hero = create_hero(client)
    with SessionLocal.begin() as db:
        db.get(EquippedWeapon, UUID(hero)).weapon.base_damage = 100
    e = client.post('/api/encounters', json={'adventurer_ids': [hero], 'template_slug': 'mosswood-epic'}).json()
    expected_total = 0
    for index, reward in enumerate([15, 16, 17, 18, 80]):
        assert e['quest']['xp_bonus_percent'] == [0, 5, 10, 15, 15][index]
        e = win(client, e, hero)
        expected_total += reward
        assert client.get('/api/adventurers/' + hero).json()['experience'] == expected_total
        if index < 4:
            path = '/api/encounters/' + e['id'] + '/continue'
            e = client.post(path, json={'choice': 'push'}).json()
            assert client.post(path, json={'choice': 'push'}).status_code == 409
            saved = client.get('/api/encounters/' + e['id']).json()
            assert saved['quest']['push_streak'] == index + 1
    assert expected_total == 146  # 84 battle XP + 30 completion + 32 flat push XP.


def test_push_xp_catalog_rules_and_legacy_snapshot(client):
    from app.journeys import boosted_encounter_xp, JourneyRules
    from pydantic import ValidationError
    import pytest
    template = client.get('/api/quest-templates/mosswood-epic').json()['journey']
    custom = {**template, 'push_xp_tiers': [3, 7]}
    assert boosted_encounter_xp(100, custom, {'push_streak': 1}) == 103
    assert boosted_encounter_xp(100, custom, {'push_streak': 99}) == 107
    assert boosted_encounter_xp(0, custom, {'push_streak': 1}) == 0
    assert boosted_encounter_xp(15, custom, {'push_streak': 1}) == 16
    for tiers in ([10, 5], [-1], [101], [1.5]):
        with pytest.raises(ValidationError):
            JourneyRules.model_validate({**template, 'push_xp_tiers': tiers})
    hero = create_hero(client)
    e = client.post('/api/encounters', json={'adventurer_ids': [hero], 'template_slug': 'mosswood-epic'}).json()
    with SessionLocal.begin() as db:
        quest = db.get(Encounter, UUID(e['id'])).quest_run.quest
        journey = {k: v for k, v in quest.rewards['journey'].items() if k != 'push_xp_tiers'}
        quest.rewards = {**quest.rewards, 'journey': journey}
    e = win(client, e, hero)
    e = client.post('/api/encounters/' + e['id'] + '/continue', json={'choice': 'push'}).json()
    assert e['quest']['xp_bonus_percent'] == 0
    e = win(client, e, hero)
    assert client.get('/api/adventurers/' + hero).json()['experience'] == 30


def test_push_xp_migration_preserves_custom_tiers_and_snapshots():
    from sqlalchemy import create_engine, MetaData, Table, Column, String, JSON
    from app.migrations.v005_push_xp import upgrade
    engine = create_engine('sqlite:///:memory:')
    metadata = MetaData()
    templates = Table('quest_templates', metadata, Column('slug', String, primary_key=True), Column('journey', JSON))
    quests = Table('quests', metadata, Column('id', String, primary_key=True), Column('rewards', JSON))
    metadata.create_all(engine)
    original = {'kind': 'epic', 'max_rests': 1}
    with engine.begin() as conn:
        conn.execute(templates.insert(), [
            {'slug': 'epic', 'journey': original},
            {'slug': 'custom', 'journey': {**original, 'push_xp_tiers': [2, 4]}},
            {'slug': 'quest', 'journey': {'kind': 'quest'}}])
        conn.execute(quests.insert().values(id='running', rewards={'journey': original}))
        upgrade(conn)
        upgrade(conn)
        results = dict(conn.execute(select(templates.c.slug, templates.c.journey)).all())
        assert results['epic']['push_xp_tiers'] == [5, 10, 15]
        assert results['custom']['push_xp_tiers'] == [2, 4]
        assert 'push_xp_tiers' not in results['quest']
        assert conn.scalar(select(quests.c.rewards)) == {'journey': original}
    engine.dispose()
