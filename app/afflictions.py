"""Ordered affliction instructions, executed only on encounter-local cast copies."""
from copy import deepcopy
from math import ceil
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Interaction(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    op: Literal['apply', 'amplify', 'spread', 'consume', 'detonate', 'convert', 'cleanse', 'preserve', 'trigger', 'exploit']
    affliction: str = Field(min_length=1, max_length=64)
    stacks: int = Field(default=1, ge=1, le=100)
    power: int = Field(default=1, ge=0, le=10000)
    rounds: int = Field(default=1, ge=1, le=100)
    threshold: int = Field(default=3, ge=1, le=100)
    effect: Literal['damage', 'heal', 'guard', 'resource'] = 'damage'
    resource: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_]{0,63}$')
    into: str | None = Field(default=None, min_length=1, max_length=64)
    move: bool = False
    definition: dict | None = None

    @model_validator(mode='after')
    def destinations(self):
        if self.op == 'apply' and not self.definition:
            raise ValueError('Apply requires a resolved affliction definition.')
        if self.op == 'convert' and bool(self.into) == bool(self.resource):
            raise ValueError('Convert requires exactly one destination affliction or resource.')
        if self.into and not self.definition:
            raise ValueError('Conversion requires a resolved destination definition.')
        if self.effect == 'resource' and not self.resource:
            raise ValueError('Resource effects require a resource name.')
        return self


def status(target, slug):
    return next((s for s in target.get('statuses', []) if s['slug'] == slug), None)


def definition(row):
    return {**{key: getattr(row, key) for key in ('slug', 'name', 'damage', 'duration', 'max_stacks')},
            **deepcopy(row.rules or {})}


def resolve_operations(ability):
    from sqlalchemy.orm import object_session
    from app.models import StatusEffect
    from app.combat import InvalidCombatAction
    operations = deepcopy(ability.affliction_ops or [])
    if len(operations) > 32:
        raise InvalidCombatAction('An ability supports at most 32 affliction instructions.')
    db = object_session(ability)
    for operation in operations:
        slug = operation.get('into') if operation.get('op') == 'convert' else operation.get('affliction') if operation.get('op') == 'apply' else None
        if slug:
            row = db.get(StatusEffect, slug) if db else None
            if row is None:
                raise InvalidCombatAction('Unknown affliction definition: ' + slug)
            operation['definition'] = definition(row)
        try:
            Interaction.model_validate(operation)
        except ValueError as exc:
            raise InvalidCombatAction(str(exc)) from exc
    return operations


def remove(target, entry, count):
    if not entry or entry.get('preserve_rounds', 0) > 0:
        return 0
    count = min(count, entry['stacks'])
    entry['stacks'] -= count
    if not entry['stacks']:
        target['statuses'].remove(entry)
    return count


def cleanse(target, slug=None, count=None):
    removed = 0
    for entry in list(target.get('statuses', [])):
        if entry.get('harmful', True) and (slug is None or entry['slug'] == slug):
            removed += remove(target, entry, entry['stacks'] if count is None else max(0, count - removed))
    return removed


def add(target, definition, source_id, stacks=1):
    from app.combat import InvalidCombatAction
    for key in ('damage', 'duration', 'max_stacks'):
        if not isinstance(definition.get(key), int) or isinstance(definition[key], bool) or definition[key] < 1:
            raise InvalidCombatAction('Invalid affliction definition.')
    slug = definition['slug']
    harmful = definition.get('harmful', True)
    resistance = max(0, min(100, target.get('status_resistances', {}).get(slug, 0))) if harmful else 0
    if resistance == 100:
        return 0
    ward = target.get('derived_stats', {}).get('status_resistance_percent', 0) if harmful else 0
    resistance = 100 - (100 - resistance) * (100 - ward) / 100
    entries = target.setdefault('statuses', [])
    active = status(target, slug)
    old_count = active['stacks'] if active else 0
    saved = {**deepcopy(definition), 'stacks': min(definition['max_stacks'], old_count + stacks),
             'remaining_rounds': definition['duration'], 'source_id': source_id, 'resistance': resistance}
    if active:
        for key in ('amplification', 'preserve_rounds'):
            if key in active:
                saved[key] = active[key]
        entries.remove(active)
    entries.append(saved)
    return saved['stacks'] - old_count


def execute(actor, ability, targets, *, turn, before, dodged=()):
    from app.combat import InvalidCombatAction, resolve_damage
    try:
        if len(ability.affliction_ops) > 32:
            raise ValueError('Too many affliction instructions.')
        instructions = [Interaction.model_validate(item) for item in ability.affliction_ops]
    except ValueError as exc:
        raise InvalidCombatAction(str(exc)) from exc
    results = []
    triggered = set()

    def report(target, op, amount, message, effect='affliction'):
        results.append(dict(actor_id=actor['id'], ability=ability.slug, target_id=target['id'],
                            effect=effect, amount=amount, target_hp=target['hp'], turn=turn,
                            message=message, interaction=op.op, affliction=op.affliction,
                            critical=False, flinch=False, dodged=False))

    def immediate(target, op, stacks, resistance=0, amplification=0):
        if not stacks:
            return
        receiver = target if op.effect == 'damage' or op.op == 'detonate' else actor
        effect = 'damage' if op.op == 'detonate' else op.effect
        amount = stacks * (op.power + amplification)
        if effect == 'damage':
            if receiver['team'] == actor['team']:
                raise InvalidCombatAction('Affliction damage must target an opponent.')
            amount = ceil(amount * (100 - resistance) / 100)
            amount = resolve_damage(receiver, amount, turn=turn) if amount else 0
        elif effect == 'heal':
            amount = min(amount, receiver['max_hp'] - receiver['hp'])
            receiver['hp'] += amount
        elif effect == 'guard':
            current = receiver.get('guard_remaining', 0) if receiver.get('guard_turn') == turn else 0
            receiver.update(guarding=True, guard_turn=turn, guard_remaining=current + amount, guard_power=current + amount)
        else:
            resources = receiver.setdefault('resources', {})
            resources[op.resource] = resources.get(op.resource, 0) + amount
        report(receiver, op, amount, f"{actor['name']} uses {ability.name}: {op.op} {op.affliction} ({stacks} stacks), {amount} {op.resource if effect == 'resource' else effect} to {receiver['name']}.", effect)

    for op in instructions:
        if op.op == 'spread':
            if len(targets) < 2:
                raise InvalidCombatAction('Spread needs a source target followed by at least one destination.')
            source = targets[0]
            if source['id'] in dodged or source['hp'] <= 0:
                continue
            entry = status(source, op.affliction)
            for destination in targets[1:]:
                if not entry or not entry['stacks'] or destination['hp'] <= 0 or destination['id'] in dodged:
                    continue
                count = min(entry['stacks'], op.stacks)
                if op.move and entry.get('preserve_rounds', 0):
                    count = 0
                definition = {k: deepcopy(entry[k]) for k in ('slug', 'name', 'damage', 'duration', 'max_stacks', 'harmful', 'periodic') if k in entry}
                added = add(destination, definition, actor['id'], count) if count else 0
                if op.move:
                    remove(source, entry, added)
                report(destination, op, added, f"{actor['name']} {'moves' if op.move else 'copies'} {added} {op.affliction} stacks from {source['name']} to {destination['name']}.")
            continue
        for target in targets:
            if target['hp'] <= 0 or target['id'] in dodged:
                continue
            entry = status(target, op.affliction)
            count = min(entry['stacks'], op.stacks) if entry else 0
            if op.op == 'apply':
                if op.definition['slug'] != op.affliction:
                    raise InvalidCombatAction('Affliction definition does not match Apply.')
                amount = add(target, op.definition, actor['id'], op.stacks)
            elif op.op == 'amplify':
                amount = min(op.power, 10000 - entry.get('amplification', 0)) if entry else 0
                if entry:
                    entry['amplification'] = min(10000, entry.get('amplification', 0) + amount)
            elif op.op == 'cleanse':
                amount = cleanse(target, op.affliction, op.stacks)
            elif op.op == 'preserve':
                amount = op.rounds if entry else 0
                if entry:
                    entry['preserve_rounds'] = max(entry.get('preserve_rounds', 0), op.rounds)
            elif op.op == 'convert':
                if op.into == op.affliction:
                    raise InvalidCombatAction('Convert requires a different affliction.')
                amount = 0
                if entry and not entry.get('preserve_rounds', 0):
                    if op.into:
                        if op.definition['slug'] != op.into:
                            raise InvalidCombatAction('Invalid conversion destination.')
                        amount = add(target, op.definition, actor['id'], count)
                        remove(target, entry, amount)
                    else:
                        amount = remove(target, entry, count)
                        resources = actor.setdefault('resources', {})
                        resources[op.resource] = resources.get(op.resource, 0) + amount * op.power
            elif op.op in ('consume', 'detonate'):
                resistance = entry.get('resistance', 0) if entry else 0
                consumed = remove(target, entry, count)
                immediate(target, op, consumed, resistance, entry.get('amplification', 0) if entry else 0)
                amount = consumed
            elif op.op == 'trigger':
                previous = before.get(target['id'], {}).get(op.affliction, 0)
                key = (target['id'], op.affliction, op.threshold)
                amount = int(bool(entry and previous < op.threshold <= entry['stacks'] and key not in triggered))
                if amount:
                    immediate(target, op, 1, entry.get('resistance', 0))
                    triggered.add(key)
            else:
                amount = count
                immediate(target, op, count, entry.get('resistance', 0) if entry else 0, entry.get('amplification', 0) if entry else 0)
            remaining = status(target, op.affliction)
            current = remaining['stacks'] if remaining else 0
            previous = before.setdefault(target['id'], {}).get(op.affliction, 0)
            before[target['id']][op.affliction] = min(previous, current)
            if target['hp'] > 0:
                report(target, op, amount, f"{actor['name']} uses {ability.name}: {op.op} {op.affliction} on {target['name']} ({amount}).")
    return results


from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.auth import current_user
from app.database import get_db
from app.models import StatusEffect, User

router = APIRouter(prefix='/api/afflictions', tags=['afflictions'])


@router.get('')
def catalog(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [definition(row) for row in db.scalars(select(StatusEffect).order_by(StatusEffect.name))]


@router.get('/grammar')
def grammar(user: User = Depends(current_user)):
    return Interaction.model_json_schema()
