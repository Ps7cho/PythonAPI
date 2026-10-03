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
    pytest.param(375, 667, id="mobile-compact"),
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


def select_tab(page, tablist, label):
    labels = tablist.locator("button").all_text_contents()
    tab = tablist.locator("button").nth(labels.index(label))
    panel_id = tab.get_attribute("aria-controls")
    key = panel_id.removeprefix("panel-")
    container = tablist.locator("..")
    mobile_nav = container.locator(".mobile-nav")
    if mobile_nav.is_visible():
        direct = mobile_nav.locator(f'[data-mobile-key="{key}"]')
        if direct.count():
            direct.click()
            expect(direct).to_have_attribute("aria-current", "page")
        else:
            more = mobile_nav.get_by_role("button", name="More", exact=True)
            more.click()
            sheet = container.locator(".mobile-nav-sheet")
            expect(sheet).to_be_visible()
            sheet.locator(f'[data-mobile-key="{key}"]').click()
            expect(sheet).to_be_hidden()
            expect(more).to_have_attribute("aria-current", "page")
    else:
        tab.click()
    expect(tab).to_have_attribute("aria-selected", "true")
    expect(page.locator("#" + panel_id)).to_be_visible()


def click_tabs(page, tablist, expected):
    assert tablist.locator("button").all_text_contents() == expected
    for label in expected:
        select_tab(page, tablist, label)


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
        hero_id = response.json()["id"]
        page.reload()
        expect(page.locator("#game")).to_be_visible()

        main = page.locator("#game .tabs[role=tablist]").first
        select_tab(page, main, "Character")
        page.locator("[data-character-select]").select_option(hero_id)
        if width <= 390:
            bottom_nav = page.locator("#game>.menu-shell>.mobile-nav")
            expect(bottom_nav).to_be_visible()
            bounds = bottom_nav.bounding_box()
            assert bounds["y"] + bounds["height"] <= height + 1
            assert all(button.bounding_box()["height"] >= 44
                       for button in bottom_nav.locator("button").all())
        main_labels = ["Home", "Quests", "Character", "Account", "Auction House",
                       "Village Shops", "Worldsmith", "Gauntlet", "Encounter", "Bestiary & Testing"]
        click_tabs(page, main, main_labels)

        select_tab(page, main, "Quests")
        quest_tablist = page.locator("#panel-journeys .tabs[role=tablist]").first
        click_tabs(page, quest_tablist,
                   ["Journeys", "Bulletin Board", "Epics", "Raids", "Quest Lobby"])
        select_tab(page, quest_tablist, "Journeys")
        expect(page.locator(".quest-current-rank")).to_contain_text("Iron rank")
        expect(page.locator(".journey-card .quest-rank-banner").first).to_be_visible()
        if width == 390:
            page.evaluate("GameSelectedCharacter.id = 'stale-character'")
        page.locator("[data-quest-template]").first.click()
        expect(page.locator("[data-lobby-hero]")).to_contain_text(f"Menu tester {width}")
        expect(page.locator("[data-lobby-rank]")).to_contain_text("Required rank")
        expect(page.locator("[data-lobby-depart]")).to_be_enabled()
        if width <= 390:
            depart_bottom = page.locator("[data-lobby-depart]").bounding_box()
            nav_top = page.locator("#game > .menu-shell > .mobile-nav").bounding_box()["y"]
            assert depart_bottom["y"] + depart_bottom["height"] <= nav_top, "Depart is below the phone navigation"

        select_tab(page, main, "Village Shops")
        shop_tabs = page.locator(".village-shop-tabs")
        expect(shop_tabs.locator("button").first).to_be_visible()
        shop_labels = shop_tabs.locator("button").all_text_contents()
        assert shop_labels, "Village Shops has no departments"
        for label in shop_labels:
            tab = shop_tabs.get_by_role("button", name=label, exact=True)
            tab.click()
            expect(tab).to_have_attribute("aria-selected", "true")
            expect(page.locator(".village-shop-grid")).to_be_visible()

        select_tab(page, main, "Auction House")
        market = page.locator(".auction-house")
        for view in ("mine", "browse"):
            button = market.locator(f'[data-market-view="{view}"]')
            button.click()
            expect(button).to_have_attribute("aria-pressed", "true")
            expect(market.locator("[data-market-list]")).to_be_visible()

        select_tab(page, main, "Gauntlet")
        expect(page.locator(".gauntlet-setup")).to_be_visible()
        expect(page.locator(".gauntlet-policy")).to_be_hidden()
        page.locator(".gauntlet-setup summary").click()
        expect(page.locator(".gauntlet-policy")).to_be_visible()
        expect(page.get_by_label("Gauntlet definition")).not_to_be_empty()
        page.locator(".gauntlet-setup summary").click()

        select_tab(page, main, "Worldsmith")
        nav = page.locator(".studio-nav")
        library_toggle = page.get_by_role("button", name="Choose Worldsmith library")
        if width <= 390:
            library_toggle.click()
            expect(nav.locator("button").first).to_be_visible()
            library_filter = page.get_by_label("Find a Worldsmith library")
            library_filter.fill("weapon")
            assert nav.locator("button:visible").count() >= 3
            assert all("weapon" in label.lower() for label in nav.locator("button:visible").all_text_contents())
            library_filter.fill("")
        else:
            expect(nav.locator("button").first).to_be_visible()
        worldsmith_labels = [label.strip() for label in nav.locator("button span").all_text_contents()]
        assert len(worldsmith_labels) >= 20, "Worldsmith menu is incomplete"
        for label in worldsmith_labels:
            if width <= 390 and library_toggle.get_attribute("aria-expanded") == "false":
                library_toggle.click()
            button = nav.locator("button").filter(has=page.get_by_text(label, exact=True))
            button.click()
            expect(button).to_have_attribute("aria-current", "true")
            expect(page.locator(".studio-heading h2")).to_have_text(
                "Manage saved data" if label == "Manage data" else label)
            if width <= 390:
                expect(library_toggle).to_have_attribute("aria-expanded", "false")

        if width <= 390:
            library_toggle.click()
            nav.locator("button").filter(has=page.get_by_text("Weapons", exact=True)).click()
            browser_panel = page.locator(".studio-browser")
            expect(browser_panel.locator(".studio-panel")).to_be_visible()
            assert browser_panel.locator(".studio-panel").evaluate("el => el.clientHeight") >= 200
            expect(browser_panel.locator(".studio-detail")).to_be_hidden()
            browser_panel.locator(".studio-row").first.click()
            expect(browser_panel.locator(".studio-detail")).to_be_visible()
            browser_panel.get_by_role("button", name="Back to list").click()
            expect(browser_panel.locator(".studio-panel")).to_be_visible()
            library_toggle.click()
            nav.locator("button").filter(has=page.get_by_text("Armor Effects", exact=True)).click()
            browser_panel.locator(".studio-row").first.click()
            expect(browser_panel.locator(".studio-detail")).to_be_visible()
            browser_panel.locator(".studio-detail").get_by_role("button", name="Edit", exact=True).click()
            editor = page.locator("dialog.catalog-editor")
            expect(editor).to_be_visible()
            editor_bounds = editor.bounding_box()
            assert editor_bounds["width"] <= width + 1
            assert editor_bounds["height"] <= height + 1
            editor.get_by_role("button", name="Cancel", exact=True).click()
            expect(editor).to_be_hidden()
            browser_panel.get_by_role("button", name="Back to list").click()
            expect(browser_panel.locator(".studio-panel")).to_be_visible()
            library_toggle.click()
            nav.locator("button").filter(has=page.get_by_text("Manage data", exact=True)).click()
            expect(page.locator(".studio-heading h2")).to_have_text("Manage saved data")
            expect(browser_panel.locator(".studio-panel")).to_be_visible()
            browser_panel.locator(".studio-row").first.click()
            expect(browser_panel.locator(".studio-detail")).to_be_visible()
            browser_panel.get_by_role("button", name="Back to list").click()
            expect(browser_panel.locator(".studio-panel")).to_be_visible()

        select_tab(page, main, "Character")
        sheet = page.frame_locator("iframe[data-character-frame]")
        expect(page.locator("iframe[data-character-frame]")).to_be_visible()
        if width <= 390:
            expect(sheet.locator(".mobile-nav")).to_be_visible()
            create_toggle = page.locator(".character-recruit-toggle")
            create_toggle.click()
            expect(create_toggle).to_have_attribute("aria-expanded", "true")
            expect(page.locator("#create")).to_be_visible()
            create_toggle.click()
            expect(page.locator("#create")).to_be_hidden()
        click_tabs(sheet, sheet.locator(".menu-shell .tabs[role=tablist]"),
                   ["Overview", "Attributes", "Equipment", "Abilities", "Friends"])
        for section in ("attributes", "gear", "abilities"):
            select_tab(sheet, sheet.locator(".menu-shell .tabs[role=tablist]"),
                       {"attributes": "Attributes", "gear": "Equipment", "abilities": "Abilities"}[section])
            panel_height = sheet.locator("#panel-" + section).evaluate("el => el.clientHeight")
            assert panel_height >= 100, f"{section} content is clipped at {width}x{height}: {panel_height}px"
        select_tab(page, main, "Quests")
        select_tab(page, quest_tablist, "Journeys")
        page.locator("[data-quest-template]").first.click()
        expect(page.locator("[data-lobby-depart]")).to_be_enabled()
        page.locator("[data-lobby-depart]").click()
        expect(page.locator("#panel-encounter")).to_be_visible()
        expect(page.locator("#battlefield-glance .battlefield-glance-card").first).to_be_visible()
        expect(page.locator("#encounter-options")).to_be_visible()
        encounter_details = page.locator(".encounter-information")
        expect(encounter_details).not_to_have_attribute("open", "")
        encounter_details.locator("summary").click()
        expect(page.locator("#combatants")).to_be_visible()
        encounter_details.locator("summary").click()
        page.locator("#action-categories button").first.click()
        expect(page.locator("#action-branch")).to_be_visible()
        page.locator("#action-buttons button").first.click()
        expect(page.locator("#action-detail")).to_be_visible()
        if width <= 390:
            page.goto(ORIGIN + "/adventurer.html?id=" + hero_id)
            expect(page.locator("#sheet")).to_be_visible()
            standalone_nav = page.locator("#sheet>.menu-shell>.mobile-nav")
            expect(standalone_nav).to_be_visible()
            bounds = standalone_nav.bounding_box()
            assert bounds["y"] + bounds["height"] <= height + 1
            select_tab(page, page.locator("#sheet>.menu-shell>.tabs"), "Equipment")
        assert not errors, f"JavaScript errors at {width}x{height}: {errors}"
    finally:
        page.close()
