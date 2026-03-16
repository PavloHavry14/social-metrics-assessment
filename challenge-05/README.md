# Challenge 05: Device Compliance Validator

Validates Android device configuration compliance via ADB. An `ADBRunner`
executes shell commands and returns structured dicts; a `Validator` evaluates
each dict and produces a pass/fail/warning verdict.

---

## 1. System Architecture

```
+-------------------------------------------------------------------+
|                          CLI / Caller                              |
+-------------------------------------------------------------------+
         |                                       |
         | device_serial                         | JSON (check_results)
         v                                       v
+--------------------+                  +--------------------+
|     ADBRunner      |   list[dict]     |     Validator      |
|                    | ---------------> |                    |
| - check_locale()   |  per-check       | - _validate_*()   |
| - check_timezone() |  raw output      | - dispatch table  |
| - check_gps_...()  |                  | - verdict logic   |
| - check_wifi()     |                  |                    |
| - check_ip()       |                  +--------+-----------+
| - check_sim_mcc()  |                           |
| - check_device_...()|                          v
| - check_screen_...() |             +------------------------+
| - check_usb_...()  |              |       Verdict           |
| - check_app_state()|              | {                       |
+--------+-----------+              |   "pass": bool,         |
         |                          |   "failures": [...],    |
         | adb -s {serial} shell    |   "warnings": [...],    |
         v                          |   "details": {per-check}|
+--------------------+              | }                       |
|   Android Device   |              +------------------------+
+--------------------+
```

---

## 2. Compliance Check Flow

All 10 checks run in sequence via `run_all()`.

```
run_all()
  |
  v
+--[ 1. Locale ]-----------------------------------------------------+
|  getprop persist.sys.locale                                         |
|  getprop persist.sys.language / persist.sys.country                 |
|  getprop ro.product.locale                                          |
|  -> Must resolve to en-US. Check property agreement.                |
+---------------------------------------------------------------------+
  |
  v
+--[ 2. Timezone ]----------------------------------------------------+
|  getprop persist.sys.timezone         -> must be US timezone         |
|  settings get global auto_time_zone   -> must be "0" (manual)       |
+---------------------------------------------------------------------+
  |
  v
+--[ 3. GPS / Location ]----------------------------------------------+
|  settings get secure location_mode    -> must be "0" (off)           |
|  dumpsys appops                       -> scan for location grants    |
+---------------------------------------------------------------------+
  |
  v
+--[ 4. WiFi ]--------------------------------------------------------+
|  settings get global wifi_on          -> must be "0" (off)           |
|  dumpsys wifi                         -> saved networks must be zero |
+---------------------------------------------------------------------+
  |
  v
+--[ 5. IP Validation ]-----------------------------------------------+
|  curl -s ifconfig.me                  -> valid public IPv4           |
|  (GeoIP lookup deferred to external service)                        |
+---------------------------------------------------------------------+
  |
  v
+--[ 6. SIM / MCC ]---------------------------------------------------+
|  getprop gsm.sim.state               -> drives MCC trust            |
|  getprop gsm.sim.operator.numeric    -> MCC 310-316 if SIM READY    |
+---------------------------------------------------------------------+
  |
  v
+--[ 7. Device Name ]-------------------------------------------------+
|  getprop net.hostname                                                |
|  settings get global device_name                                     |
|  -> reject automation-revealing patterns (bot, device-001, etc.)     |
+---------------------------------------------------------------------+
  |
  v
+--[ 8. Screen Lock ]-------------------------------------------------+
|  locksettings get-disabled            -> must be "false"             |
|  lockscreen.password_type (fallback)  -> not in {0, 65536, null}    |
+---------------------------------------------------------------------+
  |
  v
+--[ 9. USB Debugging ]-----------------------------------------------+
|  settings get global adb_enabled                                     |
|  development_settings_enabled                                        |
|  -> WARNING only (catch-22: we are running via ADB)                  |
+---------------------------------------------------------------------+
  |
  v
+--[ 10. App State ]--------------------------------------------------+
|  pm list packages | grep {pkg}        -> installed?                  |
|  dumpsys package {pkg} | versionName  -> version in range?           |
|  ls shared_prefs/                     -> leftover account data?      |
+---------------------------------------------------------------------+
  |
  v
Aggregate failures + warnings -> final verdict
```

---

## 3. SIM/MCC Stale Trap Decision Tree

The stale MCC trap: when SIM is absent, Android caches the last-known MCC
value. Checking MCC without first checking SIM state leads to false passes
or false failures.

```
                    getprop gsm.sim.state
                           |
              +------------+------------+
              |            |            |
              v            v            v
           ABSENT        READY       (other)
              |            |            |
              v            v            v
         +---------+  +---------+  +----------+
         | WARNING | | Check   |  | WARNING  |
         | MCC is  | | MCC now |  | SIM not  |
         | stale/  | |         |  | fully    |
         | cached  | |         |  | init'd   |
         | IGNORE  | |         |  | MCC may  |
         | MCC     | |         |  | be wrong |
         +---------+ +----+----+  +----------+
                           |
                           v
                  getprop gsm.sim.operator.numeric
                           |
                  +--------+--------+
                  |                 |
                  v                 v
           MCC in 310-316    MCC outside range
           (US carrier)      or unparseable
                  |                 |
                  v                 v
               PASS             FAILURE
               (valid            (non-US SIM
                US SIM)           detected)
```

---

## 4. GPS + Background Location Decision Tree

```
          settings get secure location_mode
                       |
              +--------+--------+
              |                 |
              v                 v
           mode = "0"       mode != "0"
           (off)            (on / high accuracy)
              |                 |
              v                 v
           (good)           FAILURE
              |            "location_mode must be 0"
              |
              v
         dumpsys appops
         scan for ACCESS_BACKGROUND_LOCATION
               and ACCESS_FINE_LOCATION
               with mode = "allow"
                       |
              +--------+--------+
              |                 |
              v                 v
          no grants        grants found
              |                 |
              v                 v
            PASS             FAILURE
                          "N app(s) have
                           background/fine
                           location allow"

    NOTE: Background location grants are a failure
    EVEN WHEN location_mode = 0. Apps retain the
    permission and can activate location silently.
```

---

## 5. WiFi Decision Tree

```
         settings get global wifi_on
                       |
              +--------+--------+
              |                 |
              v                 v
          wifi_on = "0"     wifi_on != "0"
          (radio off)       (radio on)
              |                 |
              |                 v
              |              FAILURE
              |           "wifi must be off"
              v
         dumpsys wifi
         parse WifiConfiguration /
         ConfiguredNetworks entries
                       |
              +--------+--------+
              |                 |
              v                 v
         zero saved        N saved networks
         networks               |
              |                 v
              v              FAILURE
            PASS          "N saved network(s)
                           found -- must be zero"

    NOTE: Hidden/saved WiFi networks persist even
    when the radio is off. The device can auto-join
    if WiFi is ever re-enabled.
```

---

## 6. Verdict Aggregation

```
   Per-Check Validators
   (10 functions)
         |
         |  each returns (failures[], warnings[])
         v
+-------------------------------------------+
| Aggregation Loop                          |
|                                           |
|   all_failures = []                       |
|   all_warnings = []                       |
|                                           |
|   for each check result:                  |
|     validator_fn = _VALIDATORS[check]     |
|     f, w = validator_fn(raw_output)       |
|     all_failures.extend(f)                |
|     all_warnings.extend(w)                |
|                                           |
|     details[check] = {                    |
|       "status": "fail" if f              |
|                  else "warn" if w         |
|                  else "pass",             |
|       "failures": f,                      |
|       "warnings": w                       |
|     }                                     |
+-------------------------------------------+
         |
         v
+-------------------------------------------+
|            Final Verdict                  |
|                                           |
|  len(all_failures) == 0                   |
|       |               |                  |
|       v               v                  |
|     "pass":true    "pass":false           |
|                                           |
|  failures = [hard blockers]               |
|  warnings = [non-blocking flags]          |
+-------------------------------------------+

Per-check status roll-up:

  +--------+   has failures?   +--------+
  | CHECK  | ----yes---------> | "fail" |
  +--------+                   +--------+
       |
       | no
       v
  has warnings?
       |           |
      yes          no
       v           v
  +---------+  +--------+
  | "warn"  |  | "pass" |
  +---------+  +--------+

Global verdict:
  ANY "fail" in any check -> overall pass = false
  All checks pass/warn    -> overall pass = true
```

---

## Key Traps and Design Notes

- **Stale MCC when SIM absent:** Android caches `gsm.sim.operator.numeric`
  from the last inserted SIM. Always check `gsm.sim.state` FIRST.
- **Locale property disagreement:** `persist.sys.locale` is authoritative on
  Android 9+. `persist.sys.language` / `persist.sys.country` may be stale
  legacy values. Disagreement triggers a warning, not a failure.
- **Background location grants even when location_mode=0:** Apps retain
  granted permissions regardless of the global toggle. Both must be clean.
- **Hidden saved WiFi networks:** Networks persist in `WifiConfiguration`
  even with the radio off. The device will auto-connect if WiFi is toggled.
- **USB debugging catch-22:** The validator itself runs via ADB, so
  `adb_enabled=1` is expected during validation. Flagged as a warning only.
