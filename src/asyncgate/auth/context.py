"""Authentication context helpers."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from asyncgate.auth.models import User


@dataclass(frozen=True)
class AuthContext:
    """Authentication context for the current request."""

    user: "User | None"
    auth_type: Literal["db_api_key", "legacy_api_key", "insecure_dev", "jwt"]
    is_internal: bool
