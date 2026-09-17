from uuid import UUID, uuid4

from app.consumables import grant
from app.database import SessionLocal
from app.models import Adventurer


def finish_encounter(client, encounter):
    for _ in range(20):
        response = client.post(f"/api/encounters/{encounter['id']}/actions", json={
            "actor_id": encounter["participants"][0]["id"],
            "expected_turn": encounter["turn"],
            "action": "attack",
        })
        assert response.status_code == 200, response.text
        encounter = response.json()
        if encounter["state"] != "player_turn":
            return encounter
    raise AssertionError("Encounter did not finish")


def test_combat_and_account_statistics_accumulate(client):
    hero = client.post("/api/adventurers", json={"name": str(uuid4())}).json()
    encounter = client.post("/api/encounters", json={"adventurer_ids": [hero["id"]]}).json()
    assert encounter["quest"]["encounter_number"] == 1
    finished = finish_encounter(client, encounter)
    assert finished["state"] == "victory"

    details = client.get(f"/api/adventurers/{hero['id']}").json()
    stats = details["statistics"]
    assert stats["damage_dealt"] > 0
    assert stats["enemies_killed"] == 1
    assert stats["highest_rank_kill"] == "iron"
    assert stats["encounters_started"] == 1
    assert stats["encounters_completed"] == 1
    assert stats["adventures_started"] == 1
    assert stats["adventures_completed"] == 1

    account = client.get("/api/auth/account").json()["statistics"]
    assert account["damage_dealt_lifetime"] == stats["damage_dealt"]
    assert account["enemies_killed_lifetime"] == 1
    assert account["encounters_completed_lifetime"] == 1


def test_consumable_use_is_counted_on_adventurer_and_account(client):
    hero = client.post("/api/adventurers", json={"name": str(uuid4())}).json()
    with SessionLocal.begin() as db:
        grant(db, UUID(hero["id"]), "might-tonic", 1)
    encounter = client.post("/api/encounters", json={"adventurer_ids": [hero["id"]]}).json()
    response = client.post(f"/api/encounters/{encounter['id']}/actions", json={
        "actor_id": hero["id"], "expected_turn": encounter["turn"],
        "consumable_slug": "might-tonic",
    })
    assert response.status_code == 200, response.text
    details = client.get(f"/api/adventurers/{hero['id']}").json()
    assert details["statistics"]["consumables_used"] == 1
    assert details["account_statistics"]["consumables_used_lifetime"] == 1


def test_account_statistics_are_shared_across_adventurers(client):
    first = client.post("/api/adventurers", json={"name": str(uuid4())}).json()
    second = client.post("/api/adventurers", json={"name": str(uuid4())}).json()
    encounter = client.post("/api/encounters", json={"adventurer_ids": [first["id"]]}).json()
    finish_encounter(client, encounter)
    second_details = client.get(f"/api/adventurers/{second['id']}").json()
    assert second_details["statistics"]["enemies_killed"] == 0
    assert second_details["account_statistics"]["enemies_killed_lifetime"] == 1