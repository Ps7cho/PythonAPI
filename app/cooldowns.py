"""Translate encounter deadlines to durable, character-owned cooldowns."""


def restore_cooldowns(saved, turn=1):
    saved = saved or {}
    return {"ability_ready_turns": {key: turn + remaining for key, remaining in saved.get("turns", {}).items()
                                    if remaining > 0},
            "ability_ready_at": dict(saved.get("ready_at", {}))}


def saved_cooldowns(actor, turn, completed=False):
    # Entering another fight advances one round, just like continue_quest.
    next_turn = turn + int(completed)
    return {"turns": {key: ready - next_turn for key, ready in actor.get("ability_ready_turns", {}).items()
                      if ready > next_turn},
            "ready_at": dict(actor.get("ability_ready_at", {}))}
