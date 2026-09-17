"""Validated journey content, saved encounter plans, and shared rest rules."""
from copy import deepcopy
from app.attributes import derived_stats
from random import choice
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from typing import Annotated, Literal
from sqlalchemy import select

from app.models import Adventurer, Enemy, EncounterEnemy, GameEvent, PartyMember, QuestRun, RestPolicy
from app.group_journeys import GroupRules, annotate_groups, validate_group_catalog, threat
from app.cooldowns import restore_cooldowns, saved_cooldowns
from app.enemies import roll_enemy


class RestRules(BaseModel):
    heal_percent: int = Field(ge=0, le=100)
    turns: int = Field(default=0, ge=0)
    seconds: int = Field(default=0, ge=0)
    clear_cooldowns: bool = False


class Stage(BaseModel):
    description: str
    groups: list[list[str]] = Field(min_length=1)


class JourneyRules(BaseModel):
    death_policy: Literal["permanent", "rescue_on_return"] = "permanent"
    bank_rewards: bool = False
    repeat_groups: bool = True
    raid: dict | None = None
    encounter_groups: GroupRules | None = None
    required_rank: str = "iron"
    kind: str
    description: str
    stages: list[Stage] = Field(min_length=1, max_length=20)
    gold: int = Field(ge=0)
    experience: int = Field(ge=0)
    completion_gold: int = Field(default=0, ge=0)
    completion_experience: int = Field(default=0, ge=0)
    camp_rest: str | None = None
    max_rests: int | None = Field(default=None, ge=0)
    push_gold: int = Field(default=0, ge=0)
    push_experience: int = Field(default=0, ge=0)
    push_xp_tiers: list[Annotated[int, Field(strict=True, ge=0, le=100)]] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_push_tiers(self):
        if self.push_xp_tiers and (self.max_rests is None or self.push_xp_tiers != sorted(self.push_xp_tiers)):
            raise ValueError("XP tiers require explicit push choices and ascending percentages.")
        return self

    enemy_health_multiplier: float = Field(default=1, gt=0, le=5)
    enemy_power_multiplier: float = Field(default=1, gt=0, le=5)


def active_adventure(db, ids):
    return db.scalar(select(QuestRun.id).join(PartyMember, PartyMember.party_id == QuestRun.party_id)
                     .where(PartyMember.adventurer_id.in_(ids),
                            QuestRun.status.in_(["active", "awaiting_continue"])).limit(1))


def build_plan(db, template):
    rules = JourneyRules.model_validate(template.journey)
    pool = {slug for stage in rules.stages for group in stage.groups for slug in group}
    if any(not group or len(group) > 6 for stage in rules.stages for group in stage.groups):
        raise HTTPException(409, "Journey groups must contain one to six enemies.")
    found = set(db.scalars(select(Enemy.slug).where(Enemy.slug.in_(pool))))
    if found != pool:
        raise HTTPException(409, "Journey references an unavailable enemy.")
    settings = rules.model_dump()
    if rules.encounter_groups and rules.max_rests is None:
        raise HTTPException(409, "Grouped journeys require a rest budget.")
    validate_group_catalog(db, settings)
    if rules.camp_rest:
        rest = db.get(RestPolicy, rules.camp_rest)
        if rest is None:
            raise HTTPException(409, "Camp rest definition is unavailable.")
        settings["rest"] = RestRules.model_validate(rest.settings).model_dump()
    settings.update(name=template.name, region=template.region)
    if rules.raid:
        from app.raids import build_raid_plan
        return build_raid_plan(db, template.slug, settings)
    plan = [{"encounter_id": str(uuid4()), "enemy_slugs": choice(stage.groups),
             "description": stage.description} for stage in rules.stages]
    if rules.encounter_groups:
        annotate_groups(plan, rules.encounter_groups.size, rules.encounter_groups.min_size, rules.encounter_groups.max_size)
        settings['original_encounter_count'] = len(plan)
    return plan, settings


def roll_group(db, entry, party_size, settings):
    slugs = entry.get("enemy_slugs", [entry.get("enemy_slug")])
    definitions = {e.slug: e for e in db.scalars(select(Enemy).where(Enemy.slug.in_(slugs)))}
    if any(slug not in definitions for slug in slugs):
        raise HTTPException(409, "An enemy definition is unavailable.")
    scale = threat(settings, entry['group_number']) if settings.get('encounter_groups') else {'health': 100, 'power': 100}
    instances = []
    for position, slug in enumerate(slugs):
        if entry.get('seeded_enemies'):
            state = deepcopy(entry['seeded_enemies'][position])
            instance_id = uuid4()
            state.update(id=str(instance_id), hp=state['max_hp'] * party_size, max_hp=state['max_hp'] * party_size)
            instance = EncounterEnemy(id=instance_id, enemy_slug=slug, position=position, state=state)
        else:
            instance = roll_enemy(definitions[slug], party_size)
        instance.position = position
        state = deepcopy(instance.state)
        state["hp"] = state["max_hp"] = max(1, round(state["hp"] * settings.get("enemy_health_multiplier", 1) * scale["health"] / 100))
        state["power"] = max(1, round(state["power"] * settings.get("enemy_power_multiplier", 1) * scale["power"] / 100))
        if state.get("equipped_weapon"):
            state["equipped_weapon"]["base_damage"] = state["power"]
        instance.state = state
        instances.append(instance)
    return instances


def apply_rest(actor, rules):
    """Rest restores living characters only; both camp and village use this path."""
    if actor["hp"] <= 0:
        return 0
    before = actor["hp"]
    actor["hp"] = min(actor["max_hp"], before + round(actor["max_hp"] * rules.heal_percent / 100))
    actor["guarding"] = False
    actor.pop("buff", None)
    actor.pop("shield", None)
    actor.pop("evasion", None)
    actor["statuses"] = []
    actor["ability_ready_turns"] = {} if rules.clear_cooldowns else {
        key: max(1, ready - rules.turns) for key, ready in actor.get("ability_ready_turns", {}).items()}
    actor["ability_ready_at"] = {} if rules.clear_cooldowns else {
        key: max(0, ready - rules.seconds) for key, ready in actor.get("ability_ready_at", {}).items()}
    actor["power_ready_turn"] = 1 if rules.clear_cooldowns else max(1, actor.get("power_ready_turn", 1) - rules.turns)
    return actor["hp"] - before


def village_rest(db, hero):
    hero = db.scalar(select(Adventurer).where(Adventurer.id == hero.id).with_for_update().execution_options(populate_existing=True))
    if active_adventure(db, [hero.id]):
        raise HTTPException(409, "Return to the village before taking a long rest.")
    if not hero.is_alive or hero.health <= 0:
        raise HTTPException(409, "Rest cannot revive a fallen adventurer.")
    policy = db.get(RestPolicy, "village")
    if policy is None:
        raise HTTPException(409, "Village rest is unavailable.")
    actor = {"hp": hero.health, "max_hp": derived_stats(hero.attributes)["max_hp"], "statuses": deepcopy(hero.combat_statuses or []), **restore_cooldowns(hero.combat_cooldowns)}
    healed = apply_rest(actor, RestRules.model_validate(policy.settings))
    if healed or hero.combat_statuses or hero.combat_cooldowns != saved_cooldowns(actor, 1):
        db.add(GameEvent(event_type="village_rest", payload={"adventurer_id": str(hero.id), "healed": healed}))
    hero.health = actor["hp"]
    hero.combat_statuses = actor["statuses"]
    hero.combat_cooldowns = saved_cooldowns(actor, 1)
    db.commit()
    return {"adventurer_id": str(hero.id), "health": hero.health, "healed": healed,
            "message": "Long rest complete. Ready for another journey."}


def push_xp_bonus(journey, streak):
    """Percentage for consecutive push choices; absent tiers preserve old snapshots."""
    tiers = journey.get("push_xp_tiers", [])
    if not tiers or streak <= 0 or journey.get("max_rests") is None:
        return 0
    return tiers[min(streak, len(tiers)) - 1]


def boosted_encounter_xp(base_xp, journey, progress):
    percent = push_xp_bonus(journey, progress.get("push_streak", 0))
    return (base_xp * (100 + percent) + 99) // 100
