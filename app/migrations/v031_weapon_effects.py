"""Reusable weapon effects/pools and immutable effects on individual weapons."""
from sqlalchemy import inspect, text


def upgrade(conn):
    from app.models import WeaponEffect, WeaponEffectPool
    for model in (WeaponEffect, WeaponEffectPool):
        model.__table__.create(conn, checkfirst=True)
    for table, name, ddl in [
        ('weapon_types', 'effect_pool_slug', 'VARCHAR REFERENCES weapon_effect_pools(slug)'),
        ('weapon_definitions', 'effect_pool_slug', 'VARCHAR REFERENCES weapon_effect_pools(slug)'),
        ('weapons', 'effects', "JSON NOT NULL DEFAULT '[]'"),
        ('enemy_weapons', 'effect_slugs', "JSON NOT NULL DEFAULT '[]'"),
    ]:
        if name not in {c['name'] for c in inspect(conn).get_columns(table)}:
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}'))
