from copy import deepcopy
from sqlalchemy import MetaData, Table, inspect, select


def default_groups(rank="iron"):
    offset = {"bronze": 4, "silver": 8}.get(rank, 0)
    tiers = []
    for name, damage in [("Trailworn", 11), ("Tempered", 13), ("Prized", 15)]:
        tiers.append({"name": name, "drops": [
            {"name": name + " " + kind.title(), "weapon_type_slug": kind,
             "base_damage": damage + offset, "weight": weight}
            for kind, weight in [("sword", 3), ("axe", 2), ("dagger", 2)]]})
    return {"size": 2, "health_step_percent": 12, "power_step_percent": 8,
            "max_threat_percent": 300, "loot_tiers": tiers}


def upgrade(conn):
    if not inspect(conn).has_table('quest_templates'):
        return
    table = Table('quest_templates', MetaData(), autoload_with=conn)
    for row in conn.execute(select(table.c.slug, table.c.journey)).mappings().all():
        journey = deepcopy(row['journey'] or {})
        if journey.get('stages') and 'encounter_groups' not in journey:
            journey['encounter_groups'] = default_groups(journey.get('required_rank', 'iron'))
            if journey.get('max_rests') is None:
                journey['max_rests'] = 1 if journey.get('camp_rest') else 0
            journey.setdefault('push_xp_tiers', [5, 10, 15])
            conn.execute(table.update().where(table.c.slug == row['slug']).values(journey=journey))
    # Saved quests retain their existing withdrawal and reward rules.
