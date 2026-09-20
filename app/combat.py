"""Combat effects shared by player commands and server-controlled enemy actions."""
from dataclasses import dataclass, field
from copy import deepcopy
from typing import Literal
from time import time
from random import Random

GUARD_REDUCTION_PERCENT = 60


class InvalidCombatAction(ValueError):
    pass


@dataclass(frozen=True)
class CombatAbility:
    slug: str
    name: str
    effect: Literal["damage", "guard", "heal", "buff", "shield", "cleanse", "evade", "affliction"] = "damage"
    damage: int | None = None
    cooldown_turns: int = 0
    cooldown_seconds: int = 0
    target_type: str = "enemy"
    catalog_slug: str | None = None
    description: str = ""
    damage_multiplier: float | None = None
    max_targets: int | None = 1
    requires_weapon: bool = False
    allowed_weapon_tags: list[str] = field(default_factory=list)
    status_effect: dict | None = None
    affliction_ops: list[dict] = field(default_factory=list)
    scales_with_attributes: bool = True
    effect_chain: list[dict] = field(default_factory=list)
    duration_turns: int | None = None
    guard_percent: int = GUARD_REDUCTION_PERCENT
    cooldown_unit_seconds: int = 1



def validate_weapon(actor: dict, ability: CombatAbility) -> None:
    if not ability.requires_weapon:
        return
    weapon = actor.get("equipped_weapon")
    if not weapon:
        raise InvalidCombatAction(f"{ability.name} requires an equipped weapon.")
    if ability.allowed_weapon_tags and not set(ability.allowed_weapon_tags).intersection(weapon.get("tags", [])):
        raise InvalidCombatAction(f"{ability.name} requires one of these weapon tags: {', '.join(ability.allowed_weapon_tags)}.")
    damage = weapon.get("base_damage")
    if not isinstance(damage, (int, float)) or isinstance(damage, bool) or damage < 0:
        raise InvalidCombatAction("Invalid weapon damage.")


def _execute_action(actor: dict, ability: CombatAbility, target: dict, *, turn: int, charge_cooldown: bool, rng, derived_damage=False) -> dict:
    """Validate and apply one effect to encounter-local state; never save or advance turns."""
    if actor["hp"] <= 0 or target["hp"] <= 0:
        raise InvalidCombatAction("Actor and target must be alive.")
    if ability.effect not in ("damage", "guard", "heal", "buff", "shield", "cleanse", "evade", "affliction"):
        raise InvalidCombatAction("Unsupported ability effect.")
    if ability.target_type not in ("enemy", "self", "party", "ally"):
        raise InvalidCombatAction("Unsupported ability target.")
    if ability.target_type == "self" and actor["id"] != target["id"]:
        raise InvalidCombatAction("This ability must target its actor.")
    if ability.effect == "guard":
        if actor["id"] != target["id"]:
            raise InvalidCombatAction("Guard must target its actor.")
    elif ability.effect == 'affliction':
        select_targets(actor, ability, [actor, target], [target['id']])
    elif ability.effect == "damage" and actor["team"] == target["team"]:
        raise InvalidCombatAction("Damage abilities must target an opponent.")
    elif ability.effect != "damage" and actor["team"] != target["team"]:
        raise InvalidCombatAction("Support abilities must target an ally.")

    validate_weapon(actor, ability)

    ready_turns = dict(actor.get("ability_ready_turns", {}))
    ready_at = ready_turns.get(ability.slug, 1)
    # Retain cooldowns in encounters saved before the shared executor existed.
    if (ability.catalog_slug or ability.slug) == "power_strike":
        ready_at = max(ready_at, actor.get("power_ready_turn", 1))
    if turn < ready_at:
        raise InvalidCombatAction(f"{ability.name} is still on turn cooldown.")
    if time() < actor.get('ability_ready_at', {}).get(ability.slug, 0):
        raise InvalidCombatAction(f"{ability.name} is still on cooldown.")

    critical = flinch = dodged = False
    if ability.effect == "guard":
        target["guarding"] = True
        target['guard_reduction_percent'] = ability.guard_percent
        target.pop('guard_power', None)
        target.pop('guard_remaining', None)
        target['guard_turn'] = turn
        amount = 0
        message = f"{actor['name']} uses {ability.name}, reducing incoming direct damage by {ability.guard_percent}% this round."
    elif ability.effect == 'evade':
        if actor['id'] != target['id']:
            raise InvalidCombatAction('Evasion must target its actor.')
        amount = min(100, max(0, ability.damage or 0))
        target['evasion'] = {'chance_percent': amount, 'until_turn': turn + (ability.duration_turns or 2)}
        message = f"{actor['name']} uses {ability.name}, preparing to evade the next direct attack."
    elif ability.effect == 'cleanse':
        from app.afflictions import cleanse
        amount = cleanse(target)
        message = f"{actor['name']} uses {ability.name} on {target['name']}, cleansing {amount} statuses."
    elif ability.effect == 'affliction':
        amount = 0
        message = f"{actor['name']} uses {ability.name} on {target['name']}."
    elif ability.effect == 'heal':
        healing_bonus = actor.get('derived_stats', {}).get('healing_bonus_percent', 0) if ability.scales_with_attributes else 0
        amount = min(target['max_hp'] - target['hp'], max(0, round(target['max_hp'] * (ability.damage or 0) / 100 * (1 + healing_bonus / 100))))
        target['hp'] += amount
        message = f"{actor['name']} uses {ability.name} on {target['name']}, restoring {amount} HP."
    elif ability.effect in ('buff', 'shield'):
        amount = ability.damage or 0
        target[ability.effect] = {'power': amount, 'until_turn': turn + (ability.duration_turns or 3)}
        message = f"{actor['name']} uses {ability.name} on {target['name']}."
    else:
        base_damage = ability.damage if ability.damage is not None else actor["power"]
        if ability.requires_weapon:
            base_damage = round(actor["equipped_weapon"]["base_damage"] *
                                (ability.damage_multiplier if ability.damage_multiplier is not None else 1.0))
        elif ability.damage_multiplier is not None:
            base_damage = round(actor["power"] * ability.damage_multiplier)
        stats = actor.get('derived_stats', {})
        bonus = stats.get('weapon_bonus_percent' if ability.requires_weapon else 'spell_bonus_percent', 0) if ability.scales_with_attributes else 0
        base_damage = round(base_damage * (1 + bonus / 100))
        buff = actor.get('buff', {})
        if not derived_damage and turn < buff.get('until_turn', 0):
            base_damage = round(base_damage * (1 + buff['power'] / 100))
        evasion = target.pop('evasion', {})
        dodged = turn < evasion.get('until_turn', 0) and roll_chance(evasion.get('chance_percent', 0), rng)
        if dodged:
            amount = 0
            message = f"{target['name']} evades {actor['name']}'s {ability.name}!"
        else:
            critical = not derived_damage and roll_chance(stats.get('critical_chance_percent', 0), rng)
            if critical:
                base_damage = round(base_damage * 1.5)
            guard_percent = target.get('guard_reduction_percent', 0) if target.get('guarding') and target.get('guard_turn', turn) == turn else 0
            guard_before = target.get('guard_remaining', target.get('guard_power', 0)) if target.get('guarding') and not guard_percent else 0
            amount = resolve_damage(target, base_damage, turn=turn)
            blocked = guard_before - target.get('guard_remaining', guard_before)
            if amount > 0 and target['hp'] > 0 and not target.get('flinched') and turn >= target.get('flinch_immune_until', 0):
                flinch = roll_chance(stats.get('flinch_chance_percent', 0), rng)
                if flinch:
                    target['flinched'] = True
            message = f"{actor['name']} uses {ability.name} on {target['name']} for {amount}."
            if guard_percent:
                message += f" Guard reduces damage by {guard_percent}%."
            elif blocked:
                message += f" Guard blocks {blocked}; {target['guard_remaining']} block remains."

    if critical:
        message += ' Critical strike!'
    if flinch:
        message += f" {target['name']} flinches!"
    status_result = None
    if ability.status_effect and ability.effect == 'damage' and target['hp'] > 0 and not dodged:
        status_result = apply_status(target, ability.status_effect, actor['id'])
        message += ' ' + status_result

    if charge_cooldown and ability.cooldown_turns:
        ready_turns[ability.slug] = turn + ability.cooldown_turns
        actor["ability_ready_turns"] = ready_turns
        if (ability.catalog_slug or ability.slug) == "power_strike":
            actor["power_ready_turn"] = ready_turns[ability.slug]
    if charge_cooldown and ability.cooldown_seconds:
        actor['ability_ready_at'] = {**actor.get('ability_ready_at', {}), ability.slug: time() + ability.cooldown_seconds}

    return {"actor_id": actor["id"], "ability": ability.slug, "target_id": target["id"],
            "effect": ability.effect, "amount": amount, "target_hp": target["hp"],
            "turn": turn, "message": message, "critical": critical, "flinch": flinch, "dodged": dodged}


def select_targets(actor, ability, combatants, target_ids=None, *, turn=None):
    """Resolve legal living targets; explicit lists are exact, never silently truncated."""
    if turn is not None:
        from app.ability_design import effective_ability
        ability = effective_ability(actor, ability, turn)
    if ability.max_targets is not None and ability.max_targets < 1:
        raise InvalidCombatAction("Ability target limit must be positive.")
    if ability.target_type == "self":
        candidates = [actor] if actor["hp"] > 0 else []
    elif ability.target_type == "enemy":
        candidates = [c for c in combatants if c["hp"] > 0 and c["team"] != actor["team"]]
    elif ability.target_type in ("ally", "party"):
        candidates = [c for c in combatants if c["hp"] > 0 and c["team"] == actor["team"]]
    else:
        raise InvalidCombatAction("Unsupported ability target.")
    if target_ids is not None:
        if not target_ids or len(set(target_ids)) != len(target_ids):
            raise InvalidCombatAction("Targets must be nonempty and unique.")
        by_id = {c["id"]: c for c in candidates}
        if any(i not in by_id for i in target_ids):
            raise InvalidCombatAction("Invalid ability target.")
        targets = [by_id[i] for i in target_ids]
        if ability.max_targets is not None and len(targets) > ability.max_targets:
            raise InvalidCombatAction("Too many ability targets.")
    else:
        targets = candidates[:ability.max_targets] if ability.max_targets is not None else candidates
    if not targets:
        raise InvalidCombatAction("No living ability targets.")
    return targets


def execute_cast(actor: dict, ability: CombatAbility, targets: list[dict], *, turn: int, rng=None, combatants=None) -> list[dict]:
    """Resolve the whole cast on copies, publishing state only if every effect succeeds."""
    from app.ability_design import effective_ability, validate_chain
    ability = effective_ability(actor, ability, turn)
    if not targets or len({t['id'] for t in targets}) != len(targets):
        raise InvalidCombatAction("Targets must be nonempty and unique.")
    if ability.max_targets is not None and (ability.max_targets < 1 or len(targets) > ability.max_targets):
        raise InvalidCombatAction("Too many ability targets.")
    rng = rng if rng is not None else Random()
    try:
        chain = validate_chain(ability.effect_chain, ability.target_type)
    except ValueError as exc:
        raise InvalidCombatAction(str(exc)) from exc
    originals = {c['id']: c for c in [*(combatants or []), actor, *targets]}
    copies = deepcopy(originals)
    before = {key: {s['slug']: s['stacks'] for s in value.get('statuses', [])} for key, value in copies.items()}
    results = [_execute_action(copies[actor['id']], ability, copies[target['id']], turn=turn,
                               charge_cooldown=index == len(targets) - 1, rng=rng)
               for index, target in enumerate(targets)]
    if ability.affliction_ops:
        from app.afflictions import execute
        dodged = {r['target_id'] for r in results if r['dodged']}
        results.extend(execute(copies[actor['id']], ability, [copies[t['id']] for t in targets],
                               turn=turn, before=before, dodged=dodged))
    if chain:
        results.extend(execute_effect_chain(copies[actor['id']], ability, chain, copies,
                                            [t['id'] for t in targets], results, turn, rng))
    for key, original in originals.items():
        original.clear()
        original.update(copies[key])
    return results


def execute_effect_chain(actor, ability, chain, combatants, target_ids, primary, turn, rng):
    """Bounded sequential steps consume prior actual outcomes; never recurse."""
    primary_amount = sum(r['amount'] for r in primary)
    damage_dealt = sum(r['amount'] for r in primary if r['effect'] == 'damage')
    hit = any(r['effect'] == 'damage' and not r.get('dodged') for r in primary)
    killed = any(r['effect'] == 'damage' and r['amount'] > 0 and r['target_hp'] == 0 for r in primary)
    previous = primary_amount
    results = []
    for step in chain:
        enabled = {'always': True, 'on_hit': hit, 'on_damage': damage_dealt > 0, 'on_kill': killed}[step['when']]
        if not enabled or actor['hp'] <= 0:
            previous = 0
            continue
        recipients = [c for c in combatants.values() if c['hp'] > 0 and (
            c['id'] == actor['id'] if step['recipient'] == 'self' else
            c['id'] in target_ids if step['recipient'] == 'targets' else
            c['team'] == actor['team'] if step['recipient'] == 'party' else c['team'] != actor['team'])]
        total = max(0, round(step['value'] if step['source'] == 'fixed' else
                            {'primary': primary_amount, 'damage_dealt': damage_dealt, 'previous': previous}[step['source']] * step['value'] / 100))
        allocations = [total] * len(recipients)
        if step['split'] and recipients:
            quotient, remainder = divmod(total, len(recipients))
            allocations = [quotient + (i < remainder) for i in range(len(recipients))]
        previous = 0
        for target, amount in zip(recipients, allocations):
            effect = step['effect']
            if effect in ('heal', 'resource') and target['team'] != actor['team']:
                raise InvalidCombatAction('Healing and resources must target allies.')
            result = dict(actor_id=actor['id'], ability=ability.slug, target_id=target['id'],
                          effect=effect, amount=0, target_hp=target['hp'], turn=turn,
                          critical=False, flinch=False, dodged=False, step_id=step['id'])
            if effect == 'damage':
                if target['team'] == actor['team']:
                    raise InvalidCombatAction('Chained damage must target opponents.')
                if amount:
                    followup = CombatAbility(slug=ability.slug + ':' + step['id'], name=ability.name,
                                             damage=amount, scales_with_attributes=False)
                    result.update(_execute_action(actor, followup, target, turn=turn,
                                                  charge_cooldown=False, rng=rng, derived_damage=True))
                    result['ability'] = ability.slug
                    amount = result['amount']
                else:
                    result['message'] = f"{ability.name}: {step['id']} deals 0 damage to {target['name']}."
            elif effect == 'heal':
                amount = min(amount, max(0, target['max_hp'] - target['hp']))
                target['hp'] += amount
                result['message'] = f"{ability.name}: {step['id']} restores {amount} HP to {target['name']}."
            elif effect == 'resource':
                resources = target.setdefault('resources', {})
                old = resources.get(step['resource'], 0)
                resources[step['resource']] = min(1000000, old + amount)
                amount = resources[step['resource']] - old
                result['message'] = f"{ability.name}: {step['id']} grants {amount} {step['resource']} to {target['name']}."
            else:
                key = f"{actor['id']}:{ability.slug}:{step['id']}"
                modifiers = [m for m in target.get('ability_modifiers', []) if turn < m['until_turn'] and m['key'] != key]
                if len(modifiers) >= 64:
                    raise InvalidCombatAction('Too many active ability modifiers.')
                modifiers.append(dict(key=key, name=ability.name, stat=step['stat'], operation=step['operation'],
                                      value=step['modifier'], ability_slug=step['ability_slug'], until_turn=turn + step['duration']))
                target['ability_modifiers'] = modifiers
                amount = 0
                result['message'] = f"{ability.name} modifies {target['name']}'s {step['stat']} by {step['modifier']:+g}{'%' if step['operation'] == 'percent' else ''} for {step['duration']} rounds."
            result.update(amount=amount, target_hp=target['hp'])
            previous += amount
            results.append(result)
    return results


def execute_action(actor: dict, ability: CombatAbility, target: dict, *, turn: int, rng=None) -> dict:
    return execute_cast(actor, ability, [target], turn=turn, rng=rng)[0]


def resolve_damage(target, damage, *, turn, periodic=False):
    """One HP/shield resolver for direct hits and periodic damage."""
    minimum_hp = 1 if turn < target.get('shield', {}).get('until_turn', 0) else 0
    if not periodic:
        damage = round(damage * (1 - target.get('derived_stats', {}).get('damage_reduction_percent', 0) / 100))
        damage = max(1, damage)
        if target.get('guarding') and target.get('guard_turn', turn) == turn:
            guard_percent = target.get('guard_reduction_percent')
            if guard_percent is not None:
                damage = max(1, round(damage * (1 - min(100, max(0, guard_percent)) / 100)))
            else:
                # Encounters and affliction interactions created before percentage
                # Guard retain their finite block pool until the round ends.
                remaining = target.get('guard_remaining', target.get('guard_power', 4))
                blocked = min(remaining, damage)
                target['guard_remaining'] = remaining - blocked
                damage -= blocked
    amount = min(max(0, target['hp'] - minimum_hp), max(0, damage))
    target['hp'] -= amount
    return amount


def apply_status(target, definition, source_id):
    """Stacks share a refreshed duration; nonstacking effects refresh only."""
    from app.afflictions import add, status
    add(target, definition, source_id)
    active = status(target, definition['slug'])
    if not active:
        return f"{target['name']} is immune to {definition['name']}."
    stacks = active['stacks']
    return f"{target['name']} gains {definition['name']} ({stacks} stack{'s' if stacks != 1 else ''})."


def tick_statuses(combatants, *, turn):
    """Called once at round end inside the encounter transaction; never on reads."""
    from math import ceil
    results = []
    for target in combatants:
        if target['hp'] <= 0:
            target['statuses'] = []
            continue
        remaining = []
        for status in target.get('statuses', []):
            if target['hp'] <= 0:
                break
            damage = ceil((status['damage'] + status.get('amplification', 0)) * status['stacks'] * (100 - status['resistance']) / 100)
            if status.get('periodic', 'damage') == 'damage':
                amount = resolve_damage(target, damage, turn=turn, periodic=True)
                results.append(dict(actor_id=status['source_id'], target_id=target['id'],
                                ability=status['slug'], effect='damage_over_time', amount=amount,
                                target_hp=target['hp'], turn=turn,
                                    message=f"{target['name']} takes {amount} {status['name']} damage."))
            if status.get('preserve_rounds', 0):
                status['preserve_rounds'] -= 1
            else:
                status['remaining_rounds'] -= 1
            if status['remaining_rounds'] > 0:
                remaining.append(status)
        target['statuses'] = remaining if target['hp'] > 0 else []
    return results


def roll_chance(percent, rng):
    return percent > 0 and rng.random() * 100 < percent


def consume_flinch(actor, turn):
    """One skipped action, followed by a full round protected from reapplication."""
    if not actor.pop('flinched', False):
        return None
    actor['flinch_immune_until'] = turn + 2
    return dict(actor_id=actor['id'], target_id=actor['id'], ability='flinch',
                effect='flinch', amount=0, target_hp=actor['hp'], turn=turn,
                message=f"{actor['name']} flinches and loses their action.")
