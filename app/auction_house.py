"""Escrow-backed fixed-price sales and timed auctions."""
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, or_
from sqlalchemy.orm import Session

from app.auth import current_user, own_adventurer
from app.database import get_db
from app.models import AuctionListing, Adventurer, User, Weapon, EquippedWeapon, OwnedConsumable, GameEvent
from app.consumables import grant
from app.journeys import active_adventure
from app.weapons import serialize_weapon

router = APIRouter(prefix='/api/auction-house', tags=['auction house'])


class ListingRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    adventurer_id: UUID
    mode: Literal['fixed', 'auction']
    weapon_id: UUID | None = None
    consumable_slug: str | None = Field(default=None, min_length=1, max_length=100)
    quantity: int = Field(default=1, ge=1, le=9999, strict=True)
    price: int = Field(ge=1, le=100000000, strict=True)
    duration_hours: int = Field(default=24, ge=1, le=168, strict=True)

    @model_validator(mode='after')
    def valid_item(self):
        if (self.weapon_id is None) == (self.consumable_slug is None):
            raise ValueError('Choose one weapon or consumable stack.')
        if self.weapon_id and self.quantity != 1:
            raise ValueError('Weapons are listed one at a time.')
        return self


class ActorRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    adventurer_id: UUID


class BidRequest(ActorRequest):
    amount: int = Field(ge=1, le=100000000, strict=True)


def lock_heroes(db, ids):
    return {h.id: h for h in db.scalars(select(Adventurer).where(Adventurer.id.in_(set(ids)))
        .order_by(Adventurer.id).with_for_update().execution_options(populate_existing=True))}


def in_village(db, hero):
    if not hero.is_alive or hero.health <= 0 or active_adventure(db, [hero.id]):
        raise HTTPException(409, 'Use the auction house with a living adventurer in the village.')


def lock_listing(db, listing_id):
    listing = db.scalar(select(AuctionListing).where(AuctionListing.id == listing_id)
        .with_for_update().execution_options(populate_existing=True))
    if listing is None:
        raise HTTPException(404, 'Listing not found.')
    return listing


def deliver(db, listing, hero):
    db.info.setdefault('village_owners', set()).add(hero.owner)
    item = listing.item
    if item['item_type'] == 'weapon':
        db.add(Weapon(id=UUID(item['id']), adventurer_id=hero.id, name=item['name'],
            weapon_type_slug=item['weapon_type'], base_damage=item['base_damage'], required_rank=item['required_rank']))
    else:
        grant(db, hero.id, item['slug'], listing.quantity)


def close(db, listing, status):
    listing.status = status
    listing.closed_at = datetime.utcnow()
    db.add(GameEvent(event_type='auction_' + status, payload={
        'listing_id': str(listing.id), 'seller_id': str(listing.seller_id),
        'buyer_id': str(listing.buyer_id) if listing.buyer_id else None,
        'price': listing.bid or listing.price, 'quantity': listing.quantity}))


def settle(db, listing):
    if listing.status != 'open' or listing.expires_at > datetime.utcnow():
        return False
    ids = [listing.seller_id] + ([listing.bidder_id] if listing.bidder_id else [])
    heroes = lock_heroes(db, ids)
    if listing.bidder_id:
        heroes[listing.seller_id].gold += listing.bid
        listing.buyer_id = listing.bidder_id
        deliver(db, listing, heroes[listing.bidder_id])
        close(db, listing, 'sold')
    else:
        deliver(db, listing, heroes[listing.seller_id])
        close(db, listing, 'expired')
    return True


def serialize(db, listing, user):
    seller = db.get(Adventurer, listing.seller_id)
    bidder = db.get(Adventurer, listing.bidder_id) if listing.bidder_id else None
    return dict(id=str(listing.id), seller=seller.name, seller_id=str(seller.id),
        mine=seller.owner == user.id, my_bid=bool(bidder and bidder.owner == user.id),
        mode=listing.mode, status=listing.status, item=listing.item, quantity=listing.quantity,
        price=listing.price, bid=listing.bid, minimum_bid=max(listing.price, listing.bid + 1),
        expires_at=listing.expires_at.replace(tzinfo=timezone.utc).isoformat())


@router.get('')
def listings(mine: bool = False, offset: int = 0, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if offset < 0:
        raise HTTPException(422, 'Offset cannot be negative.')
    settle_expired(db)
    query = select(AuctionListing)
    if mine:
        owned = select(Adventurer.id).where(Adventurer.owner == user.id)
        query = query.where(or_(AuctionListing.seller_id.in_(owned), AuctionListing.bidder_id.in_(owned), AuctionListing.buyer_id.in_(owned)))
    else:
        query = query.where(AuctionListing.status == 'open')
    rows = db.scalars(query.order_by(AuctionListing.created_at.desc(), AuctionListing.id).offset(offset).limit(51)).all()
    return {'listings': [serialize(db, row, user) for row in rows[:50]], 'has_more': len(rows) > 50}


@router.post('', status_code=201)
def create_listing(payload: ListingRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    own_adventurer(db, payload.adventurer_id, user)
    hero = lock_heroes(db, [payload.adventurer_id])[payload.adventurer_id]
    in_village(db, hero)
    if payload.weapon_id:
        weapon = db.scalar(select(Weapon).where(Weapon.id == payload.weapon_id).with_for_update())
        if weapon is None or weapon.adventurer_id != hero.id:
            raise HTTPException(409, 'You do not own that weapon.')
        if db.scalar(select(EquippedWeapon).where(EquippedWeapon.weapon_id == weapon.id)):
            raise HTTPException(409, 'Unequip the weapon before listing it.')
        item = {**serialize_weapon(weapon), 'item_type': 'weapon'}
        db.delete(weapon)
    else:
        stock = db.scalar(select(OwnedConsumable).where(OwnedConsumable.adventurer_id == hero.id,
            OwnedConsumable.consumable_slug == payload.consumable_slug).with_for_update())
        if stock is None or stock.quantity < payload.quantity:
            raise HTTPException(409, 'Not enough owned items.')
        item = {'item_type': 'consumable', 'slug': stock.consumable_slug, 'name': stock.definition.name,
                'description': stock.definition.description}
        stock.quantity -= payload.quantity
    listing = AuctionListing(seller_id=hero.id, mode=payload.mode, item=item, quantity=payload.quantity,
        price=payload.price, expires_at=datetime.utcnow() + timedelta(hours=payload.duration_hours))
    db.add(listing)
    db.flush()
    db.add(GameEvent(event_type='auction_listed', payload={'listing_id': str(listing.id), 'adventurer_id': str(hero.id)}))
    db.commit()
    return serialize(db, listing, user)


def open_listing(db, listing_id):
    listing = lock_listing(db, listing_id)
    if settle(db, listing):
        db.commit()
    if listing.status != 'open':
        raise HTTPException(409, 'This listing has closed.')
    return listing


@router.post('/{listing_id}/buy')
def buy(listing_id: UUID, payload: ActorRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    own_adventurer(db, payload.adventurer_id, user)
    listing = open_listing(db, listing_id)
    heroes = lock_heroes(db, [listing.seller_id, payload.adventurer_id])
    seller, buyer = heroes[listing.seller_id], heroes[payload.adventurer_id]
    if settle(db, listing):
        db.commit()
        raise HTTPException(409, 'This listing has closed.')
    in_village(db, buyer)
    if listing.mode != 'fixed' or seller.owner == user.id:
        raise HTTPException(409, 'Only another account can buy a fixed-price listing.')
    if buyer.gold < listing.price:
        raise HTTPException(409, 'Not enough gold.')
    buyer.gold -= listing.price
    seller.gold += listing.price
    listing.buyer_id = buyer.id
    deliver(db, listing, buyer)
    close(db, listing, 'sold')
    db.commit()
    return serialize(db, listing, user)


@router.post('/{listing_id}/bid')
def bid(listing_id: UUID, payload: BidRequest, db: Session = Depends(get_db), user: User = Depends(current_user)):
    own_adventurer(db, payload.adventurer_id, user)
    listing = open_listing(db, listing_id)
    ids = [listing.seller_id, payload.adventurer_id] + ([listing.bidder_id] if listing.bidder_id else [])
    heroes = lock_heroes(db, ids)
    buyer = heroes[payload.adventurer_id]
    if settle(db, listing):
        db.commit()
        raise HTTPException(409, 'This listing has closed.')
    in_village(db, buyer)
    if listing.mode != 'auction' or heroes[listing.seller_id].owner == user.id:
        raise HTTPException(409, 'Only another account can bid on a timed auction.')
    if payload.amount < max(listing.price, listing.bid + 1):
        raise HTTPException(409, 'Bid must exceed the current bid and meet the starting price.')
    refund = listing.bid if listing.bidder_id == buyer.id else 0
    if buyer.gold + refund < payload.amount:
        raise HTTPException(409, 'Not enough gold.')
    if listing.bidder_id:
        heroes[listing.bidder_id].gold += listing.bid
    buyer.gold -= payload.amount
    listing.bidder_id, listing.bid = buyer.id, payload.amount
    db.add(GameEvent(event_type='auction_bid', payload={'listing_id': str(listing.id),
        'adventurer_id': str(buyer.id), 'amount': payload.amount}))
    db.commit()
    return serialize(db, listing, user)


@router.post('/{listing_id}/cancel')
def cancel(listing_id: UUID, db: Session = Depends(get_db), user: User = Depends(current_user)):
    listing = open_listing(db, listing_id)
    seller = lock_heroes(db, [listing.seller_id])[listing.seller_id]
    if seller.owner != user.id:
        raise HTTPException(404, 'Listing not found.')
    if listing.bidder_id:
        raise HTTPException(409, 'An auction with bids cannot be cancelled.')
    deliver(db, listing, seller)
    close(db, listing, 'cancelled')
    db.commit()
    return serialize(db, listing, user)


def settle_expired(db):
    # Each listing uses its own transaction, also safe across multiple workers.
    expired = list(db.scalars(select(AuctionListing.id).where(
        AuctionListing.status == 'open', AuctionListing.expires_at <= datetime.utcnow())))
    for listing_id in expired:
        listing = lock_listing(db, listing_id)
        settle(db, listing)
        db.commit()


def expiry_worker(stop):
    import logging
    from app.database import SessionLocal
    while not stop.wait(15):
        try:
            with SessionLocal() as db:
                settle_expired(db)
        except Exception:
            logging.getLogger(__name__).exception('Auction expiry settlement failed; retrying.')
