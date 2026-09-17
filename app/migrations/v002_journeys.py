from sqlalchemy import inspect, text


def upgrade(conn):
    from app.models import RestPolicy
    if inspect(conn).has_table("quest_templates"):
        if "journey" not in {c["name"] for c in inspect(conn).get_columns("quest_templates")}:
            conn.execute(text("ALTER TABLE quest_templates ADD COLUMN journey JSON NOT NULL DEFAULT '{}'"))
    RestPolicy.__table__.create(conn, checkfirst=True)
