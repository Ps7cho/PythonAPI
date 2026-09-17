from app.attributes import derived_stats
from app.consumables import inventory as consumable_inventory
from app.models import OwnedConsumable
from app.progression import award_experience, rank_for, validate_rank_entry
from app.statistics import increment, increment_account, record_kill
from copy import deepcopy
from dataclasses import replace
from random import Random
from time import time
from datetime import datetime, timedelta
from app.live import notify_run
from uuid import UUID, uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, StrictBool
from typing import Literal
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EntityType, Ability, Adventurer, Encounter, Enemy, GameEvent, Party, PartyMember, Quest, QuestRun, QuestTemplate, User
from app.enemies import roll_enemy, enemy_loadout
from app.cooldowns import restore_cooldowns, saved_cooldowns
from app.combat import execute_cast, select_targets, InvalidCombatAction, CombatAbility, validate_weapon, tick_statuses, consume_flinch
from app.loadouts import combat_loadout
from app.journeys import push_xp_bonus, boosted_encounter_xp, build_plan, roll_group, RestRules, apply_rest, active_adventure
from app.group_journeys import group_view, append_group, earn_group_loot, claim_group_loot, bank_run_gains
from app.weapons import equipped_weapon, enemy_weapon, combat_weapons
from app.enemy_intents import plan_moves, preview_moves, execute_planned


class EncounterCreateRequest(BaseModel):
    accept_rank_risk: StrictBool = False
    adventurer_ids: list[UUID] = Field(min_length=1, max_length=6)
    enemy_slug: str = "roadside-bandit"
    encounter_count: Literal[1, 3] = 1
    template_slug: str | None = None


class ContinueRequest(BaseModel):
    return_to_village: bool = False
    choice: Literal["rest", "push"] | None = None


class EncounterActionRequest(BaseModel):
    actor_id: UUID
    expected_turn: int = Field(ge=1)
    action: Literal["attack", "guard", "power_strike", "wait"] | None = None
    ability_id: UUID | None = None
    weapon_id: UUID | None = None
    consumable_slug: str | None = Field(default=None, min_length=1, max_length=100)
    target_id: UUID | None = None
    target_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=100)


def get_encounter(db: Session, encounter_id: UUID, lock: bool = False) -> Encounter:
    query = select(Encounter).where(Encounter.id == encounter_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    encounter = db.execute(query).scalar_one_or_none()
    if encounter is None:
        raise HTTPException(404, "Encounter not found.")
    return encounter


def enemy_states(encounter: Encounter) -> list[dict]:
    return [instance.state for instance in encounter.enemy_instances] if encounter.enemy_instances else encounter.enemies


def snapshot(encounter: Encounter) -> dict:
    participants, enemies = prepared_combatants(encounter)
    intents = preview_moves(participants, enemies, encounter.turn, lambda enemy: combat_rng(encounter, enemy)) if encounter.state == 'player_turn' else {}
    living = [p for p in encounter.participants if p["hp"] > 0]
    plan = encounter.quest_run.quest.encounter_pool or []
    index = next((i for i, entry in enumerate(plan) if isinstance(entry, dict) and entry.get("encounter_id") == str(encounter.id)), 0)
    journey = encounter.quest_run.quest.rewards.get("journey", {})
    progress = encounter.quest_run.quest.rewards.get("journey_progress", {})
    group = group_view(encounter.quest_run.quest, plan[index]) if plan else None
    boundary = group is None or group['boundary']
    limited = journey.get("max_rests") is not None
    rests_left = max(0, journey["max_rests"] - progress.get("rests_used", 0)) if limited else None
    return {
        "id": str(encounter.id), "state": encounter.state, "turn": encounter.turn,
        "revision": encounter.updated_at.isoformat(),
        "combat_log": encounter_log(encounter),
        "participants": participants,
        "enemies": [{**enemy, 'next_move': intents.get(enemy['id'])} for enemy in enemies],
        "pending_actor_ids": [p["id"] for p in living if not p["acted"]]
        if encounter.state == "player_turn" else [],
        "quest": {"run_gains": progress.get('run_gains', {}),
                  "raid": {"seed": journey.get('raid_seed'), "period": journey.get('raid_period'),
                           "resets_at": journey.get('raid_resets_at')} if journey.get('raid') else None,
                  "is_boss": bool(plan[index].get('boss')) if plan else False, "group": group, "journey": journey, "rests_remaining": rests_left,
                  "pushes": progress.get("pushes", 0), "requires_choice": limited and boundary,
                  "push_streak": progress.get("push_streak", 0),
                  "xp_bonus_percent": push_xp_bonus(journey, progress.get("push_streak", 0)),
                  "next_push_xp_bonus_percent": push_xp_bonus(journey, progress.get("push_streak", 0) + 1),
                  "risk_bonus": {"gold": progress.get("pushes", 0) * journey.get("push_gold", 0),
                                 "experience": progress.get("pushes", 0) * journey.get("push_experience", 0)}, "stage_description": plan[index].get("description", "") if plan else "",
                  "camp_rest": journey.get("rest") if boundary and encounter.quest_run.status == "awaiting_continue" and (rests_left is None or rests_left > 0) else None,
                  "can_return": (encounter.state == "defeat" or (encounter.state == "victory" and boundary))
                  and (group is None or encounter.quest_run.current_stage in (str(encounter.id), "complete")),
                  "id": str(encounter.quest_run_id), "encounter_number": index + 1,
                  "encounter_count": len(plan) or 1, "status": encounter.quest_run.status,
                  "can_continue": encounter.quest_run.status == "awaiting_continue"
                  and encounter.quest_run.current_stage == str(encounter.id)},
    }


def start_encounter(db: Session, ids: list[UUID], enemy_slug: str = "roadside-bandit", encounter_count: int = 1, *, party: Party | None = None, template_slug: str | None = None, accept_rank_risk: bool = False, contract_id: UUID | None = None) -> dict:
    journey = {}
    plan = [{"encounter_id": str(uuid4()), "enemy_slug": enemy_slug} for _ in range(encounter_count)]
    contract = None
    if contract_id:
        from app.models import RecoveryContract
        contract = db.get(RecoveryContract, contract_id)
        if contract is None:
            raise HTTPException(404, 'Contract not found.')
        plan = [{**deepcopy(entry), 'encounter_id': str(uuid4())} for entry in contract.route]
        journey = deepcopy(contract.rewards.get('journey', {}))
        journey.update(region=contract.region, gold=contract.rewards.get('gold', 10),
                       experience=contract.rewards.get('experience', 0), contract_id=str(contract.id))
    elif template_slug:
        template = db.get(QuestTemplate, template_slug)
        if template is None or not template.journey:
            raise HTTPException(404, "Playable journey not found.")
        plan, journey = build_plan(db, template)
        journey['title'] = template.name
    elif db.get(Enemy, enemy_slug) is None:
        raise HTTPException(404, "Enemy not found.")
    else:
        journey['title'] = db.get(Enemy, enemy_slug).name + (' journey' if encounter_count > 1 else ' encounter')
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "Adventurers must be unique.")
    heroes = list(db.scalars(select(Adventurer).where(Adventurer.id.in_(ids))
                            .order_by(Adventurer.id).with_for_update().execution_options(populate_existing=True)))
    if len(heroes) != len(ids):
        raise HTTPException(404, "Adventurer not found.")
    if any(not h.is_alive or h.health <= 0 for h in heroes):
        raise HTTPException(409, "Only living adventurers can start an encounter.")
    required = journey.get("required_rank", "iron")
    under_rank = validate_rank_entry(db, heroes, required, accept_rank_risk)
    if under_rank:
        journey = {**journey, 'rank_warning_accepted': True, 'under_rank_adventurers': under_rank}
    if active_adventure(db, ids):
        raise HTTPException(409, "An adventurer already has an active encounter.")
    if contract is not None:
        contract = db.scalar(select(RecoveryContract).where(RecoveryContract.id == contract_id)
                             .with_for_update().execution_options(populate_existing=True))
        if contract.status not in ('open', 'in_progress'):
            raise HTTPException(409, 'This contract is already claimed or completed.')

    if party is not None:
        members = {member.adventurer_id: member for member in party.members}
        if any(not members[hero.id].is_ready for hero in heroes):
            raise HTTPException(409, "Every party member must mark ready before departure.")

    owners = list(db.scalars(select(User).where(User.id.in_({h.owner for h in heroes})).order_by(User.id).with_for_update()))
    owner_map = {user.id: user for user in owners}
    for hero in heroes:
        increment(hero, adventures_started=1, encounters_started=1)
        increment_account(owner_map[hero.owner], adventures_started=1, encounters_started=1)

    new_party = party is None
    if new_party:
        party = Party(name="Adventuring party", leader=heroes[0].id)
    quest = Quest(location=journey.get("region", "Forest road"), difficulty=1,
                  rewards={"gold": journey.get("gold", 10), "experience": journey.get("experience", 0), "journey": journey}, encounter_pool=plan)
    db.add_all([party, quest])
    db.flush()
    if new_party:
        db.add_all([PartyMember(party_id=party.id, adventurer_id=h.id) for h in heroes])
    run = QuestRun(party_id=party.id, quest_id=quest.id, current_stage=plan[0]["encounter_id"], status="active")
    db.add(run)
    db.flush()
    if contract is not None:
        from app.contracts import changed
        contract.active_run_id = run.id
        contract.active_run_ids = [*(contract.active_run_ids or []), str(run.id)]
        contract.status = 'in_progress'
        changed(db)
    player_type = db.get(EntityType, "humanoid")
    # Roll once at creation; subsequent actions and reads use the saved instance.
    encounter = Encounter(
        id=UUID(plan[0]["encounter_id"]), quest_run_id=run.id, state="player_turn", turn=1,
        enemy_instances=roll_group(db, plan[0], len(heroes), journey),
        participants=[{"id": str(h.id), "name": h.name, "hp": h.health,
                       "max_hp": derived_stats(h.attributes)["max_hp"], "derived_stats": derived_stats(h.attributes),
                       "attributes": deepcopy(h.attributes), "power": 10, "acted": False, "guarding": False,
                       "equipped_weapon": equipped_weapon(db, h),
                       "weapons": combat_weapons(db, h),
                       "consumables": consumable_inventory(db, h.id),
                       **restore_cooldowns(h.combat_cooldowns),
                       "statuses": deepcopy(h.combat_statuses or []),
                       "entity_type": "humanoid", "status_resistances": deepcopy(player_type.status_resistances) if player_type else {},
                       "power_ready_turn": 1, "equipped_abilities": combat_loadout(db, h)} for h in heroes],
    )
    db.add(encounter)
    db.flush()
    save_enemy_plans(encounter)
    db.add(GameEvent(quest_run_id=run.id, event_type="encounter_started",
                     payload={"encounter_id": str(encounter.id)}))
    if not new_party:
        for member in party.members:
            member.is_ready = False
    notify_run(db, run.id)
    db.commit()
    return snapshot(encounter)


def continue_quest(db: Session, encounter_id: UUID, return_to_village: bool = False, choice: str | None = None) -> dict:
    previous = get_encounter(db, encounter_id, lock=True)
    run = previous.quest_run
    db.refresh(run)
    db.refresh(run.quest)
    if previous.state != "victory" or run.status != "awaiting_continue" or run.current_stage != str(previous.id):
        raise HTTPException(409, "This encounter cannot advance the quest.")
    if return_to_village and choice is not None:
        raise HTTPException(422, "Choose return or a continuation action, not both.")
    journey = run.quest.rewards.get("journey", {})
    grouped = bool(journey.get('encounter_groups'))
    plan = run.quest.encounter_pool
    previous_index = next(i for i, entry in enumerate(plan) if entry['encounter_id'] == str(previous.id))
    boundary = not grouped or plan[previous_index]['group_end']
    if grouped and not boundary and (return_to_village or choice is not None):
        raise HTTPException(409, "Clear every battle in this group before resting, retreating, or pushing deeper.")
    if return_to_village:
        claim_events = claim_group_loot(db, run.quest, previous.participants) if grouped else []
        heroes = list(db.scalars(select(Adventurer).where(Adventurer.id.in_([UUID(p["id"]) for p in previous.participants]))
                                .order_by(Adventurer.id).with_for_update()))
        owners = list(db.scalars(select(User).where(User.id.in_({hero.owner for hero in heroes})).order_by(User.id).with_for_update()))
        owner_map = {user.id: user for user in owners}
        for hero in heroes:
            increment(hero, adventures_completed=1)
            increment_account(owner_map[hero.owner], adventures_completed=1)
        run.status = "returned"
        run.current_stage = "complete"
        from app.contracts import settle_contract
        claim_events.extend(settle_contract(db, run, previous.participants))
        db.add(GameEvent(quest_run_id=run.id, event_type="returned_to_village", payload={"encounter_id": str(previous.id), "messages": claim_events}))
        previous.updated_at = max(datetime.utcnow(), previous.updated_at + timedelta(microseconds=1))
        notify_run(db, run.id)
        db.commit()
        return {**snapshot(previous), "events": claim_events}
    journey = run.quest.rewards.get("journey", {})
    limited = journey.get("max_rests") is not None and boundary
    progress = dict(run.quest.rewards.get("journey_progress", {}))
    if limited and choice is None:
        raise HTTPException(422, "Choose rest or push on at this checkpoint.")
    rest_now = bool(journey.get("rest")) and (choice == "rest" or (choice is None and not limited and not grouped))
    if choice == "rest" and not journey.get("rest"):
        raise HTTPException(409, "This journey has no camp rest.")
    if rest_now and limited:
        if progress.get("rests_used", 0) >= journey["max_rests"]:
            raise HTTPException(409, "No camp rests remain. Press on or return to the village.")
        progress["rests_used"] = progress.get("rests_used", 0) + 1
    if rest_now:
        progress["push_streak"] = 0
    if choice == "push" and limited:
        progress["push_streak"] = progress.get("push_streak", 0) + 1
        progress["pushes"] = progress.get("pushes", 0) + 1
    if limited:
        run.quest.rewards = {**run.quest.rewards, "journey_progress": progress}
        db.add(GameEvent(quest_run_id=run.id, event_type="journey_choice", payload={"after_encounter_id": str(previous.id), "choice": choice, **progress}))
    if grouped and previous_index == len(plan) - 1:
        if not journey.get('repeat_groups', True):
            raise HTTPException(409, "This raid is complete.")
        append_group(run.quest)
    plan = run.quest.encounter_pool
    index = previous_index + 1
    entry = plan[index]
    participants = deepcopy(previous.participants)
    rest_results = []
    for actor in participants:
        actor.pop("evasion", None)
        actor.pop("flinched", None)
        actor.pop("flinch_immune_until", None)
        if rest_now:
            healed = apply_rest(actor, RestRules.model_validate(journey["rest"]))
            rest_results.append({"actor_id": actor["id"], "healed": healed})
        actor["acted"] = False
        actor["guarding"] = False
    # Keep the round clock and cooldown deadlines continuous across fights.
    encounter = Encounter(id=UUID(entry["encounter_id"]), quest_run_id=run.id,
                          state="player_turn", turn=previous.turn + 1,
                          participants=participants,
                          enemy_instances=roll_group(db, entry, sum(p["hp"] > 0 for p in participants), journey))
    db.add(encounter)
    heroes = list(db.scalars(select(Adventurer).where(Adventurer.id.in_([UUID(p["id"]) for p in participants]))
                            .order_by(Adventurer.id).with_for_update()))
    owners = list(db.scalars(select(User).where(User.id.in_({hero.owner for hero in heroes})).order_by(User.id).with_for_update()))
    owner_map = {user.id: user for user in owners}
    for hero in heroes:
        increment(hero, encounters_started=1)
        increment_account(owner_map[hero.owner], encounters_started=1)
    if rest_results:
        for actor in participants:
            hero = db.get(Adventurer, UUID(actor["id"]))
            hero.health = actor["hp"]
            hero.combat_cooldowns = saved_cooldowns(actor, encounter.turn)
            hero.combat_statuses = deepcopy(actor.get("statuses", []))
        db.add(GameEvent(quest_run_id=run.id, event_type="camp_rest", payload={"after_encounter_id": str(previous.id), "results": rest_results}))
    run.status = "active"
    run.current_stage = str(encounter.id)
    db.flush()
    save_enemy_plans(encounter)
    db.add(GameEvent(quest_run_id=run.id, event_type="encounter_started",
                     payload={"encounter_id": str(encounter.id), "encounter_number": index + 1}))
    notify_run(db, run.id)
    db.commit()
    return snapshot(encounter)


def apply_action(db: Session, encounter_id: UUID, request: EncounterActionRequest) -> dict:
    encounter = get_encounter(db, encounter_id, lock=True)
    if encounter.state != "player_turn":
        raise HTTPException(409, "Encounter is complete.")
    if encounter.turn != request.expected_turn:
        raise HTTPException(409, "Turn has changed. Fetch the encounter before acting again.")
    participants, enemies = prepared_combatants(encounter)
    for combatant in participants:
        combatant["team"] = "adventurers"
    for combatant in enemies:
        combatant["team"] = "monsters"
    actor = next((p for p in participants if p["id"] == str(request.actor_id)), None)
    if actor is None or actor["hp"] <= 0:
        raise HTTPException(400, "Actor cannot act in this encounter.")
    if actor["acted"]:
        raise HTTPException(409, "This adventurer already acted this turn.")
    initial_hp = {p["id"]: p["hp"] for p in participants}
    consumable_used = False
    flinch_result = consume_flinch(actor, encounter.turn)
    if flinch_result:
        results = [flinch_result]
    elif request.consumable_slug is not None:
        if request.action is not None or request.ability_id is not None or request.target_ids is not None or request.weapon_id is not None:
            raise HTTPException(422, 'Send a consumable and optional target_id only.')
        # Follow the reward/extraction lock order: heroes, then inventory stacks.
        list(db.scalars(select(Adventurer).where(Adventurer.id.in_([UUID(p['id']) for p in participants]))
                        .order_by(Adventurer.id).with_for_update()))
        owned = db.scalar(select(OwnedConsumable).where(
            OwnedConsumable.adventurer_id == request.actor_id,
            OwnedConsumable.consumable_slug == request.consumable_slug)
            .with_for_update(of=OwnedConsumable).execution_options(populate_existing=True))
        if owned is None or owned.quantity <= 0:
            raise HTTPException(409, 'You do not own that consumable.')
        item = owned.definition
        if item.effect in ('essence', 'orb'):
            raise HTTPException(409, 'Essences and orbs can only be used by their owner in the village.')
        ability = CombatAbility(slug=item.slug, name=item.name, effect=item.effect,
                                damage=item.power, target_type='ally', max_targets=1, scales_with_attributes=False)
        try:
            targets = select_targets(actor, ability, participants + enemies,
                                     [str(request.target_id or request.actor_id)])
            target = targets[0]
            if item.effect == 'heal' and target['hp'] >= target['max_hp']:
                raise InvalidCombatAction('That target is already at full health.')
            if item.effect == 'cleanse' and not target.get('statuses'):
                raise InvalidCombatAction('That target has no statuses to cleanse.')
            results = execute_cast(actor, ability, targets, turn=encounter.turn, rng=combat_rng(encounter, actor))
        except InvalidCombatAction as exc:
            raise HTTPException(400, str(exc)) from exc
        owned.quantity -= 1
        consumable_used = True
        db.flush()
        actor['consumables'] = consumable_inventory(db, request.actor_id)
    elif request.action == "wait":
        if request.ability_id or request.target_id or request.target_ids or request.weapon_id:
            raise HTTPException(422, "Wait does not accept an ability or target.")
        results = [{"actor_id": actor["id"], "ability": "wait", "target_id": actor["id"],
                    "effect": "wait", "amount": 0, "target_hp": actor["hp"], "turn": encounter.turn,
                    "message": f"{actor['name']} waits."}]
    else:
        if request.ability_id is None and request.action is None:
            raise HTTPException(422, 'An ability is required.')
        if request.ability_id is not None and request.action is not None:
            raise HTTPException(422, 'Send ability_id or action, not both.')
        if 'equipped_abilities' not in actor:
            hero = db.get(Adventurer, request.actor_id)
            actor['equipped_abilities'] = combat_loadout(db, hero)
            actor['equipped_weapon'] = equipped_weapon(db, hero)
        actor.setdefault('power', 10)
        # Old snapshots had only UUIDs and names; resolve aliases by stable database slug.
        alias_id = None
        if request.action:
            alias_id = db.scalar(select(Ability.id).where(Ability.slug == request.action))
        spec = next((a for a in actor['equipped_abilities'] if
                     a['slug'] == str(request.ability_id or alias_id)), None)
        if spec is None:
            raise HTTPException(409, 'That ability is not learned.')
        if request.weapon_id is not None:
            weapon = next((w for w in actor.get('weapons', []) if w['id'] == str(request.weapon_id)), None)
            if weapon is None:
                raise HTTPException(409, 'That weapon is not owned by this adventurer.')
            actor['equipped_weapon'] = deepcopy(weapon)
        ability = CombatAbility(**spec)
        if ability.catalog_slug is None:
            catalog = db.get(Ability, UUID(spec['slug']))
            if catalog is not None:
                ability = replace(ability, catalog_slug=catalog.slug)
        if request.action == 'power_strike' and encounter.turn < actor.get('power_ready_turn', 1):
            raise HTTPException(409, 'Power Strike is still on turn cooldown.')
        if request.target_id is not None and request.target_ids is not None:
            raise HTTPException(422, 'Send target_id or target_ids, not both.')
        # Preserve old party-wide snapshots that predate explicit target limits.
        if 'max_targets' not in spec and ability.target_type == 'party':
            ability = replace(ability, max_targets=None)
        try:
            ids = [str(i) for i in request.target_ids] if request.target_ids is not None else None
            targets = select_targets(actor, ability, [*participants, *enemies], ids)
            if request.target_id is not None:
                # Legacy target_id selects the first target; remaining area targets
                # are selected by the server from the same legal pool.
                primary = select_targets(actor, ability, [*participants, *enemies], [str(request.target_id)])[0]
                pool = select_targets(actor, replace(ability, max_targets=None), [*participants, *enemies])
                targets = [primary, *(t for t in pool if t is not primary)]
                if ability.max_targets is not None:
                    targets = targets[:ability.max_targets]
        except InvalidCombatAction as exc:
            raise HTTPException(400, str(exc)) from exc
        try:
            results = execute_cast(actor, ability, targets, turn=encounter.turn, rng=combat_rng(encounter, actor))
        except InvalidCombatAction as exc:
            raise HTTPException(409, str(exc)) from exc
    events = [result["message"] for result in results]
    actor["acted"] = True

    if all(e["hp"] == 0 for e in enemies):
        encounter.state = "victory"
    elif all(p["acted"] for p in participants if p["hp"] > 0):
        # Resolve the enemy phase within this action's transaction, then wait again.
        for enemy in enemies:
            if all(p["hp"] <= 0 for p in participants):
                break
            if enemy["hp"] <= 0:
                continue
            flinch_result = consume_flinch(enemy, encounter.turn)
            if flinch_result:
                results.append(flinch_result)
                events.append(flinch_result['message'])
                continue
            try:
                enemy_results = execute_planned(enemy, participants, enemies, encounter.turn, combat_rng(encounter, enemy))
            except InvalidCombatAction as exc:
                raise HTTPException(409, str(exc)) from exc
            results.extend(enemy_results)
            events.extend(result['message'] for result in enemy_results)
        periodic_results = tick_statuses([*participants, *enemies], turn=encounter.turn)
        results.extend(periodic_results)
        events.extend(result['message'] for result in periodic_results)
        if all(p["hp"] == 0 for p in participants):
            encounter.state = "defeat"
        elif all(e["hp"] == 0 for e in enemies):
            encounter.state = "victory"
        else:
            encounter.turn += 1
            for participant in participants:
                participant["acted"] = False
                participant["guarding"] = False

    encounter.participants = participants
    if encounter.enemy_instances:
        for instance, state in zip(encounter.enemy_instances, enemies, strict=True):
            instance.state = state
    else:
        encounter.enemies = enemies
    if encounter.state == 'player_turn':
        save_enemy_plans(encounter)
    journey = encounter.quest_run.quest.rewards.get("journey", {})
    progress = snapshot(encounter)["quest"]
    grouped = bool(journey.get('encounter_groups'))
    last_fight = progress["encounter_number"] == (journey['original_encounter_count'] if grouped else progress["encounter_count"])
    journey_progress = encounter.quest_run.quest.rewards.get("journey_progress", {})
    victory_gold = encounter.quest_run.quest.rewards.get("gold", 10) + ((journey.get("completion_gold", 0) + (0 if grouped else journey_progress.get("pushes", 0) * journey.get("push_gold", 0))) if last_fight else 0)
    victory_experience = boosted_encounter_xp(encounter.quest_run.quest.rewards.get("experience", 0), journey, journey_progress) + ((journey.get("completion_experience", 0) + (0 if grouped else journey_progress.get("pushes", 0) * journey.get("push_experience", 0))) if last_fight else 0)
    reward_heroes = {str(h.id): h for h in db.scalars(select(Adventurer)
        .where(Adventurer.id.in_([UUID(p["id"]) for p in participants]))
        .order_by(Adventurer.id).with_for_update().execution_options(populate_existing=True))}
    owners = list(db.scalars(select(User).where(User.id.in_({h.owner for h in reward_heroes.values()}))
                            .order_by(User.id).with_for_update()))
    owner_map = {user.id: user for user in owners}
    participant_ids = set(reward_heroes)
    enemy_ids = {enemy["id"] for enemy in enemies}
    for result in results:
        source = result.get("actor_id")
        target = result.get("target_id")
        if source not in participant_ids:
            continue
        hero = reward_heroes[source]
        account = owner_map[hero.owner]
        if target in enemy_ids and result.get("effect") in ("damage", "damage_over_time"):
            amount = result.get("amount", 0)
            if amount:
                increment(hero, damage_dealt=amount)
                increment_account(account, damage_dealt=amount)
            if result.get("target_hp") == 0:
                rank = rank_for(db, hero.level)
                hero.statistics = record_kill(hero.statistics, rank.slug, rank.min_level)
                account.statistics = record_kill(account.statistics, rank.slug, rank.min_level)
                account.statistics["enemies_killed_lifetime"] = account.statistics.get("enemies_killed_lifetime", 0) + 1
    for result in results:
        target = result.get("target_id")
        if target in participant_ids and result.get("target_hp") == 0 and initial_hp.get(target, 0) > 0:
            victim = reward_heroes[target]
            increment(victim, times_killed=1)
            increment_account(owner_map[victim.owner], adventurers_killed=1)
    if consumable_used:
        increment(reward_heroes[str(request.actor_id)], consumables_used=1)
        increment_account(owner_map[reward_heroes[str(request.actor_id)].owner], consumables_used=1)
    for participant in participants:
        hero = reward_heroes[participant["id"]]
        hero.combat_cooldowns = saved_cooldowns(participant, encounter.turn, encounter.state in ("victory", "defeat"))
        hero.health = participant["hp"]
        hero.combat_statuses = deepcopy(participant.get("statuses", []))
        rescue = journey.get('death_policy') == 'rescue_on_return'
        hero.is_alive = hero.health > 0 or (rescue and encounter.state != 'defeat')
        if encounter.state == "victory" and hero.health > 0:
            if grouped and (rescue or journey.get('bank_rewards')):
                bank_run_gains(encounter.quest_run.quest, hero.id, victory_gold, victory_experience)
            else:
                hero.gold += victory_gold
                events.extend(award_experience(db, hero, victory_experience))
    if encounter.state in ("victory", "defeat"):
        progress = snapshot(encounter)["quest"]
        if grouped:
            quest = encounter.quest_run.quest
            if encounter.state == 'victory' and progress['group']['boundary']:
                events.append(earn_group_loot(quest))
            elif encounter.state == 'defeat':
                saved_progress = deepcopy(quest.rewards.get('journey_progress', {}))
                saved_progress['lost_loot'] = saved_progress.pop('loot_stash', [])
                saved_progress['lost_run_gains'] = saved_progress.pop('run_gains', {})
                quest.rewards = {**quest.rewards, 'journey_progress': saved_progress}
                events.append('The expedition stash and unclaimed run gains were lost.' if journey.get('death_policy') == 'rescue_on_return' or journey.get('bank_rewards') else 'The expedition stash was lost. Earned battle XP and gold are kept.')
        more = encounter.state == "victory" and (grouped and journey.get("repeat_groups", True) or progress["encounter_number"] < progress["encounter_count"])
        if grouped and encounter.state == 'victory' and not more:
            events.extend(claim_group_loot(db, encounter.quest_run.quest, participants))
        for hero in reward_heroes.values():
            owner = owner_map[hero.owner]
            if encounter.state == "victory":
                increment(hero, encounters_completed=1)
                increment_account(owner, encounters_completed=1)
            elif encounter.state == "defeat":
                increment(hero, adventures_defeated=1)
                increment_account(owner, adventures_defeated=1)
            if encounter.state == "victory" and not more:
                increment(hero, adventures_completed=1)
                increment_account(owner, adventures_completed=1)
        encounter.quest_run.status = "awaiting_continue" if more else encounter.state
        encounter.quest_run.current_stage = str(encounter.id) if more else "complete"
        reward_verb = "banks" if grouped and (journey.get("death_policy") == "rescue_on_return" or journey.get("bank_rewards")) else "earns"
        events.append(f"Victory: each survivor {reward_verb} {victory_gold} gold and {victory_experience} experience." if encounter.state == "victory" else "The party was defeated. Unclaimed completion bonuses are lost.")
    if encounter.state == 'defeat':
        from app.contracts import post_defeat, settle_contract
        post_defeat(db, encounter)
        events.extend(settle_contract(db, encounter.quest_run, participants))
    elif encounter.state == 'victory' and encounter.quest_run.status == 'victory':
        from app.contracts import settle_contract
        events.extend(settle_contract(db, encounter.quest_run, participants, completed=True))
    db.add(GameEvent(quest_run_id=encounter.quest_run_id, event_type="combat_action",
                     payload={"encounter_id": str(encounter.id), "turn": request.expected_turn,
                              "actor_id": str(request.actor_id), "action": request.action or str(request.ability_id),
                              "messages": events, "action_results": results}))
    encounter.updated_at = max(datetime.utcnow(), encounter.updated_at + timedelta(microseconds=1))
    notify_run(db, encounter.quest_run_id)
    db.commit()
    return {**snapshot(encounter), "events": events, "action_results": results}


def encounter_log(encounter):
    from sqlalchemy.orm import object_session
    db = object_session(encounter)
    if db is None:
        return []
    entries = db.scalars(select(GameEvent).where(GameEvent.quest_run_id == encounter.quest_run_id)
                        .order_by(GameEvent.created_at, GameEvent.id)).all()
    return [{'id': str(entry.id), 'turn': entry.payload.get('turn'), 'messages': entry.payload['messages']}
            for entry in entries if entry.payload.get('encounter_id') == str(encounter.id)
            and entry.payload.get('messages')]


def prepared_combatants(encounter, planning_time=None):
    from sqlalchemy.orm import object_session
    participants, enemies = deepcopy(encounter.participants), deepcopy(enemy_states(encounter))
    db = object_session(encounter)
    for actor in participants:
        if 'weapons' not in actor and db is not None:
            hero = db.get(Adventurer, UUID(actor['id']))
            actor['weapons'] = combat_weapons(db, hero)
            if 'equipped_weapon' not in actor:
                actor['equipped_weapon'] = equipped_weapon(db, hero)
            # Retain saved definitions and damage when upgrading an ongoing fight.
            saved_weapon = actor.get('equipped_weapon')
            if saved_weapon:
                actor['weapons'] = [saved_weapon if w['id'] == saved_weapon.get('id') else w
                                    for w in actor['weapons']]
            saved = {a['slug']: a for a in actor.get('equipped_abilities', [])}
            actor['equipped_abilities'] = [saved.get(a['slug'], a) for a in combat_loadout(db, hero)]
    for enemy in enemies:
        if 'abilities' not in enemy and db is not None:
            enemy['abilities'] = enemy_loadout(db, enemy.get('enemy_slug'))
            enemy['equipped_weapon'] = enemy_weapon(db, enemy.get('enemy_slug'), enemy['power'])
    # Old encounters receive the same deterministic fallback on reads and actions.
    deadline = planning_time if planning_time is not None else (encounter.updated_at - datetime(1970, 1, 1)).total_seconds()
    plan_moves(participants, enemies, encounter.turn, lambda enemy: seeded_rng(encounter, enemy, 'intent'), deadline)
    return participants, enemies


def save_enemy_plans(encounter):
    _, enemies = prepared_combatants(encounter, time())
    if encounter.enemy_instances:
        for instance, state in zip(encounter.enemy_instances, enemies, strict=True):
            instance.state = state
    else:
        encounter.enemies = enemies


def combat_rng(encounter, actor):
    return seeded_rng(encounter, actor, 'combat')


def seeded_rng(encounter, actor, stream):
    """Stable action streams, independent of loot and enemy ability selection."""
    journey = encounter.quest_run.quest.rewards.get('journey', {})
    seed = journey.get('raid_seed') or str(encounter.id)
    plan = encounter.quest_run.quest.encounter_pool or []
    stage = next((i for i, entry in enumerate(plan) if entry['encounter_id'] == str(encounter.id)), 0)
    roster = [*encounter.participants, *enemy_states(encounter)]
    position = next(i for i, entry in enumerate(roster) if entry['id'] == actor['id'])
    return Random(f'{seed}:{stream}:{stage}:{encounter.turn}:{position}')
