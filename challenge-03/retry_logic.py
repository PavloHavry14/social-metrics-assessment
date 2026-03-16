"""Retry logic with exponential backoff, jitter, and failure tracking.

Key design decisions:
- Partial data is DISCARDED, not saved. A timeout that yields 30 of 47 posts
  is treated as a failure because partial data creates false "disappeared"
  signals downstream.
- Consecutive-failure tracking is per-account and persisted so that an account
  that fails 3 scrapes in a row is flagged for manual review and excluded from
  further automatic retries.
- Batch-level monitoring pauses the entire batch if >20% of jobs fail.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TypeVar

from providers import (
    ProviderError,
    ProviderResponse,
    ProviderTimeoutError,
    RateLimitError,
    ProviderUnavailableError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# -------------------------------------------------------------------------
# Configuration constants
# -------------------------------------------------------------------------

MAX_RETRIES: int = 3
BASE_DELAY: float = 1.0       # seconds
MAX_DELAY: float = 30.0       # seconds
JITTER_MAX: float = 1.0       # seconds of random jitter
CONSECUTIVE_FAILURE_THRESHOLD: int = 3
BATCH_FAILURE_RATE_THRESHOLD: float = 0.20  # 20 %
JOB_TIMEOUT_SECONDS: float = 600.0  # 10 minutes


# -------------------------------------------------------------------------
# Backoff helpers
# -------------------------------------------------------------------------


def compute_backoff(attempt: int) -> float:
    """Exponential backoff with random jitter, capped at MAX_DELAY.

    Formula: min(base * 2^attempt + random_jitter, max_delay)

    Args:
        attempt: Zero-based retry attempt number (0 = first retry).

    Returns:
        Delay in seconds before the next attempt.
    """
    delay = BASE_DELAY * (2 ** attempt) + random.uniform(0, JITTER_MAX)
    return min(delay, MAX_DELAY)


# -------------------------------------------------------------------------
# Retry wrapper for provider calls
# -------------------------------------------------------------------------


async def call_provider_with_retry(
    fetch_fn: Callable[..., Awaitable[ProviderResponse]],
    *args: Any,
    max_retries: int = MAX_RETRIES,
    **kwargs: Any,
) -> ProviderResponse:
    """Call a provider's fetch function with retry on transient failures.

    Retries on:
        - RateLimitError (HTTP 429): respects Retry-After header if present,
          otherwise uses exponential backoff.
        - ProviderTimeoutError: the provider did not respond in time.
        - ProviderUnavailableError: 5xx from the provider.

    Partial responses (is_complete=False) are treated as failures and
    discarded — partial data would create false "disappeared" posts.

    Args:
        fetch_fn: Async callable (e.g. provider.fetch_account_metrics).
        *args: Positional arguments forwarded to *fetch_fn*.
        max_retries: Maximum number of retry attempts (default 3).
        **kwargs: Keyword arguments forwarded to *fetch_fn*.

    Returns:
        A complete ProviderResponse.

    Raises:
        ProviderError: If all retries are exhausted.
    """
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):  # attempt 0 is the initial call
        try:
            response = await fetch_fn(*args, **kwargs)

            # Discard partial results — they are worse than no data.
            if not response.is_complete:
                logger.warning(
                    "Provider %s returned partial data (%d posts) for %s — "
                    "discarding and retrying (attempt %d/%d)",
                    response.provider_name,
                    len(response.posts),
                    response.account_handle,
                    attempt + 1,
                    max_retries + 1,
                )
                last_error = ProviderTimeoutError(
                    f"Partial response from {response.provider_name}: "
                    f"{len(response.posts)} posts (incomplete)"
                )
                if attempt < max_retries:
                    await asyncio.sleep(compute_backoff(attempt))
                continue

            return response

        except RateLimitError as exc:
            last_error = exc
            if attempt < max_retries:
                delay = (
                    exc.retry_after
                    if exc.retry_after is not None
                    else compute_backoff(attempt)
                )
                logger.info(
                    "Rate-limited by provider (attempt %d/%d), "
                    "backing off %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    delay,
                )
                await asyncio.sleep(delay)

        except (ProviderTimeoutError, ProviderUnavailableError) as exc:
            last_error = exc
            if attempt < max_retries:
                delay = compute_backoff(attempt)
                logger.warning(
                    "%s on attempt %d/%d — retrying in %.1fs: %s",
                    type(exc).__name__,
                    attempt + 1,
                    max_retries + 1,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

    # All retries exhausted.
    raise last_error or ProviderError("Unknown provider failure after retries")


# -------------------------------------------------------------------------
# Consecutive failure tracker (per-account)
# -------------------------------------------------------------------------


@dataclass
class AccountFailureRecord:
    """Tracks consecutive scrape failures for a single account."""

    account_id: int
    consecutive_failures: int = 0
    flagged_for_review: bool = False
    last_failure_at: float | None = None
    last_error: str | None = None


class FailureTracker:
    """Persistent store for per-account consecutive failure counts.

    In production this would be backed by PostgreSQL or Redis. Here we use
    an in-memory dict to define the interface and logic. A real deployment
    would call ``load()`` / ``save()`` against the database.
    """

    def __init__(self, threshold: int = CONSECUTIVE_FAILURE_THRESHOLD) -> None:
        self._threshold = threshold
        self._records: dict[int, AccountFailureRecord] = {}

    def record_success(self, account_id: int) -> None:
        """Reset the failure counter after a successful scrape."""
        rec = self._records.get(account_id)
        if rec:
            rec.consecutive_failures = 0
            # Note: flagged_for_review stays True — manual un-flag required.

    def record_failure(self, account_id: int, error: str) -> AccountFailureRecord:
        """Increment the failure counter and flag if threshold is reached.

        Returns:
            The updated AccountFailureRecord.
        """
        rec = self._records.setdefault(
            account_id, AccountFailureRecord(account_id=account_id)
        )
        rec.consecutive_failures += 1
        rec.last_failure_at = time.time()
        rec.last_error = error

        if rec.consecutive_failures >= self._threshold and not rec.flagged_for_review:
            rec.flagged_for_review = True
            logger.error(
                "Account %d has failed %d consecutive scrapes — "
                "flagged for manual review. Last error: %s",
                account_id,
                rec.consecutive_failures,
                error,
            )

        return rec

    def should_skip(self, account_id: int) -> bool:
        """Return True if the account has been flagged and should not be retried."""
        rec = self._records.get(account_id)
        return bool(rec and rec.flagged_for_review)

    def get_record(self, account_id: int) -> AccountFailureRecord | None:
        """Return the failure record for an account, or None."""
        return self._records.get(account_id)

    def unflag(self, account_id: int) -> None:
        """Manually clear the review flag (e.g. after an operator investigates)."""
        rec = self._records.get(account_id)
        if rec:
            rec.flagged_for_review = False
            rec.consecutive_failures = 0


# -------------------------------------------------------------------------
# Batch monitor
# -------------------------------------------------------------------------


@dataclass
class BatchMonitor:
    """Tracks job outcomes within a batch and pauses the batch when the
    failure rate exceeds the configured threshold.

    A "batch" is a set of scrape jobs dispatched together (e.g. all accounts
    due for refresh this cycle).
    """

    batch_id: str
    total_jobs: int
    failure_threshold: float = BATCH_FAILURE_RATE_THRESHOLD
    _succeeded: int = field(default=0, init=False)
    _failed: int = field(default=0, init=False)
    _paused: bool = field(default=False, init=False)
    _alert_callback: Callable[[str, str], None] | None = field(
        default=None, repr=False
    )

    @property
    def completed(self) -> int:
        return self._succeeded + self._failed

    @property
    def failure_rate(self) -> float:
        return self._failed / self.completed if self.completed else 0.0

    @property
    def is_paused(self) -> bool:
        return self._paused

    def set_alert_callback(self, cb: Callable[[str, str], None]) -> None:
        """Register a callback invoked when the batch is paused.

        Signature: cb(batch_id, message)
        """
        self._alert_callback = cb

    def record_success(self) -> None:
        """Record a successful job completion."""
        self._succeeded += 1

    def record_failure(self, error_summary: str = "") -> None:
        """Record a failed job and check whether the batch should be paused.

        Args:
            error_summary: Brief description of the failure for alerting.
        """
        self._failed += 1
        self._check_threshold(error_summary)

    def _check_threshold(self, error_summary: str) -> None:
        """Pause the batch and trigger an alert if the failure rate is too high."""
        if self._paused:
            return

        # Only evaluate after a meaningful number of jobs have completed to
        # avoid false positives on the first few jobs.
        min_sample = max(5, int(self.total_jobs * 0.10))
        if self.completed < min_sample:
            return

        if self.failure_rate > self.failure_threshold:
            self._paused = True
            msg = (
                f"Batch {self.batch_id} PAUSED: failure rate "
                f"{self.failure_rate:.0%} ({self._failed}/{self.completed}) "
                f"exceeds threshold {self.failure_threshold:.0%}. "
                f"Last error: {error_summary}"
            )
            logger.critical(msg)
            if self._alert_callback:
                self._alert_callback(self.batch_id, msg)
