from copy import deepcopy
from sqlalchemy import MetaData, Table, inspect, select


def upgrade(conn):
    from app.models import RaidRotation
    RaidRotation.__table__.create(conn, checkfirst=True)
    if not inspect(conn).has_table('quest_templates'):
        return
    table = Table('quest_templates', MetaData(), autoload_with=conn)
    for row in conn.execute(select(table.c.slug, table.c.journey)).mappings().all():
        rules = deepcopy(row['journey'] or {})
        group = rules.get('encounter_groups')
        if group and rules.get('kind') in ('quest', 'epic'):
            low, high = (2, 3) if rules['kind'] == 'quest' else (3, 5)
            group.update(size=low, min_size=low, max_size=high)
            rules['death_policy'] = 'rescue_on_return'
            conn.execute(table.update().where(table.c.slug == row['slug']).values(journey=rules))
    # Existing adventures retain their snapshotted death and group rules.
