from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select
from app.database import SessionLocal
from app.models import Adventurer, AuctionListing, Weapon, OwnedConsumable, EquippedWeapon
from app.consumables import grant


def player(client):
    username = 'market_' + uuid4().hex[:16]
    assert client.post('/api/auth/register', json={'username': username, 'password': 'market-password-123'}).status_code == 201
    hero = client.post('/api/adventurers', json={'name': username}).json()['id']
    with SessionLocal.begin() as db:
        db.get(Adventurer, UUID(hero)).gold = 1000
        grant(db, UUID(hero), 'healing-potion', 5)
    return username, hero


def login(client, who):
    assert client.post('/api/auth/login', json={'username': who[0], 'password': 'market-password-123'}).status_code == 200


def create(client, who, **extra):
    response = client.post('/api/auction-house', json={'adventurer_id': who[1], 'mode': 'fixed',
        'consumable_slug': 'healing-potion', 'quantity': 2, 'price': 100, **extra})
    assert response.status_code == 201, response.text
    return response.json()['id']


def balances(*people):
    with SessionLocal() as db:
        return [(db.get(Adventurer, UUID(p[1])).gold,
                 db.get(OwnedConsumable, (UUID(p[1]), 'healing-potion')).quantity) for p in people]


def expire(listing):
    with SessionLocal.begin() as db:
        db.get(AuctionListing, UUID(listing)).expires_at = datetime.utcnow() - timedelta(seconds=1)


def test_fixed_sale_conserves_gold_stock_and_rejects_replays(client):
    seller = player(client)
    listing = create(client, seller)
    assert balances(seller) == [(1000, 3)]
    assert client.post('/api/auction-house/'+listing+'/buy', json={'adventurer_id': seller[1]}).status_code == 409
    buyer = player(client)
    url = '/api/auction-house/'+listing+'/buy'
    assert client.post(url, json={'adventurer_id': buyer[1]}).status_code == 200
    assert balances(seller, buyer) == [(1100, 3), (900, 7)]
    assert client.post(url, json={'adventurer_id': buyer[1]}).status_code == 409
    assert balances(seller, buyer) == [(1100, 3), (900, 7)]


def test_bidding_refunds_and_expiry_awards_exactly_once(client):
    seller = player(client)
    listing = create(client, seller, mode='auction')
    first = player(client)
    url = '/api/auction-house/'+listing
    assert client.post(url+'/bid', json={'adventurer_id': first[1], 'amount': 100}).status_code == 200
    assert client.post(url+'/bid', json={'adventurer_id': first[1], 'amount': 120}).status_code == 200
    assert balances(first) == [(880, 5)]
    second = player(client)
    assert client.post(url+'/bid', json={'adventurer_id': second[1], 'amount': 120}).status_code == 409
    assert client.post(url+'/bid', json={'adventurer_id': second[1], 'amount': 150}).status_code == 200
    assert balances(first, second) == [(1000, 5), (850, 5)]
    login(client, seller)
    assert client.post(url+'/cancel').status_code == 409
    expire(listing)
    for _ in range(2):
        assert client.get('/api/auction-house').status_code == 200
    assert balances(seller, first, second) == [(1150, 3), (1000, 5), (850, 7)]
    with SessionLocal() as db:
        assert db.get(AuctionListing, UUID(listing)).status == 'sold'
    login(client, first)
    assert client.post(url+'/bid', json={'adventurer_id': first[1], 'amount': 200}).status_code == 409


def test_cancellation_expiry_and_permissions(client):
    seller = player(client)
    cancelled = create(client, seller)
    other = player(client)
    assert client.post('/api/auction-house/'+cancelled+'/cancel').status_code == 404
    assert client.post('/api/auction-house/'+cancelled+'/buy', json={'adventurer_id': seller[1]}).status_code == 404
    login(client, seller)
    assert client.post('/api/auction-house/'+cancelled+'/cancel').status_code == 200
    assert client.post('/api/auction-house/'+cancelled+'/cancel').status_code == 409
    listing = create(client, seller, mode='auction')
    expire(listing)
    assert client.get('/api/auction-house?mine=true').status_code == 200
    assert balances(seller) == [(1000, 5)]
    client.post('/api/auth/logout')
    assert client.get('/api/auction-house').status_code == 401


def test_weapon_escrow_keeps_id_and_stats(client):
    seller = player(client)
    with SessionLocal() as db:
        weapon_id = str(db.get(EquippedWeapon, UUID(seller[1])).weapon_id)
    body = {'adventurer_id': seller[1], 'mode': 'fixed', 'weapon_id': weapon_id, 'price': 20}
    assert client.post('/api/auction-house', json=body).status_code == 409
    assert client.post('/api/adventurers/'+seller[1]+'/weapon', json={'weapon_id': None}).status_code == 200
    response = client.post('/api/auction-house', json=body)
    assert response.status_code == 201
    listing = response.json()['id']
    with SessionLocal.begin() as db:
        assert db.get(Weapon, UUID(weapon_id)) is None
        from app.weapons import grant_starter_weapon
        grant_starter_weapon(db, db.get(Adventurer, UUID(seller[1])))
        assert db.scalar(select(Weapon.id).where(Weapon.adventurer_id == UUID(seller[1]))) is None
    assert client.post('/api/auction-house', json=body).status_code == 409
    buyer = player(client)
    assert client.post('/api/auction-house/'+listing+'/buy', json={'adventurer_id': buyer[1]}).status_code == 200
    with SessionLocal() as db:
        weapon = db.get(Weapon, UUID(weapon_id))
        assert weapon.adventurer_id == UUID(buyer[1])
        assert weapon.name == 'Training Sword' and weapon.base_damage == 10


def test_invalid_stock_price_and_funds_do_not_mutate(client):
    seller = player(client)
    for extra in [{'price': 0}, {'quantity': 6}, {'price': 1.5}, {'quantity': -1}]:
        result = client.post('/api/auction-house', json={'adventurer_id': seller[1], 'mode': 'fixed',
            'consumable_slug': 'healing-potion', 'price': 20, **extra})
        assert result.status_code in (409, 422)
    assert balances(seller) == [(1000, 5)]
    listing = create(client, seller, price=1001)
    buyer = player(client)
    assert client.post('/api/auction-house/'+listing+'/buy', json={'adventurer_id': buyer[1]}).status_code == 409
    assert balances(seller, buyer) == [(1000, 3), (1000, 5)]
    assert client.post('/api/encounters', json={'adventurer_ids': [buyer[1]]}).status_code == 201
    result = client.post('/api/auction-house', json={'adventurer_id': buyer[1], 'mode': 'auction',
        'consumable_slug': 'healing-potion', 'price': 10})
    assert result.status_code == 409


def test_failed_delivery_rolls_back_gold_and_listing(client, monkeypatch):
    import pytest
    seller = player(client)
    listing = create(client, seller)
    buyer = player(client)
    def fail(*args):
        raise RuntimeError('Simulated delivery failure')
    monkeypatch.setattr('app.auction_house.deliver', fail)
    with pytest.raises(RuntimeError, match='Simulated delivery'):
        client.post('/api/auction-house/'+listing+'/buy', json={'adventurer_id': buyer[1]})
    assert balances(seller, buyer) == [(1000, 3), (1000, 5)]
    with SessionLocal() as db:
        row = db.get(AuctionListing, UUID(listing))
        assert row.status == 'open' and row.buyer_id is None


def test_listing_migration_is_repeatable():
    from sqlalchemy import create_engine, inspect
    from app.migrations.v022_auction_house import upgrade
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        upgrade(conn)
        upgrade(conn)
        assert inspect(conn).has_table('auction_listings')
    engine.dispose()
