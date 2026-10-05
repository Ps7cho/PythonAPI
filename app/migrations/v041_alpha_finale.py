"""Large Iron encounter, alpha finale ledger, and account-bound reward gear."""
from datetime import datetime
from sqlalchemy import inspect, select, text


def upgrade(conn):
    from app.models import (WorldBossState, WorldBossEvent, WorldBossEntry, WorldBossReward,
                            GearDefinition, QuestTemplate)
    from app.world_boss import next_start
    for model in (WorldBossState, WorldBossEvent, WorldBossEntry, WorldBossReward):
        model.__table__.create(conn, checkfirst=True)
    if 'bound_account_id' not in {c['name'] for c in inspect(conn).get_columns('gear')}:
        conn.execute(text('ALTER TABLE gear ADD COLUMN bound_account_id UUID REFERENCES users(id)'))
    settings = {'timezone': 'America/Denver', 'weekday': 6, 'hour': 20}
    if not conn.scalar(select(WorldBossState.slug).where(WorldBossState.slug == 'alpha-wolf')):
        conn.execute(WorldBossState.__table__.insert().values(slug='alpha-wolf', phase='alpha', enabled=True,
            max_health=10_000_000, duration_seconds=600, settings=settings,
            next_start_at=next_start(datetime.utcnow(), settings)))
    templates = [
        dict(slug='iron-rat-swarm', name='Iron: Ten Rats', difficulty=1, min_encounters=1, max_encounters=1,
             region='Mosswood', enemy_pool=['Rat'], possible_rewards=['Gold', 'Experience'],
             journey={'kind': 'quest', 'description': 'An Iron battle against ten rats at once. Preview a large battlefield.',
                      'required_rank': 'iron', 'stages': [{'description': 'Ten rats swarm the cellar.', 'groups': [['rat'] * 10]}],
                      'gold': 10, 'experience': 15}),
        dict(slug='alpha-wolf-finale', name='Alpha Wolf: End of Alpha', difficulty=1, min_encounters=1, max_encounters=1,
             region='Mosswood', enemy_pool=['Alpha Wolf'], possible_rewards=['Account-bound epic Wolf Sovereign set', 'Beta unlocked'],
             journey={'kind': 'raid', 'world_boss': True, 'required_rank': 'iron', 'gold': 0, 'experience': 0,
                      'description': 'Sunday 8 PM America/Denver. Ten minutes. One shared 10,000,000 HP boss; each party faces its own attacks. Defeat unlocks beta. Failure deletes every adventurer, inventory, and progression; accounts remain.',
                      'stages': [{'description': 'The Alpha Wolf challenges every party.', 'groups': [['alpha-wolf']]}]})]
    for template in templates:
        if not conn.scalar(select(QuestTemplate.slug).where(QuestTemplate.slug == template['slug'])):
            conn.execute(QuestTemplate.__table__.insert().values(**template))
    for slug, slot, bonuses in [
        ('head', 'Head', {'Awareness': 15, 'Willpower': 10}),
        ('chest', 'Chest', {'Vitality': 20, 'Defense': 15}),
        ('hands', 'Hands', {'Might': 15, 'Precision': 10}),
        ('legs', 'Legs', {'Agility': 15, 'Affinity': 10}),
        ('feet', 'Feet', {'Speed': 15, 'Luck': 10})]:
        key = 'alpha-wolf-' + slug
        if not conn.scalar(select(GearDefinition.slug).where(GearDefinition.slug == key)):
            conn.execute(GearDefinition.__table__.insert().values(slug=key, name='Wolf Sovereign ' + slot,
                slot=slot, bonuses=bonuses, effect_slugs=[], required_rank='iron', price=0))
