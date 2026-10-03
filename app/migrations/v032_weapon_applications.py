"""One-time content conversion after legacy catalog bootstrapping has completed."""
from copy import deepcopy
from random import Random

from sqlalchemy import select, text
from sqlalchemy.orm import Session


def convert_definition(db, slug, name, description, values):
    from app.models import StatusEffect, WeaponEffect
    operations = deepcopy(values.get('affliction_ops') or [])
    applications = [op for op in operations if op.get('op') == 'apply']
    if values.get('status_effect_slug'):
        applications.insert(0, dict(op='apply', affliction=values['status_effect_slug'], stacks=1))
    if not applications:
        return None
    effect_slug = 'application-' + slug
    if not db.get(WeaponEffect, effect_slug):
        db.add(WeaponEffect(slug=effect_slug, name=name + ' weapon effect', description=description or '',
                            proc_chance_percent=100, allowed_weapon_tags=values.get('allowed_weapon_tags') or [],
                            recipient='targets' if values.get('target_type', 'enemy') == 'enemy' else 'self',
                            affliction_ops=applications, effect_chain=[]))
        db.flush()
    remaining = [op for op in operations if op.get('op') != 'apply']
    # A former same-cast threshold trigger now exploits stacks established by a weapon.
    for op in remaining:
        if op['op'] == 'trigger':
            op.update(op='exploit', stacks=op.get('threshold', 3))
    if values.get('target_type', 'enemy') == 'enemy' and (not remaining or values.get('status_effect_slug')):
        for application in applications:
            affliction = db.get(StatusEffect, application['affliction'])
            remaining.append(dict(op='exploit', affliction=application['affliction'],
                                  stacks=affliction.max_stacks, power=affliction.damage, effect='damage'))
    elif values.get('target_type', 'enemy') != 'enemy' and not remaining:
        values.update(effect_type='heal', power=20, damage_multiplier=None, requires_weapon=False)
    values.update(status_effect_slug=None, affliction_ops=remaining)
    return effect_slug


def upgrade(conn, *, backfill=True):
    from app.models import (Ability, AbilityArchetype, AuctionListing, EnemyAbility, EnemyWeapon,
                            Weapon, WeaponEffectPool, WeaponType)
    from app.weapon_effects import pool_snapshot, roll_effects
    with Session(bind=conn) as db:
        migrated = []
        for ability in db.scalars(select(Ability)).all():
            values = {key: getattr(ability, key) for key in ('status_effect_slug', 'affliction_ops', 'target_type',
                       'allowed_weapon_tags', 'effect_type', 'power', 'damage_multiplier', 'requires_weapon')}
            effect_slug = convert_definition(db, ability.slug, ability.name, ability.description, values)
            if not effect_slug:
                continue
            migrated.append(effect_slug)
            for key, value in values.items():
                setattr(ability, key, value)
            ability.description = 'Use afflictions established by weapon effects. ' + '; '.join(
                f"{op['op'].title()} {op['affliction']}" for op in ability.affliction_ops) + '.'
            enemy_slugs = db.scalars(select(EnemyAbility.enemy_slug).where(EnemyAbility.ability_id == ability.id)).all()
            if enemy_slugs and ability.effect_type == 'damage':
                ability.requires_weapon = True
                ability.allowed_weapon_tags = []
                for enemy_slug in enemy_slugs:
                    assignment = db.get(EnemyWeapon, enemy_slug)
                    if not assignment:
                        if not db.get(WeaponType, 'natural'):
                            db.add(WeaponType(slug='natural', name='Natural Weapon', tags=['weapon', 'melee', 'natural']))
                            db.flush()
                        assignment = EnemyWeapon(enemy_slug=enemy_slug, weapon_type_slug='natural', effect_slugs=[])
                        db.add(assignment)
                    assignment.effect_slugs = list(dict.fromkeys([*(assignment.effect_slugs or []), effect_slug]))
        for archetype in db.scalars(select(AbilityArchetype)).all():
            values = deepcopy(archetype.definition)
            effect_slug = convert_definition(db, 'archetype-' + archetype.slug, archetype.name, archetype.description, values)
            if effect_slug:
                migrated.append(effect_slug)
                archetype.definition = values
        if not backfill and not migrated:
            db.flush()
            return
        pool = db.get(WeaponEffectPool, 'affliction-weapons')
        if not pool:
            pool = WeaponEffectPool(slug='affliction-weapons', name='Affliction weapons',
                                    description='Migrated affliction applications. A weapon may roll one effect or remain plain.',
                                    chance_percent=35, min_effects=1, max_effects=1,
                                    entries=[dict(effect_slug=slug, weight=1) for slug in dict.fromkeys(migrated)])
            db.add(pool)
        else:
            present = {item['effect_slug'] for item in pool.entries}
            pool.entries = [*pool.entries, *[dict(effect_slug=slug, weight=1) for slug in dict.fromkeys(migrated) if slug not in present]]
        db.flush()
        if not backfill:
            return
        for kind in db.scalars(select(WeaponType)):
            if kind.effect_pool_slug is None and kind.slug not in ('shield', 'natural'):
                kind.effect_pool_slug = pool.slug
        db.flush()
        for weapon in db.scalars(select(Weapon)):
            if not weapon.effects:
                weapon.effects = roll_effects(pool_snapshot(db, weapon.weapon_type_slug), Random(str(weapon.id)))
        for listing in db.scalars(select(AuctionListing)):
            if listing.item.get('item_type') == 'weapon' and 'effects' not in listing.item:
                listing.item = {**listing.item, 'effects': roll_effects(pool_snapshot(db, listing.item['weapon_type']), Random(listing.item['id']))}
        db.flush()


def migrate_seeded_content(engine):
    """Run once after seeds so fresh installs and existing databases convert the same catalog."""
    with engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            conn.execute(text('SELECT pg_advisory_xact_lock(310032)'))
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='032_weapon_applications'")).first():
            upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('032_weapon_applications')"))
        else:
            # A deleted legacy seed may be recreated during bootstrap. Normalize only
            # newly introduced application definitions; never reroll existing weapons.
            upgrade(conn, backfill=False)
