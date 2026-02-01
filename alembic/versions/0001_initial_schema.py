"""Initial AsyncGate schema."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create base tables and enums."""
    # Define enum types - SQLAlchemy will create them automatically when used
    principalkind = sa.Enum(
        "agent",
        "worker",
        "service",
        "system",
        "human",
        name="principalkind",
    )
    taskstatus = sa.Enum(
        "queued",
        "leased",
        "succeeded",
        "failed",
        "canceled",
        name="taskstatus",
    )
    receipttype = sa.Enum(
        "task.assigned",
        "task.accepted",
        "task.completed",
        "task.failed",
        "task.canceled",
        "task.retry_scheduled",
        "task.result_ready",
        "task.progress",
        "lease.expired",
        "receipt.acknowledged",
        "system.anomaly",
        name="receipttype",
    )

    op.create_table(
        "auth_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=True),
        sa.Column("oauth_provider", sa.String(length=255), nullable=True),
        sa.Column("oauth_subject", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.UniqueConstraint("email", name="uq_auth_users_email"),
    )
    op.create_index(
        "idx_auth_oauth_provider_subject",
        "auth_users",
        ["oauth_provider", "oauth_subject"],
        unique=True,
        postgresql_where=sa.text("oauth_provider IS NOT NULL"),
    )

    op.create_table(
        "auth_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("auth_users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("key_prefix", sa.String(length=64), nullable=False),
        sa.Column("key_hash", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("scopes", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("key_hash", name="uq_auth_api_keys_hash"),
    )
    op.create_index("idx_auth_api_keys_user", "auth_api_keys", ["user_id"])
    op.create_index("idx_auth_api_keys_prefix", "auth_api_keys", ["key_prefix"])

    op.create_table(
        "tasks",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.String(length=255), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_by_kind", principalkind, nullable=False),
        sa.Column("created_by_id", sa.String(length=255), nullable=False),
        sa.Column("created_by_instance_id", sa.String(length=255), nullable=True),
        sa.Column("requirements", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", taskstatus, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("retry_backoff_seconds", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_eligible_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_outcome", sa.String(length=50), nullable=True),
        sa.Column("result_data", postgresql.JSONB, nullable=True),
        sa.Column("result_error", postgresql.JSONB, nullable=True),
        sa.Column("result_artifacts", postgresql.JSONB, nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("asyncgate_instance", sa.String(length=255), nullable=True),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_task_idempotency"),
    )
    op.create_index("idx_tasks_type", "tasks", ["type"])
    op.create_index(
        "idx_tasks_leasable",
        "tasks",
        ["tenant_id", "status", "next_eligible_at", "priority", "created_at"],
    )
    op.create_index(
        "idx_tasks_tenant_status",
        "tasks",
        ["tenant_id", "status", "created_at"],
    )
    op.create_index("idx_tasks_instance", "tasks", ["asyncgate_instance"])

    op.create_table(
        "leases",
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("worker_id", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("tenant_id", "task_id", name="uq_lease_task"),
    )
    op.create_index("idx_leases_expires", "leases", ["expires_at"])
    op.create_index("idx_leases_worker", "leases", ["tenant_id", "worker_id"])

    op.create_table(
        "receipts",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("receipt_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("receipt_type", receipttype, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("from_kind", principalkind, nullable=False),
        sa.Column("from_id", sa.String(length=255), nullable=False),
        sa.Column("to_kind", principalkind, nullable=False),
        sa.Column("to_id", sa.String(length=255), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("schedule_id", sa.String(length=255), nullable=True),
        sa.Column("parents", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("body", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("hash", sa.String(length=64), nullable=True),
        sa.Column("asyncgate_instance", sa.String(length=255), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "hash", name="uq_receipt_hash"),
    )
    op.create_index(
        "idx_receipts_to",
        "receipts",
        ["tenant_id", "to_kind", "to_id", "created_at"],
    )
    op.create_index(
        "idx_receipts_task",
        "receipts",
        ["tenant_id", "task_id", "created_at"],
    )
    op.create_index("idx_receipts_hash", "receipts", ["tenant_id", "hash"])
    op.create_index(
        "idx_receipts_parents_gin",
        "receipts",
        ["parents"],
        postgresql_using="gin",
    )

    op.create_table(
        "progress",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("progress", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "task_id"],
            ["tasks.tenant_id", "tasks.task_id"],
            ondelete="CASCADE",
        ),
    )

    op.create_table(
        "audit_events",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("event_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_audit_task",
        "audit_events",
        ["tenant_id", "task_id", "created_at"],
    )
    op.create_index(
        "idx_audit_type",
        "audit_events",
        ["tenant_id", "event_type", "created_at"],
    )

    op.create_table(
        "relationships",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("principal_kind", principalkind, primary_key=True),
        sa.Column("principal_id", sa.String(length=255), primary_key=True),
        sa.Column("principal_instance_id", sa.String(length=255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sessions_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index(
        "idx_relationships_last_seen",
        "relationships",
        ["tenant_id", "last_seen_at"],
    )


def downgrade() -> None:
    """Drop all tables and enums."""
    op.drop_index("idx_relationships_last_seen", table_name="relationships")
    op.drop_table("relationships")

    op.drop_index("idx_audit_type", table_name="audit_events")
    op.drop_index("idx_audit_task", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_table("progress")

    op.drop_index("idx_receipts_parents_gin", table_name="receipts")
    op.drop_index("idx_receipts_hash", table_name="receipts")
    op.drop_index("idx_receipts_task", table_name="receipts")
    op.drop_index("idx_receipts_to", table_name="receipts")
    op.drop_table("receipts")

    op.drop_index("idx_leases_worker", table_name="leases")
    op.drop_index("idx_leases_expires", table_name="leases")
    op.drop_table("leases")

    op.drop_index("idx_tasks_instance", table_name="tasks")
    op.drop_index("idx_tasks_tenant_status", table_name="tasks")
    op.drop_index("idx_tasks_leasable", table_name="tasks")
    op.drop_index("idx_tasks_type", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("idx_auth_api_keys_prefix", table_name="auth_api_keys")
    op.drop_index("idx_auth_api_keys_user", table_name="auth_api_keys")
    op.drop_table("auth_api_keys")

    op.drop_index("idx_auth_oauth_provider_subject", table_name="auth_users")
    op.drop_table("auth_users")

    bind = op.get_bind()
    sa.Enum(name="receipttype").drop(bind, checkfirst=True)
    sa.Enum(name="taskstatus").drop(bind, checkfirst=True)
    sa.Enum(name="principalkind").drop(bind, checkfirst=True)
