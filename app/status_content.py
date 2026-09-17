"""Initial catalog values; migrations and seeds preserve later database edits."""
EFFECTS = [
    dict(slug='bleed', name='Bleed', damage=2, duration=3, max_stacks=3),
    dict(slug='poison', name='Poison', damage=3, duration=3, max_stacks=3),
    dict(slug='sin', name='Sin', damage=3, duration=3, max_stacks=1),
    dict(slug='necrosis', name='Necrosis', damage=4, duration=2, max_stacks=1),
]
PROFILES = {
    'humanoid': {}, 'undead': {'bleed': 100, 'poison': 100, 'necrosis': 50},
    'plant': {'bleed': 100, 'poison': 50}, 'amphibian': {'poison': 50},
    'eldritch': {'sin': 100, 'necrosis': 50}, 'possessed': {'sin': 50},
    'dragon': {'bleed': 50, 'poison': 50}, 'giant': {'bleed': 50},
    'cultist': {'sin': 50}, 'beast': {}, 'orc': {}, 'vermin': {'poison': 50},
}
ASSIGNMENTS = {'rending_claws': 'bleed', 'blood_fangs': 'bleed',
               'tongue_lash': 'poison', 'profane_smite': 'sin',
               'shadow_hex': 'necrosis'}

GRAMMAR_EFFECTS = [
    dict(slug='burn', name='Burn', damage=2, duration=3, max_stacks=5, rules={}),
    dict(slug='holy', name='Holy', damage=1, duration=3, max_stacks=5,
         rules={'harmful': False, 'periodic': 'none'}),
]

GRAMMAR_ABILITIES = [
    dict(slug='kindle', name='Kindle', description='Apply 2 Burn stacks.', target_type='enemy', max_targets=1,
         affliction_ops=[dict(op='apply', affliction='burn', stacks=2)]),
    dict(slug='fan_the_flames', name='Fan the Flames', description='Add 2 potency per Burn stack already on the target.', target_type='enemy', max_targets=1,
         affliction_ops=[dict(op='amplify', affliction='burn', power=2)]),
    dict(slug='wildfire', name='Wildfire', description='Copy up to 2 Burn stacks from the primary target to a second enemy.', target_type='enemy', max_targets=2,
         affliction_ops=[dict(op='spread', affliction='burn', stacks=2)]),
    dict(slug='blood_rite', name='Blood Rite', description='Consume up to 3 Bleed stacks; restore 5 HP to yourself per stack.', target_type='enemy', max_targets=1,
         affliction_ops=[dict(op='consume', affliction='bleed', stacks=3, effect='heal', power=5)]),
    dict(slug='venom_burst', name='Venom Burst', description='Detonate up to 3 Poison stacks for 8 direct damage each.', target_type='enemy', max_targets=1,
         affliction_ops=[dict(op='detonate', affliction='poison', stacks=3, power=8)]),
    dict(slug='absolution', name='Absolution', description='Convert up to 3 Sin stacks into Holy stacks on the target.', target_type='enemy', max_targets=1,
         affliction_ops=[dict(op='convert', affliction='sin', stacks=3, into='holy')]),
    dict(slug='purifying_light', name='Purifying Light', description='Cleanse up to 3 Bleed and 3 Poison stacks from an ally.', target_type='ally', max_targets=1,
         affliction_ops=[dict(op='cleanse', affliction='bleed', stacks=3), dict(op='cleanse', affliction='poison', stacks=3)]),
    dict(slug='lingering_venom', name='Lingering Venom', description='Preserve Poison for 2 rounds: it still ticks, but cannot expire or be removed.', target_type='enemy', max_targets=1,
         affliction_ops=[dict(op='preserve', affliction='poison', rounds=2)]),
    dict(slug='flashpoint', name='Flashpoint', description='Apply 2 Burn; crossing 3 stacks triggers 12 direct damage without consuming them.', target_type='enemy', max_targets=1,
         affliction_ops=[dict(op='apply', affliction='burn', stacks=2), dict(op='trigger', affliction='burn', threshold=3, power=12)]),
    dict(slug='judgment', name='Judgment', description='Exploit up to 5 Holy stacks for 4 direct damage each without consuming them.', target_type='enemy', max_targets=1,
         affliction_ops=[dict(op='exploit', affliction='holy', stacks=5, power=4)]),
    dict(slug='holy_resolve', name='Holy Resolve', description='Apply 2 Holy to yourself; convert 1 Holy into 2 combat Zeal.', target_type='self', max_targets=1,
         affliction_ops=[dict(op='apply', affliction='holy', stacks=2), dict(op='convert', affliction='holy', stacks=1, resource='zeal', power=2)]),
]
