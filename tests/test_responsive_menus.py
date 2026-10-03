"""Click every game menu at the supported browser sizes against an isolated API.

From the PythonAPI backend directory, run:
../.venv/Scripts/python.exe -m pytest tests/test_responsive_menus.py -q
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "FrontEndJS" / "AdventuringPortal"
ORIGIN = "https://responsive-menus.test"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
sys.path.insert(0, str(ROOT / "PythonAPI"))

from app.database import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from playwright.sync_api import expect, sync_playwright  # noqa: E402
from sqlalchemy import JSON, select  # noqa: E402


SIZES = [
    pytest.param(390, 844, id="mobile"),
    pytest.param(768, 1024, id="tablet"),
    pytest.param(1920, 1080, id="1080p"),
    pytest.param(2560, 1440, id="2k"),
    pytest.param(3840, 2160, id="4k"),
]


@pytest.fixture(scope="module")
def browser_backend():
    with TestClient(app) as client, patch.object(JSON, "python_type", property(lambda self: object)), sync_playwright() as playwright:
        username, password = "responsive_menu_tester", "local-test-password-1234"
        response = client.post("/api/auth/register", json={"username": username, "password": password})
        assert response.status_code == 201, response.text
        with SessionLocal.begin() as db:
            db.scalar(select(User).where(User.username == username)).account_type = "developer"
        client.cookies.clear()
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        try:
            yield browser, client, username, password
        finally:
            browser.close()


def serve_frontend(route, client):
    request = route.request
    path = request.url.removeprefix(ORIGIN).split("?", 1)[0]
    if path.startswith("/api/"):
        headers = {key: value for key, value in request.headers.items()
                   if key in ("authorization", "content-type", "x-client-auth")}
        response = client.request(request.method, request.url.removeprefix(ORIGIN),
                                  headers=headers, content=request.post_data)
        route.fulfill(status=response.status_code, body=response.content,
                      headers={"content-type": response.headers.get("content-type", "application/json")})
    elif path == "/config.js":
        route.fulfill(content_type="application/javascript",
                      body=f'window.APP_CONFIG={{apiBaseUrl:"{ORIGIN}/api"}};')
    else:
        file = (FRONTEND / (path.lstrip("/") or "index.html")).resolve()
        if file.is_file() and file.is_relative_to(FRONTEND):
            route.fulfill(path=str(file))
        else:
            route.fulfill(status=404, body="Not found")


def click_tabs(page, tablist, expected):
    tabs = tablist.get_by_role("tab")
    assert tabs.all_text_contents() == expected
    for label in expected:
        tab = tablist.get_by_role("tab", name=label, exact=True)
        tab.click()
        expect(tab).to_have_attribute("aria-selected", "true")
        panel_id = tab.get_attribute("aria-controls")
        expect(page.locator("#" + panel_id)).to_be_visible()


@pytest.mark.parametrize("width,height", SIZES)
def test_all_menus_are_clickable_at_each_resolution(browser_backend, width, height):
    browser, client, username, password = browser_backend
    page = browser.new_page(viewport={"width": width, "height": height})
    page.set_default_timeout(15000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.route(ORIGIN + "/**", lambda route: serve_frontend(route, client))
    try:
        page.goto(ORIGIN + "/")
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
        page.get_by_role("button", name="Log In", exact=True).click()
        expect(page.locator("#game")).to_be_visible()

        # Use the real API and a real character sheet inside the Character tab.
        token = page.evaluate("GameApi.liveToken()")
        response = client.post("/api/adventurers", headers={"Authorization": "Bearer " + token},
                               json={"name": f"Menu tester {width}"})
        assert response.status_code == 200, response.text
        page.reload()
        expect(page.locator("#game")).to_be_visible()

        main = page.locator("#game .tabs[role=tablist]").first
        main_labels = ["Home", "Quests", "Character", "Account", "Auction House",
                       "Village Shops", "Worldsmith", "Gauntlet", "Encounter", "Bestiary & Testing"]
        click_tabs(page, main, main_labels)

        main.get_by_role("tab", name="Quests", exact=True).click()
        click_tabs(page, page.locator("#panel-journeys .tabs[role=tablist]").first,
                   ["Journeys", "Bulletin Board", "Epics", "Raids", "Quest Lobby"])

        main.get_by_role("tab", name="Village Shops", exact=True).click()
        shop_tabs = page.locator(".village-shop-tabs")
        expect(shop_tabs.locator("button").first).to_be_visible()
        shop_labels = shop_tabs.locator("button").all_text_contents()
        assert shop_labels, "Village Shops has no departments"
        for label in shop_labels:
            tab = shop_tabs.get_by_role("button", name=label, exact=True)
            tab.click()
            expect(tab).to_have_attribute("aria-selected", "true")
            expect(page.locator(".village-shop-grid")).to_be_visible()

        main.get_by_role("tab", name="Auction House", exact=True).click()
        market = page.locator(".auction-house")
        for view in ("mine", "browse"):
            button = market.locator(f'[data-market-view="{view}"]')
            button.click()
            expect(button).to_have_attribute("aria-pressed", "true")
            expect(market.locator("[data-market-list]")).to_be_visible()

        main.get_by_role("tab", name="Gauntlet", exact=True).click()
        expect(page.locator(".gauntlet-policy")).to_be_visible()
        expect(page.get_by_label("Gauntlet definition")).not_to_be_empty()

        main.get_by_role("tab", name="Worldsmith", exact=True).click()
        nav = page.locator(".studio-nav")
        expect(nav.locator("button").first).to_be_visible()
        worldsmith_labels = [label.strip() for label in nav.locator("button span").all_text_contents()]
        assert len(worldsmith_labels) >= 20, "Worldsmith menu is incomplete"
        for label in worldsmith_labels:
            button = nav.locator("button").filter(has=page.get_by_text(label, exact=True))
            button.click()
            expect(button).to_have_attribute("aria-current", "true")
            expect(page.locator(".studio-heading h2")).to_have_text(
                "Manage saved data" if label == "Manage data" else label)

        main.get_by_role("tab", name="Character", exact=True).click()
        sheet = page.frame_locator("iframe[data-character-frame]")
        expect(page.locator("iframe[data-character-frame]")).to_be_visible()
        click_tabs(sheet, sheet.locator(".menu-shell .tabs[role=tablist]"),
                   ["Overview", "Attributes", "Equipment", "Abilities", "Friends"])
        assert not errors, f"JavaScript errors at {width}x{height}: {errors}"
    finally:
        page.close()
