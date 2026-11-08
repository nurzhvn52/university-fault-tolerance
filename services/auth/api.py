"""Auth API: login (password -> access token) and the identity of the caller."""

import asyncio
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, text

from auth.models import User
from auth.passwords import DUMMY_HASH, verify_password
from common.db import SessionDep
from common.security import Authenticated, client_address, issue_token
from common.telemetry import AUTH_DENIED, RATE_LIMITED

logger = logging.getLogger("auth.api")
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._@-]+$")
    password: str = Field(min_length=1, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str


class MeOut(BaseModel):
    sub: str
    role: str
    student_id: int | None


# One UPDATE counts the failure and starts the lock when the limit is reached, so concurrent
# guesses against one account cannot slip past the counter.
_FAILURE = text(
    """
    UPDATE auth.users SET
      failed_attempts = CASE WHEN failed_attempts + 1 >= :max THEN 0 ELSE failed_attempts + 1 END,
      locked_until = CASE WHEN failed_attempts + 1 >= :max
                          THEN now() + make_interval(secs => :lock) ELSE locked_until END
    WHERE id = :id
    RETURNING locked_until > now() AS locked
    """
)


def _audit(request: Request, outcome: str, username: str, reason: str | None = None) -> None:
    detail = {"client": client_address(request)}
    if reason:
        detail["reason"] = reason
    request.app.state.audit.record(
        "auth.login",
        outcome,
        actor=username,
        resource="auth:login",
        detail=detail,
        dedup=(outcome, reason, username) if outcome != "ok" else None,
    )


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, session: SessionDep) -> TokenOut:
    settings = request.app.state.settings
    service = settings.service_name
    user = await session.scalar(select(User).where(User.username == body.username))
    now = datetime.now(UTC)
    if user is not None and user.locked_until is not None and user.locked_until > now:
        RATE_LIMITED.labels(service, "login_lock").inc()
        _audit(request, "denied", body.username, "account_locked")
        retry = max(int((user.locked_until - now).total_seconds()) + 1, 1)
        raise HTTPException(429, "account_locked", headers={"Retry-After": str(retry)})
    stored = user.password_hash if user is not None else DUMMY_HASH
    valid = await asyncio.to_thread(verify_password, body.password, stored)
    if user is None or not valid:
        if user is not None:
            params = {
                "id": user.id,
                "max": settings.login_max_failures,
                "lock": settings.login_lock_s,
            }
            locked = (await session.execute(_FAILURE, params)).scalar()
            await session.commit()
            if locked:
                logger.warning("account_locked", extra={"username": body.username})
        AUTH_DENIED.labels(service, "invalid_credentials").inc()
        _audit(request, "denied", body.username, "invalid_credentials")
        raise HTTPException(401, "invalid_credentials", headers={"WWW-Authenticate": "Bearer"})
    if user.failed_attempts:
        user.failed_attempts = 0
        await session.commit()
    token = issue_token(
        request.app.state.jwt_secrets,
        sub=user.username,
        role=user.role,
        student_id=user.student_id,
        ttl_s=settings.jwt_ttl_s,
    )
    _audit(request, "ok", user.username)
    return TokenOut(access_token=token, expires_in=settings.jwt_ttl_s, role=user.role)


@router.get("/me", response_model=MeOut)
async def me(principal: Authenticated) -> MeOut:
    return MeOut(sub=principal.sub, role=principal.role, student_id=principal.student_id)
