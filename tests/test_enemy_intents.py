from copy import deepcopy
from dataclasses import asdict
from random import Random
from uuid import UUID, uuid4

from app.combat import CombatAbility
from app.database import SessionLocal
from app.enemy_intents import plan_moves, preview_moves, execute_planned
from app.models import Encounter


def roster():
    players = [dict(id=str(i), name=f'Hero {i}', hp=100, max_hp=100) for i in range(3)]
    enemy = dict(id='enemy', name='Enemy', hp=100, max_hp=100, power=10,
                 abilities=[dict(ability=asdict(CombatAbility('sweep', 'Sweep', max_targets=2,
                                                              cooldown_turns=3)), priority=1, weight=1)])
    enemies = [enemy]
    plan_moves(players, enemies, 1, lambda _: Random(1), 100)
    return players, enemies


def test_preview_is_pure_matches_cast_and_caps_targets():
    players, enemies = roster()
    saved = deepcopy((players, enemies))
    preview = preview_moves(players, enemies, 1, lambda _: Random(1))['enemy']
    assert (players, enemies) == saved
    assert [t['id'] for t in preview['targets']] == ['0', '1']
    results = execute_planned(enemies[0], players, enemies, 1, Random(1))
    assert [r['amount'] for r in results] == [t['amount'] for t in preview['targets']] == [10, 10]
    assert players[2]['hp'] == 100
    assert enemies[0]['ability_ready_turns']['sweep'] == 4
    plan_moves(players, enemies, 2, lambda _: Random(1), 100)
    assert enemies[0]['intent']['ability'] is None


def test_defense_updates_prediction_without_retargeting():
    players, enemies = roster()
    plan = deepcopy(enemies[0]['intent'])
    players[0]['guarding'] = True
    preview = preview_moves(players, enemies, 1, lambda _: Random(1))['enemy']
    assert [t['amount'] for t in preview['targets']] == [6, 10]
    players[1]['hp'] = 0
    plan_moves(players, enemies, 1, lambda _: Random(99), 1000)
    assert enemies[0]['intent'] == plan
    results = execute_planned(enemies[0], players, enemies, 1, Random(1))
    assert [r['target_id'] for r in results] == ['0']
    assert players[2]['hp'] == 100
    enemies[0]['flinched'] = True
    assert preview_moves(players, enemies, 1, lambda _: Random(1))['enemy']['state'] == 'interrupted'


def test_api_persists_announced_move_and_guard_updates_preview(client):
    heroes = [client.post('/api/adventurers', json={'name': uuid4().hex}).json() for _ in range(2)]
    response = client.post('/api/encounters', json={'adventurer_ids': [h['id'] for h in heroes],
                                                  'enemy_slug': 'hobgoblin'})
    assert response.status_code == 201
    encounter = response.json()
    enemy = encounter['enemies'][0]
    assert enemy['next_move']['ability'] == 'sweeping_crush'
    assert len(enemy['next_move']['targets']) == 2
    url = f"/api/encounters/{encounter['id']}"
    for _ in range(2):
        assert client.get(url).json()['enemies'] == encounter['enemies']
    with SessionLocal() as db:
        assert db.get(Encounter, UUID(encounter['id'])).enemy_instances[0].state['intent'] == enemy['intent']
    first = client.post(url + '/actions', json={'actor_id': encounter['participants'][0]['id'],
                                              'expected_turn': 1, 'action': 'guard'}).json()
    assert first['enemies'][0]['intent'] == enemy['intent']
    preview = first['enemies'][0]['next_move']
    second = client.post(url + '/actions', json={'actor_id': encounter['participants'][1]['id'],
                                               'expected_turn': 1, 'action': 'wait'}).json()
    actual = [r for r in second['action_results'] if r['actor_id'] == enemy['id']]
    assert [(r['target_id'], r['amount']) for r in actual] == [(t['id'], t['amount']) for t in preview['targets']]
    assert second['turn'] == 2
    assert second['enemies'][0]['next_move']['ability'] != 'sweeping_crush'
