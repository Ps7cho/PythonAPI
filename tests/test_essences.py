from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database import SessionLocal
from app.models import AbsorbedEssence, OwnedConsumable, Adventurer
from app.consumables import grant
from test_encounter_groups import start, clear_group, advance


def hero(client):
    return client.post('/api/adventurers', json={'name': 'Essence seeker'}).json()['id']


def stock(id, slug, quantity=2):
    with SessionLocal.begin() as db:
        grant(db, UUID(id), slug, quantity)


def absorb(client, id, slug):
    return client.post(f'/api/adventurers/{id}/essences/{slug}/absorb', json={})


def test_absorption_cap_distinctness_and_inventory(client):
    id = hero(client)
    for slug in ('essence-dark', 'essence-holy', 'essence-fire', 'essence-magic'):
        stock(id, slug)
    for i, slug in enumerate(('essence-dark', 'essence-holy', 'essence-fire'), 1):
        response = absorb(client, id, slug)
        assert response.status_code == 200, response.text
        assert len(response.json()['essences']) == i
    assert absorb(client, id, 'essence-dark').status_code == 409
    assert absorb(client, id, 'essence-magic').status_code == 409
    with SessionLocal() as db:
        assert db.get(OwnedConsumable, (UUID(id), 'essence-dark')).quantity == 1
        assert db.get(OwnedConsumable, (UUID(id), 'essence-magic')).quantity == 2
        assert len(db.scalars(select(AbsorbedEssence).where(AbsorbedEssence.adventurer_id == UUID(id))).all()) == 3
    sheet = client.get('/api/adventurers/' + id).json()
    assert len(sheet['essence_catalog']) == 9
    assert sheet['essence_limit'] == 3
    dark = next(e for e in sheet['essence_catalog'] if e['slug'] == 'essence-dark')
    assert dark['powers']['signature']['name'] == 'Shadow Leap'
    assert not dark['powers']['signature']['implemented']


@pytest.mark.parametrize('reason', ['missing', 'unknown', 'dead', 'active'])
def test_failed_absorption_never_spends_stock(client, reason):
    id = hero(client)
    if reason != 'missing': stock(id, 'essence-blood')
    if reason == 'dead':
        with SessionLocal.begin() as db:
            saved = db.get(Adventurer, UUID(id)); saved.is_alive = False; saved.health = 0
    if reason == 'active':
        assert client.post('/api/encounters', json={'adventurer_ids': [id]}).status_code == 201
    response = absorb(client, id, 'not-an-essence' if reason == 'unknown' else 'essence-blood')
    assert response.status_code in (404, 409)
    with SessionLocal() as db:
        assert not db.scalars(select(AbsorbedEssence).where(AbsorbedEssence.adventurer_id == UUID(id))).all()
        row = db.get(OwnedConsumable, (UUID(id), 'essence-blood'))
        assert row is None if reason == 'missing' else row.quantity == 2


def test_essence_cannot_be_used_on_combat_target(client):
    id = hero(client); stock(id, 'essence-might')
    e = client.post('/api/encounters', json={'adventurer_ids': [id]}).json()
    response = client.post('/api/encounters/' + e['id'] + '/actions', json={
        'actor_id': id, 'expected_turn': 1, 'consumable_slug': 'essence-might'})
    assert response.status_code == 409
    assert client.get('/api/encounters/' + e['id']).json() == e
    with SessionLocal() as db:
        assert db.get(OwnedConsumable, (UUID(id), 'essence-might')).quantity == 2


def test_absorption_requires_ownership(client):
    id = hero(client); stock(id, 'essence-blood')
    client.post('/api/auth/logout')
    from uuid import uuid4
    client.post('/api/auth/register', json={'username': 'other_' + uuid4().hex[:12], 'password': 'test-password-1234'})
    assert absorb(client, id, 'essence-blood').status_code in (403, 404)
    with SessionLocal() as db:
        assert db.get(OwnedConsumable, (UUID(id), 'essence-blood')).quantity == 2


def test_saved_essence_loot_uses_safe_return_and_no_rerolls(client, monkeypatch):
    calls = []
    def roll(tier, rng):
        calls.append(1)
        return deepcopy(next(d for d in tier['drops'] if d.get('consumable_slug') == 'essence-magic'))
    monkeypatch.setattr('app.group_journeys.roll_loot', roll)
    ids, e = start(client)
    # Existing runs retain essence drops even though new push tables exclude them.
    from app.models import Encounter
    from app.quest_templates import JOURNEYS
    with SessionLocal.begin() as db:
        quest = db.get(Encounter, UUID(e['id'])).quest_run.quest
        rewards = deepcopy(quest.rewards)
        rewards['journey']['encounter_groups'] = deepcopy(next(t['journey']['encounter_groups'] for t in JOURNEYS if t['slug'] == 'village-patrol'))
        quest.rewards = rewards
    e = clear_group(client, e)
    with SessionLocal() as db:
        assert db.get(OwnedConsumable, (UUID(ids[0]), 'essence-magic')) is None
    advance(client, e, {'return_to_village': True})
    assert len(calls) == 1
    assert absorb(client, ids[0], 'essence-magic').status_code == 200
    assert absorb(client, ids[0], 'essence-magic').status_code == 409
    with SessionLocal() as db:
        assert db.get(OwnedConsumable, (UUID(ids[0]), 'essence-magic')).quantity == 0


def test_database_enforces_slot_limit_and_duplicate_identity(client):
    id = UUID(hero(client))
    with SessionLocal.begin() as db:
        db.add(AbsorbedEssence(adventurer_id=id, slot=1, essence_slug='essence-dark'))
    for slot, slug in [(4, 'essence-magic'), (0, 'essence-magic'), (2, 'essence-dark'), (1, 'essence-holy')]:
        with pytest.raises(IntegrityError):
            with SessionLocal.begin() as db:
                db.add(AbsorbedEssence(adventurer_id=id, slot=slot, essence_slug=slug))


def test_legacy_sqlite_migration_preserves_inventory_and_is_idempotent():
    from sqlalchemy import create_engine, text
    from app.migrations.v012_essences import upgrade
    engine = create_engine('sqlite:///:memory:')
    try:
        with engine.connect() as conn:
            conn.execute(text('PRAGMA foreign_keys=ON'))
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE consumables (slug VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, description VARCHAR NOT NULL, effect VARCHAR NOT NULL, power INTEGER NOT NULL, CONSTRAINT ck_consumable_effect CHECK (effect IN ('heal', 'buff', 'cleanse') AND power >= 0 AND power <= 100))"))
            conn.execute(text("INSERT INTO consumables VALUES ('old','Old','Custom','heal',73)"))
            conn.execute(text('CREATE TABLE owned_consumables (adventurer_id VARCHAR, consumable_slug VARCHAR REFERENCES consumables(slug), quantity INTEGER)'))
            conn.execute(text("INSERT INTO owned_consumables VALUES ('hero','old',7)"))
            upgrade(conn); upgrade(conn)
            assert conn.scalar(text("SELECT power FROM consumables WHERE slug='old'")) == 73
            assert conn.scalar(text('SELECT quantity FROM owned_consumables')) == 7
            assert conn.scalar(text('SELECT COUNT(*) FROM essence_definitions')) == 18
            assert not conn.execute(text('PRAGMA foreign_key_check')).all()
    finally:
        engine.dispose()
