from copy import deepcopy
from sqlalchemy import MetaData, Table, inspect, select


def upgrade_loot(rules, slug):
    groups = rules.get('encounter_groups')
    if not groups:
        return rules
    groups['loot_rng'] = 'run'
    raid = rules.get('kind') == 'raid'
    prefix = slug.replace('-contract', '').replace('-', ' ').title()
    preferred = 'bow' if 'wolf' in slug or 'pack' in slug else 'staff' if 'hollow' in slug or raid else 'mace'
    for i, tier in enumerate(groups['loot_tiers']):
        if 'drop_chance_percent' in tier:
            continue
        tier['drop_chance_percent'] = ([75, 85, 90] if raid else [60, 70, 80])[min(i, 2)]
        base = min(d['base_damage'] for d in tier['drops'])
        # Preserve existing entries and add uncommon/rare alternatives with real weapon types.
        additions = [
            ('mace', 'Warhammer', 1, 4), ('bow', 'Longbow', 1, 4), ('staff', 'Runestaff', 0, 4),
            ('sword', 'Duelist Blade', 2, 2), ('axe', 'Reaver', 3, 1),
            ('dagger', 'Fang', 1, 2), ('bow', 'Heartseeker', 4, 1),
            ('mace', 'Oathbreaker', 4, 1), ('staff', 'Starwood Focus', 3, 1)]
        for kind, name, bonus, weight in additions:
            tier['drops'].append({'name': prefix + ' ' + tier['name'] + ' ' + name,
                'weapon_type_slug': kind, 'base_damage': base + bonus,
                'weight': weight + (3 if kind == preferred and weight > 1 else 0)})
    return rules


def upgrade(conn):
    if inspect(conn).has_table('quest_templates'):
        table = Table('quest_templates', MetaData(), autoload_with=conn)
        for row in conn.execute(select(table.c.slug, table.c.journey)).mappings().all():
            original = row['journey'] or {}
            rules = upgrade_loot(deepcopy(original), row['slug'])
            if rules != original:
                conn.execute(table.update().where(table.c.slug == row['slug']).values(journey=rules))
    if inspect(conn).has_table('abilities'):
        table = Table('abilities', MetaData(), autoload_with=conn)
        if 'allowed_weapon_tags' in table.c:
            for row in conn.execute(select(table.c.id, table.c.allowed_weapon_tags).where(table.c.slug == 'attack')).mappings():
                if row['allowed_weapon_tags'] == ['melee']:
                    conn.execute(table.update().where(table.c.id == row['id']).values(allowed_weapon_tags=['melee', 'ranged']))
    # Running quests and existing raid rotations keep their saved loot rules.
