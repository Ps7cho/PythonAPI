from copy import deepcopy
from fractions import Fraction
from types import SimpleNamespace

from sqlalchemy import Column, JSON, MetaData, String, Table, create_engine, select

from app.group_journeys import GroupRules, earn_group_loot
from app.migrations.v019_shared_loot import rebalance, upgrade
from app.quest_templates import JOURNEYS
from app.consumables import DEFINITIONS
from app.orb_content import ORBS, ACTIVE_ESSENCES


EFFECTS = {item['slug']: item['effect'] for item in [*DEFINITIONS, *ORBS]}
EFFECTS.update({'essence-' + name: 'essence' for name in ACTIVE_ESSENCES})


def source_rules():
    return deepcopy(next(t['journey'] for t in JOURNEYS if t['slug'] == 'village-patrol'))


def odds(tier, predicate):
    return Fraction(tier['drop_chance_percent'] * sum(d['weight'] for d in tier['drops'] if predicate(d)),
                    sum(d['weight'] for d in tier['drops']))


def test_combined_pool_exact_odds_retains_all_weapon_tiers_and_custom_quantities():
    original = source_rules()
    original['encounter_groups']['loot_tiers'][0]['drops'][0]['base_damage'] = 29
    rules = rebalance(deepcopy(original), EFFECTS)
    first, later = rules['encounter_groups']['loot_tiers']
    GroupRules.model_validate(rules['encounter_groups'])
    weapons = lambda d: bool(d.get('weapon_type_slug'))
    identities = lambda tiers: {(d['name'], d['weapon_type_slug'], d['base_damage'])
                                for tier in tiers for d in tier['drops'] if weapons(d)}
    assert identities([first]) == identities(original['encounter_groups']['loot_tiers'])
    for tier, weapon_chance in [(first, 5), (later, 0)]:
        assert odds(tier, weapons) == weapon_chance
        assert odds(tier, lambda d: EFFECTS.get(d.get('consumable_slug')) == 'orb') == 15
        assert odds(tier, lambda d: EFFECTS.get(d.get('consumable_slug')) == 'essence') == 5
        assert odds(tier, lambda d: EFFECTS.get(d.get('consumable_slug')) in ('heal', 'buff', 'cleanse')) == 65
        assert all(d['quantity'] == 2 for d in tier['drops'] if EFFECTS.get(d.get('consumable_slug')) in ('heal', 'buff', 'cleanse'))
    assert rebalance(deepcopy(rules), EFFECTS) == rules


def test_first_miss_never_retries_weapon_opportunity_even_after_many_groups(monkeypatch):
    rules = rebalance(source_rules(), EFFECTS)
    quest = SimpleNamespace(rewards={'journey': rules})
    calls = []
    def choose(tier, rng):
        calls.append(tier)
        if len(calls) == 1:
            return None
        assert not any(d.get('weapon_type_slug') for d in tier['drops'])
        return deepcopy(tier['drops'][0])
    monkeypatch.setattr('app.group_journeys.roll_loot', choose)
    for _ in range(12):
        earn_group_loot(quest)
    progress = quest.rewards['journey_progress']
    assert len(calls) == 12 and progress['groups_cleared'] == 12
    assert progress['loot_rolls'][0]['item'] is None
    assert len(progress['loot_stash']) == 11
    assert all(d.get('consumable_slug') for d in progress['loot_stash'])


def test_migration_is_idempotent_preserves_runs_rotations_and_unrelated_rules():
    engine = create_engine('sqlite:///:memory:')
    metadata = MetaData()
    templates = Table('quest_templates', metadata, Column('slug', String, primary_key=True), Column('journey', JSON))
    items = Table('consumables', metadata, Column('slug', String, primary_key=True), Column('effect', String))
    quests = Table('quests', metadata, Column('id', String, primary_key=True), Column('rewards', JSON))
    rotations = Table('raid_rotations', metadata, Column('key', String, primary_key=True), Column('snapshot', JSON))
    metadata.create_all(engine)
    original = source_rules()
    try:
        with engine.begin() as conn:
            conn.execute(items.insert(), [dict(slug=slug, effect=effect) for slug, effect in EFFECTS.items()])
            conn.execute(templates.insert(), [dict(slug='test', journey=original), dict(slug='custom', journey={})])
            conn.execute(quests.insert().values(id='active', rewards={'journey': original}))
            conn.execute(rotations.insert().values(key='saved', snapshot={'settings': original}))
            upgrade(conn)
            once = dict(conn.execute(select(templates.c.slug, templates.c.journey)).all())
            upgrade(conn)
            assert dict(conn.execute(select(templates.c.slug, templates.c.journey)).all()) == once
            assert once['custom'] == {}
            assert once['test'] == rebalance(deepcopy(original), EFFECTS)
            assert conn.scalar(select(quests.c.rewards)) == {'journey': original}
            assert conn.scalar(select(rotations.c.snapshot)) == {'settings': original}
    finally:
        engine.dispose()
