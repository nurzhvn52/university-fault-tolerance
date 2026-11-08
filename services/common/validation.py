"""Input validation shared by the request models."""

from pydantic import BaseModel, model_validator

from common.config import get_settings

# Academic term such as 2026-FALL; course code such as CSE316; idempotency keys and request
# ids are opaque tokens, never free text that could end up in a log line or a query.
TERM_PATTERN = r"^\d{4}-[A-Z]{3,8}$"
COURSE_CODE_PATTERN = r"^[A-Z0-9-]{2,16}$"
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]{8,128}$"
MAX_AMOUNT = 10_000_000


class StrictInput(BaseModel):
    """Request body that rejects fields it does not define when security is on (a client
    sending ``role`` or ``student_id`` it was never meant to set gets 422, not silence)."""

    @model_validator(mode="before")
    @classmethod
    def _reject_unknown_fields(cls, data):
        if isinstance(data, dict) and get_settings().security:
            unknown = set(data) - set(cls.model_fields)
            if unknown:
                raise ValueError(f"unknown fields: {sorted(unknown)}")
        return data
