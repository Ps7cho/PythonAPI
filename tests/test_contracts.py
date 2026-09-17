from uuid import UUID, uuid4
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Adventurer, RecoveryContract, RecoveredSoul


def hero(client, hp=None):
    value = client.post('/api/adventurers', json={'name': uuid4().hex}).json()
    if hp:
        with SessionLocal.begin() as db:
            db.get(Adventurer, UUID(value['id'])).health = hp
    return value['id']


def act(client, e, actor=None, action='wait'):
    response = client.post('/api/encounters/'+e['id']+'/actions', json={
        'actor_id': actor or e['participants'][0]['id'], 'expected_turn': e['turn'], 'action': action})
    assert response.status_code == 200, response.text
    return response.json()


def defeat(client, count=1):
    ids = [hero(client, 1), hero(client, 1)]
    e = client.post('/api/encounters', json={'adventurer_ids': ids, 'encounter_count': count}).json()
    e = act(client, e, e['participants'][0]['id'])
    assert e['state'] == 'player_turn'
    e = act(client, e, e['participants'][1]['id'])
    assert e['state'] == 'defeat'
    with SessionLocal() as db:
        contract = db.scalar(select(RecoveryContract).where(RecoveryContract.source_run_id == UUID(e['quest']['id'])))
        contract_id = str(contract.id)
    return contract_id, e


def win(client, encounter):
    for _ in range(40):
        if encounter['state'] != 'player_turn':
            break
        encounter = act(client, encounter, action='attack')
    assert encounter['state'] == 'victory'
    return encounter


def test_defeat_preserves_profiles_and_full_route_awards_once(client):
    contract_id, failed = defeat(client, 3)
    contract = client.get('/api/contracts/'+contract_id).json()
    assert contract['status'] == 'open' and contract['encounter_count'] == 3
    assert len(contract['fallen']) == 2
    assert all(h['rank'] and h['abilities'] for h in contract['fallen'])
    rescuer = hero(client)
    accepted = client.post('/api/contracts/'+contract_id+'/accept', json={'adventurer_ids': [rescuer]})
    assert accepted.status_code == 201, accepted.text
    e = accepted.json()
    second_rescuer = hero(client)
    second = client.post('/api/contracts/'+contract_id+'/accept', json={'adventurer_ids': [second_rescuer]})
    assert second.status_code == 201, second.text
    assert client.get('/api/contracts/'+contract_id).json()['active_party_count'] == 2
    for stage in range(3):
        e = win(client, e)
        souls = [s for s in client.get('/api/adventurers/'+rescuer).json()['inventory'] if isinstance(s, dict) and s.get('type') == 'soul']
        assert len(souls) == (2 if stage == 2 else 0)
        if stage < 2:
            e = client.post('/api/encounters/'+e['id']+'/continue', json={}).json()
    assert client.get('/api/contracts/'+contract_id).json()['status'] == 'completed'
    assert client.post('/api/contracts/'+contract_id+'/accept', json={'adventurer_ids': [hero(client)]}).status_code == 409
    assert {s['fallen']['id'] for s in souls} == {h['id'] for h in contract['fallen']}
    assert client.get('/api/encounters/'+e['id']).json()['combat_log'][-1]['messages'][-1].startswith(''+e['participants'][0]['name']+' recovers the soul')
    with SessionLocal() as db:
        assert len(list(db.scalars(select(RecoveredSoul).where(RecoveredSoul.contract_id == UUID(contract_id))))) == 2


def test_failed_attempt_and_early_return_release_contract(client):
    contract_id, _ = defeat(client, 3)
    rescuer = hero(client, 1)
    e = client.post('/api/contracts/'+contract_id+'/accept', json={'adventurer_ids': [rescuer]}).json()
    e = act(client, e)
    assert e['state'] == 'defeat'
    assert client.get('/api/contracts/'+contract_id).json()['status'] == 'open'
    rescuer = hero(client)
    e = client.post('/api/contracts/'+contract_id+'/accept', json={'adventurer_ids': [rescuer]}).json()
    e = win(client, e)
    response = client.post('/api/encounters/'+e['id']+'/continue', json={'return_to_village': True})
    assert response.status_code == 200, response.text
    assert client.get('/api/contracts/'+contract_id).json()['status'] == 'open'
    assert not any(isinstance(i,dict) and i.get('type')=='soul' for i in client.get('/api/adventurers/'+rescuer).json()['inventory'])


def test_rescue_attempt_fallen_stack_on_original_contract(client):
    contract_id, _ = defeat(client, 3)
    rescuer = hero(client, 1)
    attempt = client.post('/api/contracts/'+contract_id+'/accept', json={'adventurer_ids': [rescuer]}).json()
    failed = act(client, attempt)
    assert failed['state'] == 'defeat'
    with SessionLocal() as db:
        contracts = list(db.scalars(select(RecoveryContract)))
    assert len(contracts) == 1
    saved = client.get('/api/contracts/'+contract_id).json()
    assert saved['status'] == 'open'
    assert len(saved['fallen']) == 3
    assert rescuer in {profile['id'] for profile in saved['fallen']}


def test_party_contract_requires_consent_and_distributes_unique_souls(client):
    contract_id, _ = defeat(client)
    ids = [hero(client), hero(client)]
    party = client.post('/api/parties', json={'adventurer_id': ids[0], 'name': 'Recovery team'}).json()
    path = '/api/parties/'+party['id']
    client.post('/api/parties/join', json={'adventurer_id': ids[1], 'code': party['invite']['code']})
    selected = client.post(path+'/selection', json={'contract_id': contract_id}).json()
    revision = selected['selection_revision']
    assert selected['selection']['contract_id'] == contract_id
    assert client.post(path+'/encounters', json={'selection_revision': revision}).status_code == 409
    for hero_id in ids:
        assert client.post(path+'/ready', json={'adventurer_id': hero_id, 'ready': True, 'selection_revision': revision}).status_code == 200
    e = client.post(path+'/encounters', json={'selection_revision': revision}).json()
    for _ in range(30):
        if e['state'] != 'player_turn':
            break
        for actor in list(e['pending_actor_ids']):
            if e['state'] == 'player_turn':
                e = act(client, e, actor, 'attack')
    assert e['state'] == 'victory'
    for hero_id in ids:
        souls = [s for s in client.get('/api/adventurers/'+hero_id).json()['inventory'] if isinstance(s, dict) and s.get('type') == 'soul']
        assert len(souls) == 1


def test_bulletin_notification_reaches_other_accounts_without_polling(client, monkeypatch):
    import threading
    from app import live
    from tests.test_live import account
    token, _, _ = account(client)
    lock = threading.Lock()
    original = live.village_snapshot
    def snapshot(*args, **kwargs):
        with lock:
            return original(*args, **kwargs)
    monkeypatch.setattr(live, 'village_snapshot', snapshot)
    with client.websocket_connect('/api/village/live') as socket:
        socket.send_json({'token': token})
        socket.receive_json()
        with lock:
            # An unrelated account posts a contract; this observer owns none of it.
            client.post('/api/auth/register', json={'username': 'fallen_'+uuid4().hex[:16], 'password': 'test-contract-password'})
            contract_id, _ = defeat(client)
        for _ in range(10):
            message = socket.receive_json()
            if any(row['id'] == contract_id for row in message['data']['contracts']):
                break
        else:
            raise AssertionError('Contract was not pushed to the observer.')
        assert client.post('/api/contracts/'+contract_id+'/accept', json={'adventurer_ids':[message['data']['adventurers'][0]['id']]}).status_code in (403,404)
