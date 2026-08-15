"""Instance identity and uniqueness validation."""

import logging
import os
import socket
from uuid import uuid4

logger = logging.getLogger(__name__)


def detect_instance_id() -> str:
    """
    Auto-detect unique instance identifier from environment.

    Checks in priority order:
    1. Fly.io: FLY_ALLOC_ID (e.g., "01j9k2m3n4p5q6r7")
    2. Kubernetes: HOSTNAME (e.g., "asyncgate-deployment-7d8f9b-xyz12")
    3. AWS ECS: ECS_CONTAINER_METADATA_URI_V4 → extract task ID
    4. Cloud Run: K_REVISION (e.g., "asyncgate-00001-abc")
    5. Generic: ASYNCGATE_INSTANCE_ID (explicitly set)
    6. Fallback: hostname + random suffix

    Returns:
        Unique instance identifier string
    """
    # 1. Fly.io allocation ID
    fly_alloc_id = os.environ.get("FLY_ALLOC_ID")
    if fly_alloc_id:
        logger.info(f"Detected Fly.io instance: {fly_alloc_id}")
        return fly_alloc_id

    # 2. Kubernetes pod name (usually unique per replica)
    k8s_hostname = os.environ.get("HOSTNAME")
    if k8s_hostname and "-" in k8s_hostname:  # Likely K8s naming
        logger.info(f"Detected Kubernetes instance: {k8s_hostname}")
        return k8s_hostname

    # 3. AWS ECS task ID
    ecs_metadata_uri = os.environ.get("ECS_CONTAINER_METADATA_URI_V4")
    if ecs_metadata_uri:
        # In ECS, we can't easily extract task ID without HTTP call
        # Use container ID from metadata URI path
        try:
            container_id = ecs_metadata_uri.split("/")[-1]
            instance_id = f"ecs-{container_id[:12]}"
            logger.info(f"Detected AWS ECS instance: {instance_id}")
            return instance_id
        except Exception as e:
            logger.warning(f"Failed to parse ECS metadata URI: {e}")

    # 4. Google Cloud Run revision
    cloud_run_revision = os.environ.get("K_REVISION")
    if cloud_run_revision:
        # Cloud Run revision is unique per deployment, add suffix for replicas
        instance_suffix = str(uuid4())[:8]
        instance_id = f"{cloud_run_revision}-{instance_suffix}"
        logger.info(f"Detected Cloud Run instance: {instance_id}")
        return instance_id

    # 5. Explicit override
    explicit_id = os.environ.get("ASYNCGATE_INSTANCE_ID")
    if explicit_id and explicit_id != "asyncgate-1":  # Not default
        logger.info(f"Using explicit instance ID: {explicit_id}")
        return explicit_id

    # 6. Fallback: hostname + random suffix
    try:
        hostname = socket.gethostname()
        random_suffix = str(uuid4())[:8]
        instance_id = f"{hostname}-{random_suffix}"
        logger.warning(
            f"No deployment environment detected, using fallback: {instance_id}"
        )
        return instance_id
    except Exception as e:
        # Last resort: pure random
        instance_id = f"asyncgate-{uuid4()}"
        logger.error(
            f"Failed to detect hostname, using random ID: {instance_id} (error: {e})"
        )
        return instance_id


def validate_instance_uniqueness(instance_id: str, env: str) -> None:
    """
    Validate instance_id is suitable for the current environment.

    Args:
        instance_id: Instance identifier to validate
        env: Environment (development, staging, production)

    Raises:
        RuntimeError: If instance_id is unsafe for the environment
    """
    # In production/staging, reject default/generic IDs
    if env in ("staging", "production"):
        unsafe_patterns = [
            "asyncgate-1",  # Default from config
            "localhost",
            "127.0.0.1",
        ]

        for pattern in unsafe_patterns:
            if instance_id == pattern or instance_id.startswith(pattern):
                raise RuntimeError(
                    f"INSTANCE ID CONFLICT RISK: instance_id='{instance_id}' is not "
                    f"safe for {env} environment. Multiple instances could share the "
                    f"same ID, causing lease conflicts and data corruption.\n\n"
                    f"Solutions:\n"
                    f"  1. Deploy to platform with auto-detection (Fly.io, K8s, ECS, Cloud Run)\n"
                    f"  2. Set ASYNCGATE_INSTANCE_ID to a unique value per instance\n"
                    f"     Example: ASYNCGATE_INSTANCE_ID=$(hostname)-$(uuidgen | cut -d'-' -f1)\n"
                    f"  3. Use deployment platform identifiers:\n"
                    f"     - Fly.io: FLY_ALLOC_ID is automatically detected\n"
                    f"     - Kubernetes: HOSTNAME (pod name) is automatically detected\n"
                    f"     - AWS ECS: ECS_CONTAINER_METADATA_URI_V4 is automatically parsed\n"
                    f"     - Cloud Run: K_REVISION is automatically detected\n\n"
                    f"Current instance_id: {instance_id}\n"
                    f"Environment: {env}"
                )

        # Warn if instance_id looks suspiciously short/generic
        if len(instance_id) < 8:
            logger.warning(
                f"Instance ID '{instance_id}' is very short for {env} environment. "
                f"Consider using a more unique identifier to avoid accidental conflicts."
            )

    logger.info(f"Instance ID validated: {instance_id} (env: {env})")
