"""Committed encounter changes fan out through PostgreSQL, not a game timer."""
import asyncio
import hashlib
import logging
import select as sockets
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

import psycopg2
from fastapi import APIRouter, HTTPException, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import SessionLocal, engine
from app.models import Adventurer, Encounter, LoginSession, Party, PartyMember, QuestRun, User

router = APIRouter()
CHANNEL = 'encounter_changes'


class Hub:
    def __init__(self):
        self.clients = {}
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.thread = None

    def subscribe(self, topic):
        subscriber = (asyncio.get_running_loop(), asyncio.Event())
        with self.lock:
            self.clients.setdefault(topic, set()).add(subscriber)
        return subscriber

    def unsubscribe(self, topic, subscriber):
        with self.lock:
            self.clients.get(topic, set()).discard(subscriber)
            if not self.clients.get(topic):
                self.clients.pop(topic, None)

    def publish(self, topic=None):
        with self.lock:
            targets = list(self.clients.get(topic, ())) if topic else [s for group in self.clients.values() for s in group]
            if topic == 'bulletin':
                targets = [s for key, group in self.clients.items() if key.startswith('village:') for s in group]
        for loop, signal in targets:
            try:
                loop.call_soon_threadsafe(signal.set)
            except RuntimeError:
                pass  # A disconnected subscriber's loop may already be closed.

    def listen(self):
        url = make_url(get_settings().database_url)
        host = url.host or ''
        # LISTEN requires a session connection, not Neon's transaction pooler.
        if host.endswith('.neon.tech'):
            host = host.replace('-pooler.', '.')
        kwargs = dict(url.query)
        kwargs.update(host=host, port=url.port or 5432, dbname=url.database,
                      user=url.username, password=url.password, connect_timeout=5)
        while not self.stop.is_set():
            conn = None
            try:
                conn = psycopg2.connect(**kwargs)
                conn.autocommit = True
                with conn.cursor() as cursor:
                    cursor.execute('LISTEN ' + CHANNEL)
                self.ready.set()
                self.publish()  # Reconcile changes missed during a listener outage.
                while not self.stop.is_set():
                    if sockets.select([conn], [], [], 1)[0]:
                        conn.poll()
                        while conn.notifies:
                            self.publish(conn.notifies.pop(0).payload)
            except (psycopg2.Error, OSError):
                self.ready.clear()
                logging.getLogger(__name__).warning('Encounter notification connection lost; reconnecting.')
                self.stop.wait(2)
            finally:
                if conn is not None:
                    conn.close()


hub = Hub()


@asynccontextmanager
async def lifespan(app):
    hub.stop.clear()
    if engine.dialect.name == 'postgresql':
        hub.thread = threading.Thread(target=hub.listen, daemon=True)
        hub.thread.start()
    else:
        hub.ready.set()
    try:
        yield
    finally:
        hub.stop.set()
        if hub.thread:
            await asyncio.to_thread(hub.thread.join, 7)
        hub.ready.clear()


def notify_run(db, run_id):
    notify_topic(db, str(run_id))


def notify_topic(db, topic):
    if db.bind.dialect.name == 'postgresql':
        db.execute(text('SELECT pg_notify(:channel, :topic)'), {'channel': CHANNEL, 'topic': topic})
    else:
        db.info.setdefault('live_topics', set()).add(topic)


@event.listens_for(Session, 'before_flush')
def collect_village_changes(db, context, instances):
    changed = set(db.new) | set(db.deleted) | {row for row in db.dirty if db.is_modified(row, include_collections=False)}
    rows = db.info.setdefault('village_rows', set())
    owners = db.info.setdefault('village_owners', set())
    for row in changed:
        if isinstance(row, User):
            rows.add(row)
        elif isinstance(row, Adventurer):
            rows.add(row)
            if row.owner:
                owners.add(row.owner)
        elif isinstance(row, (Party, QuestRun)) or getattr(row, 'party_id', None) or getattr(row, 'quest_run_id', None):
            rows.add(row)
        if getattr(row, 'adventurer_id', None):
            hero = db.get(Adventurer, row.adventurer_id)
            if hero:
                owners.add(hero.owner)
                rows.add(hero)
        if isinstance(row, LoginSession) and row in db.deleted:
            owners.add(row.user_id)


@event.listens_for(Session, 'after_flush_postexec')
def notify_village_changes(db, context):
    owners = db.info.pop('village_owners', set())
    parties, heroes = set(), set()
    for row in db.info.pop('village_rows', set()):
        if isinstance(row, User):
            owners.add(row.id)
        if isinstance(row, Adventurer):
            owners.add(row.owner)
            heroes.add(row.id)
        if isinstance(row, Party):
            parties.add(row.id)
        if getattr(row, 'party_id', None):
            parties.add(row.party_id)
        if getattr(row, 'quest_run_id', None):
            run = db.get(QuestRun, row.quest_run_id)
            if run:
                parties.add(run.party_id)
    if heroes:
        parties.update(db.scalars(select(PartyMember.party_id).where(PartyMember.adventurer_id.in_(heroes))))
    if parties:
        owners.update(db.scalars(select(Adventurer.owner).join(PartyMember, PartyMember.adventurer_id == Adventurer.id)
                                 .where(PartyMember.party_id.in_(parties))))
    for owner in owners:
        if owner:
            notify_topic(db, 'village:' + str(owner))


@event.listens_for(Session, 'after_commit')
def committed(db):
    for topic in db.info.pop('live_topics', set()):
        hub.publish(topic)


@event.listens_for(Session, 'after_rollback')
def rolled_back(db):
    db.info.pop('live_topics', None)
    db.info.pop('village_rows', None)
    db.info.pop('village_owners', None)


def authorized_snapshot(token, encounter_id, follow=False):
    from app.encounter_service import snapshot
    with SessionLocal() as db:
        session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest()) if token else None
        if session is None or session.expires_at <= datetime.utcnow():
            raise PermissionError()
        encounter = db.get(Encounter, encounter_id)
        if encounter is None or not db.scalar(select(Adventurer.id).where(
                Adventurer.id.in_([UUID(p['id']) for p in encounter.participants]),
                Adventurer.owner == session.user_id).limit(1)):
            raise PermissionError()
        run = db.get(QuestRun, encounter.quest_run_id)
        if follow and run.current_stage == 'complete':
            encounter = db.scalar(select(Encounter).where(Encounter.quest_run_id == run.id).order_by(Encounter.created_at.desc()).limit(1))
        elif follow and run.current_stage != str(encounter.id):
            encounter = db.get(Encounter, UUID(run.current_stage))
        return str(run.id), snapshot(encounter)


@router.websocket('/api/encounters/{encounter_id}/live')
async def live_encounter(ws: WebSocket, encounter_id: UUID):
    await stream_state(ws, lambda token: authorized_snapshot(token, encounter_id, True), 'encounter')


def village_snapshot(token, adventurer_id=None, identity_only=False):
    from app.contracts import bulletin
    from app.main import my_adventurers, adventurer_details
    from app.parties import my_parties
    with SessionLocal() as db:
        session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest()) if token else None
        if session is None or session.expires_at <= datetime.utcnow():
            raise PermissionError()
        if identity_only:
            return 'village:' + str(session.user_id), None
        user = db.get(User, session.user_id)
        data = {'account': {'id': str(user.id), 'username': user.username, 'statistics': user.statistics or {}},
                'adventurers': my_adventurers(db, user), 'parties': my_parties(Response(), db, user),
                'contracts': bulletin(db)}
        if adventurer_id:
            data['adventurer'] = adventurer_details(adventurer_id, db, user)
        return 'village:' + str(user.id), data


@router.websocket('/api/village/live')
async def live_village(ws: WebSocket, adventurer_id: UUID | None = None):
    await stream_state(ws, lambda token: village_snapshot(token, adventurer_id), 'village',
                       lambda token: village_snapshot(token, adventurer_id, True))


async def stream_state(ws, snapshot_for, message_type, identify=None):
    origin = ws.headers.get('origin')
    own_origin = ('https' if ws.url.scheme == 'wss' else 'http') + '://' + ws.url.netloc
    if origin and origin not in [own_origin, *get_settings().public_client_origins]:
        await ws.close(code=4403)
        return
    await ws.accept()
    subscriber = None
    topic = None
    try:
        auth = await asyncio.wait_for(ws.receive_json(), 5)
        if not isinstance(auth, dict):
            raise PermissionError()
        token = auth.get('token') or (ws.cookies.get('game_session') if origin == own_origin else None)
        if not isinstance(token, str) or len(token) > 512:
            raise PermissionError()
        topic, _ = await asyncio.to_thread(identify or snapshot_for, token)
        subscriber = hub.subscribe(topic)
        if not await asyncio.to_thread(hub.ready.wait, 8):
            await ws.close(code=1013)
            return
        signal = subscriber[1]
        while True:
            signal.clear()
            _, data = await asyncio.to_thread(snapshot_for, token)
            await ws.send_json({'type': message_type, 'data': jsonable_encoder(data)})
            while not signal.is_set():
                try:
                    await asyncio.wait_for(signal.wait(), 25)
                except asyncio.TimeoutError:
                    # Heartbeats detect dead sockets; they do not query combat or advance it.
                    await ws.send_json({'type': 'ping'})
                    reply = await asyncio.wait_for(ws.receive_json(), 10)
                    if not isinstance(reply, dict) or reply.get('type') != 'pong':
                        raise PermissionError()
    except (PermissionError, HTTPException, ValueError, KeyError, TypeError):
        await ws.close(code=4401)
    except asyncio.TimeoutError:
        await ws.close(code=4408)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if subscriber:
            hub.unsubscribe(topic, subscriber)
