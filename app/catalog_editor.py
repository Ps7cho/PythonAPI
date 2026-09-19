"""Authorized catalog design, with optimistic concurrency and an event audit."""
from copy import deepcopy
from hashlib import sha256
import json
import math
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import models as m
from app.auth import current_user
from app.database import get_db

CATALOGS = {
    'abilities': m.Ability, 'quests': m.QuestTemplate, 'enemies': m.Enemy,
    'enemy_abilities': m.EnemyAbility, 'enemy_weapons': m.EnemyWeapon,
    'weapon_types': m.WeaponType, 'weapon_definitions': m.WeaponDefinition, 'consumables': m.Consumable,
    'villages': m.Village, 'shops': m.Shop, 'shop_tables': m.ShopTable,
    'essences': m.EssenceDefinition, 'orb_outcomes': m.OrbOutcome,
    'afflictions': m.StatusEffect, 'entity_types': m.EntityType,
    'ranks': m.RankDefinition, 'rest_policies': m.RestPolicy, 'loot_types': m.LootType,
    'gear_definitions': m.GearDefinition,
}
ENUMS = {
    ('abilities', 'effect_type'): ['damage', 'guard', 'heal', 'buff', 'shield', 'cleanse', 'evade', 'affliction'],
    ('abilities', 'target_type'): ['self', 'ally', 'enemy', 'party'],
    ('abilities', 'cooldown_type'): ['turn', 'minutes', 'hours'],
    ('consumables', 'effect'): ['heal', 'buff', 'cleanse', 'essence', 'orb'],
}


def allowed(db, user):
    return getattr(user, 'account_type', 'player') == 'developer'


def columns(model):
    return [c for c in model.__table__.columns if c.name not in ('created_at', 'updated_at')]


def values(row):
    return jsonable_encoder({c.name: getattr(row, c.name) for c in columns(type(row))})


def revision(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def descriptor(row):
    value = values(row)
    return dict(key={c.name: value[c.name] for c in row.__table__.primary_key}, values=value, revision=revision(value))


def editor_catalog(db, user):
    if not allowed(db, user):
        return {'can_edit': False}
    catalogs = {}
    for name, model in CATALOGS.items():
        fields = []
        for column in columns(model):
            pytype = column.type.python_type
            kind = 'json' if pytype in (dict, list) else 'boolean' if pytype is bool else 'integer' if pytype is int else 'number' if pytype is float else 'text'
            fields.append(dict(name=column.name, type=kind, nullable=column.nullable,
                               primary_key=column.primary_key, immutable=column.primary_key or column.name == 'slug', choices=ENUMS.get((name, column.name))))
        catalogs[name] = dict(fields=fields, records=[descriptor(row) for row in db.scalars(select(model).order_by(*model.__table__.primary_key))])
    return dict(can_edit=True, catalogs=catalogs)


class Edit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    catalog: str
    key: dict
    values: dict
    expected_revision: str | None = None
    create: bool = False
    validate_only: bool = False


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_values(model, data, catalog):
    fields = {c.name: c for c in columns(model)}
    require(set(data) == set(fields), 'Supply exactly the editable fields shown in this definition.')
    require(len(json.dumps(data, allow_nan=False)) <= 200_000, 'Definition is too large (maximum 200 KB).')
    parsed = deepcopy(data)
    for name, c in fields.items():
        value = data[name]
        if value is None:
            require(c.nullable, f'{name} cannot be null.')
            continue
        kind = c.type.python_type
        if kind is UUID:
            parsed[name] = UUID(str(value))
        elif kind is float:
            require(type(value) in (int, float) and math.isfinite(value), f'{name} must be a finite number.')
        elif kind in (dict, list):
            require(type(value) in (dict, list), f'{name} must be JSON object or array.')
        else:
            require(type(value) is kind, f'{name} must be {kind.__name__}.')
        if kind is str:
            require(len(value) <= (c.type.length or (10000 if name == 'description' else 200)), f'{name} is too long.')
            if name in ('name', 'slug'):
                require(bool(value.strip()), f'{name} cannot be empty.')
        if (catalog, name) in ENUMS:
            require(value in ENUMS[catalog, name], f'Unsupported {name}.')
    return parsed


def validate_definition(db, row):
    from app.afflictions import resolve_operations
    from app.enemies import EnemyRead
    from app.journeys import JourneyRules, RestRules
    from app.group_journeys import validate_group_catalog

    def number(name, low, high):
        value = getattr(row, name)
        require(value is not None and low <= value <= high, f'{name} must be between {low} and {high}.')

    def strings(value, name):
        require(isinstance(value, list) and all(isinstance(s, str) and len(s) <= 200 for s in value), f'{name} must be a list of strings.')

    # Check every foreign key explicitly (SQLite may not enforce them in tests).
    for column in columns(type(row)):
        value = getattr(row, column.name)
        for fk in column.foreign_keys:
            if value is not None:
                require(db.scalar(select(fk.column).where(fk.column == value)) is not None,
                        f'{column.name} references a missing {fk.column.table.name} definition.')
    if isinstance(row, m.Ability):
        number('power', 0, 10000); number('cooldown_value', 0, 10000); number('cost_value', 0, 10000)
        if row.damage_multiplier is not None: number('damage_multiplier', 0, 100)
        if row.max_targets is not None: number('max_targets', 1, 100)
        strings(row.allowed_weapon_tags, 'allowed_weapon_tags')
        require(isinstance(row.affliction_ops, list), 'affliction_ops must be an array.')
        for op in row.affliction_ops:
            require(isinstance(op, dict) and db.get(m.StatusEffect, op.get('affliction')) is not None, 'Each operation must reference an existing affliction.')
        resolve_operations(row)
    elif isinstance(row, m.Enemy):
        EnemyRead.model_validate(row)
        require(all(type(v) is int and 0 <= v <= 10000 for v in row.attributes.values()), 'Enemy attributes must be nonnegative integers.')
        require(row.enemy_type == 'boss' or db.get(m.EntityType, row.enemy_type) is not None, 'Unknown enemy entity type.')
    elif isinstance(row, m.QuestTemplate):
        number('difficulty', 0, 100); number('min_encounters', 1, 100); number('max_encounters', row.min_encounters, 100)
        strings(row.enemy_pool, 'enemy_pool'); strings(row.possible_rewards, 'possible_rewards')
        if row.journey == {}:
            return  # Legacy catalog-only quest; it does not generate a journey.
        rules = JourneyRules.model_validate(row.journey)
        require(not set(row.journey) - set(JourneyRules.model_fields), 'Journey contains unsupported top-level rule fields.')
        require(db.get(m.RankDefinition, rules.required_rank) is not None, 'Unknown required rank.')
        if rules.camp_rest: require(db.get(m.RestPolicy, rules.camp_rest) is not None, 'Unknown camp rest policy.')
        for stage in rules.stages:
            for group in stage.groups:
                require(1 <= len(group) <= 6 and all(db.get(m.Enemy, slug) is not None for slug in group), 'Each enemy group needs 1–6 existing enemies.')
        if rules.encounter_groups:
            require(rules.max_rests is not None, 'Grouped quests require a rest budget.')
            validate_group_catalog(db, rules.model_dump())
        if rules.raid:
            from app.raids import RaidRules
            raid = RaidRules.model_validate(rules.raid)
            require(all(db.get(m.Enemy, s) and db.get(m.Enemy, s).enemy_type == 'boss' for s in raid.bosses), 'Raid checkpoints require existing boss enemies.')
            require(not any(db.get(m.Enemy, s).enemy_type == 'boss' for stage in rules.stages for group in stage.groups for s in group), 'Raid bosses belong in checkpoints, not ordinary stage groups.')
        row.journey = rules.model_dump()
    elif isinstance(row, m.Consumable):
        number('power', 0, 100)
        if db.get(m.EssenceDefinition, row.slug): require(row.effect == 'essence', 'This item has an essence definition and must keep the essence effect.')
        if db.scalar(select(m.OrbOutcome.orb_slug).where(m.OrbOutcome.orb_slug == row.slug).limit(1)):
            require(row.effect == 'orb', 'This item has orb recipes and must keep the orb effect.')
    elif isinstance(row, m.EnemyAbility):
        number('weight', 1, 10000); number('priority', 0, 10000)
    elif isinstance(row, m.WeaponType):
        strings(row.tags, 'tags')
    elif isinstance(row, m.WeaponDefinition):
        number('base_damage', 1, 1000)
    elif isinstance(row, m.EntityType):
        require(isinstance(row.status_resistances, dict), 'Resistances must be an object.')
        for slug, value in row.status_resistances.items():
            require(db.get(m.StatusEffect, slug) is not None and type(value) is int and 0 <= value <= 100, 'Resistances need an existing affliction and an integer from 0 to 100.')
    elif isinstance(row, m.StatusEffect):
        number('damage', 1, 10000); number('duration', 1, 100); number('max_stacks', 1, 100)
        require(isinstance(row.rules, dict), 'Affliction rules must be an object.')
        require(set(row.rules) <= {'harmful', 'periodic'}, 'Supported affliction rules are harmful and periodic.')
        require(type(row.rules.get('harmful', True)) is bool, 'harmful must be a boolean.')
        require(row.rules.get('periodic', 'damage') in ('damage', 'none'), 'periodic must be damage or none.')
    elif isinstance(row, m.RestPolicy):
        row.settings = RestRules.model_validate(row.settings).model_dump()
    elif isinstance(row, m.EssenceDefinition):
        require(isinstance(row.powers, dict), 'Essence powers must be an object.')
        require(db.get(m.Consumable, row.consumable_slug).effect == 'essence', 'Essence definitions require an essence item.')
    elif isinstance(row, m.OrbOutcome):
        require(db.get(m.Consumable, row.orb_slug).effect == 'orb', 'Orb recipes require an orb item.')
    elif isinstance(row, m.RankDefinition):
        number('min_level', 1, 1000); number('ability_slots', 1, 32); number('passive_slots', 0, 32)
        strings(row.unlocks, 'unlocks')
        if row.min_level != 1:
            require(db.scalar(select(m.RankDefinition.slug).where(m.RankDefinition.slug != row.slug, m.RankDefinition.min_level == 1)) is not None, 'A starting rank at level 1 is required.')
        if row.equipment_reward:
            require(isinstance(row.equipment_reward, dict), 'Equipment reward must be an object.')
            reward = row.equipment_reward
            require(db.get(m.WeaponType, reward.get('weapon_type_slug')) is not None, 'Reward needs an existing weapon type.')
            require(isinstance(reward.get('name'), str) and type(reward.get('base_damage')) is int and reward['base_damage'] > 0, 'Reward needs a name and positive base_damage.')


router = APIRouter(prefix='/api/catalog-editor', tags=['catalog editor'])


@router.post('')
def edit_catalog(edit: Edit, db: Session = Depends(get_db), user: m.User = Depends(current_user)):
    if not allowed(db, user):
        raise HTTPException(403, 'This account cannot edit gameplay definitions.')
    model = CATALOGS.get(edit.catalog)
    if model is None:
        raise HTTPException(422, 'Unknown editable catalog.')
    try:
        parsed = validate_values(model, edit.values, edit.catalog)
        keys = {c.name: parsed[c.name] for c in model.__table__.primary_key}
        require(jsonable_encoder(keys) == edit.key, 'Definition keys cannot change while editing. Use Create a copy instead.')
        row = db.scalar(select(model).where(*(getattr(model, key) == value for key, value in keys.items())).with_for_update().execution_options(populate_existing=True))
        if edit.create:
            if row is not None: raise HTTPException(409, 'A definition with this key already exists.')
            row = model(**parsed); before = None; db.add(row)
        else:
            if row is None: raise HTTPException(404, 'Definition no longer exists.')
            before = values(row)
            if edit.expected_revision != revision(before):
                raise HTTPException(409, 'This definition changed since you loaded it. Reload and review your draft against the latest version.')
            require('slug' not in before or parsed['slug'] == before['slug'], 'Stable slugs cannot change. Create a copy with a new slug instead.')
            if isinstance(row, m.QuestTemplate):
                require(not before['journey'] or bool(parsed['journey']), 'A playable quest cannot be changed to an empty legacy definition.')
            for name, value in parsed.items(): setattr(row, name, value)
        with db.no_autoflush:
            validate_definition(db, row)
        db.flush()
        result = descriptor(row)
        if edit.validate_only:
            db.rollback()
            return dict(valid=True, record=result)
        db.add(m.GameEvent(event_type='catalog_edited', payload=dict(user_id=str(user.id), catalog=edit.catalog, key=edit.key, before=before, after=result['values'])))
        db.commit()
        return dict(saved=True, record=result)
    except IntegrityError:
        db.rollback()
        raise HTTPException(422, 'A unique name, key, or referenced definition conflicts with this edit.')
    except (ValueError, TypeError, KeyError) as error:
        db.rollback()
        raise HTTPException(422, str(error))
