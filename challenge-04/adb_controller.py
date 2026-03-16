"""Resolution-independent ADB interaction layer.

Every coordinate is expressed as a ratio (0.0 -- 1.0) of the device's
physical screen dimensions.  Absolute pixel values are computed at
execution time so the same script works on 720p, 1080p, 1440p, etc.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class ADBError(Exception):
    """Raised when an ADB command fails irrecoverably."""


class ADBController:
    """Thin wrapper around ``adb`` shell commands.

    Parameters:
        device_id: Serial passed via ``-s``.  ``None`` uses the default
            connected device.
    """

    def __init__(self, device_id: Optional[str] = None) -> None:
        self.device_id = device_id
        self._width: Optional[int] = None
        self._height: Optional[int] = None

    # ── screen geometry ─────────────────────────────────────────

    def get_screen_size(self) -> Tuple[int, int]:
        """Return ``(width, height)`` in physical pixels.

        The result is cached after the first successful call.
        """
        if self._width and self._height:
            return self._width, self._height

        output = self.shell("wm size")
        # Typical output: "Physical size: 1080x1920"
        # May also include "Override size:" -- we want the physical line.
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
        if not match:
            # Fallback: take the last WxH pair in the output.
            match = re.search(r"(\d+)x(\d+)", output)
        if not match:
            raise ADBError(f"Cannot parse screen size from: {output!r}")

        self._width = int(match.group(1))
        self._height = int(match.group(2))
        logger.info("Screen size: %dx%d", self._width, self._height)
        return self._width, self._height

    def _abs(self, x_ratio: float, y_ratio: float) -> Tuple[int, int]:
        """Convert ratio-based coordinates to absolute pixels."""
        w, h = self.get_screen_size()
        return int(x_ratio * w), int(y_ratio * h)

    # ── low-level ADB helpers ──────────────────────────────────

    def _build_cmd(self, *args: str) -> list[str]:
        cmd = ["adb"]
        if self.device_id:
            cmd += ["-s", self.device_id]
        cmd.extend(args)
        return cmd

    def run(self, *args: str, timeout: float = 30) -> str:
        """Execute an ``adb`` command and return stdout.

        Raises:
            ADBError: on non-zero exit or timeout.
        """
        cmd = self._build_cmd(*args)
        logger.debug("ADB> %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ADBError(f"ADB command timed out: {cmd}") from exc

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise ADBError(
                f"ADB command failed (rc={result.returncode}): {stderr}"
            )
        return result.stdout.strip()

    def shell(self, command: str, timeout: float = 30) -> str:
        """Run a command inside ``adb shell``."""
        return self.run("shell", command, timeout=timeout)

    # ── input actions ──────────────────────────────────────────

    def tap(self, x_ratio: float, y_ratio: float) -> None:
        """Tap at the given ratio-based coordinate."""
        x, y = self._abs(x_ratio, y_ratio)
        self.shell(f"input tap {x} {y}")
        logger.debug("Tapped (%d, %d) [ratio %.2f, %.2f]", x, y, x_ratio, y_ratio)

    def swipe(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        duration_ms: int = 500,
    ) -> None:
        """Swipe between two ratio-based points over *duration_ms* ms."""
        sx, sy = self._abs(x1, y1)
        ex, ey = self._abs(x2, y2)
        self.shell(f"input swipe {sx} {sy} {ex} {ey} {duration_ms}")
        logger.debug(
            "Swiped (%d,%d)->(%d,%d) in %dms",
            sx, sy, ex, ey, duration_ms,
        )

    def long_press(
        self, x_ratio: float, y_ratio: float, duration_ms: int = 1000
    ) -> None:
        """Long-press at the given coordinate."""
        x, y = self._abs(x_ratio, y_ratio)
        self.shell(f"input swipe {x} {y} {x} {y} {duration_ms}")

    def input_text(self, text: str) -> None:
        """Type text via ``adb shell input text``.

        Spaces are replaced with ``%s`` as required by the ADB protocol.
        Special characters are escaped.
        """
        safe = text.replace(" ", "%s").replace("'", "\\'").replace('"', '\\"')
        self.shell(f"input text '{safe}'")
        logger.debug("Typed text (%d chars)", len(text))

    def press_key(self, keycode: str) -> None:
        """Send a keyevent.  *keycode* is e.g. ``KEYCODE_BACK``."""
        self.shell(f"input keyevent {keycode}")
        logger.debug("Key: %s", keycode)

    def press_back(self) -> None:
        """Convenience: press the Back button."""
        self.press_key("KEYCODE_BACK")

    def press_home(self) -> None:
        """Convenience: press the Home button."""
        self.press_key("KEYCODE_HOME")

    def press_enter(self) -> None:
        """Convenience: press Enter / confirm."""
        self.press_key("KEYCODE_ENTER")

    # ── app lifecycle ──────────────────────────────────────────

    def launch_app(self, package: str, activity: str) -> None:
        """Start an app by component name."""
        self.shell(
            f"am start -n {package}/{activity} "
            f"-W -S"  # wait for launch, stop first
        )
        logger.info("Launched %s/%s", package, activity)

    def force_stop(self, package: str) -> None:
        """Force-stop an application."""
        self.shell(f"am force-stop {package}")
        logger.info("Force-stopped %s", package)

    def is_app_foreground(self, package: str) -> bool:
        """Return ``True`` if *package* is the current foreground app."""
        try:
            output = self.shell("dumpsys activity activities | head -30")
            return package in output
        except ADBError:
            return False

    # ── UI introspection ───────────────────────────────────────

    def dump_ui_hierarchy(self) -> str:
        """Dump the current UI hierarchy XML via uiautomator.

        Returns the raw XML string, or an empty string on failure.
        """
        dump_path = "/sdcard/window_dump.xml"
        try:
            self.shell("uiautomator dump " + dump_path, timeout=10)
            xml = self.shell(f"cat {dump_path}", timeout=10)
            return xml
        except ADBError as exc:
            logger.warning("UI dump failed: %s", exc)
            return ""

    def take_screenshot(self, local_path: str = "/tmp/screen.png") -> str:
        """Capture a screenshot and pull it to *local_path*.

        Returns the local file path on success.
        """
        remote = "/sdcard/screenshot.png"
        self.shell(f"screencap -p {remote}")
        self.run("pull", remote, local_path)
        return local_path

    # ── device info ────────────────────────────────────────────

    def get_android_version(self) -> str:
        """Return the Android version string (e.g. '13')."""
        return self.shell("getprop ro.build.version.release")

    def get_device_model(self) -> str:
        """Return the device model string."""
        return self.shell("getprop ro.product.model")
