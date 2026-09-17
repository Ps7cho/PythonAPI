"""Committed encounter groups and extraction loot, using existing journey snapshots."""
from copy import deepcopy
from random import choice, choices, randint, Random, SystemRandom
from typing import Literal
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from app.models import Adventurer, Weapon, WeaponType, Consumable


class LootDrop(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    weapon_type_slug: str | None = None
    base_damage: int | None = Field(default=None, ge=1, le=1000)
    consumable_slug: str | None = None
    quantity: int = Field(default=1, strict=True, ge=1, le=99)

    @model_validator(mode="after")
    def item_kind(self):
        if self.consumable_slug:
            if self.weapon_type_slug or self.base_damage is not None:
                raise ValueError("Loot must be a weapon or a consumable, not both")
        elif not self.weapon_type_slug or self.base_damage is None:
            raise ValueError("Weapon loot requires a type and damage")
        return self
    weight: int = Field(default=1, ge=1)


class LootTier(BaseModel):
    drop_chance_percent: int = Field(default=100, strict=True, ge=0, le=100)
    name: str
    drops: list[LootDrop] = Field(min_length=1, max_length=640)


class GroupRules(BaseModel):
    shared_orb_essence_pool: bool = False
    loot_policy: str = 'legacy'
    loot_rng: Literal["legacy", "run"] = "legacy"
    size: int = Field(default=2, ge=2, le=5)
    min_size: int | None = Field(default=None, ge=2, le=5)
    max_size: int | None = Field(default=None, ge=2, le=5)

    @model_validator(mode="after")
    def group_range(self):
        if (self.min_size is None) != (self.max_size is None):
            raise ValueError("Both group size bounds must be provided")
        if self.min_size is not None and self.min_size > self.max_size:
            raise ValueError("Group size bounds are reversed")
        return self
    health_step_percent: int = Field(ge=0, le=100)
    power_step_percent: int = Field(ge=0, le=100)
    max_threat_percent: int = Field(ge=100, le=500)
    loot_tiers: list[LootTier] = Field(min_length=1, max_length=10)


def validate_group_catalog(db, settings):
    rules = settings.get("encounter_groups")
    if not rules:
        return
    types = {d['weapon_type_slug'] for t in rules['loot_tiers'] for d in t['drops'] if d.get('weapon_type_slug')}
    items = {d['consumable_slug'] for t in rules['loot_tiers'] for d in t['drops'] if d.get('consumable_slug')}
    if set(db.scalars(select(Consumable.slug).where(Consumable.slug.in_(items)))) != items:
        raise HTTPException(409, 'Group loot references an unavailable consumable.')
    if set(db.scalars(select(WeaponType.slug).where(WeaponType.slug.in_(types)))) != types:
        raise HTTPException(409, "Group loot references an unavailable weapon type.")


def annotate_lengths(plan, lengths):
    start = 0
    for number, size in enumerate(lengths, 1):
        for i in range(size):
            plan[start+i].update(group_number=number, group_battle=i+1, group_size=size, group_end=i == size-1)
        start += size


def bounded_lengths(count, low, high):
    # Choose a legal partition without leaving an undersized final group.
    possible = {0: []}
    for total in range(1, count + 1):
        options = [n for n in range(low, high + 1) if total-n in possible]
        if options:
            n = choice(options)
            possible[total] = [*possible[total-n], n]
    if count not in possible:
        raise HTTPException(409, "Route cannot be partitioned into legal encounter groups.")
    return possible[count]


def annotate_groups(plan, size, low=None, high=None):
    if low is not None:
        annotate_lengths(plan, bounded_lengths(len(plan), low, high))
        return
    start, number = 0, 1
    while start < len(plan):
        end = min(start + size, len(plan))
        # Avoid a one-battle final group on routes with an odd number of stages.
        if len(plan) - end == 1:
            end += 1
        for i in range(start, end):
            plan[i].update(group_number=number, group_battle=i - start + 1,
                           group_size=end - start, group_end=i == end - 1)
        start, number = end, number + 1


def threat(settings, number):
    rules = settings['encounter_groups']
    return {key: min(rules['max_threat_percent'], 100 + max(0, number - 1) * rules[key + '_step_percent'])
            for key in ('health', 'power')}


def loot_tier(rules, progress):
    """Select the saved table using the server-owned progression counter."""
    counter = 'pushes' if rules.get('loot_policy') == 'push-rewards-v1' else 'groups_cleared'
    return rules['loot_tiers'][min(progress.get(counter, 0), len(rules['loot_tiers']) - 1)]


def group_view(quest, entry):
    settings = quest.rewards.get('journey', {})
    rules = settings.get('encounter_groups')
    if not rules:
        return None
    progress = quest.rewards.get('journey_progress', {})
    cleared = progress.get('groups_cleared', 0)
    tier = loot_tier(rules, progress)
    pushed_tier = loot_tier(rules, {**progress, 'pushes': progress.get('pushes', 0) + 1})
    return {'number': entry['group_number'], 'battle': entry['group_battle'], 'size': entry['group_size'],
            'boundary': entry['group_end'], 'cleared': cleared,
            'stash': progress.get('loot_stash', []), 'loot_claimed': progress.get('loot_claimed', False),
            'next_loot_chance_percent': tier.get('drop_chance_percent', 100),
            'after_push_loot_chance_percent': pushed_tier.get('drop_chance_percent', 100),
            'after_push_loot_tier': pushed_tier['name'],
            'next_loot_tier': tier['name'], 'next_loot_options': [d['name'] for d in tier['drops']],
            'threat': threat(settings, entry['group_number']),
            'next_threat': threat(settings, entry['group_number'] + 1)}


def append_group(quest):
    settings = quest.rewards['journey']
    rules = settings['encounter_groups']
    size = randint(rules['min_size'], rules['max_size']) if rules.get('min_size') is not None else rules['size']
    number = quest.encounter_pool[-1]['group_number'] + 1
    additions = []
    for i in range(size):
        stage = choice(settings['stages'])
        additions.append({'encounter_id': str(uuid4()), 'enemy_slugs': choice(stage['groups']),
                          'description': stage['description'], 'group_number': number,
                          'group_battle': i + 1, 'group_size': size, 'group_end': i == size - 1})
    quest.encounter_pool = [*quest.encounter_pool, *additions]


def earn_group_loot(quest):
    settings = quest.rewards['journey']
    progress = deepcopy(quest.rewards.get('journey_progress', {}))
    cleared = progress.get('groups_cleared', 0)
    tier = loot_tier(settings['encounter_groups'], progress)
    rng = Random(settings['raid_seed'] + ':loot:' + str(cleared)) if settings.get('raid_seed') and settings['encounter_groups'].get('loot_rng') != 'run' else SystemRandom()
    drop = roll_loot(tier, rng)
    if drop is not None:
        drop.pop('weight', None)
        drop.update(required_rank=settings.get('required_rank', 'iron'), tier=tier['name'])
        progress['loot_stash'] = [*progress.get('loot_stash', []), drop]
    progress.setdefault('loot_rolls', []).append({'group': cleared + 1, 'tier': tier['name'],
        'drop_chance_percent': tier.get('drop_chance_percent', 100), 'item': deepcopy(drop)})
    progress['groups_cleared'] = cleared + 1
    quest.rewards = {**quest.rewards, 'journey_progress': progress}
    if drop is None:
        return "Group cleared. No item dropped this time; battle rewards are still banked."
    return f"Group cleared! {drop['name']} added to the loot at risk. Return safely to claim it for each survivor."


def claim_group_loot(db, quest, participants):
    progress = deepcopy(quest.rewards.get('journey_progress', {}))
    if progress.get('loot_claimed'):
        raise HTTPException(409, "Loot already claimed.")
    from app.progression import award_experience
    journey = quest.rewards['journey']
    bonus_gold = progress.get('pushes', 0) * journey.get('push_gold', 0)
    bonus_xp = progress.get('pushes', 0) * journey.get('push_experience', 0)
    events = [f"Safe return: each survivor claims {len(progress.get('loot_stash', []))} items, {bonus_gold} bonus gold and {bonus_xp} bonus XP."]
    heroes = db.scalars(select(Adventurer).where(Adventurer.id.in_([UUID(p['id']) for p in participants]))
                        .order_by(Adventurer.id).with_for_update().execution_options(populate_existing=True)).all()
    for hero in heroes:
        gains = progress.get('run_gains', {}).get(str(hero.id), {})
        if hero.is_alive and hero.health > 0:
            hero.gold += bonus_gold + gains.get('gold', 0)
            events.extend(award_experience(db, hero, bonus_xp + gains.get('experience', 0)))
            for drop in progress.get('loot_stash', []):
                if drop.get('consumable_slug'):
                    from app.consumables import grant
                    grant(db, hero.id, drop['consumable_slug'], drop.get('quantity', 1))
                else:
                    db.add(Weapon(adventurer_id=hero.id, **{k: drop[k] for k in ('name', 'weapon_type_slug', 'base_damage', 'required_rank')}))
        elif journey.get('death_policy') == 'rescue_on_return':
            hero.health = 1
            hero.is_alive = True
            hero.combat_cooldowns = {'turns': {}, 'ready_at': {}}
            hero.combat_statuses = []
            events.append(f"{hero.name} was rescued at 1 HP. Their run gains were lost; pre-run progression and gear are safe.")
    progress['settled_run_gains'] = progress.pop('run_gains', {})
    progress['claimed_loot'] = progress.pop('loot_stash', [])
    progress['loot_claimed'] = True
    quest.rewards = {**quest.rewards, 'journey_progress': progress}
    return events


def bank_run_gains(quest, hero_id, gold, experience):
    progress = deepcopy(quest.rewards.get('journey_progress', {}))
    gains = progress.setdefault('run_gains', {}).setdefault(str(hero_id), {'gold': 0, 'experience': 0})
    gains['gold'] += gold
    gains['experience'] += experience
    quest.rewards = {**quest.rewards, 'journey_progress': progress}


def roll_loot(tier, rng):
    """One chance to enter the weighted table; no rerolls on read or extraction."""
    chance = tier.get('drop_chance_percent', 100)
    if chance < 100 and rng.randrange(100) >= chance:
        return None
    return deepcopy(rng.choices(tier['drops'], weights=[d['weight'] for d in tier['drops']], k=1)[0])
