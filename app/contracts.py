"""Recovery contracts and non-consumable soul rewards, committed with combat."""
from copy import deepcopy
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import current_user, own_adventurer
from app.database import get_db
from app.models import Adventurer, RecoveryContract, RecoveredSoul, User
from app.progression import rank_for

router = APIRouter(prefix='/api/contracts', tags=['recovery contracts'])


def contract_view(row):
    return {'id': str(row.id), 'title': row.title, 'region': row.region, 'status': row.status,
            'created_at': row.created_at.isoformat(), 'encounter_count': len(row.route),
            'required_rank': row.rewards.get('journey', {}).get('required_rank', 'iron'),
            'fallen': row.fallen, 'soul_count': len(row.fallen),
            'active_party_count': len(row.active_run_ids or ([str(row.active_run_id)] if row.active_run_id else []))}


def bulletin(db):
    return [contract_view(row) for row in db.scalars(select(RecoveryContract)
            .where(RecoveryContract.status != 'completed').order_by(RecoveryContract.created_at.desc()).limit(100))]


def changed(db):
    from app.live import notify_topic
    notify_topic(db, 'bulletin')


def post_defeat(db, encounter):
    if encounter.state != 'defeat' or any(p['hp'] > 0 for p in encounter.participants):
        return
    if db.scalar(select(RecoveryContract.id).where(RecoveryContract.source_run_id == encounter.quest_run_id)):
        return
    fallen = []
    for actor in encounter.participants:
        hero = db.get(Adventurer, UUID(actor['id']))
        rank = rank_for(db, hero.level)
        equipped = {a.get('slug') for a in actor.get('equipped_abilities', [])}
        fallen.append({'id': actor['id'], 'name': actor['name'], 'level': hero.level,
                       'rank': rank.name, 'rank_slug': rank.slug,
                       'abilities': [{'id': str(a.ability_id), 'name': a.ability.name,
                                      'description': a.ability.description,
                                      'equipped': str(a.ability_id) in equipped}
                                     for a in hero.ability_inventory if a.unlocked]})
    quest = encounter.quest_run.quest
    rewards = deepcopy(quest.rewards)
    rewards.pop('journey_progress', None)
    journey = rewards.setdefault('journey', {})
    journey['repeat_groups'] = False
    journey['original_encounter_count'] = len(quest.encounter_pool)
    existing_id = journey.get('contract_id')
    if existing_id:
        contract = db.scalar(select(RecoveryContract).where(RecoveryContract.id == UUID(existing_id))
                             .with_for_update().execution_options(populate_existing=True))
        if contract is not None:
            known = {fallen_profile['id'] for fallen_profile in contract.fallen}
            contract.fallen = [*contract.fallen, *(profile for profile in fallen if profile['id'] not in known)]
            db.flush()
            changed(db)
            return
    db.add(RecoveryContract(source_run_id=encounter.quest_run_id,
           title='Soul recovery: ' + journey.get('title', quest.location),
           region=quest.location, fallen=fallen, route=deepcopy(quest.encounter_pool), rewards=rewards))
    db.flush()
    changed(db)


def settle_contract(db, run, participants, *, completed=False):
    contract = db.scalar(select(RecoveryContract).where(RecoveryContract.active_run_id == run.id)
                         .with_for_update().execution_options(populate_existing=True))
    if contract is None:
        for candidate in db.scalars(select(RecoveryContract).where(RecoveryContract.status == 'in_progress')
                                    .with_for_update().execution_options(populate_existing=True)):
            if str(run.id) in (candidate.active_run_ids or []):
                contract = candidate
                break
    if contract is None or contract.status != 'in_progress':
        return []
    messages = []
    if completed:
        survivors = sorted((p for p in participants if p['hp'] > 0), key=lambda p: p['id'])
        if not survivors:
            raise HTTPException(409, 'A survivor must recover the souls.')
        for index, fallen in enumerate(contract.fallen):
            owner = survivors[index % len(survivors)]
            db.add(RecoveredSoul(contract_id=contract.id, fallen_id=UUID(fallen['id']),
                                adventurer_id=UUID(owner['id']), profile=deepcopy(fallen)))
            messages.append(f"{owner['name']} recovers the soul of {fallen['name']} ({fallen['rank']}).")
        contract.status = 'completed'
    else:
        active_ids = [run_id for run_id in (contract.active_run_ids or []) if run_id != str(run.id)]
        contract.active_run_ids = active_ids
        if contract.active_run_id == run.id:
            contract.active_run_id = UUID(active_ids[0]) if active_ids else None
        if not active_ids:
            contract.status = 'open'
            messages.append('The soul recovery contract is open again.')
        else:
            messages.append('This rescue party failed. Other parties are still attempting the contract.')
    db.flush()
    changed(db)
    return messages


def soul_inventory(db, hero_id):
    return [{'id': str(s.id), 'name': 'Soul of ' + s.profile['name'], 'type': 'soul',
             'loot_type': 'souls', 'quantity': 1, 'contract_id': str(s.contract_id), 'fallen': s.profile}
            for s in db.scalars(select(RecoveredSoul).where(RecoveredSoul.adventurer_id == hero_id)
                                .order_by(RecoveredSoul.created_at, RecoveredSoul.id))]


@router.get('')
def list_contracts(response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)):
    response.headers['Cache-Control'] = 'no-store'
    return bulletin(db)


@router.get('/{contract_id}')
def inspect_contract(contract_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    row = db.get(RecoveryContract, contract_id)
    if row is None:
        raise HTTPException(404, 'Contract not found.')
    return contract_view(row)


class AcceptContract(BaseModel):
    adventurer_ids: list[UUID] = Field(min_length=1, max_length=6)
    accept_rank_risk: StrictBool = False


@router.post('/{contract_id}/accept', status_code=201)
def accept_contract(contract_id: UUID, payload: AcceptContract,
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    from app.encounter_service import start_encounter
    for hero_id in payload.adventurer_ids:
        own_adventurer(db, hero_id, user)
    return start_encounter(db, payload.adventurer_ids, contract_id=contract_id,
                           accept_rank_risk=payload.accept_rank_risk)
