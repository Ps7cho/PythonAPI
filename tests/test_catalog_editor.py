from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import JSON, event, select, func
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.main import app
from app.database import SessionLocal
from app.models import Ability, AbilityArchetype, EnemyAbility, GameEvent, QuestTemplate, User, WeaponDefinition


@pytest.fixture
def editor(client):
    username = client.get('/api/auth/me').json()['username']
    with SessionLocal.begin() as db:
        db.scalar(select(User).where(User.username == username)).account_type = 'developer'
    return client


def catalog(client):
    return client.get('/api/abilities?inspect=true').json()['editor']['catalogs']


def draft(catalog_name, record, **changes):
    return dict(catalog=catalog_name, key=record['key'], values={**deepcopy(record['values']), **changes}, expected_revision=record['revision'])


@pytest.mark.parametrize('json_python_type', [dict, object], ids=['sqlalchemy20', 'sqlalchemy21'])
@pytest.mark.parametrize('catalog_name', ['abilities', 'ability_archetypes'])
def test_json_catalog_duplicate_edit_and_delete(editor, monkeypatch, json_python_type, catalog_name):
    # SQLAlchemy 2.1 reports object for JSON.python_type, unlike 2.0's dict.
    monkeypatch.setattr(JSON, 'python_type', property(lambda self: json_python_type))
    schema = catalog(editor)[catalog_name]
    json_names = {'affliction_ops', 'effect_chain', 'rank_upgrades', 'allowed_weapon_tags'} if catalog_name == 'abilities' else {'definition'}
    assert {f['name'] for f in schema['fields'] if f['type'] == 'json'} == json_names
    source = next((r for r in schema['records'] if r['values'].get('affliction_ops')), schema['records'][0])
    unique = uuid4()
    payload = draft(catalog_name, source, name='JSON regression ' + unique.hex, slug='json-' + unique.hex)
    if catalog_name == 'abilities':
        payload['values']['id'] = str(unique)
    payload.update(create=True, expected_revision=None,
                   key={f['name']: payload['values'][f['name']] for f in schema['fields'] if f['primary_key']})
    model = Ability if catalog_name == 'abilities' else AbilityArchetype
    key = unique if catalog_name == 'abilities' else payload['values']['slug']
    try:
        review = editor.post('/api/catalog-editor', json={**payload, 'validate_only': True})
        assert review.status_code == 200, review.text
        with SessionLocal() as db:
            assert db.get(model, key) is None
        payload['values'] = review.json()['record']['values']
        saved = editor.post('/api/catalog-editor', json=payload)
        assert saved.status_code == 200, saved.text
        record = saved.json()['record']
        for name in json_names:
            assert record['values'][name] == source['values'][name]
        edit = draft(catalog_name, record, description='Edited test duplicate')
        assert editor.post('/api/catalog-editor', json={**edit, 'validate_only': True}).status_code == 200
        edited = editor.post('/api/catalog-editor', json=edit)
        assert edited.status_code == 200, edited.text
        removal = {**draft(catalog_name, edited.json()['record']), 'remove': True}
        assert editor.post('/api/catalog-editor', json={**removal, 'validate_only': True}).status_code == 200
        with SessionLocal() as db:
            assert db.get(model, key) is not None
        removed = editor.post('/api/catalog-editor', json=removal)
        assert removed.status_code == 200, removed.text
        assert not any(r['key'] == payload['key'] for r in catalog(editor)[catalog_name]['records'])
    finally:
        with SessionLocal.begin() as db:
            row = db.get(model, key)
            if row is not None:
                db.delete(row)


def test_ability_editor_locks_only_edited_table(editor):
    statements = []
    def capture(state):
        if state.is_select:
            sql = str(state.statement.compile(dialect=postgresql.dialect()))
            if 'FOR UPDATE' in sql:
                statements.append(sql)
    record = catalog(editor)['abilities']['records'][0]
    event.listen(Session, 'do_orm_execute', capture)
    try:
        response = editor.post('/api/catalog-editor', json={**draft('abilities', record), 'validate_only': True})
        assert response.status_code == 200, response.text
    finally:
        event.remove(Session, 'do_orm_execute', capture)
    assert statements
    assert all('FOR UPDATE OF abilities' in sql for sql in statements)
    assert any('LEFT OUTER JOIN status_effects' in sql for sql in statements)


def test_embedded_references_detected_with_json_object_python_type(editor, monkeypatch):
    from app.catalog_editor import deletion_references
    monkeypatch.setattr(JSON, 'python_type', property(lambda self: object))
    ability = catalog(editor)['abilities']['records'][0]
    # An embedded reference without a foreign key must still block deletion.
    with SessionLocal() as db:
        row = db.get(Ability, UUID(ability['key']['id']))
        template = AbilityArchetype(slug='ref-' + uuid4().hex, name='Embedded reference',
                                    description='', definition={'ability_id': str(row.id)})
        db.add(template)
        db.flush()
        references = deletion_references(db, Ability, row)
        assert any(r['table'] == 'ability_archetypes (embedded rule)' for r in references)
        db.rollback()


def test_editor_requires_developer_account(editor):
    record = catalog(editor)['abilities']['records'][0]
    payload = draft('abilities', record)
    with TestClient(app) as anonymous:
        assert anonymous.post('/api/catalog-editor', json=payload).status_code == 401
    username = editor.get('/api/auth/me').json()['username']
    with SessionLocal.begin() as db:
        db.scalar(select(User).where(User.username == username)).account_type = 'player'
    assert editor.post('/api/catalog-editor', json=payload).status_code == 403
    assert editor.get('/api/abilities?inspect=true').json()['editor'] == {'can_edit': False}


def test_all_seed_definitions_can_be_reviewed_without_mutation(editor):
    before = catalog(editor)
    for name, schema in before.items():
        for record in schema['records']:
            response = editor.post('/api/catalog-editor', json={**draft(name, record), 'validate_only': True})
            assert response.status_code == 200, (name, record['key'], response.text)
    assert catalog(editor) == before


def test_review_save_audit_conflict_and_live_preview(editor):
    record = catalog(editor)['abilities']['records'][0]
    key = UUID(record['key']['id'])
    original = record['values']['description']
    payload = draft('abilities', record, description='Designer-reviewed description')
    with SessionLocal() as db:
        before = db.scalar(select(func.count()).select_from(GameEvent).where(GameEvent.event_type == 'catalog_edited'))
    assert editor.post('/api/catalog-editor', json={**payload, 'validate_only': True}).status_code == 200
    with SessionLocal() as db:
        assert db.get(Ability, key).description == original
    try:
        response = editor.post('/api/catalog-editor', json=payload)
        assert response.status_code == 200, response.text
        assert editor.post('/api/catalog-editor', json=payload).status_code == 409
        assert next(a for a in editor.get('/api/abilities').json() if a['id'] == str(key))['description'] == 'Designer-reviewed description'
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(GameEvent).where(GameEvent.event_type == 'catalog_edited')) == before + 1
            event = db.scalar(select(GameEvent).where(GameEvent.event_type == 'catalog_edited').order_by(GameEvent.timestamp.desc()))
            assert event.payload['before']['description'] == original
            assert event.payload['after']['description'] == 'Designer-reviewed description'
            assert event.payload['user_id']
    finally:
        with SessionLocal.begin() as db:
            db.get(Ability, key).description = original


def test_invalid_fields_references_and_types_are_rejected(editor):
    catalogs = catalog(editor)
    record = catalogs['abilities']['records'][0]
    for changes in [dict(power=-1), dict(power=True), dict(effect_type='kill_everyone'), dict(target_type='all_enemies'),
                    dict(slug='renamed-stable-slug'),
                    dict(status_effect_slug='missing-affliction'), dict(allowed_weapon_tags={}),
                    dict(affliction_ops=[dict(op='detonate', affliction='missing-affliction')]), dict(owner='not-a-field')]:
        response = editor.post('/api/catalog-editor', json=draft('abilities', record, **changes))
        assert response.status_code == 422, response.text
    assert editor.post('/api/catalog-editor', json={**draft('abilities', record), 'catalog':'users'}).status_code == 422
    quest = next(r for r in catalogs['quests']['records'] if r['values']['journey'])
    rules = deepcopy(quest['values']['journey'])
    rules['stages'][0]['groups'] = [['missing-enemy']]
    assert editor.post('/api/catalog-editor', json=draft('quests', quest, journey=rules)).status_code == 422
    assert catalog(editor) == catalogs


def test_create_copy_conflict_and_new_reference(editor):
    catalogs = catalog(editor)
    source = catalogs['abilities']['records'][0]
    ability_id = uuid4()
    payload = draft('abilities', source, id=str(ability_id), slug='designer-' + ability_id.hex,
                    name='Designer ' + ability_id.hex, affliction_ops={})
    payload.update(create=True, key={'id': str(ability_id)}, expected_revision=None)
    try:
        assert editor.post('/api/catalog-editor', json={**payload, 'validate_only': True}).status_code == 200
        with SessionLocal() as db: assert db.get(Ability, ability_id) is None
        response = editor.post('/api/catalog-editor', json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['record']['values']['affliction_ops'] == []
        assert editor.post('/api/catalog-editor', json=payload).status_code == 409
        assignment = catalogs['enemy_abilities']['records'][0]
        linked = draft('enemy_abilities', assignment, ability_id=str(ability_id))
        linked.update(create=True, key={**assignment['key'], 'ability_id':str(ability_id)}, expected_revision=None, validate_only=True)
        assert editor.post('/api/catalog-editor', json=linked).status_code == 200
        created = response.json()['record']
        assert editor.post('/api/catalog-editor', json={**draft('abilities', created), 'remove': True}).status_code == 200
        with SessionLocal() as db:
            assert db.get(Ability, ability_id) is None
    finally:
        with SessionLocal.begin() as db:
            row = db.get(Ability, ability_id)
            if row: db.delete(row)


def test_associations_can_be_reviewed_and_removed(editor):
    assignment = catalog(editor)['enemy_abilities']['records'][0]
    payload = {**draft('enemy_abilities', assignment), 'remove': True}
    enemy_slug = assignment['values']['enemy_slug']
    ability_id = UUID(assignment['values']['ability_id'])
    try:
        assert editor.post('/api/catalog-editor', json={**payload, 'validate_only': True}).status_code == 200
        with SessionLocal() as db:
            assert db.get(EnemyAbility, (enemy_slug, ability_id)) is not None
        response = editor.post('/api/catalog-editor', json=payload)
        assert response.status_code == 200 and response.json()['removed']
        with SessionLocal() as db:
            assert db.get(EnemyAbility, (enemy_slug, ability_id)) is None
        # Definitions can now be deleted, but never while a saved relation
        # still points at them.
        remaining = catalog(editor)['enemy_abilities']['records'][0]
        ability = next(record for record in catalog(editor)['abilities']['records']
                       if record['values']['id'] == remaining['values']['ability_id'])
        blocked = editor.post('/api/catalog-editor', json={**draft('abilities', ability), 'remove': True})
        assert blocked.status_code == 409
        assert 'linked records' in blocked.json()['detail']
    finally:
        with SessionLocal.begin() as db:
            if db.get(EnemyAbility, (enemy_slug, ability_id)) is None:
                db.add(EnemyAbility(enemy_slug=enemy_slug, ability_id=ability_id,
                                    weight=assignment['values']['weight'], priority=assignment['values']['priority']))


def test_delete_uses_the_stored_record_not_an_outdated_editor_payload(editor):
    """Deletion must still be possible to review when legacy JSON is malformed."""
    assignment = catalog(editor)['enemy_abilities']['records'][0]
    ability = next(record for record in catalog(editor)['abilities']['records']
                   if record['values']['id'] == assignment['values']['ability_id'])
    payload = draft('abilities', ability, affliction_ops={})
    response = editor.post('/api/catalog-editor', json={**payload, 'remove': True, 'validate_only': True})
    assert response.status_code == 409
    assert 'linked records' in response.json()['detail']


def test_weapon_blueprint_can_be_created_and_deployed_to_quest(editor):
    catalogs = catalog(editor)
    weapon = dict(slug='designer-test-blade', name='Designer Test Blade', weapon_type_slug='sword',
                  base_damage=17, required_rank='iron')
    create = dict(catalog='weapon_definitions', key={'slug': weapon['slug']}, values=weapon,
                  create=True, expected_revision=None)
    quest = next(r for r in catalogs['quests']['records']
                 if r['values'].get('journey', {}).get('encounter_groups', {}).get('loot_tiers'))
    original = deepcopy(quest['values'])
    paired = deepcopy(original)
    paired['journey']['encounter_groups']['loot_tiers'][0]['drops'].append(
        {'name': weapon['name'], 'weapon_type_slug': weapon['weapon_type_slug'],
         'base_damage': weapon['base_damage'], 'weight': 1})
    try:
        assert editor.post('/api/catalog-editor', json={**create, 'validate_only': True}).status_code == 200
        assert editor.post('/api/catalog-editor', json=create).status_code == 200
        deploy = draft('quests', quest, journey=paired['journey'])
        assert editor.post('/api/catalog-editor', json={**deploy, 'validate_only': True}).status_code == 200
        assert editor.post('/api/catalog-editor', json=deploy).status_code == 200
        with SessionLocal() as db:
            assert db.get(WeaponDefinition, weapon['slug']).base_damage == 17
            stored = db.get(QuestTemplate, quest['key']['slug']).journey
            assert any(d.get('name') == weapon['name'] for d in stored['encounter_groups']['loot_tiers'][0]['drops'])
    finally:
        with SessionLocal.begin() as db:
            row = db.get(WeaponDefinition, weapon['slug'])
            if row: db.delete(row)
            db.get(QuestTemplate, quest['key']['slug']).journey = original['journey']
