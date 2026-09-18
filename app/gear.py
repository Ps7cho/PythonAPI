"""Armor/accessory ownership and bonuses feeding the shared attribute formulas."""
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.database import get_db
from app.auth import current_user, own_adventurer
from app.models import Gear, EquippedGear, Adventurer, RankDefinition, User
from app.attributes import DESCRIPTIONS, derived_stats

SLOTS = ('Head', 'Chest', 'Hands', 'Legs', 'Feet', 'Off Hand', 'Amulet', 'Ring')
router = APIRouter(prefix='/api/adventurers', tags=['equipment'])


def serialize(gear):
    definition = gear.definition
    return dict(id=str(gear.id), item_type='gear', definition_slug=definition.slug,
        name=definition.name, slot=definition.slot, bonuses=definition.bonuses,
        required_rank=definition.required_rank)


def inventory(db, hero):
    return [serialize(g) for g in db.scalars(select(Gear).where(Gear.adventurer_id == hero.id).order_by(Gear.id))]


def equipment(db, hero):
    result = dict.fromkeys(SLOTS)
    for row in db.scalars(select(EquippedGear).where(EquippedGear.adventurer_id == hero.id)):
        if row.gear.adventurer_id == hero.id:
            result[row.slot] = serialize(row.gear)
    return result


def effective_attributes(db, hero):
    attributes = dict(hero.attributes or {})
    for item in equipment(db, hero).values():
        for name, value in (item['bonuses'] if item else {}).items():
            if name in DESCRIPTIONS and isinstance(value, int) and not isinstance(value, bool):
                attributes[name] = attributes.get(name, 0) + value
    return attributes


def stats(db, hero):
    return derived_stats(effective_attributes(db, hero))


class EquipRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    slot: str
    gear_id: UUID | None


@router.post('/{adventurer_id}/equipment')
def equip(adventurer_id: UUID, payload: EquipRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from app.journeys import active_adventure
    owned_hero = own_adventurer(db, adventurer_id, user)
    hero = db.scalar(select(Adventurer).where(Adventurer.id == owned_hero.id)
        .with_for_update().execution_options(populate_existing=True))
    if payload.slot not in SLOTS:
        raise HTTPException(422, 'Unknown equipment slot.')
    if active_adventure(db, [hero.id]):
        raise HTTPException(409, 'Return to the village before changing equipment.')
    if not hero.is_alive or hero.health <= 0:
        raise HTTPException(409, 'Only living adventurers can change equipment.')
    gear = db.get(Gear, payload.gear_id) if payload.gear_id else None
    if payload.gear_id:
        if gear is None or gear.adventurer_id != hero.id:
            raise HTTPException(422, 'Choose an owned item.')
        if gear.definition.slot != payload.slot:
            raise HTTPException(422, 'This item does not fit that slot.')
        rank = db.get(RankDefinition, gear.definition.required_rank)
        if rank is None or hero.level < rank.min_level:
            raise HTTPException(409, 'Your rank cannot equip this item yet.')
    entry = db.get(EquippedGear, (hero.id, payload.slot))
    if gear is None:
        if entry:
            db.delete(entry)
    elif entry:
        entry.gear_id = gear.id
    else:
        db.add(EquippedGear(adventurer_id=hero.id, slot=payload.slot, gear_id=gear.id))
    db.flush()
    # Reload joined gear relationships after changing an existing slot.
    db.expire_all()
    hero = db.get(Adventurer, adventurer_id)
    hero.health = min(hero.health, stats(db, hero)['max_hp'])
    db.commit()
    return {'equipment': equipment(db, hero), 'derived_stats': stats(db, hero), 'health': hero.health}
