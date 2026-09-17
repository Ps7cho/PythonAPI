from copy import deepcopy
from dataclasses import asdict, replace
from random import randint
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import Enemy, EnemyAbility, EnemyWeapon, Encounter, EncounterEnemy
from app.loadouts import executable
from app.weapons import enemy_weapon
from sqlalchemy.orm import object_session, selectinload


ATTRIBUTE_NAMES = ("Might", "Vitality", "Defense", "Agility", "Speed", "Precision",
                   "Willpower", "Awareness", "Luck", "Affinity")


def definition(slug, name, enemy_type, attributes, hp, power, guard, speed):
    return dict(slug=slug, name=name, enemy_type=enemy_type,
                attributes=dict(zip(ATTRIBUTE_NAMES, attributes, strict=True)),
                stat_ranges={key: {"min": limits[0], "max": limits[1]}
                             for key, limits in zip(("hp", "power", "guard", "speed"),
                                                    (hp, power, guard, speed), strict=True)})


# Initial balancing values; existing database definitions are never overwritten.
STARTER_ENEMIES = [
    definition("goblin", "Goblin", "humanoid", (7, 6, 4, 10, 9, 7, 4, 7, 5, 2),
               (45, 60), (4, 6), (1, 3), (7, 10)),
    definition("goblin-archer", "Goblin Archer", "humanoid", (5, 5, 3, 10, 9, 12, 5, 10, 5, 2),
               (40, 55), (5, 8), (0, 2), (8, 11)),
    definition("wolf", "Wolf", "beast", (9, 8, 4, 12, 13, 8, 5, 14, 4, 1),
               (50, 65), (5, 7), (1, 3), (10, 14)),
    definition("hobgoblin", "Hobgoblin", "humanoid", (14, 13, 11, 7, 6, 10, 10, 9, 5, 3),
               (70, 90), (7, 10), (4, 7), (4, 7)),
    definition("roadside-bandit", "Roadside bandit", "humanoid", (10, 10, 6, 8, 8, 8, 6, 8, 5, 2),
               (60, 60), (6, 6), (0, 0), (8, 8)),
]


STARTER_ENEMIES.extend([
    definition('raid-packlord', 'The Packlord', 'boss', (18,18,12,14,14,12,10,15,5,2), (105,120), (7,9), (4,6), (8,10)),
    definition('raid-warden', 'Hollow Warden', 'boss', (20,20,16,8,7,12,14,10,5,4), (140,160), (8,10), (6,9), (4,6)),
    definition('raid-sovereign', 'Ash Sovereign', 'boss', (24,24,18,12,10,16,18,14,8,8), (170,195), (9,12), (8,10), (5,8)),
])


from app.enemy_content import ENEMY_SPECS
for slug, name, taxonomy, hp, power, armor, speed, weapon, abilities in ENEMY_SPECS:
    STARTER_ENEMIES.append(definition(slug, name, taxonomy,
        (power[1], hp[0]//5, armor[1], speed[1], speed[1], power[0], 8, 8, 5, 5), hp, power, armor, speed))


class StatRange(BaseModel):
    min: int = Field(ge=0)
    max: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.max < self.min:
            raise ValueError("Stat maximum must be at least its minimum")
        return self


class EnemyStats(BaseModel):
    hp: StatRange
    power: StatRange
    guard: StatRange
    speed: StatRange

    @model_validator(mode="after")
    def living(self):
        if self.hp.min < 1:
            raise ValueError("Enemy HP must be positive")
        return self


class EnemyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    slug: str
    name: str
    enemy_type: str
    attributes: dict[str, int]
    stat_ranges: EnemyStats


def seed_enemies() -> None:
    with SessionLocal.begin() as db:
        insert = sqlite_insert if db.bind.dialect.name == "sqlite" else postgres_insert
        for item in STARTER_ENEMIES:
            EnemyRead.model_validate(item)
            db.execute(insert(Enemy).values(**item).on_conflict_do_nothing(index_elements=["slug"]))


def enemy_loadout(db, slug, assignments=None):
    if assignments is None:
        assignments = db.scalars(select(EnemyAbility).where(EnemyAbility.enemy_slug == slug)
                                .order_by(EnemyAbility.priority.desc(), EnemyAbility.ability_id)).all()
    return [{"ability": asdict(replace(executable(entry.ability), slug=entry.ability.slug)),
             "weight": entry.weight, "priority": entry.priority} for entry in assignments]


def roll_enemy(enemy: Enemy, party_size: int, rng=None, loadouts=None) -> EncounterEnemy:
    stats = EnemyStats.model_validate(enemy.stat_ranges)
    rolled = {key: (rng.randint if rng else randint)(value.min, value.max) for key, value in
              ((key, getattr(stats, key)) for key in EnemyStats.model_fields)}
    instance_id = uuid4()
    hp = rolled.pop("hp") * party_size
    loadout = loadouts.get(enemy.slug) if loadouts is not None else None
    weapon = deepcopy(loadout['weapon']) if loadout is not None else enemy_weapon(object_session(enemy), enemy.slug, rolled['power'])
    if weapon:
        weapon['base_damage'] = rolled['power']
    return EncounterEnemy(id=instance_id, enemy_slug=enemy.slug, position=0, state={
        "id": str(instance_id), "enemy_slug": enemy.slug, "name": enemy.name,
        "enemy_type": enemy.enemy_type, "attributes": deepcopy(enemy.attributes),
        "status_resistances": deepcopy(enemy.type_profile.status_resistances) if enemy.type_profile else {},
        "hp": hp, "max_hp": hp, **rolled,
        "abilities": deepcopy(loadout["abilities"]) if loadout is not None else enemy_loadout(object_session(enemy), enemy.slug),
        "equipped_weapon": weapon,
    })


def link_legacy_enemies() -> None:
    """Link known legacy JSON enemies without rerolling or changing their health."""
    with SessionLocal.begin() as db:
        catalog = {e.name: e for e in db.scalars(select(Enemy))}
        for encounter in db.scalars(select(Encounter).order_by(Encounter.id).with_for_update()):
            if encounter.enemy_instances or not encounter.enemies:
                continue
            if any(e.get("name") not in catalog for e in encounter.enemies):
                continue
            for position, old in enumerate(encounter.enemies):
                definition = catalog[old["name"]]
                state = deepcopy(old)
                state.update(enemy_slug=definition.slug, enemy_type=definition.enemy_type,
                             attributes=deepcopy(definition.attributes))
                encounter.enemy_instances.append(EncounterEnemy(
                    id=UUID(old["id"]), enemy_slug=definition.slug, position=position, state=state))
            encounter.enemies = []


router = APIRouter(prefix="/api/enemies", tags=["enemies"])


@router.get("", response_model=list[EnemyRead])
def list_enemies(db: Session = Depends(get_db)):
    return db.scalars(select(Enemy).order_by(Enemy.name)).all()


@router.get("/{slug}", response_model=EnemyRead)
def get_enemy(slug: str, db: Session = Depends(get_db)):
    enemy = db.get(Enemy, slug)
    if enemy is None:
        raise HTTPException(404, "Enemy not found.")
    return enemy


def batch_enemy_loadouts(db, slugs):
    assignments = {slug: [] for slug in slugs}
    for entry in db.scalars(select(EnemyAbility).where(EnemyAbility.enemy_slug.in_(slugs))
                            .options(selectinload(EnemyAbility.ability))
                            .order_by(EnemyAbility.priority.desc(), EnemyAbility.ability_id)):
        assignments[entry.enemy_slug].append(entry)
    weapons = {entry.enemy_slug: entry for entry in db.scalars(select(EnemyWeapon)
               .where(EnemyWeapon.enemy_slug.in_(slugs)).options(selectinload(EnemyWeapon.weapon_type)))}
    return {slug: {'abilities': enemy_loadout(db, slug, assignments[slug]),
                   'weapon': enemy_weapon(db, slug, 1, assignments=weapons)} for slug in slugs}
