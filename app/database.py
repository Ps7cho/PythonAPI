from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

Base = declarative_base()

settings = get_settings()
engine_kwargs = {"pool_pre_ping": True}

if settings.database_url.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
    if settings.database_url == "sqlite:///:memory:":
        engine_kwargs["poolclass"] = StaticPool

engine = create_engine(settings.database_url, **engine_kwargs)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def ensure_ability_columns() -> None:
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("abilities"):
            return

        columns = {column["name"] for column in inspector.get_columns("abilities")}
        required_columns = {
            "slug": "VARCHAR",
            "damage_multiplier": "FLOAT",
            "requires_weapon": "BOOLEAN DEFAULT FALSE",
            "allowed_weapon_tags": "JSON DEFAULT '[]'",
            "starter": "BOOLEAN DEFAULT FALSE",
            "loadout_order": "INTEGER",
            "ability_type": "VARCHAR DEFAULT 'attack'",
            "cooldown_type": "VARCHAR DEFAULT 'turn'",
            "cooldown_value": "INTEGER DEFAULT 0",
            "cost_type": "VARCHAR DEFAULT 'Stamina'",
            "cost_value": "INTEGER DEFAULT 0",
            "target_type": "VARCHAR DEFAULT 'enemy'",
            "effect_type": "VARCHAR DEFAULT 'damage'",
            "power": "INTEGER DEFAULT 10",
        }

        for column_name, column_sql in required_columns.items():
            if column_name not in columns:
                conn.execute(text(f"ALTER TABLE abilities ADD COLUMN {column_name} {column_sql}"))

        # Stable identifiers for old catalogs, with collision-free custom slugs.
        legacy = {"Strike": "attack", "Power Strike": "power_strike", "Guard": "guard",
                  "Second Wind": "second_wind", "Battle Cry": "battle_cry", "Heroic Stand": "heroic_stand"}
        for row in conn.execute(text("SELECT id, name FROM abilities WHERE slug IS NULL")).mappings().all():
            slug = legacy.get(row["name"], str(row["id"]))
            conn.execute(text("UPDATE abilities SET slug=:slug, starter=:starter, loadout_order=:ordering WHERE id=:id"),
                         dict(slug=slug, starter=True, ordering={"attack": 0, "power_strike": 1, "guard": 2}.get(slug), id=row["id"]))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_abilities_slug ON abilities (slug)"))
        if "requires_weapon" not in columns:
            for slug, tag in {"attack": "melee", "power_strike": "melee", "dirty_stab": "melee",
                              "aimed_shot": "ranged", "cleave": "melee", "enemy_strike": "melee"}.items():
                from sqlalchemy import JSON, bindparam
                conn.execute(text("UPDATE abilities SET requires_weapon=:required, allowed_weapon_tags=:tags, "
                                  "damage_multiplier=COALESCE(damage_multiplier, power / 10.0) WHERE slug=:slug")
                             .bindparams(bindparam("tags", type_=JSON)),
                             dict(required=True, tags=[tag], slug=slug))



def ensure_adventurer_columns() -> None:
    with engine.begin() as conn:
        inspector = inspect(conn)
        if not inspector.has_table("adventurers"):
            return

        columns = {column["name"] for column in inspector.get_columns("adventurers")}
        if "gold" not in columns:
            conn.execute(text("ALTER TABLE adventurers ADD COLUMN IF NOT EXISTS gold INTEGER DEFAULT 0"))
        if "is_alive" not in columns:
            conn.execute(text("ALTER TABLE adventurers ADD COLUMN IF NOT EXISTS is_alive BOOLEAN DEFAULT TRUE"))


def ensure_game_event_columns() -> None:
    """Upgrade the original state-only event table for quest combat events."""
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(74829013)"))
        columns = {c["name"]: c for c in inspect(conn).get_columns("game_events")}
        if "quest_run_id" not in columns:
            conn.execute(text("ALTER TABLE game_events ADD COLUMN quest_run_id UUID REFERENCES quest_runs(id)"))
            conn.execute(text("CREATE INDEX ix_game_events_quest_run_id ON game_events (quest_run_id)"))
        if "timestamp" not in columns:
            conn.execute(text("ALTER TABLE game_events ADD COLUMN timestamp TIMESTAMP"))
            conn.execute(text("UPDATE game_events SET timestamp = created_at WHERE timestamp IS NULL"))
            if conn.dialect.name == "postgresql":
                conn.execute(text("ALTER TABLE game_events ALTER COLUMN timestamp SET NOT NULL"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
