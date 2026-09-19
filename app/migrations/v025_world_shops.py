import json
from sqlalchemy import inspect, select
from app.models import Village, Shop, ShopTable, GearDefinition, WeaponType
from app.shop import CONSUMABLE_PRICES, WEAPON_PRICES
from app.consumables import DEFINITIONS


def upgrade(conn):
    for model in (Village, Shop, ShopTable):
        model.__table__.create(conn, checkfirst=True)
    if conn.execute(select(Village.slug).where(Village.slug == 'mosswood')).first():
        return
    conn.execute(Village.__table__.insert().values(slug='mosswood', name='Mosswood', region='Mosswood', description='The village exchange and expedition hub.'))
    conn.execute(Shop.__table__.insert().values(slug='mosswood-market', village_slug='mosswood', name='Mosswood Exchange', description='Local weapons, armor, and supplies.'))
    gear = []
    for row in conn.execute(select(GearDefinition)).mappings():
        gear.append({'item_type':'gear','slug':row['slug'],'name':row['name'],'slot':row['slot'],'bonuses':row['bonuses'],'price':row['price'],'required_rank':row['required_rank']})
    weapons = []
    for row in conn.execute(select(WeaponType)).mappings():
        if row['slug'] in WEAPON_PRICES:
            weapons.append({'item_type':'weapon','slug':row['slug'],'name':row['name'],'price':WEAPON_PRICES[row['slug']], 'tags':row['tags'],'base_damage':12 + list(WEAPON_PRICES).index(row['slug']) * 2})
    consumables = [{'item_type':'consumable','slug':item['slug'],'name':item['name'],'price':CONSUMABLE_PRICES[item['slug']], 'quantity':1,'description':item['description']} for item in DEFINITIONS if item['slug'] in CONSUMABLE_PRICES]
    for category, items in [('gear', gear), ('weapons', weapons), ('consumables', consumables)]:
        conn.execute(ShopTable.__table__.insert().values(slug='mosswood-market-'+category, shop_slug='mosswood-market', category=category, items=items))
