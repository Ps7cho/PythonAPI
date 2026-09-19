import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import get_settings
from app.models import Adventurer, LoginAccount, LoginSession, User


router = APIRouter(prefix="/api/auth", tags=["authentication"])
hasher = PasswordHasher()
dummy_hash = hasher.hash(secrets.token_urlsafe(32))
COOKIE = "game_session"
SESSION_SECONDS = 86400


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=12, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        return value.lower()


def same_origin(request: Request, public_login: bool = False):
    origin = request.headers.get("origin")
    if public_login and origin in get_settings().public_client_origins:
        return
    if origin and origin != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Cross-origin requests are not allowed.")


def token_from(request: Request):
    authorization = request.headers.get("authorization", "")
    if authorization:
        scheme, _, token = authorization.partition(" ")
        return token if scheme.lower() == "bearer" else None
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        same_origin(request)
    return request.cookies.get(COOKIE)


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = token_from(request)
    session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest()) if token else None
    if session is None or session.expires_at <= datetime.utcnow():
        raise HTTPException(401, "Login required.", headers={"WWW-Authenticate": "Bearer"})
    return db.get(User, session.user_id)


def own_adventurer(db: Session, adventurer_id: UUID, user: User):
    hero = db.get(Adventurer, adventurer_id)
    if hero is None or hero.owner != user.id:
        raise HTTPException(404, "Adventurer not found.")
    return hero


def own_state(user_id: str, user: User):
    if user_id != str(user.id):
        raise HTTPException(403, "This state belongs to another account.")


def issue_session(db, user, response, request):
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                        expires_at=datetime.utcnow() + timedelta(seconds=SESSION_SECONDS)))
    db.commit()
    if request.headers.get('x-client-auth') != 'bearer':
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"user": {"id": str(user.id), "username": user.username, "account_type": user.account_type,
                      "statistics": user.statistics or {}},
            "access_token": token, "token_type": "bearer", "expires_in": SESSION_SECONDS}


@router.post("/register", status_code=201)
def register(payload: Credentials, response: Response, request: Request, db: Session = Depends(get_db)):
    same_origin(request, public_login=True)
    user_id = uuid4()
    user = User(id=user_id, username=payload.username, email=f"{user_id}@accounts.invalid",
                account_type="player")
    password_hash = hasher.hash(payload.password)
    try:
        db.add(user)
        db.flush()
        db.add(LoginAccount(username=payload.username, user_id=user.id, password_hash=password_hash))
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Username is already taken.")
    return issue_session(db, user, response, request)


@router.post("/login")
def login(payload: Credentials, response: Response, request: Request, db: Session = Depends(get_db)):
    same_origin(request, public_login=True)
    account = db.scalar(select(LoginAccount).where(LoginAccount.username == payload.username).with_for_update())
    now = datetime.utcnow()
    if account and account.locked_until and account.locked_until > now:
        raise HTTPException(429, "Too many attempts. Try again in five minutes.")
    try:
        hasher.verify(account.password_hash if account else dummy_hash, payload.password)
    except VerificationError:
        if account:
            account.failed_attempts += 1
            if account.failed_attempts >= 5:
                account.locked_until = now + timedelta(minutes=5)
                account.failed_attempts = 0
            db.commit()
        raise HTTPException(401, "Invalid username or password.")
    if account is None:
        raise HTTPException(401, "Invalid username or password.")
    account.failed_attempts = 0
    account.locked_until = None
    if hasher.check_needs_rehash(account.password_hash):
        account.password_hash = hasher.hash(payload.password)
    return issue_session(db, db.get(User, account.user_id), response, request)


@router.get("/me")
def me(response: Response, user: User = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    return {"id": str(user.id), "username": user.username, "account_type": user.account_type,
            "statistics": user.statistics or {}}


@router.get("/account")
def account(response: Response, user: User = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    return {"id": str(user.id), "username": user.username, "account_type": user.account_type,
            "statistics": user.statistics or {}}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = token_from(request)
    if token:
        session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
        if session:
            db.delete(session)
            db.commit()
    response.delete_cookie(COOKIE, httponly=True, samesite="strict")
