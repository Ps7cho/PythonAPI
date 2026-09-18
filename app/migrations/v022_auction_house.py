from app.models import AuctionListing


def upgrade(conn):
    AuctionListing.__table__.create(conn, checkfirst=True)
