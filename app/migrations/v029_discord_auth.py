"""Add Discord identity links and short-lived OAuth state records."""

from app.models import DiscordIdentity, DiscordOAuthState


def upgrade(conn):
    DiscordIdentity.__table__.create(conn, checkfirst=True)
    DiscordOAuthState.__table__.create(conn, checkfirst=True)
