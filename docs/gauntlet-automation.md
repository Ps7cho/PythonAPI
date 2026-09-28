# Unattended Gauntlet batches

`scripts/gauntlet_batch.py` is a standalone Python command-line client. Once
started, it chooses actions, advances stages, repeats runs and moves to the next
configuration without prompts or browser clicks. It uses the existing authenticated
API: no database access, extra server endpoints, migrations or combat engine.

## Setup and run

1. Copy `scripts/gauntlet-batch.example.json` and replace its placeholder IDs with
   your adventurer IDs (available from `GET /api/adventurers`).
2. Use an account bearer token from the existing login flow. Set it as an environment
   variable, not inside the JSON or on the command line. Never commit credentials.
3. From the PythonAPI backend repository, run:

```powershell
# GAUNTLET_TOKEN must already be set in this process's environment.
python scripts/gauntlet_batch.py --api http://127.0.0.1:8000/api --config my-batch.json --output batch-results.json
```

For a remote deployment, pass its HTTPS backend API address, not the Cloudflare
frontend address. A running backend with Gauntlet migration 030 is required.
The client needs only Python's standard library. It refuses remote plaintext HTTP,
credential-bearing URLs, redirects, and overwriting an existing report file.

These are **real persisted Gauntlet runs**, visible in your normal history, not
throwaway simulations. Only adventurers owned by the authenticated user may enter.
The existing nonlethal rules preserve health, cooldowns, inventory and progression.

## Configurations

Each case has a unique `name` and `adventurer_ids`. Optional fields:

| Field | Default | Behavior |
| --- | --- | --- |
| `definition_slug` | `endless-road` | Saved Worldsmith Gauntlet definition |
| `ability_priority` | `['power_strike', 'attack']` | Ordered catalog slugs or ability UUIDs; `wait` ends the list |
| `repeats` | 1 | Sequential runs with the same configuration, maximum 100 |
| `max_stages` | 50 | Retire after clearing this depth, maximum 1000 |
| `max_actions` | 5000 | Maximum successful player actions per run |
| `max_seconds` | 600 | Wall-time budget per run, checked between API calls |
| `heal_below` | 0.5 | Use a listed healing ability when a legal ally's HP fraction is below this threshold |

Compare different solo/team compositions, saved enemy/scaling definitions, and
ability priority policies. To compare different definition settings, create named
definitions in Worldsmith first. The runner does **not** edit live catalogs, grant
abilities, reallocate attributes, or change saved equipment.

The simple policy tries listed learned abilities in order, skipping cooldowns and
incompatible weapons. It chooses the strongest compatible owned weapon and targets
the lowest-HP enemy or lowest-health-fraction ally. Healing/cleansing is skipped
when unnecessary. Missing skills are skipped and listed per actor under
`unavailable_priorities` in the report. With no usable listed ability, it waits.
The server still validates all commands and computes every outcome.

## Results and stopping

The JSON report checkpoints case settings, run/encounter IDs, action/rejection counts,
depth and outcomes as commands complete. Finished runs include the server summary
and initial party snapshot. Completed batches include per-case best reached/completed
depth, capped-run count and mean completed depth for uncapped runs. Capped runs are
not falsely counted as defeats. Combat logs remain accessible through the normal UI.

On reaching a stage limit after victory, the runner retires and continues the batch.
A time/action limit reached mid-battle stops the batch with `paused_limit`; the run
remains active because the API has no mid-battle retirement operation. Open the
Gauntlet history or resume the recorded encounter in the UI to finish it before
starting a new batch with those characters. Ctrl+C also saves an interrupted result
when received during a run. The runner does not resume unfinished batches automatically.

Unexpected API/network errors stop the batch. Writes are never blindly retried:
a lost start response may have created a run without returning its ID. Check your
Gauntlet history before retrying. Each HTTP call has a 30-second timeout; this can
extend a wall-time budget. Default pacing is 0.1 seconds per command; `--delay`
can change it. Keep other clients from manually controlling the same active run.

Combat proc randomness is still the game's existing per-run randomness. Repeat
cases for comparisons; these are not identical-seed statistical experiments. Catalog
edits between repeated runs can change departure snapshots, so avoid editing during
a comparison batch. Keep the runner process alive; closing it stops automation.

## Tests

From the backend repository:

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_gauntlet_batch.py tests/test_gauntlets.py -q
```

Tests drive the actual FastAPI routes with isolated SQLite. They cover unattended
solo/team/repeat runs, state preservation, retirement, mid-battle caps, preflight
ownership, no ambiguous-write retries, policy selection and invalid configuration.
