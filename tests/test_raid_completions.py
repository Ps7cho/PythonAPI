from copy import deepcopy
from datetime import datetime
from uuid import UUID

from app.database import SessionLocal
from app.models import Encounter
from test_encounter_groups import start


def test_current_rotation_full_victories_are_sorted(client):
    records = []
    for index, status in enumerate(['victory', 'victory', 'returned', 'defeat', 'awaiting_continue', 'old', 'partial']):
        _, snapshot = start(client, 'daily-raid', count=2 if index == 0 else 1)
        with SessionLocal.begin() as db:
            encounter = db.get(Encounter, UUID(snapshot['id']))
            run = encounter.quest_run
            run.status = 'victory' if status in ('old', 'partial') else status
            run.current_stage = 'complete'
            encounter.state = 'victory'
            encounter.updated_at = datetime(2026, 9, 17, 10, 0, 7-index)
            plan = deepcopy(run.quest.encounter_pool)
            if status != 'partial':
                plan[-1]['encounter_id'] = str(encounter.id)
            run.quest.encounter_pool = plan
            if status == 'old':
                rewards = deepcopy(run.quest.rewards)
                rewards['journey']['raid_seed'] = 'previous-rotation'
                run.quest.rewards = rewards
            records.append(str(run.id))
    rotation = client.get('/api/quest-templates/daily-raid').json()['journey']['raid']['rotation']
    assert rotation['completion_count'] == 2
    clears = rotation['completions']
    assert [row['run_id'] for row in clears] == [records[1], records[0]]
    assert clears[0]['team'] is None
    assert clears[1]['team'] == 'Adventuring party'
    assert len(clears[1]['players']) == 2
    assert all(p['player'] and p['character'] for p in clears[1]['players'])
    assert clears[0]['completed_at'].endswith('+00:00')
    weekly = client.get('/api/quest-templates/weekly-raid').json()['journey']['raid']['rotation']
    assert weekly['completion_count'] == 0
    assert weekly['completions'] == []
    listed = client.get('/api/quest-templates').json()
    assert next(t for t in listed if t['slug'] == 'daily-raid')['journey']['raid']['rotation']['completions'] == clears
