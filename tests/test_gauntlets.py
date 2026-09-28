from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app.models import Adventurer, GauntletDefinition, GauntletRun, RecoveryContract, User
from app.gauntlets import composition


def heroes(client, size=1):
    result = [client.post('/api/adventurers', json={'name': 'Trial ' + uuid4().hex[:8]}) for _ in range(size)]
    assert all(r.status_code == 200 for r in result), [(r.status_code, r.text) for r in result]
    return [r.json()['id'] for r in result]


def start(client, ids, **extra):
    return client.post('/api/gauntlet-runs', json={'adventurer_ids': ids, **extra})


def resolve(client, encounter, action='attack'):
    for _ in range(250):
        if encounter['state'] != 'player_turn':
            return encounter
        response = client.post(f"/api/encounters/{encounter['id']}/actions", json={
            'actor_id': encounter['pending_actor_ids'][0], 'expected_turn': encounter['turn'], 'action': action})
        assert response.status_code == 200, response.text
        encounter = response.json()
    pytest.fail('Trial did not resolve')


def persisted(ids):
    with SessionLocal() as db:
        return [{k: deepcopy(getattr(db.get(Adventurer, UUID(i)), k)) for k in
                 ('health', 'is_alive', 'combat_cooldowns', 'combat_statuses', 'gold', 'experience', 'statistics')}
                for i in ids]


@pytest.mark.parametrize('size', [1, 2])
def test_start_victory_continue_failure_and_retry(client, size):
    ids = heroes(client, size)
    before = persisted(ids)
    response = start(client, ids)
    assert response.status_code == 201, response.text
    data = response.json()
    run_id = data['run']['id']
    assert data['run']['highest_stage_reached'] == 1
    assert data['run']['highest_stage_completed'] == 0
    assert len(data['encounter']['participants']) == size
    assert start(client, ids).status_code == 409
    won = resolve(client, data['encounter'])
    assert won['state'] == 'victory'
    assert won['gauntlet']['completed'] == 1
    assert won['quest']['can_continue']
    assert persisted(ids) == before
    advanced = client.post(f"/api/encounters/{won['id']}/continue", json={})
    assert advanced.status_code == 200, advanced.text
    next_battle = advanced.json()
    assert next_battle['gauntlet']['reached'] == 2
    assert next_battle['participants'][0]['hp'] == won['participants'][0]['hp']
    assert next_battle['enemies'][0]['max_hp'] > won['enemies'][0]['max_hp']
    assert client.post(f"/api/encounters/{won['id']}/continue", json={}).status_code == 409
    lost = resolve(client, next_battle, 'wait')
    assert lost['state'] == 'defeat'
    summary = client.get('/api/gauntlet-runs/' + run_id).json()
    assert summary['status'] == 'failed'
    assert summary['highest_stage_reached'] == 2
    assert summary['highest_stage_completed'] == summary['encounters_completed'] == 1
    assert summary['ended_at'] and summary['termination_reason']
    assert [e['state'] for e in summary['encounters']] == ['victory', 'defeat']
    assert summary['party_snapshot'][0]['hp'] == 100
    assert persisted(ids) == before
    assert client.post(f"/api/encounters/{lost['id']}/continue", json={}).status_code == 409
    assert client.get(f"/api/encounters/{lost['id']}").json()['combat_log']
    with SessionLocal() as db:
        assert db.scalar(select(RecoveryContract).where(RecoveryContract.source_run_id == UUID(run_id))) is None
    history = client.get('/api/gauntlet-runs').json()
    assert history['best_completed'] == 1 and history['best_reached'] == 2
    assert history['best_by_party'][0]['participant_ids'] == sorted(ids)
    retry = start(client, list(reversed(ids)))
    assert retry.status_code == 201, retry.text
    assert retry.json()['run']['party_key'] == data['run']['party_key']


def test_party_identity_is_order_independent():
    first, second = uuid4(), uuid4()
    assert composition([first, second]) == composition([second, first])
    assert composition([first]) != composition([first, second])


def test_nonzero_pre_run_health_and_cooldowns_survive_failure(client):
    ids = heroes(client)
    with SessionLocal.begin() as db:
        hero = db.get(Adventurer, UUID(ids[0]))
        hero.health = 17
        hero.combat_cooldowns = {'turns': {'power-strike': 3}, 'ready_at': {'test-cooldown': 2000000000}}
    before = persisted(ids)
    data = start(client, ids).json()
    participant = data['encounter']['participants'][0]
    assert participant['hp'] == 17
    assert participant['ability_ready_turns']['power-strike'] == 4
    assert participant['ability_ready_at']['test-cooldown'] == 2000000000
    assert resolve(client, data['encounter'], 'wait')['state'] == 'defeat'
    assert persisted(ids) == before
    retry = start(client, ids).json()['encounter']['participants'][0]
    assert retry['hp'] == 17
    assert retry['ability_ready_turns'] == participant['ability_ready_turns']
    assert retry['ability_ready_at'] == participant['ability_ready_at']


def test_stage_generation_is_deterministic_and_uses_frozen_pool(client):
    from app.gauntlets import departure_config, stage_entry
    with SessionLocal() as db:
        definition = db.get(GauntletDefinition, 'endless-road')
        first = departure_config(db, definition)
        second = departure_config(db, definition)
    # Each enemy has a fresh instance identity; balance/loadouts are deterministic.
    for left, right in zip(first['enemies'], second['enemies'], strict=True):
        left.pop('id')
        right.pop('id')
    assert first == second
    assert stage_entry(first, 3)['seeded_enemies'] == stage_entry(second, 3)['seeded_enemies']
    assert stage_entry(first, 3)['seeded_enemies'][0]['max_hp'] > stage_entry(first, 2)['seeded_enemies'][0]['max_hp']


def test_ownership_and_run_visibility(client):
    ids = heroes(client)
    data = start(client, ids).json()
    with TestClient(app) as other:
        assert other.post('/api/auth/register', json={'username': 'other_' + uuid4().hex[:12], 'password': 'test-password-1234'}).status_code == 201
        assert start(other, ids).status_code in (403, 404)
        assert other.get('/api/gauntlet-runs/' + data['run']['id']).status_code == 404
        assert other.get('/api/gauntlet-runs').json()['runs'] == []
        assert other.post(f"/api/encounters/{data['encounter']['id']}/actions", json={'actor_id': ids[0], 'expected_turn': 1, 'action': 'wait'}).status_code in (403, 404)
        assert other.post(f"/api/encounters/{data['encounter']['id']}/continue", json={}).status_code == 403


def test_tampering_empty_duplicate_dead_and_consumables(client):
    ids = heroes(client)
    assert start(client, []).status_code == 422
    assert start(client, ids * 2).status_code == 422
    for key, value in [('highest_stage_reached', 999), ('status', 'victory'), ('stats', {'power': 999})]:
        assert start(client, ids, **{key: value}).status_code == 422
    data = start(client, ids).json()
    url = f"/api/encounters/{data['encounter']['id']}"
    command = {'actor_id': ids[0], 'expected_turn': 1, 'action': 'wait'}
    # Shared combat commands preserve their existing ignore-extra-fields contract.
    # A forged damage/result never becomes authoritative.
    forged = client.post(url + '/actions', json={**command, 'damage': 99999, 'state': 'victory'}).json()
    assert forged['state'] == 'player_turn'
    assert forged['enemies'][0]['hp'] == data['encounter']['enemies'][0]['hp']
    assert forged['gauntlet']['reached'] == 1 and forged['gauntlet']['completed'] == 0
    assert client.post(url + '/continue', json={'highest_stage_reached': 999}).status_code == 409
    assert client.post(url + '/actions', json={'actor_id': ids[0], 'expected_turn': forged['turn'], 'consumable_slug': 'healing-potion'}).status_code == 409
    assert client.get('/api/gauntlet-runs/' + data['run']['id']).json()['highest_stage_completed'] == 0
    dead = heroes(client)
    with SessionLocal.begin() as db:
        db.get(Adventurer, UUID(dead[0])).is_alive = False
    assert start(client, dead).status_code == 409


def test_retirement_blocks_continue_and_releases_party(client):
    ids = heroes(client)
    data = start(client, ids).json()
    won = resolve(client, data['encounter'])
    url = f"/api/encounters/{won['id']}/continue"
    assert client.post(url, json={'choice': 'rest'}).status_code == 422
    assert client.post(url, json={'return_to_village': True}).status_code == 200
    summary = client.get('/api/gauntlet-runs/' + data['run']['id']).json()
    assert summary['status'] == 'retired' and summary['ended_at']
    assert client.post(url, json={}).status_code == 409
    assert start(client, ids).status_code == 201


def test_worldsmith_config_validation_limits_and_snapshots(client):
    ids = heroes(client, 2)
    user_id = UUID(client.get('/api/auth/me').json()['id'])
    with SessionLocal.begin() as db:
        db.get(User, user_id).account_type = 'developer'
    definitions = client.get('/api/abilities?inspect=true').json()['editor']['catalogs']['gauntlets']
    source = definitions['records'][0]
    slug = 'trial-' + uuid4().hex
    values = {**source['values'], 'slug': slug, 'name': slug,
              'settings': {**source['values']['settings'], 'max_party_size': 1}}
    create = {'catalog': 'gauntlets', 'key': {'slug': slug}, 'values': values, 'create': True}
    assert client.post('/api/catalog-editor', json={**create, 'values': {**values, 'settings': {**values['settings'], 'enemy_pool': ['missing']}}}).status_code == 422
    saved = client.post('/api/catalog-editor', json=create)
    assert saved.status_code == 200, saved.text
    assert start(client, ids, definition_slug=slug).status_code == 422
    result = start(client, ids[:1], definition_slug=slug).json()
    record = saved.json()['record']
    changed = {**record['values'], 'settings': {**record['values']['settings'], 'health_step_percent': 500}}
    assert client.post('/api/catalog-editor', json={'catalog': 'gauntlets', 'key': record['key'], 'values': changed, 'expected_revision': record['revision']}).status_code == 200
    with SessionLocal() as db:
        run = db.get(GauntletRun, UUID(result['run']['id']))
        assert run.config_snapshot['health_step_percent'] == source['values']['settings']['health_step_percent']
        assert run.config_snapshot['enemies'][0]['abilities']


def test_migration_is_idempotent():
    from app.database import engine
    from app.migrations.v030_gauntlets import upgrade
    with engine.begin() as conn:
        upgrade(conn)
        upgrade(conn)
    with SessionLocal() as db:
        assert db.get(GauntletDefinition, 'endless-road') is not None
