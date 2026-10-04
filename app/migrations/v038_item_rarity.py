from sqlalchemy import inspect, text


def upgrade(conn):
    for table in ('weapons', 'gear'):
        if 'rarity' not in {column['name'] for column in inspect(conn).get_columns(table)}:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN rarity VARCHAR NOT NULL DEFAULT 'common'"))
