from copy import deepcopy
from sqlalchemy import inspect, MetaData, Table, select
from app.models import Consumable, OwnedConsumable
from app.consumables import DEFINITIONS, add_loot


def upgrade(conn):
    Consumable.__table__.create(conn, checkfirst=True)
    OwnedConsumable.__table__.create(conn, checkfirst=True)
    table = Consumable.__table__
    for item in DEFINITIONS:
        if not conn.execute(select(table.c.slug).where(table.c.slug == item['slug'])).first():
            conn.execute(table.insert().values(**item))
    if inspect(conn).has_table('quest_templates'):
        templates = Table('quest_templates', MetaData(), autoload_with=conn)
        for row in conn.execute(select(templates.c.slug, templates.c.journey)).mappings():
            rules = add_loot(deepcopy(row['journey'] or {}))
            if rules != row['journey']:
                conn.execute(templates.update().where(templates.c.slug == row['slug']).values(journey=rules))
