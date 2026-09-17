from uuid import UUID

from sqlalchemy import MetaData, Table, inspect, select, text

from app.cooldowns import saved_cooldowns


def upgrade(conn):
    columns = {c["name"] for c in inspect(conn).get_columns("abilities")}
    if "max_targets" not in columns:
        conn.execute(text("ALTER TABLE abilities ADD COLUMN max_targets INTEGER DEFAULT 1 CHECK (max_targets IS NULL OR max_targets > 0)"))
        # Preserve old party-wide effects and enemy-only attacks, including custom content.
        conn.execute(text("UPDATE abilities SET max_targets=NULL WHERE target_type='party' OR "
                          "(effect_type='damage' AND id IN (SELECT ability_id FROM enemy_abilities) "
                          "AND starter=FALSE AND id NOT IN (SELECT ability_id FROM adventurer_abilities))"))
    columns = {c["name"] for c in inspect(conn).get_columns("adventurers")}
    if "combat_cooldowns" not in columns:
        conn.execute(text("ALTER TABLE adventurers ADD COLUMN combat_cooldowns JSON NOT NULL DEFAULT '{}'"))
    # Backfill from the latest saved encounter per character. Reflection gives
    # consistent UUID/JSON conversion on both supported databases.
    metadata = MetaData()
    encounters = Table("encounters", metadata, autoload_with=conn)
    heroes = Table("adventurers", metadata, autoload_with=conn)
    abilities = Table("abilities", metadata, autoload_with=conn)
    power_id = conn.scalar(select(abilities.c.id).where(abilities.c.slug == "power_strike"))
    power_id = str(UUID(str(power_id))) if power_id else None
    latest = {}
    for row in conn.execute(select(encounters).order_by(encounters.c.updated_at, encounters.c.created_at, encounters.c.id)).mappings():
        for actor in row["participants"] or []:
            actor = dict(actor)
            if power_id and actor.get("power_ready_turn", 1) > row["turn"]:
                ready = dict(actor.get("ability_ready_turns", {}))
                ready[str(power_id)] = max(ready.get(str(power_id), 1), actor["power_ready_turn"])
                actor["ability_ready_turns"] = ready
            latest[actor["id"].replace("-", "")] = saved_cooldowns(actor, row["turn"], row["state"] in ("victory", "defeat"))
    for row in conn.execute(select(heroes.c.id, heroes.c.combat_cooldowns)).mappings().all():
        saved = latest.get(str(row["id"]).replace("-", ""))
        if saved and not row["combat_cooldowns"]:
            conn.execute(heroes.update().where(heroes.c.id == row["id"]).values(combat_cooldowns=saved))
