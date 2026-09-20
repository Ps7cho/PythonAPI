"""Validated, data-driven ability dials; no executable expressions in content."""
from copy import deepcopy
from dataclasses import replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EFFECTS = ('damage', 'guard', 'heal', 'buff', 'shield', 'cleanse', 'evade', 'affliction')
# Catalog field -> (minimum, maximum, integer). Also used for temporary modifiers.
DIALS = {
    'power': (0, 10000, True), 'damage_multiplier': (0, 100, False),
    'cooldown_value': (0, 10000, True), 'max_targets': (1, 100, True),
    'duration_turns': (1, 100, True), 'guard_percent': (0, 100, True),
}


class EffectStep(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, allow_inf_nan=False)
    id: str = Field(pattern=r'^[a-z][a-z0-9_]{0,39}$')
    effect: Literal['damage', 'heal', 'resource', 'modifier'] = 'heal'
    recipient: Literal['self', 'targets', 'party', 'enemies'] = 'self'
    source: Literal['fixed', 'primary', 'damage_dealt', 'previous'] = 'damage_dealt'
    value: float = Field(default=50, ge=0, le=10000)
    split: bool = False
    when: Literal['always', 'on_hit', 'on_damage', 'on_kill'] = 'on_damage'
    resource: str = Field(default='mana', pattern=r'^[a-z][a-z0-9_]{0,39}$')
    stat: str = Field(default='power', pattern=r'^(power|damage_multiplier|cooldown_value|max_targets|duration_turns|guard_percent|(step|modifier|duration):[a-z][a-z0-9_]{0,39})$')
    operation: Literal['add', 'percent'] = 'percent'
    modifier: float = Field(default=25, ge=-10000, le=10000)
    duration: int = Field(default=1, ge=1, le=100)
    ability_slug: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def sensible_combination(self):
        if self.effect == 'damage' and self.recipient not in ('targets', 'enemies'):
            raise ValueError('Chained damage must target opponents.')
        if self.effect in ('heal', 'resource') and self.recipient == 'enemies':
            raise ValueError('Healing and resources must target allies.')
        if self.effect == 'modifier' and (self.source != 'fixed' or self.split):
            raise ValueError('Modifiers use fixed values and cannot be split.')
        if self.operation == 'percent' and self.modifier < -100:
            raise ValueError('A percentage modifier cannot be below -100%.')
        return self


def validate_chain(chain, target_type):
    if not isinstance(chain, list) or len(chain) > 16:
        raise ValueError('An ability may have up to 16 follow-up effects.')
    steps = [EffectStep.model_validate(step) for step in chain]
    if len({s.id for s in steps}) != len(steps):
        raise ValueError('Effect step IDs must be unique.')
    for step in steps:
        if step.recipient == 'targets':
            if step.effect == 'damage' and target_type != 'enemy':
                raise ValueError('Damage to selected targets requires enemy targeting.')
            if step.effect in ('heal', 'resource') and target_type == 'enemy':
                raise ValueError('Choose self or party for healing/resources after an enemy attack.')
    return [s.model_dump() for s in steps]


def validate_upgrades(upgrades, chain):
    import math
    if not isinstance(upgrades, dict) or len(upgrades) > 32:
        raise ValueError('Rank upgrades must be an object with at most 32 ranks.')
    ids = {s['id'] for s in chain}
    parsed = deepcopy(upgrades)
    for rank, values in upgrades.items():
        if not isinstance(rank, str) or not isinstance(values, dict):
            raise ValueError('Each rank needs a map of value overrides.')
        for dial, value in values.items():
            if ':' in dial:
                prefix, step_id = dial.split(':', 1)
                if prefix not in ('step', 'modifier', 'duration') or step_id not in ids:
                    raise ValueError('Rank upgrade references a missing effect step.')
                low, high, integer = (1, 100, True) if prefix == 'duration' else (-10000, 10000, False) if prefix == 'modifier' else (0, 10000, False)
                step = next(s for s in chain if s['id'] == step_id)
                if prefix == 'modifier' and step.get('operation', 'percent') == 'percent': low = -100
            elif dial in DIALS:
                low, high, integer = DIALS[dial]
            else:
                raise ValueError(f'Unsupported ability dial: {dial}.')
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high or integer and int(value) != value:
                raise ValueError(f'{dial} must be {"an integer" if integer else "a number"} between {low} and {high}.')
            parsed[rank][dial] = int(value) if integer else value
    return parsed


def rank_values(ability, ladder=(), level=1):
    values = {key: getattr(ability, key) for key in DIALS}
    chain = deepcopy(ability.effect_chain or [])
    for rank in ladder:
        if level < rank.min_level:
            continue
        for key, value in (ability.rank_upgrades or {}).get(rank.slug, {}).items():
            if ':' in key:
                prefix, step_id = key.split(':', 1)
                for step in chain:
                    if step['id'] == step_id: step[{'step': 'value', 'modifier': 'modifier', 'duration': 'duration'}[prefix]] = value
            else:
                values[key] = value
    return {**values, 'effect_chain': chain}


def effective_ability(actor, ability, turn):
    """Derive current cast values without changing the saved base definition."""
    mapping = {'power': 'damage', 'cooldown_value': 'cooldown_seconds' if ability.cooldown_seconds or ability.cooldown_unit_seconds > 1 else 'cooldown_turns'}
    updates = {}
    active = [m for m in actor.get('ability_modifiers', []) if turn < m['until_turn']
              and (not m.get('ability_slug') or m['ability_slug'] in (ability.slug, ability.catalog_slug))]
    for key, (low, high, integer) in DIALS.items():
        field = mapping.get(key, key)
        original = getattr(ability, field)
        unit = ability.cooldown_unit_seconds if field == 'cooldown_seconds' else 1
        if original is None:
            continue
        matching = [m for m in active if m['stat'] == key]
        if not matching:
            continue
        value = original / unit + sum(m['value'] for m in matching if m['operation'] == 'add')
        value *= max(0, 1 + sum(m['value'] for m in matching if m['operation'] == 'percent') / 100)
        # Timed cooldown dials use the executable's seconds unit.
        updates[field] = min(high, max(low, round(value) if integer else value)) * unit
    if any(':' in m['stat'] for m in active):
        chain = deepcopy(ability.effect_chain)
        for step in chain:
            for prefix, field, default, low, high, integer in (
                ('step', 'value', 50, 0, 10000, False),
                ('modifier', 'modifier', 25, -100 if step.get('operation', 'percent') == 'percent' else -10000, 10000, False),
                ('duration', 'duration', 1, 1, 100, True),
            ):
                matching = [m for m in active if m['stat'] == prefix + ':' + step['id']]
                if not matching: continue
                value = step.get(field, default) + sum(m['value'] for m in matching if m['operation'] == 'add')
                value *= max(0, 1 + sum(m['value'] for m in matching if m['operation'] == 'percent') / 100)
                step[field] = min(high, max(low, round(value) if integer else value))
        updates['effect_chain'] = chain
    return replace(ability, **updates) if updates else ability


TEMPLATE_FIELDS = {'effect_type', 'target_type', 'power', 'damage_multiplier', 'requires_weapon',
                   'allowed_weapon_tags', 'cooldown_type', 'cooldown_value', 'max_targets',
                   'duration_turns', 'guard_percent', 'effect_chain', 'rank_upgrades', 'ability_type',
                   'status_effect_slug', 'affliction_ops'}


def validate_template(data):
    if not isinstance(data, dict) or set(data) - TEMPLATE_FIELDS:
        raise ValueError('Archetype contains unsupported definition fields.')
    effect, target = data.get('effect_type', 'damage'), data.get('target_type', 'enemy')
    if effect not in EFFECTS or target not in ('enemy', 'self', 'ally', 'party'):
        raise ValueError('Unknown primary effect or target type.')
    if effect in ('guard', 'evade') and target != 'self':
        raise ValueError('Guard and evasion must target self.')
    if effect == 'damage' and target != 'enemy' or effect not in ('damage', 'affliction') and target == 'enemy':
        raise ValueError('Primary effect and target type are incompatible.')
    numeric = {key: value for key, value in data.items() if key in DIALS and value is not None}
    validate_upgrades({'base': numeric}, [])
    for key in ('power', 'cooldown_value', 'duration_turns', 'guard_percent'):
        if key in data and data[key] is None:
            raise ValueError(f'{key} cannot be empty.')
    if 'requires_weapon' in data and type(data['requires_weapon']) is not bool:
        raise ValueError('requires_weapon must be boolean.')
    if 'allowed_weapon_tags' in data and (not isinstance(data['allowed_weapon_tags'], list) or not all(isinstance(t, str) for t in data['allowed_weapon_tags'])):
        raise ValueError('Weapon tags must be a list of strings.')
    if data.get('cooldown_type', 'turn') not in ('turn', 'minutes', 'hours'):
        raise ValueError('Unsupported cooldown type.')
    result = deepcopy(data)
    if not isinstance(data.get('affliction_ops', []), list):
        raise ValueError('Affliction operations must be an array.')
    result['effect_chain'] = validate_chain(data.get('effect_chain', []), target)
    result['rank_upgrades'] = validate_upgrades(data.get('rank_upgrades', {}), result['effect_chain'])
    return result
