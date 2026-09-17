"""One shared weapon opportunity, followed by repeatable supply loot."""
from copy import deepcopy
from math import gcd, lcm
from functools import reduce

from sqlalchemy import MetaData, Table, inspect, select

POLICY = 'shared-rare-weapons-v1'
CHANCES = {'weapon': 5, 'consumable': 65, 'orb': 15, 'essence': 5}


def rebalance(rules, effects):
    groups = rules.get('encounter_groups')
    if not groups or not groups.get('loot_tiers') or groups.get('loot_policy') == POLICY:
        return rules
    pools = {kind: [] for kind in CHANCES}
    seen = set()
    for tier in groups['loot_tiers']:
        for original in tier['drops']:
            drop = deepcopy(original)
            if drop.get('weapon_type_slug'):
                kind = 'weapon'
            else:
                effect = effects.get(drop.get('consumable_slug'))
                if effect is None:
                    raise ValueError('Loot references an unknown consumable: ' + str(drop.get('consumable_slug')))
                kind = effect if effect in ('orb', 'essence') else 'consumable'
            # Retain distinct weapon tiers and item quantities; remove repeated entries.
            identity = (kind, drop.get('weapon_type_slug'), drop.get('base_damage'),
                        drop.get('consumable_slug'), drop.get('quantity', 1), drop['name'])
            if identity not in seen:
                pools[kind].append(drop)
                seen.add(identity)

    def table(name, kinds):
        available = [kind for kind in kinds if pools[kind]]
        totals = {kind: sum(d['weight'] for d in pools[kind]) for kind in available}
        scale = lcm(*totals.values())
        drops = []
        for kind in available:
            for original in pools[kind]:
                drop = deepcopy(original)
                drop['weight'] *= CHANCES[kind] * scale // totals[kind]
                drops.append(drop)
        divisor = reduce(gcd, (d['weight'] for d in drops), 0) or 1
        for drop in drops:
            drop['weight'] //= divisor
        return dict(name=name, drop_chance_percent=sum(CHANCES[kind] for kind in available), drops=drops)

    first = table('Shared loot', CHANCES)
    supplies = table('Supplies', ('consumable', 'orb', 'essence'))
    if not supplies['drops']:
        # A custom weapon-only table still needs a valid zero-chance repeat tier.
        supplies['drops'] = deepcopy(first['drops'])
    groups.update(loot_policy=POLICY, loot_rng='run', loot_tiers=[first, supplies])
    return rules


def upgrade(conn):
    if not inspect(conn).has_table('quest_templates'):
        return
    metadata = MetaData()
    templates = Table('quest_templates', metadata, autoload_with=conn)
    items = Table('consumables', metadata, autoload_with=conn)
    effects = dict(conn.execute(select(items.c.slug, items.c.effect)).all())
    for row in conn.execute(select(templates.c.slug, templates.c.journey)).mappings().all():
        original = row['journey'] or {}
        rules = rebalance(deepcopy(original), effects)
        if rules != original:
            conn.execute(templates.update().where(templates.c.slug == row['slug']).values(journey=rules))
    # Active quests and existing raid rotations retain their saved rules.
