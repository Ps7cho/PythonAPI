from sqlalchemy import inspect, text


def upgrade(conn):
    inspector = inspect(conn)
    for table in ("users", "adventurers"):
        if inspector.has_table(table) and "statistics" in {column["name"] for column in inspector.get_columns(table)}:
            conn.execute(text(f"UPDATE {table} SET statistics = '{{}}'"))