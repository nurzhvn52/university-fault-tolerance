"""Authentication and authorization shared by the services.

Tokens are JWT (HS256) signed by the auth service and verified locally by every service, so a
request needs no call to another service to be authenticated: when the auth service or the
database is down, users who already hold a token keep working. The price is that a token
cannot be revoked before it expires (a short lifetime limits that).

Every denial fails closed: no token, a bad or expired token, the wrong role or somebody
else's data all end in 401 or 403 with an audit event, never in a default allow.
"""

import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request

from common.config import Settings
from common.context import actor_var
from common.telemetry import AUTH_DENIED, RATE_LIMITED

ISSUER = "uft-auth"
AUDIENCE = "uft"
ROLES = ("student", "registrar", "admin", "service")
STAFF = ("registrar", "admin")
MIN_SECRET_LENGTH = 32
NO_JWT_SERVICES = {"bank", "alert_sink"}
KNOWN_DEFAULTS = {"", "changeme", "secret", "uft_local_only", "password"}


class ConfigError(Exception):
    """The configuration is unsafe; the service refuses to start."""


class TokenError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str
    student_id: int | None = None
    jti: str = ""

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF


# Used when security is off (midterm configuration): everything is allowed.
OPEN = Principal(sub="anonymous", role="admin")


def _read_secrets(file: str, value: str) -> list[str]:
    if file:
        text = Path(file).read_text(encoding="utf-8")
        return [line.strip() for line in text.splitlines() if line.strip()]
    return [value] if value else []


def load_secrets(settings: Settings) -> list[str]:
    return _read_secrets(settings.jwt_secret_file, settings.jwt_secret)


def load_bank_api_key(settings: Settings) -> str:
    keys = _read_secrets(settings.bank_api_key_file, settings.bank_api_key)
    return keys[0] if keys else ""


def validate_config(settings: Settings) -> list[str]:
    """Refuses unsafe configuration when security is on; returns the signing secrets."""
    if not settings.security:
        return []
    if settings.service_name in NO_JWT_SERVICES:
        if settings.service_name == "bank" and not load_bank_api_key(settings):
            raise ConfigError("security is on but the bank has no API key")
        return []
    if settings.service_name == "payment" and not load_bank_api_key(settings):
        raise ConfigError("security is on but payment has no bank API key")
    secrets = load_secrets(settings)
    if not secrets:
        raise ConfigError("security is on but no JWT secret is configured")
    for secret in secrets:
        if len(secret) < MIN_SECRET_LENGTH or secret.lower() in KNOWN_DEFAULTS:
            raise ConfigError(
                f"JWT secret is weak (need at least {MIN_SECRET_LENGTH} characters, not a default)"
            )
    if settings.jwt_ttl_s > 3600:
        raise ConfigError("access tokens must not live longer than an hour")
    return secrets


def issue_token(
    secrets: list[str],
    *,
    sub: str,
    role: str,
    student_id: int | None = None,
    ttl_s: int = 900,
    now: float | None = None,
) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    issued = int(now if now is not None else time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": sub,
        "role": role,
        "iat": issued,
        "exp": issued + ttl_s,
        "jti": uuid.uuid4().hex,
    }
    if student_id is not None:
        claims["sid"] = student_id
    return jwt.encode(claims, secrets[0], algorithm="HS256")


def decode_token(secrets: list[str], token: str) -> Principal:
    options = {"require": ["exp", "iat", "sub", "role", "jti"]}
    expired = False
    for secret in secrets:
        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                issuer=ISSUER,
                audience=AUDIENCE,
                options=options,
            )
        except jwt.ExpiredSignatureError:
            expired = True
            continue
        except jwt.PyJWTError:
            continue
        if claims["role"] not in ROLES:
            raise TokenError("invalid_token")
        student_id = claims.get("sid")
        if claims["role"] == "student" and not isinstance(student_id, int):
            raise TokenError("invalid_token")
        return Principal(claims["sub"], claims["role"], student_id, claims["jti"])
    raise TokenError("expired_token" if expired else "invalid_token")


class ServiceTokens:
    """Short-lived token a service presents when it calls another service."""

    def __init__(self, settings: Settings, secrets: list[str]) -> None:
        self._settings = settings
        self._secrets = secrets
        self._token = ""
        self._expires = 0.0

    def get(self) -> str:
        now = time.time()
        if now > self._expires - 30:
            ttl = self._settings.service_token_ttl_s
            self._token = issue_token(
                self._secrets,
                sub=f"svc:{self._settings.service_name}",
                role="service",
                ttl_s=ttl,
                now=now,
            )
            self._expires = now + ttl
        return self._token


def client_address(request: Request) -> str:
    """The caller as seen by the gateway (it sets X-Forwarded-For)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def deny(
    request: Request,
    status: int,
    reason: str,
    *,
    actor: str | None = None,
    resource: str | None = None,
    scope: str | None = None,
) -> HTTPException:
    """Records a denied request (metric and audit event) and returns the exception to raise."""
    service = request.app.state.settings.service_name
    if status == 429:
        RATE_LIMITED.labels(service, scope or reason).inc()
    else:
        AUTH_DENIED.labels(service, reason).inc()
    who = actor or client_address(request)
    request.app.state.audit.record(
        "rate_limit" if status == 429 else "auth.denied",
        "denied",
        actor=who,
        resource=resource or request.url.path,
        detail={"reason": reason, "status": status, "method": request.method},
        dedup=(reason, who, request.url.path if status == 429 else resource),
    )
    headers = {"WWW-Authenticate": "Bearer"} if status == 401 else {}
    if status == 429:
        headers["Retry-After"] = "1"
    return HTTPException(status, reason, headers=headers)


async def authenticate(request: Request) -> Principal:
    state = request.app.state
    if not state.settings.security:
        return OPEN
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise deny(request, 401, "missing_token")
    try:
        principal = decode_token(state.jwt_secrets, token)
    except TokenError as exc:
        raise deny(request, 401, exc.reason) from exc
    actor_var.set(principal.sub)
    cost = 1.0 / state.settings.staff_rate_factor if principal.is_staff else 1.0
    if principal.role != "service" and not state.limiter.allow(principal.sub, cost):
        raise deny(request, 429, "rate_limited", actor=principal.sub, scope="principal")
    request.state.principal = principal
    return principal


Authenticated = Annotated[Principal, Depends(authenticate)]


async def staff_only(request: Request, principal: Authenticated) -> Principal:
    if not principal.is_staff:
        raise deny(request, 403, "forbidden_role", actor=principal.sub)
    return principal


Staff = Annotated[Principal, Depends(staff_only)]


def authorize_student(request: Request, principal: Principal, student_id: int) -> None:
    """Students may act only on their own data; staff and services on anybody's."""
    if principal.role == "student" and principal.student_id != student_id:
        raise deny(
            request, 403, "forbidden_owner", actor=principal.sub, resource=f"student:{student_id}"
        )


async def student_access(request: Request, student_id: int, principal: Authenticated) -> Principal:
    authorize_student(request, principal, student_id)
    return principal


StudentAccess = Annotated[Principal, Depends(student_access)]
