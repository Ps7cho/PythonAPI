"""Orbs unlock existing ability records without changing equipped loadouts."""
from fastapi import HTTPException
from sqlalchemy import select
from app.models import OrbOutcome, AbsorbedEssence, EssenceDefinition, AdventurerAbility, OwnedConsumable, GameEvent
from app.essences import village_character


def options(db):
    return [dict(orb_slug=row.orb_slug, essence_slug=row.essence_slug,
                 ability_id=str(row.ability_id), name=row.ability.name,
                 description=row.ability.description)
            for row in db.scalars(select(OrbOutcome).join(EssenceDefinition,
                EssenceDefinition.consumable_slug == OrbOutcome.essence_slug)
                .where(EssenceDefinition.active.is_(True)).order_by(OrbOutcome.orb_slug, OrbOutcome.essence_slug))]


def use(db, hero, essence_slug, orb_slug):
    hero = village_character(db, hero)
    essence = db.get(EssenceDefinition, essence_slug)
    if essence is None or not essence.active:
        raise HTTPException(404, 'Essence is unavailable or retired.')
    if not db.scalar(select(AbsorbedEssence).where(AbsorbedEssence.adventurer_id == hero.id,
                                                 AbsorbedEssence.essence_slug == essence_slug)):
        raise HTTPException(409, 'Choose an essence this character has absorbed.')
    recipe = db.get(OrbOutcome, (orb_slug, essence_slug))
    if recipe is None:
        raise HTTPException(404, 'This orb has no outcome for that essence.')
    learned = db.scalar(select(AdventurerAbility).where(AdventurerAbility.adventurer_id == hero.id,
                                                       AdventurerAbility.ability_id == recipe.ability_id))
    if learned and learned.unlocked:
        raise HTTPException(409, 'This ability is already learned. The orb was not consumed.')
    owned = db.scalar(select(OwnedConsumable).where(OwnedConsumable.adventurer_id == hero.id,
                      OwnedConsumable.consumable_slug == orb_slug).with_for_update(of=OwnedConsumable)
                      .execution_options(populate_existing=True))
    if owned is None or owned.quantity <= 0 or owned.definition.effect != 'orb':
        raise HTTPException(409, 'You do not own that orb.')
    if learned:
        learned.unlocked = True
    else:
        db.add(AdventurerAbility(adventurer_id=hero.id, ability_id=recipe.ability_id, unlocked=True))
    owned.quantity -= 1
    db.add(GameEvent(event_type='orb_used', payload={'adventurer_id': str(hero.id), 'orb_slug': orb_slug,
                'essence_slug': essence_slug, 'ability_id': str(recipe.ability_id)}))
    db.commit()
    return dict(ability_id=str(recipe.ability_id), name=recipe.ability.name,
                message='Learned ' + recipe.ability.name + '. Equip it in the Abilities tab.')
