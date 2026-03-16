"""Screen-state detection via UI hierarchy parsing and screenshot fallback.

The primary strategy parses the XML produced by ``uiautomator dump`` and
looks for signature UI elements that identify each screen.  When the
dump is unavailable or ambiguous a screenshot-based pixel-sampling
fallback is used.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from .adb_controller import ADBController
from .state_machine import AppState

logger = logging.getLogger(__name__)

# ── Signature element patterns per state ────────────────────────
# Each entry maps an AppState to a list of (attribute, regex) pairs.
# If *any* element in the hierarchy matches *all* pairs in a group,
# the state is considered detected.

_SIGNATURE_PATTERNS: Dict[AppState, List[List[Tuple[str, str]]]] = {
    AppState.HOME_FEED: [
        # TikTok shows "Following" and "For You" tabs on the home feed.
        [("text", r"(?i)following")],
        [("text", r"(?i)for you")],
        [("resource-id", r"(?i).*home.*tab")],
        [("content-desc", r"(?i)home")],
    ],
    AppState.PROFILE: [
        [("text", r"(?i)followers")],
        [("text", r"(?i)following"), ("text", r"(?i)likes")],
        [("resource-id", r"(?i).*profile")],
        [("content-desc", r"(?i)profile")],
    ],
    AppState.UPLOAD: [
        [("text", r"(?i)upload")],
        [("text", r"(?i)camera")],
        [("text", r"(?i)gallery")],
        [("resource-id", r"(?i).*upload|.*camera|.*gallery")],
        [("content-desc", r"(?i)record|camera|gallery")],
    ],
    AppState.CAPTION_EDITOR: [
        [("text", r"(?i)caption|describe your video")],
        [("resource-id", r"(?i).*caption|.*describe")],
        [("text", r"(?i)post"), ("text", r"(?i)hashtag|#")],
    ],
    AppState.POSTING: [
        [("text", r"(?i)posting|uploading")],
        [("resource-id", r"(?i).*progress|.*upload")],
    ],
    AppState.POST_COMPLETE: [
        [("text", r"(?i)your video is (now )?posted")],
        [("text", r"(?i)uploaded successfully")],
    ],
    AppState.POPUP: [
        [("resource-id", r"(?i).*dialog|.*modal|.*popup|.*alert")],
        [("text", r"(?i)rate this app")],
        [("text", r"(?i)update available")],
        [("text", r"(?i)login expired|session expired")],
        [("text", r"(?i)network error|no internet")],
        [("class", r"android\.app\.Dialog")],
    ],
}

# Known dismiss-button patterns for popups.
POPUP_DISMISS_PATTERNS: List[List[Tuple[str, str]]] = [
    [("text", r"(?i)^not now$")],
    [("text", r"(?i)^later$")],
    [("text", r"(?i)^cancel$")],
    [("text", r"(?i)^dismiss$")],
    [("text", r"(?i)^close$")],
    [("text", r"(?i)^no thanks$")],
    [("text", r"(?i)^skip$")],
    [("content-desc", r"(?i)close|dismiss|cancel")],
]


class ScreenDetector:
    """Detect the current screen state of the target app.

    Parameters:
        adb: An initialised ``ADBController``.
        app_package: The package name of the target app (used to filter
            irrelevant system UI nodes).
    """

    def __init__(self, adb: ADBController, app_package: str) -> None:
        self.adb = adb
        self.app_package = app_package

    # ── public API ──────────────────────────────────────────────

    def detect(self) -> AppState:
        """Return the best-guess ``AppState`` for the current screen.

        1. Try UI hierarchy XML parsing.
        2. Fall back to screenshot pixel-sampling.
        3. Return ``UNKNOWN`` if neither method is conclusive.
        """
        xml = self.adb.dump_ui_hierarchy()
        if xml:
            state = self._detect_from_xml(xml)
            if state is not None:
                logger.debug("Detected state from XML: %s", state.value)
                return state

        # Fallback: screenshot-based heuristic.
        state = self._detect_from_screenshot()
        if state is not None:
            logger.debug("Detected state from screenshot: %s", state.value)
            return state

        logger.warning("Could not detect screen state; returning UNKNOWN")
        return AppState.UNKNOWN

    def find_popup_dismiss_button(self) -> Optional[Tuple[float, float]]:
        """Locate a dismiss button in a popup dialog.

        Returns ratio-based ``(x, y)`` coordinates, or ``None``.
        """
        xml = self.adb.dump_ui_hierarchy()
        if not xml:
            return None

        root = self._parse_xml(xml)
        if root is None:
            return None

        for patterns in POPUP_DISMISS_PATTERNS:
            for node in root.iter("node"):
                if self._node_matches(node, patterns):
                    bounds = self._parse_bounds(node.get("bounds", ""))
                    if bounds:
                        return bounds
        return None

    def find_element_coords(
        self, attr: str, pattern: str
    ) -> Optional[Tuple[float, float]]:
        """Find the centre of the first element whose *attr* matches *pattern*.

        Returns ratio-based ``(x, y)`` or ``None``.
        """
        xml = self.adb.dump_ui_hierarchy()
        if not xml:
            return None

        root = self._parse_xml(xml)
        if root is None:
            return None

        regex = re.compile(pattern, re.IGNORECASE)
        for node in root.iter("node"):
            value = node.get(attr, "")
            if regex.search(value):
                bounds = self._parse_bounds(node.get("bounds", ""))
                if bounds:
                    return bounds
        return None

    def is_element_present(self, attr: str, pattern: str) -> bool:
        """Return ``True`` if any element's *attr* matches *pattern*."""
        return self.find_element_coords(attr, pattern) is not None

    # ── XML-based detection ────────────────────────────────────

    def _detect_from_xml(self, xml: str) -> Optional[AppState]:
        """Score each candidate state against the UI hierarchy."""
        root = self._parse_xml(xml)
        if root is None:
            return None

        # Check popup first -- popups overlay other screens.
        if self._state_matches(root, AppState.POPUP):
            return AppState.POPUP

        # Score remaining states by number of matching signature groups.
        scores: Dict[AppState, int] = {}
        for state, groups in _SIGNATURE_PATTERNS.items():
            if state is AppState.POPUP:
                continue
            score = sum(1 for g in groups if self._group_matches(root, g))
            if score > 0:
                scores[state] = score

        if not scores:
            return None

        best = max(scores, key=lambda s: scores[s])
        return best

    def _state_matches(self, root: ET.Element, state: AppState) -> bool:
        groups = _SIGNATURE_PATTERNS.get(state, [])
        return any(self._group_matches(root, g) for g in groups)

    def _group_matches(
        self, root: ET.Element, patterns: List[Tuple[str, str]]
    ) -> bool:
        """Return ``True`` if any node in *root* satisfies all *patterns*."""
        for node in root.iter("node"):
            if self._node_matches(node, patterns):
                return True
        return False

    @staticmethod
    def _node_matches(
        node: ET.Element, patterns: List[Tuple[str, str]]
    ) -> bool:
        for attr, regex in patterns:
            value = node.get(attr, "")
            if not re.search(regex, value):
                return False
        return True

    # ── bounds parsing ─────────────────────────────────────────

    def _parse_bounds(self, bounds_str: str) -> Optional[Tuple[float, float]]:
        """Parse ``[x1,y1][x2,y2]`` into ratio-based centre ``(rx, ry)``."""
        match = re.match(
            r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds_str
        )
        if not match:
            return None

        x1, y1, x2, y2 = (int(g) for g in match.groups())
        cx = (x1 + x2) / 2
        cy = (y1 + y2) / 2

        w, h = self.adb.get_screen_size()
        return cx / w, cy / h

    # ── screenshot fallback ────────────────────────────────────

    def _detect_from_screenshot(self) -> Optional[AppState]:
        """Heuristic detection from a screenshot.

        This is intentionally lightweight: it checks whether the target
        app is in the foreground at all.  Pixel-level classifiers would
        go here in a production system (e.g. a small CNN or template
        matching), but for portability we keep it simple.
        """
        if not self.adb.is_app_foreground(self.app_package):
            return AppState.ERROR
        # Without a trained model we cannot reliably distinguish screens
        # from a screenshot alone.
        return None

    # ── XML parsing helper ─────────────────────────────────────

    @staticmethod
    def _parse_xml(xml: str) -> Optional[ET.Element]:
        try:
            return ET.fromstring(xml)
        except ET.ParseError as exc:
            logger.warning("Failed to parse UI XML: %s", exc)
            return None
