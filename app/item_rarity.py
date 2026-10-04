"""Shared rarity names and empty future effect-slot capacity."""
from random import SystemRandom

RARITIES = ('common', 'uncommon', 'rare', 'epic', 'legendary')
SLOTS = {name: index for index, name in enumerate(RARITIES)}


def roll_rarity(rng=None):
    rng = rng or SystemRandom()
    roll = rng.randrange(100)
    return next(name for limit, name in ((55, 'common'), (83, 'uncommon'),
                                         (95, 'rare'), (99, 'epic'), (100, 'legendary'))
                if roll < limit)


def item_rarity(name):
    rarity = name if name in SLOTS else 'common'
    return {'rarity': rarity, 'effect_slots': SLOTS[rarity], 'slotted_effects': []}
