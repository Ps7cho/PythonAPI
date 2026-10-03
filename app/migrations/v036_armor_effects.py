"""Armor reaction catalogs and equipment references."""
from sqlalchemy import inspect, text


def upgrade(conn):
    from app.models import ArmorEffect
    ArmorEffect.__table__.create(conn, checkfirst=True)
    for table, column in [('gear_definitions', 'effect_slugs'), ('enemies', 'armor_slugs')]:
        if column not in {c['name'] for c in inspect(conn).get_columns(table)}:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} JSON NOT NULL DEFAULT '[]'"))
