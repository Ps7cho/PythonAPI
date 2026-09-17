from sqlalchemy import inspect, text


def upgrade(conn):
    if inspect(conn).has_table("party_members") and "is_ready" not in {column["name"] for column in inspect(conn).get_columns("party_members") }:
        conn.execute(text("ALTER TABLE party_members ADD COLUMN is_ready BOOLEAN NOT NULL DEFAULT FALSE"))