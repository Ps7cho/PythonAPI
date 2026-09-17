"""Initial orb recipes; runtime outcomes are read from database mappings."""
ACTIVE_ESSENCES = ('dark', 'holy', 'magic', 'sin', 'swift', 'fire', 'blood', 'balance', 'might')
ORBS = [
    dict(slug='evasion-orb', name='Evasion Orb', effect='orb', power=0,
         description='Use on an absorbed essence in the village to learn its evasive ability.'),
    dict(slug='strike-orb', name='Strike Orb', effect='orb', power=0,
         description='Use on an absorbed essence in the village to learn its strike or magical attack.'),
]
VARIANTS = [
    ('dark', 'Shadow Veil', 'Shadow Bolt', []),
    ('holy', 'Sanctified Step', 'Smite', ['melee']),
    ('magic', 'Blink', 'Arcane Missile', []),
    ('sin', 'Tempting Feint', 'Sinful Strike', []),
    ('swift', 'Quick Step', 'Quick Strike', ['melee', 'ranged']),
    ('fire', 'Cinder Step', 'Fire Bolt', []),
    ('blood', 'Crimson Slip', 'Blood Strike', ['melee']),
    ('balance', 'Centered Step', 'Balanced Strike', ['melee', 'ranged']),
    ('might', 'Mighty Sidestep', 'Crushing Strike', ['melee']),
]


def recipes():
    for essence, evasion, strike, tags in VARIANTS:
        for orb, name in [('evasion-orb', evasion), ('strike-orb', strike)]:
            evade = orb == 'evasion-orb'
            yield orb, 'essence-' + essence, dict(
                slug=f'orb-{essence}-' + ('evasion' if evade else 'strike'), name=name,
                description=('Reposition evasively: 60% chance to dodge the next direct attack within two rounds. Does not avoid damage-over-time.' if evade else
                             'An essence-infused attack dealing 120% ' + ('weapon damage.' if tags else 'nonweapon attack power.')),
                effect_type='evade' if evade else 'damage', target_type='self' if evade else 'enemy',
                power=60 if evade else 0, damage_multiplier=None if evade else 1.2,
                requires_weapon=bool(tags) and not evade, allowed_weapon_tags=[] if evade else tags,
                cooldown_type='turn', cooldown_value=3 if evade else 1, max_targets=1,
                cost_type='None', starter=False, loadout_order=50)


def update_loot(rules):
    active = {'essence-' + name for name in ACTIVE_ESSENCES}
    for tier in rules.get('encounter_groups', {}).get('loot_tiers', []):
        tier['drops'] = [d for d in tier['drops'] if not (d.get('consumable_slug') or '').startswith('essence-') or d['consumable_slug'] in active]
        existing = {d.get('consumable_slug') for d in tier['drops']}
        for orb in ORBS:
            if orb['slug'] not in existing:
                tier['drops'].append(dict(name=orb['name'], consumable_slug=orb['slug'], quantity=1, weight=2))
    return rules
