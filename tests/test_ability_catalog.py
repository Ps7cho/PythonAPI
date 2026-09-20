from uuid import uuid4

from sqlalchemy import create_engine, delete, select, text

from app.database import SessionLocal
from app.models import Ability, Enemy, EnemyAbility, EncounterEnemy


def test_custom_enemy_uses_database_priority_cooldown_and_snapshot(client, request):
    slug = 'custom-' + uuid4().hex
    with SessionLocal.begin() as db:
        enemy = Enemy(slug=slug, name=slug, enemy_type='beast', attributes={},
                      stat_ranges={key: {'min': value, 'max': value}
                                   for key, value in {'hp': 200, 'power': 8, 'guard': 0, 'speed': 1}.items()})
        heavy = Ability(slug=slug + '-heavy', name=slug + ' heavy', damage_multiplier=2,
                        cooldown_value=3, cost_type='None', max_targets=None)
        basic = Ability(slug=slug + '-basic', name=slug + ' basic', damage_multiplier=1, max_targets=None)
        db.add_all([enemy, heavy, basic])
        db.flush()
        heavy_id = heavy.id
        db.add_all([EnemyAbility(enemy_slug=slug, ability_id=heavy.id, priority=2),
                    EnemyAbility(enemy_slug=slug, ability_id=basic.id)])
    def cleanup():
        with SessionLocal.begin() as db:
            db.execute(delete(EncounterEnemy).where(EncounterEnemy.enemy_slug == slug))
            db.execute(delete(EnemyAbility).where(EnemyAbility.enemy_slug == slug))
            db.execute(delete(Enemy).where(Enemy.slug == slug))
            db.execute(delete(Ability).where(Ability.slug.in_([slug + '-heavy', slug + '-basic'])))
    request.addfinalizer(cleanup)
    heroes = [client.post('/api/adventurers', json={'name': 'Catalog hero'}).json() for _ in range(2)]
    sheet = client.get('/api/adventurers/' + heroes[0]['id']).json()
    assert all(a['slug'] not in (slug + '-heavy', slug + '-basic') for a in sheet['abilities'])
    encounter = client.post('/api/encounters', json={'adventurer_ids': [h['id'] for h in heroes], 'enemy_slug': slug}).json()
    with SessionLocal.begin() as db:
        db.get(Ability, heavy_id).damage_multiplier = 9
    for turn, expected_slug, damage in [(1, slug + '-heavy', 6), (2, slug + '-basic', 3)]:
        for hero in heroes:
            response = client.post('/api/encounters/' + encounter['id'] + '/actions', json={
                'actor_id': hero['id'], 'expected_turn': turn, 'action': 'guard'})
            assert response.status_code == 200, response.text
        results = response.json()['action_results'][1:]
        assert len(results) == 2
        assert [r['ability'] for r in results] == [expected_slug, expected_slug]
        assert [r['amount'] for r in results] == [damage, damage]


def test_renamed_player_ability_uses_stable_alias_and_scaled_power(client):
    with SessionLocal.begin() as db:
        ability = db.scalar(select(Ability).where(Ability.slug == 'attack'))
        old_name, old_multiplier = ability.name, ability.damage_multiplier
        ability_id = ability.id
        ability.name, ability.damage_multiplier = 'Renamed strike', 2.5
    try:
        hero = client.post('/api/adventurers', json={'name': 'Alias test'}).json()
        encounter = client.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
        response = client.post('/api/encounters/' + encounter['id'] + '/actions', json={
            'actor_id': hero['id'], 'expected_turn': 1, 'action': 'attack'})
        assert response.status_code == 200, response.text
        assert response.json()['action_results'][0]['amount'] == 25
        assert response.json()['action_results'][0]['ability'] == str(ability_id)
    finally:
        with SessionLocal.begin() as db:
            ability = db.get(Ability, ability_id)
            ability.name, ability.damage_multiplier = old_name, old_multiplier


def test_legacy_catalog_upgrade_is_repeatable_and_preserves_balance(monkeypatch):
    from app import database
    engine = create_engine('sqlite:///:memory:')
    monkeypatch.setattr(database, 'engine', engine)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE abilities (id VARCHAR PRIMARY KEY, name VARCHAR, power INTEGER)'))
        conn.execute(text("INSERT INTO abilities VALUES ('old-id', 'Strike', 37)"))
    database.ensure_ability_columns()
    database.ensure_ability_columns()
    with engine.connect() as conn:
        row = conn.execute(text('SELECT * FROM abilities')).mappings().one()
        assert row['slug'] == 'attack'
        assert row['power'] == 37
        assert row['damage_multiplier'] == 3.7
        assert row['requires_weapon'] == 1
        assert row['starter'] == 1
        assert row['loadout_order'] == 0
    engine.dispose()
