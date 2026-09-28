"""Persistent Gauntlet definitions and results; existing combat stores battles."""
from app.models import GauntletDefinition, GauntletRun


def upgrade(conn):
    GauntletDefinition.__table__.create(conn, checkfirst=True)
    GauntletRun.__table__.create(conn, checkfirst=True)
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as postgres_insert
    insert = sqlite_insert if conn.dialect.name == 'sqlite' else postgres_insert
    conn.execute(insert(GauntletDefinition).values(
        slug='endless-road', name='Endless Road',
        description='A nonlethal endurance trial. No rewards, rests, or consumables.',
        settings={'enemy_pool': ['roadside-bandit'], 'health_step_percent': 20,
                  'power_step_percent': 10, 'max_party_size': None}
    ).on_conflict_do_nothing(index_elements=['slug']))
