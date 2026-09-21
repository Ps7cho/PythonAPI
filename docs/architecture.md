# Architecture

This document describes the current architecture, not a feature roadmap or change log.
Read it before architectural changes; update it only when the architecture changes.

## Authoritative Server Rules

FastAPI owns action legality, combat execution, enemy decisions, and persistent
state changes. Clients display server state and submit commands; they never
calculate authoritative results. Content belongs in the database where reasonable;
the rules interpreting that content stay in reusable server code.

## Database Model

PostgreSQL/Neon persists SQLAlchemy models in `app/models.py`; tests use SQLite.
Catalogs define abilities, weapon types, enemies, quest templates, and loot types.
Characters own learned/equipped abilities, weapon instances, inventory, and cooldowns.
Parties and memberships connect characters to quest runs and encounters. Game events
record combat and quest outcomes alongside encounter and quest-run records.

Schema changes use ordered migrations in `app/migrations`, tracked in
`schema_migrations` and serialized with a PostgreSQL advisory lock. Startup currently
performs the legacy schema bootstrap, pending migrations, and catalog seeding.
Seeds supply initial content while preserving existing definitions.

## Progression

XP is lifetime experience. `app/progression.py` computes each level cost as
ceil(100 * level^1.5), awards three attribute points per level crossed, and derives
rank from database definitions at levels 1, 10, 20, 30, 40, 50, 60, and 70.
Rank capacities control loadout validation. Rank rewards grant owned weapon instances;
Departure checks every party member against the ordered rank catalog. One rank below
the requirement is permitted only with explicit `accept_rank_risk` acknowledgement;
more than one rank below is rejected. Accepted warnings and affected members are
saved in the run snapshot. Equipment rank checks remain strict.
XP, levels, points, rank equipment rewards, and combat messages commit with victory.
Attribute allocation locks the character and spends saved points outside adventures.
`app/attributes.py` derives player combat stats: Might/Precision scale weapon hits,
Affinity/Willpower scale nonweapon hits and ability healing, Vitality/Willpower
increase max HP, Defense/Agility reduce direct damage (60% cap), Awareness/Speed
increase Guard, and Luck/Vitality resist DOT damage (50% cap). Exact per-point
formulas are exposed in character-sheet descriptions. All ten attributes contribute.
Stats snapshot at departure and carry through the run. Legacy snapshots without
derived stats keep their old balance; enemy rolled stats remain unchanged.
Creation starts at derived max HP; allocation increases capacity without healing.
Village/camp healing uses the applicable max HP. Consumables keep catalog potency.
Direct mitigation runs before Guard's 60% reduction in the shared resolver. DOT wards combine
multiplicatively with entity resistances; immunity remains absolute. Passive slot capacities are recorded, but
passive execution and advanced party roles remain planned, explicitly labeled in UI.

## Combat Resolution

`app/encounter_service.py` locks the encounter, checks the expected turn and actor,
and resolves any learned ability against legal living targets. The existing action
request accepts an optional owned weapon ID; switching weapons costs no extra action.
`app/combat.py` uses a shared target selector and atomic cast executor for both teams.
Effects resolve on copies; a failed cast changes no combatants. A successful cast
charges one cooldown and returns one result per target.

Direct damage rolls critical chance (0.4% Precision + 0.2% Luck, capped at 40%)
and flinch chance (0.2% Might + 0.2% Speed, capped at 20%) from saved derived stats.
Critical damage is 1.5x before mitigation. Flinch skips one action, cannot stack,
and grants immunity through the following round after the skip. DOT and support
effects cannot trigger either. Enemy snapshots without these ratings have zero chance.
The shared atomic cast publishes proc flags with damage; encounter actions persist
skips and events under the same lock. Combat RNG uses encounter/turn/actor position;
raids substitute the saved rotation seed and stage, independent of loot and AI RNG.
Old derived-stat snapshots default missing chances to zero. Flinch state clears
when moving to another encounter.

Status-effect catalogs define tick damage, duration, and stack caps. Abilities link
one optional status; entity-type profiles map status slugs to percentage resistance
(100 means immunity). Definitions and profiles are eagerly loaded and snapshotted.
Players use the humanoid profile. Reapplication adds a stack up to the cap and
refreshes the shared duration; a cap of one gives refresh-only behavior.

After all living players act, the server resolves the enemy phase and then ticks
statuses once on every living combatant. New applications tick that round; durations
count ticks, not wall time. Direct hits and ticks share the HP/shield resolver;
ticks bypass Guard. A simultaneous wipe is defeat, and status kills use normal
reward/death resolution. A direct victory ends the fight without another tick.
Statuses persist on characters and between encounters, never tick outside combat,
and clear on camp/village rest or rescue. Encounter locks and expected-turn checks
protect ticking with the same transaction as actions. Old snapshots default to no
statuses or resistances until a new encounter snapshots current content.

Encounter state,
character health/rewards/cooldowns, and events commit in the same transaction.

## Ability System

Database definitions supply stable slugs, display text, effects, power/multipliers,
weapon requirements, targeting limits, and turn or timed cooldown settings.
Learned relationships determine combat skills; equipped slots remain saved favorites. Enemy assignments
provide priorities and selection weights. `app/loadouts.py` translates definitions
into executable encounter snapshots.

Worldsmith persists reusable `ability_archetypes` as starting definitions. Applying
an archetype copies its values into an independent Ability; later template edits
do not rewrite variants. Abilities store duration, Guard percentage, ordered
`effect_chain` steps, and sparse `rank_upgrades` keyed by rank slug. Upgrades are
absolute dial overrides accumulated through the character's rank ladder and
resolved at departure; active adventure snapshots retain their saved values.

After the primary effect and affliction operations, bounded follow-up steps can
deal damage, restore HP, grant a named encounter resource, or modify ability
values. Amounts use a fixed value or a percentage of actual primary damage/results
or the previous step's result. Triggers test the primary hit, damage, or kill.
Recipients are self, selected targets, living party, or opponents; split amounts
divide a single integer budget, with remainder assigned in roster order. Overheal
is discarded, overkill cannot create extra resources, and dead targets are skipped.
Follow-up damage goes through the same damage resolver, without reapplying damage
bonuses or critical multipliers to already-derived amounts. Named resources are
encounter pools capped at one million; this does not add resource spending rules.

Temporary `ability_modifiers` change primary dials or individual follow-up values
for subsequent casts, optionally restricted to a stable ability slug. Flat changes
apply before summed percentage changes and validated bounds. Reapplying the same
caster/ability/step refreshes it; distinct sources combine. Expiry is an exclusive
round deadline; transitions and rest clear modifiers. The server exposes effective
ability values and previews enemy actions using the same modified executor. All
recipients are part of the atomic cast copy, and only the primary cast charges a
cooldown. Definitions accept validated data, never executable expressions or loops.

Enemy catalogs include undead, cultists, vermin, amphibians, giants, orcs, eldritch
creatures, possessed, dragons, plants, beasts, and humanoid roles. Their assigned
abilities reuse the shared resolver; healing is skipped when no legal target needs it.
Throwing axes use database `ranged`/`thrown` tags.

Supported effects are damage, guard, heal, buff, shield, cleanse, and evade. `max_targets` is a
positive limit or NULL for all legal targets. Encounter snapshots hold cooldown
deadlines; character cooldown state preserves remaining turns and absolute timed
deadlines across new quests. Entering another fight advances one round.

## Consumables

Database consumable definitions supply healing, attack boosts, or status cleansing.
Owned stacks use a character/item composite primary key and nonnegative quantities.
The existing encounter action endpoint accepts `consumable_slug` and optional
`target_id` (defaults to self). Use costs the actor's action and targets one living
ally in that encounter. The shared cast executor resolves healing/buffs/cleansing;
cleansing removes all DOT statuses, boosts refresh rather than stack. Full-health
heals and cleansing unaffected targets are rejected without spending stock.
Encounter, ordered character, and inventory locks protect validation, effects,
quantity decrements, and events in one transaction. Clients never supply quantities
or effect power. Inventory snapshots support display; use checks current owned stock.
Weighted loot tables support weapons or consumable stacks, with the same no-drop
roll and safe-extraction/death rules. Consumable drops become usable after claiming
on return; saved runs and raid rotations preserve their existing loot tables.

## Essences

Essences and orbs reuse owned consumable stacks and weighted loot rolls. Nine
active essences are Dark, Holy, Magic, Sin, Swift, Fire, Blood, Balance, and Might.
Retired essence records, owned items, and absorbed slots remain intact, but cannot
be newly absorbed or targeted by orbs; current catalogs and future loot exclude them.
Village absorption consumes one item and fills one of three permanent distinct slots.
Character locking and database slot/uniqueness constraints enforce this limit.

`orb_outcomes` maps an orb and essence to an existing Ability definition. Village
orb use requires an owned orb and that character's absorbed active essence. It
unlocks the mapped AdventurerAbility and consumes one orb in the same transaction
as its event. Already-learned outcomes reject without spending; current loadouts
are preserved. Recipes are deterministic and previewed before use. Evasion and
Strike Orbs each have nine named variants sharing generic combat mechanics.
Evasion grants one 60% dodge attempt against a direct attack before two rounds
expire. The attempt is consumed even on failure; successful evasion avoids direct
damage, attached DOT applications, criticals, and flinch. DOT ticks cannot be dodged.
Evasion resolves inside the shared atomic cast using the same server/raid RNG,
charges the attacker's normal cooldown, and clears on rest or encounter transition.
Strike variants use existing weapon or nonweapon damage scaling at 120% power.
All learned abilities are available in combat and obey normal cooldowns.
The original richer signature/passive designs remain planned.

Character ownership is required; village item use rejects dead/active characters.
Combat actions reject essence/orb consumption. The village endpoints share character
validation and locking; stock locks and the unique learned-ability relationship
prevent duplicate spending. New loot includes orbs and the reduced essence set;
existing runs and raid rotations retain saved loot snapshots.

## Weapon System

Weapon types carry database tag arrays. Owned weapon instances provide base damage;
equipped-weapon records select the character's default weapon. Enemy weapon assignments
pair a type with rolled encounter power. Weapon attacks require a compatible
selected weapon and use its damage multiplied by the ability coefficient.
Nonweapon attacks use their own damage or actor-power scaling.

Saved equipment and favorite slots are locked during adventures. All owned weapons
and learned skills are snapshotted for combat, carried in a pocket dimension.
Combat weapon selection has no rank or carrying limit; skill tag requirements remain.
Older encounters gain missing inventory while retaining existing saved definitions. Proficiency, sharpness, and resource costs are not yet implemented as combat rules.

## Encounter Model

Playable quest templates store journey stages, alternative enemy groups, descriptions,
reward amounts, and camp-rest references. Departure saves the chosen route and rules
in the existing quest record; custom encounters remain supported. Rest policies are
database-defined and share one server resolver for camp and village recovery.

Quest runs hold the encounter plan; encounters persist participants, turn, status,
and referenced enemy instances. Enemy stats are rolled once. Ability and weapon
snapshots keep ongoing fights stable when catalog content changes. Legacy encounters
retain compatibility paths for older snapshots.

Quest continuation carries health and cooldowns forward. Saved journey rules define
rest budgets, flat push bonuses, XP percentage tiers, and committed group boundaries.
New quests use 2-3 battles per group; epics use 3-5. Continuation can append another
group after the original route, while its base completion bounty pays only once.
Threat scales through the existing enemy roller and is capped by catalog settings.
Only cleared group boundaries permit retreat or camp. The camp budget spans the run.

`app/group_journeys.py` resolves each group's loot table once: an explicit percentage
can yield no gear, otherwise one entry is selected by weight. Deeper tiers improve
the chance and item pool for legacy tables. New catalogs combine weapon tiers into
repeatable weighted tables selected by the run's total saved push count. Initial
odds are 5% weapon, 10% shared orb/essence pool, 20% ordinary consumable, and 65%
nothing. Each push adds one percentage point to weapon/shared-pool odds, capped
at 10%/15% after five pushes; consumables stay at 20%. The shared pool uses item
weights (initially 2 per orb and 1 per active essence). Rest does not increase or
reset this loot bonus. The existing resolver still rolls once per group,
including after a previous weapon drop; weights and caps live in the saved catalog.
Results, including misses, persist in run progress; reads,
retries, and extraction never reroll. New raid loot uses independent per-run randomness
while raid enemies/AI stay seeded. Old rotations and runs retain their saved loot rules.
Catalog previews report unconditional item odds and current database weapon tags.
Weapon drops use existing type relationships; basic attack accepts melee or ranged
weapons, while Power Strike still requires melee. Weapon-type shields remain excluded from weapon drops; off-hand bucklers use
the gear catalog.
Run gains and loot are banked in quest-progress JSON. For new quests/epics, a downed
member stays at zero HP during the run. If a survivor returns, survivors claim their
banked rewards and loot, while downed members recover at 1 HP and forfeit their own
run gains. Pre-run XP, levels, attribute points, currency, and gear are never rolled
back. A full wipe is fatal. Withdrawal locks characters and settles rewards, rescue,
and run closure atomically. Legacy snapshots keep their original reward/death rules.

## Raids

Daily (10-12 encounters) and weekly (13-15 encounters) raid templates define fixed,
finite routes with exactly three boss checkpoints. Each death is permanent; survivors
cannot rescue raid casualties. Final victory settles rewards automatically. Survivors
can extract at earlier boss checkpoints. Raids do not offer endless extensions.

`app/raids.py` derives seeds from the template slug and UTC day or Monday-start week.
`raid_rotations` has a unique period key and stores the shared route, rolled enemy
stats, abilities, equipment, loot rules, and reset time. Conflict-safe insertion picks
one snapshot for concurrent departures. Each run gets unique encounter/enemy IDs and
party-scaled HP while reusing the saved rolls. Raid AI uses derived seed streams; new loot tables roll independently per run. Running raids retain their period snapshot across resets and catalog
edits. Clients submit the existing template/action/continuation commands, never seeds.

Village long rests require no active adventure and restore living characters only.
Party invite codes authorize joining a saved roster; leaders start and continue runs,
and accounts control only their own characters. Invite digests/expiry are persisted;
rosters cannot grow during active adventures.
Shared-party relationships form the player-facing friends directory. Party snapshots
include each member's player identity and whether that account currently has an
authenticated village WebSocket connection. Presence is ephemeral and process-local;
connect and disconnect events refresh the village snapshots of shared party members.

## API Conventions

Request bodies are capped before JSON parsing: 4 KiB for login, registration,
and character creation; 1 MiB for other writes. Authentication throttles run
before password hashing: five registration attempts per source per hour,
30 login attempts per source per minute, and five per source/username pair per
minute. Throttles return 429 with Retry-After; failed passwords no longer lock
the username globally. These bounded counters are process-local and reset on
restart; multi-worker deployments need shared proxy/gateway limits and trusted
proxy configuration for client addresses. Forwarded headers are not read by the
application limiter. Character creation allows ten attempts per account per
minute and defaults to 20 total characters per account (including fallen heroes),
configurable with MAX_CHARACTERS_PER_ACCOUNT. PostgreSQL locks the owning user
while checking the quota and creating the character and starter inventory in
one transaction.

The deployment folder contains the Python backend only. Frontend assets are hosted
separately; bundled page/asset routes return 404 when those files are absent.
Cross-origin clients use the configured `PUBLIC_CLIENT_ORIGINS` allowlist.

Player-facing naming uses **Quests** for the overall selection page and **Journeys**
for the short 2-3 battle category; Epics and Raids retain their names. API paths,
JSON keys, and the internal `quest` kind remain stable.

Gameplay endpoints use `/api`, JSON, and UUID resource identifiers; catalogs also
use stable slugs. Authentication accepts session cookies or bearer tokens, and the
server checks character ownership. Party members can read their shared encounters.

The settings designer exposes source records through authenticated catalog inspection.
`POST /api/catalog-editor` validates edits using gameplay schemas and references,
locks existing records, rejects stale revisions, and commits an audit GameEvent
with each change. Validation-only requests roll back. A server configuration flag
enables authoring (disabled by default) for developer accounts or explicitly
allowlisted usernames. An empty allowlist grants no access, and the disable
switch applies to developers too.
Edits change live catalog definitions; existing encounter/raid snapshots remain saved.

Combat commands include `actor_id`, `expected_turn`, and `ability_id`. Legacy
`attack`, `power_strike`, and `guard` aliases remain supported; `wait` advances a
character action without casting, allowing turn cooldowns to recover. Optional `target_id`
selects a primary target; `target_ids` selects an explicit ordered list. Commands
cannot supply authoritative damage, weapon stats, or results. Responses return
saved snapshots and action results. Stale turns and unavailable actions are rejected.


## Auction House

Fixed-price listings and timed auctions hold items outside character inventory in
`auction_listings`. Listing a weapon preserves its ID and stats in escrow JSON;
consumable lots subtract owned stock. Equipped weapons cannot be listed. Trading
requires a living character outside an active adventure. Souls are not tradable.
Gold and items transfer atomically under a listing lock and ordered character locks.
Highest bids are held in gold; outbids refund immediately. Auctions with bids cannot
be cancelled. Expiry delivers to the winner and pays the seller, or returns unsold
items. PostgreSQL workers settle every 15 seconds; market reads and actions also
settle overdue listings after downtime. Closed rows and game events retain history.


## Armor and Accessories

Database gear definitions provide a slot, rank, price, and attribute bonuses.
Owned gear instances and equipped-slot rows cover Head, Chest, Hands, Legs, Feet,
Off Hand, Amulet, and Ring. One item fits each slot; weapons keep their existing
Main Hand system. Gear purchases use the shop and unequipped gear can be auctioned.
Equipping locks the character, requires ownership and rank, and is blocked during
adventures. Base attributes remain permanent; equipped bonuses feed the existing
stat formulas and snapshot at departure. Gear changes never heal; reducing maximum
HP clamps current HP. Village rest uses the equipped maximum. Gear is currently
acquired from the shop or other players, not quest loot tables.
