"""
Authentication Models for AsyncGate

User and API key models adapted from MemoryGate pattern.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import relationship

from asyncgate.db.base import Base


class User(Base):
    """User account - created via OAuth flow or admin provisioning"""
    __tablename__ = "auth_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False, index=True)
    name = Column(String, nullable=True)

    # OAuth provider info (optional - can be null for API-only users)
    oauth_provider = Column(String, nullable=True)  # 'google', 'github', etc.
    oauth_subject = Column(String, nullable=True)   # Provider's user ID

    # Account status
    is_active = Column(Boolean, default=True, nullable=False)
    is_admin = Column(Boolean, default=False, nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_login = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Metadata storage
    metadata_ = Column("metadata", JSONB, default=dict, nullable=False)

    # Relationships
    api_keys = relationship("APIKey", back_populates="user", cascade="all, delete-orphan")

    # Unique constraint on provider + subject (when OAuth is used)
    __table_args__ = (
        Index('idx_auth_oauth_provider_subject', 'oauth_provider', 'oauth_subject', unique=True,
              postgresql_where=Column('oauth_provider').isnot(None)),
    )

    def __repr__(self):
        return f"<User {self.email}>"


class APIKey(Base):
    """API keys for programmatic access"""
    __tablename__ = "auth_api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False, index=True)

    # Key components
    key_prefix = Column(String, nullable=False, index=True)  # First 11 chars (ag_ + 8) for identification
    key_hash = Column(String, nullable=False, unique=True)   # bcrypt hash of full key

    # Key metadata
    name = Column(String, nullable=False)  # User-provided name
    scopes = Column(JSONB, default=list, nullable=False)  # Permission scopes

    # Usage tracking
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    last_used = Column(DateTime, nullable=True)
    usage_count = Column(Integer, default=0, nullable=False)

    # Expiry and revocation
    expires_at = Column(DateTime, nullable=True)  # None = no expiry
    is_revoked = Column(Boolean, default=False, nullable=False)

    # Relationship
    user = relationship("User", back_populates="api_keys")

    @property
    def is_valid(self) -> bool:
        if self.is_revoked:
            return False
        if self.expires_at and datetime.utcnow() > self.expires_at:
            return False
        return True

    def increment_usage(self):
        """Track key usage"""
        self.usage_count += 1
        self.last_used = datetime.utcnow()
