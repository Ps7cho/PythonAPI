"""Persistent gameplay counters for adventurers and their owning accounts."""
from copy import deepcopy


STAT_KEYS = (
    "damage_dealt",
    "enemies_killed",
    "highest_rank_kill",
    "highest_rank_kill_level",
    "encounters_started",
    "encounters_completed",
    "adventures_started",
    "adventures_completed",
    "adventures_defeated",
    "times_killed",
    "consumables_used",
    "adventurers_killed",
)
LIFETIME_KEYS = {
    "damage_dealt": "damage_dealt_lifetime",
    "enemies_killed": "enemies_killed_lifetime",
    "encounters_started": "encounters_started_lifetime",
    "encounters_completed": "encounters_completed_lifetime",
    "adventures_started": "adventures_started_lifetime",
    "adventures_completed": "adventures_completed_lifetime",
    "adventures_defeated": "adventures_defeated_lifetime",
    "adventurers_killed": "adventurers_killed_lifetime",
    "consumables_used": "consumables_used_lifetime",
}


def empty_stats():
    return {key: 0 for key in STAT_KEYS}


def add(stats, **increments):
    updated = empty_stats()
    updated.update(deepcopy(stats or {}))
    for key, amount in increments.items():
        if key not in updated:
            updated[key] = 0
        updated[key] += amount
    return updated


def record_kill(stats, rank_slug, rank_level):
    updated = add(stats, enemies_killed=1)
    if rank_level >= updated.get("highest_rank_kill_level", 0):
        updated["highest_rank_kill"] = rank_slug
        updated["highest_rank_kill_level"] = rank_level
    return updated


def increment(entity, **increments):
    entity.statistics = add(entity.statistics, **increments)


def increment_account(entity, **increments):
    lifetime = {LIFETIME_KEYS[key]: amount for key, amount in increments.items() if key in LIFETIME_KEYS}
    increment(entity, **increments, **lifetime)