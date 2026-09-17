"""Read-only content inspection; never roll encounters or mutate game state."""
from datetime import datetime, timezone

from sqlalchemy import select

from app import models
from app.attributes import DESCRIPTIONS
from app.afflictions import definition
from app.orbs import options
from app.progression import level_cost, level_floor
from app.quest_templates import read_templates


def gameplay_catalog(db, abilities):
    # Only content tables: no accounts, characters, inventory, or run state.
    catalogs = {
        'enemies': models.Enemy,
        'enemy_abilities': models.EnemyAbility,
        'enemy_weapons': models.EnemyWeapon,
        'weapon_types': models.WeaponType,
        'consumables': models.Consumable,
        'essences': models.EssenceDefinition,
        'ranks': models.RankDefinition,
        'rest_policies': models.RestPolicy,
        'entity_types': models.EntityType,
        'loot_types': models.LootType,
    }
    result = {key: [dict(row) for row in db.execute(
        select(*model.__table__.columns).order_by(*model.__table__.primary_key.columns)
    ).mappings()] for key, model in catalogs.items()}
    starter_ids = {str(value) for value in db.scalars(select(models.Ability.id).where(models.Ability.starter.is_(True)))}
    abilities = [dict(ability, starter=ability['id'] in starter_ids) for ability in abilities]
    result.update(
        generated_at=datetime.now(timezone.utc).isoformat(),
        abilities=abilities,
        orb_outcomes=options(db),
        afflictions=[definition(row) for row in db.scalars(select(models.StatusEffect).order_by(models.StatusEffect.slug))],
        quests=read_templates(db, db.scalars(select(models.QuestTemplate).order_by(models.QuestTemplate.name)).all()),
        # Preserve source rules separately: raid previews may use saved rotation loot.
        quest_definitions=[dict(slug=row.slug, name=row.name, journey=row.journey)
                           for row in db.scalars(select(models.QuestTemplate).order_by(models.QuestTemplate.name))],
        attributes=DESCRIPTIONS,
        levels=[dict(level=level, lifetime_xp=level_floor(level), next_level_cost=level_cost(level))
                for level in range(1, 81)],
    )
    return result
