"""Tokens for the load generator and the attack probes.

The lab holds the signing secret (it is the test harness), so it mints tokens directly; the
password login itself is exercised by the security scenarios.
"""

import time

from common.config import Settings
from common.security import issue_token, load_secrets

TOKEN_TTL_S = 4 * 3600


class Tokens:
    def __init__(self, settings: Settings) -> None:
        # Without a security layer nothing checks the tokens; any key will do.
        self._secrets = load_secrets(settings) or ["f" * 48]
        self._students: dict[int, str] = {}
        self._admin: str | None = None

    def student(self, student_id: int) -> str:
        if student_id not in self._students:
            self._students[student_id] = issue_token(
                self._secrets,
                sub=f"lab-student-{student_id}",
                role="student",
                student_id=student_id,
                ttl_s=TOKEN_TTL_S,
            )
        return self._students[student_id]

    def attacker(self, student_id: int) -> str:
        """A valid token of the attacking student, with its own subject so that throttling
        it does not touch the legitimate load generator."""
        return issue_token(
            self._secrets,
            sub="lab-attacker",
            role="student",
            student_id=student_id,
            ttl_s=TOKEN_TTL_S,
        )

    def admin(self) -> str:
        if self._admin is None:
            self._admin = issue_token(
                self._secrets, sub="lab-admin", role="admin", ttl_s=TOKEN_TTL_S
            )
        return self._admin

    def expired_student(self, student_id: int) -> str:
        """A correctly signed token whose lifetime ended long ago."""
        return issue_token(
            self._secrets,
            sub=f"lab-student-{student_id}",
            role="student",
            student_id=student_id,
            ttl_s=900,
            now=time.time() - 7200,
        )

    def wrong_key(self, role: str = "admin") -> str:
        return issue_token(["x" * 48], sub="attacker", role=role, ttl_s=900)
