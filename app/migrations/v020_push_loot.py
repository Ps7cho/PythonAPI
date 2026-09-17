"""Repeatable loot with progressively better odds for committed pushes."""
from copy import deepcopy
from functools import reduce
from math import gcd, lcm

from sqlalchemy import MetaData, Table, inspect, select
from app.migrations.v019_shared_loot import rebalance as combine

POLICY = 'push-rewards-v1'


def rebalance(rules, effects):
    groups = rules.get('encounter_groups')
    if not groups or not groups.get('loot_tiers') or groups.get('loot_policy') == POLICY:
        return rules
    # Reuse the previous consolidation to retain every weapon tier and item variant.
    source = combine(deepcopy(rules), effects)['encounter_groups']['loot_tiers'][0]['drops']
    pools = {'weapon': [], 'orb': [], 'consumable': []}
    for drop in source:
        effect = effects.get(drop.get('consumable_slug'))
        if effect == 'essence':
            continue
        kind = 'weapon' if drop.get('weapon_type_slug') else 'orb' if effect == 'orb' else 'consumable'
        pools[kind].append(drop)
    if not any(pools.values()):
        # Preserve a valid inert table for custom essence-only content.
        groups.update(loot_policy=POLICY, loot_rng='run',
                      loot_tiers=[dict(name='No eligible loot', drop_chance_percent=0, drops=source)])
        return rules
    totals = {kind: sum(d['weight'] for d in pool) for kind, pool in pools.items() if pool}
    scale = lcm(*totals.values())
    tiers = []
    for pushes in range(6):
        chances = {'weapon': 5 + pushes, 'orb': 10 + pushes, 'consumable': 20}
        drops = []
        for kind, total in totals.items():
            for original in pools[kind]:
                drop = deepcopy(original)
                drop['weight'] *= chances[kind] * scale // total
                drops.append(drop)
        divisor = reduce(gcd, (d['weight'] for d in drops))
        for drop in drops:
            drop['weight'] //= divisor
        tiers.append(dict(name='Initial loot' if pushes == 0 else f'Push {pushes}' + ('+ (cap)' if pushes == 5 else ''),
                          drop_chance_percent=sum(chances[kind] for kind in totals), drops=drops))
    groups.update(loot_policy=POLICY, loot_rng='run', loot_tiers=tiers)
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
