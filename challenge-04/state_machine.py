"""State machine definitions and transition logic.

Each state represents a distinct screen or context inside the target
social-media app.  The transition table encodes which states can
legally follow which, and what action triggers the transition.
"""

from __future__ import annotations

import enum
import logging
from typing import Dict, FrozenSet, Optional

logger = logging.getLogger(__name__)


class AppState(enum.Enum):
    """All recognised screen states."""

    UNKNOWN = "UNKNOWN"
    HOME_FEED = "HOME_FEED"
    PROFILE = "PROFILE"
    UPLOAD = "UPLOAD"
    CAPTION_EDITOR = "CAPTION_EDITOR"
    POSTING = "POSTING"
    POST_COMPLETE = "POST_COMPLETE"
    POPUP = "POPUP"
    ERROR = "ERROR"


# Map each state to the set of states reachable from it.
TRANSITIONS: Dict[AppState, FrozenSet[AppState]] = {
    AppState.UNKNOWN:        frozenset({
        AppState.HOME_FEED, AppState.PROFILE, AppState.UPLOAD,
        AppState.POPUP, AppState.ERROR,
    }),
    AppState.HOME_FEED:      frozenset({
        AppState.PROFILE, AppState.UPLOAD, AppState.POPUP, AppState.ERROR,
    }),
    AppState.PROFILE:        frozenset({
        AppState.HOME_FEED, AppState.UPLOAD, AppState.POPUP, AppState.ERROR,
    }),
    AppState.UPLOAD:         frozenset({
        AppState.HOME_FEED, AppState.CAPTION_EDITOR, AppState.POPUP,
        AppState.ERROR,
    }),
    AppState.CAPTION_EDITOR: frozenset({
        AppState.UPLOAD, AppState.POSTING, AppState.POPUP, AppState.ERROR,
    }),
    AppState.POSTING:        frozenset({
        AppState.POST_COMPLETE, AppState.ERROR, AppState.POPUP,
    }),
    AppState.POST_COMPLETE:  frozenset({
        AppState.HOME_FEED, AppState.PROFILE, AppState.POPUP, AppState.ERROR,
    }),
    AppState.POPUP:          frozenset({
        # After dismissing a popup we can land back on any normal screen.
        AppState.HOME_FEED, AppState.PROFILE, AppState.UPLOAD,
        AppState.CAPTION_EDITOR, AppState.POSTING, AppState.POST_COMPLETE,
        AppState.POPUP, AppState.ERROR, AppState.UNKNOWN,
    }),
    AppState.ERROR:          frozenset({
        AppState.HOME_FEED, AppState.UNKNOWN,
    }),
}


class StateMachine:
    """Track and validate screen-state transitions.

    Attributes:
        current: The last confirmed ``AppState``.
        history: Ordered list of every state visited (most recent last).
    """

    def __init__(self, initial: AppState = AppState.UNKNOWN) -> None:
        self.current: AppState = initial
        self.history: list[AppState] = [initial]

    # ── public interface ────────────────────────────────────────

    def transition(self, target: AppState) -> bool:
        """Attempt to move to *target*.

        Returns ``True`` if the transition is valid per the table,
        ``False`` otherwise (the state is still updated so the rest of
        the system stays in sync, but a warning is logged).
        """
        valid = self._is_valid(self.current, target)
        if not valid:
            logger.warning(
                "Invalid transition %s -> %s (forcing anyway)",
                self.current.value,
                target.value,
            )
        self.current = target
        self.history.append(target)
        logger.debug("State: %s", target.value)
        return valid

    def can_transition(self, target: AppState) -> bool:
        """Check whether moving to *target* is legal without doing it."""
        return self._is_valid(self.current, target)

    @property
    def previous(self) -> Optional[AppState]:
        """Return the state before the current one, if any."""
        if len(self.history) >= 2:
            return self.history[-2]
        return None

    # ── internals ───────────────────────────────────────────────

    @staticmethod
    def _is_valid(source: AppState, target: AppState) -> bool:
        allowed = TRANSITIONS.get(source, frozenset())
        return target in allowed
