"""Remove former affliction application abilities after weapon conversion."""
from copy import deepcopy
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session


def upgrade(conn):
    from app.models import (Ability, AbilityArchetype, AdventurerAbility, EquippedAbility,
                            EnemyAbility, OrbOutcome, WeaponEffect)
    with Session(bind=conn) as db:
        sources = {slug.removeprefix('application-') for slug in db.scalars(select(WeaponEffect.slug)) if slug.startswith('application-')}
        removed = list(db.scalars(select(Ability).where(Ability.slug.in_(sources))))
        ids = [a.id for a in removed]
        slugs = {a.slug for a in removed}
        if ids:
            assignments = list(db.scalars(select(EnemyAbility).where(EnemyAbility.ability_id.in_(ids))))
            if assignments:
                fallback = db.scalar(select(Ability).where(Ability.slug == 'weapon_strike'))
                if fallback is None:
                    fallback = Ability(slug='weapon_strike', name='Weapon Strike', description='Attack with the equipped weapon and its effects.',
                                       requires_weapon=True, damage_multiplier=1, power=0, cost_type='None', allowed_weapon_tags=[])
                    db.add(fallback); db.flush()
                for assignment in assignments:
                    existing = db.get(EnemyAbility, (assignment.enemy_slug, fallback.id))
                    if existing is None:
                        db.add(EnemyAbility(enemy_slug=assignment.enemy_slug, ability_id=fallback.id,
                                            weight=assignment.weight, priority=assignment.priority))
                        db.flush()
            for model in (EnemyAbility, EquippedAbility, AdventurerAbility, OrbOutcome):
                db.execute(delete(model).where(model.ability_id.in_(ids)))
            # Drop modifiers aimed at a removed skill rather than silently broadening them.
            for model in (Ability, WeaponEffect):
                for row in db.scalars(select(model)):
                    chain = [s for s in row.effect_chain if s.get('ability_slug') not in slugs]
                    if len(chain) != len(row.effect_chain):
                        row.effect_chain = chain
                        if isinstance(row, Ability):
                            valid = {s['id'] for s in chain}
                            row.rank_upgrades = {rank: {k:v for k,v in values.items() if ':' not in k or k.split(':',1)[1] in valid}
                                                 for rank,values in row.rank_upgrades.items()}
            db.execute(delete(Ability).where(Ability.id.in_(ids)))
        for archetype in list(db.scalars(select(AbilityArchetype))):
            if 'archetype-' + archetype.slug in sources:
                for ability in db.scalars(select(Ability).where(Ability.archetype_slug == archetype.slug)):
                    ability.archetype_slug = None
                db.flush(); db.delete(archetype)
            else:
                values = deepcopy(archetype.definition)
                chain = values.get('effect_chain', [])
                values['effect_chain'] = [s for s in chain if s.get('ability_slug') not in slugs]
                if values['effect_chain'] != chain:
                    valid = {s['id'] for s in values['effect_chain']}
                    values['rank_upgrades'] = {rank: {k:v for k,v in dials.items() if ':' not in k or k.split(':',1)[1] in valid}
                                              for rank,dials in values.get('rank_upgrades', {}).items()}
                    archetype.definition = values
        db.flush()


def seed_examples(conn):
    from app.models import Ability, AbilityArchetype, Adventurer, AdventurerAbility
    with Session(bind=conn) as db:
        definitions = [
            dict(slug='double_strike', name='Double Strike', description='Strike twice with your weapon.',
                 effect_type='damage', target_type='enemy', requires_weapon=True, damage_multiplier=0.75, strike_count=2,
                 cooldown_value=2),
            dict(slug='riposte', name='Riposte', description='While equipped: 35% chance to retaliate against an attacker with a weapon strike. Cannot trigger other reactions.',
                 effect_type='damage', target_type='enemy', trigger_mode='on_hit', requires_weapon=True,
                 damage_multiplier=0.75, proc_chance_percent=35, cooldown_value=1),
            dict(slug='recovery_reflex', name='Recovery Reflex', description='While equipped: 25% chance when hit to recover 10% maximum HP.',
                 effect_type='heal', target_type='self', trigger_mode='on_hit', power=10,
                 proc_chance_percent=25, cooldown_value=2),
        ]
        for values in definitions:
            ability = db.scalar(select(Ability).where(Ability.slug == values['slug']))
            if ability is None:
                ability = Ability(**values, starter=True, loadout_order=30, cost_type='None', allowed_weapon_tags=[])
                db.add(ability); db.flush()
                for hero_id in db.scalars(select(Adventurer.id)):
                    db.add(AdventurerAbility(adventurer_id=hero_id, ability_id=ability.id))
        db.flush()


def migrate_seeded_content(engine):
    with engine.begin() as conn:
        if conn.dialect.name == 'postgresql':
            conn.execute(text('SELECT pg_advisory_xact_lock(310032)'))
        # Legacy bootstrap may recreate a seed; always normalize before serving.
        upgrade(conn)
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='034_remove_application_abilities'")).first():
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('034_remove_application_abilities')"))
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='035_combat_options'")).first():
            seed_examples(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('035_combat_options')"))
