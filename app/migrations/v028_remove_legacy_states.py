"""Remove the obsolete generic state snapshot/event API schema."""

from sqlalchemy import inspect, text


def upgrade(conn):
    inspector = inspect(conn)

    if inspector.has_table("game_events"):
        columns = {column["name"] for column in inspector.get_columns("game_events")}
        if "state_id" in columns:
            if conn.dialect.name == "postgresql":
                conn.execute(text("ALTER TABLE game_events DROP CONSTRAINT IF EXISTS game_events_state_id_fkey"))
            conn.execute(text("DROP INDEX IF EXISTS ix_game_events_state_id"))
            conn.execute(text("ALTER TABLE game_events DROP COLUMN state_id"))

    conn.execute(text("DROP TABLE IF EXISTS game_states"))
