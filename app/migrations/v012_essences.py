from copy import deepcopy
from sqlalchemy import inspect, text, select, Table, MetaData
from app.models import Consumable, EssenceDefinition, AbsorbedEssence
from app.essence_content import ESSENCES
from app.essences import add_essence_loot


def upgrade(conn):
    checks = inspect(conn).get_check_constraints('consumables')
    old = next((c for c in checks if c['name'] == 'ck_consumable_effect' and "'essence'" not in c['sqltext']), None)
    if old:
        if conn.dialect.name == 'postgresql':
            conn.execute(text('ALTER TABLE consumables DROP CONSTRAINT ck_consumable_effect'))
            conn.execute(text("ALTER TABLE consumables ADD CONSTRAINT ck_consumable_effect CHECK (effect IN ('heal', 'buff', 'cleanse', 'essence') AND power >= 0 AND power <= 100)"))
        else:
            # Defer inbound FK checks until the replacement has its original name.
            deferred = conn.scalar(text('PRAGMA defer_foreign_keys'))
            conn.execute(text('PRAGMA defer_foreign_keys=ON'))
            conn.execute(text("CREATE TABLE consumables_new (slug VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, description VARCHAR NOT NULL, effect VARCHAR NOT NULL, power INTEGER NOT NULL, CONSTRAINT ck_consumable_effect CHECK (effect IN ('heal', 'buff', 'cleanse', 'essence') AND power >= 0 AND power <= 100))"))
            conn.execute(text('INSERT INTO consumables_new SELECT slug,name,description,effect,power FROM consumables'))
            conn.execute(text('DROP TABLE consumables'))
            conn.execute(text('ALTER TABLE consumables_new RENAME TO consumables'))
            if conn.execute(text('PRAGMA foreign_key_check')).first():
                raise RuntimeError('Consumable migration failed foreign-key validation.')
            conn.execute(text('PRAGMA defer_foreign_keys=OFF'))
            if deferred:
                conn.execute(text('PRAGMA defer_foreign_keys=ON'))
    EssenceDefinition.__table__.create(conn, checkfirst=True)
    AbsorbedEssence.__table__.create(conn, checkfirst=True)
    for item in ESSENCES:
        if not conn.scalar(select(Consumable.slug).where(Consumable.slug == item['slug'])):
            conn.execute(Consumable.__table__.insert().values(slug=item['slug'], name=item['name'], effect='essence', power=0,
                description='Permanently absorb in the village. Three distinct essences maximum; cannot be replaced. Associated powers are planned, not yet active.'))
        if not conn.scalar(select(EssenceDefinition.consumable_slug).where(EssenceDefinition.consumable_slug == item['slug'])):
            conn.execute(EssenceDefinition.__table__.insert().values(consumable_slug=item['slug'], powers=item['powers']))
    if inspect(conn).has_table('quest_templates'):
        table = Table('quest_templates', MetaData(), autoload_with=conn)
        for row in conn.execute(select(table.c.slug, table.c.journey)).mappings().all():
            rules = add_essence_loot(deepcopy(row['journey'] or {}))
            if rules != row['journey']:
                conn.execute(table.update().where(table.c.slug == row['slug']).values(journey=rules))
