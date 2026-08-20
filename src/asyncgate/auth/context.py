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
    # The tenant this request acts in, resolved from the credential rather than
    # taken from the request body. Callers used to supply it as a tool argument
    # with nothing checking it against the credential, so any holder of a key
    # could act in any tenant simply by typing a different value.
    tenant_id: str = ""

