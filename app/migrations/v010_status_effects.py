from sqlalchemy import inspect, text, MetaData, Table
from app.models import StatusEffect, EntityType
from app.status_content import EFFECTS, PROFILES, ASSIGNMENTS


def upgrade(conn):
    StatusEffect.__table__.create(conn, checkfirst=True)
    EntityType.__table__.create(conn, checkfirst=True)
    for table, rows in [(StatusEffect.__table__, EFFECTS), (EntityType.__table__,
                        [dict(slug=k, status_resistances=v) for k, v in PROFILES.items()])]:
        for row in rows:
            if not conn.execute(table.select().where(table.c.slug == row['slug'])).first():
                conn.execute(table.insert().values(**row))
    inspector = inspect(conn)
    if inspector.has_table('adventurers') and 'combat_statuses' not in {c['name'] for c in inspector.get_columns('adventurers')}:
        conn.execute(text("ALTER TABLE adventurers ADD COLUMN combat_statuses JSON NOT NULL DEFAULT '[]'"))
    if inspector.has_table('abilities'):
        if 'status_effect_slug' not in {c['name'] for c in inspector.get_columns('abilities')}:
            conn.execute(text('ALTER TABLE abilities ADD COLUMN status_effect_slug VARCHAR REFERENCES status_effects(slug)'))
        conn.execute(text('CREATE INDEX IF NOT EXISTS ix_abilities_status_effect_slug ON abilities (status_effect_slug)'))
        table = Table('abilities', MetaData(), autoload_with=conn)
        for slug, effect in ASSIGNMENTS.items():
            conn.execute(table.update().where(table.c.slug == slug).values(status_effect_slug=effect))
