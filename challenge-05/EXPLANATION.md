# Challenge 05 — Device Compliance Validator: Written Explanation

## 1. The Stale MCC Trap

The Android property `gsm.sim.operator.numeric` caches the Mobile Country Code (MCC) and Mobile Network Code (MNC) of the last SIM card that was active on the device. Critically, this value persists even after the SIM is physically removed. A device with no SIM inserted might report `"45201"` — the MCC/MNC for Viettel in Vietnam — because a foreign SIM was previously used in the device. Naively checking this property would flag a perfectly compliant US-based device as non-compliant, or worse, pass a device that was last used with a foreign SIM but now has a US SIM that has not yet initialized.

The fix is in `_validate_sim_mcc()` (validator.py lines 273–321) and `check_sim_mcc()` (adb_runner.py lines 200–213). The runner reads `gsm.sim.state` before `gsm.sim.operator.numeric`. The validator checks state first:

- If `sim_state == "ABSENT"`: the MCC value is known to be stale. The validator returns a **warning** (not a failure) explaining that the cached MCC is being ignored. The device passes this check because there is no SIM to evaluate.
- If `sim_state == "READY"`: the SIM is active and the MCC is trustworthy. The validator extracts the first 3 digits and checks against `US_MCC_RANGE = range(310, 317)` (MCCs 310 through 316, the US allocation under ITU-T E.212).
- Any other state (e.g., `"NOT_READY"`, `"PIN_REQUIRED"`, `"PUK_REQUIRED"`): the validator issues a warning that the SIM is not fully initialized and the MCC may be unreliable, without failing the check.

This ordering — state check first, MCC check only when state is READY — prevents every variant of the stale MCC trap.

## 2. Locale Property Disagreements

Android exposes locale through multiple system properties with different semantics:

- `persist.sys.locale`: The authoritative user-chosen locale on Android 9+ (API 28+). Format: `en-US`.
- `persist.sys.language` / `persist.sys.country`: The legacy pair used before Android 9. On newer devices, these may retain values from before a locale change and are not updated by the Settings app.
- `ro.product.locale`: The factory default baked into the ROM. Read-only (`ro.` prefix). Reflects the manufacturer's locale for the device SKU, not the user's choice.

The validator in `_validate_locale()` (validator.py lines 136–178) resolves the effective locale by priority: `persist.sys.locale` first, then the legacy pair, then `ro.product.locale` as a last resort. If none are set, the check fails.

After determining the effective locale (which must normalize to `en-us`), the validator collects all non-empty property values and compares them. If they disagree — for example, `persist.sys.locale=en-US` but `persist.sys.language=vi` and `persist.sys.country=VN` — the validator issues a **warning**, not a failure. This is intentional: on Android 9+, stale legacy properties are expected behavior after a locale change. The Settings app updates `persist.sys.locale` but often does not clear the old `persist.sys.language`/`persist.sys.country` values. Failing on this disagreement would reject correctly configured devices.

The `check_locale()` method in `adb_runner.py` (lines 77–92) reads all four properties in a single pass, giving the validator the full picture to make this determination.

## 3. GPS: location_mode Is Not Enough

The validator checks location compliance in `_validate_gps_location()` (validator.py lines 202–221). It evaluates two independent conditions:

**Condition 1: `location_mode` must be `"0"` (off).** This is the global setting controlling whether location services are enabled. But this alone is insufficient.

**Condition 2: No apps may hold background or fine location permissions in "allow" mode.** An app that was previously granted `ACCESS_BACKGROUND_LOCATION` or `ACCESS_FINE_LOCATION` can retain that grant even when `location_mode` is 0. More dangerously, an app with `ACCESS_BACKGROUND_LOCATION` could programmatically call `Settings.ACTION_LOCATION_SOURCE_SETTINGS` or use device admin privileges to re-enable location mode without user interaction. Once location mode is toggled back on (even momentarily), the pre-existing permission grant means the app immediately starts receiving location updates.

The runner's `check_gps_location()` (adb_runner.py lines 115–148) executes `dumpsys appops` with a 60-second timeout (this dump can be large on devices with many apps) and filters for lines containing `ACCESS_BACKGROUND_LOCATION` or `ACCESS_FINE_LOCATION` with `"allow"` in the line. These matches are passed to the validator as `location_grants`.

If any grants are found, the validator produces a **hard failure**, not a warning. The message includes up to 5 sample grants (`grants[:5]`). This is strict by design: a single app with background location permission is a compliance risk regardless of the current location_mode setting.

## 4. WiFi: Saved Networks Are the Real Risk

The `_validate_wifi()` function (validator.py lines 224–241) checks two things:

**`wifi_on` must be `"0"`.** This confirms the WiFi radio is currently off. But turning off WiFi is a one-tap toggle — it does not remove saved network configurations.

**Zero saved networks.** The runner's `check_wifi()` (adb_runner.py lines 154–178) parses `dumpsys wifi` output for lines containing `WifiConfiguration` or `ConfiguredNetworks`. Each match represents a network the device will automatically connect to when WiFi is toggled on. This includes:

- Open networks (auto-join without authentication)
- WPA/WPA2 networks with saved passwords
- Hidden networks, which are particularly dangerous

Hidden networks deserve special attention. When a device has a hidden network saved, it actively broadcasts probe requests for that SSID. Any attacker who sets up a rogue access point with the matching SSID will receive a connection attempt from the device. This happens regardless of `wifi_on` status on some Android versions (the probing can occur during background scanning even when WiFi is "off" in the UI, if `wifi_scan_always_enabled` is set).

The validator treats saved networks as a hard failure, not a warning. Even with `wifi_on=0`, the device is one toggle (or one `svc wifi enable` command from a rogue app) away from connecting to a known network and leaking its real IP, location, or traffic.

## 5. The USB Debugging Catch-22

`_validate_usb_debugging()` (validator.py lines 377–402) reads `adb_enabled` and `development_settings_enabled`. When `adb_enabled` is `"1"`, the validator produces a **warning**, not a failure:

```python
if adb_enabled == "1":
    warnings.append(
        "usb_debugging: adb_enabled is '1'. This is expected during ADB-based "
        "validation (catch-22). Ensure USB debugging is disabled before "
        "production deployment."
    )
```

This is a genuine catch-22: the validation itself runs over ADB (`adb -s {serial} shell settings get global adb_enabled`). For this command to execute, USB debugging must be enabled. The value will therefore always be `"1"` during any ADB-based compliance check. Treating this as a hard failure would make every device fail, rendering the check useless.

The intended workflow is: run all compliance checks with USB debugging enabled, verify everything else passes, then disable USB debugging (`adb shell settings put global adb_enabled 0`) as the final step before production deployment. Since that final command severs the ADB connection, it cannot be verified programmatically — it must be confirmed through an out-of-band mechanism (e.g., verifying the device is no longer visible in `adb devices`).

The `development_settings_enabled` check is also a warning for the same reason: developer options must be enabled to enable USB debugging, which is required for the validation to run.

## 6. Two-File Architecture (Runner vs Validator)

The compliance system is split into two files with a strict boundary:

**`adb_runner.py`** is device-specific. It knows how to execute ADB commands (`subprocess.run` with `["adb", "-s", serial, "shell", ...]`), handle timeouts (`ADB_TIMEOUT_SECONDS = 30`, extended to 60s for `dumpsys` commands), and structure raw output. Each method returns a dict with `{"check": str, "raw_output": dict, "commands_run": list[str]}`. It has no opinions about what constitutes a pass or fail.

**`validator.py`** is pure logic. It takes the list of dicts from `run_all()` and evaluates each one against compliance rules. It uses a dispatch table `_VALIDATORS` (line 465) mapping check names to validator functions. It never imports `subprocess`, never references ADB, and never touches a device. Its only inputs are Python dicts; its only output is a report dict with `pass`, `failures`, `warnings`, and `details`.

**Why this separation matters:**

1. **Testing without hardware.** The validator can be unit-tested by constructing raw_output dicts that simulate any device state — absent SIM, foreign MCC, stale locale properties, saved WiFi networks. No Android device or emulator is needed. The test constructs a dict, calls `_validate_sim_mcc(raw)`, and asserts on the returned failures/warnings.

2. **Swappable transport.** The runner abstracts the ADB transport. In production, devices might be accessed over remote ADB through a proxy server, through a device farm API (like Firebase Test Lab), or through an SSH tunnel to a remote host running ADB. A different runner class that implements the same `run_all() -> list[dict]` interface can be substituted without changing any validation logic. The validator does not know or care how the raw data was collected.

3. **Independent evolution.** ADB command syntax changes between Android versions (e.g., `locksettings` behavior varies across Android 8/10/12). Compliance rules change per deployment requirements (different MCC ranges for different target countries, different app version windows). By separating the two concerns, a change to how we read WiFi state from Android 14 only touches `adb_runner.py`, while a change to what constitutes a valid timezone only touches `validator.py`.

The `_result()` static method (adb_runner.py lines 61–71) enforces a consistent output structure across all checks, and the `validate()` function (validator.py lines 484–545) iterates over the list generically, dispatching each entry by its `check` name. This makes adding a new check a two-step process: add a `check_*` method to the runner and a `_validate_*` function to the validator's dispatch table.
