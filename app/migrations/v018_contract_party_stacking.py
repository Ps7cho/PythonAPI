from sqlalchemy import inspect, text


def upgrade(conn):
    if inspect(conn).has_table('recovery_contracts') and 'active_run_ids' not in {column['name'] for column in inspect(conn).get_columns('recovery_contracts')}:
        conn.execute(text("ALTER TABLE recovery_contracts ADD COLUMN active_run_ids JSON NOT NULL DEFAULT '[]'"))