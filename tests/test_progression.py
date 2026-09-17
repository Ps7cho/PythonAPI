from uuid import UUID

from sqlalchemy import select
from app.database import SessionLocal
from app.models import Adventurer, Encounter, Weapon, AdventurerAbility, Ability
from app.progression import award_experience, describe, level_cost, level_floor


def create(client):
    return client.post('/api/adventurers', json={"name": "Rank tester"}).json()['id']


def test_xp_formula_boundaries_multiple_levels_and_rank_reward(client):
    assert [level_cost(n) for n in (1, 2, 3, 4, 10)] == [100, 283, 520, 800, 3163]
    hero_id = create(client)
    with SessionLocal.begin() as db:
        hero = db.get(Adventurer, UUID(hero_id))
        award_experience(db, hero, 99)
        assert (hero.level, hero.attribute_points) == (1, 0)
        award_experience(db, hero, 1)
        assert (hero.level, hero.attribute_points) == (2, 3)
        messages = award_experience(db, hero, level_floor(10) - 100)
        assert (hero.level, hero.attribute_points, hero.experience) == (10, 27, level_floor(10))
        assert any('RANK UP:' in m and 'Bronze' in m for m in messages)
        assert describe(db, hero)['ability_slots'] == 4
        db.flush()
        weapons = db.scalars(select(Weapon).where(Weapon.adventurer_id == hero.id, Weapon.required_rank == 'bronze')).all()
        assert len(weapons) == 1 and weapons[0].base_damage == 14
        assert award_experience(db, hero, 0) == []
    data = client.get('/api/adventurers/' + hero_id).json()
    assert data['progression']['xp_to_rank'] == level_floor(20) - level_floor(10)
    assert data['progression']['level_xp'] == 0


def test_attribute_spending_validation_and_persistence(client):
    hero_id = create(client)
    with SessionLocal.begin() as db:
        hero = db.get(Adventurer, UUID(hero_id))
        award_experience(db, hero, 100)
        before = hero.attributes['Might']
    path = '/api/adventurers/' + hero_id + '/attributes'
    for values in ({'Might': -1}, {'Might': True}, {'fake': 1}, {}):
        assert client.post(path, json={'allocations': values}).status_code == 422
    assert client.post(path, json={'allocations': {'Might': 4}}).status_code == 409
    response = client.post(path, json={'allocations': {'Might': 3}})
    assert response.status_code == 200
    assert response.json()['attributes']['Might'] == before + 3
    assert response.json()['attribute_points'] == 0
    assert client.post(path, json={'allocations': {'Might': 1}}).status_code == 409


def test_rank_gates_equipment_and_journey(client):
    hero_id = create(client)
    assert client.post('/api/encounters', json={'adventurer_ids': [hero_id], 'template_slug': 'bronze-contract'}).status_code == 409
    with SessionLocal.begin() as db:
        weapon = Weapon(adventurer_id=UUID(hero_id), name='Restricted', weapon_type_slug='sword', base_damage=14, required_rank='bronze')
        db.add(weapon); db.flush(); weapon_id = str(weapon.id)
    path = '/api/adventurers/' + hero_id + '/weapon'
    assert client.post(path, json={'weapon_id': weapon_id}).status_code == 409
    with SessionLocal.begin() as db:
        award_experience(db, db.get(Adventurer, UUID(hero_id)), level_floor(10))
    assert client.post(path, json={'weapon_id': weapon_id}).status_code == 200
    assert client.post('/api/encounters', json={'adventurer_ids': [hero_id], 'template_slug': 'bronze-contract'}).status_code == 201
    assert client.post('/api/adventurers/' + hero_id + '/attributes', json={'allocations': {'Might': 1}}).status_code == 409


def test_victory_levels_once_and_replay_does_not_duplicate(client):
    hero_id = create(client)
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero_id]}).json()
    with SessionLocal.begin() as db:
        saved = db.get(Encounter, UUID(encounter['id']))
        saved.quest_run.quest.rewards = {**saved.quest_run.quest.rewards, 'experience': 100}
        for enemy in saved.enemy_instances:
            enemy.state = {**enemy.state, 'hp': 1}
    path = '/api/encounters/' + encounter['id'] + '/actions'
    command = {'actor_id': hero_id, 'expected_turn': 1, 'action': 'attack'}
    response = client.post(path, json=command)
    assert response.status_code == 200, response.text
    assert response.json()['state'] == 'victory'
    assert any('LEVEL UP:' in event for event in response.json()['events'])
    assert client.post(path, json=command).status_code == 409
    with SessionLocal() as db:
        hero = db.get(Adventurer, UUID(hero_id))
        assert (hero.experience, hero.level, hero.attribute_points) == (100, 2, 3)


def test_bronze_loadout_capacity_is_enforced_by_server(client):
    hero_id = create(client)
    with SessionLocal.begin() as db:
        hero = db.get(Adventurer, UUID(hero_id))
        known = {entry.ability_id for entry in hero.ability_inventory}
        extra = db.scalar(select(Ability).where(Ability.id.not_in(known), Ability.effect_type == 'damage'))
        db.add(AdventurerAbility(adventurer_id=hero.id, ability_id=extra.id, unlocked=True))
        ids = [str(entry.ability_id) for entry in hero.ability_inventory if entry.unlocked][:3] + [str(extra.id)]
    path = '/api/adventurers/' + hero_id + '/loadout'
    assert len(ids) == 4
    assert client.post(path, json={'ability_ids': ids}).status_code == 422
    with SessionLocal.begin() as db:
        award_experience(db, db.get(Adventurer, UUID(hero_id)), level_floor(10))
    response = client.post(path, json={'ability_ids': ids})
    assert response.status_code == 200, response.text
    assert response.json()['equipped_ability_ids'] == ids


def test_migration_backfills_lifetime_xp_once():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from uuid import uuid4
    from app.database import Base
    from app.migrations import migrate
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    from sqlalchemy import text
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE schema_migrations (version VARCHAR PRIMARY KEY)'))
        for version in ('001_targeting_cooldowns', '002_journeys', '003_epic_choices'):
            conn.execute(text('INSERT INTO schema_migrations VALUES (:version)'), {'version': version})
    hero_id = uuid4()
    with Session(engine) as db:
        db.add(Adventurer(id=hero_id, owner=uuid4(), name='Legacy', experience=level_floor(10)))
        db.commit()
    migrate(engine)
    with Session(engine) as db:
        hero = db.get(Adventurer, hero_id)
        assert (hero.level, hero.attribute_points, hero.experience) == (10, 27, level_floor(10))
        hero.attribute_points -= 3
        db.commit()
    migrate(engine)
    with Session(engine) as db:
        assert db.get(Adventurer, hero_id).attribute_points == 24
    engine.dispose()


def test_rank_awards_rollback_with_transaction_and_legendary_has_no_next_rank(client):
    hero_id = create(client)
    try:
        with SessionLocal.begin() as db:
            award_experience(db, db.get(Adventurer, UUID(hero_id)), level_floor(20))
            db.flush()
            raise RuntimeError('simulate failed combat transaction')
    except RuntimeError:
        pass
    with SessionLocal.begin() as db:
        hero = db.get(Adventurer, UUID(hero_id))
        assert (hero.level, hero.experience, hero.attribute_points) == (1, 0, 0)
        assert not db.scalar(select(Weapon.id).where(Weapon.adventurer_id == hero.id, Weapon.required_rank != 'iron'))
        award_experience(db, hero, level_floor(70))
        info = describe(db, hero)
        assert info['rank'] == 'Legendary' and info['next_rank'] is None
        assert info['xp_to_rank'] is None and info['passive_slots'] == 2
        assert [r['level'] for r in info['ladder']] == [1, 10, 20, 30, 40, 50, 60, 70]
