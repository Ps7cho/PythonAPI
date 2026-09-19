import os
from uuid import uuid4

import pytest


os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient
from app.main import app
from app.database import engine
from app.database import SessionLocal
from app.models import Ability

# Fixed balance for turn-count regression tests; production reads its own catalog.
with SessionLocal.begin() as db:
    for ability in db.query(Ability).all():
        if ability.name == 'Strike':
            ability.power = 10
            ability.damage_multiplier = 1.0
        elif ability.name == 'Power Strike':
            ability.power = 16
            ability.damage_multiplier = 1.6
            ability.cooldown_value = 3


@pytest.fixture
def reset_limits():
    from app.request_limits import limiter
    limiter.clear()
    yield
    limiter.clear()


@pytest.fixture(autouse=True)
def isolated_request_limits(reset_limits):
    pass


@pytest.fixture
def client(monkeypatch):
    # Neutral attributes keep existing fixed-damage regression scenarios meaningful.
    # Attribute integration tests replace this generator with explicit real builds.
    from app import main
    monkeypatch.setattr(main, 'generate_attribute_budget', lambda: {name: 0 for name in main.ATTRIBUTE_NAMES})
    with TestClient(app) as client:
        response = client.post('/api/auth/register', json={
            'username': 'test_' + uuid4().hex[:20], 'password': 'test-password-1234',
        })
        assert response.status_code == 201
        yield client


def pytest_sessionfinish(session, exitstatus):
    engine.dispose()
