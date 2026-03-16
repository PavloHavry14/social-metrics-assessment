"""
Main orchestrator — drives the fleet rollout end-to-end.

Usage (standalone demo):
    python -m challenge-06.orchestrator

Or import and call:
    from challenge_06.orchestrator import Orchestrator
    await Orchestrator.run(devices, adb, script_path)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from io import StringIO
from typing import Dict, List, Optional, TextIO

from .dashboard import Dashboard
from .device_ops import (
    DeviceOps,
    DeviceRolloutRecord,
    DeviceRolloutStatus,
    DeviceStatus,
    MockADB,
)
from .wave_planner import Wave, WavePlanner

logger = logging.getLogger(__name__)


class Orchestrator:
    """
    Coordinates the full rollout lifecycle:

    1. Fetch device statuses
    2. Plan waves
    3. For each wave — preflight, execute, postflight, evaluate, maybe rollback
    4. Produce final report
    """

    def __init__(
        self,
        devices: List[DeviceStatus],
        device_ops: DeviceOps,
        output: TextIO = sys.stdout,
    ):
        self.devices: Dict[str, DeviceStatus] = {d.device_id: d for d in devices}
        self.ops = device_ops
        self.records: Dict[str, DeviceRolloutRecord] = {}
        self.waves: List[Wave] = []
        self.halted = False
        self.halt_reason: Optional[str] = None
        self._output = output

    # -- Record helpers -------------------------------------------------------

    def _ensure_record(self, device_id: str, wave_number: int) -> DeviceRolloutRecord:
        if device_id not in self.records:
            self.records[device_id] = DeviceRolloutRecord(
                device_id=device_id, wave=wave_number
            )
        else:
            self.records[device_id].wave = wave_number
        return self.records[device_id]

    def _wave_records(self, wave: Wave) -> List[DeviceRolloutRecord]:
        return [self.records[did] for did in wave.device_ids if did in self.records]

    # -- Phase runners --------------------------------------------------------

    async def _run_preflight(self, wave: Wave) -> None:
        """Run preflight on all devices in a wave in parallel."""
        tasks = []
        for did in wave.device_ids:
            device = self.devices[did]
            record = self._ensure_record(did, wave.number)
            tasks.append(self.ops.preflight(device, record))
        await asyncio.gather(*tasks)

    async def _run_execute(self, wave: Wave) -> None:
        """Execute the config script on all preflight-passed devices in parallel."""
        tasks = []
        for did in wave.device_ids:
            record = self.records[did]
            if record.status == DeviceRolloutStatus.FAILED:
                # Already failed preflight — skip execution
                continue
            device = self.devices[did]
            tasks.append(self.ops.execute(device, record))
        if tasks:
            await asyncio.gather(*tasks)

    async def _run_postflight(self, wave: Wave) -> None:
        """Post-flight compliance check for devices that completed execution."""
        tasks = []
        for did in wave.device_ids:
            record = self.records[did]
            if record.status not in (
                DeviceRolloutStatus.IN_PROGRESS,
                # IN_PROGRESS is the status after successful execution
                # (before postflight sets SUCCEEDED)
            ):
                continue
            device = self.devices[did]
            tasks.append(self.ops.postflight(device, record))
        if tasks:
            await asyncio.gather(*tasks)

    async def _run_rollback(self, wave: Wave) -> None:
        """Rollback all non-interrupted devices in the wave."""
        tasks = []
        for did in wave.device_ids:
            record = self.records[did]
            if record.status in (
                DeviceRolloutStatus.SUCCEEDED,
                DeviceRolloutStatus.FAILED,
            ):
                device = self.devices[did]
                tasks.append(self.ops.rollback(device, record))
        if tasks:
            await asyncio.gather(*tasks)

    # -- Main loop ------------------------------------------------------------

    async def run(self) -> Dict[str, DeviceRolloutRecord]:
        """
        Execute the full rollout.

        Returns the final dict of device_id -> DeviceRolloutRecord.
        """
        # 1. Plan waves
        planner = WavePlanner(list(self.devices.values()))
        self.waves = planner.plan()

        if not self.waves:
            logger.error("No waves planned — aborting rollout")
            self.halted = True
            self.halt_reason = "No eligible devices or missing Android version coverage"
            return self.records

        # Build version lookup for dashboard
        android_versions = {d.device_id: d.android_version for d in self.devices.values()}
        dashboard = Dashboard(
            self.waves, self.records, android_versions, output=self._output
        )

        total_waves = len(self.waves)

        # 2. Process each wave
        for wave in self.waves:
            if self.halted:
                wave.status = "halted"
                continue

            wave.status = "in_progress"
            dashboard.log_wave_start(wave, total_waves)

            # a. Preflight
            await self._run_preflight(wave)

            # b. Execute
            await self._run_execute(wave)

            # c. Postflight
            await self._run_postflight(wave)

            # d. Compute failure rate
            wave_records = self._wave_records(wave)
            failure_rate = planner.compute_failure_rate(wave_records)

            # e/f. Decide whether to halt
            should_halt = False
            if wave.number == 1 and planner.should_halt_wave1(wave_records):
                should_halt = True
                self.halt_reason = (
                    f"Wave 1 failure detected (failure_rate={failure_rate:.1%})"
                )
            elif wave.number > 1 and planner.should_halt_later_wave(wave_records):
                should_halt = True
                self.halt_reason = (
                    f"Wave {wave.number} failure rate {failure_rate:.1%} "
                    f"exceeds 2% threshold"
                )

            if should_halt:
                wave.status = "failed"
                dashboard.log_wave_result(wave, total_waves, failure_rate)

                # Rollback the wave
                dashboard.log_rollback_start(wave)
                await self._run_rollback(wave)

                self.halted = True
                dashboard.log_halt(self.halt_reason)  # type: ignore[arg-type]
            else:
                wave.status = "completed"
                dashboard.log_wave_result(wave, total_waves, failure_rate)

        # 3. Final report
        dashboard.log_final_report()
        return self.records


# ---------------------------------------------------------------------------
# Mock device fleet generator
# ---------------------------------------------------------------------------

def generate_mock_fleet(
    count: int = 200,
    offline_pct: float = 0.05,
    flaky_pct: float = 0.10,
    busy_pct: float = 0.08,
) -> List[DeviceStatus]:
    """Generate a realistic mock device fleet."""
    import random

    devices: List[DeviceStatus] = []
    versions = [8, 9, 10, 11]

    for i in range(1, count + 1):
        device_id = f"device-{i:03d}"

        # Assign connection quality
        roll = random.random()
        if roll < offline_pct:
            conn = "offline"
        elif roll < offline_pct + flaky_pct:
            conn = "flaky"
        else:
            conn = "good"

        # Assign task
        task = None
        if conn == "good" and random.random() < busy_pct:
            task = f"task-{random.randint(1000, 9999)}"

        devices.append(
            DeviceStatus(
                device_id=device_id,
                android_version=random.choice(versions),
                current_task=task,
                connection_quality=conn,
                last_heartbeat=datetime.now(timezone.utc) - timedelta(seconds=random.randint(0, 300)),
            )
        )

    # Ensure wave 1 diversity: force the first 4 devices to cover all versions
    for idx, ver in enumerate(versions):
        devices[idx].android_version = ver
        devices[idx].connection_quality = "good"
        devices[idx].current_task = None

    return devices


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Run a demo rollout with a mock fleet."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    import random
    random.seed(42)

    fleet = generate_mock_fleet(count=200)

    # Inject a few failures for realism
    adb = MockADB(
        exec_disconnect_ids={"device-012", "device-045"},
        compliance_failure_ids={"device-030"},
    )

    ops = DeviceOps(adb=adb, script_path="config_script.sh")
    orchestrator = Orchestrator(devices=fleet, device_ops=ops)
    await orchestrator.run()


if __name__ == "__main__":
    asyncio.run(main())
