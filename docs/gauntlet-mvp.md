# Gauntlet MVP implementation and verification

## Behavior

One path supports solo and multi-adventurer trials through the existing combat
engine. Players choose owned, living, available adventurers, resolve each battle,
advance after victory, and finish on defeat or retire after a cleared stage.
Runs preserve original character state and grant no rewards or progression.
Encounter health, cooldowns and statuses carry forward without rests. Consumables
are disabled. The existing six-member maximum applies, with a configurable lower
limit per definition.

Reached and completed depths are separate. History stores initial party/loadout
and enemy/rule snapshots, duration, termination reason, and links to existing
combat logs. Party identity uses sorted adventurer IDs. History aggregates overall
and composition bests; one-member compositions provide individual solo bests.

## Files

Backend paths are relative to the PythonAPI repository:

- `app/gauntlets.py`: validation, deterministic stage generation, orchestration,
  results, history and summary APIs.
- `app/models.py`: GauntletDefinition and GauntletRun; QuestRun relationship.
- `app/migrations/v030_gauntlets.py`, `app/migrations/__init__.py`: migration 030.
- `app/encounter_service.py`: reuse combat/continuation while keeping trial state
  separate from persistent character settlement.
- `app/main.py`: register the router.
- `app/catalog_editor.py`: reviewed, developer-only definition editing.
- `tests/test_gauntlets.py`: ten focused tests.
- `docs/architecture.md`: Gauntlet architecture notes.

Frontend paths are relative to FrontEndJS/AdventuringPortal:

- `gauntlet.js`: selection, deployment, summaries, history, party reuse.
- `village.js`: entry tab, shared battle controls, summary/retirement navigation.
- `debug.js`: Worldsmith Gauntlets entry; retains Manage data scroll preservation.
- `index.html`: script loading and cache versions.
- `LOCAL-SETUP.md`: deployment order and player workflow.

Workspace diagnostics outside those two repositories:

- `verify_gauntlet_integration.py`: browser + real FastAPI integration test.
- `verify_worldsmith_integration.py`: extended to cover Gauntlet definitions.

## Database and API

Migration `030_gauntlets` adds `gauntlet_definitions` and `gauntlet_runs`, then
idempotently seeds Endless Road. Normal backend startup runs migrations. Existing
QuestRun/Encounter/Event tables retain combat state and logs.

New authenticated endpoints:

- `GET /api/gauntlets`
- `POST /api/gauntlet-runs`
- `GET /api/gauntlet-runs` (limit/offset, personal and composition bests)
- `GET /api/gauntlet-runs/{run_id}`

Combat actions and continuation use existing encounter endpoints. Shared commands
continue ignoring unknown fields for backward compatibility; submitted damage,
victory and depth fields cannot influence authoritative results. New start/config
payloads reject unknown fields. Reads enforce run ownership; character ownership
and availability are checked before departure.

## Verification

- Gauntlet suite: **10 passed**. Covers solo/team runs, unauthorized control and
  visibility, advance/failure/retirement, counters, composition identity, stale
  continuation, tampering, nonzero starting health/cooldown preservation, deterministic
  balance, Worldsmith validation/config snapshots, and migration idempotence.
- Focused Gauntlet/attributes/encounter-groups/catalog-editor run: **46 passed**
  before adding the final two Gauntlet tests; all are included in the full run.
- Final full backend suite: **270 passed, 5 failed**.
- Unchanged commit `6c29b8b`, extracted into an isolated temporary directory:
  **260 passed, the same 5 failed**. No existing tests were removed or weakened.
- Browser Gauntlet test: **passed**, 26 real API commands, no page errors. Tested
  selecting/removing members, solo victory/advance/defeat, summary, reload/history,
  unchanged character state, party reuse, two-member victory and retirement.
- Browser Worldsmith test: **passed** duplicate/review/save/delete for abilities,
  ability archetypes and Gauntlet definitions; saved/removal state verified by API.
- JavaScript syntax and git whitespace checks: **passed**.

Baseline failures reproduced unchanged:

- `test_contracts.py::test_rescue_attempt_fallen_stack_on_original_contract`
- `test_enemies.py::test_catalog_and_referenced_rolls`
- `test_multitarget_cooldowns.py::test_versioned_migration_preserves_existing_targeting_and_cooldowns`
- `test_rank_risk_enemies.py::test_all_new_enemies_have_usable_abilities_and_live_in_route_pools`
- `test_statistics.py::test_account_statistics_are_shared_across_adventurers`

Tests use isolated SQLite. Browser requests are routed into the actual FastAPI
application, not fabricated API responses. This does not verify deployed Render,
Cloudflare caching, PostgreSQL runtime behavior, or production websocket delivery.
No live Neon data was changed and nothing was deployed.

## Limitations and next step

The default pool is one bandit type with linear growth. There are no rewards,
leaderboards, seasons, bosses, automated play, or new analytics. History UI shows
the latest 50 runs; the API is paginated. Players can retire between encounters,
not mid-battle. Character ownership is required for every selected party member.

Next: apply migration 030 to an isolated PostgreSQL/Neon staging branch, deploy
the backend before the matching frontend, and smoke-test the same browser loop
against that deployment before publishing to production. The five baseline test
failures remain a separate follow-up.
