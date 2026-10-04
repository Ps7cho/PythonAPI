"""Durable UTC job cursors and daily shop snapshots."""


def upgrade(conn):
    from app.models import ScheduledJob, ShopRotation, ShopTable

    ScheduledJob.__table__.create(conn, checkfirst=True)
    ShopRotation.__table__.create(conn, checkfirst=True)
    # These basic supplies stay available while the rest of each catalog rotates.
    essentials = {'gear': {'leather-coat', 'lucky-ring'},
                  'weapons': {'sword', 'axe'},
                  'consumables': {'healing-potion'}}
    # Select only the columns this migration owns; later model columns may not
    # exist yet when upgrading an older installation.
    from sqlalchemy import select
    for row in conn.execute(select(ShopTable.slug, ShopTable.category, ShopTable.items)).mappings():
        required = essentials.get(row['category'], set())
        items = [{**item, 'always_stocked': True} if item.get('slug') in required else item
                 for item in row['items'] or []]
        if items != row['items']:
            conn.execute(ShopTable.__table__.update().where(ShopTable.slug == row['slug']).values(items=items))
