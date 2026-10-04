"""Click every game menu at the supported browser sizes against an isolated API.

From the PythonAPI backend directory, run:
../.venv/Scripts/python.exe -m pytest tests/test_responsive_menus.py -q
"""

import json
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
    page = browser.new_page(viewport={"width": width, "height": height}, has_touch=width <= 390)
    page.set_default_timeout(15000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    page.route(ORIGIN + "/**", lambda route: serve_frontend(route, client))
    try:
        page.goto(ORIGIN + "/")
        expect(page.locator("#login-panel")).to_be_visible()
        assert page.locator("#login-panel").bounding_box()["y"] < page.locator("#public-home").bounding_box()["y"]
        login_bounds = page.get_by_role("button", name="Log In", exact=True).bounding_box()
        assert login_bounds["y"] + login_bounds["height"] <= height, "Log In is below the first screen"
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
        pending_auth = []
        if width == 390:
            page.route(ORIGIN + "/api/auth/me", lambda route: pending_auth.append(route))
        page.reload(wait_until="domcontentloaded")
        if width == 390:
            page.wait_for_timeout(150)
            assert pending_auth, "Startup did not request the saved session"
            expect(page.locator("#login-panel")).to_be_visible()
            expect(page.get_by_role("button", name="Log In", exact=True)).to_be_enabled()
            assert page.locator("script[src*='debug.js']").count() == 0, "Worldsmith loaded before its tab opened"
            page.unroute(ORIGIN + "/api/auth/me")
            account = client.get("/api/auth/me", headers={"Authorization": "Bearer " + token})
            pending_auth.pop().fulfill(status=account.status_code, body=account.content,
                                       headers={"content-type": "application/json"})
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
        expect(page.locator(".journey-card .quest-rank-banner").first).to_have_attribute("data-rank", "iron")
        expect(page.locator(".journey-card .quest-rank-emblem").first).to_be_visible()
        expect(page.locator(".journey-card .quest-rank-emblem").first).to_have_text("I")
        if page.locator(".journey-card .quest-rank-banner[data-rank=bronze]").count():
            expect(page.locator(".journey-card .quest-rank-banner[data-rank=bronze] .quest-rank-emblem").first).to_have_text("II")
        expect(page.locator(".journey-card .quest-rank-banner").first).to_contain_text("Quest rank")
        if width <= 390:
            def swipe_quest(start_x, end_x):
                page.evaluate("""({startX,endX}) => {
                  const active=document.querySelector('#panel-journeys .tabs[role=tablist] > button[aria-selected=true]');
                  const panel=document.getElementById(active.getAttribute('aria-controls'));
                  const touch=x=>new Touch({identifier:1,target:panel,clientX:x,clientY:250});
                  panel.dispatchEvent(new TouchEvent('touchstart',{bubbles:true,touches:[touch(startX)],changedTouches:[touch(startX)]}));
                  panel.dispatchEvent(new TouchEvent('touchend',{bubbles:true,touches:[],changedTouches:[touch(endX)]}));
                }""", {"startX": start_x, "endX": end_x})
            swipe_quest(300, 100)
            expect(quest_tablist.get_by_role("tab", name="Bulletin Board")).to_have_attribute("aria-selected", "true")
            swipe_quest(300, 100)
            expect(quest_tablist.get_by_role("tab", name="Epics")).to_have_attribute("aria-selected", "true")
            swipe_quest(100, 300)
            expect(quest_tablist.get_by_role("tab", name="Bulletin Board")).to_have_attribute("aria-selected", "true")
            select_tab(page, quest_tablist, "Journeys")
            scrolled = page.evaluate("""() => {
              const outer=document.getElementById('panel-journeys');outer.scrollTop=80;
              const panel=document.getElementById('panel-quest-list');
              const touch=y=>new Touch({identifier:2,target:panel,clientX:150,clientY:y});
              panel.dispatchEvent(new TouchEvent('touchstart',{bubbles:true,touches:[touch(150)],changedTouches:[touch(150)]}));
              panel.dispatchEvent(new TouchEvent('touchmove',{bubbles:true,cancelable:true,touches:[touch(260)],changedTouches:[touch(260)]}));
              const prompted=!document.querySelector('.pull-refresh-indicator').hidden;
              panel.dispatchEvent(new TouchEvent('touchend',{bubbles:true,touches:[],changedTouches:[touch(260)]}));
              return {scrollTop:outer.scrollTop,prompted};
            }""")
            assert scrolled["scrollTop"] > 1 and not scrolled["prompted"], "Pull refresh started below the top"
            page.locator("#panel-journeys").evaluate("el => el.scrollTop = 0")
        if width == 390:
            page.evaluate("GameSelectedCharacter.id = 'stale-character'")
        page.locator("[data-quest-template]").first.click()
        expect(page.locator("[data-lobby-hero]")).to_contain_text(f"Menu tester {width}")
        expect(page.locator("[data-lobby-rank]")).to_contain_text("Quest rank")
        expect(page.locator("[data-lobby-rank]")).to_contain_text("One rank below can enter")
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
        expect(page.locator(".dev-studio")).to_be_visible()
        assert page.locator("script[src*='debug.js']").count() == 1
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
            if section == "gear":
                slot = sheet.locator(".equipped-slot").first
                expect(slot).to_be_visible()
                contrast = slot.evaluate("""el => {
                  const color=value=>[...value.matchAll(/\\d+/g)].slice(0,3).map(x=>Number(x[0])/255);
                  const light=value=>color(value).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
                  const style=getComputedStyle(el),text=getComputedStyle(el.querySelector('strong'));
                  const a=light(style.backgroundColor),b=light(text.color);return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
                }""")
                assert contrast >= 4.5, f"Equipment slot text contrast is only {contrast:.1f}:1"
                card = sheet.locator(".inventory-card").first
                if card.count():
                    card.click()
                    expect(card).to_have_attribute("aria-pressed", "true")
                    card_contrast = card.evaluate("""el => {
                      const rgb=value=>[...value.matchAll(/\\d+/g)].slice(0,3).map(x=>Number(x[0])/255);
                      const light=value=>rgb(value).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
                      const a=light(getComputedStyle(el).backgroundColor),b=light(getComputedStyle(el.querySelector('strong')).color);
                      return (Math.max(a,b)+.05)/(Math.min(a,b)+.05);
                    }""")
                    assert card_contrast >= 4.5, f"Inventory item text contrast is only {card_contrast:.1f}:1"
        select_tab(page, main, "Quests")
        select_tab(page, quest_tablist, "Journeys")
        page.locator("[data-quest-template]").first.click()
        expect(page.locator("[data-lobby-depart]")).to_be_enabled()
        lost_response = {"count": 0}
        if width == 390:
            def lose_departure_response(route):
                payload = json.loads(route.request.post_data or "{}")
                if lost_response["count"]:
                    route.fallback()
                    return
                response = client.post("/api/encounters", json=payload,
                                       headers={"Authorization": "Bearer " + token})
                assert response.status_code == 201, response.text
                lost_response["count"] += 1
                route.abort("failed")
            page.route(ORIGIN + "/api/encounters", lose_departure_response)
        page.locator("[data-lobby-depart]").click()
        expect(page.locator("#panel-encounter")).to_be_visible()
        if width == 390:
            assert lost_response["count"] == 1, "Lost departure response was not exercised"
        expect(page.locator("#battlefield-glance .battlefield-glance-card").first).to_be_visible()
        expect(page.locator("#player-glance")).to_contain_text(f"Menu tester {width}")
        expect(page.locator("#player-glance")).to_contain_text("HP")
        if width == 390:
            forecast = page.evaluate("""() => {
              const actor=actionActor(),saved=actor.statuses;
              actor.statuses=[
                {slug:'poison',name:'Poison',damage:3,stacks:2,resistance:0,remaining_rounds:2},
                {slug:'bleed',name:'Bleed',damage:2,stacks:1,resistance:0,remaining_rounds:3},
                {slug:'holy',name:'Holy',damage:1,stacks:1,resistance:0,periodic:'none',remaining_rounds:2}
              ];
              renderPlayerGlance();
              const bar=document.querySelector('.player-health-track');
              const result={damage:bar.dataset.projectedDamage,
                segments:[...bar.querySelectorAll('.player-health-effect')].map(el=>({type:el.dataset.effect,damage:el.dataset.damage,color:el.style.getPropertyValue('--effect-color')})),
                markers:bar.querySelectorAll('.player-health-marker').length,
                text:document.getElementById('player-glance').textContent};
              actor.statuses=saved;renderPlayerGlance();return result;
            }""")
            assert forecast["damage"] == "8"
            assert [(part["type"], part["damage"]) for part in forecast["segments"]] == [("poison", "6"), ("bleed", "2")]
            assert forecast["segments"][0]["color"] != forecast["segments"][1]["color"]
            assert forecast["markers"] == 1 and "Poison" not in forecast["text"]
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
            use_button = page.locator("#action-use")
            use_bounds = use_button.bounding_box()
            back_bounds = page.locator("#action-back").bounding_box()
            nav_top = page.locator("#game > .menu-shell > .mobile-nav").bounding_box()["y"]
            assert use_bounds["y"] >= 0 and use_bounds["y"] + use_bounds["height"] <= nav_top, "Selected action needs scrolling to use"
            assert abs(use_bounds["y"] - back_bounds["y"]) < 20, "Use action is not beside the action navigation"
            expect(page.locator("#player-glance")).to_be_visible()
            gesture = """distance => {
              const panel=document.querySelector('#panel-encounter');panel.scrollTop=0;
              const touch=y=>new Touch({identifier:1,target:panel,clientX:100,clientY:y});
              panel.dispatchEvent(new TouchEvent('touchstart',{bubbles:true,cancelable:true,touches:[touch(100)],changedTouches:[touch(100)]}));
              panel.dispatchEvent(new TouchEvent('touchmove',{bubbles:true,cancelable:true,touches:[touch(100+distance)],changedTouches:[touch(100+distance)]}));
              panel.dispatchEvent(new TouchEvent('touchend',{bubbles:true,cancelable:true,touches:[],changedTouches:[touch(100+distance)]}));
            }"""
            page.evaluate(gesture, 40)
            expect(page.locator('.pull-refresh-indicator')).to_be_hidden()
            with page.expect_navigation(wait_until="domcontentloaded"):
                page.evaluate(gesture, 110)
            expect(page.locator('#game')).to_be_visible()
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


def test_mobile_quest_results_show_combat_and_loot(browser_backend):
    from uuid import UUID
    from app.models import EquippedWeapon

    browser, client, username, password = browser_backend
    page = browser.new_page(viewport={'width': 390, 'height': 844}, has_touch=True)
    page.route(ORIGIN + '/**', lambda route: serve_frontend(route, client))
    try:
        page.goto(ORIGIN + '/')
        page.locator('#username').fill(username)
        page.locator('#password').fill(password)
        page.get_by_role('button', name='Log In', exact=True).click()
        expect(page.locator('#game')).to_be_visible()
        headers = {'Authorization': 'Bearer ' + page.evaluate('GameApi.liveToken()')}
        hero = client.post('/api/adventurers', headers=headers, json={'name': 'Results browser hero'}).json()
        with SessionLocal.begin() as db:
            db.get(EquippedWeapon, UUID(hero['id'])).weapon.base_damage = 90
        encounter = client.post('/api/encounters', headers=headers,
                                json={'adventurer_ids': [hero['id']], 'enemy_slug': 'goblin'}).json()
        response = client.post(f"/api/encounters/{encounter['id']}/actions", headers=headers, json={
            'actor_id': hero['id'], 'expected_turn': encounter['turn'], 'action': 'attack',
            'target_id': encounter['enemies'][0]['id']})
        assert response.status_code == 200 and response.json()['state'] == 'victory', response.text
        page.goto(ORIGIN + '/?results=' + encounter['quest']['id'])
        expect(page.locator('#quest-results')).to_be_visible()
        expect(page.get_by_role('heading', name='Party performance')).to_be_visible()
        expect(page.get_by_role('heading', name='Party loot rolls')).to_be_visible()
        expect(page.get_by_role('heading', name='Individual loot & rewards')).to_be_visible()
        assert page.locator('.results-metric').count() == 4
        assert page.locator('.results-bar-fill').count() > 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    finally:
        page.close()


def test_raid_refresh_countdown_uses_server_time(browser_backend):
    browser, client, username, password = browser_backend
    page = browser.new_page(viewport={'width': 390, 'height': 844}, has_touch=True)
    page.route(ORIGIN + '/**', lambda route: serve_frontend(route, client))
    try:
        page.goto(ORIGIN + '/')
        page.locator('#username').fill(username)
        page.locator('#password').fill(password)
        page.get_by_role('button', name='Log In', exact=True).click()
        expect(page.locator('#game')).to_be_visible()
        select_tab(page, page.locator('#game .tabs[role=tablist]').first, 'Quests')
        select_tab(page, page.locator('#panel-journeys .tabs[role=tablist]').first, 'Raids')
        countdown = page.locator('#panel-raids .raid-countdown').first
        expect(countdown).to_be_visible()
        first = countdown.inner_text()
        assert first.startswith('Refresh in ')
        expect(countdown).not_to_have_text(first, timeout=5000)
        assert countdown.get_attribute('title').startswith('Refreshes ')
        with page.expect_request(lambda request: '/api/quest-templates' in request.url, timeout=5000):
            countdown.evaluate("node=>{node.dataset.serverTime=node.dataset.raidReset;node.dataset.anchorTime=String(Date.now());}")
        select_tab(page, page.locator('#game .tabs[role=tablist]').first, 'Village Shops')
        shop_timer = page.locator('.village-shop-controls .raid-countdown')
        expect(shop_timer).to_be_visible()
        expect(shop_timer).to_contain_text('Stock refresh in ')
        with page.expect_request(lambda request: '/api/shop/villages' in request.url, timeout=5000):
            shop_timer.evaluate("node=>{node.dataset.serverTime=node.dataset.resetAt;node.dataset.anchorTime=String(Date.now());}")
    finally:
        page.close()


def test_finished_gauntlet_does_not_reopen_after_refresh(browser_backend):
    browser, client, username, password = browser_backend
    page = browser.new_page(viewport={'width': 390, 'height': 844}, has_touch=True)
    page.route(ORIGIN + '/**', lambda route: serve_frontend(route, client))
    try:
        page.goto(ORIGIN + '/')
        page.locator('#username').fill(username)
        page.locator('#password').fill(password)
        page.get_by_role('button', name='Log In', exact=True).click()
        expect(page.locator('#game')).to_be_visible()
        headers = {'Authorization': 'Bearer ' + page.evaluate('GameApi.liveToken()')}
        account_id = client.get('/api/auth/me', headers=headers).json()['id']
        hero = client.post('/api/adventurers', headers=headers,
                           json={'name': 'Gauntlet refresh hero'}).json()
        started = client.post('/api/gauntlet-runs', headers=headers,
                              json={'adventurer_ids': [hero['id']]})
        assert started.status_code == 201, started.text
        encounter = started.json()['encounter']
        encounter_id = encounter['id']

        page.goto(ORIGIN + '/?encounter=' + encounter_id)
        expect(page.locator('#combat')).to_be_visible()
        assert page.evaluate('(id) => localStorage.getItem("encounter-id:" + id)', account_id) == encounter_id

        for _ in range(250):
            if encounter['state'] == 'defeat':
                break
            response = client.post(f'/api/encounters/{encounter_id}/actions', headers=headers,
                                   json={'actor_id': encounter['pending_actor_ids'][0],
                                         'expected_turn': encounter['turn'], 'action': 'wait'})
            assert response.status_code == 200, response.text
            encounter = response.json()
        assert encounter['state'] == 'defeat'

        # An old bookmarked URL should open the run summary instead of the fight.
        page.evaluate('(id) => localStorage.removeItem("encounter-id:" + id)', account_id)
        page.goto(ORIGIN + '/?encounter=' + encounter_id)
        expect(page.locator('#tab-gauntlet')).to_have_attribute('aria-selected', 'true')
        expect(page.locator('#panel-gauntlet')).to_be_visible()
        assert 'encounter=' not in page.url
        assert page.evaluate('(id) => localStorage.getItem("encounter-id:" + id)', account_id) is None

        # Older sessions may have saved only the encounter ID in local storage.
        page.evaluate('([id, encounter]) => localStorage.setItem("encounter-id:" + id, encounter)',
                      [account_id, encounter_id])
        page.goto(ORIGIN + '/')
        expect(page.locator('#tab-gauntlet')).to_have_attribute('aria-selected', 'true')
        assert page.evaluate('(id) => localStorage.getItem("encounter-id:" + id)', account_id) is None

        page.get_by_role('button', name='View final battle').click()
        expect(page.locator('#combat')).to_be_visible()
        assert 'encounter=' not in page.url
        page.reload()
        expect(page.locator('#game')).to_be_visible()
        expect(page.locator('#combat')).to_be_hidden()
        assert 'encounter=' not in page.url
    finally:
        page.close()
