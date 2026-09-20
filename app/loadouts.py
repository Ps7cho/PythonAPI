from dataclasses import asdict
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, delete

from app.combat import CombatAbility
from app.afflictions import definition, resolve_operations
from app.progression import rank_for
from app.models import Adventurer, EquippedAbility, Encounter, QuestRun


class LoadoutRequest(BaseModel):
    ability_ids: list[UUID] = Field(min_length=1, max_length=32)


class AbilityUseRequest(BaseModel):
    encounter_id: UUID
    ability_id: UUID
    weapon_id: UUID | None = None
    expected_turn: int = Field(ge=1)
    target_id: UUID | None = None
    target_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)


def executable(ability, ladder=(), level=1):
    from app.ability_design import rank_values
    values = rank_values(ability, ladder, level)
    return CombatAbility(slug=str(ability.id), name=ability.name, effect=ability.effect_type,
                         effect_chain=values['effect_chain'], duration_turns=values['duration_turns'],
                         guard_percent=values['guard_percent'],
                         cooldown_unit_seconds={'minutes': 60, 'hours': 3600}.get(ability.cooldown_type, 1),
                         status_effect=definition(ability.status_effect) if ability.status_effect else None,
                         affliction_ops=resolve_operations(ability),
                         damage=values['power'], catalog_slug=ability.slug, description=ability.description,
                         damage_multiplier=values['damage_multiplier'],
                         requires_weapon=ability.requires_weapon, allowed_weapon_tags=list(ability.allowed_weapon_tags or []),
                         cooldown_turns=values['cooldown_value'] if ability.cooldown_type == 'turn' else 0,
                         cooldown_seconds=values['cooldown_value'] * {'minutes': 60, 'hours': 3600}.get(ability.cooldown_type, 0),
                         target_type=ability.target_type, max_targets=values['max_targets'])


def equipped(db, hero):
    known = {a.ability_id: a.ability for a in hero.ability_inventory if a.unlocked}
    slots = db.scalars(select(EquippedAbility).where(EquippedAbility.adventurer_id == hero.id)
                       .order_by(EquippedAbility.slot)).all()
    if slots:
        return [known[s.ability_id] for s in slots if s.ability_id in known]
    return sorted(known.values(), key=lambda a: (a.loadout_order if a.loadout_order is not None else 99, a.name))[:rank_for(db, hero.level).ability_slots]


def combat_loadout(db, hero):
    from app.progression import ranks
    known = [entry.ability for entry in hero.ability_inventory if entry.unlocked]
    return [asdict(executable(a, ranks(db), hero.level)) for a in sorted(known, key=lambda a: (
        a.loadout_order if a.loadout_order is not None else 99, a.name))]


def save_loadout(db, hero, ids):
    db.execute(select(Adventurer).where(Adventurer.id == hero.id).with_for_update().execution_options(populate_existing=True)).scalar_one()
    if not 1 <= len(ids) <= rank_for(db, hero.level).ability_slots:
        raise HTTPException(422, "Your rank does not provide that many ability slots.")
    if len(set(ids)) != len(ids):
        raise HTTPException(422, 'Each ability can only occupy one slot.')
    active = db.scalars(select(Encounter).join(QuestRun).where(QuestRun.status.in_(['active', 'awaiting_continue']))).all()
    if any(any(p['id'] == str(hero.id) for p in e.participants) for e in active):
        raise HTTPException(409, 'Finish your adventure before changing equipped abilities.')
    known = {entry.ability_id: entry.ability for entry in hero.ability_inventory if entry.unlocked}
    if any(i not in known for i in ids):
        raise HTTPException(422, 'Only learned abilities can be equipped.')
    if not any(known[i].effect_type == 'damage' or any(op.get('op') == 'detonate' or op.get('op') in ('consume', 'trigger', 'exploit') and op.get('effect', 'damage') == 'damage' for op in known[i].affliction_ops or []) for i in ids):
        raise HTTPException(422, 'Equip at least one damage ability.')
    if any(known[i].effect_type not in ('damage', 'guard', 'heal', 'buff', 'shield', 'cleanse', 'evade', 'affliction') for i in ids):
        raise HTTPException(422, 'This ability effect is not supported.')
    db.execute(delete(EquippedAbility).where(EquippedAbility.adventurer_id == hero.id))
    db.add_all([EquippedAbility(adventurer_id=hero.id, slot=slot, ability_id=i) for slot, i in enumerate(ids)])
    db.commit()
