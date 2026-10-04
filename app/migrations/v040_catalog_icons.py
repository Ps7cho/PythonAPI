"""Optional artwork for every editable Worldsmith catalog; existing rows stay blank."""
from sqlalchemy import inspect, text


def upgrade(conn):
    from app.catalog_editor import CATALOGS

    inspector = inspect(conn)
    for model in CATALOGS.values():
        table = model.__tablename__
        if inspector.has_table(table) and 'icon_path' not in {c['name'] for c in inspector.get_columns(table)}:
            conn.execute(text(f'ALTER TABLE {table} ADD COLUMN icon_path VARCHAR(500)'))
    if inspector.has_table('weapons') and 'weapon_definition_slug' not in {c['name'] for c in inspector.get_columns('weapons')}:
        conn.execute(text('ALTER TABLE weapons ADD COLUMN weapon_definition_slug VARCHAR REFERENCES weapon_definitions(slug)'))
        # Recover the blueprint for existing weapons whose names still match it.
        conn.execute(text('UPDATE weapons SET weapon_definition_slug = '
                          '(SELECT slug FROM weapon_definitions WHERE weapon_definitions.name = weapons.name '
                          'AND weapon_definitions.weapon_type_slug = weapons.weapon_type_slug)'))
