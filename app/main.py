from __future__ import annotations
from app.progression import describe as describe_progression

import random
import uuid
from pathlib import Path
from typing import Any, Dict, List

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from app.config import get_settings
from sqlalchemy.orm import Session

from app.database import Base, engine, ensure_ability_columns, ensure_adventurer_columns, ensure_game_event_columns, get_db
from app.models import Ability, Adventurer, AdventurerAbility, Encounter, QuestRun, Inventory, PartyMember, User  # noqa: F401
from app.journeys import village_rest, active_adventure
from sqlalchemy import select, func
from app.request_limits import limiter, RequestLimits
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from app.migrations import migrate
from app.contracts import router as contract_router, soul_inventory
from app.afflictions import router as affliction_router
from app.parties import router as party_router, require_leader
from app.weapons import router as weapon_router, seed_weapons, grant_starter_weapon, equipped_weapon, serialize_weapon
from app.models import Weapon
from app.loadouts import AbilityUseRequest, LoadoutRequest, equipped, save_loadout
from app.orbs import use as use_orb, options as orb_options
from app.essences import describe as describe_essences, absorb as absorb_essence, catalog as essence_catalog
from app.attributes import derived_stats, DESCRIPTIONS as ATTRIBUTE_DESCRIPTIONS
from app.ability_design import rank_values
from app.progression import ranks
from app.consumables import inventory as consumable_inventory
from app.seed_abilities import seed_abilities, seed_enemy_abilities, seed_status_abilities
from app import encounter_service
from app.quest_templates import router as quest_template_router, seed_quest_templates
from app.enemies import router as enemy_router, seed_enemies, link_legacy_enemies
from app.loot_types import router as loot_type_router, seed_loot_types
from app import gear
from app.shop import router as shop_router
from app.auction_house import router as auction_router
from app.auth import router as auth_router, current_user, own_adventurer
from app.live import router as live_router, lifespan
from app.catalog_editor import router as catalog_editor_router, editor_catalog

ATTRIBUTE_NAMES = [
    "Might",
    "Vitality",
    "Defense",
    "Agility",
    "Speed",
    "Precision",
    "Willpower",
    "Awareness",
    "Luck",
    "Affinity",
]

Base.metadata.create_all(bind=engine)
ensure_ability_columns()
ensure_adventurer_columns()
ensure_game_event_columns()
migrate(engine)
seed_abilities()
seed_quest_templates()
seed_enemies()
seed_enemy_abilities()
seed_status_abilities()
from app.migrations.v018_affliction_grammar import seed_grammar
seed_grammar(engine)
seed_weapons()
seed_loot_types()
link_legacy_enemies()


def generate_attribute_budget() -> Dict[str, int]:
    attributes: Dict[str, int] = {name: 1 for name in ATTRIBUTE_NAMES}
    remaining = 100 - len(ATTRIBUTE_NAMES)
    while remaining > 0:
        choice = random.choice(ATTRIBUTE_NAMES)
        attributes[choice] += 1
        remaining -= 1
    return attributes


def serialize_ability(ability: Ability) -> Dict[str, Any]:
    from app.afflictions import definition, resolve_operations
    return {
        "id": str(ability.id),
        "name": ability.name,
        "slug": ability.slug,
        "damage_multiplier": ability.damage_multiplier,
        "status_effect": definition(ability.status_effect) if ability.status_effect else None,
        "affliction_ops": resolve_operations(ability),
        "requires_weapon": ability.requires_weapon,
        "allowed_weapon_tags": ability.allowed_weapon_tags,
        "description": ability.description,
        "ability_type": ability.ability_type,
        "cooldown_type": ability.cooldown_type,
        "cooldown_value": ability.cooldown_value,
        "cost_type": ability.cost_type,
        "cost_value": ability.cost_value,
        "target_type": ability.target_type,
        "max_targets": ability.max_targets,
        "effect_type": ability.effect_type,
        "power": ability.power,
        "archetype_slug": ability.archetype_slug,
        "effect_chain": ability.effect_chain,
        "rank_upgrades": ability.rank_upgrades,
        "duration_turns": ability.duration_turns,
        "guard_percent": ability.guard_percent,
    }


app = FastAPI(
    lifespan=lifespan,
    title="Game API",
    version="1.0.0",
    description="Server-side game logic with event sourcing patterns and Postgres persistence.",
)

app.add_middleware(RequestLimits)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().public_client_origins,
                   allow_credentials=False, allow_methods=["GET", "POST"],
                   allow_headers=["Authorization", "Content-Type", "X-Client-Auth"])


app.include_router(quest_template_router)
app.include_router(enemy_router)
app.include_router(loot_type_router)
app.include_router(shop_router)
app.include_router(gear.router)
app.include_router(auction_router)
app.include_router(auth_router)
app.include_router(weapon_router)
app.include_router(party_router)
app.include_router(live_router)
app.include_router(contract_router)
app.include_router(affliction_router)
app.include_router(catalog_editor_router)


def frontend_file(name: str, media_type: str | None = None) -> FileResponse:
    path = Path(__file__).with_name(name)
    if not path.is_file():
        raise HTTPException(404, "Frontend is hosted separately. API documentation is at /docs.")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def main_page() -> FileResponse:
    return frontend_file("dev.html")


@app.get("/ui/{asset}")
def ui_asset(asset: str):
    if asset not in ("ui.css", "ui.js", "lobby.js", "debug.js", "catalog_editor.js"):
        raise HTTPException(404, "Asset not found.")
    return frontend_file(asset, media_type="text/css" if asset.endswith(".css") else "text/javascript")


@app.get("/journey-ui.js")
def journey_ui():
    return frontend_file("journey_ui.js", media_type="text/javascript")


@app.get("/main", response_class=HTMLResponse)
def main_page_alias() -> FileResponse:
    return frontend_file("dev.html")


@app.get("/adventurers/{adventurer_id}", response_class=HTMLResponse)
def adventurer_page(adventurer_id: uuid.UUID) -> FileResponse:
    return frontend_file("adventurer.html")


@app.get("/api/abilities")
def list_abilities(request: Request, inspect: bool = False, db: Session = Depends(get_db)):
    if inspect:
        user = current_user(request, db)
    abilities = db.query(Ability).order_by(Ability.name.asc()).all()
    if inspect:
        from app.debug_catalog import gameplay_catalog
        result = gameplay_catalog(db, [serialize_ability(ability) for ability in abilities])
        result['editor'] = editor_catalog(db, user)
        return result
    return [serialize_ability(ability) for ability in abilities]


@app.post("/api/adventurers")
def create_adventurer(payload: dict, db: Session = Depends(get_db), user: User = Depends(current_user)):
    limiter.hit(('characters', str(user.id)), 10, 60)
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    count = db.scalar(select(func.count()).select_from(Adventurer).where(Adventurer.owner == user.id))
    if count >= get_settings().max_characters_per_account:
        raise HTTPException(409, 'Character limit reached for this account.')
    name = str(payload.get("name", "")).strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Adventurer name is required.")

    attributes = generate_attribute_budget()
    adventurer = Adventurer(
        name=name,
        level=1,
        attributes=attributes,
        health=derived_stats(attributes)["max_hp"],
        experience=0,
        gold=0,
        is_alive=True,
        owner=user.id,
    )
    db.add(adventurer)
    db.flush()
    db.refresh(adventurer)

    inventory = Inventory(adventurer_id=adventurer.id, items=[])
    db.add(inventory)
    grant_starter_weapon(db, adventurer)
    db.flush()

    default_abilities = db.query(Ability).filter(Ability.starter.is_(True)).order_by(Ability.name.asc()).all()
    for ability in default_abilities:
        exists = db.query(AdventurerAbility).filter(
            AdventurerAbility.adventurer_id == adventurer.id,
            AdventurerAbility.ability_id == ability.id,
        ).first()
        if exists is None:
            db.add(AdventurerAbility(adventurer_id=adventurer.id, ability_id=ability.id, unlocked=True))
    db.commit()

    ability_inventory = [
        {
            "ability_id": str(entry.ability_id),
            "name": entry.ability.name,
            "description": entry.ability.description,
            "ability_type": entry.ability.ability_type,
            "cooldown_type": entry.ability.cooldown_type,
            "cooldown_value": entry.ability.cooldown_value,
            "cost_type": entry.ability.cost_type,
            "cost_value": entry.ability.cost_value,
            "target_type": entry.ability.target_type,
            "max_targets": entry.ability.max_targets,
            "effect_type": entry.ability.effect_type,
            "power": entry.ability.power,
            "requires_weapon": entry.ability.requires_weapon,
            "allowed_weapon_tags": entry.ability.allowed_weapon_tags,
            "damage_multiplier": entry.ability.damage_multiplier,
            "unlocked": entry.unlocked,
        }
        for entry in db.query(AdventurerAbility).filter(AdventurerAbility.adventurer_id == adventurer.id).all()
    ]

    return {
        "id": str(adventurer.id),
        "name": adventurer.name,
        "level": adventurer.level,
        "health": adventurer.health,
        "experience": adventurer.experience,
        "gold": adventurer.gold,
        "is_alive": adventurer.is_alive,
        "attributes": adventurer.attributes,
        "statistics": adventurer.statistics or {},
        "owner": str(adventurer.owner),
        "ability_inventory": ability_inventory,
    }


@app.get("/api/adventurers")
def my_adventurers(db: Session = Depends(get_db), user: User = Depends(current_user)):
    heroes = list(db.scalars(select(Adventurer).where(Adventurer.owner == user.id).order_by(Adventurer.created_at)))
    hero_ids = {hero.id for hero in heroes}
    active_encounters = db.scalars(select(Encounter).join(QuestRun).where(
        QuestRun.status.in_(["active", "awaiting_continue"]),
        QuestRun.party_id.in_(select(PartyMember.party_id).where(PartyMember.adventurer_id.in_(hero_ids))))).all() if heroes else []
    active_by_hero = {
        participant["id"]: str(encounter.id)
        for encounter in active_encounters
        for participant in encounter.participants
        if participant["id"] in {str(hero_id) for hero_id in hero_ids} and str(encounter.id) == encounter.quest_run.current_stage
    }
    return [{"id": str(h.id), "name": h.name, "health": h.health, "is_alive": h.is_alive,
             "rank": describe_progression(db, h)["rank"], "rank_level": h.level,
             "essence_types": [essence["name"] for essence in describe_essences(db, h.id)],
             "active_encounter_id": active_by_hero.get(str(h.id)),
             "statistics": h.statistics or {}}
            for h in heroes]


@app.post("/api/adventurers/{adventurer_id}/quest")
def go_on_quest(adventurer_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    own_adventurer(db, adventurer_id, user)
    return encounter_service.start_encounter(db, [adventurer_id])


@app.post("/api/encounters", status_code=201)
def create_encounter(payload: encounter_service.EncounterCreateRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    for adventurer_id in payload.adventurer_ids:
        own_adventurer(db, adventurer_id, user)
    return encounter_service.start_encounter(db, payload.adventurer_ids, payload.enemy_slug, payload.encounter_count, template_slug=payload.template_slug, accept_rank_risk=payload.accept_rank_risk)


@app.get("/api/encounters/{encounter_id}")
def read_encounter(encounter_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    encounter = encounter_service.get_encounter(db, encounter_id)
    ids = [uuid.UUID(p['id']) for p in encounter.participants]
    if not db.query(Adventurer).filter(Adventurer.id.in_(ids), Adventurer.owner == user.id).first():
        raise HTTPException(404, "Encounter not found.")
    return encounter_service.snapshot(encounter)


@app.post("/api/encounters/{encounter_id}/actions")
def encounter_action(encounter_id: uuid.UUID, payload: encounter_service.EncounterActionRequest,
                     db: Session = Depends(get_db), user: User = Depends(current_user)):
    own_adventurer(db, payload.actor_id, user)
    return encounter_service.apply_action(db, encounter_id, payload)


@app.post("/api/encounters/{encounter_id}/continue")
def continue_quest(encounter_id: uuid.UUID, payload: encounter_service.ContinueRequest = Body(default=encounter_service.ContinueRequest()), db: Session = Depends(get_db), user: User = Depends(current_user)):
    encounter = encounter_service.get_encounter(db, encounter_id)
    require_leader(encounter.quest_run.party, user)
    return encounter_service.continue_quest(db, encounter_id, payload.return_to_village, payload.choice)


@app.get("/api/adventurers/{adventurer_id}")
def adventurer_details(adventurer_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    hero = own_adventurer(db, adventurer_id, user)
    active = db.query(Encounter).join(QuestRun).filter(
        QuestRun.status.in_(["active", "awaiting_continue"]),
        QuestRun.party_id.in_(select(PartyMember.party_id).where(PartyMember.adventurer_id == hero.id))).all()
    current = next((e for e in active if any(p['id'] == str(hero.id) for p in e.participants)
                    and (e.state == 'player_turn' or e.quest_run.current_stage == str(e.id))), None)
    runs = db.scalars(select(QuestRun).where(QuestRun.party_id.in_(select(PartyMember.party_id).where(PartyMember.adventurer_id == hero.id)))
                      .order_by(QuestRun.created_at.desc()).limit(50)).all()
    quest_history = [{
        'id': str(run.id),
        'title': run.quest.rewards.get('journey', {}).get('title', run.quest.location),
        'location': run.quest.location,
        'status': run.status,
        'encounter_count': len(run.quest.encounter_pool or []),
        'created_at': run.created_at.isoformat(),
    } for run in runs]
    return {"id": str(hero.id), "name": hero.name, "level": hero.level,
            "health": hero.health, "max_health": (next(p["max_hp"] for p in current.participants if p["id"] == str(hero.id)) if current else gear.stats(db, hero)["max_hp"]),
            "derived_stats": (next(p.get("derived_stats", {}) for p in current.participants if p["id"] == str(hero.id)) if current else gear.stats(db, hero)), "attribute_descriptions": ATTRIBUTE_DESCRIPTIONS, "statuses": hero.combat_statuses, "consumables": consumable_inventory(db, hero.id), "experience": hero.experience,
            "gold": hero.gold, "is_alive": hero.is_alive, "attributes": hero.attributes, "effective_attributes": gear.effective_attributes(db, hero),
            "statistics": hero.statistics or {}, "account_statistics": user.statistics or {},
            "progression": describe_progression(db, hero),
            "inventory": (hero.inventory.items if hero.inventory else []) + soul_inventory(db, hero.id) +
                         [serialize_weapon(w) for w in db.query(Weapon).filter(Weapon.adventurer_id == hero.id).all()] + gear.inventory(db, hero),
            "equipment": {**gear.equipment(db, hero),
                          "Main Hand": equipped_weapon(db, hero)},
            "essences": describe_essences(db, hero.id), "essence_limit": 3, "essence_catalog": essence_catalog(db), "orb_options": orb_options(db),
            "skills": [],
            "equipped_ability_ids": [str(a.id) for a in equipped(db, hero)],
            "abilities": [{**serialize_ability(entry.ability), "unlocked": entry.unlocked,
                           "rank_values": rank_values(entry.ability, ranks(db), hero.level)}
                          for entry in hero.ability_inventory],
            "quest_history": quest_history,
            "active_encounter_id": str(current.id) if current else None}


@app.post('/api/adventurers/{adventurer_id}/rest')
def long_rest(adventurer_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return village_rest(db, own_adventurer(db, adventurer_id, user))


@app.post('/api/adventurers/{adventurer_id}/loadout')
def update_loadout(adventurer_id: uuid.UUID, payload: LoadoutRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    hero = own_adventurer(db, adventurer_id, user)
    save_loadout(db, hero, payload.ability_ids)
    return {'equipped_ability_ids': [str(a.id) for a in equipped(db, hero)]}


@app.post("/api/adventurers/{adventurer_id}/abilities/use")
def use_ability(adventurer_id: uuid.UUID, payload: AbilityUseRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    own_adventurer(db, adventurer_id, user)
    action = encounter_service.EncounterActionRequest(actor_id=adventurer_id,
        ability_id=payload.ability_id, weapon_id=payload.weapon_id, expected_turn=payload.expected_turn, target_id=payload.target_id, target_ids=payload.target_ids)
    return encounter_service.apply_action(db, payload.encounter_id, action)


@app.get("/health")
def healthcheck() -> dict:
    return {"status": "ok"}


class AttributeAllocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    allocations: dict[str, StrictInt]


@app.post("/api/adventurers/{adventurer_id}/attributes")
def allocate_attributes(adventurer_id: uuid.UUID, payload: AttributeAllocation,
                        db: Session = Depends(get_db), user: User = Depends(current_user)):
    hero = own_adventurer(db, adventurer_id, user)
    hero = db.scalar(select(Adventurer).where(Adventurer.id == hero.id).with_for_update().execution_options(populate_existing=True))
    if active_adventure(db, [hero.id]):
        raise HTTPException(409, "Return to the village before assigning attributes.")
    values = payload.allocations
    if not values or any(k not in ATTRIBUTE_NAMES or v <= 0 for k, v in values.items()):
        raise HTTPException(422, "Choose known attributes and positive whole points.")
    total = sum(values.values())
    if total > hero.attribute_points:
        raise HTTPException(409, "Not enough attribute points.")
    hero.attributes = {**hero.attributes, **{k: hero.attributes.get(k, 0) + v for k, v in values.items()}}
    hero.attribute_points -= total
    db.commit()
    return {"attributes": hero.attributes, "attribute_points": hero.attribute_points, "derived_stats": gear.stats(db, hero)}


@app.post('/api/adventurers/{adventurer_id}/essences/{essence_slug}/absorb')
def absorb_character_essence(adventurer_id: uuid.UUID, essence_slug: str,
                             db: Session = Depends(get_db), user: User = Depends(current_user)):
    return absorb_essence(db, own_adventurer(db, adventurer_id, user), essence_slug)


@app.post('/api/adventurers/{adventurer_id}/essences/{essence_slug}/orbs/{orb_slug}/use')
def use_character_orb(adventurer_id: uuid.UUID, essence_slug: str, orb_slug: str,
                      db: Session = Depends(get_db), user: User = Depends(current_user)):
    return use_orb(db, own_adventurer(db, adventurer_id, user), essence_slug, orb_slug)
