"""
adb_runner.py — Executes device compliance checks on a real Android device via ADB.

Each check runs one or more `adb -s {device_serial} shell ...` commands and returns
structured raw output for downstream validation by validator.py.

Return format per check:
    {"check": str, "raw_output": dict, "commands_run": list[str]}
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

ADB_TIMEOUT_SECONDS = 30


@dataclass
class ADBRunner:
    """Runs compliance checks against a single Android device over ADB."""

    device_serial: str
    target_package: str = "com.zhiliaoapp.musically"
    _adb_prefix: list[str] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._adb_prefix = ["adb", "-s", self.device_serial, "shell"]

    # ------------------------------------------------------------------ #
    #  Internal helpers
    # ------------------------------------------------------------------ #

    def _exec(self, shell_cmd: str, timeout: int = ADB_TIMEOUT_SECONDS) -> str:
        """Execute a single adb shell command and return stripped stdout.

        Returns the empty string on any execution error so that callers can
        treat missing output uniformly without try/except noise.
        """
        full_cmd = self._adb_prefix + [shell_cmd]
        logger.debug("Running: %s", " ".join(full_cmd))
        try:
            result = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.warning("Timeout executing: %s", shell_cmd)
            return ""
        except OSError as exc:
            logger.error("OS error executing ADB command: %s", exc)
            return ""

    @staticmethod
    def _result(
        check: str,
        raw_output: dict,
        commands_run: list[str],
    ) -> dict:
        return {
            "check": check,
            "raw_output": raw_output,
            "commands_run": commands_run,
        }

    # ------------------------------------------------------------------ #
    #  1. Language / Region
    # ------------------------------------------------------------------ #

    def check_locale(self) -> dict:
        """Read all locale-related system properties.

        On Android 9+ ``persist.sys.locale`` is authoritative.  The legacy
        ``persist.sys.language`` / ``persist.sys.country`` pair may be stale.
        ``ro.product.locale`` is the factory ROM default and may legitimately
        differ from the user-chosen locale.
        """
        commands = [
            "getprop persist.sys.locale",
            "getprop persist.sys.language",
            "getprop persist.sys.country",
            "getprop ro.product.locale",
        ]
        results = {cmd: self._exec(cmd) for cmd in commands}
        return self._result("locale", results, commands)

    # ------------------------------------------------------------------ #
    #  2. Timezone
    # ------------------------------------------------------------------ #

    def check_timezone(self) -> dict:
        """Read timezone property and auto-timezone setting.

        For compliance ``auto_time_zone`` must be ``0`` (manual) so the
        timezone cannot silently change via network signal.
        """
        commands = [
            "getprop persist.sys.timezone",
            "settings get global auto_time_zone",
        ]
        results = {cmd: self._exec(cmd) for cmd in commands}
        return self._result("timezone", results, commands)

    # ------------------------------------------------------------------ #
    #  3. GPS / Location
    # ------------------------------------------------------------------ #

    def check_gps_location(self) -> dict:
        """Check global location mode and scan for background-location grants.

        ``location_mode`` must be ``0`` (off).  We also dump appops to look
        for any package with ``ACCESS_BACKGROUND_LOCATION`` or
        ``ACCESS_FINE_LOCATION`` in mode ``allow``.
        """
        commands = [
            "settings get secure location_mode",
            "dumpsys appops",
        ]
        location_mode = self._exec(commands[0])

        # dumpsys appops can be very large; we only need location-related lines.
        appops_raw = self._exec(commands[1], timeout=60)

        # Extract lines referencing background/fine location with "allow"
        location_grants: list[str] = []
        for line in appops_raw.splitlines():
            stripped = line.strip()
            if any(
                tok in stripped
                for tok in (
                    "ACCESS_BACKGROUND_LOCATION",
                    "ACCESS_FINE_LOCATION",
                )
            ) and "allow" in stripped.lower():
                location_grants.append(stripped)

        raw_output = {
            "location_mode": location_mode,
            "location_grants": location_grants,
        }
        return self._result("gps_location", raw_output, commands)

    # ------------------------------------------------------------------ #
    #  4. WiFi
    # ------------------------------------------------------------------ #

    def check_wifi(self) -> dict:
        """Check WiFi radio state and list saved networks.

        ``wifi_on`` must be ``0``.  The ``dumpsys wifi`` output is parsed
        for ``WifiConfiguration`` or ``ConfiguredNetworks`` entries.
        """
        commands = [
            "settings get global wifi_on",
            "dumpsys wifi",
        ]
        wifi_on = self._exec(commands[0])
        wifi_dump = self._exec(commands[1], timeout=60)

        # Extract saved-network lines
        saved_networks: list[str] = []
        for line in wifi_dump.splitlines():
            stripped = line.strip()
            if "WifiConfiguration" in stripped or "ConfiguredNetworks" in stripped:
                saved_networks.append(stripped)

        raw_output = {
            "wifi_on": wifi_on,
            "saved_networks": saved_networks,
        }
        return self._result("wifi", raw_output, commands)

    # ------------------------------------------------------------------ #
    #  5. IP Validation
    # ------------------------------------------------------------------ #

    def check_ip(self) -> dict:
        """Retrieve the device's public IP via ``curl -s ifconfig.me``.

        Full GeoIP validation (country, datacenter classification) requires
        an external service.  We return the raw IP so the validator can
        perform whatever lookup is appropriate.
        """
        commands = ["curl -s ifconfig.me"]
        public_ip = self._exec(commands[0])
        raw_output = {"public_ip": public_ip}
        return self._result("ip_validation", raw_output, commands)

    # ------------------------------------------------------------------ #
    #  6. SIM / MCC  — THE STALE MCC TRAP
    # ------------------------------------------------------------------ #

    def check_sim_mcc(self) -> dict:
        """Read SIM state *before* reading MCC to avoid the stale-MCC trap.

        If ``gsm.sim.state`` is not ``READY`` the MCC value cached in
        ``gsm.sim.operator.numeric`` is unreliable and must be ignored.
        """
        commands = [
            "getprop gsm.sim.state",
            "getprop gsm.sim.operator.numeric",
            "getprop gsm.sim.operator.iso-country",
            "getprop gsm.sim.operator.alpha",
        ]
        results = {cmd: self._exec(cmd) for cmd in commands}
        return self._result("sim_mcc", results, commands)

    # ------------------------------------------------------------------ #
    #  7. Device Name
    # ------------------------------------------------------------------ #

    def check_device_name(self) -> dict:
        """Read hostname and user-visible device name.

        The validator will check for automation-revealing patterns (e.g.
        ``device-001``, ``bot``, ``auto``, ``test``).
        """
        commands = [
            "getprop net.hostname",
            "settings get global device_name",
        ]
        results = {cmd: self._exec(cmd) for cmd in commands}
        return self._result("device_name", results, commands)

    # ------------------------------------------------------------------ #
    #  8. Screen Lock
    # ------------------------------------------------------------------ #

    def check_screen_lock(self) -> dict:
        """Determine whether a secure lock screen is configured.

        ``locksettings get-disabled`` should return ``false``.
        ``lockscreen.password_type`` provides a numeric lock-type indicator
        as a fallback (may return ``null`` on Android 10+).
        """
        commands = [
            "locksettings get-disabled",
            "settings get secure lockscreen.password_type",
        ]
        results = {cmd: self._exec(cmd) for cmd in commands}
        return self._result("screen_lock", results, commands)

    # ------------------------------------------------------------------ #
    #  9. USB Debugging
    # ------------------------------------------------------------------ #

    def check_usb_debugging(self) -> dict:
        """Check whether ADB / USB debugging is enabled.

        Catch-22: because this check itself runs over ADB, the value will
        almost always be ``1`` during validation.  The validator should
        treat this as a *warning* rather than a hard failure.
        """
        commands = [
            "settings get global adb_enabled",
            "settings get global development_settings_enabled",
        ]
        results = {cmd: self._exec(cmd) for cmd in commands}
        return self._result("usb_debugging", results, commands)

    # ------------------------------------------------------------------ #
    #  10. App State
    # ------------------------------------------------------------------ #

    def check_app_state(self, package: Optional[str] = None) -> dict:
        """Verify target app installation, version, and leftover data.

        Checks:
        - ``pm list packages`` for presence
        - ``dumpsys package`` for ``versionName``
        - ``ls /data/data/{pkg}/shared_prefs/`` for leftover account data
          (requires root or ``run-as`` on debuggable builds)
        """
        pkg = package or self.target_package

        commands = [
            f"pm list packages | grep {pkg}",
            f"dumpsys package {pkg} | grep versionName",
            f"ls /data/data/{pkg}/shared_prefs/",
        ]

        pm_output = self._exec(commands[0])
        version_output = self._exec(commands[1])
        prefs_output = self._exec(commands[2])

        # Try run-as fallback for shared_prefs if direct ls failed
        if "Permission denied" in prefs_output or not prefs_output:
            fallback_cmd = f"run-as {pkg} ls shared_prefs/"
            commands.append(fallback_cmd)
            prefs_output = self._exec(fallback_cmd)

        raw_output = {
            "pm_list": pm_output,
            "version_name": version_output,
            "shared_prefs": prefs_output,
        }
        return self._result("app_state", raw_output, commands)

    # ------------------------------------------------------------------ #
    #  Run all checks
    # ------------------------------------------------------------------ #

    def run_all(self) -> list[dict]:
        """Execute every compliance check and return the list of results."""
        return [
            self.check_locale(),
            self.check_timezone(),
            self.check_gps_location(),
            self.check_wifi(),
            self.check_ip(),
            self.check_sim_mcc(),
            self.check_device_name(),
            self.check_screen_lock(),
            self.check_usb_debugging(),
            self.check_app_state(),
        ]


# ------------------------------------------------------------------ #
#  CLI convenience
# ------------------------------------------------------------------ #

def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Run ADB device compliance checks.",
    )
    parser.add_argument(
        "device_serial",
        help="ADB device serial (from `adb devices`).",
    )
    parser.add_argument(
        "--package",
        default="com.zhiliaoapp.musically",
        help="Target app package name (default: TikTok).",
    )
    parser.add_argument(
        "--check",
        choices=[
            "locale",
            "timezone",
            "gps_location",
            "wifi",
            "ip_validation",
            "sim_mcc",
            "device_name",
            "screen_lock",
            "usb_debugging",
            "app_state",
        ],
        help="Run a single check instead of all.",
    )
    args = parser.parse_args()

    runner = ADBRunner(
        device_serial=args.device_serial,
        target_package=args.package,
    )

    if args.check:
        check_map = {
            "locale": runner.check_locale,
            "timezone": runner.check_timezone,
            "gps_location": runner.check_gps_location,
            "wifi": runner.check_wifi,
            "ip_validation": runner.check_ip,
            "sim_mcc": runner.check_sim_mcc,
            "device_name": runner.check_device_name,
            "screen_lock": runner.check_screen_lock,
            "usb_debugging": runner.check_usb_debugging,
            "app_state": runner.check_app_state,
        }
        results = [check_map[args.check]()]
    else:
        results = runner.run_all()

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
