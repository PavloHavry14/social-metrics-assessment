# Challenge 06 — Fleet Orchestrator: Written Explanation

## 1. Wave Strategy Rationale

Wave 1 is exactly 5 devices, with one device per Android version (8, 9, 10, 11) plus one extra. This is defined in `wave_planner.py` as `WAVE_1_SIZE = 5` and `WAVE_1_REQUIRED_VERSIONS: Set[int] = {8, 9, 10, 11}`.

This is a canary deployment. The config script runs shell commands that modify device settings and system properties. If the script has an OS-version-specific bug — say, a `settings put` command that works on Android 10+ but silently corrupts a database on Android 8 — Wave 1 catches it before it reaches the fleet. The `_select_wave1` method enforces this: it iterates eligible devices, picks one per required version in a first pass, then fills the remaining slot from whatever is left. If any required version is missing from the eligible pool, the method returns `None` and the entire rollout aborts (`wave1_ids is None` causes `plan()` to return `[]`).

Any single failure in Wave 1 halts everything. The method `should_halt_wave1` is a simple `any()` check — no threshold, no percentages:

```python
@staticmethod
def should_halt_wave1(records: List[DeviceRolloutRecord]) -> bool:
    """Wave 1 rule: ANY failure means halt."""
    return any(r.status == DeviceRolloutStatus.FAILED for r in records)
```

The reasoning: with only 5 devices, even 1 failure is a 20% rate. A config script that fails on 20% of a carefully selected, known-good set of devices is not safe to deploy to 200. More importantly, this is a canary — the whole point is to be paranoid. If the canary dies, you stop.

## 2. Progressive Scaling and the 2% Threshold

After Wave 1's 5 devices, the progression is: Wave 2 = 20, Wave 3+ = double the previous wave (capped at remaining devices). This is implemented in `WavePlanner.plan()`:

```python
wave2_size = min(20, len(pool))
...
prev_size = wave2_size if wave2_size > 0 else WAVE_1_SIZE
while pool:
    next_size = min(prev_size * 2, len(pool))
```

For a 200-device fleet, the wave sizes are roughly: 5 → 20 → 40 → 80 → 55 (remainder). Each wave is a confidence gate. If Wave 2 (20 devices) passes, we have statistical confidence that the script works on a broader population, so we can afford to go bigger.

The `FAILURE_RATE_THRESHOLD = 0.02` (2%) applies to Wave 2 onward via `should_halt_later_wave`. This is conservative by design because device bricking requires physical on-site recovery — someone has to drive to the device, reflash it, and re-enroll it. At scale, 2% of 200 devices = 4 bricked devices. Even 4 might be too many if on-site staff is limited or devices are geographically dispersed.

The threshold is a constant today. In a production system, it should be tunable based on fleet size, geographic distribution, and available recovery staff. A fleet of 10,000 devices spread across 50 sites has very different risk tolerance than 200 devices in one warehouse.

## 3. Interrupted vs Failed: Why the Distinction Matters

The `DeviceRolloutStatus` enum has both `FAILED` and `INTERRUPTED`. They are not interchangeable. In `device_ops.py`, a device becomes `INTERRUPTED` only when ADB disconnects during script execution and all retries (with backoff at 5s, 15s, 30s via `RETRY_DELAYS`) are exhausted:

```python
# Exhausted retries — mark interrupted (NOT a failure)
record.status = DeviceRolloutStatus.INTERRUPTED
record.error_details = "execute: ADB disconnected after all retries"
```

A `FAILED` device ran the script and it either errored, or it completed but the device failed post-flight compliance. We know the outcome. An `INTERRUPTED` device lost its ADB connection mid-script — we do not know whether the script finished, half-finished, or never started.

This distinction is critical in `compute_failure_rate`:

```python
non_interrupted = [
    r for r in records if r.status != DeviceRolloutStatus.INTERRUPTED
]
...
failed = sum(1 for r in non_interrupted if r.status == DeviceRolloutStatus.FAILED)
return failed / len(non_interrupted)
```

Interrupted devices are excluded from both the numerator AND the denominator. If we counted them as failures, then a flaky WiFi router causing 3 ADB disconnects in a wave of 20 would show a 15% failure rate and trigger a rollback — even though the script itself is perfectly fine and the 17 connected devices all succeeded. That would be a false rollback cascade caused by network instability, not a script bug.

By excluding interrupted devices, the failure rate reflects only devices where we actually know the script's outcome. Interrupted devices need separate handling (manual check, re-queue for next rollout).

## 4. Pre-flight Config Snapshot for Rollback

Before any script runs on a device, `DeviceOps.preflight` captures its current config state via `adb.snapshot_config()`:

```python
snapshot = await self.adb.snapshot_config(device.device_id)
record.preflight_snapshot = snapshot
```

The snapshot captures `global_settings`, `secure_settings`, `system_settings`, and key `getprop` values. This is stored on the `DeviceRolloutRecord.preflight_snapshot` field.

This is the rollback target. When rollback is triggered, `DeviceOps.rollback` passes this exact snapshot to `adb.restore_config()`:

```python
if record.preflight_snapshot is None:
    record.error_details = "rollback: no snapshot available"
    ...
    return False

restored = await self.adb.restore_config(device.device_id, record.preflight_snapshot)
```

Without this snapshot, a rollback would have no known-good state to restore to. The device would be left in whatever state the failed script produced — potentially worse than the failed update itself. The snapshot-before-modify pattern is the same principle as database transactions: you need the "before" image to undo the change.

Currently, snapshots are stored in-memory on the `DeviceRolloutRecord` dataclass. If the orchestrator process crashes mid-rollout, all snapshots are lost. In a production system, snapshots should be persisted (to a database or file) so that crash recovery can still rollback devices that were mid-wave when the orchestrator died.

## 5. Post-flight Compliance Verification

After the config script executes successfully, `DeviceOps.postflight` runs `adb.validate_compliance()`:

```python
compliant = await self.adb.validate_compliance(device.device_id)
if not compliant:
    record.status = DeviceRolloutStatus.FAILED
    record.error_details = "postflight: compliance check failed"
```

A device that was "updated" but fails compliance is a `FAILED` device, not a succeeded one. This is important because there are real scenarios where a script runs to completion (exit code 0) but doesn't produce the intended effect:

- A setting was written but reverted by a system process after a reboot.
- The script applied the change to the wrong settings namespace.
- An Android version handles a `settings put` differently and silently ignores it.

Without post-flight verification, these devices would be counted as successes, and the rollout would continue with a false sense of confidence. The compliance check is the only way to confirm that the script's *intent* was realized, not just that its *process* completed.

The orchestrator in `_run_postflight` only runs compliance on devices still in `IN_PROGRESS` status — devices that already failed during preflight or execution are skipped, which avoids wasting time checking devices we already know are bad.

## 6. Rollback Mechanics

When a wave exceeds its failure threshold, the orchestrator triggers rollback for the entire wave. In `orchestrator.py`:

```python
if should_halt:
    wave.status = "failed"
    ...
    await self._run_rollback(wave)
    self.halted = True
```

The `_run_rollback` method rolls back both `SUCCEEDED` and `FAILED` devices in the wave:

```python
if record.status in (DeviceRolloutStatus.SUCCEEDED, DeviceRolloutStatus.FAILED):
    ...
    tasks.append(self.ops.rollback(device, record))
```

Rolling back succeeded devices is deliberate. If the wave's failure rate is above threshold, the script itself is suspect. Devices that "succeeded" got the same potentially-bad script — they may be ticking time bombs that will exhibit the failure later. It is safer to revert them to a known-good state than to leave a mix of updated and reverted devices in the fleet.

`INTERRUPTED` devices are NOT rolled back — their state is unknown, and attempting to restore a config on a device with a broken ADB connection would fail anyway.

The rollback process in `DeviceOps.rollback` has three steps:
1. Check that a preflight snapshot exists (if not, rollback is impossible — log error and return).
2. Restore the snapshotted config via `adb.restore_config()`.
3. Re-verify compliance after the restore.

Step 3 is critical. If the rollback itself fails (the restore didn't take, or the device is now in a worse state), we need to know. A device that fails post-rollback compliance needs manual intervention — it is flagged but not retried, because automated retries on a device in an unknown state risk making things worse.

## 7. Why Async and Parallel Execution

All phase runners — `_run_preflight`, `_run_execute`, `_run_postflight`, `_run_rollback` — use `asyncio.gather(*tasks)` to run operations on all devices in a wave concurrently:

```python
async def _run_preflight(self, wave: Wave) -> None:
    tasks = []
    for did in wave.device_ids:
        ...
        tasks.append(self.ops.preflight(device, record))
    await asyncio.gather(*tasks)
```

Devices within a wave are independent. Device A's preflight result has no bearing on Device B's. Serial execution would be unnecessarily slow — a wave of 80 devices with 50-150ms per ADB command would take minutes instead of milliseconds.

Importantly, parallelism within a wave does not increase risk. The risk boundary is the wave itself: all devices in a wave get the same script, and the go/no-go decision happens at the wave boundary. Whether device A finishes before or after device B within the same wave is irrelevant to the safety model.

However, waves themselves are sequential. The orchestrator processes them in a `for wave in self.waves` loop, evaluating the failure rate after each wave completes. This is the core of the progressive rollout: the result of Wave N determines whether Wave N+1 runs at all. Parallelizing across waves would defeat the entire purpose of staged deployment.
