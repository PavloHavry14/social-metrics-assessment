"""
Tests for Challenge 06 — Fleet Orchestrator.

Covers: wave planning, failure rate thresholds, connection drops,
rollback behaviour, and pre-flight idle checks.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import List

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: load the "challenge-06" package (hyphen in name prevents normal
# import).  Register the package and its submodules so relative imports work.
# ---------------------------------------------------------------------------

_ROOT = Path(__file__).resolve().parent

def _load_module(fqn: str, filepath: Path):
    """Load a single module by file path and register it in sys.modules."""
    spec = importlib.util.spec_from_file_location(
        fqn, str(filepath),
        submodule_search_locations=[str(filepath.parent)] if filepath.name == "__init__.py" else None,
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[fqn] = mod
    spec.loader.exec_module(mod)
    return mod

_PKG = "challenge-06"
_pkg_mod = _load_module(_PKG, _ROOT / "__init__.py")
_device_ops = _load_module(f"{_PKG}.device_ops", _ROOT / "device_ops.py")
_wave_planner = _load_module(f"{_PKG}.wave_planner", _ROOT / "wave_planner.py")
_dashboard = _load_module(f"{_PKG}.dashboard", _ROOT / "dashboard.py")
_orchestrator = _load_module(f"{_PKG}.orchestrator", _ROOT / "orchestrator.py")

# Pull names into module scope
DeviceOps = _device_ops.DeviceOps
DeviceRolloutRecord = _device_ops.DeviceRolloutRecord
DeviceRolloutStatus = _device_ops.DeviceRolloutStatus
DeviceStatus = _device_ops.DeviceStatus
MockADB = _device_ops.MockADB
Orchestrator = _orchestrator.Orchestrator
FAILURE_RATE_THRESHOLD = _wave_planner.FAILURE_RATE_THRESHOLD
WAVE_1_SIZE = _wave_planner.WAVE_1_SIZE
Wave = _wave_planner.Wave
WavePlanner = _wave_planner.WavePlanner


# ======================================================================
# Helpers
# ======================================================================


def _make_device(
    device_id: str,
    version: int = 10,
    task: str | None = None,
    conn: str = "good",
) -> DeviceStatus:
    return DeviceStatus(
        device_id=device_id,
        android_version=version,
        current_task=task,
        connection_quality=conn,
        last_heartbeat=datetime.now(timezone.utc),
    )


def _make_fleet(n: int, versions: list[int] | None = None) -> List[DeviceStatus]:
    """Create *n* idle, good-connection devices cycling through versions."""
    if versions is None:
        versions = [8, 9, 10, 11]
    devices = []
    for i in range(n):
        devices.append(
            _make_device(
                device_id=f"dev-{i:03d}",
                version=versions[i % len(versions)],
            )
        )
    return devices


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ======================================================================
# 1-3. Wave planning
# ======================================================================


class TestWavePlanning:
    """Wave 1 size, diversity, and wave 2 sizing."""

    def test_wave1_has_exactly_5_devices_with_version_diversity(self):
        """#1: Wave 1 has exactly 5 devices, one per Android version where possible."""
        fleet = _make_fleet(50)
        planner = WavePlanner(fleet)
        waves = planner.plan()

        assert len(waves) >= 2
        wave1 = waves[0]
        assert wave1.number == 1
        assert len(wave1.device_ids) == WAVE_1_SIZE

        # Check version diversity: at least versions 8, 9, 10, 11 represented
        w1_versions = set()
        device_map = {d.device_id: d for d in fleet}
        for did in wave1.device_ids:
            w1_versions.add(device_map[did].android_version)
        assert {8, 9, 10, 11}.issubset(w1_versions)

    def test_wave1_failure_halts_entire_rollout(self):
        """#2: Wave 1 failure -> entire rollout halts, no wave 2 executed."""
        fleet = _make_fleet(50)
        # Make wave-1 device fail execution
        adb = MockADB(exec_failure_ids={fleet[0].device_id})
        ops = DeviceOps(adb=adb, script_path="test.sh")
        output = StringIO()
        orchestrator = Orchestrator(devices=fleet, device_ops=ops, output=output)
        records = _run(orchestrator.run())

        assert orchestrator.halted is True
        assert "Wave 1" in (orchestrator.halt_reason or "")

        # No wave-2 device should have been processed
        planner = WavePlanner(fleet)
        waves = planner.plan()
        if len(waves) >= 2:
            wave2_ids = set(waves[1].device_ids)
            for did in wave2_ids:
                if did in records:
                    # Should not exist as a wave-2 record at all
                    assert records[did].wave != 2, (
                        f"Wave 2 device {did} should not have been processed"
                    )

    def test_wave2_has_20_devices(self):
        """#3: Wave 2 has 20 devices (given sufficient fleet)."""
        fleet = _make_fleet(100)
        planner = WavePlanner(fleet)
        waves = planner.plan()

        assert len(waves) >= 2
        wave2 = waves[1]
        assert wave2.number == 2
        assert len(wave2.device_ids) == 20


# ======================================================================
# 4-5. Failure rate
# ======================================================================


class TestFailureRate:

    def test_wave2_5pct_failure_exceeds_threshold_halts(self):
        """#4: Wave 2 with 1/20 failures (5%) > 2% threshold -> halt + rollback."""
        fleet = _make_fleet(50)
        planner = WavePlanner(fleet)
        waves = planner.plan()
        wave2_first_id = waves[1].device_ids[0] if len(waves) >= 2 else None
        assert wave2_first_id is not None

        adb = MockADB(compliance_failure_ids={wave2_first_id})
        ops = DeviceOps(adb=adb, script_path="test.sh")
        output = StringIO()
        orchestrator = Orchestrator(devices=fleet, device_ops=ops, output=output)
        records = _run(orchestrator.run())

        assert orchestrator.halted is True
        assert "2%" in (orchestrator.halt_reason or "") or "threshold" in (
            orchestrator.halt_reason or ""
        ).lower()

    def test_wave2_zero_failures_proceeds(self):
        """#5: Wave 2 with 0 failures -> proceed to wave 3."""
        fleet = _make_fleet(100)
        adb = MockADB()  # no injected failures
        ops = DeviceOps(adb=adb, script_path="test.sh")
        output = StringIO()
        orchestrator = Orchestrator(devices=fleet, device_ops=ops, output=output)
        records = _run(orchestrator.run())

        assert orchestrator.halted is False
        # Verify wave 3 devices were processed
        planner = WavePlanner(fleet)
        waves = planner.plan()
        if len(waves) >= 3:
            wave3_sample = waves[2].device_ids[0]
            assert wave3_sample in records
            assert records[wave3_sample].status == DeviceRolloutStatus.SUCCEEDED


# ======================================================================
# 6-7. Connection drop
# ======================================================================


class TestConnectionDrop:

    def test_disconnect_mid_script_marked_interrupted(self):
        """#6: Device disconnects mid-script -> marked INTERRUPTED, not FAILED."""
        fleet = _make_fleet(50)
        target_id = fleet[0].device_id
        adb = MockADB(exec_disconnect_ids={target_id})
        ops = DeviceOps(adb=adb, script_path="test.sh")

        device = fleet[0]
        record = DeviceRolloutRecord(device_id=target_id, wave=1)
        record.status = DeviceRolloutStatus.IN_PROGRESS

        _run(ops.execute(device, record))

        assert record.status == DeviceRolloutStatus.INTERRUPTED

    def test_interrupted_excluded_from_failure_rate(self):
        """#7: Interrupted device does not affect failure rate calculation."""
        records = [
            DeviceRolloutRecord(
                device_id="d1", wave=2, status=DeviceRolloutStatus.SUCCEEDED
            ),
            DeviceRolloutRecord(
                device_id="d2", wave=2, status=DeviceRolloutStatus.SUCCEEDED
            ),
            DeviceRolloutRecord(
                device_id="d3", wave=2, status=DeviceRolloutStatus.INTERRUPTED
            ),
        ]
        rate = WavePlanner.compute_failure_rate(records)
        # 0 failures out of 2 non-interrupted -> 0%
        assert rate == 0.0

        # Now add a failure: 1 failure / 3 non-interrupted = 33.3%
        records.append(
            DeviceRolloutRecord(
                device_id="d4", wave=2, status=DeviceRolloutStatus.FAILED
            )
        )
        rate = WavePlanner.compute_failure_rate(records)
        assert abs(rate - 1 / 3) < 0.01


# ======================================================================
# 8-9. Rollback
# ======================================================================


class TestRollback:

    def test_failed_wave_triggers_rollback_of_succeeded_devices(self):
        """#8: Failed wave triggers rollback of all succeeded devices."""
        fleet = _make_fleet(50)
        planner = WavePlanner(fleet)
        waves = planner.plan()
        assert len(waves) >= 2

        # Make one wave-2 device fail compliance so the wave fails
        fail_id = waves[1].device_ids[0]
        adb = MockADB(compliance_failure_ids={fail_id})
        ops = DeviceOps(adb=adb, script_path="test.sh")
        output = StringIO()
        orchestrator = Orchestrator(devices=fleet, device_ops=ops, output=output)
        records = _run(orchestrator.run())

        assert orchestrator.halted is True

        # Succeeded devices in wave 2 should have been rolled back
        for did in waves[1].device_ids:
            if did == fail_id:
                continue
            if did in records and records[did].wave == 2:
                assert records[did].status in (
                    DeviceRolloutStatus.ROLLED_BACK,
                    DeviceRolloutStatus.INTERRUPTED,
                ), f"Device {did} should be rolled back, got {records[did].status}"

    def test_rollback_re_runs_compliance_check(self):
        """#9: Rollback re-runs compliance check (validate_compliance called)."""
        device = _make_device("rb-dev", version=10)
        adb = MockADB()
        # Spy on validate_compliance
        original = adb.validate_compliance
        call_log = []

        async def spy(device_id):
            call_log.append(device_id)
            return await original(device_id)

        adb.validate_compliance = spy

        ops = DeviceOps(adb=adb)
        record = DeviceRolloutRecord(
            device_id="rb-dev",
            wave=1,
            status=DeviceRolloutStatus.SUCCEEDED,
            preflight_snapshot={"some": "config"},
        )

        result = _run(ops.rollback(device, record))
        assert result is True
        assert "rb-dev" in call_log, "validate_compliance should be called during rollback"


# ======================================================================
# 10. Pre-flight idle check
# ======================================================================


class TestPreFlight:

    def test_device_with_current_task_skipped(self):
        """#10: Device with current_task != None -> skipped (not idle)."""
        device = _make_device("busy-dev", version=10, task="task-9999")
        adb = MockADB()
        ops = DeviceOps(adb=adb)
        record = DeviceRolloutRecord(device_id="busy-dev", wave=1)

        result = _run(ops.preflight(device, record))

        assert result is False
        assert record.status == DeviceRolloutStatus.FAILED
        assert "busy" in (record.error_details or "").lower()

    def test_busy_device_excluded_from_wave_by_planner(self):
        """Busy devices are not eligible for wave planning at all."""
        devices = [
            _make_device("idle-1", version=8),
            _make_device("idle-2", version=9),
            _make_device("idle-3", version=10),
            _make_device("idle-4", version=11),
            _make_device("idle-5", version=10),
            _make_device("busy-1", version=10, task="some-task"),
        ]
        planner = WavePlanner(devices)

        assert planner.is_eligible(devices[-1]) is False

        waves = planner.plan()
        all_wave_ids = set()
        for w in waves:
            all_wave_ids.update(w.device_ids)
        assert "busy-1" not in all_wave_ids
