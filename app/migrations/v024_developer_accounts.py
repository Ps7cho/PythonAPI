from sqlalchemy import inspect, text


def upgrade(conn):
    columns = {column['name'] for column in inspect(conn).get_columns('users')}
    if 'account_type' not in columns:
        conn.execute(text("ALTER TABLE users ADD COLUMN account_type VARCHAR(20) NOT NULL DEFAULT 'player'"))
    # The named development account is the first explicitly authorized editor.
    conn.execute(text("UPDATE users SET account_type='developer' WHERE lower(username)=lower('borkse')"))
