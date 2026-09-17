"""Permanent essence absorption uses the existing owned-consumable inventory."""
from fastapi import HTTPException
from sqlalchemy import select
from app.models import Adventurer, AbsorbedEssence, EssenceDefinition, OwnedConsumable, GameEvent
from app.essence_content import ESSENCES
from app.orb_content import ACTIVE_ESSENCES, update_loot


def add_essence_loot(rules):
    for tier in rules.get('encounter_groups', {}).get('loot_tiers', []):
        existing = {d.get('consumable_slug') for d in tier['drops']}
        for item in ESSENCES:
            if item['slug'].removeprefix('essence-') not in ACTIVE_ESSENCES:
                continue
            if item['slug'] not in existing:
                tier['drops'].append(dict(name=item['name'], consumable_slug=item['slug'], quantity=1, weight=1))
    return rules


def describe(db, hero_id):
    return [dict(slot=row.slot, slug=row.essence_slug, name=row.definition.consumable.name, active=row.definition.active,
                 powers=row.definition.powers, absorbed_at=row.absorbed_at.isoformat())
            for row in db.scalars(select(AbsorbedEssence).where(AbsorbedEssence.adventurer_id == hero_id)
                                  .order_by(AbsorbedEssence.slot))]


def village_character(db, hero):
    from app.journeys import active_adventure
    hero = db.scalar(select(Adventurer).where(Adventurer.id == hero.id).with_for_update()
                     .execution_options(populate_existing=True))
    if not hero.is_alive or hero.health <= 0:
        raise HTTPException(409, 'Only a living character can use essences or orbs.')
    if active_adventure(db, [hero.id]):
        raise HTTPException(409, 'Return to the village before using essences or orbs.')
    return hero


def absorb(db, hero, slug):
    hero = village_character(db, hero)
    definition = db.get(EssenceDefinition, slug)
    if definition is None or not definition.active:
        raise HTTPException(404, 'Essence is unavailable or retired.')
    absorbed = db.scalars(select(AbsorbedEssence).where(AbsorbedEssence.adventurer_id == hero.id)).all()
    if any(row.essence_slug == slug for row in absorbed):
        raise HTTPException(409, 'This essence is already absorbed.')
    if len(absorbed) >= 3:
        raise HTTPException(409, 'A character can permanently absorb only three essences.')
    owned = db.scalar(select(OwnedConsumable).where(OwnedConsumable.adventurer_id == hero.id,
                      OwnedConsumable.consumable_slug == slug).with_for_update(of=OwnedConsumable)
                      .execution_options(populate_existing=True))
    if owned is None or owned.quantity <= 0:
        raise HTTPException(409, 'You do not own this essence item.')
    slot = next(n for n in (1, 2, 3) if n not in {row.slot for row in absorbed})
    db.add(AbsorbedEssence(adventurer_id=hero.id, slot=slot, essence_slug=slug))
    owned.quantity -= 1
    db.add(GameEvent(event_type='essence_absorbed', payload={'adventurer_id': str(hero.id), 'essence_slug': slug, 'slot': slot}))
    db.commit()
    return {'essences': describe(db, hero.id), 'message': definition.consumable.name + ' permanently absorbed.'}


def catalog(db):
    return [dict(slug=row.consumable_slug, name=row.consumable.name, powers=row.powers)
            for row in db.scalars(select(EssenceDefinition).where(EssenceDefinition.active.is_(True)).order_by(EssenceDefinition.consumable_slug))]
