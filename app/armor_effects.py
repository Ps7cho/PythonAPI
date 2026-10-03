"""Resolve equipped armor reactions through the shared ability executor."""
from dataclasses import asdict
from types import SimpleNamespace
from app.models import ArmorEffect, EquippedGear, GearDefinition
from app.loadouts import executable
from app.progression import ranks
from sqlalchemy import select

DEFAULTS = dict(effect_type='damage', target_type='enemy', power=10, damage_multiplier=None,
                requires_weapon=False, allowed_weapon_tags=[], cooldown_type='turn', cooldown_value=0,
                max_targets=1, duration_turns=3, guard_percent=60, effect_chain=[], rank_upgrades={},
                ability_type='attack', status_effect_slug=None, affliction_ops=[], strike_count=1,
                extra_strike_chance=0, max_extra_strikes=1, trigger_mode='on_hit', proc_chance_percent=100)


def resolve(db, definition, source, level=1):
    result = []
    for slug in definition.effect_slugs or []:
        effect = db.get(ArmorEffect, slug)
        if effect:
            values = {**DEFAULTS, **effect.definition, 'trigger_mode':'on_hit'}
            row = SimpleNamespace(**values, id='armor:'+source+':'+slug, slug=slug,
                                  name=effect.name, description=effect.description, status_effect=None)
            result.append(asdict(executable(row, ranks(db), level, db=db)))
    return result


def combat_armor_effects(db, hero):
    effects = []
    for entry in db.scalars(select(EquippedGear).where(EquippedGear.adventurer_id == hero.id).order_by(EquippedGear.slot)):
        if entry.gear.adventurer_id == hero.id:
            effects.extend(resolve(db, entry.gear.definition, str(entry.gear_id), hero.level))
    return effects


def enemy_armor_effects(db, enemy):
    return [effect for slug in (enemy.armor_slugs or [])
            if (definition := db.get(GearDefinition, slug)) is not None
            for effect in resolve(db, definition, enemy.slug+':'+slug)]
