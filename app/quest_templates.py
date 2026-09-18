from copy import deepcopy

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.database import SessionLocal, get_db
from app.models import QuestTemplate, RestPolicy


GOBLIN_TROUBLE = {
    "slug": "goblin-trouble",
    "name": "Goblin Trouble",
    "difficulty": 2,
    "min_encounters": 4,
    "max_encounters": 7,
    "region": "Mosswood",
    "enemy_pool": ["Goblin", "Goblin Archer", "Wolf", "Hobgoblin"],
    "possible_rewards": ["Gold", "Weapons", "Armor", "Essence"],
}


JOURNEYS = [
    dict(slug="village-patrol", name="The Mosswood Trail", difficulty=1, min_encounters=2, max_encounters=2,
         region="Mosswood", enemy_pool=["Goblin", "Wolf", "Goblin Archer"], possible_rewards=["Gold", "Experience"],
         journey=dict(kind="quest", description="A short patrol beyond the village. Track a lone prowler, then clear a goblin lookout. Return with coin and experience; there is no camp rest on this short route.",
                      stages=[dict(description="Tracks at the forest edge: a lone goblin or wolf.", groups=[["goblin"], ["wolf"]]),
                              dict(description="The lookout: a goblin, sometimes accompanied by an archer.", groups=[["goblin-archer"], ["goblin", "goblin-archer"]])],
                      gold=10, experience=8, completion_gold=5, completion_experience=5,
                      enemy_health_multiplier=0.6, enemy_power_multiplier=0.6)),
    dict(slug="mosswood-epic", name="Into the Hollow", difficulty=3, min_encounters=5, max_encounters=5,
         region="Deep Mosswood", enemy_pool=["Wolf", "Goblin", "Goblin Archer", "Hobgoblin"], possible_rewards=["Gold", "Experience"],
         journey=dict(kind="epic", description="A long expedition into occupied woodland. Expect wolf packs, mixed goblin patrols, and a hobgoblin stronghold. You have one camp rest for the whole expedition. Press on without resting to build a completion bonus, or return early with earned rewards. Greater rewards await those who finish; you can return early from camp with what you have earned.",
                      stages=[dict(description="Forest crossing: a lone wolf or goblin scout.", groups=[["wolf"], ["goblin"]]),
                              dict(description="Hunting grounds: two wolves circle the trail.", groups=[["wolf", "wolf"]]),
                              dict(description="Ambush: a goblin fights beside an archer.", groups=[["goblin", "goblin-archer"]]),
                              dict(description="Ruined watchtower: a hobgoblin or two archers guard the path.", groups=[["hobgoblin"], ["goblin-archer", "goblin-archer"]]),
                              dict(description="The hollow: a hobgoblin commander and its goblin escort.", groups=[["hobgoblin", "goblin"]])],
                      gold=18, experience=15, completion_gold=25, completion_experience=30,
                      camp_rest="camp", max_rests=1, push_xp_tiers=[5, 10, 15], push_gold=10, push_experience=8, enemy_health_multiplier=0.65, enemy_power_multiplier=0.65)),
]


# Additional routes reuse the same validated journey and enemy-group systems.
for slug, name, kind, region, description, groups, gold, xp, bonus, health, power in [
    ("wolf-tracks", "Wolf Tracks", "quest", "Pine Ridge", "A short hunt along the ridge. Expect a lone wolf, then a small pack. No camp rests; return with pelts represented by the village's gold bounty.", [["wolf"], ["wolf", "wolf"]], 12, 10, 8, .55, .55),
    ("roadside-contract", "The Roadside Contract", "quest", "Old Trade Road", "Clear three stretches of the trade road. Bandits work alone before joining a goblin patrol. More encounters and a larger village bounty; no camp rest.", [["roadside-bandit"], ["goblin-archer"], ["roadside-bandit", "goblin"]], 14, 12, 12, .55, .55),
    ("packlands", "Through the Packlands", "epic", "Northern Pines", "Four battles through wolf territory and a goblin hunting camp. You have one camp rest. Press on to build a bonus paid only if you finish; returning early keeps ordinary battle rewards.", [["wolf"], ["wolf", "wolf"], ["goblin", "wolf"], ["hobgoblin"]], 17, 14, 25, .6, .6),
    ("ironwood-siege", "The Ironwood Siege", "epic", "Ironwood Ruins", "Six battles against scouts, archers and hobgoblin warbands. One camp rest must last the entire siege. Pressing on increases your completion bonus, but defeat or retreat forfeits that bonus.", [["goblin"], ["goblin", "goblin-archer"], ["hobgoblin"], ["goblin-archer", "goblin-archer"], ["hobgoblin", "goblin"], ["hobgoblin", "goblin-archer"]], 22, 18, 40, .65, .65),
]:
    template = deepcopy(JOURNEYS[1 if kind == "epic" else 0])
    template.update(slug=slug, name=name, region=region, min_encounters=len(groups), max_encounters=len(groups),
                    difficulty=4 if slug == "ironwood-siege" else 2,
                    enemy_pool=sorted({enemy.replace('-', ' ').title() for group in groups for enemy in group}))
    template['journey'].update(kind=kind, description=description, gold=gold, experience=xp,
                               completion_gold=bonus, completion_experience=bonus,
                               enemy_health_multiplier=health, enemy_power_multiplier=power,
                               stages=[dict(description="Stage " + str(i+1) + ": " + " and ".join(s.replace('-', ' ') for s in group) + ".", groups=[group]) for i, group in enumerate(groups)])
    JOURNEYS.append(template)


# Rank contracts reuse the same journey rules, encounters, rests, and reward resolver.
for rank, name, base, xp, gold in [
    ("bronze", "Bronze: Vanguard Contract", 0, 180, 35),
    ("silver", "Silver: Hollow Expedition", 1, 420, 60),
]:
    template = deepcopy(JOURNEYS[base])
    template.update(slug=rank + "-contract", name=name)
    template["journey"].update(required_rank=rank, experience=xp, gold=gold,
        completion_experience=xp, completion_gold=gold,
        description=rank.title() + " guild contract. Every party member needs " + rank.title() +
                    " rank. Veteran enemies guard an increased XP and gold payout.",
        enemy_health_multiplier=.9, enemy_power_multiplier=.7)
    JOURNEYS.append(template)


from app.migrations.v006_encounter_groups import default_groups

for template in JOURNEYS:
    template['journey']['encounter_groups'] = default_groups(template['journey'].get('required_rank', 'iron'))
    low, high = (3, 5) if template['journey']['kind'] == 'epic' else (2, 3)
    template['journey']['encounter_groups'].update(size=low, min_size=low, max_size=high)
    template['journey']['death_policy'] = 'rescue_on_return'
    template['journey'].setdefault('max_rests', 0)
    template['journey'].setdefault('push_xp_tiers', [5, 10, 15])


for cadence, low, high in [('daily', 10, 12), ('weekly', 13, 15)]:
    groups = default_groups('bronze')
    groups.update(size=4, min_size=3, max_size=5, health_step_percent=5, power_step_percent=3)
    for tier in groups['loot_tiers']:
        tier['name'] = 'Raid ' + tier['name']
        for drop in tier['drops']:
            drop['name'] = 'Raid ' + drop['name']
    JOURNEYS.append(dict(slug=cadence + '-raid', name='The Hollow Convergence' if cadence == 'daily' else 'The Ashen Crown',
        difficulty=5 if cadence == 'daily' else 6, min_encounters=low, max_encounters=high,
        region='Hollow Citadel' if cadence == 'daily' else 'Ashen Depths',
        enemy_pool=['Goblin', 'Wolf', 'Hobgoblin', 'Raid bosses'], possible_rewards=['Raid weapons', 'Gold', 'Experience'],
        journey=dict(kind='raid', description='A fixed ' + cadence + ' expedition with three boss checkpoints. Death is permanent.',
            stages=[dict(description='Citadel patrol', groups=[['goblin'], ['wolf'], ['goblin', 'goblin-archer'], ['hobgoblin']])],
            gold=30 if cadence == 'daily' else 45, experience=60 if cadence == 'daily' else 90,
            completion_gold=150, completion_experience=300, camp_rest='camp', max_rests=2,
            push_gold=15, push_experience=20, push_xp_tiers=[5, 10, 15], required_rank='iron',
            enemy_health_multiplier=.65, enemy_power_multiplier=.6, encounter_groups=groups,
            death_policy='permanent', bank_rewards=True, repeat_groups=False,
            raid=dict(cadence=cadence, min_encounters=low, max_encounters=high,
                      bosses=['raid-packlord', 'raid-warden', 'raid-sovereign']))))


from app.migrations.v008_loot_chances import upgrade_loot
from app.consumables import add_loot
from app.essences import add_essence_loot
from app.orb_content import update_loot
for template in JOURNEYS:
    update_loot(add_essence_loot(add_loot(upgrade_loot(template['journey'], template['slug']))))


# Additional routes reuse encounter groups, rest budgets, rank gates, and loot tables.
from app.enemy_content import ENEMY_SPECS
from app.migrations.v009_enemy_ecology import expand_raid
for route in JOURNEYS:
    expand_raid(route['journey'])

for slug, name, kind, rank, region, groups, gold, xp in [
    ('bogwater-trail', 'The Bogwater Trail', 'quest', 'iron', 'Bogwater Marsh',
     [['leech','leech'],['bog-bullfrog'],['overgrown-toad','leech']], 14, 12),
    ('damned-chapel', 'Chapel of the Damned', 'quest', 'bronze', 'Blackbell Crypt',
     [['undead','acolyte'],['damned','possessed'],['priest','vampire']], 35, 180),
    ('broken-banner', 'The Broken Banner', 'epic', 'bronze', 'Orc Borderlands',
     [['hound','orc'],['axe-thrower','orc'],['troll'],['ogre','monk']], 38, 200),
    ('cinderwilds', 'The Cinderwilds', 'epic', 'silver', 'Ancient Cinderwood',
     [['ent'],['firemancer','monk'],['possessed','ent'],['uncean'],['dragon']], 65, 450),
]:
    template = deepcopy(next(t for t in JOURNEYS if t['journey']['kind']==kind))
    names = {s[0]:s[1] for s in ENEMY_SPECS}
    template.update(slug=slug,name=name,region=region,min_encounters=len(groups),max_encounters=len(groups),
        enemy_pool=sorted({names[s] for group in groups for s in group}), difficulty={'iron':2,'bronze':4,'silver':5}[rank])
    rules = template['journey']
    rules.update(required_rank=rank,description='Explore ' + region + ' and challenge its inhabitants.',gold=gold,experience=xp,
        completion_gold=gold,completion_experience=xp,
        stages=[dict(description=' and '.join(names[s] for s in group),groups=[group]) for group in groups])
    low, high = (2,3) if kind=='quest' else (3,5)
    rules['encounter_groups']=default_groups(rank)
    rules['encounter_groups'].update(size=low,min_size=low,max_size=high)
    update_loot(add_essence_loot(add_loot(upgrade_loot(rules,slug))))
    JOURNEYS.append(template)


class QuestTemplateRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    slug: str
    name: str
    difficulty: int
    min_encounters: int
    max_encounters: int
    region: str
    enemy_pool: list[str]
    possible_rewards: list[str]
    journey: dict = Field(default_factory=dict)


def seed_quest_templates() -> None:
    with SessionLocal.begin() as db:
        from app.migrations.v021_orb_essence_pool import rebalance, catalog
        effects, essences = catalog(db.connection())
        insert = sqlite_insert if db.bind.dialect.name == "sqlite" else postgres_insert
        for item in JOURNEYS:
            item = deepcopy(item)
            rebalance(item['journey'], effects, essences)
            db.execute(insert(QuestTemplate).values(**item).on_conflict_do_nothing(index_elements=["slug"]))
        for slug, settings in {"camp": dict(heal_percent=35, turns=3, seconds=300),
                               "village": dict(heal_percent=100, clear_cooldowns=True)}.items():
            db.execute(insert(RestPolicy).values(slug=slug, settings=settings).on_conflict_do_nothing(index_elements=["slug"]))
        # Preserve catalog edits in Neon and tolerate simultaneous server startups.
        db.execute(insert(QuestTemplate).values(**GOBLIN_TROUBLE)
                   .on_conflict_do_nothing(index_elements=["slug"]))


router = APIRouter(prefix="/api/quest-templates", tags=["quest templates"])


@router.get("", response_model=list[QuestTemplateRead])
def list_templates(db: Session = Depends(get_db)):
    return read_templates(db, db.scalars(select(QuestTemplate).order_by(QuestTemplate.name)).all())


@router.get("/{slug}", response_model=QuestTemplateRead)
def get_template(slug: str, db: Session = Depends(get_db)):
    template = db.get(QuestTemplate, slug)
    if template is None:
        raise HTTPException(404, "Quest template not found.")
    return read_templates(db, [template])[0]


def read_template(template):
    data = QuestTemplateRead.model_validate(template).model_dump()
    if data['journey'].get('raid'):
        from app.raids import rotation_info
        data['journey'] = deepcopy(data['journey'])
        data['journey']['raid']['rotation'] = rotation_info(template.slug, data['journey']['raid']['cadence'])
    return data


def read_templates(db, templates):
    from random import Random
    from app.models import RaidRotation
    data = [read_template(t) for t in templates]
    raids = [d['journey']['raid'] for d in data if d['journey'].get('raid')]
    from app.raids import rotation_completions
    completions = rotation_completions(db, [r['rotation']['seed'] for r in raids])
    keys = [r['rotation']['key'] for r in raids]
    saved = {r.key: r for r in db.scalars(select(RaidRotation).where(RaidRotation.key.in_(keys)))} if keys else {}
    for entry in data:
        raid = entry['journey'].get('raid')
        if not raid:
            continue
        info = raid['rotation']
        info['completions'] = completions[info['seed']]
        info['completion_count'] = len(info['completions'])
        rotation = saved.get(info['key'])
        count = len(rotation.snapshot['plan']) if rotation else Random(info['seed']).randint(raid['min_encounters'], raid['max_encounters'])
        info['encounter_count'] = count
        info['boss_encounters'] = [count // 3, count * 2 // 3, count]
        if rotation:
            entry['journey']['encounter_groups'] = deepcopy(rotation.snapshot['settings']['encounter_groups'])
    from app.models import WeaponType, Consumable
    types = {t.slug: t for t in db.scalars(select(WeaponType))}
    effects = dict(db.execute(select(Consumable.slug, Consumable.effect)).all())
    def category(drop):
        if drop.get('weapon_type_slug'):
            return 'weapon'
        effect = effects.get(drop.get('consumable_slug'))
        return effect if effect in ('orb', 'essence') else 'consumable'
    for entry in data:
        groups = entry['journey'].get('encounter_groups')
        if not groups:
            continue
        entry['journey']['loot_tables'] = []
        for index, tier in enumerate(groups['loot_tiers']):
            chance = tier.get('drop_chance_percent', 100)
            total = sum(d['weight'] for d in tier['drops'])
            applies_to = (f'After {index} total pushes' + (' or more (capped)' if index == len(groups['loot_tiers']) - 1 else '') + '; one loot roll per cleared group.') if groups.get('loot_policy') == 'push-rewards-v1' else None
            entry['journey']['loot_tables'].append({'name': tier['name'], 'drop_chance_percent': chance,
                'orb_essence_chance_percent': round(chance * sum(d['weight'] for d in tier['drops'] if category(d) in ('orb', 'essence')) / total, 6),
                'applies_to': ('First cleared group only; the run’s only weapon opportunity.' if index == 0 else 'Every subsequent cleared group; no further weapon rolls.')
                    if groups.get('loot_policy') == 'shared-rare-weapons-v1' else applies_to or 'Group-depth tier; the last tier repeats.',
                'category_chances_percent': {kind: round(chance * sum(d['weight'] for d in tier['drops'] if category(d) == kind) / total, 6)
                    for kind in ('weapon', 'consumable', 'orb', 'essence')},
                'no_drop_chance_percent': 100 - chance, 'items': [
                    {'name': d['name'], 'category': category(d), 'consumable_slug': d.get('consumable_slug'), 'quantity': d.get('quantity', 1), 'weapon_type': d.get('weapon_type_slug'), 'base_damage': d.get('base_damage'),
                     'tags': list(types[d.get('weapon_type_slug')].tags) if d.get('weapon_type_slug') in types else [],
                     'chance_percent': round(chance * d['weight'] / total, 6)} for d in tier['drops']]})
    return data
