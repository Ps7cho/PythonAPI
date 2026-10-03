"""Convert passive abilities into armor effects without replacing equipped gear."""
from copy import deepcopy
from sqlalchemy import select, delete, text
from sqlalchemy.orm import Session


def upgrade(conn):
    from app.models import (Ability, AbilityArchetype, ArmorEffect, GearDefinition, Gear, Enemy,
                            EnemyAbility, AdventurerAbility, EquippedAbility, OrbOutcome, ShopTable,
                            WeaponEffect, RankDefinition)
    from app.ability_design import TEMPLATE_FIELDS
    with Session(bind=conn) as db:
        passives = list(db.scalars(select(Ability).where(Ability.trigger_mode == 'on_hit')))
        ids = [a.id for a in passives]
        slugs = {a.slug for a in passives}
        for ability in passives:
            if not db.get(ArmorEffect, ability.slug):
                definition = {key: deepcopy(getattr(ability, key)) for key in TEMPLATE_FIELDS}
                db.add(ArmorEffect(slug=ability.slug, name=ability.name, description=ability.description, definition=definition))
                db.flush()
            slug = 'reaction-' + ability.slug
            armor = db.get(GearDefinition, slug)
            if armor is None:
                armor = GearDefinition(slug=slug, name=ability.name + ' Armor', slot='Hands' if ability.slug == 'riposte' else 'Chest',
                                       bonuses={'Defense':1}, required_rank='iron', price=100, effect_slugs=[ability.slug])
                db.add(armor); db.flush()
            # Preserve equipped choices as owned items; leave the current outfit intact.
            for hero_id in db.scalars(select(EquippedAbility.adventurer_id).where(EquippedAbility.ability_id == ability.id)):
                if not db.scalar(select(Gear.id).where(Gear.adventurer_id == hero_id, Gear.definition_slug == slug)):
                    db.add(Gear(adventurer_id=hero_id, definition_slug=slug))
            for enemy_slug in db.scalars(select(EnemyAbility.enemy_slug).where(EnemyAbility.ability_id == ability.id)):
                enemy = db.get(Enemy, enemy_slug)
                enemy.armor_slugs = list(dict.fromkeys([*(enemy.armor_slugs or []), slug]))
            for shop in db.scalars(select(ShopTable).where(ShopTable.category == 'gear', ShopTable.shop_slug == 'mosswood-market')):
                if not any(item.get('slug') == slug for item in shop.items):
                    shop.items = [*shop.items, dict(item_type='gear', slug=slug, name=armor.name, slot=armor.slot,
                                                  bonuses=armor.bonuses, price=armor.price, required_rank=armor.required_rank,
                                                  description=ability.description)]
        for model in (EquippedAbility, AdventurerAbility, EnemyAbility, OrbOutcome):
            db.execute(delete(model).where(model.ability_id.in_(ids)))
        # A modifier aimed at a removed passive must not turn into an all-ability modifier.
        for model in (Ability, ArmorEffect, WeaponEffect):
            for row in db.scalars(select(model)):
                values = row.definition if isinstance(row, ArmorEffect) else None
                chain = values.get('effect_chain', []) if values is not None else row.effect_chain
                kept = [step for step in chain if step.get('ability_slug') not in slugs]
                if len(kept) == len(chain):
                    continue
                if values is not None:
                    row.definition = {**values, 'effect_chain': kept}
                else:
                    row.effect_chain = kept
                if isinstance(row, Ability):
                    valid = {step['id'] for step in kept}
                    row.rank_upgrades = {rank: {key: value for key, value in dials.items()
                                               if ':' not in key or key.split(':', 1)[1] in valid}
                                         for rank, dials in row.rank_upgrades.items()}
        db.execute(delete(Ability).where(Ability.id.in_(ids)))
        for archetype in list(db.scalars(select(AbilityArchetype))):
            if archetype.definition.get('trigger_mode') == 'on_hit':
                slug = 'archetype-' + archetype.slug
                if not db.get(ArmorEffect, slug):
                    db.add(ArmorEffect(slug=slug, name=archetype.name, description=archetype.description, definition=deepcopy(archetype.definition)))
                for ability in db.scalars(select(Ability).where(Ability.archetype_slug == archetype.slug)):
                    ability.archetype_slug = None
                db.flush(); db.delete(archetype)
        for rank in db.scalars(select(RankDefinition)):
            rank.unlocks = [item for item in rank.unlocks or []
                            if item != 'Second passive slot (passive system planned)']
        db.flush()


def migrate_seeded_content(engine):
    with engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            conn.execute(text('SELECT pg_advisory_xact_lock(310032)'))
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='037_move_passives_to_armor'")).first():
            upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('037_move_passives_to_armor')"))
