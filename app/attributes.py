"""Authoritative character attribute formulas; snapshots freeze adventure balance."""
DESCRIPTIONS = {
    'Might': 'Adds 0.2% flinch chance per point (with Speed, capped at 20%). Each point adds 2% weapon damage; combines with Precision.',
    'Precision': 'Adds 0.4% critical chance per point (with Luck, capped at 40%). Each point adds 1% weapon damage; combines with Might.',
    'Affinity': 'Each point adds 2% nonweapon damage and 1% ability healing; combines with Willpower.',
    'Willpower': 'Each point adds 1 maximum HP, 1% nonweapon damage, and 1% ability healing.',
    'Vitality': 'Each point adds 3 maximum HP and 0.5% resistance to damage-over-time effects.',
    'Defense': 'Each point adds 0.6% direct damage reduction; combines with Agility, capped at 60%.',
    'Agility': 'Each point adds 0.3% direct damage reduction; combines with Defense, capped at 60%.',
    'Awareness': 'Each point adds 0.2 Guard protection; combines with Speed and rounds down.',
    'Speed': 'Adds 0.2% flinch chance per point (with Might, capped at 20%). Each point adds 0.2 Guard protection; combines with Awareness and rounds down.',
    'Luck': 'Adds 0.2% critical chance per point (with Precision, capped at 40%). Each point adds 0.5% damage-over-time resistance; combines with Vitality, capped at 50%.',
}


def derived_stats(attributes):
    def a(name):
        value = (attributes or {}).get(name, 0)
        return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else 0
    return {
        'critical_chance_percent': min(40, (4 * a('Precision') + 2 * a('Luck')) / 10),
        'flinch_chance_percent': min(20, (a('Might') + a('Speed')) / 5),
        'max_hp': 100 + 3 * a('Vitality') + a('Willpower'),
        'weapon_bonus_percent': 2 * a('Might') + a('Precision'),
        'spell_bonus_percent': 2 * a('Affinity') + a('Willpower'),
        'healing_bonus_percent': a('Affinity') + a('Willpower'),
        'damage_reduction_percent': min(60, (6 * a('Defense') + 3 * a('Agility')) / 10),
        'guard_bonus': (a('Awareness') + a('Speed')) // 5,
        'status_resistance_percent': min(50, (a('Luck') + a('Vitality')) / 2),
    }
