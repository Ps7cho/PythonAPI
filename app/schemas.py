from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from pydantic import BaseModel, Field


class EventPayload(BaseModel):
    type: str = Field(..., description="Game event type such as move, attack, inventory_change")
    data: Dict[str, Any] = Field(default_factory=dict)


class UserCreate(BaseModel):
    email: str
    username: str
    account_info: Dict[str, Any] = Field(default_factory=dict)


class UserRead(BaseModel):
    id: UUID
    email: str
    username: str
    account_info: Dict[str, Any]
    statistics: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AdventurerCreate(BaseModel):
    name: str
    level: int = 1
    attributes: Dict[str, Any] = Field(default_factory=dict)
    health: int = 100
    experience: int = 0
    owner: UUID


class AbilityCreate(BaseModel):
    name: str
    description: str = ""
    ability_type: str = "attack"
    cooldown_type: str = "turn"
    cooldown_value: int = 0
    cost_type: str = "Stamina"
    cost_value: int = 0
    target_type: str = "enemy"
    max_targets: int | None = Field(default=1, ge=1)
    effect_type: str = "damage"
    power: int = 10
    requires_weapon: bool = False
    allowed_weapon_tags: List[str] = Field(default_factory=list)
    damage_multiplier: float | None = Field(default=None, ge=0)


class AbilityRead(BaseModel):
    id: UUID
    name: str
    description: str
    ability_type: str
    cooldown_type: str
    cooldown_value: int
    cost_type: str
    cost_value: int
    target_type: str
    max_targets: int | None
    effect_type: str
    power: int
    requires_weapon: bool
    allowed_weapon_tags: List[str]
    damage_multiplier: float | None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AdventurerRead(BaseModel):
    id: UUID
    name: str
    level: int
    attributes: Dict[str, Any]
    health: int
    experience: int
    owner: UUID
    statistics: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    ability_inventory: List[Dict[str, Any]] = Field(default_factory=list)

    class Config:
        from_attributes = True


class AdventurerAbilityRead(BaseModel):
    id: UUID
    adventurer_id: UUID
    ability_id: UUID
    unlocked: bool
    learned_at: datetime

    class Config:
        from_attributes = True


class AbilityUseEvent(BaseModel):
    type: str
    actor_id: Optional[UUID] = None
    ability: Optional[str] = None
    source_id: Optional[UUID] = None
    target_id: Optional[UUID] = None
    amount: Optional[int] = None
    ability_id: Optional[UUID] = None
    remaining_turns: Optional[int] = None
    cooldown_seconds: Optional[int] = None
    cost_type: Optional[str] = None
    cost_value: Optional[int] = None


class AbilityUseResult(BaseModel):
    success: bool
    events: List[AbilityUseEvent]


class PartyCreate(BaseModel):
    name: str
    leader: UUID
    members: List[UUID] = Field(default_factory=list)


class PartyRead(BaseModel):
    id: UUID
    name: str
    leader: UUID
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class QuestCreate(BaseModel):
    difficulty: int
    location: str
    rewards: Dict[str, Any] = Field(default_factory=dict)
    encounter_pool: List[Dict[str, Any]] = Field(default_factory=list)


class QuestRead(BaseModel):
    id: UUID
    difficulty: int
    location: str
    rewards: Dict[str, Any]
    encounter_pool: List[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class QuestRunCreate(BaseModel):
    party_id: UUID
    quest_id: UUID
    current_stage: str = "queued"
    status: str = "planned"


class QuestRunRead(BaseModel):
    id: UUID
    party_id: UUID
    quest_id: UUID
    current_stage: str
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EncounterCreate(BaseModel):
    quest_run_id: UUID
    enemies: List[Dict[str, Any]] = Field(default_factory=list)
    participants: List[Dict[str, Any]] = Field(default_factory=list)
    turn: int = 1
    state: str = "init"


class EncounterRead(BaseModel):
    id: UUID
    quest_run_id: UUID
    enemies: List[Dict[str, Any]]
    participants: List[Dict[str, Any]]
    turn: int
    state: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ItemCreate(BaseModel):
    type: str
    modifiers: Dict[str, Any] = Field(default_factory=dict)
    rarity: str = "common"


class ItemRead(BaseModel):
    id: UUID
    type: str
    modifiers: Dict[str, Any]
    rarity: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class InventoryCreate(BaseModel):
    adventurer_id: UUID
    items: List[Dict[str, Any]] = Field(default_factory=list)


class InventoryRead(BaseModel):
    id: UUID
    adventurer_id: UUID
    items: List[Dict[str, Any]]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class GameStateCreate(BaseModel):
    user_id: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class GameStateRead(BaseModel):
    id: UUID
    user_id: str
    version: int
    payload: Dict[str, Any]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class GameEventRead(BaseModel):
    id: UUID
    state_id: Optional[UUID] = None
    quest_run_id: Optional[UUID] = None
    event_type: str
    payload: Dict[str, Any]
    timestamp: datetime
    created_at: datetime
    applied: bool

    class Config:
        from_attributes = True


class ApplyEventRequest(BaseModel):
    event_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)


class StateVersionResult(BaseModel):
    state_id: UUID
    user_id: str
    version: int
    payload: Dict[str, Any]
    event_count: int
    last_event_type: Optional[str] = None
    updated_at: datetime
