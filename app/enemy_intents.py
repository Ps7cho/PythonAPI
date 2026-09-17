"""Round plans are chosen once; previews simulate the shared executor on copies."""
from copy import deepcopy
from dataclasses import asdict, replace

from app.combat import CombatAbility, InvalidCombatAction, execute_cast, select_targets, validate_weapon


def plan_moves(participants, enemies, turn, rng_for, now):
    for actor in participants:
        actor['team'] = 'adventurers'
    for enemy in enemies:
        enemy['team'] = 'monsters'
    for enemy in enemies:
        if enemy.get('intent', {}).get('turn') == turn:
            continue
        intent = {'turn': turn, 'ability': None, 'target_ids': []}
        enemy['intent'] = intent
        if enemy['hp'] <= 0:
            continue
        candidates = []
        for entry in enemy.get('abilities', []):
            spec = entry['ability']
            if (entry['weight'] <= 0 or turn < enemy.get('ability_ready_turns', {}).get(spec['slug'], 1)
                    or now < enemy.get('ability_ready_at', {}).get(spec['slug'], 0)):
                continue
            ability = CombatAbility(**spec)
            if 'max_targets' not in spec and (ability.effect == 'damage' or ability.target_type == 'party'):
                ability = replace(ability, max_targets=None)
            try:
                validate_weapon(enemy, ability)
                targets = select_targets(enemy, ability, participants + enemies)
                if any(op.get('op') == 'spread' for op in ability.affliction_ops) and len(targets) < 2:
                    continue
                if ability.effect == 'heal' and not any(t['hp'] < t['max_hp'] for t in targets):
                    continue
            except InvalidCombatAction:
                continue
            candidates.append((entry, ability, targets))
        if candidates:
            priority = max(entry['priority'] for entry, _, _ in candidates)
            candidates = [c for c in candidates if c[0]['priority'] == priority]
            _, ability, targets = rng_for(enemy).choices(candidates, weights=[c[0]['weight'] for c in candidates])[0]
            intent.update(ability=asdict(ability), target_ids=[t['id'] for t in targets])


def execute_planned(enemy, participants, enemies, turn, rng):
    intent = enemy.get('intent', {})
    if intent.get('turn') != turn or not intent.get('ability'):
        return []
    ability = CombatAbility(**intent['ability'])
    by_id = {t['id']: t for t in participants + enemies if t['hp'] > 0}
    # Never redirect an announced attack onto an unannounced replacement target.
    ids = [key for key in intent['target_ids'] if key in by_id]
    if any(op.get('op') == 'spread' for op in ability.affliction_ops):
        if len(ids) < 2 or ids[0] != intent['target_ids'][0]:
            return []
    if not ids:
        return []
    targets = select_targets(enemy, ability, participants + enemies, ids)
    return execute_cast(enemy, ability, targets, turn=turn, rng=rng)


def preview_moves(participants, enemies, turn, rng_for):
    participants, enemies = deepcopy(participants), deepcopy(enemies)
    previews = {}
    names = {c['id']: c['name'] for c in participants + enemies}
    for enemy in enemies:
        intent = enemy.get('intent', {})
        ability = intent.get('ability')
        state = 'planned' if ability else 'waiting'
        results = []
        if enemy['hp'] <= 0:
            state = 'defeated'
        elif enemy.get('flinched'):
            state = 'interrupted'
        elif ability:
            try:
                results = execute_planned(enemy, participants, enemies, turn, rng_for(enemy))
                if not results:
                    state = 'cancelled'
            except InvalidCombatAction:
                state = 'cancelled'
        damage_results = [r for r in results if r['effect'] == 'damage']
        visible = damage_results or [r for r in results if r['effect'] != 'affliction'] or results[:1]
        projected = {}
        for result in visible:
            key = result['target_id']
            target = projected.setdefault(key, {'id': key, 'name': names[key], 'amount': 0, 'critical': False, 'dodged': False})
            target['amount'] += result['amount']
            target['critical'] |= result['critical']
            target['dodged'] |= result['dodged']
        previews[enemy['id']] = {
            'turn': turn, 'state': state, 'ability': ability['slug'] if ability else None,
            'name': ability['name'] if ability else 'Wait',
            'effect': 'damage' if damage_results else ability['effect'] if ability else 'wait',
            'targets': list(projected.values()),
            'damage_basis': 'projected_direct_damage',
        }
    return previews
