from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.group_journeys import earn_group_loot, loot_tier, GroupRules
from app.migrations.v020_push_loot import rebalance, upgrade
from test_shared_loot import EFFECTS, source_rules, odds


@pytest.mark.parametrize('pushes', [0, 1, 2, 3, 4, 5, 6, 20])
def test_exact_push_odds_and_cap(pushes):
    rules = rebalance(source_rules(), EFFECTS)
    groups = rules['encounter_groups']
    GroupRules.model_validate(groups)
    tier = loot_tier(groups, {'pushes': pushes, 'groups_cleared': 99, 'push_streak': 0})
    bonus = min(pushes, 5)
    assert odds(tier, lambda d: bool(d.get('weapon_type_slug'))) == 5 + bonus
    assert odds(tier, lambda d: EFFECTS.get(d.get('consumable_slug')) == 'orb') == 10 + bonus
    assert odds(tier, lambda d: EFFECTS.get(d.get('consumable_slug')) in ('heal', 'buff', 'cleanse')) == 20
    assert 100 - tier['drop_chance_percent'] == 65 - bonus * 2
    assert not any(EFFECTS.get(d.get('consumable_slug')) == 'essence' for d in tier['drops'])


def test_weapon_rolls_repeat_after_wins_and_misses(monkeypatch):
    quest = SimpleNamespace(rewards={'journey': rebalance(source_rules(), EFFECTS)})
    calls = []
    def roll(tier, rng):
        calls.append(tier['drop_chance_percent'])
        return None if len(calls) == 2 else deepcopy(next(d for d in tier['drops'] if d.get('weapon_type_slug')))
    monkeypatch.setattr('app.group_journeys.roll_loot', roll)
    for pushes in range(8):
        quest.rewards.setdefault('journey_progress', {})['pushes'] = pushes
        earn_group_loot(quest)
    progress = quest.rewards['journey_progress']
    assert calls == [35, 37, 39, 41, 43, 45, 45, 45]
    assert len(progress['loot_stash']) == 7
    assert progress['loot_rolls'][1]['item'] is None


def test_corrective_migration_preserves_saved_snapshots(monkeypatch):
    # Run the existing migration preservation harness against the corrective migration.
    import test_shared_loot as harness
    from app.migrations.v019_shared_loot import rebalance as old_rebalance
    original_source = harness.source_rules
    monkeypatch.setattr(harness, 'source_rules', lambda: old_rebalance(original_source(), EFFECTS))
    monkeypatch.setattr(harness, 'rebalance', rebalance)
    monkeypatch.setattr(harness, 'upgrade', upgrade)
    harness.test_migration_is_idempotent_preserves_runs_rotations_and_unrelated_rules()
