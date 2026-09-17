from fastapi.testclient import TestClient
from uuid import UUID
from sqlalchemy import func, select

from app.main import app
from app.database import SessionLocal
from app.models import Ability, GameEvent, QuestRun, RaidRotation


def test_inspection_requires_login_and_preserves_public_catalog():
    with TestClient(app) as browser:
        response = browser.get('/api/abilities')
        assert response.status_code == 200
        assert isinstance(response.json(), list)
        assert browser.get('/api/abilities?inspect=true').status_code == 401


def test_inspector_uses_live_catalog_and_existing_loot_previews(client):
    def counts():
        with SessionLocal() as db:
            return [db.scalar(select(func.count()).select_from(model))
                    for model in (GameEvent, QuestRun, RaidRotation)]

    before = counts()
    with SessionLocal.begin() as db:
        ability = db.scalar(select(Ability).order_by(Ability.name))
        ability_id, original = str(ability.id), ability.description
        ability.description = 'Live inspector catalog edit'
    try:
        response = client.get('/api/abilities?inspect=true')
        assert response.status_code == 200
        data = response.json()
        assert next(a for a in data['abilities'] if a['id'] == ability_id)['description'] == 'Live inspector catalog edit'
        assert data['quests'] == client.get('/api/quest-templates').json()
        ability_ids = {a['id'] for a in data['abilities']}
        active = {e['consumable_slug'] for e in data['essences'] if e['active']}
        assert data['orb_outcomes']
        assert all(r['ability_id'] in ability_ids and r['essence_slug'] in active for r in data['orb_outcomes'])
        assert data['quest_definitions'] and data['enemy_abilities'] and data['ranks']
        assert not {'users', 'inventory', 'adventurers', 'sessions'} & data.keys()
        assert counts() == before
    finally:
        with SessionLocal.begin() as db:
            db.get(Ability, UUID(ability_id)).description = original
