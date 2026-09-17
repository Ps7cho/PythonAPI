from copy import deepcopy
from sqlalchemy import inspect, text, select, Table, MetaData
from app.models import Consumable, EssenceDefinition, OrbOutcome, Ability
from app.orb_content import ACTIVE_ESSENCES, ORBS, recipes, update_loot


def upgrade(conn):
    if 'active' not in {c['name'] for c in inspect(conn).get_columns('essence_definitions')}:
        conn.execute(text('ALTER TABLE essence_definitions ADD COLUMN active BOOLEAN NOT NULL DEFAULT TRUE'))
    checks = inspect(conn).get_check_constraints('consumables')
    if any(c['name'] == 'ck_consumable_effect' and "'orb'" not in c['sqltext'] for c in checks):
        clause = "CHECK (effect IN ('heal', 'buff', 'cleanse', 'essence', 'orb') AND power >= 0 AND power <= 100)"
        if conn.dialect.name == 'postgresql':
            conn.execute(text('ALTER TABLE consumables DROP CONSTRAINT ck_consumable_effect'))
            conn.execute(text('ALTER TABLE consumables ADD CONSTRAINT ck_consumable_effect ' + clause))
        else:
            deferred = conn.scalar(text('PRAGMA defer_foreign_keys'))
            conn.execute(text('PRAGMA defer_foreign_keys=ON'))
            conn.execute(text('CREATE TABLE consumables_new (slug VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, description VARCHAR NOT NULL, effect VARCHAR NOT NULL, power INTEGER NOT NULL, CONSTRAINT ck_consumable_effect ' + clause + ')'))
            conn.execute(text('INSERT INTO consumables_new SELECT slug,name,description,effect,power FROM consumables'))
            conn.execute(text('DROP TABLE consumables'))
            conn.execute(text('ALTER TABLE consumables_new RENAME TO consumables'))
            if conn.execute(text('PRAGMA foreign_key_check')).first():
                raise RuntimeError('Orb migration failed foreign-key validation.')
            conn.execute(text('PRAGMA defer_foreign_keys=OFF'))
            if deferred:
                conn.execute(text('PRAGMA defer_foreign_keys=ON'))
    active = ['essence-' + name for name in ACTIVE_ESSENCES]
    conn.execute(EssenceDefinition.__table__.update().values(active=EssenceDefinition.consumable_slug.in_(active)))
    conn.execute(Consumable.__table__.update().where(Consumable.slug.in_(active)).values(
        description='Permanently absorb in the village (three distinct essences maximum), then apply orbs to learn abilities.'))
    for orb in ORBS:
        if not conn.scalar(select(Consumable.slug).where(Consumable.slug == orb['slug'])):
            conn.execute(Consumable.__table__.insert().values(**orb))
    OrbOutcome.__table__.create(conn, checkfirst=True)
    # Minimal legacy-schema tests may not have the complete ability catalog yet.
    if {'name', 'power', 'cooldown_value'}.issubset({c['name'] for c in inspect(conn).get_columns('abilities')}):
        for orb, essence, spec in recipes():
            ability_id = conn.scalar(select(Ability.id).where(Ability.slug == spec['slug']))
            if ability_id is None:
                ability_id = conn.execute(Ability.__table__.insert().values(**spec).returning(Ability.id)).scalar_one()
            if not conn.execute(select(OrbOutcome.orb_slug).where(OrbOutcome.orb_slug == orb, OrbOutcome.essence_slug == essence)).first():
                conn.execute(OrbOutcome.__table__.insert().values(orb_slug=orb, essence_slug=essence, ability_id=ability_id))
    if inspect(conn).has_table('quest_templates'):
        table = Table('quest_templates', MetaData(), autoload_with=conn)
        for row in conn.execute(select(table.c.slug, table.c.journey)).mappings().all():
            rules = update_loot(deepcopy(row['journey'] or {}))
            if rules != row['journey']:
                conn.execute(table.update().where(table.c.slug == row['slug']).values(journey=rules))
