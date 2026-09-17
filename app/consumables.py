"""Consumable catalog and owned stacks; execution uses the shared cast resolver."""
from sqlalchemy import select
from app.models import OwnedConsumable

DEFINITIONS = [
    dict(slug='healing-potion', name='Healing Potion', effect='heal', power=35,
         description='Restore 35% maximum HP to yourself or a living ally. Uses one action.'),
    dict(slug='cleansing-draught', name='Cleansing Draught', effect='cleanse', power=0,
         description='Remove all damage-over-time statuses from yourself or a living ally. Uses one action.'),
    dict(slug='might-tonic', name='Might Tonic', effect='buff', power=25,
         description='Boost attack damage by 25% for 3 turns on yourself or a living ally. Refreshes rather than stacking. Uses one action.'),
]


def add_loot(rules):
    for tier in rules.get('encounter_groups', {}).get('loot_tiers', []):
        existing = {d.get('consumable_slug') for d in tier['drops']}
        for item in DEFINITIONS:
            if item['slug'] not in existing:
                tier['drops'].append(dict(name=item['name'], consumable_slug=item['slug'], quantity=2, weight=3))
    return rules


def inventory(db, hero_id):
    return [dict(slug=row.consumable_slug, name=row.definition.name,
                 description=row.definition.description, quantity=row.quantity,
                 effect=row.definition.effect, power=row.definition.power, item_type='consumable')
            for row in db.scalars(select(OwnedConsumable).where(
                OwnedConsumable.adventurer_id == hero_id, OwnedConsumable.quantity > 0)
                .order_by(OwnedConsumable.consumable_slug))]


def grant(db, hero_id, slug, quantity):
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as postgres_insert
    insert = sqlite_insert if db.bind.dialect.name == 'sqlite' else postgres_insert
    db.execute(insert(OwnedConsumable).values(adventurer_id=hero_id, consumable_slug=slug, quantity=quantity)
        .on_conflict_do_update(index_elements=['adventurer_id', 'consumable_slug'],
                              set_={'quantity': OwnedConsumable.quantity + quantity}))
