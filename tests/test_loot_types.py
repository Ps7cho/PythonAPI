from app.database import SessionLocal
from app.loot_types import STARTER_LOOT_TYPES, seed_loot_types
from app.models import LootType


def test_loot_type_catalog(client):
    seed_loot_types()
    seed_loot_types()
    response = client.get('/api/loot-types')
    assert response.status_code == 200
    assert response.json() == sorted(STARTER_LOOT_TYPES, key=lambda item: item['name'])
    assert client.get('/api/loot-types/exp').json() == {'slug': 'exp', 'name': 'Exp'}
    assert client.get('/api/loot-types/missing').status_code == 404


def test_seed_preserves_existing_loot_names(client):
    try:
        with SessionLocal.begin() as db:
            db.get(LootType, 'exp').name = 'Experience'
        seed_loot_types()
        assert client.get('/api/loot-types/exp').json()['name'] == 'Experience'
    finally:
        with SessionLocal.begin() as db:
            db.get(LootType, 'exp').name = 'Exp'
