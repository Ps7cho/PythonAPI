"""Nightly testing with an explicitly armed, default-off adventurer wipe."""
from datetime import datetime
from sqlalchemy import inspect, select, text


def upgrade(conn):
    from app.models import WorldBossState, QuestTemplate
    from app.world_boss import next_start
    table = WorldBossState.__table__
    if 'delete_adventurers_on_failure' not in {c['name'] for c in inspect(conn).get_columns(table.name)}:
        conn.execute(text('ALTER TABLE world_boss_state ADD COLUMN delete_adventurers_on_failure BOOLEAN NOT NULL DEFAULT FALSE'))
    row = conn.execute(select(table).where(table.c.slug == 'alpha-wolf')).mappings().first()
    if row:
        settings = {**row['settings'], 'cadence': 'daily'}
        conn.execute(table.update().where(table.c.slug == 'alpha-wolf').values(
            settings=settings, next_start_at=next_start(datetime.utcnow(), settings)))
    quest = QuestTemplate.__table__
    journey = conn.scalar(select(quest.c.journey).where(quest.c.slug == 'alpha-wolf-finale'))
    if journey:
        conn.execute(quest.update().where(quest.c.slug == 'alpha-wolf-finale').values(journey={
            **journey, 'description': 'Nightly 8 PM America/Denver. Ten minutes. Shared 10,000,000 HP; independent party attacks. Testing retains adventurers and continues nightly after victory. With the developer deletion switch on, failure deletes all adventurers while keeping accounts, and victory unlocks beta.'}))
