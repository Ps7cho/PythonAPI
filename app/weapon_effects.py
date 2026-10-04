"""Catalog weapon effects, immutable generation rolls, and shared combat execution."""
from copy import deepcopy
from random import SystemRandom

from sqlalchemy import select

from app.ability_design import validate_chain
from app.afflictions import resolve_operations
from app.models import Ability, StatusEffect, Weapon, WeaponDefinition, WeaponEffect, WeaponEffectPool, WeaponType


def validate_ability_role(status_slug, operations):
    if status_slug or any(op.get('op') == 'apply' for op in operations):
        raise ValueError('Affliction application belongs to weapon effects. Abilities may exploit, transform, or recover afflictions.')


def validate_effect(db, effect):
    if type(effect.proc_chance_percent) is not int or not 0 <= effect.proc_chance_percent <= 100:
        raise ValueError('Effect proc chance must be an integer from 0 to 100.')
    if effect.recipient not in ('targets', 'self'):
        raise ValueError('Afflictions must apply to struck targets or self.')
    if not isinstance(effect.allowed_weapon_tags, list) or any(not isinstance(t, str) or not t or len(t) > 100 for t in effect.allowed_weapon_tags):
        raise ValueError('Weapon tags must be a list of nonempty strings.')
    if not isinstance(effect.affliction_ops, list) or len(effect.affliction_ops) > 16:
        raise ValueError('A weapon effect supports up to 16 affliction applications.')
    for op in effect.affliction_ops:
        if not isinstance(op, dict) or op.get('op') != 'apply' or not db.get(StatusEffect, op.get('affliction')):
            raise ValueError('Weapon applications require Apply and an existing affliction.')
    resolve_operations(effect, db)
    effect.effect_chain = validate_chain(effect.effect_chain, 'enemy')
    for step in effect.effect_chain:
        if step.get('ability_slug') and not db.scalar(select(Ability.id).where(Ability.slug == step['ability_slug'])):
            raise ValueError('Modifier references an unknown ability.')
    if not effect.affliction_ops and not effect.effect_chain:
        raise ValueError('Add an affliction application or follow-up effect.')


def validate_pool(db, pool):
    if type(pool.chance_percent) is not int or not 0 <= pool.chance_percent <= 100:
        raise ValueError('Pool chance must be an integer from 0 to 100.')
    if any(type(n) is not int for n in (pool.min_effects, pool.max_effects)) or not 0 <= pool.min_effects <= pool.max_effects <= 4:
        raise ValueError('Effect counts must satisfy 0 <= minimum <= maximum <= 4.')
    if not isinstance(pool.entries, list) or len(pool.entries) > 200:
        raise ValueError('A pool supports up to 200 weighted effects.')
    seen = set()
    for item in pool.entries:
        if not isinstance(item, dict) or set(item) != {'effect_slug', 'weight'}:
            raise ValueError('Pool entries need effect_slug and weight.')
        slug = item['effect_slug']
        if not isinstance(slug, str) or slug in seen or not db.get(WeaponEffect, slug):
            raise ValueError('Pool effects must exist and cannot be repeated.')
        if type(item['weight']) is not int or not 1 <= item['weight'] <= 10000:
            raise ValueError('Effect weights must be integers from 1 to 10000.')
        seen.add(slug)
    if pool.chance_percent and pool.max_effects and not pool.entries:
        raise ValueError('An active effect pool needs at least one entry.')


def snapshot_effect(db, effect):
    return dict(slug=effect.slug, name=effect.name, description=effect.description,
                proc_chance_percent=effect.proc_chance_percent, recipient=effect.recipient,
                affliction_ops=resolve_operations(effect, db), effect_chain=deepcopy(effect.effect_chain))


def pool_snapshot(db, weapon_type_slug, *, definition_slug=None, pool_slug=None):
    """Freeze candidates at generation/departure, before any loot roll."""
    kind = db.get(WeaponType, weapon_type_slug)
    blueprint = db.get(WeaponDefinition, definition_slug) if definition_slug else None
    if definition_slug and (not blueprint or blueprint.weapon_type_slug != weapon_type_slug):
        raise ValueError('Weapon blueprint does not match its weapon type.')
    slug = pool_slug or (blueprint.effect_pool_slug if blueprint else None) or (kind.effect_pool_slug if kind else None)
    pool = db.get(WeaponEffectPool, slug) if slug else None
    if not pool:
        return None
    entries = []
    for entry in pool.entries:
        effect = db.get(WeaponEffect, entry['effect_slug'])
        if effect and (not effect.allowed_weapon_tags or set(effect.allowed_weapon_tags).intersection(kind.tags)):
            entries.append(dict(weight=entry['weight'], effect=snapshot_effect(db, effect)))
    return dict(slug=pool.slug, chance_percent=pool.chance_percent, min_effects=pool.min_effects,
                max_effects=pool.max_effects, entries=entries)


def roll_effects(pool, rng=None):
    if not pool or not pool['entries'] or not pool['max_effects']:
        return []
    rng = rng or SystemRandom()
    if rng.randrange(100) >= pool['chance_percent']:
        return []
    candidates = list(pool['entries'])
    count = min(len(candidates), rng.randint(pool['min_effects'], pool['max_effects']))
    result = []
    for _ in range(count):
        entry = rng.choices(candidates, weights=[item['weight'] for item in candidates], k=1)[0]
        result.append(deepcopy(entry['effect']))
        candidates.remove(entry)
    return result


def create_weapon(db, *, adventurer_id, weapon_type_slug, name, base_damage, required_rank='iron',
                  effects=None, effect_pool_slug=None, weapon_definition_slug=None, rarity='common'):
    if effects is None:
        effects = roll_effects(pool_snapshot(db, weapon_type_slug, definition_slug=weapon_definition_slug, pool_slug=effect_pool_slug))
    weapon = Weapon(adventurer_id=adventurer_id, weapon_type_slug=weapon_type_slug, name=name,
                    weapon_definition_slug=weapon_definition_slug,
                    base_damage=base_damage, required_rank=required_rank, effects=deepcopy(effects), rarity=rarity)
    db.add(weapon)
    return weapon


def execute_weapon_effects(actor, ability, combatants, primary, turn, rng, before):
    """Weapon procs share the parent cast's copies, hit results, and RNG; no extra cast."""
    from app.combat import CombatAbility, execute_effect_chain, roll_chance, InvalidCombatAction
    from app.afflictions import execute
    if not ability.requires_weapon or ability.effect != 'damage':
        return []
    landed = [r for r in primary if r['effect'] == 'damage' and not r.get('dodged')]
    if not landed:
        return []
    weapon = actor.get('equipped_weapon') or {}
    effects = weapon.get('effects') or []
    if len(effects) > 4:
        raise InvalidCombatAction('A weapon supports at most four effects.')
    results = []
    for effect in effects:
        if not roll_chance(effect['proc_chance_percent'], rng):
            continue
        proc = CombatAbility(slug='weapon:' + str(weapon.get('id', weapon.get('weapon_type', 'natural'))) + ':' + effect['slug'],
                             name=effect['name'], effect='affliction', target_type='enemy', max_targets=None,
                             affliction_ops=effect.get('affliction_ops', []))
        targets = [actor] if effect.get('recipient') == 'self' else [combatants[r['target_id']] for r in landed]
        results.extend(execute(actor, proc, targets, turn=turn, before=deepcopy(before), dodged=set()))
        chain = validate_chain(effect.get('effect_chain', []), 'enemy')
        results.extend(execute_effect_chain(actor, proc, chain, combatants,
                                             [r['target_id'] for r in landed], landed, turn, rng))
    return results
