import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.auth import current_user, own_adventurer
from app.database import get_db
from app.models import Adventurer, Enemy, Party, PartyInvite, PartyMember, PartyPlan, QuestRun, QuestTemplate, User


router = APIRouter(prefix="/api/parties", tags=["parties"])
MAX_MEMBERS = 6


class PartyCreate(BaseModel):
    adventurer_id: UUID
    name: str = Field(min_length=1, max_length=80)


class PartyJoin(BaseModel):
    adventurer_id: UUID
    code: str = Field(min_length=1, max_length=64)


class PartyLeave(BaseModel):
    adventurer_id: UUID


class PartyEncounter(BaseModel):
    contract_id: UUID | None = None
    selection_revision: UUID | None = None
    accept_rank_risk: StrictBool = False
    enemy_slug: str = "roadside-bandit"
    encounter_count: Literal[1, 3] = 1
    template_slug: str | None = None


class PartyReady(BaseModel):
    selection_revision: UUID | None = None
    adventurer_id: UUID | None = None
    ready: StrictBool | None = None


def locked_party(db, party_id):
    party = db.scalar(select(Party).where(Party.id == party_id).with_for_update())
    if party is None:
        raise HTTPException(404, "Party not found.")
    return party


def require_leader(party, user):
    if party.leader_adventurer.owner != user.id:
        raise HTTPException(403, "Only the party leader can do this.")


def require_idle(db, party):
    if db.scalar(select(QuestRun.id).where(QuestRun.party_id == party.id,
                                         QuestRun.status.in_(["active", "awaiting_continue"])).limit(1)):
        raise HTTPException(409, "Finish the party's adventure first.")


def view(db, party, user, runs=None, plans=None):
    plan = plans.get(party.id) if plans is not None else db.get(PartyPlan, party.id)
    run = runs.get(party.id) if runs is not None else db.scalar(select(QuestRun).where(QuestRun.party_id == party.id,
                                         QuestRun.status.in_(["active", "awaiting_continue"])).limit(1))
    current_member = next((m for m in party.members if m.adventurer.owner == user.id), None)
    return {"id": str(party.id), "name": party.name, "leader_id": str(party.leader),
        "is_leader": party.leader_adventurer.owner == user.id,
        "is_ready": bool(plan and current_member and current_member.is_ready),
        "members": [{"id": str(m.adventurer_id), "name": m.adventurer.name,
             "is_ready": bool(plan and m.is_ready), "is_leader": m.adventurer_id == party.leader,
             "is_yours": m.adventurer.owner == user.id,
             "health": m.adventurer.health, "is_alive": m.adventurer.is_alive}
                    for m in party.members], "max_members": MAX_MEMBERS,
        "selection": plan.selection if plan else None,
        "selection_revision": str(plan.revision) if plan else None,
        "all_ready": bool(plan and party.members) and all(m.is_ready and m.adventurer.is_alive for m in party.members),
        "active_encounter_id": run.current_stage if run else None}


def issue_invite(db, party):
    # 80 random bits; human-friendly hexadecimal, case-insensitive and grouped.
    raw = secrets.token_hex(10).upper()
    code = "-".join(raw[i:i + 5] for i in range(0, len(raw), 5))
    invite = db.get(PartyInvite, party.id)
    if invite is None:
        invite = PartyInvite(party_id=party.id)
        db.add(invite)
    invite.code_hash = hashlib.sha256(raw.encode()).hexdigest()
    invite.expires_at = datetime.utcnow() + timedelta(hours=24)
    return {"code": code, "expires_at": invite.expires_at.isoformat() + "Z"}


@router.get("")
def my_parties(response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)):
    response.headers['Cache-Control'] = 'no-store'
    rows = db.scalars(select(Party).join(PartyMember).join(Adventurer, Adventurer.id == PartyMember.adventurer_id)
                      .where(Adventurer.owner == user.id).distinct().order_by(Party.created_at.desc())
                      .options(selectinload(Party.leader_adventurer),
                               selectinload(Party.members).joinedload(PartyMember.adventurer))).all()
    runs = {run.party_id: run for run in db.scalars(select(QuestRun).where(
        QuestRun.party_id.in_([party.id for party in rows]),
        QuestRun.status.in_(['active', 'awaiting_continue'])))} if rows else {}
    plans = {plan.party_id: plan for plan in db.scalars(select(PartyPlan).where(PartyPlan.party_id.in_([party.id for party in rows])))} if rows else {}
    return [view(db, party, user, runs, plans) for party in rows]


@router.post("", status_code=201)
def create_party(payload: PartyCreate, response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)):
    hero = own_adventurer(db, payload.adventurer_id, user)
    name = payload.name.strip()
    if not name:
        raise HTTPException(422, "Party name is required.")
    party = Party(name=name, leader=hero.id)
    db.add(party)
    db.flush()
    db.add(PartyMember(party_id=party.id, adventurer_id=hero.id, is_ready=False))
    invite = issue_invite(db, party)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return {**view(db, party, user), "invite": invite}


@router.post("/join")
def join_party(payload: PartyJoin, db: Session = Depends(get_db), user: User = Depends(current_user)):
    hero = own_adventurer(db, payload.adventurer_id, user)
    raw = payload.code.strip().replace("-", "").upper()
    code_hash = hashlib.sha256(raw.encode()).hexdigest()
    party_id = db.scalar(select(PartyInvite.party_id).where(PartyInvite.code_hash == code_hash))
    if party_id is None:
        raise HTTPException(404, "Invite code is invalid or expired.")
    party = locked_party(db, party_id)
    # Re-read after the party lock so rotation and joins serialize together.
    invite = db.get(PartyInvite, party_id, populate_existing=True)
    if invite.code_hash != code_hash or invite.expires_at <= datetime.utcnow():
        raise HTTPException(404, "Invite code is invalid or expired.")
    if any(m.adventurer_id == hero.id for m in party.members):
        return view(db, party, user)
    require_idle(db, party)
    if len(party.members) >= MAX_MEMBERS:
        raise HTTPException(409, "Party is full.")
    db.add(PartyMember(party_id=party.id, adventurer_id=hero.id))
    db.commit()
    return view(db, party, user)


@router.post("/{party_id}/ready")
def set_ready(party_id: UUID, ready: bool | None = None, payload: PartyReady | None = Body(default=None),
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    party = locked_party(db, party_id)
    member = next((m for m in party.members if m.adventurer.owner == user.id
                   and (payload is None or payload.adventurer_id is None or m.adventurer_id == payload.adventurer_id)), None)
    if member is None:
        raise HTTPException(403, "You are not in this party.")
    if db.scalar(select(QuestRun.id).where(QuestRun.party_id == party.id,
                                           QuestRun.status.in_(["active", "awaiting_continue"]))) is not None:
        raise HTTPException(409, "The party is already adventuring.")
    if not member.adventurer.is_alive:
        raise HTTPException(409, 'A defeated adventurer cannot ready up.')
    plan = db.get(PartyPlan, party.id)
    if plan is None or payload is None or payload.selection_revision != plan.revision:
        raise HTTPException(409, 'Review the current party encounter before readying up.')
    member.is_ready = payload.ready if payload and payload.ready is not None else ready if ready is not None else True
    db.commit()
    return view(db, party, user)


@router.post('/{party_id}/leave')
def leave_party(party_id: UUID, payload: PartyLeave,
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    hero = own_adventurer(db, payload.adventurer_id, user)
    party = locked_party(db, party_id)
    member = next((m for m in party.members if m.adventurer_id == hero.id), None)
    if member is None:
        raise HTTPException(403, 'That adventurer is not in this party.')
    require_idle(db, party)
    remaining = [m for m in party.members if m is not member]
    if party.leader == hero.id and remaining:
        party.leader = remaining[0].adventurer_id
    for other in remaining:
        other.is_ready = False
    plan = db.get(PartyPlan, party.id)
    if plan:
        plan.revision = uuid4()
    db.delete(member)
    # Retain historical parties because completed quest runs reference them.
    db.commit()
    return {'left': True, 'party_id': str(party.id), 'adventurer_id': str(hero.id)}


@router.post('/{party_id}/selection')
def select_encounter(party_id: UUID, payload: PartyEncounter,
                     db: Session = Depends(get_db), user: User = Depends(current_user)):
    party = locked_party(db, party_id)
    require_leader(party, user)
    require_idle(db, party)
    from app.models import RecoveryContract
    definition = db.get(RecoveryContract, payload.contract_id) if payload.contract_id else db.get(QuestTemplate, payload.template_slug) if payload.template_slug else db.get(Enemy, payload.enemy_slug)
    if definition is None:
        raise HTTPException(404, 'Encounter selection not found.')
    if payload.contract_id and definition.status not in ('open', 'in_progress'):
        raise HTTPException(409, 'This contract is not open.')
    selection = {'template_slug': payload.template_slug,
                 'enemy_slug': None if payload.template_slug else payload.enemy_slug,
                 'encounter_count': None if payload.template_slug else payload.encounter_count,
                 'name': definition.title if payload.contract_id else definition.name, 'accept_rank_risk': payload.accept_rank_risk,
                 'contract_id': str(payload.contract_id) if payload.contract_id else None}
    if payload.contract_id:
        selection.update(template_slug=None, enemy_slug=None, encounter_count=len(definition.route))
    plan = db.get(PartyPlan, party.id)
    if plan is None:
        plan = PartyPlan(party_id=party.id, selection=selection)
        db.add(plan)
    elif plan.selection == selection:
        return view(db, party, user)
    else:
        plan.selection = selection
        plan.revision = uuid4()
    for member in party.members:
        member.is_ready = False
    db.commit()
    return view(db, party, user)


@router.post("/{party_id}/invite")
def rotate_invite(party_id: UUID, response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)):
    party = locked_party(db, party_id)
    require_leader(party, user)
    invite = issue_invite(db, party)
    db.commit()
    response.headers["Cache-Control"] = "no-store"
    return invite


@router.post("/{party_id}/encounters", status_code=201)
def start_party_encounter(party_id: UUID, payload: PartyEncounter,
                          db: Session = Depends(get_db), user: User = Depends(current_user)):
    from app.encounter_service import start_encounter
    party = locked_party(db, party_id)
    require_leader(party, user)
    require_idle(db, party)
    plan = db.get(PartyPlan, party.id)
    if plan is None or payload.selection_revision != plan.revision:
        raise HTTPException(409, 'Select an encounter and have everyone approve its current selection first.')
    selection = plan.selection
    ids = [m.adventurer_id for m in party.members]
    if not 1 <= len(ids) <= MAX_MEMBERS:
        raise HTTPException(409, "Party must have between one and six adventurers.")
    return start_encounter(db, ids, selection['enemy_slug'] or 'roadside-bandit', selection['encounter_count'] or 1,
                           party=party, template_slug=selection['template_slug'], accept_rank_risk=selection['accept_rank_risk'],
                           contract_id=UUID(selection['contract_id']) if selection.get('contract_id') else None)
