"""Persist editable archetypes, effect chains, and rank value overrides."""
from sqlalchemy import inspect, select, text


def upgrade(conn):
    from app.models import AbilityArchetype
    AbilityArchetype.__table__.create(conn, checkfirst=True)
    columns = {c['name'] for c in inspect(conn).get_columns('abilities')}
    for name, ddl in {
        'archetype_slug': 'VARCHAR REFERENCES ability_archetypes(slug)',
        'effect_chain': "JSON NOT NULL DEFAULT '[]'",
        'rank_upgrades': "JSON NOT NULL DEFAULT '{}'",
        'duration_turns': 'INTEGER NOT NULL DEFAULT 3',
        'guard_percent': 'INTEGER NOT NULL DEFAULT 60',
    }.items():
        if name not in columns:
            conn.execute(text(f'ALTER TABLE abilities ADD COLUMN {name} {ddl}'))
    if 'duration_turns' not in columns:
        conn.execute(text("UPDATE abilities SET duration_turns=2 WHERE effect_type='evade'"))
    base = dict(effect_type='damage', target_type='enemy', power=10, damage_multiplier=None,
                requires_weapon=False, allowed_weapon_tags=[], cooldown_type='turn', cooldown_value=0,
                max_targets=1, duration_turns=3, guard_percent=60, effect_chain=[], rank_upgrades={}, ability_type='attack')
    templates = [
        ('direct_damage', 'Direct damage', 'Tune fixed damage or a power/weapon multiplier.', {}),
        ('weapon_strike', 'Weapon strike', 'Scale a hit from the equipped weapon.', dict(requires_weapon=True, damage_multiplier=1.0)),
        ('healing', 'Healing', 'Restore a percentage of maximum health.', dict(effect_type='heal', target_type='ally', power=20, ability_type='support')),
        ('guard', 'Guard', 'Reduce incoming direct damage for the current round.', dict(effect_type='guard', target_type='self', ability_type='defense')),
        ('evasion', 'Evasion', 'Chance to evade the next attack.', dict(effect_type='evade', target_type='self', power=60, duration_turns=2, ability_type='defense')),
        ('life_drain', 'Life drain', 'Heal yourself from actual damage dealt.', dict(effect_chain=[dict(id='drain', effect='heal', recipient='self', source='damage_dealt', value=50)])),
        ('party_siphon', 'Party siphon', 'Split healing from damage among living party members.', dict(effect_chain=[dict(id='share', effect='heal', recipient='party', source='damage_dealt', value=50, split=True)])),
        ('mana_siphon', 'Mana siphon', 'Convert actual damage dealt into mana.', dict(effect_chain=[dict(id='mana', effect='resource', recipient='self', source='damage_dealt', value=50, resource='mana')])),
        ('empower', 'Empower abilities', 'Temporarily boost an ally’s ability values.', dict(effect_type='affliction', target_type='ally', ability_type='support', effect_chain=[dict(id='empower', effect='modifier', recipient='targets', source='fixed', when='always', stat='power', operation='percent', modifier=25, duration=2)])),
        ('weaken', 'Weaken abilities', 'Temporarily reduce an opponent’s ability values.', dict(effect_type='affliction', effect_chain=[dict(id='weaken', effect='modifier', recipient='targets', source='fixed', when='always', stat='power', operation='percent', modifier=-25, duration=2)])),
    ]
    table = AbilityArchetype.__table__
    for slug, name, description, overrides in templates:
        if not conn.execute(select(table.c.slug).where(table.c.slug == slug)).first():
            conn.execute(table.insert().values(slug=slug, name=name, description=description, definition={**base, **overrides}))
