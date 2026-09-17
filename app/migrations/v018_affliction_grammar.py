from sqlalchemy import inspect, select, text, literal, true


def upgrade(conn):
    from app.models import Ability, StatusEffect, Adventurer, AdventurerAbility
    from app.status_content import GRAMMAR_EFFECTS, GRAMMAR_ABILITIES
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as postgres_insert
    insert = sqlite_insert if conn.dialect.name == 'sqlite' else postgres_insert
    for table, column, default in [('status_effects', 'rules', '{}'), ('abilities', 'affliction_ops', '[]')]:
        if column not in {c['name'] for c in inspect(conn).get_columns(table)}:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} JSON NOT NULL DEFAULT '{default}'"))
    for item in GRAMMAR_EFFECTS:
        conn.execute(insert(StatusEffect).values(**item).on_conflict_do_nothing(index_elements=['slug']))


def seed_grammar(engine):
    with engine.begin() as conn:
        seed_abilities(conn)


def seed_abilities(conn):
    from app.models import Ability, Adventurer, AdventurerAbility
    from app.status_content import GRAMMAR_ABILITIES
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as postgres_insert
    insert = sqlite_insert if conn.dialect.name == 'sqlite' else postgres_insert
    for item in GRAMMAR_ABILITIES:
        ability_id = conn.scalar(insert(Ability).values(**item, starter=True, effect_type='affliction',
             ability_type='affliction', power=0, cost_type='None', loadout_order=40,
             cooldown_type='turn', cooldown_value=2).on_conflict_do_nothing(index_elements=['slug']).returning(Ability.id))
        if ability_id:
            conn.execute(insert(AdventurerAbility).from_select(
                ['adventurer_id', 'ability_id'], select(Adventurer.id, literal(ability_id)).where(true())
            ).on_conflict_do_nothing(index_elements=['adventurer_id', 'ability_id']))
