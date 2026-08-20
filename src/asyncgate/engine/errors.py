"""AsyncGate engine errors."""


class AsyncGateError(Exception):
    """Base error for AsyncGate operations."""

    def __init__(self, message: str, code: str = "ASYNCGATE_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class TaskNotFound(AsyncGateError):
    """Task does not exist."""

    def __init__(self, task_id: str):
        super().__init__(f"Task not found: {task_id}", "TASK_NOT_FOUND")
        self.task_id = task_id


class InvalidStateTransition(AsyncGateError):
    """Invalid task state transition."""

    def __init__(self, current_status: str, requested_status: str):
        super().__init__(
            f"Invalid transition from {current_status} to {requested_status}",
            "INVALID_STATE_TRANSITION",
        )
        self.current_status = current_status
        self.requested_status = requested_status


class LeaseInvalidOrExpired(AsyncGateError):
    """Lease is invalid or has expired."""

    def __init__(self, task_id: str = "", lease_id: str = ""):
        super().__init__(
            f"Lease invalid or expired for task {task_id}",
            "LEASE_INVALID_OR_EXPIRED",
        )
        self.task_id = task_id
        self.lease_id = lease_id


class UnauthorizedError(AsyncGateError):
    """Operation not authorized."""

    def __init__(self, message: str = "Unauthorized"):
        super().__init__(message, "UNAUTHORIZED")


class QuotaExceededError(AsyncGateError):
    """Quota exceeded."""

    def __init__(self, quota_type: str, limit: int):
        super().__init__(
            f"{quota_type} quota exceeded (limit: {limit})",
            "QUOTA_EXCEEDED",
        )
        self.quota_type = quota_type
        self.limit = limit


class RateLimitExceededError(AsyncGateError):
    """Rate limit exceeded."""

    def __init__(self, limit: int, window: str, retry_after: int):
        super().__init__(
            f"Rate limit exceeded ({limit}/{window})",
            "RATE_LIMIT_EXCEEDED",
        )
        self.limit = limit
        self.window = window
        self.retry_after = retry_after


class LeaseRenewalLimitExceeded(AsyncGateError):
    """Lease has reached maximum renewal count (P1.1)."""

    def __init__(self, task_id: str, lease_id: str, renewal_count: int, max_renewals: int):
        super().__init__(
            f"Lease {lease_id} for task {task_id} has reached maximum renewals "
            f"({renewal_count}/{max_renewals}). Release and reclaim to continue.",
            "LEASE_RENEWAL_LIMIT_EXCEEDED",
        )
        self.task_id = task_id
        self.lease_id = lease_id
        self.renewal_count = renewal_count
        self.max_renewals = max_renewals


class LeaseLifetimeExceeded(AsyncGateError):
    """Lease has exceeded absolute lifetime limit (P1.1)."""

    def __init__(self, task_id: str, lease_id: str, lifetime_seconds: int, max_lifetime: int):
        super().__init__(
            f"Lease {lease_id} for task {task_id} has exceeded maximum lifetime "
            f"({lifetime_seconds}s/{max_lifetime}s). Release and reclaim to continue.",
            "LEASE_LIFETIME_EXCEEDED",
        )
        self.task_id = task_id
        self.lease_id = lease_id
        self.lifetime_seconds = lifetime_seconds
        self.max_lifetime = max_lifetime


class ReceiptNotYetCommitted(AsyncGateError):
    """The receipt is durably buffered but has not been committed by the ledger.

    Raised so the caller's state changes roll back. A buffered transition is not
    an authoritative one: the emitter must not advance its own state machine on
    it, so a worker whose `complete` sits in the outbox has not completed and
    may not release custody or report closure.

    Nothing is lost -- the receipt is persisted and replayed -- but the moment
    it becomes true is decided by the ledger, and operational state follows from
    there rather than running ahead of it.
    """

    def __init__(self, receipt_type: str, task_id: str | None, buffer_id: str):
        self.receipt_type = receipt_type
        self.task_id = task_id
        self.buffer_id = buffer_id
        super().__init__(
            f"{receipt_type} for task {task_id} is buffered (id={buffer_id}), "
            f"not committed; local state must not advance"
        )
