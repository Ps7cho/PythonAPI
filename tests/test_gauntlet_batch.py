from copy import deepcopy
from uuid import uuid4

import pytest

from scripts.gauntlet_batch import ApiError, commands, run_batch, validate


def adapter(client):
    def api(path, body=None):
        response = client.get('/api' + path) if body is None else client.post('/api' + path, json=body)
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.text)
        return response.json()
    return api


def create(client):
    response = client.post('/api/adventurers', json={'name': 'Batch ' + uuid4().hex[:8]})
    assert response.status_code == 200
    return response.json()['id']


def case(ids, **kwargs):
    return {'name': 'test', 'adventurer_ids': ids, **kwargs}


def test_unattended_solo_team_repeats_preserve_characters(client):
    ids = [create(client), create(client)]
    before = [client.get('/api/adventurers/' + i).json() for i in ids]
    checkpoints = []
    report = run_batch(adapter(client), {'cases': [
        case(ids[:1], name='solo', ability_priority=['attack'], repeats=2),
        case(ids, name='pair', ability_priority=['power_strike', 'attack']),
    ]}, lambda r: checkpoints.append(deepcopy(r)), delay=0)
    assert report['complete']
    assert len(report['runs']) == 3
    assert all(r['outcome'] == 'failed' and r['completed'] > 0 and r['reached'] == r['completed'] + 1 for r in report['runs'])
    assert len({r['run_id'] for r in report['runs']}) == 3
    assert [r['runs'] for r in report['comparison']] == [2, 1]
    assert all(r['mean_completed_uncapped'] > 0 and r['capped_runs'] == 0 for r in report['comparison'])
    assert len(checkpoints) > 6
    for identifier, original in zip(ids, before):
        after = client.get('/api/adventurers/' + identifier).json()
        for field in ('health', 'is_alive', 'gold', 'experience', 'statistics'):
            assert after.get(field) == original.get(field)


def test_stage_cap_retires_and_releases_for_next_repeat(client):
    identifier = create(client)
    report = run_batch(adapter(client), {'cases': [case([identifier], max_stages=1, repeats=2)]}, delay=0)
    assert report['complete']
    assert all(r['outcome'] == 'capped_retired' and r['summary']['status'] == 'retired' for r in report['runs'])
    assert all(r['completed'] == 1 for r in report['runs'])
    assert report['comparison'][0]['capped_runs'] == 2
    assert report['comparison'][0]['mean_completed_uncapped'] is None


def test_mid_battle_limit_stops_batch_and_preserves_resume_ids(client):
    identifier = create(client)
    report = run_batch(adapter(client), {'cases': [case([identifier], max_actions=1, repeats=2)]}, delay=0)
    assert not report['complete'] and len(report['runs']) == 1
    row = report['runs'][0]
    assert row['outcome'] == 'paused_limit' and row['actions'] == 1
    assert row['summary']['status'] == 'active'
    assert client.get('/api/encounters/' + row['encounter_id']).status_code == 200


def test_preflight_rejects_unauthorized_party_before_start(client):
    owned = create(client)
    with pytest.raises(ValueError, match='unauthorized'):
        run_batch(adapter(client), {'cases': [case([owned]), case([str(uuid4())], name='foreign')]}, delay=0)
    assert client.get('/api/gauntlet-runs').json()['runs'] == []


def test_network_error_never_retries_write(client):
    identifier = create(client)
    real, writes = adapter(client), []
    def broken(path, body=None):
        if path.endswith('/actions'):
            writes.append(body)
            raise TimeoutError('ambiguous response')
        return real(path, body)
    report = run_batch(broken, {'cases': [case([identifier], repeats=2)]}, delay=0)
    assert not report['complete'] and len(report['runs']) == len(writes) == 1
    assert report['runs'][0]['outcome'] == 'error'
    assert report['runs'][0]['run_id']


def test_cooldown_priority_weapon_target_and_wait_policy():
    actor = {'id': 'hero', 'hp': 20, 'max_hp': 100, 'acted': False,
             'equipped_abilities': [
                 {'slug': 'p', 'catalog_slug': 'power_strike', 'effect': 'damage', 'target_type': 'enemy'},
                 {'slug': 'h', 'catalog_slug': 'heal', 'effect': 'heal', 'target_type': 'ally'},
                 {'slug': 'a', 'catalog_slug': 'attack', 'effect': 'damage', 'target_type': 'enemy', 'requires_weapon': True, 'allowed_weapon_tags': ['melee']}],
             'ability_ready_turns': {'p': 4},
             'weapons': [{'id': 'weak', 'base_damage': 10, 'tags': ['melee']}, {'id': 'strong', 'base_damage': 20, 'tags': ['melee']}]}
    encounter = {'turn': 2, 'participants': [actor], 'enemies': [{'id': 'enemy', 'hp': 40}]}
    policy = {'ability_priority': ['power_strike', 'heal', 'attack'], 'heal_below': 0.5}
    choices = list(commands(encounter, policy))
    assert choices[0]['ability_id'] == 'h' and choices[0]['target_id'] == 'hero'
    assert choices[1]['weapon_id'] == 'strong' and choices[1]['target_id'] == 'enemy'
    assert choices[-1]['action'] == 'wait'
    actor['hp'] = 100
    assert next(commands(encounter, policy))['ability_id'] == 'a'


@pytest.mark.parametrize('extra', [{'repeats': 0}, {'max_actions': True}, {'ability_priority': []}, {'heal_below': 2}, {'damage': 999}])
def test_bad_configuration_is_rejected(extra):
    with pytest.raises(ValueError):
        validate({'cases': [case([str(uuid4())], **extra)]})
