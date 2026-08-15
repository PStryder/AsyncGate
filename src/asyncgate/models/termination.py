"""Obligation termination type semantics.

AsyncGate's termination registry defines WHAT types can terminate WHAT,
not HOW to detect termination. The database answers "did it happen."

Key principle: Termination is TYPE SEMANTICS + DB EVIDENCE.
- Static: "task.completed CAN terminate task.assigned" (this module)
- Runtime: "Does terminator receipt exist?" (database query)

This keeps AsyncGate dumb and fast - no semantic inference, no ledger scanning.

Type Rules:
- task.assigned → can be terminated by [task.completed, task.failed, task.canceled]
- lease.granted → can be terminated by [lease.released, lease.expired]
- schedule.created → can be terminated by [schedule.deleted, schedule.failed]

Parent linkage is required:
- Terminal receipts MUST reference obligation via parents field
- Without linkage, obligations remain open forever (haunted bootstrap)
"""

from asyncgate.models.enums import ReceiptType

# Termination rules: maps obligation types to their terminal types
# This is a STATIC TYPE TRUTH TABLE, not runtime logic
TERMINATION_RULES: dict[ReceiptType, set[ReceiptType]] = {
    # Task lifecycle
    ReceiptType.TASK_ASSIGNED: {
        ReceiptType.TASK_COMPLETED,
        ReceiptType.TASK_FAILED,
        ReceiptType.TASK_CANCELED,
    },
    # Future: Lease lifecycle
    # ReceiptType.LEASE_GRANTED: {
    #     ReceiptType.LEASE_RELEASED,
    #     ReceiptType.LEASE_EXPIRED,
    # },
    # Future: Schedule lifecycle
    # ReceiptType.SCHEDULE_CREATED: {
    #     ReceiptType.SCHEDULE_DELETED,
    #     ReceiptType.SCHEDULE_FAILED,
    # },
}

# Union of all terminal types (any receipt type that can terminate something)
TERMINAL_TYPES: set[ReceiptType] = set()
for terminal_set in TERMINATION_RULES.values():
    TERMINAL_TYPES.update(terminal_set)

# Canonical terminal receipt type set (public alias)
TERMINAL_RECEIPT_TYPES: set[ReceiptType] = set(TERMINAL_TYPES)


def get_terminal_types(obligation_type: ReceiptType) -> set[ReceiptType]:
    """
    Get receipt types that can terminate an obligation.

    This is TYPE SEMANTICS: "What types are ALLOWED to terminate this?"
    NOT runtime detection: "Did termination happen?" (that's a DB query)

    Args:
        obligation_type: The receipt type that creates an obligation

    Returns:
        Set of receipt types that can terminate this obligation
        Empty set if obligation_type is not registered
    """
    return TERMINATION_RULES.get(obligation_type, set())


def is_terminal_type(receipt_type: ReceiptType) -> bool:
    """
    Check if a receipt type is terminal (can discharge obligations).

    This is TYPE CHECKING: "Is this type capable of termination?"
    NOT: "Did this receipt terminate something?" (that requires parent linkage check)

    Args:
        receipt_type: Receipt type to check

    Returns:
        True if this receipt type appears in any termination rule
    """
    return receipt_type in TERMINAL_RECEIPT_TYPES


def can_terminate_type(
    terminal_type: ReceiptType,
    obligation_type: ReceiptType,
) -> bool:
    """
    Check if a terminal type can terminate an obligation type.

    This is TYPE COMPATIBILITY: "Are these types compatible for termination?"
    NOT: "Did termination happen?" (that requires checking DB for parent linkage)

    Args:
        terminal_type: Receipt type that might terminate
        obligation_type: The obligation type to check

    Returns:
        True if terminal_type is registered to terminate obligation_type
    """
    terminal_types = get_terminal_types(obligation_type)
    return terminal_type in terminal_types


def get_obligation_types() -> set[ReceiptType]:
    """
    Get all receipt types that create obligations.

    Returns:
        Set of receipt types that appear as keys in TERMINATION_RULES
    """
    return set(TERMINATION_RULES.keys())
