"""UTC raid rotations: shared, persisted generation snapshots; unique run IDs."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from hashlib import sha256
from random import Random
from typing import Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from app.models import RaidRotation, Enemy
from app.enemies import roll_enemy, batch_enemy_loadouts
from app.group_journeys import annotate_lengths


class RaidRules(BaseModel):
    cadence: Literal['daily', 'weekly']
    min_encounters: int = Field(ge=10, le=15)
    max_encounters: int = Field(ge=10, le=15)
    bosses: list[str] = Field(min_length=3, max_length=3)

    @model_validator(mode='after')
    def valid_route(self):
        if self.max_encounters < self.min_encounters or len(set(self.bosses)) != 3:
            raise ValueError('Raids require ordered encounter bounds and three distinct bosses')
        return self


def rotation_info(slug, cadence, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if cadence == 'weekly':
        start -= timedelta(days=start.weekday())
    end = start + timedelta(days=7 if cadence == 'weekly' else 1)
    key = slug + ':' + start.date().isoformat()
    return {'key': key, 'seed': sha256(key.encode()).hexdigest()[:16],
            'period': start.date().isoformat(), 'resets_at': end.isoformat()}


def build_raid_plan(db, slug, settings, now=None):
    rules = RaidRules.model_validate(settings['raid'])
    if settings.get('death_policy') != 'permanent' or settings.get('repeat_groups', True):
        raise HTTPException(409, 'Raids must use permanent death and a finite route.')
    info = rotation_info(slug, rules.cadence, now)
    saved = db.get(RaidRotation, info['key'])
    if saved is None:
        rng = Random(info['seed'])
        count = rng.randint(rules.min_encounters, rules.max_encounters)
        boss_positions = [count // 3, count * 2 // 3, count]
        slugs = set(rules.bosses) | {s for stage in settings['stages'] for group in stage['groups'] for s in group}
        catalog = {e.slug: e for e in db.scalars(select(Enemy).where(Enemy.slug.in_(slugs)))}
        if set(catalog) != slugs or any(catalog[s].enemy_type != 'boss' for s in rules.bosses):
            raise HTTPException(409, 'Raid boss or enemy definitions are unavailable.')
        if any(catalog[s].enemy_type == 'boss' for stage in settings['stages'] for group in stage['groups'] for s in group):
            raise HTTPException(409, 'Bosses must appear only at the three raid checkpoints.')
        loadouts = batch_enemy_loadouts(db, slugs)
        plan = []
        for number in range(1, count + 1):
            boss = number in boss_positions
            stage = rng.choice(settings['stages'])
            enemies = [rules.bosses[boss_positions.index(number)]] if boss else rng.choice(stage['groups'])
            plan.append({'enemy_slugs': enemies, 'boss': boss,
                         'description': 'Boss: ' + catalog[enemies[0]].name if boss else stage['description'],
                         'seeded_enemies': [roll_enemy(catalog[s], 1, rng=rng, loadouts=loadouts).state for s in enemies]})
        annotate_lengths(plan, [boss_positions[0], boss_positions[1]-boss_positions[0], count-boss_positions[1]])
        snapshot_settings = {**settings, 'raid_seed': info['seed'], 'raid_period': info['period'],
                             'raid_resets_at': info['resets_at'], 'original_encounter_count': count}
        insert = sqlite_insert if db.bind.dialect.name == 'sqlite' else postgres_insert
        db.execute(insert(RaidRotation).values(key=info['key'], seed=info['seed'],
            resets_at=datetime.fromisoformat(info['resets_at']).replace(tzinfo=None),
            snapshot={'plan': plan, 'settings': snapshot_settings}).on_conflict_do_nothing(index_elements=['key']))
        saved = db.get(RaidRotation, info['key'], populate_existing=True)
    plan = deepcopy(saved.snapshot['plan'])
    for entry in plan:
        entry['encounter_id'] = str(uuid4())
    return plan, deepcopy(saved.snapshot['settings'])
