from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import LootType


STARTER_LOOT_TYPES = [
    {"slug": "souls", "name": "Souls"},
    {"slug": "consumables", "name": "Consumables"},
    {"slug": "gold", "name": "Gold"},
    {"slug": "weapons", "name": "Weapons"},
    {"slug": "armor", "name": "Armor"},
    {"slug": "essence", "name": "Essence"},
    {"slug": "exp", "name": "Exp"},
    {"slug": "orbs", "name": "Orbs"},
]


class LootTypeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    slug: str
    name: str


def seed_loot_types() -> None:
    with SessionLocal.begin() as db:
        insert = sqlite_insert if db.bind.dialect.name == "sqlite" else postgres_insert
        for item in STARTER_LOOT_TYPES:
            db.execute(insert(LootType).values(**item)
                       .on_conflict_do_nothing(index_elements=["slug"]))


router = APIRouter(prefix="/api/loot-types", tags=["loot types"])


@router.get("", response_model=list[LootTypeRead])
def list_loot_types(db: Session = Depends(get_db)):
    return db.scalars(select(LootType).order_by(LootType.name)).all()


@router.get("/{slug}", response_model=LootTypeRead)
def get_loot_type(slug: str, db: Session = Depends(get_db)):
    loot_type = db.get(LootType, slug)
    if loot_type is None:
        raise HTTPException(404, "Loot type not found.")
    return loot_type
