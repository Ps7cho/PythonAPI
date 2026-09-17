from copy import deepcopy
from uuid import UUID
from sqlalchemy import select
from app.database import SessionLocal
from app.models import Adventurer, Encounter, Enemy, Ability, EnemyAbility, WeaponType, QuestRun
from app.enemy_content import ENEMY_SPECS
from app.enemies import roll_enemy, seed_enemies
from app.seed_abilities import seed_enemy_abilities
from app.progression import award_experience, level_floor


def hero(client, name='Scout', level=1):
    identifier = client.post('/api/adventurers',json={'name':name}).json()['id']
    if level > 1:
        with SessionLocal.begin() as db:
            award_experience(db,db.get(Adventurer,UUID(identifier)),level_floor(level))
    return identifier


def test_one_rank_requires_acknowledgement_and_two_ranks_cannot_override(client):
    h = hero(client)
    body={'adventurer_ids':[h],'template_slug':'bronze-contract'}
    warning=client.post('/api/encounters',json=body)
    assert warning.status_code==409
    assert warning.json()['detail']['code']=='rank_warning'
    assert client.get('/api/adventurers/'+h).json()['active_encounter_id'] is None
    assert client.post('/api/encounters',json={**body,'accept_rank_risk':'true'}).status_code==422
    accepted=client.post('/api/encounters',json={**body,'accept_rank_risk':True})
    assert accepted.status_code==201,accepted.text
    assert accepted.json()['quest']['journey']['under_rank_adventurers']==[h]
    assert accepted.json()['quest']['journey']['rank_warning_accepted'] is True
    another=hero(client)
    for accept in (False,True):
        result=client.post('/api/encounters',json={'adventurer_ids':[another],'template_slug':'silver-contract','accept_rank_risk':accept})
        assert result.status_code==409
        assert 'within one rank' in result.json()['detail']


def test_party_start_checks_lowest_member_and_accepts_warning(client):
    leader=hero(client,'Leader',10)
    low=hero(client,'Low rank')
    party=client.post('/api/parties',json={'adventurer_id':leader,'name':'Risk party'}).json()
    assert client.post('/api/parties/join',json={'adventurer_id':low,'code':party['invite']['code']}).status_code==200
    path='/api/parties/'+party['id']+'/encounters'
    def approve(template, risk=False):
        selection=client.post(path.replace('/encounters','/selection'),json={'template_slug':template,'accept_rank_risk':risk}).json()
        body={'selection_revision':selection['selection_revision']}
        for member in (leader,low):
            assert client.post(path.replace('/encounters','/ready'),json={**body,'adventurer_id':member,'ready':True}).status_code==200
        return body
    denied=client.post(path,json=approve('silver-contract',True))
    assert denied.status_code==409  # The leader being Bronze does not bypass an Iron member.
    warning=client.post(path,json=approve('bronze-contract'))
    assert warning.status_code==409 and 'Low rank' in warning.json()['detail']['message']
    result=client.post(path,json=approve('bronze-contract',True))
    assert result.status_code==201,result.text
    assert result.json()['quest']['journey']['under_rank_adventurers']==[low]


def test_all_new_enemies_have_usable_abilities_and_live_in_route_pools(client):
    catalog=client.get('/api/enemies').json()
    new={row[0] for row in ENEMY_SPECS}
    assert len(new)==19 and new <= {e['slug'] for e in catalog}
    templates=client.get('/api/quest-templates').json()
    pool={s for t in templates for stage in t['journey'].get('stages',[]) for group in stage['groups'] for s in group}
    assert new <= pool
    for slug in new:
        h=hero(client,slug)
        e=client.post('/api/encounters',json={'adventurer_ids':[h],'enemy_slug':slug}).json()
        assert e['enemies'][0]['abilities']
        result=client.post('/api/encounters/'+e['id']+'/actions',json={'actor_id':h,'expected_turn':1,'action':'wait'})
        assert result.status_code==200,result.text
        monster=result.json()['enemies'][0]
        assert result.json()['participants'][0]['hp']<100 or monster.get('guarding')


def test_priest_heals_its_team_not_players(client):
    h=hero(client)
    e=client.post('/api/encounters',json={'adventurer_ids':[h],'enemy_slug':'priest'}).json()
    with SessionLocal.begin() as db:
        encounter=db.get(Encounter,UUID(e['id']))
        ally=roll_enemy(db.get(Enemy,'undead'),1);ally.position=1
        ally.state={**ally.state,'hp':1,'max_hp':100};encounter.enemy_instances.append(ally)
        # This fixture changes the starting roster after creation; re-plan it.
        priest = encounter.enemy_instances[0]
        priest.state = {key: value for key, value in priest.state.items() if key != 'intent'}
        encounter.participants=[{**encounter.participants[0],'hp':50}]
    result=client.post('/api/encounters/'+e['id']+'/actions',json={'actor_id':h,'expected_turn':1,'action':'wait'}).json()
    assert result['enemies'][1]['hp']==13
    assert result['participants'][0]['hp']<=50
    assert any('Dark Mending' in message for message in result['events'])


def test_firemancer_hits_multiple_players_and_thrower_uses_thrown_tags(client):
    ids=[hero(client),hero(client)]
    e=client.post('/api/encounters',json={'adventurer_ids':ids,'enemy_slug':'firemancer'}).json()
    # Select only its area attack to prove multi-target execution independently of weighting.
    with SessionLocal.begin() as db:
        saved=db.get(Encounter,UUID(e['id'])).enemy_instances[0]
        saved.state={**saved.state,'abilities':[a for a in saved.state['abilities'] if a['ability']['catalog_slug']=='flame_burst']}
        saved.state = {key: value for key, value in saved.state.items() if key != 'intent'}
    for h in ids:
        result=client.post('/api/encounters/'+e['id']+'/actions',json={'actor_id':h,'expected_turn':1,'action':'wait'})
        assert result.status_code==200,result.text
    assert all(p['hp']<100 for p in result.json()['participants'])
    thrower=hero(client)
    e=client.post('/api/encounters',json={'adventurer_ids':[thrower],'enemy_slug':'axe-thrower'}).json()
    weapon=e['enemies'][0]['equipped_weapon']
    assert weapon['weapon_type']=='throwing-axe' and {'thrown','ranged'} <= set(weapon['tags'])
    result=client.post('/api/encounters/'+e['id']+'/actions',json={'actor_id':thrower,'expected_turn':1,'action':'wait'}).json()
    assert result['participants'][0]['hp']<100
    assert any('Hurled Axe' in message for message in result['events'])


def test_expanded_seeds_are_idempotent_and_preserve_balance_edits(client):
    with SessionLocal.begin() as db:
        priest=db.get(Enemy,'priest');old=deepcopy(priest.stat_ranges)
        priest.stat_ranges={**old,'hp':{'min':101,'max':101}}
        before=len(db.scalars(select(EnemyAbility)).all())
    try:
        seed_enemies();seed_enemy_abilities();seed_enemy_abilities()
        with SessionLocal() as db:
            assert db.get(Enemy,'priest').stat_ranges['hp']=={'min':101,'max':101}
            assert len(db.scalars(select(EnemyAbility)).all())==before
    finally:
        with SessionLocal.begin() as db: db.get(Enemy,'priest').stat_ranges=old
