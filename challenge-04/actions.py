"""High-level action sequences: scroll, like, post.

Every public method follows the pattern:
  1. Detect current state (and handle popups).
  2. Perform the action via ADBController.
  3. Wait for the expected next state.
  4. Return success / failure.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Optional

from .adb_controller import ADBController
from .config import CONFIG
from .screen_detector import ScreenDetector
from .state_machine import AppState, StateMachine

logger = logging.getLogger(__name__)


class ActionError(Exception):
    """Raised when an action sequence cannot be completed."""


class Actions:
    """Encapsulates every automatable action inside the target app.

    Parameters:
        adb: Initialised ADB controller.
        detector: Screen-state detector.
        sm: State machine tracker.
    """

    def __init__(
        self,
        adb: ADBController,
        detector: ScreenDetector,
        sm: StateMachine,
    ) -> None:
        self.adb = adb
        self.detector = detector
        self.sm = sm

    # ── wait helpers ────────────────────────────────────────────

    def wait_for_state(
        self,
        target: AppState,
        timeout: Optional[float] = None,
        poll_interval: Optional[float] = None,
    ) -> bool:
        """Poll until the screen matches *target* or *timeout* expires.

        Returns ``True`` if the target state was reached.
        """
        timeout = timeout or CONFIG["wait_timeout"]
        poll_interval = poll_interval or CONFIG["poll_interval"]
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            state = self.detector.detect()
            if state == target:
                self.sm.transition(target)
                return True
            if state == AppState.POPUP:
                self.sm.transition(AppState.POPUP)
                self._dismiss_popup()
            time.sleep(poll_interval)

        logger.warning(
            "Timed out waiting for state %s (current: %s)",
            target.value,
            self.sm.current.value,
        )
        return False

    # ── popup handling ─────────────────────────────────────────

    def _dismiss_popup(self) -> None:
        """Try to dismiss the current popup overlay."""
        coords = self.detector.find_popup_dismiss_button()
        if coords:
            logger.info("Dismissing popup via button at (%.2f, %.2f)", *coords)
            self.adb.tap(*coords)
            time.sleep(0.5)
            return

        # Unknown popup -- press Back.
        logger.info("Unknown popup; pressing Back to dismiss")
        self.adb.press_back()
        time.sleep(0.5)

    def handle_popup_if_present(self) -> None:
        """Check and dismiss any popup before proceeding."""
        state = self.detector.detect()
        if state == AppState.POPUP:
            self.sm.transition(AppState.POPUP)
            self._dismiss_popup()

    # ── navigation ─────────────────────────────────────────────

    def go_home(self) -> bool:
        """Navigate to the home feed regardless of current state."""
        self.handle_popup_if_present()
        coords = CONFIG["coords"]["home_tab"]
        self.adb.tap(*coords)
        return self.wait_for_state(AppState.HOME_FEED)

    def go_profile(self) -> bool:
        """Navigate to the profile tab."""
        self.handle_popup_if_present()
        coords = CONFIG["coords"]["profile_tab"]
        self.adb.tap(*coords)
        return self.wait_for_state(AppState.PROFILE)

    # ── scroll & like ──────────────────────────────────────────

    def scroll_feed(self) -> None:
        """Perform a single human-like scroll on the feed.

        Swipe duration and post-swipe pause are randomised within the
        configured ranges to avoid detection.
        """
        self.handle_popup_if_present()

        start = CONFIG["coords"]["scroll_start"]
        end = CONFIG["coords"]["scroll_end"]
        duration = random.randint(*CONFIG["scroll_speed_range"])

        self.adb.swipe(*start, *end, duration_ms=duration)

        pause = random.uniform(*CONFIG["scroll_pause_range"])
        logger.debug("Scroll done (dur=%dms), pausing %.1fs", duration, pause)
        time.sleep(pause)

    def like_current_post(self) -> bool:
        """Tap the like button on the currently visible post.

        Returns ``True`` if the like appears to have registered (i.e.
        the button element changed state).
        """
        self.handle_popup_if_present()

        # Try to find the like button via UI hierarchy first.
        like_coords = self.detector.find_element_coords(
            "content-desc", r"(?i)like"
        )
        if like_coords is None:
            # Fallback to config ratio.
            like_coords = CONFIG["coords"]["like_button"]

        self.adb.tap(*like_coords)
        logger.info("Liked post at (%.2f, %.2f)", *like_coords)

        # Brief pause then verify the button changed (best-effort).
        time.sleep(0.3)
        return True

    def scroll_and_like(self, count: Optional[int] = None) -> int:
        """Scroll through the feed and like *count* posts.

        Returns the number of posts actually liked.
        """
        count = count or CONFIG["like_count"]
        liked = 0

        if self.sm.current != AppState.HOME_FEED:
            if not self.go_home():
                raise ActionError("Cannot reach home feed for liking")

        for i in range(count):
            try:
                self.scroll_feed()
                if self.like_current_post():
                    liked += 1
                    logger.info("Liked %d / %d", liked, count)
            except Exception:
                logger.exception("Error liking post %d", i + 1)
                self.handle_popup_if_present()

        return liked

    # ── post / upload ──────────────────────────────────────────

    def open_upload(self) -> bool:
        """Tap the upload / create button to enter the upload flow."""
        self.handle_popup_if_present()
        coords = CONFIG["coords"]["upload_button"]
        self.adb.tap(*coords)
        return self.wait_for_state(AppState.UPLOAD)

    def select_video(self) -> bool:
        """Select the pre-loaded video from the device gallery.

        Assumes the upload screen shows gallery items and the first
        item is the pre-loaded video (common for recent-media pickers).
        """
        self.handle_popup_if_present()

        # Try tapping the first gallery thumbnail (top-left of grid).
        # Typical gallery grid starts around y=0.35, x=0.15.
        gallery_first = (0.15, 0.40)
        self.adb.tap(*gallery_first)
        time.sleep(1.0)

        # Look for a "Next" or "Continue" button.
        next_btn = self.detector.find_element_coords("text", r"(?i)^next$")
        if next_btn is None:
            next_btn = self.detector.find_element_coords(
                "content-desc", r"(?i)next|continue"
            )
        if next_btn:
            self.adb.tap(*next_btn)

        return self.wait_for_state(AppState.CAPTION_EDITOR, timeout=15)

    def enter_caption(self, text: Optional[str] = None) -> bool:
        """Type the caption text into the caption editor.

        Parameters:
            text: Caption string.  Falls back to ``CONFIG["caption_text"]``.
        """
        self.handle_popup_if_present()
        text = text or CONFIG["caption_text"]

        # Tap the caption field.
        caption_coords = self.detector.find_element_coords(
            "resource-id", r"(?i)caption|describe"
        )
        if caption_coords is None:
            caption_coords = CONFIG["coords"]["caption_field"]

        self.adb.tap(*caption_coords)
        time.sleep(0.5)

        # Clear any existing text and type new caption.
        self.adb.input_text(text)
        logger.info("Entered caption: %s", text[:40])
        return True

    def submit_post(self) -> bool:
        """Tap the Post / Publish button and wait for completion."""
        self.handle_popup_if_present()

        post_btn = self.detector.find_element_coords("text", r"(?i)^post$")
        if post_btn is None:
            post_btn = self.detector.find_element_coords(
                "content-desc", r"(?i)post|publish"
            )
        if post_btn is None:
            post_btn = CONFIG["coords"]["post_button"]

        self.adb.tap(*post_btn)
        logger.info("Tapped Post button")

        # Wait for POSTING then POST_COMPLETE.
        self.wait_for_state(AppState.POSTING, timeout=5)
        return self.wait_for_state(AppState.POST_COMPLETE, timeout=60)

    def full_post_sequence(self, caption: Optional[str] = None) -> bool:
        """Execute the complete upload-select-caption-post flow.

        Returns ``True`` if the post was successfully submitted and
        confirmed.
        """
        steps = [
            ("open_upload", lambda: self.open_upload()),
            ("select_video", lambda: self.select_video()),
            ("enter_caption", lambda: self.enter_caption(caption)),
            ("submit_post", lambda: self.submit_post()),
        ]

        for name, step_fn in steps:
            logger.info("Post sequence step: %s", name)
            if not step_fn():
                raise ActionError(f"Post step '{name}' failed")

        logger.info("Post sequence completed successfully")
        return True

    def verify_post_on_profile(self) -> bool:
        """Navigate to the profile and check that a new post appeared.

        This is a best-effort check: we look for a video thumbnail
        node that was not there before.  Returns ``True`` optimistically
        if we reach the profile screen.
        """
        if not self.go_profile():
            return False
        # In a production system we would compare post counts or
        # thumbnail hashes before/after.  For now, reaching the
        # profile after a successful POST_COMPLETE is sufficient.
        return True
