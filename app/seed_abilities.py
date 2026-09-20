from sqlalchemy import select

from app.database import SessionLocal
from app.models import Ability, Enemy, EnemyAbility

STARTER_ABILITIES = [
    {"name": "Guard", "description": "Reduce incoming direct damage by 60% this round.",
     "ability_type": "defense", "cooldown_type": "turn", "cooldown_value": 0,
     "cost_type": "None", "cost_value": 0, "target_type": "self", "effect_type": "guard", "power": 60},
    {
        "name": "Strike",
        "description": "A straightforward melee attack that deals reliable damage.",
        "ability_type": "attack",
        "cooldown_type": "turn",
        "cooldown_value": 0,
        "cost_type": "Stamina",
        "cost_value": 2,
        "target_type": "enemy",
        "effect_type": "damage",
        "power": 12,
    },
    {
        "name": "Power Strike",
        "description": "A heavier blow that sacrifices speed for greater impact.",
        "ability_type": "attack",
        "cooldown_type": "turn",
        "cooldown_value": 1,
        "cost_type": "Rage",
        "cost_value": 4,
        "target_type": "enemy",
        "effect_type": "damage",
        "power": 22,
    },
    {
        "name": "Second Wind",
        "description": "Heal 30% maximum HP.",
        "ability_type": "support",
        "cooldown_type": "minutes",
        "cooldown_value": 10,
        "cost_type": "Spirit",
        "cost_value": 3,
        "target_type": "self",
        "effect_type": "heal",
        "power": 30,
    },
    {
        "name": "Battle Cry",
        "description": "For 3 turns, party gains +20% attack.",
        "ability_type": "buff",
        "cooldown_type": "hours",
        "cooldown_value": 4,
        "cost_type": "Focus",
        "cost_value": 5,
        "target_type": "party",
        "effect_type": "buff",
        "power": 20,
    },
    {
        "name": "Heroic Stand",
        "description": "For 3 turns, cannot fall below 1 HP.",
        "ability_type": "defense",
        "cooldown_type": "hours",
        "cooldown_value": 24,
        "cost_type": "Mana",
        "cost_value": 6,
        "target_type": "self",
        "effect_type": "shield",
        "power": 100,
    },
]


def seed_abilities() -> None:
    db = SessionLocal()
    try:
        slugs = {"Strike": "attack", "Power Strike": "power_strike", "Guard": "guard",
                 "Second Wind": "second_wind", "Battle Cry": "battle_cry", "Heroic Stand": "heroic_stand"}
        for item in STARTER_ABILITIES:
            item = {**item, "slug": slugs[item["name"]], "starter": True,
                    "requires_weapon": item["effect_type"] == "damage",
                    "max_targets": None if item["target_type"] == "party" else 1,
                    "allowed_weapon_tags": (["melee", "ranged"] if slugs[item["name"]] == "attack" else ["melee"]) if item["effect_type"] == "damage" else [],
                    "loadout_order": {"attack": 0, "power_strike": 1, "guard": 2}.get(slugs[item["name"]])}
            exists = db.execute(select(Ability).where(Ability.slug == item["slug"])).scalar_one_or_none()
            if exists is None:
                db.add(Ability(**item, damage_multiplier=item["power"] / 10 if item["effect_type"] == "damage" else None))
        db.commit()
    finally:
        db.close()

    # The database is the source of truth for the catalog. If a record already
    # exists, leave it alone so the app reads the current names/descriptions that
    # were stored in the database instead of overwriting them with startup data.


def seed_enemy_abilities() -> None:
    """Bootstrap enemy content; never overwrite existing assignments or balance."""
    definitions = [("goblin", "dirty_stab", "Dirty Stab"),
                   ("goblin-archer", "aimed_shot", "Aimed Shot"),
                   ("wolf", "bite", "Bite"), ("hobgoblin", "cleave", "Cleave"),
                   ("roadside-bandit", "enemy_strike", "Strike"),
                   ("raid-packlord", "bite", "Bite"), ("raid-warden", "cleave", "Cleave"),
                   ("raid-sovereign", "cleave", "Cleave")]
    with SessionLocal.begin() as db:
        for enemy_slug, slug, name in definitions:
            ability = db.scalar(select(Ability).where(Ability.slug == slug))
            if ability is None:
                # Names remain unique in the existing schema.
                ability = Ability(slug=slug, name="Enemy Strike" if slug == "enemy_strike" else name,
                                  damage_multiplier=1.0, power=0, cost_type="None", max_targets=None,
                                  requires_weapon=slug != "bite",
                                  allowed_weapon_tags=[] if slug == "bite" else ["ranged" if slug == "aimed_shot" else "melee"])
                db.add(ability)
                db.flush()
            if db.get(Enemy, enemy_slug) and not db.scalar(select(EnemyAbility).where(EnemyAbility.enemy_slug == enemy_slug)):
                db.add(EnemyAbility(enemy_slug=enemy_slug, ability_id=ability.id))


    # Catalog-driven expanded enemy roles; conflict-safe seeding preserves edits.
    from app.enemy_content import ABILITIES, ENEMY_SPECS
    from app.status_content import ASSIGNMENTS
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as postgres_insert
    with SessionLocal.begin() as db:
        insert = sqlite_insert if db.bind.dialect.name == 'sqlite' else postgres_insert
        for spec in ABILITIES:
            db.execute(insert(Ability).values(**spec, status_effect_slug=ASSIGNMENTS.get(spec["slug"])).on_conflict_do_nothing(index_elements=['slug']))
        for enemy_slug, slug, name, cap, multiplier in [
            ('goblin-archer', 'scatter_volley', 'Scatter Volley', 3, 0.75),
            ('hobgoblin', 'sweeping_crush', 'Sweeping Crush', 2, 1.2),
            ('raid-sovereign', 'ash_wave', 'Ash Wave', None, 0.8),
        ]:
            db.execute(insert(Ability).values(
                slug=slug, name=name, description='Strike multiple adventurers with a telegraphed attack.',
                effect_type='damage', target_type='enemy', max_targets=cap,
                damage_multiplier=multiplier, power=0, cost_type='None',
                cooldown_type='turn', cooldown_value=3,
            ).on_conflict_do_nothing(index_elements=['slug']))
            ability_id = db.scalar(select(Ability.id).where(Ability.slug == slug))
            if db.get(Enemy, enemy_slug):
                db.execute(insert(EnemyAbility).values(enemy_slug=enemy_slug, ability_id=ability_id,
                    weight=1, priority=3).on_conflict_do_nothing(index_elements=['enemy_slug', 'ability_id']))
        catalog = {a.slug:a for a in db.scalars(select(Ability).where(Ability.slug.in_([a['slug'] for a in ABILITIES])))}
        for slug, _, _, _, _, _, _, _, assignments in ENEMY_SPECS:
            for name in assignments:
                ability = catalog[name]
                db.execute(insert(EnemyAbility).values(enemy_slug=slug, ability_id=ability.id,
                    weight=1, priority=2 if ability.effect_type in ('heal','guard') else 1)
                    .on_conflict_do_nothing(index_elements=['enemy_slug','ability_id']))


def seed_status_abilities():
    """Grant new catalog additions once; preserve player loadouts and later edits."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as postgres_insert
    from app.models import Adventurer, AdventurerAbility
    definitions = [
        ('rending_strike', 'Rending Strike', 'bleed', ['melee']),
        ('venom_strike', 'Venom Strike', 'poison', ['melee', 'ranged']),
        ('brand_of_sin', 'Brand of Sin', 'sin', []),
        ('necrotic_touch', 'Necrotic Touch', 'necrosis', []),
    ]
    with SessionLocal.begin() as db:
        insert = sqlite_insert if db.bind.dialect.name == 'sqlite' else postgres_insert
        hero_ids = list(db.scalars(select(Adventurer.id)))
        for slug, name, effect, tags in definitions:
            ability_id = db.scalar(insert(Ability).values(
                slug=slug, name=name, description=f'A light hit that applies {effect.title()}.',
                status_effect_slug=effect, starter=True, loadout_order=20,
                effect_type='damage', target_type='enemy', max_targets=1,
                power=4, damage_multiplier=0.4, requires_weapon=bool(tags),
                allowed_weapon_tags=tags, cost_type='None', cooldown_value=0
            ).on_conflict_do_nothing(index_elements=['slug']).returning(Ability.id))
            if ability_id is not None:
                # This transaction owns the new definition and its one-time unlocks.
                if hero_ids:
                    db.execute(insert(AdventurerAbility).on_conflict_do_nothing(
                        index_elements=['adventurer_id', 'ability_id']),
                        [dict(adventurer_id=hero_id, ability_id=ability_id) for hero_id in hero_ids])
