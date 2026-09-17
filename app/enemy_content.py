"""Initial enemy content; runtime behavior reads the database catalogs."""
# slug, name, taxonomy, HP, power, armor, speed, weapon type, ability assignments
ENEMY_SPECS = [
 ('undead','Restless Undead','undead',(48,62),(5,7),(1,3),(3,5),None,['rending_claws']),
 ('acolyte','Acolyte','cultist',(42,55),(5,7),(0,2),(5,8),'staff',['shadow_hex']),
 ('leech','Giant Leech','vermin',(24,35),(3,5),(0,1),(2,4),None,['rending_claws']),
 ('damned','The Damned','undead',(65,80),(7,9),(2,4),(4,6),None,['shadow_hex']),
 ('bog-bullfrog','Bog Bullfrog','amphibian',(50,65),(5,7),(1,3),(4,7),None,['tongue_lash']),
 ('overgrown-toad','Overgrown Toad','amphibian',(65,85),(6,8),(3,5),(2,4),None,['tongue_lash']),
 ('troll','Troll','giant',(115,145),(10,13),(4,7),(3,5),None,['crushing_blow','monster_mend']),
 ('orc','Orc','orc',(72,90),(7,10),(3,5),(5,8),'axe',['enemy_chop']),
 ('ogre','Ogre','giant',(125,155),(11,14),(3,6),(2,4),'mace',['crushing_blow']),
 ('uncean','Uncean','eldritch',(100,125),(11,14),(3,6),(6,9),None,['shadow_hex','dread_wave']),
 ('possessed','Possessed Wanderer','possessed',(60,78),(7,9),(1,3),(7,10),None,['rending_claws','shadow_hex']),
 ('priest','Fallen Priest','humanoid',(58,72),(6,8),(1,3),(5,7),'mace',['profane_smite','dark_mending']),
 ('vampire','Vampire','undead',(100,125),(10,13),(3,5),(10,14),None,['blood_fangs','monster_mend']),
 ('dragon','Cinder Dragon','dragon',(200,250),(16,20),(8,12),(6,9),None,['dragon_breath','crushing_blow']),
 ('ent','Ent','plant',(120,150),(10,13),(7,10),(2,4),None,['root_slam']),
 ('hound','Wild Hound','beast',(38,50),(4,6),(0,2),(10,14),None,['blood_fangs']),
 ('axe-thrower','Orc Axe Thrower','orc',(55,70),(7,10),(1,3),(6,9),'throwing-axe',['hurled_axe']),
 ('firemancer','Firemancer','humanoid',(58,75),(8,11),(0,2),(7,10),'staff',['flame_burst','profane_smite']),
 ('monk','Exiled Monk','humanoid',(70,90),(8,11),(2,4),(10,13),None,['flurry','focused_guard']),
]


def ability(slug, name, *, effect='damage', multiplier=1.0, power=0, target='enemy', limit=1, cooldown=0, tags=None):
    return dict(slug=slug, name=name, description=name + ': ' + ('strike foes' if effect=='damage' else 'restore allies' if effect=='heal' else 'brace against incoming attacks') + '.',
                effect_type=effect, damage_multiplier=multiplier, power=power, target_type=target,
                max_targets=limit, cooldown_type='turn', cooldown_value=cooldown,
                requires_weapon=bool(tags), allowed_weapon_tags=tags or [], cost_type='None', starter=False)


ABILITIES = [
 ability('rending_claws','Rending Claws'), ability('shadow_hex','Shadow Hex',multiplier=1.1),
 ability('tongue_lash','Tongue Lash'), ability('crushing_blow','Crushing Blow',multiplier=1.2),
 ability('monster_mend','Monstrous Recovery',effect='heal',multiplier=None,power=18,target='self',cooldown=3),
 ability('enemy_chop','Savage Chop',multiplier=1.1,tags=['melee']),
 ability('dread_wave','Dread Wave',multiplier=1.0,limit=None,cooldown=2),
 ability('profane_smite','Profane Smite'),
 ability('dark_mending','Dark Mending',effect='heal',multiplier=None,power=12,target='party',limit=None,cooldown=3),
 ability('blood_fangs','Fang Strike'),
 ability('dragon_breath','Dragon Breath',multiplier=1.25,limit=None,cooldown=2),
 ability('root_slam','Root Slam',multiplier=1.1,limit=2),
 ability('hurled_axe','Hurled Axe',multiplier=1.0,tags=['thrown']),
 ability('flame_burst','Flame Burst',multiplier=1.0,limit=None,cooldown=2),
 ability('flurry','Monk Flurry',multiplier=1.2),
 ability('focused_guard','Focused Guard',effect='guard',multiplier=None,power=4,target='self',cooldown=3),
]
