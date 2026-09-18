from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.progression import rank_for
from app.models import RankDefinition
from app.auth import current_user, own_adventurer
from app.database import SessionLocal, get_db
from app.models import (Adventurer, Encounter, Enemy, EnemyWeapon, EquippedWeapon,
                        QuestRun, User, Weapon, WeaponType)


STARTER_WEAPON_TYPES = [
    {"slug":"throwing-axe", "name":"Throwing Axe", "tags":["weapon","ranged","thrown","axe","one_handed","slashing"]},
    {"slug": "sword", "name": "Sword", "tags": ["weapon", "melee", "sword", "one_handed", "slashing"]},
    {"slug": "axe", "name": "Axe", "tags": ["weapon", "melee", "axe", "one_handed", "slashing"]},
    {"slug": "mace", "name": "Mace", "tags": ["weapon", "melee", "mace", "one_handed", "blunt"]},
    {"slug": "dagger", "name": "Dagger", "tags": ["weapon", "melee", "dagger", "one_handed", "piercing"]},
    {"slug": "bow", "name": "Bow", "tags": ["weapon", "ranged", "bow", "two_handed", "piercing"]},
    {"slug": "staff", "name": "Staff", "tags": ["weapon", "melee", "staff", "two_handed", "blunt"]},
    {"slug": "shield", "name": "Shield", "tags": ["weapon", "shield", "one_handed", "blunt"]},
]


def grant_starter_weapon(db, hero):
    """Grant only to characters with no weapon inventory; never re-equip an existing weapon."""
    if db.scalar(select(Weapon.id).where(Weapon.adventurer_id == hero.id).limit(1)):
        return
    from app.models import AuctionListing
    if db.scalar(select(AuctionListing.id).where(AuctionListing.seller_id == hero.id,
            AuctionListing.item['item_type'].as_string() == 'weapon').limit(1)):
        return
    weapon = Weapon(adventurer_id=hero.id, weapon_type_slug="sword", name="Training Sword", base_damage=10)
    db.add(weapon)
    db.flush()
    db.add(EquippedWeapon(adventurer_id=hero.id, weapon_id=weapon.id))


def seed_weapons():
    with SessionLocal.begin() as db:
        for spec in STARTER_WEAPON_TYPES:
            if db.get(WeaponType, spec["slug"]) is None:
                db.add(WeaponType(**spec))
        db.flush()
        for slug, weapon_type in {"goblin": "dagger", "goblin-archer": "bow",
                                  "hobgoblin": "axe", "roadside-bandit": "sword",
                                  "raid-warden": "mace", "raid-sovereign": "sword"}.items():
            if db.get(Enemy, slug) and db.get(EnemyWeapon, slug) is None:
                db.add(EnemyWeapon(enemy_slug=slug, weapon_type_slug=weapon_type))
        from app.enemy_content import ENEMY_SPECS
        for slug, _, _, _, _, _, _, weapon_type, _ in ENEMY_SPECS:
            if weapon_type and db.get(EnemyWeapon, slug) is None:
                db.add(EnemyWeapon(enemy_slug=slug, weapon_type_slug=weapon_type))
        for hero in db.scalars(select(Adventurer)):
            grant_starter_weapon(db, hero)


def serialize_weapon(weapon):
    return {"id": str(weapon.id), "name": weapon.name, "weapon_type": weapon.weapon_type_slug,
            "tags": list(weapon.weapon_type.tags), "base_damage": weapon.base_damage, "required_rank": weapon.required_rank}


def equipped_weapon(db, hero):
    entry = db.get(EquippedWeapon, hero.id)
    if entry is None:
        return None
    if entry.weapon.adventurer_id != hero.id:
        raise HTTPException(409, "Equipped weapon is not owned by this adventurer.")
    return serialize_weapon(entry.weapon)


def combat_weapons(db, hero):
    """Snapshot every owned weapon carried in the character's pocket dimension."""
    return [serialize_weapon(w) for w in db.scalars(select(Weapon).where(
        Weapon.adventurer_id == hero.id).order_by(Weapon.name, Weapon.id))]


def enemy_weapon(db, slug, power, assignments=None):
    entry = assignments.get(slug) if assignments is not None else (db.get(EnemyWeapon, slug) if slug else None)
    if entry is None:
        return None
    return {"name": entry.weapon_type.name, "weapon_type": entry.weapon_type_slug,
            "tags": list(entry.weapon_type.tags), "base_damage": power}


class EquipWeaponRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weapon_id: UUID | None


router = APIRouter(prefix="/api", tags=["weapons"])


@router.get("/weapon-types")
def list_weapon_types(db: Session = Depends(get_db)):
    return [{"slug": t.slug, "name": t.name, "tags": t.tags}
            for t in db.scalars(select(WeaponType).order_by(WeaponType.name))]


@router.get("/adventurers/{adventurer_id}/weapons")
def list_weapons(adventurer_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    hero = own_adventurer(db, adventurer_id, user)
    return {"weapons": [serialize_weapon(w) for w in db.scalars(
        select(Weapon).where(Weapon.adventurer_id == hero.id).order_by(Weapon.name, Weapon.id))],
        "equipped_weapon": equipped_weapon(db, hero)}


@router.post("/adventurers/{adventurer_id}/weapon")
def equip_weapon(adventurer_id: UUID, payload: EquipWeaponRequest,
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    hero = own_adventurer(db, adventurer_id, user)
    db.execute(select(Adventurer).where(Adventurer.id == hero.id).with_for_update().execution_options(populate_existing=True)).scalar_one()
    active = db.scalars(select(Encounter).join(QuestRun).where(
        QuestRun.status.in_(["active", "awaiting_continue"])))
    if any(any(p["id"] == str(hero.id) for p in e.participants) for e in active):
        raise HTTPException(409, "Finish your adventure before changing weapons.")
    weapon = db.get(Weapon, payload.weapon_id) if payload.weapon_id else None
    if payload.weapon_id and (weapon is None or weapon.adventurer_id != hero.id):
        raise HTTPException(422, "Only an owned weapon can be equipped.")
    if weapon:
        minimum = db.get(RankDefinition, weapon.required_rank)
        if minimum is None or hero.level < minimum.min_level:
            raise HTTPException(409, "Your rank cannot equip this weapon yet.")
    entry = db.get(EquippedWeapon, hero.id)
    if weapon is None:
        if entry:
            db.delete(entry)
    elif entry:
        entry.weapon_id = weapon.id
    else:
        db.add(EquippedWeapon(adventurer_id=hero.id, weapon_id=weapon.id))
    db.commit()
    return {"equipped_weapon": equipped_weapon(db, hero)}
