"""Restore active essences inside the existing orb allocation."""
from copy import deepcopy
from functools import reduce
from math import gcd

from sqlalchemy import MetaData, Table, inspect, select
from app.migrations.v019_shared_loot import rebalance as combine
from app.migrations.v020_push_loot import rebalance as push_tables


def rebalance(rules, effects, essences):
    groups = rules.get('encounter_groups')
    if not groups or not groups.get('loot_tiers') or groups.get('shared_orb_essence_pool'):
        return rules
    source = combine(deepcopy(rules), effects)['encounter_groups']['loot_tiers'][0]['drops']
    active = {item['slug'] for item in essences}
    source = [d for d in source if effects.get(d.get('consumable_slug')) != 'essence' or d['consumable_slug'] in active]
    # Undo category-wide scaling while retaining relative item weights.
    for effect, base in [('orb', 2), ('essence', 1)]:
        entries = [d for d in source if effects.get(d.get('consumable_slug')) == effect]
        divisor = reduce(gcd, (d['weight'] for d in entries), 0) or 1
        for drop in entries:
            drop['weight'] = drop['weight'] // divisor * base
    existing = {d.get('consumable_slug') for d in source}
    for item in sorted(essences, key=lambda item: item['slug']):
        if item['slug'] not in existing:
            source.append(dict(name=item['name'], consumable_slug=item['slug'], quantity=1, weight=1))
    groups.update(loot_policy='combined-orb-essence-source',
                  loot_tiers=[dict(name='Combined pool', drops=source)])
    # Both item types participate in the same budget; execution still uses roll_loot.
    shared_effects = {slug: 'orb' if effect == 'essence' else effect for slug, effect in effects.items()}
    push_tables(rules, shared_effects)
    groups['shared_orb_essence_pool'] = True
    return rules


def catalog(conn):
    metadata = MetaData()
    items = Table('consumables', metadata, autoload_with=conn)
    definitions = Table('essence_definitions', metadata, autoload_with=conn)
    effects = dict(conn.execute(select(items.c.slug, items.c.effect)).all())
    essences = [dict(row) for row in conn.execute(select(items.c.slug, items.c.name)
        .join(definitions, definitions.c.consumable_slug == items.c.slug)
        .where(definitions.c.active.is_(True)).order_by(items.c.slug)).mappings()]
    return effects, essences


def upgrade(conn):
    if not inspect(conn).has_table('quest_templates'):
        return
    templates = Table('quest_templates', MetaData(), autoload_with=conn)
    effects, essences = catalog(conn)
    for row in conn.execute(select(templates.c.slug, templates.c.journey)).mappings().all():
        original = row['journey'] or {}
        rules = rebalance(deepcopy(original), effects, essences)
        if rules != original:
            conn.execute(templates.update().where(templates.c.slug == row['slug']).values(journey=rules))
