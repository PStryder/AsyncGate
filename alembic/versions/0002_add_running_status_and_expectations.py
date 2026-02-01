"""Add running status, task expectations, and receipt indexes."""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _find_enum_type(bind, labels: list[str]) -> str | None:
    query = sa.text(
        """
        SELECT t.typname
        FROM pg_type t
        JOIN pg_enum e ON t.oid = e.enumtypid
        WHERE e.enumlabel = ANY(:labels)
        GROUP BY t.typname
        HAVING COUNT(DISTINCT e.enumlabel) >= :label_count
        ORDER BY t.typname
        LIMIT 1
        """
    )
    result = bind.execute(
        query,
        {"labels": labels, "label_count": len(labels)},
    )
    return result.scalar_one_or_none()


def upgrade() -> None:
    """Upgrade schema with running status and expectation metadata."""
    bind = op.get_bind()

    task_status_enum = _find_enum_type(
        bind, ["queued", "leased", "succeeded", "failed", "canceled"]
    )
    if task_status_enum:
        op.execute(
            sa.text(
                f"ALTER TYPE {task_status_enum} ADD VALUE IF NOT EXISTS 'running'"
            )
        )

    receipt_type_enum = _find_enum_type(
        bind, ["task.assigned", "task.accepted", "task.completed"]
    )
    if receipt_type_enum:
        op.execute(
            sa.text(
                f"ALTER TYPE {receipt_type_enum} ADD VALUE IF NOT EXISTS 'task.started'"
            )
        )
        op.execute(
            sa.text(
                f"ALTER TYPE {receipt_type_enum} ADD VALUE IF NOT EXISTS 'task.escalated'"
            )
        )

    op.add_column(
        "tasks",
        sa.Column("expected_outcome_kind", sa.String(length=50), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("expected_artifact_mime", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "tasks",
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "idx_tasks_running",
        "tasks",
        ["tenant_id", "status", "started_at"],
    )

    op.create_index(
        "idx_receipts_lease",
        "receipts",
        ["tenant_id", "lease_id", "created_at"],
    )
    op.create_index(
        "idx_receipts_type",
        "receipts",
        ["tenant_id", "receipt_type", "created_at"],
    )


def downgrade() -> None:
    """Downgrade schema to remove running status additions."""
    op.drop_index("idx_receipts_type", table_name="receipts")
    op.drop_index("idx_receipts_lease", table_name="receipts")
    op.drop_index("idx_tasks_running", table_name="tasks")

    op.drop_column("tasks", "started_at")
    op.drop_column("tasks", "expected_artifact_mime")
    op.drop_column("tasks", "expected_outcome_kind")
