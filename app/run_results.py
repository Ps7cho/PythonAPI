"""Read-only, event-backed results for completed quests."""
from collections import defaultdict
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.models import Adventurer, Encounter, GameEvent, QuestRun
from app.encounter_service import enemy_states


def summary(db, run_id: UUID, user):
    run = db.get(QuestRun, run_id)
    if run is None or run.status not in ('victory', 'returned', 'defeat'):
        raise HTTPException(404, 'Completed quest not found.')
    if run.quest.rewards.get('journey', {}).get('gauntlet'):
        raise HTTPException(404, 'Quest results are unavailable for gauntlets.')
    encounters = db.scalars(select(Encounter).where(Encounter.quest_run_id == run_id)
                            .order_by(Encounter.created_at, Encounter.id)).all()
    if not encounters:
        raise HTTPException(404, 'Completed quest not found.')
    roster = {p['id']: p['name'] for p in encounters[0].participants}
    owned = db.scalar(select(Adventurer.id).where(Adventurer.id.in_([UUID(i) for i in roster]),
                                                 Adventurer.owner == user.id).limit(1))
    if owned is None:
        raise HTTPException(404, 'Completed quest not found.')

    enemies = {}
    for encounter in encounters:
        for enemy in enemy_states(encounter):
            enemies[enemy['id']] = enemy.get('enemy_type') or enemy.get('enemy_slug') or 'Unknown'
    stats = {key: {'id': key, 'name': name, 'damage_dealt': 0, 'damage_received': 0,
                   'healing_done': 0, 'healing_received': 0} for key, name in roster.items()}
    dealt_by_type = defaultdict(int)
    received_by_type = defaultdict(int)
    event_gains = defaultdict(lambda: {'gold': 0, 'experience': 0})
    for event in db.scalars(select(GameEvent).where(GameEvent.quest_run_id == run_id,
            GameEvent.event_type == 'combat_action').order_by(GameEvent.timestamp, GameEvent.id)):
        for hero_id, gain in event.payload.get('reward_gains', {}).items():
            if hero_id in stats:
                event_gains[hero_id]['gold'] += gain.get('gold', 0)
                event_gains[hero_id]['experience'] += gain.get('experience', 0)
        for result in event.payload.get('action_results', []):
            actor, target = result.get('actor_id'), result.get('target_id')
            amount = result.get('amount', 0)
            if type(amount) not in (int, float) or amount <= 0:
                continue
            effect = result.get('effect')
            if effect in ('damage', 'damage_over_time'):
                if actor in stats and target in enemies:
                    stats[actor]['damage_dealt'] += amount
                    dealt_by_type[enemies[target]] += amount
                if target in stats:
                    stats[target]['damage_received'] += amount
                    received_by_type[enemies.get(actor, 'Other')] += amount
            elif effect in ('heal', 'healing_over_time'):
                if actor in stats:
                    stats[actor]['healing_done'] += amount
                if target in stats:
                    stats[target]['healing_received'] += amount

    progress = run.quest.rewards.get('journey_progress', {})
    claimed = progress.get('claimed_loot', []) if progress.get('loot_claimed') else []
    rolls = progress.get('loot_rolls', [])
    gains = progress.get('settled_run_gains', progress.get('run_gains', {}))
    if not gains:
        gains = event_gains
    elif progress.get('loot_claimed'):
        journey = run.quest.rewards.get('journey', {})
        for hero_id, gain in gains.items():
            if hero_id in stats:
                gains = {**gains, hero_id: {**gain,
                    'gold': gain.get('gold', 0) + progress.get('pushes', 0) * journey.get('push_gold', 0),
                    'experience': gain.get('experience', 0) + progress.get('pushes', 0) * journey.get('push_experience', 0)}}
    surviving = {p['id'] for p in encounters[-1].participants if p['hp'] > 0}
    return {
        'id': str(run.id), 'title': run.quest.rewards.get('journey', {}).get('title', run.quest.location),
        'kind': run.quest.rewards.get('journey', {}).get('kind', 'journey'),
        'status': run.status, 'encounters': len(encounters),
        'totals': {field: sum(row[field] for row in stats.values()) for field in
                   ('damage_dealt', 'damage_received', 'healing_done', 'healing_received')},
        'party': list(stats.values()),
        'damage_by_enemy_type': [{'type': kind, 'damage': amount} for kind, amount in sorted(dealt_by_type.items(), key=lambda item: -item[1])],
        'received_by_enemy_type': [{'type': kind, 'damage': amount} for kind, amount in sorted(received_by_type.items(), key=lambda item: -item[1])],
        'loot_rolls': rolls,
        'individual_loot': [{'id': key, 'name': name,
                             'items': claimed if key in surviving and progress.get('loot_claimed') else [],
                             'gains': gains.get(key, {}) if key in surviving or not progress.get('loot_claimed') else {}}
                            for key, name in roster.items()],
        'loot_claimed': bool(progress.get('loot_claimed')),
    }
