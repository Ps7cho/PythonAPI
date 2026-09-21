import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote
from urllib.request import Request as URLRequest, urlopen

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.config import get_settings
from app.models import Adventurer, DiscordIdentity, DiscordOAuthState, LoginAccount, LoginSession, User
from app.request_limits import limiter, source


router = APIRouter(prefix="/api/auth", tags=["authentication"])
hasher = PasswordHasher()
dummy_hash = hasher.hash(secrets.token_urlsafe(32))
COOKIE = "game_session"
SESSION_SECONDS = 86400
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
DISCORD_TOKEN_URL = "https://discord.com/api/oauth2/token"
DISCORD_ME_URL = "https://discord.com/api/users/@me"
DISCORD_STATE_SECONDS = 600


class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_]+$")
    password: str = Field(min_length=12, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize(cls, value):
        return value.lower()


class DiscordPasswordReset(BaseModel):
    discord_access_token: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=128)


def discord_settings():
    settings = get_settings()
    if not settings.discord_client_id or not settings.discord_client_secret:
        raise HTTPException(503, "Discord authentication is not configured.")
    return settings


def discord_state(db: Session, purpose: str, user_id=None) -> str:
    now = datetime.utcnow()
    db.query(DiscordOAuthState).filter(DiscordOAuthState.expires_at <= now).delete()
    value = secrets.token_urlsafe(48)
    db.add(DiscordOAuthState(state=value, purpose=purpose, user_id=user_id,
                             expires_at=now + timedelta(seconds=DISCORD_STATE_SECONDS)))
    db.commit()
    return value


def discord_authorize_url(settings, state: str, redirect_uri: str) -> str:
    return DISCORD_AUTHORIZE_URL + "?" + urlencode({
        "client_id": settings.discord_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "identify email",
        "state": state,
    })


def discord_request(url: str, data=None, token: str | None = None):
    body = urlencode(data).encode() if data is not None else None
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = URLRequest(url, data=body, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=8) as response:
            import json
            return json.loads(response.read())
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        raise HTTPException(502, "Discord authentication could not be completed.") from exc


def consume_discord_state(db: Session, value: str, purpose: str | None = None):
    state = db.get(DiscordOAuthState, value)
    if state is None or (purpose is not None and state.purpose != purpose) or state.expires_at <= datetime.utcnow():
        raise HTTPException(400, "Invalid or expired Discord authorization state.")
    db.delete(state)
    db.flush()
    return state


def discord_user(code: str, redirect_uri: str):
    settings = discord_settings()
    token_data = discord_request(DISCORD_TOKEN_URL, {
        "client_id": settings.discord_client_id,
        "client_secret": settings.discord_client_secret,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    })
    token = token_data.get("access_token")
    if not isinstance(token, str):
        raise HTTPException(502, "Discord did not return an access token.")
    profile = discord_request(DISCORD_ME_URL, token=token)
    discord_id = profile.get("id")
    if not isinstance(discord_id, str) or not discord_id:
        raise HTTPException(502, "Discord returned an invalid account.")
    return profile, token


def same_origin(request: Request, public_login: bool = False):
    origin = request.headers.get("origin")
    # Community clients authenticate with an explicit bearer-client marker and
    # receive a token instead of a browser cookie. Their hosting origin is not
    # part of the server's trust boundary.
    if public_login and request.headers.get("x-client-auth", "").lower() == "bearer":
        return
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


def issue_session(db, user, response, request):
    token = create_session(db, user)
    db.commit()
    if request.headers.get('x-client-auth', '').lower() != 'bearer':
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict")
    response.headers["Cache-Control"] = "no-store"
    return {"user": {"id": str(user.id), "username": user.username, "account_type": user.account_type,
                      "statistics": user.statistics or {},
                      "discord_linked": db.scalar(select(DiscordIdentity).where(DiscordIdentity.user_id == user.id)) is not None},
            "access_token": token, "token_type": "bearer", "expires_in": SESSION_SECONDS}


def create_session(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(LoginSession(token_hash=hashlib.sha256(token.encode()).hexdigest(), user_id=user.id,
                        expires_at=datetime.utcnow() + timedelta(seconds=SESSION_SECONDS)))
    return token


def frontend_discord_redirect(settings, token: str, expires_in: int = SESSION_SECONDS, **values):
    target = settings.discord_frontend_redirect_uri
    fragment = {"discord_access_token": token, "expires_in": str(expires_in), **values}
    return RedirectResponse(target + "#" + urlencode(fragment), status_code=303)


def frontend_discord_status(settings, **values):
    return RedirectResponse(settings.discord_frontend_redirect_uri + "#" + urlencode(values), status_code=303)


@router.post("/register", status_code=201)
def register(payload: Credentials, response: Response, request: Request, db: Session = Depends(get_db)):
    same_origin(request, public_login=True)
    limiter.hit(('register', source(request)), 5, 3600)
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
    limiter.hit(('login-source', source(request)), 30, 60)
    limiter.hit(('login-account-source', source(request), payload.username), 5, 60)
    account = db.scalar(select(LoginAccount).where(LoginAccount.username == payload.username).with_for_update())
    try:
        hasher.verify(account.password_hash if account else dummy_hash, payload.password)
    except VerificationError:
        raise HTTPException(401, "Invalid username or password.")
    if account is None:
        raise HTTPException(401, "Invalid username or password.")
    account.failed_attempts = 0
    account.locked_until = None
    if hasher.check_needs_rehash(account.password_hash):
        account.password_hash = hasher.hash(payload.password)
    return issue_session(db, db.get(User, account.user_id), response, request)


@router.get("/discord/login")
def discord_login(purpose: str = "login", db: Session = Depends(get_db)):
    if purpose not in {"login", "recovery"}:
        raise HTTPException(400, "Unsupported Discord authentication flow.")
    settings = discord_settings()
    state = discord_state(db, purpose)
    return RedirectResponse(discord_authorize_url(settings, state, settings.discord_login_redirect_uri), status_code=307)


@router.post("/discord/link")
def discord_link(db: Session = Depends(get_db), user: User = Depends(current_user)):
    settings = discord_settings()
    state = discord_state(db, "link", user.id)
    return {"authorization_url": discord_authorize_url(settings, state, settings.discord_redirect_uri)}


@router.get("/discord/callback")
def discord_callback(code: str | None = None, state: str | None = None, error: str | None = None,
                    db: Session = Depends(get_db)):
    if error or not code or not state:
        raise HTTPException(400, "Discord authorization was cancelled.")
    settings = discord_settings()
    oauth_state = consume_discord_state(db, state, "link")
    profile, _ = discord_user(code, settings.discord_redirect_uri)
    identity = db.get(DiscordIdentity, profile["id"])
    if identity is not None and identity.user_id != oauth_state.user_id:
        raise HTTPException(409, "That Discord account is already linked.")
    if identity is None:
        identity = DiscordIdentity(discord_id=profile["id"], user_id=oauth_state.user_id)
        db.add(identity)
    identity.username = profile.get("username")
    identity.email = profile.get("email")
    identity.updated_at = datetime.utcnow()
    db.commit()
    if getattr(settings, "discord_frontend_redirect_uri", None):
        return frontend_discord_status(settings, discord_linked="1")
    return {"linked": True, "discord_id": identity.discord_id}


@router.get("/discord/login/callback")
def discord_login_callback(request: Request, response: Response,
                          code: str | None = None, state: str | None = None, error: str | None = None,
                          db: Session = Depends(get_db)):
    if error or not code or not state:
        raise HTTPException(400, "Discord authorization was cancelled.")
    settings = discord_settings()
    oauth_state = consume_discord_state(db, state)
    if oauth_state.purpose not in {"login", "recovery"}:
        raise HTTPException(400, "Invalid Discord authentication flow.")
    profile, discord_token = discord_user(code, settings.discord_login_redirect_uri)
    identity = db.get(DiscordIdentity, profile["id"])
    if identity is None:
        raise HTTPException(404, "No game account is linked to that Discord account.")
    user = db.get(User, identity.user_id)
    if oauth_state.purpose == "recovery":
        db.commit()
        return frontend_discord_redirect(settings, discord_token, recovery="1") if getattr(settings, "discord_frontend_redirect_uri", None) else {"discord_access_token": discord_token}
    if getattr(settings, "discord_frontend_redirect_uri", None):
        token = create_session(db, user)
        db.commit()
        return frontend_discord_redirect(settings, token)
    return issue_session(db, user, response, request)


@router.post("/password/reset-with-discord")
def reset_password_with_discord(payload: DiscordPasswordReset, request: Request,
                                db: Session = Depends(get_db)):
    limiter.hit(('discord-password-reset', source(request)), 10, 3600)
    discord_settings()
    profile = discord_request(DISCORD_ME_URL, token=payload.discord_access_token)
    discord_id = profile.get("id")
    identity = db.get(DiscordIdentity, discord_id) if isinstance(discord_id, str) else None
    if identity is None:
        raise HTTPException(404, "That Discord account is not linked to a game account.")
    account = db.scalar(select(LoginAccount).where(LoginAccount.user_id == identity.user_id).with_for_update())
    if account is None:
        raise HTTPException(404, "That game account cannot use password recovery.")
    account.password_hash = hasher.hash(payload.new_password)
    account.failed_attempts = 0
    account.locked_until = None
    db.query(LoginSession).filter(LoginSession.user_id == identity.user_id).delete(synchronize_session=False)
    db.commit()
    return {"reset": True}


@router.delete("/discord/link", status_code=204)
def unlink_discord(db: Session = Depends(get_db), user: User = Depends(current_user)):
    identity = db.scalar(select(DiscordIdentity).where(DiscordIdentity.user_id == user.id).with_for_update())
    if identity is None:
        raise HTTPException(404, "No Discord account is linked.")
    db.delete(identity)
    db.commit()


@router.get("/me")
def me(response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    return {"id": str(user.id), "username": user.username, "account_type": user.account_type,
            "statistics": user.statistics or {},
            "discord_linked": db.scalar(select(DiscordIdentity).where(DiscordIdentity.user_id == user.id)) is not None}


@router.get("/account")
def account(response: Response, db: Session = Depends(get_db), user: User = Depends(current_user)):
    response.headers["Cache-Control"] = "no-store"
    return {"id": str(user.id), "username": user.username, "account_type": user.account_type,
            "statistics": user.statistics or {},
            "discord_linked": db.scalar(select(DiscordIdentity).where(DiscordIdentity.user_id == user.id)) is not None}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = token_from(request)
    if token:
        session = db.get(LoginSession, hashlib.sha256(token.encode()).hexdigest())
        if session:
            db.delete(session)
            db.commit()
    response.delete_cookie(COOKIE, httponly=True, samesite="strict")
