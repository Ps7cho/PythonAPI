from sqlalchemy import MetaData, Table, inspect, select


def upgrade(conn):
    if not inspect(conn).has_table('quest_templates'):
        return
    table = Table('quest_templates', MetaData(), autoload_with=conn)
    for row in conn.execute(select(table)).mappings().all():
        journey = dict(row['journey'] or {})
        if journey.get('kind') == 'epic' and 'max_rests' not in journey:
            journey.update(max_rests=1, push_gold=10, push_experience=8)
            journey['description'] = journey.get('description', '').replace('Camp between battles to recover.', 'You have one camp rest for the whole expedition. Press on for a bonus paid only on completion.')
            conn.execute(table.update().where(table.c.slug == row['slug']).values(journey=journey))
    # Existing quest snapshots are intentionally unchanged.
