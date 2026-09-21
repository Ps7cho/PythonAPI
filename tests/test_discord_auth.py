from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from app import auth


def test_discord_link_reset_and_unlink(client, monkeypatch):
    settings = SimpleNamespace(
        discord_client_id="client-id",
        discord_client_secret="client-secret",
        discord_redirect_uri="https://api.example.test/api/auth/discord/callback",
        discord_login_redirect_uri="https://api.example.test/api/auth/discord/login/callback",
        public_client_origins=[],
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    username = client.get("/api/auth/me").json()["username"]
    link = client.post("/api/auth/discord/link")
    assert link.status_code == 200
    query = parse_qs(urlparse(link.json()["authorization_url"]).query)
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == [settings.discord_redirect_uri]

    profile = {"id": "discord-123", "username": "Community", "email": "community@example.test"}
    monkeypatch.setattr(auth, "discord_user", lambda code, redirect_uri: (profile, "discord-token"))
    callback = client.get("/api/auth/discord/callback", params={"code": "code", "state": query["state"][0]})
    assert callback.status_code == 200
    assert callback.json() == {"linked": True, "discord_id": "discord-123"}

    monkeypatch.setattr(auth, "discord_request", lambda url, data=None, token=None: profile)
    reset = client.post("/api/auth/password/reset-with-discord", json={
        "discord_access_token": "discord-token", "new_password": "discord-reset-password",
    })
    assert reset.status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.post("/api/auth/login", json={
        "username": username, "password": "discord-reset-password",
    }).status_code == 200

    # The account remains linked until the authenticated owner explicitly removes it.
    assert client.delete("/api/auth/discord/link").status_code == 204
