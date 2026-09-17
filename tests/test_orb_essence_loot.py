from copy import deepcopy
from fractions import Fraction

from sqlalchemy import Boolean, Column, JSON, MetaData, String, Table, create_engine, select
from app.group_journeys import GroupRules
from app.migrations.v020_push_loot import rebalance as previous
from app.migrations.v021_orb_essence_pool import rebalance, upgrade
from test_shared_loot import EFFECTS, source_rules, odds

ESSENCES = [dict(slug=slug, name=slug) for slug, effect in EFFECTS.items() if effect == 'essence']


def test_shared_pool_exact_odds_all_pushes_and_repaired_essences():
    for source in [source_rules(), previous(source_rules(), EFFECTS)]:
        rules = rebalance(source, EFFECTS, ESSENCES)
        validated = GroupRules.model_validate(rules['encounter_groups'])
        assert validated.shared_orb_essence_pool
        for pushes, tier in enumerate(rules['encounter_groups']['loot_tiers']):
            assert odds(tier, lambda d: bool(d.get('weapon_type_slug'))) == 5 + pushes
            assert odds(tier, lambda d: EFFECTS.get(d.get('consumable_slug')) == 'orb') == Fraction((10 + pushes) * 4, 13)
            assert odds(tier, lambda d: EFFECTS.get(d.get('consumable_slug')) == 'essence') == Fraction((10 + pushes) * 9, 13)
            assert odds(tier, lambda d: EFFECTS.get(d.get('consumable_slug')) in ('heal', 'buff', 'cleanse')) == 20
            assert 100 - tier['drop_chance_percent'] == 65 - 2 * pushes
        assert rebalance(deepcopy(rules), EFFECTS, ESSENCES) == rules


def test_pool_migration_preserves_runs_and_rotations_and_uses_active_catalog():
    engine = create_engine('sqlite:///:memory:')
    metadata = MetaData()
    templates = Table('quest_templates', metadata, Column('slug', String, primary_key=True), Column('journey', JSON))
    items = Table('consumables', metadata, Column('slug', String, primary_key=True), Column('name', String), Column('effect', String))
    definitions = Table('essence_definitions', metadata, Column('consumable_slug', String, primary_key=True), Column('active', Boolean))
    quests = Table('quests', metadata, Column('id', String, primary_key=True), Column('rewards', JSON))
    rotations = Table('raid_rotations', metadata, Column('key', String, primary_key=True), Column('snapshot', JSON))
    metadata.create_all(engine)
    original = previous(source_rules(), EFFECTS)
    try:
        with engine.begin() as conn:
            conn.execute(items.insert(), [dict(slug=slug, name=slug, effect=effect) for slug, effect in {**EFFECTS, 'retired': 'essence'}.items()])
            conn.execute(definitions.insert(), [dict(consumable_slug=e['slug'], active=True) for e in ESSENCES] + [dict(consumable_slug='retired', active=False)])
            conn.execute(templates.insert().values(slug='test', journey=original))
            conn.execute(quests.insert().values(id='active', rewards=original))
            conn.execute(rotations.insert().values(key='saved', snapshot=original))
            upgrade(conn)
            once = conn.scalar(select(templates.c.journey))
            upgrade(conn)
            assert conn.scalar(select(templates.c.journey)) == once
            assert once == rebalance(deepcopy(original), EFFECTS, ESSENCES)
            assert all(d.get('consumable_slug') != 'retired' for t in once['encounter_groups']['loot_tiers'] for d in t['drops'])
            assert conn.scalar(select(quests.c.rewards)) == original
            assert conn.scalar(select(rotations.c.snapshot)) == original
    finally:
        engine.dispose()
