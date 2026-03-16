"""Main orchestrator for ADB social-media automation.

Ties together the state machine, screen detector, ADB controller, and
action sequences into a single entry point with structured JSON logging
and automatic error recovery.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .adb_controller import ADBController, ADBError
from .actions import ActionError, Actions
from .config import CONFIG
from .screen_detector import ScreenDetector
from .state_machine import AppState, StateMachine


# ── structured JSON logger ──────────────────────────────────────

class _JSONFormatter(logging.Formatter):
    """Emit each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = str(record.exc_info[1])
        # Attach structured fields added via ``extra``.
        for key in ("device_id", "action", "target_state", "result"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        return json.dumps(entry, default=str)


def _configure_logging(device_id: Optional[str] = None) -> logging.Logger:
    """Set up root + automation loggers with JSON output."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, CONFIG["log_level"], logging.DEBUG))

    # Console handler (JSON lines).
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(_JSONFormatter())
    root.addHandler(console)

    # File handler.
    fh = logging.FileHandler(CONFIG["log_file"])
    fh.setFormatter(_JSONFormatter())
    root.addHandler(fh)

    logger = logging.getLogger("automation")
    return logger


# ── orchestrator ────────────────────────────────────────────────

class Automation:
    """Top-level controller for a full automation run.

    Parameters:
        device_id: ADB device serial (``None`` for default device).
    """

    def __init__(self, device_id: Optional[str] = None) -> None:
        self.device_id = device_id
        self.logger = _configure_logging(device_id)

        self.adb = ADBController(device_id=device_id)
        self.sm = StateMachine()
        self.detector = ScreenDetector(
            self.adb, CONFIG["app_package"]
        )
        self.actions = Actions(self.adb, self.detector, self.sm)

    # ── structured log helper ──────────────────────────────────

    def _log_action(
        self,
        action: str,
        target_state: str,
        result: str,
        **extra: Any,
    ) -> None:
        self.logger.info(
            "%s -> %s: %s",
            action,
            target_state,
            result,
            extra={
                "device_id": self.device_id or "default",
                "action": action,
                "target_state": target_state,
                "result": result,
                **extra,
            },
        )

    # ── error recovery ─────────────────────────────────────────

    def _recover(self) -> bool:
        """Attempt to return the app to a known good state.

        Strategy:
            1. Press Back up to ``back_press_limit`` times.
            2. If still lost, force-stop and relaunch.
            3. Wait for HOME_FEED.

        Returns ``True`` if recovery succeeds.
        """
        self.logger.warning("Starting recovery sequence")

        # Step 1: press Back repeatedly.
        for i in range(CONFIG["back_press_limit"]):
            self.adb.press_back()
            time.sleep(0.5)
            state = self.detector.detect()
            if state == AppState.HOME_FEED:
                self.sm.transition(AppState.HOME_FEED)
                self._log_action("recover", "HOME_FEED", "success")
                return True

        # Step 2: force-stop and relaunch.
        self.logger.info("Back presses insufficient; force-stopping app")
        self.adb.force_stop(CONFIG["app_package"])
        time.sleep(1.0)
        self.adb.launch_app(CONFIG["app_package"], CONFIG["app_activity"])

        if self.actions.wait_for_state(AppState.HOME_FEED, timeout=15):
            self._log_action("recover", "HOME_FEED", "success")
            return True

        self._log_action("recover", "HOME_FEED", "fail")
        return False

    def _with_recovery(self, label: str, fn, *args, **kwargs) -> Any:
        """Execute *fn* with up to ``max_recovery_attempts`` retries.

        On each failure the recovery sequence runs before retrying.
        """
        max_attempts = CONFIG["max_recovery_attempts"]
        last_err: Optional[Exception] = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = fn(*args, **kwargs)
                self._log_action(label, self.sm.current.value, "success")
                return result
            except (ActionError, ADBError) as exc:
                last_err = exc
                self.logger.error(
                    "%s failed (attempt %d/%d): %s",
                    label, attempt, max_attempts, exc,
                )
                self.sm.transition(AppState.ERROR)
                self._log_action(label, "ERROR", "fail")
                if attempt < max_attempts:
                    if not self._recover():
                        self.logger.error("Recovery failed; aborting %s", label)
                        break

        raise ActionError(
            f"{label} failed after {max_attempts} attempts: {last_err}"
        )

    # ── public run methods ─────────────────────────────────────

    def launch(self) -> None:
        """Launch the target app and wait for the home feed."""
        self.logger.info("Launching app")
        self.adb.launch_app(CONFIG["app_package"], CONFIG["app_activity"])
        if not self.actions.wait_for_state(AppState.HOME_FEED, timeout=15):
            raise ActionError("App did not reach HOME_FEED after launch")
        self._log_action("launch", "HOME_FEED", "success")

    def run_scroll_and_like(self, count: Optional[int] = None) -> int:
        """Scroll the feed and like *count* posts with recovery."""
        return self._with_recovery(
            "scroll_and_like",
            self.actions.scroll_and_like,
            count,
        )

    def run_post(self, caption: Optional[str] = None) -> bool:
        """Execute the full post sequence with recovery."""
        return self._with_recovery(
            "post",
            self.actions.full_post_sequence,
            caption,
        )

    def run_full(
        self,
        like_count: Optional[int] = None,
        caption: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run the complete automation: launch, scroll-like, post.

        Returns a summary dict.
        """
        summary: Dict[str, Any] = {
            "device": self.device_id or "default",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "liked": 0,
            "posted": False,
            "errors": [],
        }

        try:
            self.launch()

            liked = self.run_scroll_and_like(like_count)
            summary["liked"] = liked

            posted = self.run_post(caption)
            summary["posted"] = posted

            if posted:
                self.actions.verify_post_on_profile()

        except (ActionError, ADBError) as exc:
            summary["errors"].append(str(exc))
            self.logger.error("Run failed: %s", exc)
        finally:
            summary["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.logger.info("Run summary: %s", json.dumps(summary))

        return summary


# ── CLI entry point ─────────────────────────────────────────────

def main() -> None:
    """Command-line entry point.

    Usage::

        python -m challenge-04.automation [--device SERIAL]
                                          [--likes N]
                                          [--caption TEXT]
                                          [--scroll-only]
                                          [--post-only]
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="ADB social-media automation script"
    )
    parser.add_argument("--device", "-d", default=None, help="ADB device serial")
    parser.add_argument(
        "--likes", "-l", type=int, default=None, help="Number of posts to like"
    )
    parser.add_argument("--caption", "-c", default=None, help="Post caption text")
    parser.add_argument(
        "--scroll-only", action="store_true", help="Only scroll and like"
    )
    parser.add_argument(
        "--post-only", action="store_true", help="Only post content"
    )
    args = parser.parse_args()

    bot = Automation(device_id=args.device)

    try:
        bot.launch()

        if args.post_only:
            bot.run_post(caption=args.caption)
        elif args.scroll_only:
            bot.run_scroll_and_like(count=args.likes)
        else:
            bot.run_full(like_count=args.likes, caption=args.caption)

    except (ActionError, ADBError) as exc:
        bot.logger.critical("Fatal: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
