from app.quest_templates import GOBLIN_TROUBLE, seed_quest_templates
from app.database import SessionLocal
from app.models import QuestTemplate


def test_catalog_matches_template(client):
    response = client.get('/api/quest-templates/goblin-trouble')
    assert response.status_code == 200
    assert response.json() == {**GOBLIN_TROUBLE, "journey": {}}
    assert len(client.get('/api/quest-templates').json()) == 15
    assert client.get('/api/quest-templates/missing').status_code == 404


def test_seeding_preserves_database_edits(client):
    with SessionLocal.begin() as db:
        db.get(QuestTemplate, 'goblin-trouble').region = 'Edited region'
    try:
        seed_quest_templates()
        seed_quest_templates()
        templates = client.get('/api/quest-templates').json()
        assert len(templates) == 15
        assert next(t for t in templates if t['slug'] == 'goblin-trouble')['region'] == 'Edited region'
    finally:
        with SessionLocal.begin() as db:
            db.get(QuestTemplate, 'goblin-trouble').region = 'Mosswood'
