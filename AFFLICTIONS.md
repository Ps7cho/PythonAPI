# Affliction Instructions

Abilities use an ordered `affliction_ops` JSON array in the shared combat executor.
Players and enemies use identical rules. Clients submit an equipped ability ID and
target IDs, never instructions, stack counts, damage values, or definitions.

Authenticated discovery:
- `GET /api/afflictions`: shared catalog with stack caps and rules.
- `GET /api/afflictions/grammar`: instruction JSON schema.

## Operations

| Operation | Behavior |
| --- | --- |
| Apply | Add stacks up to the catalog cap; refresh their duration. |
| Amplify | Increase existing stacks' per-stack potency, capped at +10,000. |
| Spread | Copy stacks from the first selected target to subsequent targets; `move: true` transfers them instead. |
| Consume | Remove up to `stacks` and grant an immediate effect per removed stack. |
| Detonate | Consume for direct damage. |
| Convert | Exchange stacks 1:1 into another affliction, or into actor combat resources at `power` per stack. |
| Cleanse | Remove up to `stacks` harmful stacks of the named affliction. |
| Preserve | Pause expiry and prevent removal for `rounds`; periodic effects still happen. |
| Trigger | Fire an immediate effect once when the pipeline crosses `threshold` from below. |
| Exploit | Grant an effect per present stack, up to `stacks`, without removal. |

Immediate effects are `damage` to the selected opponent, or `heal`, `guard`,
and `resource` to the actor. Default effect is damage. Guard is a shared
current-round block pool. Damage uses resistance, damage reduction, Guard and
shields; periodic damage continues to bypass Guard.

Amplification increases periodic potency and per-stack Consume, Detonate and
Exploit effects. Trigger power is a flat threshold reward. Spread copies base
stacks with a fresh duration, not source amplification or preservation.
Destination caps/immunity never destroy source stacks during Move or Convert.
Preservation blocks Cleanse, Consume, Detonate, Move and Convert removal.
Rest and death retain their encounter lifecycle cleanup behavior.

Trigger is an ordered instruction in an ability, not a persistent global listener.
It fires at most once per target/affliction/threshold per cast, does not consume,
and does not repeat while already above threshold. Dropping below the threshold
rearms a future crossing.

## Example

Stored ability instructions for Flashpoint:

```json
[
  {"op": "apply", "affliction": "burn", "stacks": 2},
  {"op": "trigger", "affliction": "burn", "threshold": 3, "power": 12}
]
```

The server resolves catalog definitions when creating combat ability snapshots.
Existing encounters retain their saved snapshots. New encounters receive the
current equipped abilities. Failed pipelines publish no partial damage, stacks
or cooldowns. Interaction results appear in the persistent combat log and are
forwarded with the existing encounter WebSocket updates; no ticking or polling
system was introduced.

## Catalog And Testing

Existing Bleed, Poison, Sin and Necrosis retain their balance. Burn adds a harmful
periodic effect; Holy is beneficial, non-periodic, and is not cleansed.
Eleven example abilities are unlocked for existing and new adventurers without
changing equipped loadouts. Equip them from the character page.

Holy Resolve demonstrates a combat-only resource, Zeal. It is visible in combat
but is not inventory, currency, experience or a persistent reward, and currently
has no spending ability. Example abilities are development content, not a final
balance pass.

Run `python -m pytest tests/test_affliction_grammar.py` for focused coverage.
