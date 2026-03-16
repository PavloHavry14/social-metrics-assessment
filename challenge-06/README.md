# Challenge 06: Fleet Orchestrator with Wave-Based Rollout

Coordinates configuration rollouts across a fleet of Android devices using
progressive wave-based deployment with automatic failure detection, halt
logic, and rollback capabilities.

---

## 1. System Architecture

```
+-------------------------------------------------------------------+
|                        Orchestrator                                |
|  (main loop: plan waves, process each, halt/continue decisions)   |
+--------+----------+----------+------------------------------------+
         |          |          |
         v          v          v
+------------+ +----------+ +-------------------------------------------+
| WavePlanner| | Dashboard| |               DeviceOps                   |
| - plan()   | | - render | | - preflight(device, record)               |
| - wave 1   | | - log_*  | | - execute(device, record)                |
|   selection| | - summary| | - postflight(device, record)              |
| - failure  | |          | | - rollback(device, record)                |
|   rate calc| |          | |                                           |
+------------+ +----------+ +-------------------+-----------------------+
                                                 |
                                                 v
                                      +--------------------+
                                      |   MockADB / ADB    |
                                      | - ping()           |
                                      | - snapshot_config() |
                                      | - push_script()    |
                                      | - execute_script() |
                                      | - validate_compl() |
                                      | - restore_config() |
                                      +--------+-----------+
                                               |
                                               | adb -s {id} shell
                                               v
                                      +--------------------+
                                      |  Device Fleet      |
                                      +--------------------+
```

---

## 2. Wave Progression

```
Wave 1 (Canary)                Wave 2                  Wave 3+
+-------------------------+    +-------------------+    +-------------------+
| 5 devices               |    | 20 devices        |    | prev_size * 2     |
| - 1x Android 8          |    | (or remaining)    |    | (or remaining)    |
| - 1x Android 9          |    |                   |    |                   |
| - 1x Android 10         |    | Eligible, idle,   |    | Progressive       |
| - 1x Android 11         |    | good connection   |    | doubling          |
| - 1x any version        |    |                   |    |                   |
|                          |    |                   |    |                   |
| Version diversity        |    |                   |    |                   |
| REQUIRED                 |    |                   |    |                   |
+-------------------------+    +-------------------+    +-------------------+
        |                             |                        |
        | ANY fail -> HALT            | >2% fail -> HALT       | >2% fail -> HALT
        v                             v                        v
   strict gate                   threshold gate           threshold gate


Sizing example for 200-device fleet (170 eligible):
+--------+---------+------------------------------------------+
| Wave   | Size    | Cumulative                               |
+--------+---------+------------------------------------------+
|   1    |    5    |     5 devices                            |
|   2    |   20    |    25 devices                            |
|   3    |   40    |    65 devices                            |
|   4    |   80    |   145 devices                            |
|   5    |   25    |   170 devices (remaining)                |
+--------+---------+------------------------------------------+

Device eligibility filter:
+--------------------+     +------------------+
| All fleet devices  | --> | connection_quality|
+--------------------+     | == "good"?        |
                           +------------------+
                             |            |
                            yes           no (offline/flaky)
                             v            v
                      +-------------+   SKIP
                      | current_task|
                      | == None?    |
                      +-------------+
                        |         |
                       yes        no (busy)
                        v         v
                    ELIGIBLE    SKIP
```

---

## 3. Per-Device Lifecycle

```
+------------------------------------------------------------------+
|                    Per-Device Pipeline                            |
+------------------------------------------------------------------+

    PENDING
       |
       v
+--[ PREFLIGHT ]----------------------------------------------------+
|                                                                    |
|   1. Ping test      adb shell echo ok                             |
|      |                                                             |
|      +-- fail --> FAILED ("preflight: ping failed")                |
|      |                                                             |
|   2. Idle check     current_task == None?                          |
|      |                                                             |
|      +-- busy --> FAILED ("preflight: device busy")                |
|      |                                                             |
|   3. Config snapshot                                               |
|      adb shell settings list global/secure/system                  |
|      adb shell getprop (key values)                                |
|      |                                                             |
|      +-- error --> FAILED ("preflight: snapshot error")            |
|      |                                                             |
|      +-- ok --> store in record.preflight_snapshot                 |
+--------------------------------------------------------------------+
       |
       v
+--[ EXECUTE ]------------------------------------------------------+
|                                                                    |
|   1. Push script    adb push config_script.sh /data/local/tmp/    |
|      +-- fail --> FAILED                                           |
|                                                                    |
|   2. Run script     adb shell sh /data/local/tmp/config_script.sh |
|      |                                                             |
|      +-- "success"      --> continue to postflight                 |
|      +-- "failed"       --> FAILED                                 |
|      +-- "disconnected" --> retry with backoff:                     |
|                             [5s, 15s, 30s]                         |
|                             exhausted --> INTERRUPTED               |
+--------------------------------------------------------------------+
       |
       v
+--[ POSTFLIGHT ]---------------------------------------------------+
|                                                                    |
|   Compliance validator re-run                                      |
|      |                                                             |
|      +-- pass --> SUCCEEDED                                        |
|      +-- fail --> FAILED ("postflight: compliance check failed")   |
+--------------------------------------------------------------------+
       |
       v
    SUCCEEDED / FAILED / INTERRUPTED
```

---

## 4. Failure Handling Decision Tree

```
                     Wave completed
                          |
                          v
                  +----------------+
                  | Compute        |
                  | failure_rate   |
                  |                |
                  | INTERRUPTED    |
                  | devices are    |
                  | EXCLUDED from  |
                  | denominator    |
                  +-------+--------+
                          |
               +----------+----------+
               |                     |
               v                     v
         Wave 1?                Wave 2+ ?
               |                     |
               v                     v
    +--------------------+  +---------------------+
    | ANY failure at all?|  | failure_rate > 2% ?  |
    +--------------------+  +---------------------+
      |            |          |             |
     yes           no        yes            no
      v            v          v             v
    HALT       CONTINUE     HALT        CONTINUE
    + rollback              + rollback
    this wave               this wave


Failure rate formula:

                     count(status == FAILED)
  failure_rate = --------------------------------
                 count(status != INTERRUPTED)

  - INTERRUPTED devices (ADB drops) are removed from
    both numerator and denominator
  - This prevents flaky connections from triggering
    unnecessary rollouts
```

---

## 5. Rollback Flow

```
                      HALT triggered
                           |
                           v
               +------------------------+
               | For each device in     |
               | wave where status is   |
               | SUCCEEDED or FAILED:   |
               +------------------------+
                           |
                           v
               +------------------------+
               | Has preflight_snapshot? |
               +------------------------+
                 |                |
                yes               no
                 v                v
          +------------+    +-----------+
          | restore_   |    | Log error |
          | config()   |    | "no       |
          | from       |    |  snapshot" |
          | snapshot   |    +-----------+
          +-----+------+
                |
           +----+----+
           |         |
          ok       failed
           |         |
           v         v
    +----------+  +-------------+
    | validate |  | Log error   |
    | complian |  | "restore    |
    | ce()     |  |  failed"    |
    +-----+----+  +-------------+
          |
     +----+----+
     |         |
    pass     fail
     |         |
     v         v
  ROLLED    Log error
  _BACK     "post-rollback
             compliance
             failed"
```

---

## 6. Dashboard Output

```
================================================================
  FLEET ROLLOUT DASHBOARD
================================================================

Wave 1/5 [COMPLETED]
  device-001 (Android 8)  - succeeded
  device-002 (Android 9)  - succeeded
  device-003 (Android 10) - succeeded
  device-004 (Android 11) - succeeded
  device-005 (Android 8)  - succeeded

Wave 2/5 [COMPLETED]
  device-006 (Android 10) - succeeded
  device-007 (Android 9)  - succeeded
  ...
  device-012 (Android 11) - interrupted (connection lost)
  ...

Wave 3/5 [FAILED]
  device-026 (Android 8)  - succeeded
  device-030 (Android 10) - failed -- postflight: compliance check failed
  ...

~~~ Rolling back Wave 3 ~~~

!!! ROLLOUT HALTED: Wave 3 failure rate 3.1% exceeds 2% threshold

----------------------------------------------------------------
Status: pending=105, in_progress=0, succeeded=42,
        failed=2, interrupted=1, rolled_back=20
----------------------------------------------------------------

Rollout finished at 2026-03-16T12:34:56.789012+00:00Z
```

---

## Key Design Decisions

- **Interrupted != Failed:** Devices that lose ADB connection mid-script are
  marked `INTERRUPTED` and excluded from the failure rate denominator. This
  prevents flaky USB/network connections from triggering unnecessary
  fleet-wide halts.
- **Pre-flight snapshot:** Every device's config state (`settings list`,
  `getprop`) is snapshotted before execution. This snapshot is the restore
  target during rollback.
- **Post-flight compliance re-run:** After script execution, the compliance
  validator runs again. A device that was updated but fails compliance is
  counted as a failure.
- **Rollback restores snapshotted config** and re-verifies compliance after
  restoration. This ensures the device returns to a known-good state.
- **Wave 1 version diversity** is a hard requirement. If any of Android
  8/9/10/11 cannot be represented in wave 1, the entire rollout is aborted.
