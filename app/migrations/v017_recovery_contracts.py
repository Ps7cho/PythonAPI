def upgrade(conn):
    from app.models import RecoveryContract, RecoveredSoul
    RecoveryContract.__table__.create(conn, checkfirst=True)
    RecoveredSoul.__table__.create(conn, checkfirst=True)
