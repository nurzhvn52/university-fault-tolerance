"""Auth users, audit trail and least-privilege grants for the service roles.

Each service connects with its own database role (svc_<service>, created when the cluster is
initialised) that may use only the tables of its own schema and append to ``audit.events``.
The grants are skipped for roles that do not exist (the single-node baseline stack).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-08 18:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SERVICES = ["student", "payment", "records", "timetable", "auth"]


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS auth")
    op.execute("CREATE SCHEMA IF NOT EXISTS audit")
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("password_hash", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=True),
        sa.Column("failed_attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("username", name=op.f("uq_users_username")),
        schema="auth",
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("service", sa.String(32)),
        sa.Column("instance", sa.String(64)),
        sa.Column("request_id", sa.String(64)),
        sa.Column("actor", sa.String(128)),
        sa.Column("action", sa.String(48)),
        sa.Column("outcome", sa.String(16)),
        sa.Column("resource", sa.String(160)),
        sa.Column("detail", sa.JSON()),
        schema="audit",
    )
    op.create_index("ix_events_ts", "events", ["ts"], schema="audit")
    op.create_index("ix_events_action_outcome", "events", ["action", "outcome"], schema="audit")
    for service in SERVICES:
        op.execute(
            f"""
            DO $$ BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'svc_{service}') THEN
                GRANT USAGE ON SCHEMA {service} TO svc_{service};
                GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {service}
                  TO svc_{service};
                GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {service} TO svc_{service};
                GRANT USAGE ON SCHEMA audit TO svc_{service};
                GRANT INSERT ON audit.events TO svc_{service};
                GRANT USAGE ON SEQUENCE audit.events_id_seq TO svc_{service};
              END IF;
            END $$
            """
        )


def downgrade() -> None:
    op.drop_table("events", schema="audit")
    op.drop_table("users", schema="auth")
    op.execute("DROP SCHEMA IF EXISTS audit")
    op.execute("DROP SCHEMA IF EXISTS auth")
