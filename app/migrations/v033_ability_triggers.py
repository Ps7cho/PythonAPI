"""Bounded multi-strikes and equipped on-hit passive abilities."""
from sqlalchemy import inspect, text


def upgrade(conn):
    columns = {c['name'] for c in inspect(conn).get_columns('abilities')}
    for name, ddl in {
        'strike_count': 'INTEGER NOT NULL DEFAULT 1',
        'extra_strike_chance': 'INTEGER NOT NULL DEFAULT 0',
        'max_extra_strikes': 'INTEGER NOT NULL DEFAULT 1',
        'trigger_mode': "VARCHAR NOT NULL DEFAULT 'active'",
        'proc_chance_percent': 'INTEGER NOT NULL DEFAULT 100',
    }.items():
        if name not in columns:
            conn.execute(text(f'ALTER TABLE abilities ADD COLUMN {name} {ddl}'))
