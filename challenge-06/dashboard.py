"""
Real-time dashboard for fleet rollout status.

Provides both a streaming line-by-line reporter and a full summary renderer.
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional, TextIO

from .device_ops import DeviceRolloutRecord, DeviceRolloutStatus
from .wave_planner import Wave


# ---------------------------------------------------------------------------
# Status glyphs
# ---------------------------------------------------------------------------

_STATUS_GLYPH = {
    DeviceRolloutStatus.PENDING: "  ",       # hourglass-pending
    DeviceRolloutStatus.IN_PROGRESS: "  ",   # spinner
    DeviceRolloutStatus.SUCCEEDED: "  ",     # checkmark
    DeviceRolloutStatus.FAILED: "  ",        # cross
    DeviceRolloutStatus.INTERRUPTED: "  ",   # lightning
    DeviceRolloutStatus.ROLLED_BACK: "  ",   # rewind
}

_STATUS_LABEL = {
    DeviceRolloutStatus.PENDING: "pending",
    DeviceRolloutStatus.IN_PROGRESS: "in progress",
    DeviceRolloutStatus.SUCCEEDED: "succeeded",
    DeviceRolloutStatus.FAILED: "failed",
    DeviceRolloutStatus.INTERRUPTED: "interrupted (connection lost)",
    DeviceRolloutStatus.ROLLED_BACK: "rolled back",
}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class Dashboard:
    """Renders rollout progress to a text stream."""

    def __init__(
        self,
        waves: List[Wave],
        records: Dict[str, DeviceRolloutRecord],
        android_versions: Optional[Dict[str, int]] = None,
        output: TextIO = sys.stdout,
    ):
        self._waves = waves
        self._records = records
        self._android_versions = android_versions or {}
        self._out = output

    def _write(self, text: str = "") -> None:
        self._out.write(text + "\n")

    # -- Per-device line ------------------------------------------------------

    def _device_line(self, device_id: str) -> str:
        record = self._records.get(device_id)
        if record is None:
            return f"  ? {device_id} - no record"

        glyph = _STATUS_GLYPH.get(record.status, "?")
        label = _STATUS_LABEL.get(record.status, record.status.value)

        version_str = ""
        if device_id in self._android_versions:
            version_str = f" (Android {self._android_versions[device_id]})"

        error_str = ""
        if record.error_details:
            error_str = f" — {record.error_details}"

        return f"  {glyph} {device_id}{version_str} - {label}{error_str}"

    # -- Wave block -----------------------------------------------------------

    def _wave_block(self, wave: Wave, total_waves: int) -> List[str]:
        lines: List[str] = []
        lines.append(f"Wave {wave.number}/{total_waves} [{wave.status.upper()}]")
        for did in wave.device_ids:
            lines.append(self._device_line(did))
        return lines

    # -- Summary counters -----------------------------------------------------

    def _summary_line(self) -> str:
        counts: Counter = Counter()
        for rec in self._records.values():
            counts[rec.status] += 1

        parts = [
            f"pending={counts.get(DeviceRolloutStatus.PENDING, 0)}",
            f"in_progress={counts.get(DeviceRolloutStatus.IN_PROGRESS, 0)}",
            f"succeeded={counts.get(DeviceRolloutStatus.SUCCEEDED, 0)}",
            f"failed={counts.get(DeviceRolloutStatus.FAILED, 0)}",
            f"interrupted={counts.get(DeviceRolloutStatus.INTERRUPTED, 0)}",
            f"rolled_back={counts.get(DeviceRolloutStatus.ROLLED_BACK, 0)}",
        ]
        return "Status: " + ", ".join(parts)

    # -- Full render ----------------------------------------------------------

    def render(self) -> str:
        """Render the full dashboard to a string and write it to the output stream."""
        lines: List[str] = []
        total = len(self._waves)

        lines.append("=" * 64)
        lines.append("  FLEET ROLLOUT DASHBOARD")
        lines.append("=" * 64)
        lines.append("")

        for wave in self._waves:
            lines.extend(self._wave_block(wave, total))
            lines.append("")

        lines.append("-" * 64)
        lines.append(self._summary_line())
        lines.append("-" * 64)

        output = "\n".join(lines)
        self._write(output)
        return output

    # -- Event-based updates --------------------------------------------------

    def log_wave_start(self, wave: Wave, total_waves: int) -> None:
        self._write(
            f"\n>>> Starting Wave {wave.number}/{total_waves} "
            f"({len(wave.device_ids)} devices)"
        )

    def log_wave_result(
        self, wave: Wave, total_waves: int, failure_rate: float
    ) -> None:
        self._write(
            f"<<< Wave {wave.number}/{total_waves} {wave.status.upper()} "
            f"(failure_rate={failure_rate:.1%})"
        )

    def log_halt(self, reason: str) -> None:
        self._write(f"\n!!! ROLLOUT HALTED: {reason}")

    def log_rollback_start(self, wave: Wave) -> None:
        self._write(f"\n~~~ Rolling back Wave {wave.number} ~~~")

    def log_device_update(self, device_id: str) -> None:
        self._write(self._device_line(device_id))

    def log_final_report(self) -> None:
        self._write("\n")
        self.render()
        self._write(f"\nRollout finished at {datetime.now(timezone.utc).isoformat()}Z")
