# Game Event API

## Backend-only deployment folder

This folder is the Git repository to publish for the Python API. It includes
server code, migrations, backend tests, and development documentation. JavaScript,
HTML, CSS, and browser verification scripts remain outside this folder. The UI
sections below describe the separately hosted client; bundled frontend URLs return
404 in this backend-only package. API documentation is available at `/docs`.

Run all installation, server, and test commands from this folder. For local tests,
install `requirements-dev.txt` and run `python -m pytest`. The existing virtual
environment remains one directory above and can also be used with
`..\.venv\Scripts\python -m pytest`.

### Render settings

- Language: Python 3.
- Root Directory: leave blank when publishing this folder as the repository root.
- Build Command: `pip install -r requirements.txt`.
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
- Health Check Path: `/health`.
- Environment: set `DATABASE_URL` to your Neon connection string with SSL enabled.
  Set `PUBLIC_CLIENT_ORIGINS` to a JSON array of your frontend origins, for example
  `["https://your-frontend.example"]`.

`.env.example` contains placeholders. Create a local `.env` when needed; on Render,
configure environment variables in the service dashboard. Startup applies existing
migrations and seeds to the configured database.

See [Render's FastAPI guide](https://render.com/docs/deploy-fastapi).

## Afflictions

See [AFFLICTIONS.md](AFFLICTIONS.md) for the shared ten-operation grammar,
catalog endpoints, example abilities, stack rules and combat-only resources.

## Public JavaScript Client

The default village view is a deployment lobby: choose an adventurer, encounter
length and opponent directly in Play. Desktop uses side navigation; mobile puts
deployment before the portrait. Current squads are reached through Party, create,
join or an explicit party link. Historical solo parties are no longer listed or
automatically selected. `app/lobby.js` and the standalone `lobby.js` share the menu.

`D:\Game Assets\FrontEndJS` is the separate, publishable frontend template. Its only client
configuration is `config.js` (`apiBaseUrl`, currently `http://127.0.0.1:8001/api`).
Serve that folder alone on port 5173; see `D:\Game Assets\FrontEndJS\README.md` for usage and
request examples. No backend configuration or bundled credentials are included.

For separately hosted sites, set server-side `PUBLIC_CLIENT_ORIGINS` to a JSON
array of exact allowed origins. Local defaults are `http://127.0.0.1:5173` and
`http://localhost:5173`. The public client uses user-specific bearer sessions;
existing same-origin browser sessions remain supported. Cross-origin cookie
mutations are still rejected. `verify_public_client.py` checks the separate-site
browser flow against the local API and creates a test account.

A FastAPI application that keeps all game rules and state mutation on the server. The client can submit events, while the server enforces state transitions and stores versioned snapshots in PostgreSQL.

## Turn-Based Encounters

### Soul Recovery Bulletin

New full-party defeats post a recovery contract under Expeditions. Contracts save
the fallen names, levels, ranks, known abilities and equipped flags at defeat,
plus the saved quest route. Existing historical defeats are not backfilled.
The recovery route is finite, including for formerly endless expeditions.

Authenticated clients use `GET /api/contracts` (latest 100 open/in-progress),
`GET /api/contracts/{id}`, and `POST /api/contracts/{id}/accept` with
`adventurer_ids` and optional `accept_rank_risk`. Group leaders instead select
`contract_id` through the normal party selection/ready/departure flow.
Claims serialize in PostgreSQL; one attempt may hold a contract at a time.
Defeat or early return reopens it. Completed contracts cannot be claimed again.

Full-route victory awards one unique soul per fallen adventurer, round-robin by
surviving adventurer ID. Souls appear in character inventory with `type: soul`,
`loot_type: souls`, and the frozen `fallen` profile. Souls have no use/consume
action yet. Migration 017 adds contract and soul tables without changing existing
inventory. Bulletin changes broadcast to connected village clients after commit;
there is no bulletin polling timer.

Guard reduces every direct hit by 60% for the current round. The reduction is not
consumed by hits; damage-over-time bypasses it. Enemy projections use the same rule.
`combat_log` in encounter snapshots contains saved turn-numbered messages,
including guard use, reduced damage and status ticks, shared over WebSockets.
The combat view shows that history beneath the actions, including after reconnect.

Choose **Play Solo** to stop proposing quests to a selected party; this preference
survives live village updates. **Leave Party** removes the chosen adventurer from
an idle party via `POST /api/parties/{id}/leave` with `adventurer_id`. Leadership
passes to a remaining member and readiness resets. Active adventures must finish
or return at an existing checkpoint before members can leave.

Enemy snapshots include `next_move`: the planned ability, state, and named targets
with projected per-target amounts. Plans are saved at the start of each round and
executed by the shared combat engine; reads never reroll them. Existing encounters
without a saved plan use a deterministic fallback until their next action saves it.
Damage projections simulate the enemy phase in order using current defenses and
the same random streams as execution. Guarding, evasion, healing, interruptions,
or deaths during the player phase can change the result. Amounts exclude later
damage-over-time ticks. Dead targets are skipped, never silently replaced.
Snapshots, including changed projections, use the existing WebSocket broadcasts.

New encounters can roll Scatter Volley (Goblin Archer, up to 3 targets), Sweeping
Crush (Hobgoblin, up to 2), and Ash Wave (Ash Sovereign, all living adventurers).
Each has a 3-turn cooldown. Seeding preserves existing catalog balance edits;
already-running encounters retain their original ability loadouts.

### Character Pages and Adventures

Open **Character Sheet** beside an adventurer, or `/adventurers/{id}`. The private
`GET /api/adventurers/{id}` endpoint returns stats, saved inventory, one ability list,
equipped ability IDs, and the current adventure. Equipment slots are displayed
empty and skills show an empty state: equipping items and learning skills are not
implemented yet. No gear or skills are fabricated.

Choose Single Encounter or Three-Encounter Quest on the character page. The lobby
also offers these lengths for a selected party. `POST /api/encounters` accepts
`encounter_count: 1` (default) or `3`. The selected enemy is used in each fight.
After a victory with fights remaining, `POST /api/encounters/{id}/continue` starts
the next fight. Nothing advances on its own. Health and cooldown deadlines carry
over; surviving characters earn 10 gold per victory. An unfinished quest reserves
its party between encounters. Defeat ends the quest.

Quest templates remain available through the API but are hidden from the interface.

### Accounts

Use Create Account or Log In on the development screen. Usernames are case-insensitive,
3-32 letters/numbers/underscores; passwords are 12-128 characters. Passwords use
Argon2id hashes. Neon stores accounts and hashed session tokens; sessions expire
after 24 hours and logout revokes the current session. Five failed password attempts
lock that username for five minutes.

- `POST /api/auth/register` and `/api/auth/login` accept `username` and `password`.
- `GET /api/auth/me` returns the signed-in user; `POST /api/auth/logout` logs out.
- Browser sessions use HttpOnly, SameSite=Strict cookies, Secure when served over HTTPS.
- API clients can send the returned `access_token` as `Authorization: Bearer <token>`.
- `GET /api/adventurers` lists your characters. Character creation, encounter actions,
  and state endpoints require login. For `/states`, use your account UUID as `user_id`.

Use HTTPS outside local development. Existing test characters remain with their
original accounts and are not automatically claimable. Encounter creation currently
accepts only your own characters; cross-account party invitations and password
recovery are not implemented. Catalog endpoints remain public.

### Quest Catalog

`GET /api/quest-templates` lists reusable templates stored in Neon;
`GET /api/quest-templates/goblin-trouble` returns **Goblin Trouble**:
difficulty 2/5, 4-7 encounters, Mosswood. Its enemy pool is Goblin,
Goblin Archer, Wolf, and Hobgoblin. Possible rewards are Gold, Weapons,
Armor, and Essence. The catalog remains accessible through the API.

Startup seeds missing templates without overwriting database edits. This is
template data only: the fixed three-encounter option does not yet generate runs
from this template. Template-driven generation and reward drop rules remain unimplemented.

### Shared Enemy Catalog

The Neon `enemies` table defines names, types, the ten adventurer-style attributes,
and inclusive HP/power/guard/speed ranges. Initial balance values are editable in
Neon; startup only inserts missing definitions. Goblin, Goblin Archer, Wolf,
Hobgoblin, and the existing Roadside bandit are seeded.

`GET /api/enemies` lists definitions; `GET /api/enemies/{slug}` returns one.
Pass `enemy_slug` to `POST /api/encounters` or select an enemy in the development
console. The default remains `roadside-bandit`.

`encounter_enemies` links each combat instance to both its encounter and shared
enemy with foreign keys. Stats are rolled once, HP is multiplied by party size,
and combat updates only instance state. Catalog edits affect future spawns.
Attributes, guard, and speed are saved for future mechanics; current combat uses
HP and power. These starting values are provisional, not a final balance pass.

Existing recognized JSON enemies are linked on startup without changing their
health or rolled stats; unknown legacy enemies remain readable in their original
format. No local database is created.

### Loot Types

Neon's `loot_types` table contains Gold, Weapons, Armor, Essence, Exp, and Orbs,
each with a stable slug and display name. Startup seeds missing entries without
overwriting existing names. `GET /api/loot-types` lists the catalog and
`GET /api/loot-types/{slug}` retrieves one type. This catalog does not yet define
drop rates, amounts, or reward distribution.

### Turn Processing

Player and enemy effects now use `execute_action(actor, ability, target, turn=...)`
in `app/combat.py`. It validates living actors/targets, target teams, and cooldowns,
then applies damage or guarding and returns a structured result. It does not
advance rounds or write to the database. The encounter service chooses targets,
enforces ownership/turn order, runs enemy AI, and saves the entire action transaction.

For example, players use `POWER_STRIKE` and goblins use `DIRTY_STAB` through the
same executor. Archer, wolf, and hobgoblin attacks also use this path. The action
API accepts an optional `target_id`; omission keeps the previous default target.
Responses and persisted events include `action_results` with actor, ability,
target, actual HP loss, resulting HP, and turn.

Player abilities now use the Neon `abilities` records for names, effects, power,
targeting, and cooldowns. The character sheet has one Abilities list and three
Equipped Abilities slots. Save changes using
`POST /api/adventurers/{id}/loadout` with `{"ability_ids":["<ability UUID>"]}`.
Loadouts allow 1-3 unique learned abilities and require one damage ability. Slots
are persisted in `equipped_abilities`; unsaved loadouts default to Strike, Power
Strike, and Guard when learned. Swapping is blocked throughout unfinished adventures.

New encounter participants snapshot their equipped rules. Combat accepts
`ability_id` in place of `action` and rejects unequipped abilities. Existing action
names remain aliases for equipped Strike, Power Strike, and Guard. Old encounters
without a loadout snapshot retain their legacy actions until finished.
The former ability preview route now executes an actual action and requires
`encounter_id`, `ability_id`, and `expected_turn` (optional `target_id`).

Damage, guard, percent-max-HP healing, three-turn damage buffs, and three-turn
minimum-1-HP shields are supported, including party support casts. Turn cooldowns
use rounds; minutes/hours cooldowns use saved real-time deadlines checked on each
action. No timer advances combat. Resource costs are still catalog metadata until
resource pools are implemented.

Combat advances only when a player submits an action. There is no tick endpoint,
timer, or background combat loop. GET requests never advance combat.

- `POST /api/encounters` with `{"adventurer_ids": ["<id>"]}` starts a solo encounter;
  supply up to six adventurer IDs for a group.
- `POST /api/adventurers/{id}/quest` now starts a saved solo encounter instead of
  resolving an entire quest immediately. Its response is an encounter snapshot.
- `GET /api/encounters/{id}` returns the saved state, turn, health, and pending actors.
- `POST /api/encounters/{id}/actions` accepts
  `{"actor_id":"<id>","expected_turn":1,"action":"attack"}`.

Each living adventurer acts once per round, in any order. The final player action
resolves the enemy response in the same transaction, then returns the next player
turn (or victory/defeat). Nothing happens while players are deciding.
Duplicate actions within a round and stale turn numbers return HTTP 409.
PostgreSQL row locks serialize actions for the same encounter.

Damage and cooldown values come from the equipped database abilities. Enemy HP
scales with party size. Guard reduces incoming direct damage by 60% (minimum 1).
Survivors receive 10 gold on victory, once. Health and defeat persist.

Open `/` for the development combat console. It supports selecting a party,
submitting actions, and loading a saved encounter by ID. Reloading the page resumes
the last encounter. Generic `/states` endpoints do not drive encounter combat.

Run isolated in-memory tests with `.venv\Scripts\python -m pytest` after installing
`requirements-dev.txt`. Tests create no database files and do not access Neon;
PostgreSQL concurrency behavior requires a separate integration test.

Optional browser check: install `playwright` and run
`.venv\Scripts\python verify_encounter_ui.py --base-url http://127.0.0.1:8001`.
This uses installed Microsoft Edge and creates one test adventurer in the target database.

## Existing State API

- FastAPI service layer for gameplay endpoints
- PostgreSQL persistence through SQLAlchemy
- State version tracking per user
- Event history stored alongside the latest state
- Server-side game logic for movement, combat, loot, and XP updates

## Local setup

1. Create a virtual environment and install dependencies:

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```

2. The application uses Neon PostgreSQL only. Keep your existing `.env`.
   For a fresh checkout, create `.env` from `.env.example` and set `DATABASE_URL`
   to your Neon connection string with `sslmode=require`:

   ```bash
   copy .env.example .env
   ```

   There is no local database or Docker database setup. The connection string is
   required, has no hardcoded fallback, and is loaded from the project's `.env`.

3. Run the API:

   ```bash
   uvicorn app.main:app --reload
   ```

4. Open the interactive docs:

   - http://127.0.0.1:8000/docs

## Example flow

### Create or fetch a state

```http
POST /states
{
  "user_id": "<your-account-uuid>",
  "payload": {
    "hp": 100,
    "inventory": [],
    "location": "spawn",
    "xp": 0
  }
}
```

### Apply an event

```http
POST /states/<your-account-uuid>/events?expected_version=1
{
  "event_type": "move",
  "payload": {
    "location": "forest"
  }
}
```

### Check the latest version

```http
GET /states/<your-account-uuid>/version
```

## Why this design fits a game backend

- Front ends are thin and can send events instead of owning rules.
- The server is the source of truth for game logic and validation.
- Game state can be reconstituted from the latest snapshot and event history.
- Postgres stores durable state and event data with version checks to prevent stale client updates.


## Ability catalog and combat

`abilities` stores stable slugs, display text, effects, targeting, cooldowns,
`power`, and an optional `damage_multiplier`. A multiplier uses the actor's
attack power for nonweapon attacks; otherwise damage uses the absolute `power` value. New player
attack seeds use multipliers; existing database balance is preserved. Player
natural attack power currently starts at 10. Weapon attacks use equipped weapon
damage instead; attribute and proficiency scaling are not implemented.
Guard applies a fixed 60% direct-damage reduction for its round. Effect interpretation stays in
`app/combat.py`; the supported effects are damage, guard, heal, buff, and shield.
Structured multi-effect JSON and additional status rules are future extensions.

`starter` controls which abilities new adventurers learn. `loadout_order` orders
the default three slots; learned and equipped relationships still authorize
player actions. Enemy abilities are not automatically granted to players.
`enemy_abilities` assigns ability IDs to enemy slugs with a weight and priority.
The server picks among ready abilities at the highest priority using positive
weights; an enemy with no ready assignments skips its phase. Existing enemy
attacks hit each living hero once per cast and charge cooldown only once.
Add catalog rows and assignments to give a new enemy supported abilities.

Encounter creation snapshots the catalog and loadout. Database edits apply to
new snapshots, while active fights keep their balance and cooldowns. Old
encounters without snapshots resolve their loadouts on their next action.
Clients send an equipped `ability_id` and optional `target_id`; the legacy
`attack`, `power_strike`, and `guard` actions resolve by stable catalog slug.
The server validates actions, executes effects, and persists state and events
in the same transaction. Descriptions and cooldown settings are included in
snapshots, with cooldown deadlines stored on each combatant.

Startup upgrades old ability columns and seeds missing catalog entries and
enemy assignments. Existing definitions and nonempty enemy loadouts are left
intact. Resource costs remain catalog metadata; resource consumption, weapon
durability, and additional RNG/defense calculations are not implemented yet.


## Weapon requirements

`weapon_types` is a database catalog exposed as an array at `GET /api/weapon-types`.
Each type has a `tags` JSON array (for example sword, melee, one_handed).
`abilities.requires_weapon` distinguishes weapon actions from spells and natural
attacks. `allowed_weapon_tags` is a JSON array with **any-match** semantics: sword
or axe accepts either; an empty array accepts any equipped weapon. Tags do not
make a weapon mandatory unless `requires_weapon` is true.

Weapon damage is `round(weapon.base_damage * ability.damage_multiplier)` before
existing buffs and guard mitigation. A missing multiplier means 1.0 for weapon
attacks. Nonweapon abilities ignore the weapon and keep their prior damage rules.
There are no new sharpness, proficiency, attribute, or RNG modifiers in this change.

`weapons` holds owned weapon instances; `equipped_weapons` identifies the selected
weapon. New characters and existing characters without weapons receive an equipped
Training Sword with 10 damage. Startup leaves existing weapon inventories alone,
including intentionally unequipped characters. The one-time ability upgrade marks
existing Strike/Power Strike and armed enemy attacks as weapon attacks, retaining
multipliers and converting fixed damage to a coefficient against the 10-damage
starter weapon. Bite and support abilities do not require weapons.

- `GET /api/adventurers/{id}/weapons` lists owned weapons and current equipment.
- `POST /api/adventurers/{id}/weapon` accepts `{"weapon_id": "<owned UUID>"}`;
  use `{"weapon_id": null}` to unequip. Ownership and active-adventure locks are
  enforced by the server. Weapon stats cannot be supplied through this endpoint.
- The character sheet displays requirements and provides a weapon selector.
- `enemy_weapons` assigns a weapon type to an enemy. Its rolled encounter power
  supplies base weapon damage; wolf Bite remains a natural attack.

Encounters snapshot weapon damage and tags alongside ability requirements. Old
snapshots retain their original behavior. New weapon attacks fail before changing
HP or cooldowns when the weapon is absent or incompatible. Enemies skip abilities
whose weapon requirements they cannot meet.


## Party invite codes

Create or join parties from the character sheet. Creating a party returns a
shareable invite code; leaders can generate a new code, invalidating the old one.
Codes expire after 24 hours, accept lowercase and optional hyphens, and admit up
to six adventurers. Only a SHA-256 digest is stored in `party_invites`; the code
is shown when issued and is not returned in party listings. Multiple characters
from the same account may join. Joining twice with the same character is harmless.

- `POST /api/parties`: `{"adventurer_id":"<owned UUID>","name":"Friends"}`.
- `POST /api/parties/join`: `{"adventurer_id":"<owned UUID>","code":"<invite>"}`.
- `GET /api/parties`: parties containing your characters, members, and active encounter.
- `POST /api/parties/{id}/invite`: leader generates a replacement code.
- `POST /api/parties/{id}/selection`: leader proposes `enemy_slug` and
  `encounter_count` (1 or 3), or `template_slug`; optional `accept_rank_risk`
  is visible to all members. Returns the shared selection and `selection_revision`.
- `POST /api/parties/{id}/ready`: submit `adventurer_id`, `ready`, and the displayed
  `selection_revision`. Every member, including the leader, must approve.
- `POST /api/parties/{id}/encounters`: leader sends `selection_revision` to start
  the stored selection with the full roster. Other encounter fields cannot override it.

Selections persist in `party_plans` on Neon; the normal startup schema creation
adds this table without deleting existing data. Existing parties start without
a selection and must choose one before readying up. Changing the selection resets
all readiness; stale approvals and stale departures are rejected. Readiness is
consumed at departure. The Party tab refreshes selections and readiness automatically.

Joining alone does not authorize departure; readiness approves the selection. Rosters
cannot grow during an active adventure. Each account can submit actions only for
its own characters, while members receive shared encounter updates automatically.
Only the leader can continue to the next
fight. The encounter actor selector shows only your characters. Party joins,
code rotation, and starts serialize on the party row in PostgreSQL.

## Live encounters

Encounter pages subscribe to `WS /api/encounters/{id}/live` (WSS on HTTPS).
The first frame is `{"token":"<player access token>"}`; the built-in same-origin
UI sends `{"token":null}` and uses its HttpOnly session cookie. Tokens must never
appear in URLs. Only encounter participants may subscribe. Public browser origins
must be in `PUBLIC_CLIENT_ORIGINS`, just as for the HTTP API.

The server sends `{"type":"encounter","data":<snapshot>}` initially, after
committed changes, and after reconnection. It follows the quest into subsequent
encounters. Snapshot `revision` and encounter number prevent late HTTP responses
from replacing newer state. Actions still use the normal HTTP endpoints.
Reply to `{"type":"ping"}` with `{"type":"pong"}`; these heartbeats neither
read nor advance combat. Clients reconnect with bounded backoff and receive the
current state, without replaying actions. Village and encounter pages do not poll
state; local cooldown display timers do not make network requests.

Each server process holds one dedicated PostgreSQL LISTEN connection; transaction
NOTIFY messages carry only quest-run IDs and are delivered after commit. This
supports players connected to different workers. A reconnecting listener reloads
active subscribers to recover missed notifications. For Neon pooler URLs, only
the listener uses the corresponding direct host; credentials stay on the server.
Keep this connection in mind when configuring Neon compute suspension and connection
budgets. Reverse proxies must forward WebSocket upgrades and allow idle heartbeats.
See [Neon's connection guidance](https://github.com/neondatabase/agent-skills/blob/main/plugins/neon-postgres/skills/neon-postgres/SKILL.md)
and [Psycopg notifications](https://www.psycopg.org/docs/advanced.html#asynchronous-notifications).

## Live village

`WS /api/village/live` uses the same authentication, origin checks, heartbeat and
reconnect protocol. It sends `{"type":"village","data":{...}}` with `account`,
`adventurers` and `parties`. The optional `adventurer_id` query parameter adds the
owned character's full `adventurer` view; another user's character is rejected.

SQLAlchemy flush hooks identify changed characters, their owners and shared party
members. PostgreSQL delivers coalesced per-user notifications only after commit.
This covers joining, readiness, selection, departures, recovery, equipment,
loadouts, purchases, progression and combat outcomes. Reads and rolled-back writes
do not broadcast. A subscriber always receives its own authorized snapshot, never
another user's private account, inventory, credentials or invitation code.

The built-in village and both character clients consume pushed snapshots directly.
Initial page bootstrap and explicit commands can still fetch API data, but there
are no periodic party or village GET requests. The optional Refresh Party command
remains available. Keep asset version tags in sync when publishing frontend changes.


## Multi-target casts and persistent cooldowns

The ability catalog now has `max_targets`: a positive count, or NULL for all legal
living targets. New abilities default to one target. Party-wide support seeds and
existing enemy-only damage definitions retain their previous all-target behavior.
Both teams use the same target selector and atomic cast executor; a failure on
any target leaves every combatant unchanged, and one cast charges one cooldown.

Action requests may include `target_ids` for an explicit, ordered list. Duplicate,
dead, opposing-team support targets, and lists exceeding the limit are rejected.
Without a list the server selects up to the configured limit. The existing
`target_id` selects the primary target, followed by other legal targets for area
abilities. Send either field, not both. Both encounter actions and the character
ability-use endpoint accept this extension. Results still contain one event per
affected target with the original ability identifier.

`adventurers.combat_cooldowns` stores remaining turn cooldowns and absolute timed
cooldown deadlines in the same transaction as combat state and events. New quests
restore these values, including cooldowns on abilities that are not currently
equipped. Entering a new fight advances one round, matching quest continuation;
real-time cooldowns retain their original deadline. Existing encounter snapshots
continue to persist enemy cooldowns and support reloads. Persistent character
cooldowns are backfilled from the latest saved encounter.

Schema changes use the versioned, transactional runner in `app/migrations` and
are recorded in `schema_migrations`. Startup runs pending migrations after the
existing legacy schema bootstrap and before catalog seeding. Migration 001 adds
target limits and durable cooldowns without overwriting existing balance. It is
safe to rerun and serializes migration execution with a PostgreSQL advisory lock.


## Village, quests, and epics

The village and character sheet offer database-defined journey cards with route
previews and rewards. The Mosswood Trail is a two-encounter quest. Into the Hollow
is a five-encounter epic with mixed enemy groups and four camp rests. The displayed
reward totals assume the character survives every victory and completes the route.
Gold and experience are real persistent rewards; no unimplemented item drops are
advertised. An epic survivor currently earns 115 gold and 105 experience.

Use the existing encounter creation endpoint with `template_slug` (also supported
for parties). Without it, the original custom encounter behavior is retained.
The chosen groups and journey rules are saved at departure. Epic continuation
applies a camp rest once before creating the next encounter: the seeded policy
restores 35% maximum HP and recovers three cooldown turns and five minutes of timed
cooldowns. The usual one-round encounter transition still applies. A camp is a
player-paced transition, not a real-time waiting timer.

After victory, continue or return early using the existing `/continue` endpoint
with `{"return_to_village": true}`. Early return keeps encounter rewards but forfeits
the completion bonus. Finished journeys can return directly to the village screen.
`POST /api/adventurers/{id}/rest` performs a long rest only outside active adventures,
restoring full HP and clearing cooldowns under the seeded village policy. Neither
rest revives dead characters. Rest and departure rules stay authoritative on the
server. `action: "wait"` on encounter actions allows a turn to advance without an
ability; damage abilities may still use an explicit enemy target.

Migration 002 adds journey JSON to quest templates and the `rest_policies` catalog.
Seeds preserve existing catalog edits. Epic camp healing, character state, and
camp events persist with encounter continuation in one transaction.


## Gameplay designer

Sign in to the server-hosted village, open **Settings**, and click **Load gameplay data**.
Select a definition and choose **Edit definition** or **Create a copy**. Simple
fields have form controls; nested rules, affliction operations, and quest loot
tables use JSON fields. **Review changes** validates the draft and displays the
before/after values without saving. **Save to database** commits the reviewed edit.
New source definitions can be created by copying an existing one and changing its
key/name; deletion is not offered. **Export JSON** downloads the inspected catalog.

Catalog authoring is available only to users whose database `account_type` is
`developer`. The API enforces this role on every save; hiding the menu is not access
control. Changing an account's role takes effect on its next request.
Saved edits create `catalog_edited` events with the author and before/after values.
Stale edits are rejected; refresh the catalog before reopening the editor.

The existing `/api/abilities?inspect=true` catalog view requires authentication;
ordinary `/api/abilities` responses remain unchanged. Inspection never generates a
run or awards rewards. Raid loot previews can use a saved current rotation, while
source quest rules are edited separately. Active runs and saved raid rotations
keep their snapshots. Code-defined attribute formulas and level costs are read-only.
Planned essence powers/passives are
labeled separately from implemented orb unlocks.

## Epic rest choices and route selection

The catalog offers three quests (Mosswood Trail, Wolf Tracks, Roadside Contract)
and three epics (Into the Hollow, Through the Packlands, Ironwood Siege).
Quests have two or three battles; epics have four to six. Each new epic has one
camp rest for the whole journey. At a checkpoint send `{"choice":"rest"}` or
`{"choice":"push"}` to the existing continuation endpoint, or return as before.
An omitted choice is rejected for new epics; legacy snapshots keep automatic rests.

Rest uses the existing recovery policy and consumes the shared party rest budget.
Pushing grants no recovery and adds 10 gold / 8 experience per survivor to an
unclaimed completion bonus. The bonus is paid only at final victory. Retreat and
defeat forfeit it; already-earned battle rewards remain. Rest does not erase prior
pushes. Resting and pushing are saved atomically with the next encounter, so replaying
a checkpoint cannot farm bonuses or healing. Catalog values control the budget and
bonus rates; migration 003 upgrades future epic definitions without altering saved
journeys. New route seeds preserve database edits.
