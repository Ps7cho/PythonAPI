from copy import deepcopy
from uuid import UUID
import pytest
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Adventurer, AdventurerAbility, Ability, OwnedConsumable, EssenceDefinition, OrbOutcome
from app.consumables import grant
from app.combat import CombatAbility, execute_action, execute_cast, tick_statuses, apply_status, InvalidCombatAction
from app.status_content import EFFECTS
from app.loadouts import executable
from test_essences import hero, stock, absorb
from test_critical_flinch import Rolls
from test_encounter_groups import start, clear_group, advance


def use(client, id, essence='essence-dark', orb='evasion-orb'):
    return client.post(f'/api/adventurers/{id}/essences/{essence}/orbs/{orb}/use', json={})


def prepared(client):
    id = hero(client)
    for essence in ['essence-dark', 'essence-magic']:
        stock(id, essence)
        assert absorb(client, id, essence).status_code == 200
    for orb in ['evasion-orb', 'strike-orb']: stock(id, orb, 3)
    return id


def test_chosen_essence_unlocks_named_ability_once_without_changing_loadout(client):
    id = prepared(client)
    before = client.get('/api/adventurers/' + id).json()
    result = use(client, id, 'essence-magic')
    assert result.status_code == 200, result.text
    assert result.json()['name'] == 'Blink'
    assert use(client, id, 'essence-magic').status_code == 409
    after = client.get('/api/adventurers/' + id).json()
    assert after['equipped_ability_ids'] == before['equipped_ability_ids']
    assert next(a for a in after['abilities'] if a['name'] == 'Blink')['unlocked']
    assert use(client, id, 'essence-dark').json()['name'] == 'Shadow Veil'
    with SessionLocal() as db:
        assert db.get(OwnedConsumable, (UUID(id), 'evasion-orb')).quantity == 1


def test_learned_strike_and_evasion_equip_and_execute_through_action_endpoint(client, monkeypatch):
    id = prepared(client)
    strike = use(client, id, orb='strike-orb').json()['ability_id']
    evade = use(client, id).json()['ability_id']
    assert client.post(f'/api/adventurers/{id}/loadout', json={'ability_ids': [strike, evade]}).status_code == 200
    e = client.post('/api/encounters', json={'adventurer_ids': [id]}).json()
    monkeypatch.setattr('app.encounter_service.combat_rng', lambda *args: Rolls(0))
    result = client.post('/api/encounters/' + e['id'] + '/actions', json={
        'actor_id': id, 'expected_turn': 1, 'ability_id': evade}).json()
    assert result['participants'][0]['hp'] == 100
    assert result['action_results'][1]['dodged']
    assert result['participants'][0]['ability_ready_turns'][evade] == 4
    next_turn = client.post('/api/encounters/' + e['id'] + '/actions', json={
        'actor_id': id, 'expected_turn': 2, 'ability_id': strike}).json()
    assert next_turn['action_results'][0]['amount'] == 12
    assert next_turn['participants'][0]['hp'] == 94


@pytest.mark.parametrize('reason', ['unabsorbed', 'retired', 'unknown', 'empty', 'active', 'dead'])
def test_invalid_use_never_spends_or_unlocks(client, reason):
    id = hero(client)
    stock(id, 'essence-dark'); absorb(client, id, 'essence-dark')
    if reason != 'empty': stock(id, 'evasion-orb')
    if reason == 'active': client.post('/api/encounters', json={'adventurer_ids': [id]})
    if reason == 'dead':
        with SessionLocal.begin() as db:
            saved = db.get(Adventurer, UUID(id)); saved.is_alive = False; saved.health = 0
    essence = {'unabsorbed': 'essence-magic', 'retired': 'essence-ice'}.get(reason, 'essence-dark')
    response = use(client, id, essence, 'missing-orb' if reason == 'unknown' else 'evasion-orb')
    assert response.status_code in (404, 409)
    with SessionLocal() as db:
        item = db.get(OwnedConsumable, (UUID(id), 'evasion-orb'))
        assert item is None if reason == 'empty' else item.quantity == 2
        ability_id = db.scalar(select(Ability.id).where(Ability.slug == 'orb-dark-evasion'))
        assert db.scalar(select(AdventurerAbility).where(AdventurerAbility.adventurer_id == UUID(id), AdventurerAbility.ability_id == ability_id)) is None


def combatants():
    return (dict(id='p', name='Player', team='a', hp=100, max_hp=100, power=10),
            dict(id='e', name='Enemy', team='b', hp=100, max_hp=100, power=10))


def test_evasion_boundaries_status_prevention_cooldown_and_one_attempt():
    player, enemy = combatants()
    execute_action(player, CombatAbility('blink', 'Blink', effect='evade', damage=60, target_type='self'), player, turn=1)
    hit = CombatAbility('hit', 'Hit', damage=10, status_effect=EFFECTS[0], cooldown_turns=3)
    result = execute_action(enemy, hit, player, turn=1, rng=Rolls(.5999))
    assert result['dodged'] and result['amount'] == 0 and not player.get('statuses')
    assert enemy['ability_ready_turns']['hit'] == 4
    assert 'evasion' not in player
    execute_action(enemy, CombatAbility('other', 'Other', damage=10), player, turn=1, rng=Rolls())
    assert player['hp'] == 90
    for turn, roll in [(2, .6), (3, 0)]:
        player['evasion'] = {'chance_percent': 60, 'until_turn': 3}
        result = execute_action(enemy, CombatAbility('other', 'Other', damage=10), player, turn=turn, rng=Rolls(roll))
        assert not result['dodged'] and 'evasion' not in player


def test_dot_bypasses_evasion_and_failed_cast_keeps_attempt():
    player, enemy = combatants()
    player['evasion'] = {'chance_percent': 60, 'until_turn': 3}
    apply_status(player, EFFECTS[0], enemy['id'])
    assert tick_statuses([player], turn=1)[0]['amount'] == 2
    assert player['evasion']['chance_percent'] == 60
    before = deepcopy([player, enemy])
    with pytest.raises(InvalidCombatAction):
        execute_cast(enemy, CombatAbility('area', 'Area', damage=10, max_targets=None), [player, enemy], turn=1, rng=Rolls(0))
    assert [player, enemy] == before


def test_all_nine_essences_have_two_executable_outcomes():
    with SessionLocal() as db:
        rows = db.scalars(select(OrbOutcome)).all()
        assert len(rows) == 18
        for row in rows:
            player, enemy = combatants()
            player['equipped_weapon'] = {'base_damage': 10, 'tags': ['melee', 'ranged']}
            ability = executable(row.ability)
            result = execute_action(player, ability, player if ability.effect == 'evade' else enemy, turn=1, rng=Rolls())
            assert result['effect'] in ('evade', 'damage')
            assert not row.ability.starter


def test_orb_loot_and_reduced_catalog(client, monkeypatch):
    expected = {'dark', 'holy', 'magic', 'sin', 'swift', 'fire', 'blood', 'balance', 'might'}
    id = hero(client)
    assert {e['slug'].removeprefix('essence-') for e in client.get('/api/adventurers/' + id).json()['essence_catalog']} == expected
    monkeypatch.setattr('app.group_journeys.roll_loot', lambda tier, rng: deepcopy(next(d for d in tier['drops'] if d.get('consumable_slug') == 'strike-orb')))
    ids, e = start(client)
    e = clear_group(client, e)
    advance(client, e, {'return_to_village': True})
    with SessionLocal() as db:
        assert db.get(OwnedConsumable, (UUID(ids[0]), 'strike-orb')).quantity == 1


def test_migration_preserves_retired_possessions_and_saved_loot():
    from uuid import uuid4
    from datetime import datetime
    from sqlalchemy import create_engine, text, func
    from app.database import Base
    from app.models import Consumable, AbsorbedEssence, QuestTemplate, RaidRotation, User
    from app.migrations.v012_essences import upgrade as seed_essences
    from app.migrations.v013_orbs import upgrade
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    hero_id, owner_id = uuid4(), uuid4()
    old = {'encounter_groups': {'loot_tiers': [{'name': 'Old', 'drops': [
        dict(name='Ice Essence', consumable_slug='essence-ice', quantity=1, weight=1),
        dict(name='Dark Essence', consumable_slug='essence-dark', quantity=1, weight=1)]}]}}
    try:
        with engine.begin() as conn:
            # Exercise replacement of the old consumable constraint as well.
            conn.execute(text('DROP TABLE consumables'))
            conn.execute(text("CREATE TABLE consumables (slug VARCHAR PRIMARY KEY, name VARCHAR NOT NULL, description VARCHAR NOT NULL, effect VARCHAR NOT NULL, power INTEGER NOT NULL, CONSTRAINT ck_consumable_effect CHECK (effect IN ('heal', 'buff', 'cleanse', 'essence') AND power >= 0 AND power <= 100))"))
            seed_essences(conn)
            conn.execute(User.__table__.insert().values(id=owner_id, email='test@example.com', username='test'))
            conn.execute(Adventurer.__table__.insert().values(id=hero_id, owner=owner_id, name='Legacy'))
            conn.execute(OwnedConsumable.__table__.insert().values(adventurer_id=hero_id, consumable_slug='essence-ice', quantity=5))
            conn.execute(AbsorbedEssence.__table__.insert().values(adventurer_id=hero_id, slot=1, essence_slug='essence-ice'))
            conn.execute(QuestTemplate.__table__.insert().values(slug='old', name='Old', difficulty=1, min_encounters=2, max_encounters=2, region='Old', journey=old))
            conn.execute(RaidRotation.__table__.insert().values(key='saved', seed='seed', resets_at=datetime(2030, 1, 1), snapshot=old))
            upgrade(conn)
            conn.execute(Ability.__table__.update().where(Ability.slug == 'orb-dark-evasion').values(power=55))
            upgrade(conn)
            assert conn.scalar(select(func.count()).select_from(EssenceDefinition).where(EssenceDefinition.active.is_(True))) == 9
            assert conn.scalar(select(func.count()).select_from(OrbOutcome)) == 18
            assert conn.scalar(select(OwnedConsumable.quantity)) == 5
            assert conn.scalar(select(AbsorbedEssence.essence_slug)) == 'essence-ice'
            assert conn.scalar(select(RaidRotation.snapshot)) == old
            drops = conn.scalar(select(QuestTemplate.journey))['encounter_groups']['loot_tiers'][0]['drops']
            assert {d['consumable_slug'] for d in drops} == {'essence-dark', 'evasion-orb', 'strike-orb'}
            assert conn.scalar(select(Ability.power).where(Ability.slug == 'orb-dark-evasion')) == 55
    finally:
        engine.dispose()


def test_orb_use_requires_character_ownership(client):
    from uuid import uuid4
    id = prepared(client)
    client.post('/api/auth/logout')
    client.post('/api/auth/register', json={'username': 'orb_other_' + uuid4().hex[:12], 'password': 'test-password-1234'})
    assert use(client, id).status_code in (403, 404)
    with SessionLocal() as db:
        assert db.get(OwnedConsumable, (UUID(id), 'evasion-orb')).quantity == 3
