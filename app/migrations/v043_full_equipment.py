"""Expand armor/accessory slots and complete existing Wolf Sovereign rewards."""
from uuid import UUID, uuid4
from sqlalchemy import select


def upgrade(conn):
    from app.models import EquippedGear, GearDefinition, Gear, WorldBossReward
    equipment, definitions = EquippedGear.__table__, GearDefinition.__table__
    # Ring remains the catalog item type; equipped locations are separate.
    conn.execute(equipment.update().where(equipment.c.slot == 'Ring').values(slot='Ring 1'))
    specs = [
        ('shoulders', 'Shoulders', {'Defense':15, 'Might':10}),
        ('bracers', 'Bracers', {'Precision':15, 'Defense':10}),
        ('belt', 'Belt', {'Vitality':15, 'Luck':10}),
        ('cape', 'Cape', {'Agility':15, 'Awareness':10}),
    ]
    for piece, slot, bonuses in specs:
        slug = 'alpha-wolf-' + piece
        if not conn.scalar(select(definitions.c.slug).where(definitions.c.slug == slug)):
            name = 'Wolf Sovereign ' + slot
            conn.execute(definitions.insert().values(slug=slug, name=name, slot=slot,
                bonuses=bonuses, effect_slugs=[], required_rank='iron', price=0))
    # Extend already-claimed sets where the original reward items still exist.
    # The saved IDs prevent duplicate grants if this upgrade is retried.
    rewards, gear = WorldBossReward.__table__, Gear.__table__
    for reward in conn.execute(select(rewards).where(rewards.c.claimed.is_(True))).mappings().all():
        ids = reward['gear_ids'] or []
        pieces = conn.execute(select(gear).where(gear.c.id.in_([UUID(i) for i in ids]),
            gear.c.bound_account_id == reward['account_id'])).mappings().all()
        if not pieces:
            continue
        present = {piece['definition_slug'] for piece in pieces}
        added = []
        for piece, _, _ in specs:
            slug = 'alpha-wolf-' + piece
            if slug not in present:
                gear_id = uuid4()
                conn.execute(gear.insert().values(id=gear_id, adventurer_id=pieces[0]['adventurer_id'],
                    definition_slug=slug, rarity='epic', bound_account_id=reward['account_id']))
                added.append(str(gear_id))
        if added:
            conn.execute(rewards.update().where(rewards.c.event_id == reward['event_id'],
                rewards.c.account_id == reward['account_id']).values(gear_ids=ids+added))
