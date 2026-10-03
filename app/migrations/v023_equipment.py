from sqlalchemy import JSON, bindparam, inspect, select, text
from app.models import GearDefinition, Gear, EquippedGear


def upgrade(conn):
    for model in (GearDefinition, Gear, EquippedGear):
        model.__table__.create(conn, checkfirst=True)
    specs = [
        ('scout-hood', 'Scout Hood', 'Head', {'Awareness': 3}, 40),
        ('leather-coat', 'Leather Coat', 'Chest', {'Defense': 4, 'Vitality': 2}, 80),
        ('steady-gloves', 'Steady Gloves', 'Hands', {'Precision': 3}, 45),
        ('trail-leggings', 'Trail Leggings', 'Legs', {'Defense': 2, 'Agility': 2}, 55),
        ('traveller-boots', 'Traveller Boots', 'Feet', {'Speed': 3}, 45),
        ('wooden-buckler', 'Wooden Buckler', 'Off Hand', {'Defense': 3}, 50),
        ('focus-amulet', 'Focus Amulet', 'Amulet', {'Affinity': 3, 'Willpower': 2}, 85),
        ('lucky-ring', 'Lucky Ring', 'Ring', {'Luck': 3}, 65),
    ]
    table = GearDefinition.__table__
    has_effects = 'effect_slugs' in {column['name'] for column in inspect(conn).get_columns('gear_definitions')}
    for slug, name, slot, bonuses, price in specs:
        if not conn.execute(select(table.c.slug).where(table.c.slug == slug)).first():
            # This migration also runs against older databases before migration 036
            # adds effect_slugs. Name only columns present in the physical table.
            fields = 'slug, name, slot, bonuses, price, required_rank'
            values = ':slug, :name, :slot, :bonuses, :price, :required_rank'
            if has_effects:
                fields += ', effect_slugs'
                values += ', :effect_slugs'
            statement = text(f'INSERT INTO gear_definitions ({fields}) VALUES ({values})')
            parameters = dict(slug=slug, name=name, slot=slot, bonuses=bonuses,
                              price=price, required_rank='iron')
            bindings = [bindparam('bonuses', type_=JSON)]
            if has_effects:
                parameters['effect_slugs'] = []
                bindings.append(bindparam('effect_slugs', type_=JSON))
            conn.execute(statement.bindparams(*bindings), parameters)
