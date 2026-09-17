from copy import deepcopy
from dataclasses import asdict
from random import Random

import pytest

from app.afflictions import add, cleanse, status
from app.combat import CombatAbility, InvalidCombatAction, execute_cast, tick_statuses
from app.status_content import GRAMMAR_EFFECTS
from tests.test_status_effects import fighter

BURN, HOLY = [{**item, **item.get('rules', {})} for item in GRAMMAR_EFFECTS]


def cast(actor, targets, *ops, **kw):
    return execute_cast(actor, CombatAbility('test', 'Test', effect='affliction',
                        max_targets=None, affliction_ops=list(ops), **kw), targets, turn=1)


def test_apply_amplify_and_expiry():
    actor, enemy = fighter(), fighter('e', 'enemies')
    cast(actor, [enemy], dict(op='apply', affliction='burn', stacks=99, definition=BURN),
         dict(op='amplify', affliction='burn', power=2))
    assert status(enemy, 'burn')['stacks'] == 5
    assert tick_statuses([enemy], turn=1)[0]['amount'] == 20
    add(enemy, BURN, actor['id'])
    assert status(enemy, 'burn')['amplification'] == 2
    for turn in range(2, 5):
        tick_statuses([enemy], turn=turn)
    assert enemy['statuses'] == []


def test_copy_move_caps_and_immunity():
    actor, source, target = fighter(), fighter('s', 'enemies'), fighter('t', 'enemies')
    add(source, BURN, 'p', 4)
    cast(actor, [source, target], dict(op='spread', affliction='burn', stacks=3))
    assert status(source, 'burn')['stacks'] == 4
    assert status(target, 'burn')['stacks'] == 3
    cast(actor, [source, target], dict(op='spread', affliction='burn', stacks=3, move=True))
    assert status(source, 'burn')['stacks'] == 2
    assert status(target, 'burn')['stacks'] == 5
    target['status_resistances'] = {'burn': 100}
    target['statuses'] = []
    cast(actor, [source, target], dict(op='spread', affliction='burn', stacks=2, move=True))
    assert status(source, 'burn')['stacks'] == 2
    assert target['statuses'] == []


def test_consume_detonate_exploit_and_guard():
    actor, enemy = fighter(), fighter('e', 'enemies', guarding=True, guard_remaining=4, guard_turn=1)
    actor['hp'] = 50
    add(enemy, BURN, 'p', 4)
    cast(actor, [enemy], dict(op='exploit', affliction='burn', stacks=2, power=3))
    assert enemy['hp'] == 98
    assert status(enemy, 'burn')['stacks'] == 4
    cast(actor, [enemy], dict(op='consume', affliction='burn', stacks=2, effect='heal', power=5))
    assert actor['hp'] == 60
    cast(actor, [enemy], dict(op='detonate', affliction='burn', stacks=2, power=7))
    assert enemy['hp'] == 84 and enemy['statuses'] == []


def test_conversion_resource_and_beneficial_cleanse():
    actor, enemy = fighter(), fighter('e', 'enemies')
    add(enemy, BURN, 'p', 3)
    cast(actor, [enemy], dict(op='convert', affliction='burn', stacks=2, into='holy', definition=HOLY))
    assert status(enemy, 'holy')['stacks'] == 2
    assert cleanse(enemy) == 1
    assert status(enemy, 'holy')['stacks'] == 2
    assert tick_statuses([enemy], turn=1) == [] and enemy['hp'] == 100
    cast(actor, [enemy], dict(op='convert', affliction='holy', stacks=1, resource='zeal', power=3))
    assert actor['resources'] == {'zeal': 3}
    assert status(enemy, 'holy')['stacks'] == 1


def test_preserve_blocks_removal_and_pauses_expiry_not_damage():
    actor, enemy = fighter(), fighter('e', 'enemies')
    add(enemy, BURN, 'p', 3)
    cast(actor, [enemy], dict(op='preserve', affliction='burn', rounds=2),
         dict(op='cleanse', affliction='burn', stacks=3),
         dict(op='detonate', affliction='burn', stacks=3),
         dict(op='convert', affliction='burn', stacks=3, resource='zeal'))
    assert status(enemy, 'burn')['stacks'] == 3
    assert enemy['hp'] == 100 and not actor.get('resources')
    for turn in (1, 2):
        assert tick_statuses([enemy], turn=turn)[0]['amount'] == 6
        assert status(enemy, 'burn')['remaining_rounds'] == 3
    cast(actor, [enemy], dict(op='cleanse', affliction='burn', stacks=2))
    assert status(enemy, 'burn')['stacks'] == 1


def test_threshold_crossing_only_once_and_rearms_below_threshold():
    actor, enemy = fighter(), fighter('e', 'enemies')
    apply = dict(op='apply', affliction='burn', stacks=2, definition=BURN)
    trigger = dict(op='trigger', affliction='burn', threshold=3, power=12)
    cast(actor, [enemy], apply, trigger)
    assert enemy['hp'] == 100
    cast(actor, [enemy], apply, trigger, trigger)
    assert enemy['hp'] == 88
    cast(actor, [enemy], apply, trigger)
    assert enemy['hp'] == 88
    cast(actor, [enemy], dict(op='consume', affliction='burn', stacks=4, power=0), apply, trigger)
    assert enemy['hp'] == 76


def test_invalid_pipeline_rolls_back_everything():
    actor, enemy = fighter(), fighter('e', 'enemies')
    before = deepcopy([actor, enemy])
    with pytest.raises(InvalidCombatAction):
        cast(actor, [enemy], dict(op='apply', affliction='burn', definition=BURN),
             dict(op='spread', affliction='burn'), cooldown_turns=3)
    assert [actor, enemy] == before
    with pytest.raises(InvalidCombatAction):
        cast(actor, [enemy], dict(op='apply', affliction='burn', stacks=-1, definition=BURN))
    assert [actor, enemy] == before


def test_dodged_spread_does_not_change_source():
    actor, source, target = fighter(), fighter('s', 'enemies'), fighter('t', 'enemies')
    source['evasion'] = {'chance_percent': 100, 'until_turn': 3}
    add(source, BURN, 'p', 2)
    execute_cast(actor, CombatAbility('hit', 'Hit', damage=1, max_targets=2,
                 affliction_ops=[dict(op='spread', affliction='burn', stacks=2)]),
                 [source, target], turn=1)
    assert not target.get('statuses')


def test_preview_aggregates_direct_and_affliction_damage():
    from app.enemy_intents import preview_moves, execute_planned
    player, enemy = fighter(), fighter('e', 'enemies')
    add(player, BURN, 'e', 2)
    ability = CombatAbility('burst', 'Burst', damage=3,
              affliction_ops=[dict(op='detonate', affliction='burn', stacks=2, power=4)])
    enemy['intent'] = dict(turn=1, ability=asdict(ability), target_ids=['p'])
    preview = preview_moves([player], [enemy], 1, lambda _: Random(1))
    assert player['hp'] == 100
    assert preview['e']['targets'][0]['amount'] == 11
    execute_planned(enemy, [player], [enemy], 1, Random(1))
    assert player['hp'] == 89


def test_catalog_loadout_and_api_execution(client):
    catalog = client.get('/api/afflictions')
    assert catalog.status_code == 200
    assert {'bleed', 'poison', 'sin', 'necrosis', 'burn', 'holy'} <= {s['slug'] for s in catalog.json()}
    assert client.get('/api/afflictions/grammar').status_code == 200
    hero = client.post('/api/adventurers', json={'name': 'Grammar tester'}).json()['id']
    abilities = client.get('/api/adventurers/' + hero).json()['abilities']
    flash = next(a for a in abilities if a['name'] == 'Flashpoint')
    assert client.post('/api/adventurers/' + hero + '/loadout',
                       json={'ability_ids': [flash['id']]}).status_code == 200
    encounter = client.post('/api/encounters', json={'adventurer_ids': [hero]}).json()
    response = client.post('/api/encounters/' + encounter['id'] + '/actions', json={
        'actor_id': hero, 'expected_turn': 1, 'ability_id': flash['id']})
    assert response.status_code == 200, response.text
    state = response.json()
    assert state['enemies'][0]['statuses'][0]['slug'] == 'burn'
    assert any(r.get('interaction') == 'apply' for r in state['action_results'])
    assert client.get('/api/encounters/' + encounter['id']).json()['enemies'] == state['enemies']
