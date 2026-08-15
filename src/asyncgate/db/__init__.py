"""AsyncGate database layer."""

from asyncgate.db.base import Base, get_session, init_db
from asyncgate.db.tables import (
    AuditEventTable,
    LeaseTable,
    ProgressTable,
    ReceiptTable,
    RelationshipTable,
    TaskTable,
)

# NOTE: auth.models is deliberately NOT imported here.
#
# It imports asyncgate.db.base, and importing that submodule executes this
# package first -- so `import asyncgate.auth` raised
# "cannot import name 'User' from partially initialized module". The app's
# entry point happened to import in an order that avoided it, which is why it
# survived; any direct import of the auth or mcp packages hit it.
#
# Registration with SQLAlchemy metadata now happens in db.base.register_models(),
# called from init_db(), which is where the metadata is actually used.

__all__ = [
    "Base",
    "get_session",
    "init_db",
    "TaskTable",
    "LeaseTable",
    "ReceiptTable",
    "ProgressTable",
    "AuditEventTable",
    "RelationshipTable",
]
