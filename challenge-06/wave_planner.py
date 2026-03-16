"""
Wave planning and progression logic.

Decides which devices go into each wave, enforces the diversity and eligibility
rules, and calculates progressive wave sizes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .device_ops import DeviceRolloutRecord, DeviceRolloutStatus, DeviceStatus

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WAVE_1_SIZE = 5
WAVE_1_REQUIRED_VERSIONS: Set[int] = {8, 9, 10, 11}
FAILURE_RATE_THRESHOLD = 0.02  # 2 %


# ---------------------------------------------------------------------------
# Wave data model
# ---------------------------------------------------------------------------

@dataclass
class Wave:
    """Represents one rollout wave."""
    number: int
    device_ids: List[str] = field(default_factory=list)
    status: str = "pending"  # pending | in_progress | completed | failed | halted


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class WavePlanner:
    """
    Plans rollout waves according to the specification:

    - Wave 1: 5 idle devices with good connections, covering Android 8/9/10/11
      plus one extra.  ANY failure in wave 1 halts everything.
    - Wave 2: 20 devices.
    - Wave 3+: progressively larger — next_wave = min(prev_wave * 2, remaining).
    - Skips offline, busy, or flaky-connection devices.
    """

    def __init__(self, devices: List[DeviceStatus]):
        self._all_devices = {d.device_id: d for d in devices}

    # -- Eligibility ----------------------------------------------------------

    @staticmethod
    def is_eligible(device: DeviceStatus) -> bool:
        """A device is eligible if it is idle, online, and has a good connection."""
        if device.connection_quality == "offline":
            return False
        if device.connection_quality == "flaky":
            return False
        if device.current_task is not None:
            return False
        return True

    # -- Wave 1 selection -----------------------------------------------------

    def _select_wave1(
        self, eligible_ids: List[str]
    ) -> Optional[List[str]]:
        """
        Pick 5 devices for wave 1:
        - One per required Android version (8, 9, 10, 11).
        - One extra (any version) to reach 5.

        Returns None if we cannot meet the diversity requirement.
        """
        selected: List[str] = []
        covered_versions: Set[int] = set()
        remaining: List[str] = []

        # First pass: pick one device per required version
        for did in eligible_ids:
            dev = self._all_devices[did]
            if (
                dev.android_version in WAVE_1_REQUIRED_VERSIONS
                and dev.android_version not in covered_versions
            ):
                selected.append(did)
                covered_versions.add(dev.android_version)
            else:
                remaining.append(did)

        if covered_versions != WAVE_1_REQUIRED_VERSIONS:
            missing = WAVE_1_REQUIRED_VERSIONS - covered_versions
            logger.error(
                "Cannot form wave 1: missing Android versions %s", missing
            )
            return None

        # Fill up to WAVE_1_SIZE
        for did in remaining:
            if len(selected) >= WAVE_1_SIZE:
                break
            selected.append(did)

        if len(selected) < WAVE_1_SIZE:
            logger.warning(
                "Wave 1 has only %d devices (wanted %d)", len(selected), WAVE_1_SIZE
            )
            # Still proceed — the diversity constraint is the hard requirement.

        return selected

    # -- Full plan ------------------------------------------------------------

    def plan(self) -> List[Wave]:
        """
        Build the full list of waves.

        Returns an ordered list of Wave objects.  The orchestrator processes
        them sequentially, deciding after each wave whether to continue.
        """
        eligible_ids = [
            d.device_id
            for d in self._all_devices.values()
            if self.is_eligible(d)
        ]
        logger.info(
            "Eligible devices: %d / %d total",
            len(eligible_ids),
            len(self._all_devices),
        )

        if not eligible_ids:
            logger.error("No eligible devices for rollout")
            return []

        waves: List[Wave] = []

        # Wave 1
        wave1_ids = self._select_wave1(eligible_ids)
        if wave1_ids is None:
            return []
        waves.append(Wave(number=1, device_ids=wave1_ids))
        used: Set[str] = set(wave1_ids)

        # Remaining eligible pool
        pool = [did for did in eligible_ids if did not in used]

        # Wave 2: fixed size 20 (or whatever remains)
        wave2_size = min(20, len(pool))
        if wave2_size > 0:
            wave2_ids = pool[:wave2_size]
            waves.append(Wave(number=2, device_ids=wave2_ids))
            pool = pool[wave2_size:]

        # Wave 3+: progressive doubling
        prev_size = wave2_size if wave2_size > 0 else WAVE_1_SIZE
        wave_num = 3
        while pool:
            next_size = min(prev_size * 2, len(pool))
            wave_ids = pool[:next_size]
            waves.append(Wave(number=wave_num, device_ids=wave_ids))
            pool = pool[next_size:]
            prev_size = next_size
            wave_num += 1

        logger.info("Planned %d waves", len(waves))
        for w in waves:
            logger.info("  Wave %d: %d devices", w.number, len(w.device_ids))

        return waves

    # -- Failure rate ---------------------------------------------------------

    @staticmethod
    def compute_failure_rate(records: List[DeviceRolloutRecord]) -> float:
        """
        Compute the failure rate for a set of device records.

        Interrupted devices are excluded from the denominator (they are not
        counted as failures).
        """
        if not records:
            return 0.0

        non_interrupted = [
            r for r in records if r.status != DeviceRolloutStatus.INTERRUPTED
        ]
        if not non_interrupted:
            return 0.0

        failed = sum(
            1 for r in non_interrupted if r.status == DeviceRolloutStatus.FAILED
        )
        return failed / len(non_interrupted)

    @staticmethod
    def should_halt_wave1(records: List[DeviceRolloutRecord]) -> bool:
        """Wave 1 rule: ANY failure means halt."""
        return any(r.status == DeviceRolloutStatus.FAILED for r in records)

    @staticmethod
    def should_halt_later_wave(records: List[DeviceRolloutRecord]) -> bool:
        """Wave 2+ rule: halt if failure rate exceeds 2 %."""
        rate = WavePlanner.compute_failure_rate(records)
        return rate > FAILURE_RATE_THRESHOLD
