"""Server-owned lifetime XP progression and rank capacities."""
from math import isqrt
from sqlalchemy import select
from app.models import RankDefinition, Weapon


def level_cost(level):
    # Exact ceil(100 * level ** 1.5), without floating-point rounding.
    squared = 10000 * level ** 3
    root = isqrt(squared)
    return root if root * root == squared else root + 1


def level_floor(level):
    return sum(level_cost(i) for i in range(1, level))


def ranks(db):
    transaction = db.get_transaction()
    if db.info.get("progression_transaction") is not transaction or "progression_ranks" not in db.info:
        db.info["progression_ranks"] = list(db.scalars(select(RankDefinition).order_by(RankDefinition.min_level)))
        db.info["progression_transaction"] = db.get_transaction()
    return db.info["progression_ranks"]


def rank_for(db, level):
    return next(r for r in reversed(ranks(db)) if level >= r.min_level)


def award_experience(db, hero, amount):
    if amount < 0:
        raise ValueError("Experience rewards cannot be negative")
    old_level, old_rank = hero.level, rank_for(db, hero.level)
    hero.experience += amount
    threshold = level_floor(hero.level) + level_cost(hero.level)
    while hero.experience >= threshold:
        hero.level += 1
        hero.attribute_points += 3
        threshold += level_cost(hero.level)
    events = []
    if hero.level > old_level:
        events.append(f"LEVEL UP: {hero.name} reached level {hero.level}; +{3 * (hero.level - old_level)} attribute points.")
    for rank in ranks(db):
        if old_rank.min_level < rank.min_level <= hero.level:
            if rank.equipment_reward:
                db.add(Weapon(adventurer_id=hero.id, required_rank=rank.slug, **rank.equipment_reward))
            events.append(f"RANK UP: {hero.name} reached {rank.name}! " + "; ".join(rank.unlocks))
    return events


def describe(db, hero):
    catalog = ranks(db)
    rank = rank_for(db, hero.level)
    next_rank = next((r for r in catalog if r.min_level > hero.level), None)
    floor = level_floor(hero.level)
    rank_floor = level_floor(rank.min_level)
    target = level_floor(next_rank.min_level) if next_rank else None
    return {"rank": rank.name, "rank_slug": rank.slug, "attribute_points": hero.attribute_points,
            "level_xp": max(0, hero.experience - floor), "level_xp_required": level_cost(hero.level),
            "xp_to_level": max(0, floor + level_cost(hero.level) - hero.experience),
            "next_rank": next_rank.name if next_rank else None,
            "xp_to_rank": max(0, target - hero.experience) if target is not None else None,
            "rank_xp": max(0, hero.experience - rank_floor),
            "rank_xp_required": target - rank_floor if target is not None else None,
            "ability_slots": rank.ability_slots, "passive_slots": rank.passive_slots,
            "ladder": [{"name": r.name, "level": r.min_level, "unlocks": r.unlocks,
                        "reached": hero.level >= r.min_level} for r in catalog]}


def validate_rank_entry(db, heroes, required_slug, accept_risk=False):
    from fastapi import HTTPException
    ladder = ranks(db)
    required = next((i for i, r in enumerate(ladder) if r.slug == required_slug), None)
    if required is None:
        raise HTTPException(409, "Quest rank requirement is unavailable.")
    under = []
    for hero in heroes:
        actual = max(i for i, r in enumerate(ladder) if hero.level >= r.min_level)
        if required - actual > 1:
            raise HTTPException(409, "Every party member must be within one rank of the quest requirement.")
        if actual < required:
            under.append(hero)
    if under and not accept_risk:
        raise HTTPException(409, {'code':'rank_warning', 'required_rank':ladder[required].name,
            'message':f"This quest is {ladder[required].name} rank. " + ', '.join(h.name for h in under) +
                      " are one rank below it. Enemies may be deadly, and higher-rank gear cannot be equipped yet."})
    return [str(h.id) for h in under]
