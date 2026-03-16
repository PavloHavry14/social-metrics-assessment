"""
Device operations: preflight checks, execution, post-flight verification, and rollback.

All ADB commands are mocked for safe local testing. In production, replace the
mock layer with real subprocess calls.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class DeviceRolloutStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    ROLLED_BACK = "rolled_back"


@dataclass
class DeviceStatus:
    device_id: str
    android_version: int  # 8, 9, 10, 11
    current_task: Optional[str]  # None if idle
    connection_quality: str  # "good", "flaky", "offline"
    last_heartbeat: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass
class DeviceRolloutRecord:
    """Tracks per-device rollout progress."""
    device_id: str
    wave: int = 0
    status: DeviceRolloutStatus = DeviceRolloutStatus.PENDING
    error_details: Optional[str] = None
    preflight_snapshot: Optional[Dict[str, Any]] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Mock ADB layer
# ---------------------------------------------------------------------------

class MockADB:
    """
    Simulates ADB interactions. Provides controllable failure injection so the
    orchestrator can be exercised under realistic fault conditions.
    """

    def __init__(
        self,
        ping_failure_ids: Optional[set] = None,
        push_failure_ids: Optional[set] = None,
        exec_failure_ids: Optional[set] = None,
        exec_disconnect_ids: Optional[set] = None,
        compliance_failure_ids: Optional[set] = None,
        rollback_failure_ids: Optional[set] = None,
    ):
        self.ping_failure_ids = ping_failure_ids or set()
        self.push_failure_ids = push_failure_ids or set()
        self.exec_failure_ids = exec_failure_ids or set()
        self.exec_disconnect_ids = exec_disconnect_ids or set()
        self.compliance_failure_ids = compliance_failure_ids or set()
        self.rollback_failure_ids = rollback_failure_ids or set()

    async def ping(self, device_id: str) -> bool:
        """Simulate `adb -s {id} shell echo ok`."""
        await asyncio.sleep(random.uniform(0.01, 0.05))
        if device_id in self.ping_failure_ids:
            logger.debug("Mock ADB ping FAILED for %s", device_id)
            return False
        return True

    async def snapshot_config(self, device_id: str) -> Dict[str, Any]:
        """
        Simulate snapshotting device config state:
        `adb shell settings list global/secure/system` + key getprop values.
        """
        await asyncio.sleep(random.uniform(0.01, 0.05))
        return {
            "global_settings": {f"setting_{i}": f"value_{i}" for i in range(5)},
            "secure_settings": {f"secure_{i}": f"val_{i}" for i in range(3)},
            "system_settings": {f"sys_{i}": f"val_{i}" for i in range(3)},
            "props": {
                "ro.build.version.sdk": "30",
                "ro.product.model": "MockDevice",
                "persist.sys.timezone": "UTC",
            },
            "snapshot_time": datetime.now(timezone.utc).isoformat(),
        }

    async def push_script(self, device_id: str, local_path: str) -> bool:
        """Simulate `adb push config_script.sh /data/local/tmp/`."""
        await asyncio.sleep(random.uniform(0.02, 0.08))
        if device_id in self.push_failure_ids:
            logger.warning("Mock ADB push FAILED for %s", device_id)
            return False
        return True

    async def execute_script(self, device_id: str) -> str:
        """
        Simulate `adb shell sh /data/local/tmp/config_script.sh`.

        Returns:
            "success" | "failed" | "disconnected"
        """
        await asyncio.sleep(random.uniform(0.05, 0.15))
        if device_id in self.exec_disconnect_ids:
            logger.warning("Mock ADB exec DISCONNECTED for %s", device_id)
            return "disconnected"
        if device_id in self.exec_failure_ids:
            logger.warning("Mock ADB exec FAILED for %s", device_id)
            return "failed"
        return "success"

    async def validate_compliance(self, device_id: str) -> bool:
        """Post-flight compliance check."""
        await asyncio.sleep(random.uniform(0.02, 0.06))
        if device_id in self.compliance_failure_ids:
            logger.warning("Mock compliance FAILED for %s", device_id)
            return False
        return True

    async def restore_config(
        self, device_id: str, snapshot: Dict[str, Any]
    ) -> bool:
        """Restore snapshotted config state."""
        await asyncio.sleep(random.uniform(0.03, 0.08))
        if device_id in self.rollback_failure_ids:
            logger.error("Mock rollback FAILED for %s", device_id)
            return False
        return True


# ---------------------------------------------------------------------------
# Device Operations
# ---------------------------------------------------------------------------

# Retry schedule for disconnected execution (seconds)
RETRY_DELAYS = [5, 15, 30]


class DeviceOps:
    """Encapsulates all device-level operations for the rollout."""

    def __init__(self, adb: MockADB, script_path: str = "config_script.sh"):
        self.adb = adb
        self.script_path = script_path

    # -- Pre-flight -----------------------------------------------------------

    async def preflight(
        self, device: DeviceStatus, record: DeviceRolloutRecord
    ) -> bool:
        """
        Run pre-flight checks on a single device.

        1. Ping test
        2. Verify device is idle
        3. Snapshot current config state

        Returns True if all checks pass.
        """
        record.started_at = datetime.now(timezone.utc)
        record.status = DeviceRolloutStatus.IN_PROGRESS

        # 1. Ping
        if not await self.adb.ping(device.device_id):
            record.status = DeviceRolloutStatus.FAILED
            record.error_details = "preflight: ping failed"
            record.finished_at = datetime.now(timezone.utc)
            logger.error("Preflight FAILED (ping) for %s", device.device_id)
            return False

        # 2. Idle check
        if device.current_task is not None:
            record.status = DeviceRolloutStatus.FAILED
            record.error_details = (
                f"preflight: device busy with task '{device.current_task}'"
            )
            record.finished_at = datetime.now(timezone.utc)
            logger.error(
                "Preflight FAILED (busy) for %s: task=%s",
                device.device_id,
                device.current_task,
            )
            return False

        # 3. Snapshot
        try:
            snapshot = await self.adb.snapshot_config(device.device_id)
            record.preflight_snapshot = snapshot
        except Exception as exc:
            record.status = DeviceRolloutStatus.FAILED
            record.error_details = f"preflight: snapshot error — {exc}"
            record.finished_at = datetime.now(timezone.utc)
            logger.exception(
                "Preflight FAILED (snapshot) for %s", device.device_id
            )
            return False

        logger.info("Preflight PASSED for %s", device.device_id)
        return True

    # -- Execution ------------------------------------------------------------

    async def execute(
        self, device: DeviceStatus, record: DeviceRolloutRecord
    ) -> bool:
        """
        Push and execute the config script on a single device.

        Handles retries on disconnect with backoff (5s, 15s, 30s).
        Returns True on success.
        """
        # Push
        if not await self.adb.push_script(device.device_id, self.script_path):
            record.status = DeviceRolloutStatus.FAILED
            record.error_details = "execute: push failed"
            record.finished_at = datetime.now(timezone.utc)
            return False

        # Execute with disconnect retry
        for attempt, delay in enumerate(
            [0] + RETRY_DELAYS  # first attempt has no delay
        ):
            if delay:
                logger.info(
                    "Retrying execution for %s in %ds (attempt %d)",
                    device.device_id,
                    delay,
                    attempt,
                )
                await asyncio.sleep(delay * 0.01)  # scaled down for mock

            result = await self.adb.execute_script(device.device_id)

            if result == "success":
                logger.info("Execution SUCCEEDED for %s", device.device_id)
                return True

            if result == "failed":
                record.status = DeviceRolloutStatus.FAILED
                record.error_details = "execute: script execution failed"
                record.finished_at = datetime.now(timezone.utc)
                return False

            # result == "disconnected" — retry if attempts remain
            if attempt < len(RETRY_DELAYS):
                logger.warning(
                    "ADB dropped mid-script for %s, will retry", device.device_id
                )

        # Exhausted retries — mark interrupted (NOT a failure)
        record.status = DeviceRolloutStatus.INTERRUPTED
        record.error_details = "execute: ADB disconnected after all retries"
        record.finished_at = datetime.now(timezone.utc)
        logger.warning(
            "Device %s marked INTERRUPTED after retries exhausted",
            device.device_id,
        )
        return False

    # -- Post-flight ----------------------------------------------------------

    async def postflight(
        self, device: DeviceStatus, record: DeviceRolloutRecord
    ) -> bool:
        """
        Run compliance validation after script execution.

        A device that was updated but fails compliance counts as a FAILURE.
        """
        compliant = await self.adb.validate_compliance(device.device_id)
        if not compliant:
            record.status = DeviceRolloutStatus.FAILED
            record.error_details = "postflight: compliance check failed"
            record.finished_at = datetime.now(timezone.utc)
            logger.error(
                "Post-flight FAILED (compliance) for %s", device.device_id
            )
            return False

        record.status = DeviceRolloutStatus.SUCCEEDED
        record.finished_at = datetime.now(timezone.utc)
        logger.info("Post-flight PASSED for %s", device.device_id)
        return True

    # -- Rollback -------------------------------------------------------------

    async def rollback(
        self, device: DeviceStatus, record: DeviceRolloutRecord
    ) -> bool:
        """
        Restore device to its pre-rollout config state and re-verify compliance.
        """
        if record.preflight_snapshot is None:
            record.error_details = "rollback: no snapshot available"
            logger.error("Cannot rollback %s — no snapshot", device.device_id)
            return False

        restored = await self.adb.restore_config(
            device.device_id, record.preflight_snapshot
        )
        if not restored:
            record.error_details = "rollback: config restore failed"
            logger.error("Rollback FAILED for %s", device.device_id)
            return False

        compliant = await self.adb.validate_compliance(device.device_id)
        if not compliant:
            record.error_details = "rollback: post-rollback compliance failed"
            logger.error(
                "Rollback compliance FAILED for %s", device.device_id
            )
            return False

        record.status = DeviceRolloutStatus.ROLLED_BACK
        record.finished_at = datetime.now(timezone.utc)
        logger.info("Rollback SUCCEEDED for %s", device.device_id)
        return True
