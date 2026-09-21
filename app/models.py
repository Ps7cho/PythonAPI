import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Boolean, Column, DateTime, ForeignKey, Float, Integer, JSON, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from app.database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    username = Column(String, nullable=False, index=True)
    account_type = Column(String(20), nullable=False, default="player", server_default="player")
    account_info = Column(JSON, nullable=False, default=dict)
    statistics = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    adventurers = relationship("Adventurer", back_populates="owner_user", cascade="all, delete-orphan")


class LoginAccount(Base):
    __tablename__ = "login_accounts"

    username = Column(String(32), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    failed_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime, nullable=True)


class LoginSession(Base):
    __tablename__ = "login_sessions"

    token_hash = Column(String(64), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)


class DiscordIdentity(Base):
    __tablename__ = "discord_identities"

    discord_id = Column(String(32), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True, index=True)
    username = Column(String(120), nullable=True)
    email = Column(String(320), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User")


class DiscordOAuthState(Base):
    __tablename__ = "discord_oauth_states"

    state = Column(String(128), primary_key=True)
    purpose = Column(String(20), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True)
    expires_at = Column(DateTime, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Adventurer(Base):
    __tablename__ = "adventurers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    level = Column(Integer, nullable=False, default=1)
    attributes = Column(JSON, nullable=False, default=dict)
    health = Column(Integer, nullable=False, default=100)
    combat_cooldowns = Column(JSON, nullable=False, default=dict)
    combat_statuses = Column(JSON, nullable=False, default=list)
    experience = Column(Integer, nullable=False, default=0)
    attribute_points = Column(Integer, nullable=False, default=0)
    gold = Column(Integer, nullable=False, default=0)
    statistics = Column(JSON, nullable=False, default=dict)
    is_alive = Column(Boolean, nullable=False, default=True)
    owner = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    owner_user = relationship("User", back_populates="adventurers")
    inventory = relationship("Inventory", back_populates="adventurer", uselist=False, cascade="all, delete-orphan")
    ability_inventory = relationship("AdventurerAbility", back_populates="adventurer", cascade="all, delete-orphan")


class StatusEffect(Base):
    __tablename__ = "status_effects"
    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    damage = Column(Integer, nullable=False)
    duration = Column(Integer, nullable=False)
    max_stacks = Column(Integer, nullable=False, default=1)
    rules = Column(JSON, nullable=False, default=dict)
    __table_args__ = (CheckConstraint("damage > 0 AND duration > 0 AND max_stacks > 0", name="ck_status_positive"),)


class EntityType(Base):
    __tablename__ = "entity_types"
    slug = Column(String, primary_key=True)
    # Percentage reduction per status slug: 100 is immune.
    status_resistances = Column(JSON, nullable=False, default=dict)


class AbilityArchetype(Base):
    __tablename__ = 'ability_archetypes'

    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(String, nullable=False, default='')
    definition = Column(JSON, nullable=False, default=dict)


class Ability(Base):
    __tablename__ = "abilities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False, unique=True, index=True)
    slug = Column(String, nullable=False, unique=True, default=lambda: uuid.uuid4().hex)
    status_effect_slug = Column(String, ForeignKey("status_effects.slug"), nullable=True, index=True)
    status_effect = relationship("StatusEffect", lazy="joined")
    affliction_ops = Column(JSON, nullable=False, default=list)
    archetype_slug = Column(String, ForeignKey('ability_archetypes.slug'), nullable=True)
    effect_chain = Column(JSON, nullable=False, default=list)
    rank_upgrades = Column(JSON, nullable=False, default=dict)
    duration_turns = Column(Integer, nullable=False, default=lambda context: 2 if context.get_current_parameters().get('effect_type') == 'evade' else 3)
    guard_percent = Column(Integer, nullable=False, default=60)
    damage_multiplier = Column(Float, nullable=True)
    requires_weapon = Column(Boolean, nullable=False, default=False)
    allowed_weapon_tags = Column(JSON, nullable=False, default=list)
    starter = Column(Boolean, nullable=False, default=False)
    loadout_order = Column(Integer, nullable=True)
    description = Column(String, nullable=False, default="")
    ability_type = Column(String, nullable=False, default="attack")
    cooldown_type = Column(String, nullable=False, default="turn")
    cooldown_value = Column(Integer, nullable=False, default=0)
    cost_type = Column(String, nullable=False, default="Stamina")
    cost_value = Column(Integer, nullable=False, default=0)
    target_type = Column(String, nullable=False, default="enemy")
    max_targets = Column(Integer().evaluates_none(), nullable=True, default=1)
    effect_type = Column(String, nullable=False, default="damage")
    power = Column(Integer, nullable=False, default=10)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class AdventurerAbility(Base):
    __tablename__ = "adventurer_abilities"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey("adventurers.id"), nullable=False, index=True)
    ability_id = Column(UUID(as_uuid=True), ForeignKey("abilities.id"), nullable=False, index=True)
    unlocked = Column(Boolean, nullable=False, default=True)
    learned_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    adventurer = relationship("Adventurer", back_populates="ability_inventory")
    ability = relationship("Ability")

    __table_args__ = (UniqueConstraint("adventurer_id", "ability_id", name="uq_adventurer_ability"),)


class EquippedAbility(Base):
    __tablename__ = "equipped_abilities"

    adventurer_id = Column(UUID(as_uuid=True), ForeignKey("adventurers.id"), primary_key=True)
    slot = Column(Integer, primary_key=True)
    ability_id = Column(UUID(as_uuid=True), ForeignKey("abilities.id"), nullable=False)
    __table_args__ = (UniqueConstraint("adventurer_id", "ability_id", name="uq_equipped_ability"),)


class Party(Base):
    __tablename__ = "parties"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    leader = Column(UUID(as_uuid=True), ForeignKey("adventurers.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    leader_adventurer = relationship("Adventurer", foreign_keys=[leader])
    members = relationship("PartyMember", back_populates="party", cascade="all, delete-orphan")


class PartyPlan(Base):
    __tablename__ = 'party_plans'

    party_id = Column(UUID(as_uuid=True), ForeignKey('parties.id', ondelete='CASCADE'), primary_key=True)
    revision = Column(UUID(as_uuid=True), nullable=False, default=uuid.uuid4)
    selection = Column(JSON, nullable=False)


class PartyMember(Base):
    __tablename__ = "party_members"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    party_id = Column(UUID(as_uuid=True), ForeignKey("parties.id"), nullable=False, index=True)
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey("adventurers.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    is_ready = Column(Boolean, nullable=False, default=False)

    party = relationship("Party", back_populates="members")
    adventurer = relationship("Adventurer", foreign_keys=[adventurer_id])

    __table_args__ = (UniqueConstraint("party_id", "adventurer_id", name="uq_party_member"),)


class QuestTemplate(Base):
    __tablename__ = "quest_templates"

    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    difficulty = Column(Integer, nullable=False)
    min_encounters = Column(Integer, nullable=False)
    max_encounters = Column(Integer, nullable=False)
    region = Column(String, nullable=False)
    enemy_pool = Column(JSON, nullable=False, default=list)
    possible_rewards = Column(JSON, nullable=False, default=list)
    journey = Column(JSON, nullable=False, default=dict)


class Quest(Base):
    __tablename__ = "quests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    difficulty = Column(Integer, nullable=False, default=1)
    location = Column(String, nullable=False)
    rewards = Column(JSON, nullable=False, default=dict)
    encounter_pool = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class RecoveryContract(Base):
    __tablename__ = 'recovery_contracts'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_run_id = Column(UUID(as_uuid=True), ForeignKey('quest_runs.id'), nullable=False, unique=True)
    active_run_id = Column(UUID(as_uuid=True), ForeignKey('quest_runs.id'), unique=True)
    active_run_ids = Column(JSON, nullable=False, default=list)
    status = Column(String, nullable=False, default='open', index=True)
    title = Column(String, nullable=False)
    region = Column(String, nullable=False)
    fallen = Column(JSON, nullable=False)
    route = Column(JSON, nullable=False)
    rewards = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class RecoveredSoul(Base):
    __tablename__ = 'recovered_souls'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    contract_id = Column(UUID(as_uuid=True), ForeignKey('recovery_contracts.id'), nullable=False)
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey('adventurers.id'), nullable=False, index=True)
    fallen_id = Column(UUID(as_uuid=True), ForeignKey('adventurers.id'), nullable=False)
    profile = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint('contract_id', 'fallen_id'),)


class QuestRun(Base):
    __tablename__ = "quest_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    party_id = Column(UUID(as_uuid=True), ForeignKey("parties.id"), nullable=False, index=True)
    quest_id = Column(UUID(as_uuid=True), ForeignKey("quests.id"), nullable=False, index=True)
    current_stage = Column(String, nullable=False, default="queued")
    status = Column(String, nullable=False, default="planned")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    party = relationship("Party", foreign_keys=[party_id])
    quest = relationship("Quest", foreign_keys=[quest_id])
    encounters = relationship("Encounter", back_populates="quest_run", cascade="all, delete-orphan")
    events = relationship("GameEvent", back_populates="quest_run", cascade="all, delete-orphan")


class Enemy(Base):
    __tablename__ = "enemies"

    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    enemy_type = Column(String, nullable=False)
    type_profile = relationship("EntityType", primaryjoin="foreign(Enemy.enemy_type) == EntityType.slug", lazy="joined", viewonly=True)
    attributes = Column(JSON, nullable=False, default=dict)
    stat_ranges = Column(JSON, nullable=False, default=dict)


class EnemyAbility(Base):
    __tablename__ = "enemy_abilities"

    enemy_slug = Column(String, ForeignKey("enemies.slug"), primary_key=True)
    ability_id = Column(UUID(as_uuid=True), ForeignKey("abilities.id"), primary_key=True)
    weight = Column(Integer, nullable=False, default=1)
    priority = Column(Integer, nullable=False, default=0)
    ability = relationship("Ability")


class EncounterEnemy(Base):
    __tablename__ = "encounter_enemies"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    encounter_id = Column(UUID(as_uuid=True), ForeignKey("encounters.id", ondelete="CASCADE"), nullable=False, index=True)
    enemy_slug = Column(String, ForeignKey("enemies.slug", ondelete="RESTRICT"), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    state = Column(JSON, nullable=False)

    enemy = relationship("Enemy")
    encounter = relationship("Encounter", back_populates="enemy_instances")
    __table_args__ = (UniqueConstraint("encounter_id", "position", name="uq_encounter_enemy_position"),)


class Encounter(Base):
    __tablename__ = "encounters"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quest_run_id = Column(UUID(as_uuid=True), ForeignKey("quest_runs.id"), nullable=False, index=True)
    enemies = Column(JSON, nullable=False, default=list)
    participants = Column(JSON, nullable=False, default=list)
    turn = Column(Integer, nullable=False, default=1)
    state = Column(String, nullable=False, default="init")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    quest_run = relationship("QuestRun", back_populates="encounters")
    enemy_instances = relationship("EncounterEnemy", back_populates="encounter",
                                   cascade="all, delete-orphan", order_by="EncounterEnemy.position")


class LootType(Base):
    __tablename__ = "loot_types"

    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)


class Item(Base):
    __tablename__ = "items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    type = Column(String, nullable=False)
    modifiers = Column(JSON, nullable=False, default=dict)
    rarity = Column(String, nullable=False, default="common")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Inventory(Base):
    __tablename__ = "inventories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey("adventurers.id"), nullable=False, unique=True, index=True)
    items = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    adventurer = relationship("Adventurer", back_populates="inventory")


class GameEvent(Base):
    __tablename__ = "game_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    quest_run_id = Column(UUID(as_uuid=True), ForeignKey("quest_runs.id"), nullable=True, index=True)
    event_type = Column(String, nullable=False, index=True)
    payload = Column(JSON, nullable=False, default=dict)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    applied = Column(Boolean, default=True, nullable=False)

    quest_run = relationship("QuestRun", back_populates="events")


class WeaponType(Base):
    __tablename__ = "weapon_types"
    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    tags = Column(JSON, nullable=False, default=list)


class WeaponDefinition(Base):
    """Reusable weapon blueprint which can be assigned to quest loot tables."""
    __tablename__ = "weapon_definitions"
    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    weapon_type_slug = Column(String, ForeignKey("weapon_types.slug"), nullable=False)
    base_damage = Column(Integer, nullable=False)
    required_rank = Column(String, ForeignKey("rank_definitions.slug"), nullable=False, default="iron")
    weapon_type = relationship("WeaponType")


class Village(Base):
    __tablename__ = "villages"
    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    region = Column(String, nullable=False)
    description = Column(String, nullable=False, default="")


class Shop(Base):
    __tablename__ = "shops"
    slug = Column(String, primary_key=True)
    village_slug = Column(String, ForeignKey("villages.slug"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=False, default="")
    village = relationship("Village")


class ShopTable(Base):
    __tablename__ = "shop_tables"
    slug = Column(String, primary_key=True)
    shop_slug = Column(String, ForeignKey("shops.slug"), nullable=False, index=True)
    category = Column(String, nullable=False)
    items = Column(JSON, nullable=False, default=list)
    shop = relationship("Shop")


class Weapon(Base):
    required_rank = Column(String, ForeignKey("rank_definitions.slug"), nullable=False, default="iron")
    __tablename__ = "weapons"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey("adventurers.id"), nullable=False, index=True)
    weapon_type_slug = Column(String, ForeignKey("weapon_types.slug"), nullable=False)
    name = Column(String, nullable=False)
    base_damage = Column(Integer, nullable=False)
    weapon_type = relationship("WeaponType")


class EquippedWeapon(Base):
    __tablename__ = "equipped_weapons"
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey("adventurers.id"), primary_key=True)
    weapon_id = Column(UUID(as_uuid=True), ForeignKey("weapons.id"), nullable=False, unique=True)
    weapon = relationship("Weapon")


class EnemyWeapon(Base):
    __tablename__ = "enemy_weapons"
    enemy_slug = Column(String, ForeignKey("enemies.slug"), primary_key=True)
    weapon_type_slug = Column(String, ForeignKey("weapon_types.slug"), nullable=False)
    weapon_type = relationship("WeaponType")


class PartyInvite(Base):
    __tablename__ = "party_invites"
    party_id = Column(UUID(as_uuid=True), ForeignKey("parties.id"), primary_key=True)
    code_hash = Column(String(64), nullable=False, unique=True)
    expires_at = Column(DateTime, nullable=False)


class RestPolicy(Base):
    __tablename__ = "rest_policies"
    slug = Column(String, primary_key=True)
    settings = Column(JSON, nullable=False)


class RankDefinition(Base):
    __tablename__ = "rank_definitions"
    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    min_level = Column(Integer, nullable=False, unique=True)
    ability_slots = Column(Integer, nullable=False)
    passive_slots = Column(Integer, nullable=False)
    unlocks = Column(JSON, nullable=False, default=list)
    equipment_reward = Column(JSON, nullable=True)


class RaidRotation(Base):
    __tablename__ = "raid_rotations"
    key = Column(String, primary_key=True)
    seed = Column(String, nullable=False)
    resets_at = Column(DateTime, nullable=False)
    snapshot = Column(JSON, nullable=False)


class Consumable(Base):
    __tablename__ = "consumables"
    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=False)
    effect = Column(String, nullable=False)
    power = Column(Integer, nullable=False, default=0)
    __table_args__ = (CheckConstraint("effect IN ('heal', 'buff', 'cleanse', 'essence', 'orb') AND power >= 0 AND power <= 100", name="ck_consumable_effect"),)


class OwnedConsumable(Base):
    __tablename__ = "owned_consumables"
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey("adventurers.id"), primary_key=True)
    consumable_slug = Column(String, ForeignKey("consumables.slug"), primary_key=True, index=True)
    quantity = Column(Integer, nullable=False, default=0)
    definition = relationship("Consumable", lazy="joined")
    __table_args__ = (CheckConstraint("quantity >= 0", name="ck_consumable_quantity"),)


class EssenceDefinition(Base):
    __tablename__ = "essence_definitions"
    consumable_slug = Column(String, ForeignKey("consumables.slug"), primary_key=True)
    active = Column(Boolean, nullable=False, default=True, server_default="true")
    powers = Column(JSON, nullable=False, default=dict)
    consumable = relationship("Consumable", lazy="joined")


class AbsorbedEssence(Base):
    __tablename__ = "absorbed_essences"
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey("adventurers.id"), primary_key=True)
    slot = Column(Integer, primary_key=True)
    essence_slug = Column(String, ForeignKey("essence_definitions.consumable_slug"), nullable=False, index=True)
    absorbed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    definition = relationship("EssenceDefinition", lazy="joined")
    __table_args__ = (
        CheckConstraint("slot >= 1 AND slot <= 3", name="ck_essence_slot"),
        UniqueConstraint("adventurer_id", "essence_slug", name="uq_absorbed_essence"),
    )


class OrbOutcome(Base):
    __tablename__ = "orb_outcomes"
    orb_slug = Column(String, ForeignKey("consumables.slug"), primary_key=True)
    essence_slug = Column(String, ForeignKey("essence_definitions.consumable_slug"), primary_key=True, index=True)
    ability_id = Column(UUID(as_uuid=True), ForeignKey("abilities.id"), nullable=False, index=True)
    ability = relationship("Ability", lazy="joined")


class AuctionListing(Base):
    __tablename__ = 'auction_listings'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    seller_id = Column(UUID(as_uuid=True), ForeignKey('adventurers.id'), nullable=False, index=True)
    bidder_id = Column(UUID(as_uuid=True), ForeignKey('adventurers.id'), nullable=True)
    buyer_id = Column(UUID(as_uuid=True), ForeignKey('adventurers.id'), nullable=True)
    mode = Column(String, nullable=False)
    status = Column(String, nullable=False, default='open', index=True)
    item = Column(JSON, nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Integer, nullable=False)
    bid = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    closed_at = Column(DateTime, nullable=True)
    __table_args__ = (
        CheckConstraint("mode IN ('fixed', 'auction')"),
        CheckConstraint("status IN ('open', 'sold', 'cancelled', 'expired')"),
        CheckConstraint('quantity > 0 AND price > 0 AND bid >= 0'),
    )


class GearDefinition(Base):
    __tablename__ = 'gear_definitions'
    slug = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    slot = Column(String, nullable=False)
    bonuses = Column(JSON, nullable=False, default=dict)
    required_rank = Column(String, ForeignKey('rank_definitions.slug'), nullable=False, default='iron')
    price = Column(Integer, nullable=False)


class Gear(Base):
    __tablename__ = 'gear'
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey('adventurers.id'), nullable=False, index=True)
    definition_slug = Column(String, ForeignKey('gear_definitions.slug'), nullable=False)
    definition = relationship('GearDefinition', lazy='joined')


class EquippedGear(Base):
    __tablename__ = 'equipped_gear'
    adventurer_id = Column(UUID(as_uuid=True), ForeignKey('adventurers.id'), primary_key=True)
    slot = Column(String, primary_key=True)
    gear_id = Column(UUID(as_uuid=True), ForeignKey('gear.id'), nullable=False, unique=True)
    gear = relationship('Gear', lazy='joined')
