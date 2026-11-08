"""Per-request context shared by logging, outgoing calls and security checks."""

import re
import uuid
from contextvars import ContextVar

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def current_request_id() -> str | None:
    return request_id_var.get()
