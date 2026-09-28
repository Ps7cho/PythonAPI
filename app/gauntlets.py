"""Endurance-run orchestration around the shared encounter service."""
from copy import deepcopy
from datetime import datetime
from random import Random
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import current_user, own_adventurer
from app.database import get_db
from app.enemies import roll_enemy
from app.models import Enemy, Encounter, GauntletDefinition, GauntletRun, User
from app.parties import MAX_MEMBERS

router = APIRouter(tags=['gauntlets'])


class Rules(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enemy_pool: list[str] = Field(min_length=1, max_length=100)
    health_step_percent: StrictInt = Field(default=20, ge=1, le=1000)
    power_step_percent: StrictInt = Field(default=10, ge=1, le=1000)
    max_party_size: StrictInt | None = Field(default=None, ge=1, le=MAX_MEMBERS)


class Start(BaseModel):
    model_config = ConfigDict(extra='forbid')
    definition_slug: str = 'endless-road'
    adventurer_ids: list[UUID] = Field(min_length=1, max_length=MAX_MEMBERS)


def composition(ids):
    return '|'.join(sorted(str(i) for i in ids))


def validate_rules(db, data):
    rules = Rules.model_validate(data)
    found = set(db.scalars(select(Enemy.slug).where(Enemy.slug.in_(rules.enemy_pool))))
    if found != set(rules.enemy_pool):
        raise ValueError('Gauntlet enemy pool references an unavailable enemy.')
    return rules.model_dump()


def departure_config(db, definition):
    config = validate_rules(db, definition.settings)
    # Snapshot enemy stats, skills and equipment once. Worldsmith edits affect
    # future runs only, including enemies in stages not yet entered.
    config['enemies'] = [deepcopy(roll_enemy(db.get(Enemy, slug), 1,
                            rng=Random(f'{definition.slug}:{slug}')).state)
                         for slug in config['enemy_pool']]
    config['name'] = definition.name
    return config


def stage_entry(config, depth):
    enemy = deepcopy(config['enemies'][(depth - 1) % len(config['enemies'])])
    enemy['hp'] = enemy['max_hp'] = max(1, enemy['max_hp'] * (100 + (depth - 1) * config['health_step_percent']) // 100)
    enemy['power'] = max(1, enemy['power'] * (100 + (depth - 1) * config['power_step_percent']) // 100)
    if enemy.get('equipped_weapon'):
        enemy['equipped_weapon']['base_damage'] = enemy['power']
    return {'encounter_id': str(uuid4()), 'enemy_slugs': [enemy['enemy_slug']],
            'seeded_enemies': [enemy], 'description': f'Gauntlet stage {depth}'}


def attach_run(db, quest_run, encounter, owner_id, definition, config):
    ids = sorted(p['id'] for p in encounter.participants)
    record = GauntletRun(id=quest_run.id, owner_id=owner_id, definition_slug=definition.slug,
                        party_key=composition(ids), participant_ids=ids,
                        party_snapshot=deepcopy(encounter.participants), config_snapshot=config,
                        current_stage=1, highest_stage_reached=1, highest_stage_completed=0,
                        encounters_completed=0, status='active', quest_run=quest_run)
    db.add(record)


def advance(encounter, return_to_village, choice):
    run = encounter.quest_run.gauntlet
    if run.status != 'awaiting_continue':
        raise HTTPException(409, 'This Gauntlet cannot advance.')
    if choice is not None:
        raise HTTPException(422, 'Gauntlets do not offer camp rests or push choices.')
    if return_to_village:
        run.status = 'retired'
        run.termination_reason = 'Player ended the run after clearing a stage.'
        run.ended_at = datetime.utcnow()
        return
    run.current_stage += 1
    run.highest_stage_reached = run.current_stage
    run.status = 'active'
    quest = encounter.quest_run.quest
    quest.encounter_pool = [*quest.encounter_pool, stage_entry(run.config_snapshot, run.current_stage)]


def record_result(encounter):
    run = encounter.quest_run.gauntlet
    if encounter.state == 'victory':
        run.highest_stage_completed = run.current_stage
        run.encounters_completed += 1
        run.status = 'awaiting_continue'
        encounter.quest_run.status = 'awaiting_continue'
    elif encounter.state == 'defeat':
        run.status = 'failed'
        run.ended_at = datetime.utcnow()
        run.termination_reason = 'All participating adventurers were defeated.'
        encounter.quest_run.status = 'defeat'
        encounter.quest_run.current_stage = 'complete'


def view(run):
    end = run.ended_at or datetime.utcnow()
    return {'id': str(run.id), 'definition_slug': run.definition_slug,
            'name': run.config_snapshot['name'], 'party_key': run.party_key,
            'participant_ids': run.participant_ids,
            'party': [{k: p.get(k) for k in ('id', 'name', 'max_hp', 'attributes')} for p in run.party_snapshot],
            'current_stage': run.current_stage, 'highest_stage_reached': run.highest_stage_reached,
            'highest_stage_completed': run.highest_stage_completed, 'encounters_completed': run.encounters_completed,
            'status': run.status, 'started_at': run.started_at, 'ended_at': run.ended_at,
            'duration_seconds': max(0, int((end - run.started_at).total_seconds())),
            'termination_reason': run.termination_reason,
            'encounter_id': run.quest_run.quest.encounter_pool[-1]['encounter_id']}


@router.get('/api/gauntlets')
def definitions(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [{'slug': d.slug, 'name': d.name, 'description': d.description,
             'settings': d.settings, 'party_limit': d.settings.get('max_party_size') or MAX_MEMBERS}
            for d in db.scalars(select(GauntletDefinition).order_by(GauntletDefinition.slug))]


@router.post('/api/gauntlet-runs', status_code=201)
def start(payload: Start, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from app.encounter_service import start_encounter
    for hero_id in payload.adventurer_ids:
        own_adventurer(db, hero_id, user)
    definition = db.get(GauntletDefinition, payload.definition_slug)
    if definition is None:
        raise HTTPException(404, 'Gauntlet not found.')
    try:
        config = departure_config(db, definition)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    if len(payload.adventurer_ids) > (config['max_party_size'] or MAX_MEMBERS):
        raise HTTPException(422, 'This Gauntlet party is too large.')
    encounter = start_encounter(db, payload.adventurer_ids,
                               gauntlet=(user.id, definition, config))
    return {'run': view(db.get(GauntletRun, UUID(encounter['quest']['id']))), 'encounter': encounter}


@router.get('/api/gauntlet-runs')
def history(limit: int = Query(default=50, ge=1, le=100), offset: int = Query(default=0, ge=0),
            db: Session = Depends(get_db), user: User = Depends(current_user)):
    runs = db.scalars(select(GauntletRun).where(GauntletRun.owner_id == user.id)
                     .order_by(GauntletRun.started_at.desc(), GauntletRun.id).offset(offset).limit(limit)).all()
    best = db.execute(select(GauntletRun.party_key, func.max(GauntletRun.highest_stage_completed),
                             func.max(GauntletRun.highest_stage_reached))
                      .where(GauntletRun.owner_id == user.id).group_by(GauntletRun.party_key)).all()
    return {'runs': [view(r) for r in runs], 'best_completed': max((r[1] for r in best), default=0),
            'best_reached': max((r[2] for r in best), default=0),
            'best_by_party': [{'participant_ids': key.split('|'), 'completed': completed, 'reached': reached}
                              for key, completed, reached in best]}


@router.get('/api/gauntlet-runs/{run_id}')
def read(run_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    run = db.scalar(select(GauntletRun).where(GauntletRun.id == run_id, GauntletRun.owner_id == user.id))
    if run is None:
        raise HTTPException(404, 'Gauntlet run not found.')
    battles = db.scalars(select(Encounter).where(Encounter.quest_run_id == run.id).order_by(Encounter.created_at)).all()
    return {**view(run), 'encounters': [{'id': str(e.id), 'stage': index + 1, 'state': e.state, 'turn': e.turn}
                                      for index, e in enumerate(battles)],
            'party_snapshot': run.party_snapshot}
