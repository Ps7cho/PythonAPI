from sqlalchemy import inspect, text, select


def upgrade(conn):
    from app.models import RankDefinition
    from app.progression import level_cost
    RankDefinition.__table__.create(conn, checkfirst=True)
    names = ["Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond", "Mythic", "Legendary"]
    for i, name in enumerate(names):
        if conn.execute(select(RankDefinition.slug).where(RankDefinition.slug == name.lower())).first():
            continue
        unlocks = []
        if i == 1:
            unlocks = ["Fourth ability slot", "Bronze contracts", "Bronze Vanguard Sword"]
        if i == 2:
            unlocks = ["Second passive slot (passive system planned)", "Silver contracts and Vanguard Sword", "Advanced party roles (planned)"]
        conn.execute(RankDefinition.__table__.insert().values(slug=name.lower(), name=name,
            min_level=max(1, i * 10), ability_slots=3 if i == 0 else 4,
            passive_slots=1 if i < 2 else 2, unlocks=unlocks,
            equipment_reward={"name": name + " Vanguard Sword", "weapon_type_slug": "sword", "base_damage": 14 if i == 1 else 18} if i in (1, 2) else None))
    if inspect(conn).has_table("weapons") and "required_rank" not in {c["name"] for c in inspect(conn).get_columns("weapons")}:
        conn.execute(text("ALTER TABLE weapons ADD COLUMN required_rank VARCHAR NOT NULL DEFAULT 'iron' REFERENCES rank_definitions(slug)"))
    columns = {c["name"] for c in inspect(conn).get_columns("adventurers")}
    if "attribute_points" not in columns:
        conn.execute(text("ALTER TABLE adventurers ADD COLUMN attribute_points INTEGER NOT NULL DEFAULT 0"))
    if {"level", "experience"} <= columns:
        for row in conn.execute(text("SELECT id, level, experience FROM adventurers")).mappings().all():
            level, spent = 1, 0
            while spent + level_cost(level) <= row["experience"]:
                spent += level_cost(level)
                level += 1
            level = max(level, row["level"])
            conn.execute(text("UPDATE adventurers SET level=:level, attribute_points=:points WHERE id=:id"),
                         {"level": level, "points": (level - 1) * 3, "id": row["id"]})
