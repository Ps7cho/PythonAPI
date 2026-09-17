from copy import deepcopy
from sqlalchemy import MetaData, Table, inspect, select

RAID_GROUPS = [['undead','acolyte'], ['orc','axe-thrower'], ['possessed','priest'], ['hound','hound']]


def expand_raid(rules):
    if rules.get('kind') == 'raid':
        groups = rules['stages'][0]['groups']
        for group in RAID_GROUPS:
            if group not in groups:
                groups.append(list(group))
    return rules


def upgrade(conn):
    if not inspect(conn).has_table('quest_templates'):
        return
    table = Table('quest_templates', MetaData(), autoload_with=conn)
    for row in conn.execute(select(table.c.slug,table.c.journey)).mappings().all():
        rules = expand_raid(deepcopy(row['journey'] or {}))
        if rules != (row['journey'] or {}):
            conn.execute(table.update().where(table.c.slug==row['slug']).values(journey=rules))
    # Seed additions create new catalog rows. Existing runs and raid rotations stay frozen.
