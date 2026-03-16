"""
validator.py — Evaluates raw ADB check output and produces a compliance verdict.

Takes the list of dicts produced by ``adb_runner.ADBRunner.run_all()`` and
returns a single report:

    {
        "pass": bool,          # True only if zero failures
        "failures": [...],     # Hard blockers — device must not be deployed
        "warnings": [...],     # Non-blocking flags for human review
    }
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Constants
# ------------------------------------------------------------------ #

# Valid US timezones (Olson / IANA identifiers)
US_TIMEZONES: frozenset[str] = frozenset(
    {
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Los_Angeles",
        "America/Phoenix",
        "America/Anchorage",
        "America/Adak",
        "America/Honolulu",
        "America/Boise",
        "America/Indiana/Indianapolis",
        "America/Indiana/Knox",
        "America/Indiana/Marengo",
        "America/Indiana/Petersburg",
        "America/Indiana/Tell_City",
        "America/Indiana/Vevay",
        "America/Indiana/Vincennes",
        "America/Indiana/Winamac",
        "America/Juneau",
        "America/Kentucky/Louisville",
        "America/Kentucky/Monticello",
        "America/Menominee",
        "America/Metlakatla",
        "America/Nome",
        "America/North_Dakota/Beulah",
        "America/North_Dakota/Center",
        "America/North_Dakota/New_Salem",
        "America/Sitka",
        "America/Yakutat",
        "Pacific/Honolulu",
        "US/Eastern",
        "US/Central",
        "US/Mountain",
        "US/Pacific",
        "US/Alaska",
        "US/Hawaii",
        "US/Arizona",
    }
)

# US Mobile Country Codes (ITU-T E.212)
US_MCC_RANGE = range(310, 317)  # 310–316 inclusive

# Regex patterns that suggest automated / farm device naming
AUTOMATION_NAME_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?:device|phone|handset|node|worker)[\-_]?\d{2,}", re.IGNORECASE),
    re.compile(r"\b(?:bot|auto|test|farm|emul|clone|dummy|fake)\b", re.IGNORECASE),
    re.compile(r"^\d+$"),  # purely numeric name
]

# lockscreen.password_type values that indicate *no* security
INSECURE_PASSWORD_TYPES: frozenset[str] = frozenset({"0", "65536", "null"})

# Known datacenter / cloud provider IP ranges are impractical to embed.
# We use a simple heuristic: reject obviously-private or loopback IPs and
# flag anything that doesn't look like a valid public IPv4.  Full GeoIP
# classification should be layered on top via an external service.

# Acceptable app version range (inclusive).  Adjust per deployment window.
MIN_APP_VERSION: str = "20.0.0"
MAX_APP_VERSION: str = "99.99.99"

# Files in shared_prefs that indicate a prior login / leftover account data.
ACCOUNT_PREFS_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"account", re.IGNORECASE),
    re.compile(r"login", re.IGNORECASE),
    re.compile(r"auth", re.IGNORECASE),
    re.compile(r"session", re.IGNORECASE),
    re.compile(r"token", re.IGNORECASE),
    re.compile(r"user", re.IGNORECASE),
]


# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #


def _version_tuple(version_str: str) -> tuple[int, ...]:
    """Convert a dotted version string to a comparable tuple of ints."""
    parts: list[int] = []
    for segment in version_str.split("."):
        digits = re.match(r"(\d+)", segment)
        parts.append(int(digits.group(1)) if digits else 0)
    return tuple(parts)


def _is_valid_public_ipv4(ip_str: str) -> bool:
    """Return True if *ip_str* looks like a usable public IPv4 address."""
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        isinstance(addr, ipaddress.IPv4Address)
        and addr.is_global
        and not addr.is_loopback
        and not addr.is_reserved
        and not addr.is_multicast
    )


# ------------------------------------------------------------------ #
#  Per-check validators
# ------------------------------------------------------------------ #


def _validate_locale(raw: dict) -> tuple[list[str], list[str]]:
    """All locale properties must agree and resolve to en-US."""
    failures: list[str] = []
    warnings: list[str] = []

    persist_locale = raw.get("getprop persist.sys.locale", "").strip()
    persist_lang = raw.get("getprop persist.sys.language", "").strip()
    persist_country = raw.get("getprop persist.sys.country", "").strip()
    ro_locale = raw.get("getprop ro.product.locale", "").strip()

    # Determine the effective locale (Android 9+ resolution order)
    if persist_locale:
        effective = persist_locale
    elif persist_lang and persist_country:
        effective = f"{persist_lang}-{persist_country}"
    elif ro_locale:
        effective = ro_locale
    else:
        failures.append("locale: unable to determine effective locale — all properties empty")
        return failures, warnings

    if effective.lower().replace("_", "-") != "en-us":
        failures.append(f"locale: effective locale is '{effective}', expected 'en-US'")

    # Check for disagreement between properties (may indicate stale values)
    known_values: dict[str, str] = {}
    if persist_locale:
        known_values["persist.sys.locale"] = persist_locale
    if persist_lang and persist_country:
        known_values["legacy (language+country)"] = f"{persist_lang}-{persist_country}"
    if ro_locale:
        known_values["ro.product.locale"] = ro_locale

    normalized = {k: v.lower().replace("_", "-") for k, v in known_values.items()}
    unique_values = set(normalized.values())
    if len(unique_values) > 1:
        detail = ", ".join(f"{k}={v}" for k, v in known_values.items())
        warnings.append(
            f"locale: properties disagree ({detail}). "
            "Legacy props may be stale on Android 9+."
        )

    return failures, warnings


def _validate_timezone(raw: dict) -> tuple[list[str], list[str]]:
    """Timezone must be a US zone and auto-timezone must be disabled."""
    failures: list[str] = []
    warnings: list[str] = []

    tz = raw.get("getprop persist.sys.timezone", "").strip()
    auto_tz = raw.get("settings get global auto_time_zone", "").strip()

    if not tz:
        failures.append("timezone: persist.sys.timezone is empty")
    elif tz not in US_TIMEZONES:
        failures.append(f"timezone: '{tz}' is not a recognized US timezone")

    if auto_tz != "0":
        failures.append(
            f"timezone: auto_time_zone is '{auto_tz}', must be '0' (manual)"
        )

    return failures, warnings


def _validate_gps_location(raw: dict) -> tuple[list[str], list[str]]:
    """Location mode must be 0 (off) and no apps may hold background location."""
    failures: list[str] = []
    warnings: list[str] = []

    mode = raw.get("location_mode", "").strip()
    grants: list[str] = raw.get("location_grants", [])

    if mode != "0":
        failures.append(
            f"gps_location: location_mode is '{mode}', must be '0' (off)"
        )

    if grants:
        failures.append(
            f"gps_location: {len(grants)} app(s) have background/fine location "
            f"permission set to allow: {grants[:5]}"
        )

    return failures, warnings


def _validate_wifi(raw: dict) -> tuple[list[str], list[str]]:
    """WiFi must be off and there must be zero saved networks."""
    failures: list[str] = []
    warnings: list[str] = []

    wifi_on = raw.get("wifi_on", "").strip()
    saved: list[str] = raw.get("saved_networks", [])

    if wifi_on != "0":
        failures.append(f"wifi: wifi_on is '{wifi_on}', must be '0' (off)")

    if saved:
        failures.append(
            f"wifi: {len(saved)} saved network(s) found — must be zero. "
            f"Sample: {saved[:3]}"
        )

    return failures, warnings


def _validate_ip(raw: dict) -> tuple[list[str], list[str]]:
    """Public IP must be a valid, non-private IPv4.

    Full US-based / non-datacenter classification requires an external GeoIP
    service.  We perform basic structural validation here and flag anything
    we can rule out locally.
    """
    failures: list[str] = []
    warnings: list[str] = []

    ip_str = raw.get("public_ip", "").strip()

    if not ip_str:
        failures.append("ip_validation: could not retrieve public IP (curl may be unavailable)")
        return failures, warnings

    if not _is_valid_public_ipv4(ip_str):
        failures.append(
            f"ip_validation: '{ip_str}' is not a valid public IPv4 address"
        )
    else:
        warnings.append(
            f"ip_validation: public IP is {ip_str}. "
            "GeoIP lookup required to confirm US-based, non-datacenter origin."
        )

    return failures, warnings


def _validate_sim_mcc(raw: dict) -> tuple[list[str], list[str]]:
    """SIM state drives whether MCC is trustworthy.

    If ABSENT  -> ignore MCC, pass with warning (stale MCC trap).
    If READY   -> MCC first 3 digits must be 310–316 (US).
    Otherwise  -> warning (SIM not fully initialized).
    """
    failures: list[str] = []
    warnings: list[str] = []

    sim_state = raw.get("getprop gsm.sim.state", "").strip().upper()
    operator_numeric = raw.get("getprop gsm.sim.operator.numeric", "").strip()

    if sim_state == "ABSENT":
        warnings.append(
            "sim_mcc: SIM is ABSENT — MCC value is stale/cached and ignored. "
            "Device has no active SIM."
        )
        return failures, warnings

    if sim_state != "READY":
        warnings.append(
            f"sim_mcc: SIM state is '{sim_state}' (not READY). "
            "MCC may be unreliable."
        )
        return failures, warnings

    # SIM is READY — validate MCC
    if not operator_numeric or len(operator_numeric) < 3:
        failures.append(
            f"sim_mcc: SIM is READY but operator numeric is "
            f"'{operator_numeric}' (expected ≥3 digits)"
        )
        return failures, warnings

    try:
        mcc = int(operator_numeric[:3])
    except ValueError:
        failures.append(
            f"sim_mcc: cannot parse MCC from '{operator_numeric}'"
        )
        return failures, warnings

    if mcc not in US_MCC_RANGE:
        failures.append(
            f"sim_mcc: MCC {mcc} (from '{operator_numeric}') is not US "
            f"(expected 310–316)"
        )
    return failures, warnings


def _validate_device_name(raw: dict) -> tuple[list[str], list[str]]:
    """Fail if hostname or device_name matches automation-revealing patterns."""
    failures: list[str] = []
    warnings: list[str] = []

    hostname = raw.get("getprop net.hostname", "").strip()
    device_name = raw.get("settings get global device_name", "").strip()

    for label, value in [("net.hostname", hostname), ("device_name", device_name)]:
        if not value or value.lower() == "null":
            continue
        for pattern in AUTOMATION_NAME_PATTERNS:
            if pattern.search(value):
                failures.append(
                    f"device_name: '{value}' ({label}) matches automation "
                    f"pattern /{pattern.pattern}/"
                )
                break  # one failure per name is enough

    return failures, warnings


def _validate_screen_lock(raw: dict) -> tuple[list[str], list[str]]:
    """Screen lock must be enabled (lock-disabled == false)."""
    failures: list[str] = []
    warnings: list[str] = []

    lock_disabled = raw.get("locksettings get-disabled", "").strip().lower()
    password_type = raw.get(
        "settings get secure lockscreen.password_type", ""
    ).strip()

    # Primary check: locksettings get-disabled should return "false"
    if lock_disabled == "true":
        failures.append("screen_lock: lock screen is disabled (locksettings get-disabled = true)")
    elif lock_disabled == "false":
        pass  # good
    else:
        # locksettings command may not be available; fall back to password_type
        if password_type in INSECURE_PASSWORD_TYPES:
            failures.append(
                f"screen_lock: lockscreen.password_type is '{password_type}' "
                "(no secure lock configured)"
            )
        elif not password_type:
            warnings.append(
                "screen_lock: unable to determine lock state — "
                "both locksettings and password_type returned empty/null"
            )

    return failures, warnings


def _validate_usb_debugging(raw: dict) -> tuple[list[str], list[str]]:
    """USB debugging should be off for production, but will be on during ADB
    validation — so this is always a warning, never a hard failure.
    """
    failures: list[str] = []
    warnings: list[str] = []

    adb_enabled = raw.get("settings get global adb_enabled", "").strip()
    dev_settings = raw.get(
        "settings get global development_settings_enabled", ""
    ).strip()

    if adb_enabled == "1":
        warnings.append(
            "usb_debugging: adb_enabled is '1'. This is expected during ADB-based "
            "validation (catch-22). Ensure USB debugging is disabled before "
            "production deployment."
        )

    if dev_settings == "1":
        warnings.append(
            "usb_debugging: developer options are enabled. Disable before "
            "production deployment."
        )

    return failures, warnings


def _validate_app_state(
    raw: dict,
    package: str = "com.zhiliaoapp.musically",
    min_version: str = MIN_APP_VERSION,
    max_version: str = MAX_APP_VERSION,
) -> tuple[list[str], list[str]]:
    """App must be installed, correct version, no leftover account data."""
    failures: list[str] = []
    warnings: list[str] = []

    pm_list = raw.get("pm_list", "").strip()
    version_raw = raw.get("version_name", "").strip()
    prefs_raw = raw.get("shared_prefs", "").strip()

    # 1. Installed?
    if not pm_list or package not in pm_list:
        failures.append(f"app_state: package '{package}' is not installed")
        return failures, warnings  # no point checking further

    # 2. Version in acceptable range
    # dumpsys output may contain multiple "versionName=..." lines; take the first.
    version_match = re.search(r"versionName=(\S+)", version_raw)
    if version_match:
        version = version_match.group(1)
        try:
            vtup = _version_tuple(version)
            if vtup < _version_tuple(min_version):
                failures.append(
                    f"app_state: version {version} is below minimum {min_version}"
                )
            elif vtup > _version_tuple(max_version):
                warnings.append(
                    f"app_state: version {version} exceeds expected maximum {max_version}"
                )
        except Exception:
            warnings.append(
                f"app_state: could not parse version '{version}' for range check"
            )
    else:
        warnings.append("app_state: could not extract versionName from dumpsys output")

    # 3. Leftover account data in shared_prefs
    if prefs_raw and "No such file" not in prefs_raw and "Permission denied" not in prefs_raw:
        pref_files = [f for f in prefs_raw.splitlines() if f.strip()]
        for pf in pref_files:
            for pattern in ACCOUNT_PREFS_PATTERNS:
                if pattern.search(pf):
                    failures.append(
                        f"app_state: leftover account data detected in "
                        f"shared_prefs: '{pf.strip()}'"
                    )
                    break

    return failures, warnings


# ------------------------------------------------------------------ #
#  Dispatch table
# ------------------------------------------------------------------ #

_VALIDATORS: dict[str, callable] = {
    "locale": _validate_locale,
    "timezone": _validate_timezone,
    "gps_location": _validate_gps_location,
    "wifi": _validate_wifi,
    "ip_validation": _validate_ip,
    "sim_mcc": _validate_sim_mcc,
    "device_name": _validate_device_name,
    "screen_lock": _validate_screen_lock,
    "usb_debugging": _validate_usb_debugging,
    "app_state": _validate_app_state,
}


# ------------------------------------------------------------------ #
#  Public API
# ------------------------------------------------------------------ #


def validate(
    check_results: list[dict],
    target_package: str = "com.zhiliaoapp.musically",
    min_version: str = MIN_APP_VERSION,
    max_version: str = MAX_APP_VERSION,
) -> dict:
    """Evaluate a list of raw check results and return a compliance report.

    Parameters
    ----------
    check_results:
        Output of ``ADBRunner.run_all()`` — a list of dicts each containing
        ``check``, ``raw_output``, and ``commands_run``.
    target_package:
        Package name passed through to the app_state validator.
    min_version / max_version:
        Acceptable app version range.

    Returns
    -------
    dict with keys ``pass`` (bool), ``failures`` (list[str]),
    ``warnings`` (list[str]), and ``details`` (per-check breakdown).
    """
    all_failures: list[str] = []
    all_warnings: list[str] = []
    details: dict[str, dict] = {}

    for entry in check_results:
        check_name = entry.get("check", "unknown")
        raw = entry.get("raw_output", {})

        validator_fn = _VALIDATORS.get(check_name)
        if validator_fn is None:
            all_warnings.append(f"No validator registered for check '{check_name}'")
            continue

        # app_state needs extra kwargs
        if check_name == "app_state":
            failures, warnings = validator_fn(
                raw,
                package=target_package,
                min_version=min_version,
                max_version=max_version,
            )
        else:
            failures, warnings = validator_fn(raw)

        all_failures.extend(failures)
        all_warnings.extend(warnings)

        details[check_name] = {
            "failures": failures,
            "warnings": warnings,
            "status": "fail" if failures else ("warn" if warnings else "pass"),
        }

    return {
        "pass": len(all_failures) == 0,
        "failures": all_failures,
        "warnings": all_warnings,
        "details": details,
    }


# ------------------------------------------------------------------ #
#  CLI convenience
# ------------------------------------------------------------------ #

def main() -> None:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(
        description="Validate device compliance from ADB check output.",
    )
    parser.add_argument(
        "input_file",
        nargs="?",
        default="-",
        help="JSON file with ADB check results (default: stdin).",
    )
    parser.add_argument(
        "--package",
        default="com.zhiliaoapp.musically",
        help="Target app package name.",
    )
    parser.add_argument(
        "--min-version",
        default=MIN_APP_VERSION,
        help=f"Minimum acceptable app version (default: {MIN_APP_VERSION}).",
    )
    parser.add_argument(
        "--max-version",
        default=MAX_APP_VERSION,
        help=f"Maximum acceptable app version (default: {MAX_APP_VERSION}).",
    )
    args = parser.parse_args()

    if args.input_file == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.input_file) as fh:
            data = json.load(fh)

    report = validate(
        data,
        target_package=args.package,
        min_version=args.min_version,
        max_version=args.max_version,
    )

    print(json.dumps(report, indent=2))
    sys.exit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
