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
    quests = read_templates(db, db.scalars(select(models.QuestTemplate).order_by(models.QuestTemplate.name)).all())
    villages = [dict(row) for row in db.execute(select(models.Village.__table__.columns).order_by(models.Village.slug)).mappings()]
    shops = [dict(row) for row in db.execute(select(models.Shop.__table__.columns).order_by(models.Shop.slug)).mappings()]
    shop_tables = [dict(row) for row in db.execute(select(models.ShopTable.__table__.columns).order_by(models.ShopTable.slug)).mappings()]
    from app.shop import catalog as shop_catalog
    stock = shop_catalog(db)
    result.update(
        generated_at=datetime.now(timezone.utc).isoformat(),
        abilities=abilities,
        orb_outcomes=options(db),
        afflictions=[definition(row) for row in db.scalars(select(models.StatusEffect).order_by(models.StatusEffect.slug))],
        quests=quests,
        villages=villages,
        shops=shops,
        shop_tables=shop_tables,
        # Preserve source rules separately: raid previews may use saved rotation loot.
        quest_definitions=[dict(slug=row.slug, name=row.name, journey=row.journey)
                           for row in db.scalars(select(models.QuestTemplate).order_by(models.QuestTemplate.name))],
        attributes=DESCRIPTIONS,
        levels=[dict(level=level, lifetime_xp=level_floor(level), next_level_cost=level_cost(level))
                for level in range(1, 81)],
    )
    return result
