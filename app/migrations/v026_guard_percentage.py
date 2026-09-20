from sqlalchemy import inspect, update

from app.models import Ability


def upgrade(conn):
    """Keep the persisted starter Guard definition aligned with combat rules."""
    if not inspect(conn).has_table('abilities'):
        return
    columns = {column['name'] for column in inspect(conn).get_columns('abilities')}
    if not {'slug', 'description', 'power'} <= columns:
        return
    conn.execute(
        update(Ability)
        .where(Ability.slug == 'guard')
        .values(
            description='Reduce incoming direct damage by 60% this round.',
            power=60,
        )
    )
