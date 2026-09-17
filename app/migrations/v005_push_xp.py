"""Add modest XP tiers to epic catalogs without changing saved adventure rules."""
from sqlalchemy import MetaData, Table, inspect, select


def upgrade(conn):
    if not inspect(conn).has_table("quest_templates"):
        return
    table = Table("quest_templates", MetaData(), autoload_with=conn)
    for row in conn.execute(select(table.c.slug, table.c.journey)).mappings():
        journey = dict(row["journey"] or {})
        if journey.get("kind") == "epic" and journey.get("max_rests") is not None and "push_xp_tiers" not in journey:
            journey["push_xp_tiers"] = [5, 10, 15]
            conn.execute(table.update().where(table.c.slug == row["slug"]).values(journey=journey))
