from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import create_engine, inspect, select, text

from app.catalog_editor import CATALOGS
from app.catalog_icons import ICON_LIBRARY
from app.database import SessionLocal
from app.models import User, WeaponDefinition
from app.migrations.v040_catalog_icons import upgrade


@pytest.fixture
def icon_editor(client):
    username = client.get('/api/auth/me').json()['username']
    with SessionLocal.begin() as db:
        db.scalar(select(User).where(User.username == username)).account_type = 'developer'
    return client


def records(client):
    return client.get('/api/abilities?inspect=true').json()['editor']['catalogs']


def test_worldsmith_catalog_icons_exclude_quest_routes(icon_editor):
    for name, schema in records(icon_editor).items():
        if name == 'quests':
            assert all(f['name'] != 'icon_path' for f in schema['fields'])
            assert all('icon_path' not in r['values'] for r in schema['records'])
            continue
        field = next(f for f in schema['fields'] if f['name'] == 'icon_path')
        assert field['nullable'] and not field['immutable']


@pytest.mark.parametrize('catalog', ['abilities', 'enemies', 'weapon_definitions', 'gear_definitions', 'consumables', 'enemy_abilities'])
def test_icon_review_save_reload_and_clear(icon_editor, catalog):
    if catalog == 'weapon_definitions':
        with SessionLocal.begin() as db:
            db.add(WeaponDefinition(slug='icon-test-blade', name='Icon Test Blade', weapon_type_slug='sword', base_damage=10, required_rank='iron'))
    original = deepcopy(records(icon_editor)[catalog]['records'][0])
    current = original

    def edit(path, review=False):
        return icon_editor.post('/api/catalog-editor', json=dict(
            catalog=catalog, key=current['key'], values={**current['values'], 'icon_path': path},
            expected_revision=current['revision'], validate_only=review))

    try:
        path = ICON_LIBRARY[0]['path']
        assert edit(path, review=True).status_code == 200
        untouched = next(r for r in records(icon_editor)[catalog]['records'] if r['key'] == original['key'])
        assert untouched['values']['icon_path'] == original['values']['icon_path']
        saved = edit(path)
        assert saved.status_code == 200, saved.text
        current = saved.json()['record']
        reloaded = next(r for r in records(icon_editor)[catalog]['records'] if r['key'] == original['key'])
        assert reloaded['values']['icon_path'] == path
        assert reloaded['revision'] != original['revision']
        stale = icon_editor.post('/api/catalog-editor', json=dict(
            catalog=catalog, key=original['key'], values=original['values'], expected_revision=original['revision']))
        assert stale.status_code == 409
        cleared = edit(None)
        assert cleared.status_code == 200, cleared.text
        current = cleared.json()['record']
        assert current['values']['icon_path'] is None
    finally:
        assert edit(original['values']['icon_path']).status_code == 200
        with SessionLocal.begin() as db:
            if catalog == 'weapon_definitions':
                db.delete(db.get(WeaponDefinition, 'icon-test-blade'))


@pytest.mark.parametrize('path', ['https://example.com/icon.webp', 'assets/icons/../private.webp', 'assets/icons/unknown.webp'])
def test_uninstalled_icons_rejected(icon_editor, path):
    record = records(icon_editor)['abilities']['records'][0]
    result = icon_editor.post('/api/catalog-editor', json=dict(
        catalog='abilities', key=record['key'], values={**record['values'], 'icon_path': path},
        expected_revision=record['revision']))
    assert result.status_code == 422


def test_icon_migration_preserves_existing_rows_and_can_repeat():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        for model in CATALOGS.values():
            table = model.__tablename__
            conn.execute(text(f'CREATE TABLE {table} (legacy_value VARCHAR)'))
            conn.execute(text(f"INSERT INTO {table} VALUES ('preserved')"))
        upgrade(conn)
        upgrade(conn)
        for model in CATALOGS.values():
            table = model.__tablename__
            if 'icon_path' not in model.__table__.columns:
                assert 'icon_path' not in {c['name'] for c in inspect(conn).get_columns(table)}
                assert conn.execute(text(f'SELECT legacy_value FROM {table}')).scalar_one() == 'preserved'
                continue
            assert 'icon_path' in {c['name'] for c in inspect(conn).get_columns(table)}
            assert conn.execute(text(f'SELECT legacy_value, icon_path FROM {table}')).one() == ('preserved', None)
    engine.dispose()


def test_weapon_inventory_uses_blueprint_assignment(client):
    from app.weapon_effects import create_weapon
    from app.weapons import serialize_weapon
    hero = client.post('/api/adventurers', json={'name': 'Icon weapon owner'}).json()
    with SessionLocal() as db:
        definition = WeaponDefinition(slug='icon-inventory-blade', name='Icon Inventory Blade',
                                      weapon_type_slug='sword', base_damage=12, required_rank='iron',
                                      icon_path=ICON_LIBRARY[0]['path'])
        db.add(definition)
        weapon = create_weapon(db, adventurer_id=UUID(hero['id']), weapon_type_slug='sword',
                               name=definition.name, base_damage=12, effects=[],
                               weapon_definition_slug=definition.slug)
        db.flush()
        assert serialize_weapon(weapon)['icon_path'] == ICON_LIBRARY[0]['path']
        assert serialize_weapon(weapon)['weapon_definition_slug'] == definition.slug
        definition.icon_path = None
        assert serialize_weapon(weapon)['icon_path'] is None
        db.rollback()


def test_shop_artwork_does_not_mutate_saved_stock():
    from app.models import Consumable
    from app.shop import stock_icons
    stock = [{'item_type': 'consumable', 'slug': 'healing-potion', 'price': 20}]
    before = deepcopy(stock)
    with SessionLocal() as db:
        db.get(Consumable, 'healing-potion').icon_path = ICON_LIBRARY[0]['path']
        db.flush()
        display = stock_icons(db, stock)
        assert display[0]['icon_path'] == ICON_LIBRARY[0]['path']
        assert display[0]['price'] == 20
        assert stock == before
        db.rollback()


@pytest.mark.parametrize('stored_icon', ['missing', 'outdated'])
def test_active_encounter_uses_current_icons_and_preserves_saved_combat(icon_editor, stored_icon):
    from app.models import Ability, Encounter
    hero = icon_editor.post('/api/adventurers', json={'name': 'Active encounter icons'}).json()
    started = icon_editor.post('/api/encounters', json={'adventurer_ids': [hero['id']]}).json()
    encounter_id = UUID(started['id'])
    catalog_record = next(r for r in records(icon_editor)['abilities']['records'] if r['values']['slug'] == 'attack')
    ability_id = catalog_record['values']['id']
    with SessionLocal.begin() as db:
        encounter = db.get(Encounter, encounter_id)
        actors = deepcopy(encounter.participants)
        spec = next(s for s in actors[0]['equipped_abilities'] if s['slug'] == ability_id)
        spec['damage'] = 17
        if stored_icon == 'missing':
            spec.pop('icon_path', None)
        else:
            spec['icon_path'] = ICON_LIBRARY[1]['path']
        encounter.participants = actors
        original_combat = deepcopy(actors)
    try:
        values = {**catalog_record['values'], 'icon_path': ICON_LIBRARY[0]['path'], 'power': catalog_record['values']['power'] + 50}
        result = icon_editor.post('/api/catalog-editor', json=dict(
            catalog='abilities', key=catalog_record['key'], values=values, expected_revision=catalog_record['revision']))
        assert result.status_code == 200, result.text
        saved = result.json()['record']
        response = icon_editor.get('/api/encounters/' + started['id']).json()
        actor = response['participants'][0]
        for field in ['equipped_abilities', 'effective_abilities']:
            ability = next(a for a in actor[field] if a['slug'] == ability_id)
            assert ability['icon_path'] == ICON_LIBRARY[0]['path']
            assert ability['damage'] == 17
        with SessionLocal() as db:
            assert db.get(Encounter, encounter_id).participants == original_combat
        cleared = icon_editor.post('/api/catalog-editor', json=dict(
            catalog='abilities', key=saved['key'], values={**saved['values'], 'icon_path': None}, expected_revision=saved['revision']))
        assert cleared.status_code == 200, cleared.text
        actor = icon_editor.get('/api/encounters/' + started['id']).json()['participants'][0]
        assert next(a for a in actor['effective_abilities'] if a['slug'] == ability_id)['icon_path'] is None
    finally:
        with SessionLocal.begin() as db:
            ability = db.get(Ability, UUID(ability_id))
            ability.icon_path = catalog_record['values']['icon_path']
            ability.power = catalog_record['values']['power']
