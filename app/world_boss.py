"""Alpha finale: one locked health pool, existing combat per party, durable outcomes."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, object_session

from app.auth import current_user, own_adventurer
from app.database import Base, SessionLocal, get_db
from app.models import (Adventurer, Encounter, GameEvent, Gear,
                        User, WorldBossEntry, WorldBossEvent,
                        WorldBossReward, WorldBossState)

router = APIRouter(prefix='/api/world-boss', tags=['world boss'])
SLUG = 'alpha-wolf'
GEAR_SET = tuple('alpha-wolf-' + piece for piece in (
    'head', 'shoulders', 'chest', 'bracers', 'hands', 'belt', 'legs', 'feet', 'cape'))


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def next_start(now, settings):
    """Next configured evening in the local zone, including DST transitions."""
    local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(settings['timezone']))
    target = local.replace(hour=settings['hour'], minute=0, second=0, microsecond=0)
    daily = settings.get('cadence') == 'daily'
    if not daily:
        target += timedelta(days=(settings['weekday'] - local.weekday()) % 7)
    if target <= local:
        target += timedelta(days=1 if daily else 7)
    return target.astimezone(timezone.utc).replace(tzinfo=None)


def locked_state(db):
    return db.scalar(select(WorldBossState).where(WorldBossState.slug == SLUG)
                     .with_for_update().execution_options(populate_existing=True))


def latest(db):
    return db.scalar(select(WorldBossEvent).where(WorldBossEvent.boss_slug == SLUG)
                     .order_by(WorldBossEvent.starts_at.desc()).limit(1))


def tick(now=None):
    """Commit deadline outcomes even when no adventurers are connected."""
    now = now or now_utc()
    with SessionLocal.begin() as db:
        state = locked_state(db)
        if state is None or state.phase != 'alpha' or not state.enabled:
            return None
        event = latest(db)
        if event and event.status == 'active' and now >= event.ends_at:
            event.status = 'failed'
            event.finished_at = now
            if state.delete_adventurers_on_failure:
                event.wiped_adventurers = wipe_adventurers(db, event)
            else:
                from app.live import notify_run
                for entry in db.scalars(select(WorldBossEntry).where(WorldBossEntry.event_id == event.id)):
                    battle = db.get(Encounter, entry.encounter_id)
                    if battle and battle.quest_run.status == 'active':
                        battle.state = 'defeat'
                        battle.quest_run.status = 'defeat'
                        battle.updated_at = now
                        notify_run(db, battle.quest_run_id)
            announce(db, 'world_boss_failed', dict(event_id=str(event.id), wiped=event.wiped_adventurers))
        if now >= state.next_start_at:
            start = state.next_start_at
            state.next_start_at = next_start(now, state.settings)
            # A server outage must not manufacture an unseen event and wipe.
            if now < start + timedelta(seconds=state.duration_seconds):
                event = WorldBossEvent(boss_slug=SLUG, period=start.isoformat(), starts_at=start,
                    ends_at=start + timedelta(seconds=state.duration_seconds),
                    health=state.max_health, max_health=state.max_health)
                db.add(event)
                db.flush()
                announce(db, 'world_boss_started', {'event_id': str(event.id)})
            else:
                announce(db, 'world_boss_missed', {'scheduled_at': start.isoformat()})
        if event and event.status == 'active':
            return min(event.ends_at, state.next_start_at)
        return state.next_start_at


def lifecycle_worker(stop, wakeup, retry_seconds=1):
    """Settle lifecycle transitions at their stored deadlines, not by polling."""
    import logging
    while not stop.is_set():
        # Clear before reading so a committed schedule change cannot be lost.
        wakeup.clear()
        try:
            deadline = tick()
            delay = None if deadline is None else max(
                0, (deadline - now_utc()).total_seconds())
        except Exception:
            logging.getLogger(__name__).exception(
                'Alpha Wolf lifecycle check failed; retrying.')
            delay = retry_seconds
        wakeup.wait(delay)


def announce(db, kind, payload):
    from app.live import notify_topic
    db.add(GameEvent(event_type=kind, payload=payload))
    notify_topic(db, 'world-boss')
    if payload.get('event_id'):
        notify_topic(db, 'world-boss:' + payload['event_id'])
    for account_id in db.scalars(select(User.id)):
        notify_topic(db, 'village:' + str(account_id))


def wipe_adventurers(db, event):
    """Delete character state in FK order, retaining accounts, catalogs and audit."""
    count = db.scalar(select(func.count()).select_from(Adventurer))
    roots = {'adventurers', 'parties', 'quests', 'quest_runs', 'encounters'}
    tables = list(Base.metadata.sorted_tables)
    affected = set(roots)
    while True:
        more = {table.name for table in tables if any(fk.column.table.name in affected for fk in table.foreign_keys)}
        if more <= affected:
            break
        affected |= more
    # Flush the failed event first; it has no FK to the erased game state.
    db.flush()
    for table in reversed(tables):
        if table.name not in affected:
            continue
        statement = delete(table)
        if table.name == 'game_events':
            statement = statement.where(table.c.quest_run_id.is_not(None))
        db.execute(statement)
    db.expire_all()
    return count


def lock_entry(db, template_slug):
    from app.models import QuestTemplate
    template = db.get(QuestTemplate, template_slug) if template_slug else None
    if template is None or not (template.journey or {}).get('world_boss'):
        return None
    state = locked_state(db)
    event = latest(db)
    if state.phase != 'alpha' or not event or event.status != 'active' or now_utc() >= event.ends_at:
        raise HTTPException(409, 'The Alpha Wolf event is not active. It opens nightly at 8 PM America/Denver.')
    return event


def attach(db, event, encounter, heroes, party):
    if db.scalar(select(WorldBossEntry).where(WorldBossEntry.event_id == event.id, WorldBossEntry.party_id == party.id)):
        raise HTTPException(409, 'This party has already joined this Alpha Wolf event.')
    for instance in encounter.enemy_instances:
        instance.state = {**instance.state, 'hp': event.health, 'max_hp': event.max_health}
    db.add(WorldBossEntry(encounter_id=encounter.id, event_id=event.id, party_id=party.id,
                         party_name=party.name, account_ids=sorted({str(h.owner) for h in heroes})))


def before_action(db, encounter_id):
    """Lock shared health before the party encounter; every writer uses this order."""
    entry = db.get(WorldBossEntry, encounter_id)
    if entry is None:
        return None
    state = locked_state(db)
    event = db.get(WorldBossEvent, entry.event_id, populate_existing=True)
    if event.status != 'active' or state.phase != 'alpha' or now_utc() >= event.ends_at:
        raise HTTPException(409, 'This Alpha Wolf event has ended. Refresh the event status.')
    return event


def sync_health(encounter, enemies, event=None):
    db = object_session(encounter)
    entry = db.get(WorldBossEntry, encounter.id) if db else None
    if entry is None:
        return None
    event = event or db.get(WorldBossEvent, entry.event_id)
    for enemy in enemies:
        enemy['hp'] = event.health
        enemy['max_hp'] = event.max_health
    return event


def finish_party_action(db, encounter, event, participants, enemies, request, results, messages):
    if now_utc() >= event.ends_at:
        raise HTTPException(409, 'The ten-minute deadline passed before this action completed.')
    from app.cooldowns import saved_cooldowns
    from app.encounter_service import finish_action
    from app.live import notify_run
    damage = max(0, event.health - enemies[0]['hp'])
    event.health = max(0, event.health - damage)
    entry = db.get(WorldBossEntry, encounter.id)
    entry.damage += damage
    db.flush()
    for actor in participants:
        hero = db.get(Adventurer, UUID(actor['id']))
        hero.health = actor['hp']
        hero.is_alive = actor['hp'] > 0
        hero.combat_statuses = actor.get('statuses', [])
        hero.combat_cooldowns = saved_cooldowns(actor, encounter.turn, encounter.state != 'player_turn')
    if encounter.state == 'defeat':
        encounter.quest_run.status = 'defeat'
        messages.append('Your party fell. Other parties can continue fighting the shared Alpha Wolf.')
    if event.health == 0:
        state = locked_state(db)
        if state.delete_adventurers_on_failure:
            state.phase = 'beta'
        event.status = 'defeated'
        event.finished_at = now_utc()
        # One entitlement per contributing account, even if its party died.
        accounts = {account for row in db.scalars(select(WorldBossEntry).where(WorldBossEntry.event_id == event.id, WorldBossEntry.damage > 0)) for account in row.account_ids}
        for account in accounts:
            db.add(WorldBossReward(event_id=event.id, account_id=UUID(account)))
        for row in db.scalars(select(WorldBossEntry).where(WorldBossEntry.event_id == event.id)):
            battle = db.get(Encounter, row.encounter_id)
            if battle and battle.state == 'player_turn':
                battle.state = 'victory'
            if battle:
                battle.quest_run.status = 'completed'
                battle.updated_at = now_utc()
                notify_run(db, battle.quest_run_id)
        encounter.state = 'victory'
        encounter.quest_run.status = 'completed'
        outcome = 'Beta has begun!' if state.phase == 'beta' else 'Nightly testing continues.'
        messages.append(f'The Alpha Wolf is defeated. {outcome} Claim your account-bound epic Wolf Sovereign set.')
        announce(db, 'world_boss_defeated', {'event_id': str(event.id), 'phase': state.phase})
    # One shared topic fans out private snapshots to subscribed parties. Avoid
    # rewriting hundreds of encounter rows for every hit against the boss.
    from app.live import notify_topic
    notify_topic(db, 'world-boss:' + str(event.id))
    return finish_action(db, encounter, request, messages, results)


def event_view(db, event):
    if event is None:
        return None
    return dict(id=str(event.id), name='Alpha Wolf', status=event.status, health=event.health,
                max_health=event.max_health, starts_at=event.starts_at.isoformat()+'Z',
                ends_at=event.ends_at.isoformat()+'Z', wiped_adventurers=event.wiped_adventurers,
                parties=db.scalar(select(func.count()).select_from(WorldBossEntry).where(WorldBossEntry.event_id == event.id)))


@router.get('')
def status(db: Session = Depends(get_db), user: User = Depends(current_user)):
    tick()
    db.expire_all()
    state = db.get(WorldBossState, SLUG)
    event = latest(db)
    rewards = list(db.scalars(select(WorldBossReward).where(WorldBossReward.account_id == user.id, WorldBossReward.claimed.is_(False))))
    return dict(phase=state.phase, enabled=state.enabled, schedule='Nightly 8 PM America/Denver',
                delete_adventurers_on_failure=state.delete_adventurers_on_failure,
                can_manage=user.account_type == 'developer',
                next_start_at=state.next_start_at.isoformat()+'Z', server_time=now_utc().isoformat()+'Z',
                event=event_view(db, event), template_slug='alpha-wolf-finale',
                rewards=[str(r.event_id) for r in rewards], max_health=state.max_health)


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    delete_adventurers_on_failure: StrictBool


@router.patch('')
def update_settings(payload: Settings, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.account_type != 'developer':
        raise HTTPException(403, 'Only developers can change the adventurer deletion switch.')
    state = locked_state(db)
    previous = state.delete_adventurers_on_failure
    state.delete_adventurers_on_failure = payload.delete_adventurers_on_failure
    if not payload.delete_adventurers_on_failure and state.phase == 'beta':
        # Explicitly returning to testing resumes the nightly event schedule.
        state.phase = 'alpha'
        state.next_start_at = next_start(now_utc(), state.settings)
    announce(db, 'world_boss_settings_changed', {
        'account_id': str(user.id), 'previous': previous,
        'delete_adventurers_on_failure': state.delete_adventurers_on_failure})
    db.commit()
    return {'delete_adventurers_on_failure': state.delete_adventurers_on_failure}


class Claim(BaseModel):
    adventurer_id: UUID
    event_id: UUID


@router.post('/rewards/claim')
def claim(payload: Claim, db: Session = Depends(get_db), user: User = Depends(current_user)):
    hero = own_adventurer(db, payload.adventurer_id, user)
    reward = db.scalar(select(WorldBossReward).where(WorldBossReward.event_id == payload.event_id,
                WorldBossReward.account_id == user.id).with_for_update().execution_options(populate_existing=True))
    if reward is None:
        raise HTTPException(404, 'No earned Alpha Wolf reward for this account.')
    if reward.claimed:
        raise HTTPException(409, 'This account has already claimed its set.')
    if not hero.is_alive:
        raise HTTPException(409, 'Choose a living adventurer to receive the set.')
    pieces = [Gear(adventurer_id=hero.id, definition_slug=slug, rarity='epic', bound_account_id=user.id) for slug in GEAR_SET]
    db.add_all(pieces)
    db.flush()
    reward.claimed = True
    reward.gear_ids = [str(piece.id) for piece in pieces]
    db.commit()
    return {'claimed': True, 'gear_ids': reward.gear_ids, 'message': 'Wolf Sovereign epic set claimed. Bound to your account; cannot be auctioned.'}
