from sqlalchemy import inspect, text


def upgrade(conn):
    tables = inspect(conn)
    if tables.has_table("users") and "statistics" not in {c["name"] for c in tables.get_columns("users")}:
        conn.execute(text("ALTER TABLE users ADD COLUMN statistics JSON NOT NULL DEFAULT '{}'"))
    if tables.has_table("adventurers") and "statistics" not in {c["name"] for c in tables.get_columns("adventurers")}:
        conn.execute(text("ALTER TABLE adventurers ADD COLUMN statistics JSON NOT NULL DEFAULT '{}'"))