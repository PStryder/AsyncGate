"""AsyncGate main application."""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from asyncgate.api.deps import validate_auth_config
from asyncgate.config import settings
from asyncgate.db.base import close_db, init_db
from asyncgate.instance import detect_instance_id, validate_instance_uniqueness
from asyncgate.integrations import (
    acknowledge_startup,
    bootstrap_from_metagate,
    shutdown_receiptgate_client,
)
from asyncgate.middleware.trace import trace_id_middleware
from asyncgate.mcp.http import router as mcp_router
from asyncgate.tasks.sweep import start_lease_sweep, stop_lease_sweep

# Configure logging
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper()),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("asyncgate")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager."""
    logger.info("Starting AsyncGate server...")
    
    # Auto-detect instance_id if using default
    if settings.instance_id == "asyncgate-1":
        detected_id = detect_instance_id()
        settings.instance_id = detected_id
        logger.info(f"Auto-detected instance ID: {settings.instance_id}")
    else:
        logger.info(f"Using configured instance ID: {settings.instance_id}")
    
    # Validate instance uniqueness (fail fast if unsafe)
    validate_instance_uniqueness(settings.instance_id, settings.env.value)
    
    logger.info(f"Environment: {settings.env.value}")

    # Resolve peer endpoints from MetaGate before anything that depends on them.
    # Deliberately best-effort: a failure here must never prevent startup, or
    # the bootstrap authority becomes a hidden master.
    bootstrap_result = await bootstrap_from_metagate(settings)
    if bootstrap_result is not None and bootstrap_result.succeeded:
        await acknowledge_startup(settings, bootstrap_result)

    logger.info(f"Receipt mode: {settings.receipt_mode.value}")

    # Validate authentication configuration (fail fast if insecure)
    validate_auth_config()

    # Initialize database
    await init_db()
    logger.info("Database initialized")

    # Start background tasks
    await start_lease_sweep()
    logger.info("Lease sweep task started")

    yield

    # Cleanup
    logger.info("Shutting down AsyncGate server...")
    await stop_lease_sweep()
    await shutdown_receiptgate_client()
    await close_db()
    logger.info("Shutdown complete")


# Create FastAPI application
app = FastAPI(
    title="AsyncGate",
    description="Durable, lease-based asynchronous task execution MCP server",
    version="0.1.0",
    lifespan=lifespan,
)

# Trace ID middleware (correlation across logs/receipts)
app.middleware("http")(trace_id_middleware)

# Add CORS middleware (P0.3 - explicit allowlist, no wildcards with credentials)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allowed_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=settings.cors_allowed_methods,
    allow_headers=settings.cors_allowed_headers,
)

# MCP JSON-RPC endpoint
app.include_router(mcp_router)


def main():
    """Entry point for the application."""
    uvicorn.run(
        "asyncgate.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
