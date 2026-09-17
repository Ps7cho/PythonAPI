from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import GameEvent, GameState


class InvalidStateVersionError(Exception):
    def __init__(self, expected: int, actual: int):
        self.expected = expected
        self.actual = actual


class GameService:
    def __init__(self, db: Session):
        self.db = db

    def get_or_create_state(self, user_id: str) -> GameState:
        state = self.db.execute(
            select(GameState).where(GameState.user_id == user_id).order_by(GameState.version.desc())
        ).scalars().first()

        if state is None:
            state = GameState(user_id=user_id, payload={"hp": 100, "inventory": [], "location": "spawn", "xp": 0})
            self.db.add(state)
            self.db.commit()
            self.db.refresh(state)
        return state

    def apply_event(self, user_id: str, event_type: str, payload: Dict[str, Any], expected_version: Optional[int] = None) -> GameState:
        state = self.get_or_create_state(user_id)

        if expected_version is not None and state.version != expected_version:
            raise InvalidStateVersionError(expected=expected_version, actual=state.version)

        new_state = self._reduce_state(state.payload, event_type, payload)

        state.payload = new_state
        state.version += 1
        state.updated_at = datetime.utcnow()

        event = GameEvent(
            state_id=state.id,
            event_type=event_type,
            payload=payload,
            applied=True,
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(state)

        return state

    def get_state(self, user_id: str) -> GameState:
        state = self.db.execute(
            select(GameState).where(GameState.user_id == user_id).order_by(GameState.version.desc())
        ).scalars().first()
        if state is None:
            raise ValueError(f"No state found for user: {user_id}")
        return state

    def get_event_history(self, user_id: str):
        state = self.get_state(user_id)
        return self.db.execute(
            select(GameEvent).where(GameEvent.state_id == state.id).order_by(GameEvent.created_at.asc())
        ).scalars().all()

    def _reduce_state(self, current: Dict[str, Any], event_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        next_state = dict(current)

        if event_type == "move":
            location = payload.get("location")
            if location:
                next_state["location"] = location
        elif event_type == "damage":
            damage = int(payload.get("amount", 0))
            next_state["hp"] = max(0, int(next_state.get("hp", 0)) - damage)
        elif event_type == "heal":
            heal_amount = int(payload.get("amount", 0))
            next_state["hp"] = min(100, int(next_state.get("hp", 0)) + heal_amount)
        elif event_type == "pickup_item":
            item_name = payload.get("item")
            if item_name:
                next_state.setdefault("inventory", [])
                next_state["inventory"].append(item_name)
        elif event_type == "award_xp":
            xp_gain = int(payload.get("amount", 0))
            next_state["xp"] = int(next_state.get("xp", 0)) + xp_gain
        else:
            next_state["last_event"] = {"type": event_type, "payload": payload}

        return next_state
