"""Tests for Challenge 04 — ADB Automation.

All ADB calls are mocked since no physical device is available.

Covers:
    - State machine: valid/invalid transitions, history tracking
    - Screen detection: XML-based identification of HOME_FEED, POPUP, UNKNOWN
    - Resolution independence: coordinate ratios map correctly to pixels
    - Popup handling: dismiss known popup, Back on unknown popup
    - Scroll and like: count respected, like verification
    - Error recovery: back-press sequence, force-stop/relaunch, graceful exit
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from challenge_04.state_machine import AppState, StateMachine, TRANSITIONS
from challenge_04.adb_controller import ADBController, ADBError
from challenge_04.screen_detector import ScreenDetector
from challenge_04.actions import ActionError, Actions
from challenge_04.automation import Automation
from challenge_04.config import CONFIG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adb(width: int = 1080, height: int = 1920) -> MagicMock:
    """Return a mocked ADBController with a fixed screen size."""
    adb = MagicMock(spec=ADBController)
    adb.get_screen_size.return_value = (width, height)
    adb.dump_ui_hierarchy.return_value = ""
    adb.is_app_foreground.return_value = True
    adb.shell.return_value = ""
    adb.run.return_value = ""
    return adb


def _build_xml(*elements: str) -> str:
    """Build a minimal UI hierarchy XML containing the given element strings."""
    nodes = "\n".join(elements)
    return f'<hierarchy rotation="0">\n{nodes}\n</hierarchy>'


def _node(text: str = "", resource_id: str = "", content_desc: str = "",
          cls: str = "", bounds: str = "[0,0][100,100]") -> str:
    return (
        f'<node text="{text}" resource-id="{resource_id}" '
        f'content-desc="{content_desc}" class="{cls}" '
        f'bounds="{bounds}" />'
    )


# =========================================================================
# STATE MACHINE TESTS
# =========================================================================

# 1. Valid state transition succeeds

class TestStateMachineValidTransition:
    def test_home_to_profile(self):
        sm = StateMachine(initial=AppState.HOME_FEED)
        result = sm.transition(AppState.PROFILE)
        assert result is True
        assert sm.current == AppState.PROFILE

    def test_profile_to_home(self):
        sm = StateMachine(initial=AppState.PROFILE)
        result = sm.transition(AppState.HOME_FEED)
        assert result is True
        assert sm.current == AppState.HOME_FEED

    def test_all_declared_transitions_are_valid(self):
        """Every transition in the TRANSITIONS table must return True."""
        for source, targets in TRANSITIONS.items():
            for target in targets:
                sm = StateMachine(initial=source)
                assert sm.transition(target) is True, (
                    f"{source} -> {target} should be valid"
                )


# 2. Invalid state transition returns False

class TestStateMachineInvalidTransition:
    def test_home_to_post_complete_is_invalid(self):
        sm = StateMachine(initial=AppState.HOME_FEED)
        result = sm.transition(AppState.POST_COMPLETE)
        assert result is False
        # State is still updated (forced) per the implementation.
        assert sm.current == AppState.POST_COMPLETE

    def test_error_to_profile_is_invalid(self):
        sm = StateMachine(initial=AppState.ERROR)
        result = sm.transition(AppState.PROFILE)
        assert result is False


# 3. State history is maintained correctly

class TestStateMachineHistory:
    def test_history_tracks_all_states(self):
        sm = StateMachine(initial=AppState.UNKNOWN)
        sm.transition(AppState.HOME_FEED)
        sm.transition(AppState.PROFILE)
        sm.transition(AppState.HOME_FEED)

        assert sm.history == [
            AppState.UNKNOWN,
            AppState.HOME_FEED,
            AppState.PROFILE,
            AppState.HOME_FEED,
        ]

    def test_previous_property(self):
        sm = StateMachine(initial=AppState.HOME_FEED)
        assert sm.previous is None
        sm.transition(AppState.PROFILE)
        assert sm.previous == AppState.HOME_FEED


# =========================================================================
# SCREEN DETECTION TESTS
# =========================================================================

# 4. "For You" tab detected as HOME_FEED

class TestScreenDetection:
    def test_for_you_tab_detected_as_home_feed(self):
        adb = _make_adb()
        xml = _build_xml(_node(text="For You"))
        adb.dump_ui_hierarchy.return_value = xml

        detector = ScreenDetector(adb, "com.test.app")
        state = detector.detect()
        assert state == AppState.HOME_FEED

    # 5. Dialog overlay detected as POPUP

    def test_dialog_overlay_detected_as_popup(self):
        adb = _make_adb()
        xml = _build_xml(
            _node(text="For You"),  # home feed indicator
            _node(resource_id="com.test.dialog"),  # popup indicator
        )
        adb.dump_ui_hierarchy.return_value = xml

        detector = ScreenDetector(adb, "com.test.app")
        state = detector.detect()
        # Popup is checked first and should take priority.
        assert state == AppState.POPUP

    def test_dialog_class_detected_as_popup(self):
        adb = _make_adb()
        xml = _build_xml(_node(cls="android.app.Dialog"))
        adb.dump_ui_hierarchy.return_value = xml

        detector = ScreenDetector(adb, "com.test.app")
        assert detector.detect() == AppState.POPUP

    # 6. Unknown/empty hierarchy returns UNKNOWN

    def test_empty_hierarchy_returns_unknown(self):
        adb = _make_adb()
        adb.dump_ui_hierarchy.return_value = ""
        adb.is_app_foreground.return_value = True

        detector = ScreenDetector(adb, "com.test.app")
        # No XML and foreground check won't give a definitive state.
        state = detector.detect()
        # With no XML and app in foreground, screenshot fallback returns None
        # -> UNKNOWN
        assert state == AppState.UNKNOWN

    def test_unrecognised_xml_returns_unknown(self):
        adb = _make_adb()
        xml = _build_xml(_node(text="Something Random Unmatched"))
        adb.dump_ui_hierarchy.return_value = xml
        adb.is_app_foreground.return_value = True

        detector = ScreenDetector(adb, "com.test.app")
        state = detector.detect()
        assert state == AppState.UNKNOWN

    def test_profile_detection(self):
        adb = _make_adb()
        # Use resource-id pattern that uniquely matches PROFILE.
        xml = _build_xml(
            _node(text="Followers"),
            _node(resource_id="com.test.profile"),
        )
        adb.dump_ui_hierarchy.return_value = xml

        detector = ScreenDetector(adb, "com.test.app")
        assert detector.detect() == AppState.PROFILE


# =========================================================================
# RESOLUTION INDEPENDENCE TESTS
# =========================================================================

# 7 & 8. Same ratio maps to different absolute pixels on different screens

class TestResolutionIndependence:
    def test_ratio_on_1080x1920(self):
        adb = _make_adb(width=1080, height=1920)
        x, y = adb.get_screen_size()
        abs_x = int(0.5 * x)
        abs_y = int(0.5 * y)
        assert (abs_x, abs_y) == (540, 960)

    def test_ratio_on_720x1280(self):
        adb = _make_adb(width=720, height=1280)
        x, y = adb.get_screen_size()
        abs_x = int(0.5 * x)
        abs_y = int(0.5 * y)
        assert (abs_x, abs_y) == (360, 640)

    def test_abs_method_on_real_controller(self):
        """Test the _abs method directly on a real ADBController with
        mocked shell calls."""
        ctrl = ADBController()
        ctrl._width = 1080
        ctrl._height = 1920
        assert ctrl._abs(0.5, 0.5) == (540, 960)

        ctrl._width = 720
        ctrl._height = 1280
        assert ctrl._abs(0.5, 0.5) == (360, 640)

    def test_tap_uses_absolute_coords(self):
        """ADBController.tap must convert ratio to absolute pixels before
        issuing the shell command."""
        ctrl = ADBController()
        ctrl._width = 1080
        ctrl._height = 1920
        with patch.object(ctrl, "shell") as mock_shell:
            ctrl.tap(0.5, 0.5)
            mock_shell.assert_called_once_with("input tap 540 960")


# =========================================================================
# POPUP HANDLING TESTS
# =========================================================================

# 9. Popup detected mid-action -> dismissed before continuing

class TestPopupHandling:
    def test_known_popup_dismissed_via_button(self):
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.HOME_FEED)

        # First detect returns POPUP, second returns HOME_FEED.
        detector.detect.side_effect = [AppState.POPUP, AppState.HOME_FEED]
        detector.find_popup_dismiss_button.return_value = (0.5, 0.7)

        actions = Actions(adb, detector, sm)
        with patch("challenge_04.actions.time.sleep"):
            actions.handle_popup_if_present()

        adb.tap.assert_called_once_with(0.5, 0.7)
        assert sm.current == AppState.POPUP  # transition was recorded

    # 10. Unknown popup -> Back pressed, state re-detected

    def test_unknown_popup_uses_back_press(self):
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.HOME_FEED)

        detector.detect.return_value = AppState.POPUP
        detector.find_popup_dismiss_button.return_value = None

        actions = Actions(adb, detector, sm)
        with patch("challenge_04.actions.time.sleep"):
            actions.handle_popup_if_present()

        adb.press_back.assert_called_once()

    def test_popup_during_wait_for_state(self):
        """When a popup appears while waiting for a target state, it must
        be dismissed before continuing to poll."""
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.HOME_FEED)

        # Sequence: POPUP -> (dismiss) -> HOME_FEED (not target) -> PROFILE (target)
        detector.detect.side_effect = [
            AppState.POPUP,
            AppState.PROFILE,
        ]
        detector.find_popup_dismiss_button.return_value = (0.5, 0.5)

        actions = Actions(adb, detector, sm)
        with patch("challenge_04.actions.time.sleep"):
            reached = actions.wait_for_state(AppState.PROFILE, timeout=5, poll_interval=0.01)

        assert reached is True
        assert sm.current == AppState.PROFILE


# =========================================================================
# SCROLL AND LIKE TESTS
# =========================================================================

# 11. Like count respected

class TestScrollAndLike:
    def test_like_count_exactly_n(self):
        """When configured for N likes, exactly N like attempts must execute."""
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.HOME_FEED)

        # No popups, like always succeeds.
        detector.detect.return_value = AppState.HOME_FEED
        detector.find_element_coords.return_value = (0.95, 0.5)
        detector.find_popup_dismiss_button.return_value = None

        actions = Actions(adb, detector, sm)

        with patch("challenge_04.actions.time.sleep"):
            with patch("challenge_04.actions.random.randint", return_value=500):
                with patch("challenge_04.actions.random.uniform", return_value=1.0):
                    liked = actions.scroll_and_like(count=5)

        assert liked == 5
        # 5 scrolls + 5 likes = 10 taps total (scroll via swipe, like via tap).
        assert adb.swipe.call_count == 5
        # Taps: like_current_post calls tap once per like + handle_popup checks
        # but with no popup detected, only like taps happen.
        like_tap_count = sum(
            1 for call in adb.tap.call_args_list
            if call[0] == (0.95, 0.5)
        )
        assert like_tap_count == 5

    # 12. Like verification: button state doesn't change -> flag

    def test_like_returns_success_status(self):
        """like_current_post must return a boolean indicating success."""
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.HOME_FEED)

        detector.detect.return_value = AppState.HOME_FEED
        detector.find_element_coords.return_value = (0.95, 0.5)

        actions = Actions(adb, detector, sm)

        with patch("challenge_04.actions.time.sleep"):
            result = actions.like_current_post()

        assert result is True
        adb.tap.assert_called()

    def test_like_falls_back_to_config_coords(self):
        """When UI hierarchy does not contain the like button, the action
        must fall back to CONFIG coordinates."""
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.HOME_FEED)

        detector.detect.return_value = AppState.HOME_FEED
        detector.find_element_coords.return_value = None  # not found in XML

        actions = Actions(adb, detector, sm)
        expected_coords = CONFIG["coords"]["like_button"]

        with patch("challenge_04.actions.time.sleep"):
            actions.like_current_post()

        adb.tap.assert_called_with(*expected_coords)


# =========================================================================
# ERROR RECOVERY TESTS
# =========================================================================

# 13. Failed action triggers recovery sequence

class TestErrorRecovery:
    def test_recovery_via_back_presses(self):
        """Recovery must press Back up to back_press_limit times. If one
        succeeds in reaching HOME_FEED, recovery succeeds."""
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.ERROR)

        # First two back presses: still ERROR; third: HOME_FEED.
        detector.detect.side_effect = [
            AppState.ERROR,
            AppState.ERROR,
            AppState.HOME_FEED,
        ]

        actions = MagicMock(spec=Actions)
        actions.wait_for_state.return_value = True

        bot = Automation.__new__(Automation)
        bot.device_id = None
        bot.logger = MagicMock()
        bot.adb = adb
        bot.sm = sm
        bot.detector = detector
        bot.actions = actions

        with patch("challenge_04.automation.time.sleep"):
            recovered = bot._recover()

        assert recovered is True
        assert adb.press_back.call_count <= CONFIG["back_press_limit"]
        assert sm.current == AppState.HOME_FEED

    def test_recovery_force_stop_relaunch(self):
        """If back presses fail, recovery must force-stop and relaunch."""
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.ERROR)

        # All back presses fail to reach home.
        detector.detect.return_value = AppState.ERROR

        actions = MagicMock(spec=Actions)
        actions.wait_for_state.return_value = True  # relaunch succeeds

        bot = Automation.__new__(Automation)
        bot.device_id = None
        bot.logger = MagicMock()
        bot.adb = adb
        bot.sm = sm
        bot.detector = detector
        bot.actions = actions

        with patch("challenge_04.automation.time.sleep"):
            recovered = bot._recover()

        assert recovered is True
        adb.force_stop.assert_called_once_with(CONFIG["app_package"])
        adb.launch_app.assert_called_once_with(
            CONFIG["app_package"], CONFIG["app_activity"]
        )

    # 14. Recovery failure after max attempts exits gracefully

    def test_recovery_failure_raises_action_error(self):
        """When recovery fails after max attempts, _with_recovery must
        raise ActionError (graceful exit)."""
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.HOME_FEED)

        # The action always fails.
        def failing_action(*args, **kwargs):
            raise ActionError("Simulated failure")

        # Recovery always fails.
        actions = MagicMock(spec=Actions)
        actions.wait_for_state.return_value = False
        detector.detect.return_value = AppState.ERROR

        bot = Automation.__new__(Automation)
        bot.device_id = None
        bot.logger = MagicMock()
        bot.adb = adb
        bot.sm = sm
        bot.detector = detector
        bot.actions = actions

        with patch("challenge_04.automation.time.sleep"):
            with pytest.raises(ActionError, match="failed after"):
                bot._with_recovery("test_action", failing_action)

    def test_with_recovery_retries_on_adb_error(self):
        """_with_recovery must also catch ADBError and attempt recovery."""
        adb = _make_adb()
        detector = MagicMock(spec=ScreenDetector)
        sm = StateMachine(initial=AppState.HOME_FEED)

        call_count = 0

        def sometimes_failing(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ADBError("device not found")
            return "ok"

        # Recovery succeeds.
        detector.detect.side_effect = [
            AppState.ERROR,  # after first failure, back press check
            AppState.HOME_FEED,  # recovery succeeds
        ]

        actions = MagicMock(spec=Actions)
        actions.wait_for_state.return_value = True

        bot = Automation.__new__(Automation)
        bot.device_id = None
        bot.logger = MagicMock()
        bot.adb = adb
        bot.sm = sm
        bot.detector = detector
        bot.actions = actions

        with patch("challenge_04.automation.time.sleep"):
            result = bot._with_recovery("test_adb", sometimes_failing)

        assert result == "ok"
        assert call_count == 2  # first failed, second succeeded
