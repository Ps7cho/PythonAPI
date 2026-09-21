"""Ordered, transactional migrations for SQLite tests and PostgreSQL deployments."""
from sqlalchemy import text
from app.migrations.v021_orb_essence_pool import upgrade as orb_essence_pool_upgrade
from app.migrations.v020_push_loot import upgrade as push_loot_upgrade
from app.migrations.v019_shared_loot import upgrade as shared_loot_upgrade
from app.migrations.v017_recovery_contracts import upgrade as contracts_upgrade
from app.migrations.v018_affliction_grammar import upgrade as affliction_upgrade
from app.migrations.v018_contract_party_stacking import upgrade as contract_party_stacking_upgrade
from app.migrations.v013_orbs import upgrade as orb_upgrade
from app.migrations.v014_statistics import upgrade as statistics_upgrade
from app.migrations.v015_fresh_statistics import upgrade as fresh_statistics_upgrade
from app.migrations.v016_party_ready import upgrade as party_ready_upgrade
from app.migrations.v012_essences import upgrade as essence_upgrade
from app.migrations.v011_consumables import upgrade as consumables_upgrade
from app.migrations.v010_status_effects import upgrade as status_upgrade
from app.migrations.v009_enemy_ecology import upgrade as ecology_upgrade
from app.migrations.v008_loot_chances import upgrade as loot_upgrade
from app.migrations.v007_raids_recovery import upgrade as raids_upgrade
from app.migrations.v006_encounter_groups import upgrade as groups_upgrade
from app.migrations.v005_push_xp import upgrade as push_xp_upgrade
from app.migrations.v004_progression import upgrade as progression_upgrade

from app.migrations.v001_targeting_cooldowns import upgrade
from app.migrations.v002_journeys import upgrade as journeys_upgrade
from app.migrations.v003_epic_choices import upgrade as epic_upgrade


def migrate(engine):
    with engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text("SELECT pg_advisory_xact_lock(74829014)"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR PRIMARY KEY)"))
        for version, migration in [("001_targeting_cooldowns", upgrade), ("002_journeys", journeys_upgrade), ("003_epic_choices", epic_upgrade), ("004_progression", progression_upgrade), ("005_push_xp", push_xp_upgrade), ("006_encounter_groups", groups_upgrade), ("007_raids_recovery", raids_upgrade), ("008_loot_chances", loot_upgrade), ("009_enemy_ecology", ecology_upgrade), ("010_status_effects", status_upgrade), ("011_consumables", consumables_upgrade), ("012_essences", essence_upgrade), ("013_orbs", orb_upgrade), ("014_statistics", statistics_upgrade), ("015_fresh_statistics", fresh_statistics_upgrade), ("016_party_ready", party_ready_upgrade)]:
            if conn.execute(text("SELECT version FROM schema_migrations WHERE version=:version"),
                            {"version": version}).first():
                continue
            migration(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES (:version)"), {"version": version})
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='017_recovery_contracts'")).first():
            contracts_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('017_recovery_contracts')"))
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='018_affliction_grammar'")).first():
            affliction_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('018_affliction_grammar')"))
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='018_contract_party_stacking'")).first():
            contract_party_stacking_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('018_contract_party_stacking')"))
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='019_shared_loot'")).first():
            shared_loot_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('019_shared_loot')"))
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='020_push_loot'")).first():
            push_loot_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('020_push_loot')"))
        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='021_orb_essence_pool'")).first():
            orb_essence_pool_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('021_orb_essence_pool')"))

        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='022_auction_house'")).first():
            from app.migrations.v022_auction_house import upgrade as auction_upgrade
            auction_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('022_auction_house')"))

        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='023_equipment'")).first():
            from app.migrations.v023_equipment import upgrade as gear_upgrade
            gear_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('023_equipment')"))

        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='024_developer_accounts'")).first():
            from app.migrations.v024_developer_accounts import upgrade as developer_account_upgrade
            developer_account_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('024_developer_accounts')"))

        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='025_world_shops'")).first():
            from app.migrations.v025_world_shops import upgrade as world_shops_upgrade
            world_shops_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('025_world_shops')"))

        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='026_guard_percentage'")).first():
            from app.migrations.v026_guard_percentage import upgrade as guard_percentage_upgrade
            guard_percentage_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('026_guard_percentage')"))

        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='027_ability_design'")).first():
            from app.migrations.v027_ability_design import upgrade as ability_design_upgrade
            ability_design_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('027_ability_design')"))

        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='028_remove_legacy_states'")).first():
            from app.migrations.v028_remove_legacy_states import upgrade as remove_legacy_states_upgrade
            remove_legacy_states_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('028_remove_legacy_states')"))

        if not conn.execute(text("SELECT version FROM schema_migrations WHERE version='029_discord_auth'")).first():
            from app.migrations.v029_discord_auth import upgrade as discord_auth_upgrade
            discord_auth_upgrade(conn)
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES ('029_discord_auth')"))
