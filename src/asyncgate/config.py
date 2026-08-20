"""AsyncGate configuration management."""

import json
from enum import Enum
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """Application environment."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class ReceiptMode(str, Enum):
    """Receipt storage mode."""

    STANDALONE = "standalone"
    RECEIPTGATE_INTEGRATED = "receiptgate_integrated"


class EscalationTarget(BaseModel):
    """Escalation target configuration."""

    model_config = SettingsConfigDict(extra="ignore", populate_by_name=True)

    class_id: int = Field(alias="class", description="Escalation class identifier")
    to_kind: str = Field(default="agent", description="Target principal kind")
    to_id: str = Field(..., description="Target principal identifier")
    tenant_id: str | None = Field(default=None, description="Override tenant ID for escalation")


class Settings(BaseSettings):
    """AsyncGate configuration settings."""

    model_config = SettingsConfigDict(
        env_prefix="ASYNCGATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # General
    env: Environment = Environment.DEVELOPMENT
    instance_id: str = Field(
        default="asyncgate-1",
        description="Unique instance identifier (auto-detected at startup if not set)",
    )
    log_level: str = "INFO"
    debug: bool = False

    # Database
    database_url: str = "postgresql+asyncpg://asyncgate:asyncgate@localhost:5432/asyncgate"

    # Redis (for rate limiting)
    redis_url: str | None = None

    # Receipt mode
    receipt_mode: ReceiptMode = ReceiptMode.STANDALONE

    # MetaGate bootstrap. Optional by design: MetaGate is a describe-only,
    # non-blocking authority, so an unset endpoint simply means "use the values
    # configured here" rather than a misconfiguration.
    metagate_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ASYNCGATE_METAGATE_ENDPOINT",
            "ASYNCGATE_METAGATE_URL",
            "METAGATE_ENDPOINT",
            "METAGATE_URL",
        ),
    )
    metagate_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ASYNCGATE_METAGATE_API_KEY",
            "METAGATE_API_KEY",
        ),
    )
    metagate_component_key: str = "asyncgate"
    metagate_bootstrap_timeout_seconds: float = 5.0

    # ReceiptGate integration (only used if receipt_mode = RECEIPTGATE_INTEGRATED)
    receiptgate_endpoint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ASYNCGATE_RECEIPTGATE_ENDPOINT",
            "ASYNCGATE_RECEIPTGATE_URL",
            "RECEIPTGATE_ENDPOINT",
            "RECEIPTGATE_URL",
        ),
    )
    receiptgate_auth_token: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ASYNCGATE_RECEIPTGATE_AUTH_TOKEN",
            "ASYNCGATE_RECEIPTGATE_API_KEY",
            "RECEIPTGATE_AUTH_TOKEN",
            "RECEIPTGATE_API_KEY",
        ),
    )
    # No receiptgate_tenant_id. It existed here, was documented as "tenant for
    # receipt writes", was set by the demo stack, and was read by nothing --
    # receipts carry the obligation's own tenant. Keeping it implied AsyncGate
    # could emit into a tenant other than the obligation's, which is not a thing
    # it can do and not a thing the ledger would accept.
    # The tenant used when the credential does not name one. JWTs carry a
    # tenant claim; DB API keys currently cannot, because the User model has no
    # tenant column, so those deployments are single-tenant and this is that
    # tenant. It is a fallback for an unnamed tenant, never an override for a
    # named one: a credential that names a tenant always wins.
    default_tenant_id: str = "00000000-0000-0000-0000-000000000000"

    receiptgate_emission_timeout_ms: int = 500
    receiptgate_emission_buffer_size: int = 10000
    receiptgate_emission_buffer_path: str = ".asyncgate/receiptgate_emission_buffer.json"
    receiptgate_emission_retry_interval_seconds: int = 30
    receiptgate_emission_max_retries: int = 10

    # ReceiptGate circuit breaker
    receiptgate_circuit_breaker_enabled: bool = Field(
        default=True, description="Enable circuit breaker for ReceiptGate calls"
    )
    receiptgate_circuit_breaker_failure_threshold: int = Field(
        default=5, description="Failures before opening circuit"
    )
    receiptgate_circuit_breaker_timeout_seconds: int = Field(
        default=60, description="Seconds before attempting half-open"
    )
    receiptgate_circuit_breaker_half_open_max_calls: int = Field(
        default=3, description="Test calls in half-open state"
    )
    receiptgate_circuit_breaker_success_threshold: int = Field(
        default=2, description="Successes to close from half-open"
    )

    # Rate limiting (P0.4 - enabled by default)
    rate_limit_enabled: bool = Field(default=True, description="Enable rate limiting")
    rate_limit_backend: str = Field(
        default="memory", description="Rate limit backend: memory or redis"
    )
    rate_limit_default_calls: int = Field(
        default=100, description="Default calls per window"
    )
    rate_limit_default_window_seconds: int = Field(
        default=60, description="Default window size in seconds"
    )

    @property
    def rate_limit_active(self) -> bool:
        """Rate limiting is forced on in staging/production regardless of config."""
        if self.env in [Environment.STAGING, Environment.PRODUCTION]:
            return True
        return self.rate_limit_enabled

    # Lease behavior
    default_lease_ttl_seconds: int = Field(default=120, description="Default lease TTL (2 min)")
    max_lease_ttl_seconds: int = Field(default=1800, description="Max lease TTL (30 min)")
    lease_sweep_interval_seconds: int = Field(default=5, description="Lease sweep cadence")
    lease_grace_seconds: int = Field(default=0, description="Lease grace period")

    # P1.1: Lease renewal limits (prevents hoarding DoS)
    max_lease_renewals: int = Field(
        default=10,
        description="Maximum times a lease can be renewed before forcing release"
    )
    max_lease_lifetime_seconds: int = Field(
        default=7200,  # 2 hours
        description="Absolute maximum lifetime for a lease (acquired_at to now)"
    )

    # Task retries
    default_max_attempts: int = Field(default=2, description="Default max task attempts")
    default_retry_backoff_seconds: int = Field(default=15, description="Default retry backoff")
    max_retry_backoff_seconds: int = Field(default=900, description="Max retry backoff (15 min)")

    # Task ordering
    default_priority: int = Field(default=0, description="Default task priority")

    # Pagination
    default_list_limit: int = Field(default=50, description="Default list limit")
    max_list_limit: int = Field(default=200, description="Max list limit")

    # Bootstrap
    default_bootstrap_max_items: int = Field(default=50, description="Default bootstrap items")
    max_bootstrap_max_items: int = Field(default=200, description="Max bootstrap items")

    # Receipt retention
    receipt_retention_days: int = Field(default=30, description="Active receipt retention")
    task_retention_days: int = Field(default=7, description="Terminal task retention")

    # Escalation routing
    escalation_enabled: bool = Field(default=False, description="Enable escalation receipts")
    escalation_targets: list[EscalationTarget] = Field(
        default_factory=list,
        description="Escalation targets keyed by class",
    )
    escalation_lease_expiry_class: int = Field(
        default=1,
        description="Escalation class to use for lease expiry events",
    )

    # Security (v0 - simple shared token)
    allow_insecure_dev: bool = Field(default=False, description="Allow unauthenticated in dev")
    api_key: str | None = None

    # CORS configuration (P0.3 - explicit allowlist)
    cors_allowed_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:8080"],
        description="Allowed CORS origins (explicit allowlist for security)"
    )
    cors_allow_credentials: bool = Field(
        default=True,
        description="Allow credentials in CORS requests"
    )
    cors_allowed_methods: list[str] = Field(
        default=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        description="Allowed HTTP methods"
    )
    cors_allowed_headers: list[str] = Field(
        default=["Authorization", "Content-Type", "X-Tenant-ID", "X-Trace-ID", "X-Request-ID"],
        description="Allowed request headers"
    )

    # JWT settings (for OAuth)
    jwt_algorithm: str = "RS256"
    jwt_private_key_path: str | None = None
    jwt_public_key_path: str | None = None
    jwt_access_token_ttl_days: int = 30
    jwt_refresh_token_ttl_days: int = 90

    # Server
    host: str = "0.0.0.0"
    port: int = 8080

    # Validators
    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Validate PostgreSQL database URL format."""
        if not v.startswith(("postgresql://", "postgresql+asyncpg://")):
            raise ValueError("database_url must be a PostgreSQL URL (postgresql:// or postgresql+asyncpg://)")
        return v

    @field_validator("port")
    @classmethod
    def validate_port(cls, v: int) -> int:
        """Validate port number range."""
        if not 1 <= v <= 65535:
            raise ValueError(f"Port must be between 1 and 65535, got {v}")
        return v

    @field_validator("receiptgate_endpoint", "redis_url")
    @classmethod
    def validate_integration_url(cls, v: str | None) -> str | None:
        """Validate integration URLs are HTTP(S) or redis://."""
        if v:
            if not v.startswith(("http://", "https://", "redis://", "rediss://")):
                raise ValueError(f"URL must start with http://, https://, redis://, or rediss://, got {v}")
        return v

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: str | None, info) -> str | None:
        """Validate API key is set when auth is required."""
        allow_insecure = info.data.get("allow_insecure_dev", False)
        env = info.data.get("env")

        # Production/staging must have api_key
        if env in [Environment.PRODUCTION, Environment.STAGING] and not v:
            raise ValueError(f"api_key is required in {env.value} environment")

        # Dev without api_key requires explicit allow_insecure_dev
        if not v and not allow_insecure:
            raise ValueError("api_key is required when allow_insecure_dev=False")

        return v

    @field_validator("escalation_targets", mode="before")
    @classmethod
    def parse_escalation_targets(cls, v: Any) -> list[EscalationTarget]:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return json.loads(v)
        return v

    def get_escalation_target(self, class_id: int) -> EscalationTarget | None:
        """Return escalation target for a class identifier."""
        for target in self.escalation_targets:
            if target.class_id == class_id:
                return target
        return None


settings = Settings()
