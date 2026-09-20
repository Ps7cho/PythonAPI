from copy import deepcopy
from uuid import uuid4

import pytest

from app.combat import CombatAbility, InvalidCombatAction, execute_action

# Executor fixtures; production definitions are loaded from the database.
POWER_STRIKE = CombatAbility("power_strike", "Power Strike", damage=16, cooldown_turns=3)
GUARD = CombatAbility("guard", "Guard", effect="guard", target_type="self")
DIRTY_STAB = CombatAbility("dirty_stab", "Dirty Stab")


def fighter(name, team, hp=100):
    return {"id": str(uuid4()), "name": name, "team": team, "hp": hp, "power": 6}


def test_same_executor_handles_players_enemies_and_guard():
    player, goblin = fighter('Player', 'adventurers'), fighter('Goblin', 'monsters')
    result = execute_action(player, POWER_STRIKE, goblin, turn=1)
    assert result['amount'] == 16
    assert goblin['hp'] == 84
    execute_action(player, GUARD, player, turn=2)
    result = execute_action(goblin, DIRTY_STAB, player, turn=2)
    assert result['amount'] == 2
    assert result['ability'] == 'dirty_stab'
    assert player['hp'] == 98
    # The same ability works for either team without player-specific effect logic.
    assert execute_action(goblin, POWER_STRIKE, player, turn=3)['amount'] == 16


def test_rejections_do_not_mutate_state_and_old_cooldowns_survive():
    player, goblin = fighter('Player', 'adventurers'), fighter('Goblin', 'monsters')
    player['power_ready_turn'] = 4
    before = deepcopy((player, goblin))
    with pytest.raises(InvalidCombatAction):
        execute_action(player, POWER_STRIKE, goblin, turn=3)
    assert (player, goblin) == before
    with pytest.raises(InvalidCombatAction):
        execute_action(player, GUARD, goblin, turn=4)
    assert (player, goblin) == before
    execute_action(player, POWER_STRIKE, goblin, turn=4)
    assert player['ability_ready_turns']['power_strike'] == 7
    with pytest.raises(InvalidCombatAction):
        execute_action(player, DIRTY_STAB, player, turn=5)


def test_guard_reduces_every_direct_hit_by_sixty_percent_for_the_round():
    player, enemy = fighter('Player', 'adventurers'), fighter('Enemy', 'monsters')
    execute_action(player, GUARD, player, turn=1)
    first = execute_action(enemy, CombatAbility('first', 'First hit', damage=10), player, turn=1)
    second = execute_action(enemy, CombatAbility('second', 'Second hit', damage=10), player, turn=1)
    assert [first['amount'], second['amount']] == [4, 4]
    assert player['hp'] == 92
    assert 'Guard reduces damage by 60%' in first['message']
    execute_action(player, GUARD, player, turn=2)
    assert execute_action(enemy, CombatAbility('hit', 'Hit', damage=10), player, turn=3)['amount'] == 10


def test_damage_result_reports_actual_hp_loss_and_rejects_dead_target():
    player, goblin = fighter('Player', 'adventurers'), fighter('Goblin', 'monsters', hp=3)
    result = execute_action(player, POWER_STRIKE, goblin, turn=1)
    assert result['amount'] == 3
    assert result['target_hp'] == 0
    with pytest.raises(InvalidCombatAction):
        execute_action(player, POWER_STRIKE, goblin, turn=4)


def test_player_and_goblin_results_are_returned_and_persisted(client):
    from uuid import UUID
    from sqlalchemy import select
    from app.database import SessionLocal
    from app.models import Encounter, GameEvent

    hero = client.post('/api/adventurers', json={'name': 'Shared executor test'}).json()
    encounter = client.post('/api/encounters', json={
        'adventurer_ids': [hero['id']], 'enemy_slug': 'goblin',
    }).json()
    action = {'actor_id': hero['id'], 'expected_turn': 1, 'action': 'power_strike',
              'target_id': encounter['enemies'][0]['id']}
    response = client.post(f"/api/encounters/{encounter['id']}/actions", json={**action, 'target_id': str(uuid4())})
    assert response.status_code == 400
    assert client.get(f"/api/encounters/{encounter['id']}").json() == encounter
    response = client.post(f"/api/encounters/{encounter['id']}/actions", json=action)
    assert response.status_code == 200
    results = response.json()['action_results']
    power = next(a for a in client.get('/api/adventurers/' + hero['id']).json()['abilities'] if a['name'] == 'Power Strike')
    assert [r['ability'] for r in results] == [power['id'], 'dirty_stab']
    assert results[0]['target_id'] == results[1]['actor_id']
    assert results[1]['target_id'] == hero['id']
    with SessionLocal() as db:
        stored = db.get(Encounter, UUID(encounter['id']))
        event = db.scalar(select(GameEvent).where(GameEvent.quest_run_id == stored.quest_run_id,
                                                GameEvent.event_type == 'combat_action'))
        assert event.payload['action_results'] == results
