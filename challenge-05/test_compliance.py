"""
Tests for Challenge 05 — Device Compliance (validator.py).

Covers: stale MCC trap, locale disagreements, GPS with background location,
WiFi saved networks, device name patterns, USB debugging catch-22, and app state.
"""

from __future__ import annotations

import pytest

from validator import (
    _validate_app_state,
    _validate_device_name,
    _validate_gps_location,
    _validate_locale,
    _validate_sim_mcc,
    _validate_usb_debugging,
    _validate_wifi,
    validate,
)


# ======================================================================
# Helpers
# ======================================================================

def _make_check(name: str, raw_output: dict) -> dict:
    """Build a single check-result dict as returned by ADBRunner."""
    return {"check": name, "raw_output": raw_output, "commands_run": []}


def _run_single(name: str, raw_output: dict, **kwargs) -> dict:
    """Run the full validate() pipeline with a single check entry."""
    return validate([_make_check(name, raw_output)], **kwargs)


# ======================================================================
# 1-3. Stale MCC trap
# ======================================================================


class TestStaleMCC:
    """SIM state must gate whether MCC is evaluated."""

    def test_sim_absent_foreign_mcc_passes_with_warning(self):
        """#1: SIM ABSENT + foreign MCC → PASS with warning (stale MCC ignored)."""
        raw = {
            "getprop gsm.sim.state": "ABSENT",
            "getprop gsm.sim.operator.numeric": "45201",
            "getprop gsm.sim.operator.iso-country": "vn",
            "getprop gsm.sim.operator.alpha": "Mobifone",
        }
        failures, warnings = _validate_sim_mcc(raw)
        assert failures == [], f"Expected no failures, got {failures}"
        assert any("stale" in w.lower() or "ABSENT" in w for w in warnings)

    def test_sim_ready_us_mcc_passes(self):
        """#2: SIM READY + US MCC 310260 → PASS, no failures or warnings."""
        raw = {
            "getprop gsm.sim.state": "READY",
            "getprop gsm.sim.operator.numeric": "310260",
            "getprop gsm.sim.operator.iso-country": "us",
            "getprop gsm.sim.operator.alpha": "T-Mobile",
        }
        failures, warnings = _validate_sim_mcc(raw)
        assert failures == []
        assert warnings == []

    def test_sim_ready_foreign_mcc_fails(self):
        """#3: SIM READY + foreign MCC 452 → FAIL."""
        raw = {
            "getprop gsm.sim.state": "READY",
            "getprop gsm.sim.operator.numeric": "45201",
            "getprop gsm.sim.operator.iso-country": "vn",
            "getprop gsm.sim.operator.alpha": "Mobifone",
        }
        failures, warnings = _validate_sim_mcc(raw)
        assert len(failures) == 1
        assert "not US" in failures[0]


# ======================================================================
# 4-6. Locale disagreements
# ======================================================================


class TestLocale:
    """Locale checks: authoritative prop wins, legacy stale props produce warnings."""

    def test_all_agree_en_us_passes(self):
        """#4: All locale properties agree on en-US → PASS."""
        raw = {
            "getprop persist.sys.locale": "en-US",
            "getprop persist.sys.language": "en",
            "getprop persist.sys.country": "US",
            "getprop ro.product.locale": "en-US",
        }
        failures, warnings = _validate_locale(raw)
        assert failures == []

    def test_persist_locale_en_us_legacy_ko_passes_with_warning(self):
        """#5: persist.sys.locale=en-US, persist.sys.language=ko → PASS + warning."""
        raw = {
            "getprop persist.sys.locale": "en-US",
            "getprop persist.sys.language": "ko",
            "getprop persist.sys.country": "KR",
            "getprop ro.product.locale": "en-US",
        }
        failures, warnings = _validate_locale(raw)
        assert failures == [], f"Expected no failures, got {failures}"
        assert any("disagree" in w.lower() or "stale" in w.lower() for w in warnings)

    def test_persist_locale_ko_kr_fails(self):
        """#6: persist.sys.locale=ko-KR → FAIL."""
        raw = {
            "getprop persist.sys.locale": "ko-KR",
            "getprop persist.sys.language": "ko",
            "getprop persist.sys.country": "KR",
            "getprop ro.product.locale": "ko-KR",
        }
        failures, warnings = _validate_locale(raw)
        assert len(failures) == 1
        assert "ko-KR" in failures[0]


# ======================================================================
# 7-9. GPS with background location
# ======================================================================


class TestGPSLocation:
    """GPS location mode and background location grants."""

    def test_location_off_no_grants_passes(self):
        """#7: location_mode=0, no bg location apps → PASS."""
        raw = {"location_mode": "0", "location_grants": []}
        failures, warnings = _validate_gps_location(raw)
        assert failures == []
        assert warnings == []

    def test_location_off_but_bg_grant_fails(self):
        """#8: location_mode=0, but an app has ACCESS_BACKGROUND_LOCATION allow → FAIL."""
        raw = {
            "location_mode": "0",
            "location_grants": [
                "ACCESS_BACKGROUND_LOCATION: allow; time=+2h30m (running)"
            ],
        }
        failures, warnings = _validate_gps_location(raw)
        assert len(failures) == 1
        assert "background" in failures[0].lower() or "location" in failures[0].lower()

    def test_location_high_accuracy_fails(self):
        """#9: location_mode=3 (high accuracy) → FAIL."""
        raw = {"location_mode": "3", "location_grants": []}
        failures, warnings = _validate_gps_location(raw)
        assert len(failures) == 1
        assert "location_mode" in failures[0]


# ======================================================================
# 10-12. WiFi saved networks
# ======================================================================


class TestWiFi:
    """WiFi must be off with zero saved networks."""

    def test_wifi_off_no_saved_passes(self):
        """#10: wifi_on=0, zero saved networks → PASS."""
        raw = {"wifi_on": "0", "saved_networks": []}
        failures, warnings = _validate_wifi(raw)
        assert failures == []

    def test_wifi_off_hidden_saved_network_fails(self):
        """#11: wifi_on=0, has a hidden saved network → FAIL."""
        raw = {
            "wifi_on": "0",
            "saved_networks": [
                'WifiConfiguration: "hidden-net" hiddenSSID=true'
            ],
        }
        failures, warnings = _validate_wifi(raw)
        assert len(failures) == 1
        assert "saved network" in failures[0].lower()

    def test_wifi_on_fails(self):
        """#12: wifi_on=1 → FAIL."""
        raw = {"wifi_on": "1", "saved_networks": []}
        failures, warnings = _validate_wifi(raw)
        assert any("wifi_on" in f for f in failures)


# ======================================================================
# 13-15. Device name patterns
# ======================================================================


class TestDeviceName:
    """Device names matching automation patterns must fail."""

    def test_normal_name_passes(self):
        """#13: 'Galaxy S21' → PASS."""
        raw = {
            "getprop net.hostname": "Galaxy-S21",
            "settings get global device_name": "Galaxy S21",
        }
        failures, warnings = _validate_device_name(raw)
        assert failures == []

    def test_automation_name_fails(self):
        """#14: 'device-001' → FAIL."""
        raw = {
            "getprop net.hostname": "device-001",
            "settings get global device_name": "device-001",
        }
        failures, warnings = _validate_device_name(raw)
        assert len(failures) >= 1
        assert any("automation" in f.lower() or "pattern" in f.lower() for f in failures)

    def test_bot_or_test_name_fails(self):
        """#15: Name containing 'bot' or 'test' → FAIL."""
        # Test "bot"
        raw_bot = {
            "getprop net.hostname": "my-bot-phone",
            "settings get global device_name": "my-bot-phone",
        }
        failures_bot, _ = _validate_device_name(raw_bot)
        assert len(failures_bot) >= 1

        # Test "test"
        raw_test = {
            "getprop net.hostname": "test-device",
            "settings get global device_name": "test-device",
        }
        failures_test, _ = _validate_device_name(raw_test)
        assert len(failures_test) >= 1


# ======================================================================
# 16. USB debugging catch-22
# ======================================================================


class TestUSBDebugging:
    """USB debugging enabled must produce WARNING, not failure."""

    def test_adb_enabled_is_warning_not_failure(self):
        """#16: adb_enabled=1 → WARNING only (catch-22)."""
        raw = {
            "settings get global adb_enabled": "1",
            "settings get global development_settings_enabled": "0",
        }
        failures, warnings = _validate_usb_debugging(raw)
        assert failures == [], "USB debugging ON should not produce a failure"
        assert len(warnings) >= 1
        assert any("catch-22" in w.lower() or "adb_enabled" in w for w in warnings)


# ======================================================================
# 17-18. App state
# ======================================================================


class TestAppState:
    """App installation, version, and leftover data checks."""

    def test_installed_correct_version_no_leftovers_passes(self):
        """#17: Package installed, correct version, no leftover data → PASS."""
        raw = {
            "pm_list": "package:com.zhiliaoapp.musically",
            "version_name": "versionName=30.1.4",
            "shared_prefs": "analytics_config.xml\nui_prefs.xml",
        }
        failures, warnings = _validate_app_state(raw)
        assert failures == []

    def test_leftover_auth_session_files_fails(self):
        """#18: shared_prefs with auth/session files → FAIL."""
        raw = {
            "pm_list": "package:com.zhiliaoapp.musically",
            "version_name": "versionName=30.1.4",
            "shared_prefs": (
                "auth_token_store.xml\n"
                "session_data.xml\n"
                "analytics_config.xml"
            ),
        }
        failures, warnings = _validate_app_state(raw)
        assert len(failures) >= 1
        assert any("auth" in f.lower() or "session" in f.lower() for f in failures)


# ======================================================================
# Integration: full validate() pipeline
# ======================================================================


class TestValidateIntegration:
    """End-to-end validation with multiple checks combined."""

    def test_all_clean_passes(self):
        """All checks clean → report['pass'] is True."""
        checks = [
            _make_check("sim_mcc", {
                "getprop gsm.sim.state": "READY",
                "getprop gsm.sim.operator.numeric": "310260",
                "getprop gsm.sim.operator.iso-country": "us",
                "getprop gsm.sim.operator.alpha": "T-Mobile",
            }),
            _make_check("locale", {
                "getprop persist.sys.locale": "en-US",
                "getprop persist.sys.language": "en",
                "getprop persist.sys.country": "US",
                "getprop ro.product.locale": "en-US",
            }),
            _make_check("wifi", {"wifi_on": "0", "saved_networks": []}),
            _make_check("gps_location", {"location_mode": "0", "location_grants": []}),
        ]
        report = validate(checks)
        assert report["pass"] is True
        assert report["failures"] == []

    def test_single_failure_blocks_pass(self):
        """One failing check → report['pass'] is False."""
        checks = [
            _make_check("wifi", {"wifi_on": "1", "saved_networks": []}),
        ]
        report = validate(checks)
        assert report["pass"] is False
        assert len(report["failures"]) >= 1
