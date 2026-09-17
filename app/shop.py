from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user, own_adventurer
from app.consumables import DEFINITIONS, grant
from app.database import get_db
from app.models import Adventurer, Consumable, RankDefinition, User, Weapon, WeaponType

WEAPON_PRICES = {
    "dagger": 35,
    "sword": 50,
    "mace": 60,
    "axe": 70,
    "bow": 75,
    "staff": 80,
    "throwing-axe": 85,
}
CONSUMABLE_PRICES = {
    "healing-potion": 20,
    "cleansing-draught": 25,
    "might-tonic": 30,
}


class PurchaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_type: str
    item_slug: str
    adventurer_id: UUID
    quantity: int = 1


router = APIRouter(prefix="/api/shop", tags=["shop"])


def catalog(db: Session):
    return {
        "weapons": [{"item_type": "weapon", "slug": weapon.slug, "name": weapon.name,
                     "price": WEAPON_PRICES[weapon.slug], "tags": weapon.tags,
                     "base_damage": 12 + list(WEAPON_PRICES).index(weapon.slug) * 2}
                    for weapon in db.scalars(select(WeaponType).where(WeaponType.slug.in_(WEAPON_PRICES)).order_by(WeaponType.name))],
        "consumables": [{"item_type": "consumable", "slug": item["slug"], "name": item["name"],
                         "price": CONSUMABLE_PRICES[item["slug"]], "quantity": 1,
                         "description": item["description"]} for item in DEFINITIONS if item["slug"] in CONSUMABLE_PRICES],
    }


@router.get("")
def shop_catalog(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return catalog(db)


@router.post("/purchase")
def purchase(payload: PurchaseRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if payload.quantity < 1 or payload.quantity > 20:
        raise HTTPException(422, "Quantity must be between 1 and 20.")
    hero = own_adventurer(db, payload.adventurer_id, user)
    hero = db.scalar(select(Adventurer).where(Adventurer.id == hero.id).with_for_update().execution_options(populate_existing=True))
    if not hero.is_alive or hero.health <= 0:
        raise HTTPException(409, "Only living adventurers can shop.")
    if payload.item_type == "consumable":
        item = next((item for item in DEFINITIONS if item["slug"] == payload.item_slug), None)
        price = CONSUMABLE_PRICES.get(payload.item_slug)
    elif payload.item_type == "weapon":
        item = db.get(WeaponType, payload.item_slug)
        price = WEAPON_PRICES.get(payload.item_slug)
    else:
        raise HTTPException(422, "Unknown shop category.")
    if item is None or price is None:
        raise HTTPException(404, "Shop item not found.")
    total = price * payload.quantity
    if hero.gold < total:
        raise HTTPException(409, "Not enough gold.")
    hero.gold -= total
    if payload.item_type == "consumable":
        grant(db, hero.id, payload.item_slug, payload.quantity)
    else:
        rank = db.scalar(select(RankDefinition).where(RankDefinition.slug == "iron"))
        for _ in range(payload.quantity):
            db.add(Weapon(adventurer_id=hero.id, weapon_type_slug=item.slug,
                          name=item.name, base_damage=12 + list(WEAPON_PRICES).index(item.slug) * 2,
                          required_rank=rank.slug if rank else "iron"))
    db.commit()
    return {"gold": hero.gold, "message": f"Bought {payload.quantity} x {item.name}."}
