"""
Authentication Middleware and Utilities for AsyncGate

Provides authentication via API keys (headers).
Async-compatible version of MemoryGate auth pattern.
"""

import secrets

import bcrypt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from asyncgate.auth.models import APIKey, User

# API key prefix for AsyncGate
API_KEY_PREFIX = "ag_"


def hash_api_key(api_key: str) -> str:
    """Hash API key with bcrypt"""
    return bcrypt.hashpw(api_key.encode(), bcrypt.gensalt()).decode()


def verify_api_key_hash(api_key: str, key_hash: str) -> bool:
    """Verify API key against hash"""
    return bcrypt.checkpw(api_key.encode(), key_hash.encode())


def generate_api_key() -> tuple[str, str, str]:
    """Generate API key with prefix and hash

    Returns:
        tuple: (full_key, prefix, hash)
    """
    full_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    prefix = full_key[:11]  # "ag_" + first 8 chars
    key_hash = hash_api_key(full_key)
    return full_key, prefix, key_hash


async def verify_request_api_key(db: AsyncSession, headers: dict) -> User | None:
    """
    Verify API key from request headers (async version).

    Checks Authorization: Bearer or X-API-Key header.
    Returns User if valid, None if missing/invalid.
    """
    # Extract API key from headers
    api_key = None

    # Check Authorization header (case-insensitive)
    auth_header = None
    for key, value in headers.items():
        if key.lower() == 'authorization':
            auth_header = value
            break

    if auth_header:
        parts = auth_header.split(' ', 1)
        if len(parts) == 2 and parts[0].lower() == 'bearer':
            api_key = parts[1]

    # Fallback to X-API-Key header (case-insensitive)
    if not api_key:
        for key, value in headers.items():
            if key.lower() == 'x-api-key':
                api_key = value
                break

    if not api_key:
        return None

    # Validate prefix - reject non-AsyncGate tokens early
    if not api_key.startswith(API_KEY_PREFIX):
        return None

    key_prefix = api_key[:11]

    # Find key by prefix
    result = await db.execute(
        select(APIKey).where(APIKey.key_prefix == key_prefix)
    )
    api_key_obj = result.scalar_one_or_none()

    if not api_key_obj:
        return None

    # Verify full key against hash
    if not verify_api_key_hash(api_key, api_key_obj.key_hash):
        return None

    # Check validity
    if not api_key_obj.is_valid:
        return None

    # Update usage tracking
    api_key_obj.increment_usage()
    await db.commit()

    # Load user relationship
    await db.refresh(api_key_obj, ["user"])
    return api_key_obj.user


async def get_or_create_bootstrap_user(db: AsyncSession, email: str = "system@asyncgate.local") -> User:
    """Get or create a bootstrap/system user for initial API key generation."""
    result = await db.execute(
        select(User).where(User.email == email)
    )
    user = result.scalar_one_or_none()

    if not user:
        user = User(
            email=email,
            name="System",
            is_active=True,
            is_admin=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    return user


async def create_api_key_for_user(
    db: AsyncSession,
    user: User,
    name: str = "Default API Key",
    scopes: list[str] = None
) -> tuple[str, APIKey]:
    """Create a new API key for a user.

    Returns:
        tuple: (full_key_string, api_key_object)
        Note: full_key_string is only returned once and should be shown to user
    """
    full_key, prefix, key_hash = generate_api_key()

    api_key = APIKey(
        user_id=user.id,
        key_prefix=prefix,
        key_hash=key_hash,
        name=name,
        scopes=scopes or [],
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return full_key, api_key
