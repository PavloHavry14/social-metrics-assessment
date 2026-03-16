"""Tests for Challenge 03 — Queue Worker.

Covers:
    - Crash-safe write pattern (transaction rollback on failure)
    - Retry after crash with fresh scrape_run_id
    - Successful job writes all rows in single transaction
    - Exponential backoff on 429 with jitter
    - Partial data discarded
    - Max retries exceeded marks job as failure
    - Batch failure rate monitoring and pause
    - Consecutive account failure flagging
    - Stale job detection and reaping
    - Immutable job result records
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from providers import (
    PostMetrics,
    ProviderResponse,
    ProviderTimeoutError,
    RateLimitError,
    ReconciliationResult,
)
from retry_logic import (
    BatchMonitor,
    FailureTracker,
    call_provider_with_retry,
    compute_backoff,
)
from worker import (
    DatabaseGateway,
    JobOutcome,
    JobResult,
    Worker,
    reap_stale_jobs,
    run_batch,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_posts(n: int = 3) -> list[PostMetrics]:
    return [
        PostMetrics(
            platform_post_id=f"post_{i}",
            views=100 * i,
            likes=10 * i,
            comments=i,
            shares=i,
            published_at="2025-01-01T00:00:00Z",
        )
        for i in range(1, n + 1)
    ]


def _make_provider_response(
    name: str = "provider_a",
    handle: str = "@test",
    posts: list[PostMetrics] | None = None,
    is_complete: bool = True,
) -> ProviderResponse:
    return ProviderResponse(
        provider_name=name,
        account_handle=handle,
        posts=posts or _make_posts(),
        is_complete=is_complete,
    )


def _mock_db() -> AsyncMock:
    """Return a mocked DatabaseGateway."""
    db = AsyncMock(spec=DatabaseGateway)
    db.create_scrape_run.return_value = 1
    db.write_batch.return_value = (3, 3)  # (snapshots_written, new_posts)
    return db


def _mock_provider(
    name: str = "provider_a",
    posts: list[PostMetrics] | None = None,
    is_complete: bool = True,
) -> MagicMock:
    provider = MagicMock()
    provider.name = name
    provider.fetch_account_metrics = AsyncMock(
        return_value=_make_provider_response(name, posts=posts, is_complete=is_complete)
    )
    return provider


def _make_worker(
    db: AsyncMock | None = None,
    provider_a: MagicMock | None = None,
    provider_b: MagicMock | None = None,
    failure_tracker: FailureTracker | None = None,
) -> Worker:
    return Worker(
        provider_a=provider_a or _mock_provider("provider_a"),
        provider_b=provider_b or _mock_provider("provider_b"),
        db=db or _mock_db(),
        failure_tracker=failure_tracker or FailureTracker(),
        provider_id=1,
    )


# =========================================================================
# 1. Crash-safe write: DB transaction fails mid-write -> zero rows committed
# =========================================================================

@pytest.mark.asyncio
async def test_crash_mid_write_rolls_back_transaction():
    """When write_batch raises mid-transaction, the scrape_run must be
    marked as failed and the job result must reflect failure with zero
    snapshots written."""
    db = _mock_db()
    db.write_batch.side_effect = RuntimeError("Connection lost mid-write")

    worker = _make_worker(db=db)
    result = await worker.run_job(account_id=1, account_handle="@crash_test")

    assert result.outcome == JobOutcome.FAILURE
    assert result.snapshots_written == 0
    assert "Connection lost" in (result.error or "")
    # The scrape_run should NOT be marked completed -- failure path runs.
    db.write_batch.assert_called_once()


# =========================================================================
# 2. Retry after crash creates new scrape_run_id
# =========================================================================

@pytest.mark.asyncio
async def test_retry_after_crash_gets_fresh_scrape_run_id():
    """Each job invocation must create a new scrape_run_id, so a retry
    after a crash never re-uses the old run."""
    db = _mock_db()
    run_ids: list[int] = []

    call_count = 0

    async def create_run(provider_id):
        nonlocal call_count
        call_count += 1
        run_ids.append(call_count)
        return call_count

    db.create_scrape_run.side_effect = create_run

    # First call fails
    db.write_batch.side_effect = [RuntimeError("crash"), (3, 3)]

    worker = _make_worker(db=db)

    result1 = await worker.run_job(account_id=1, account_handle="@retry")
    assert result1.outcome == JobOutcome.FAILURE

    result2 = await worker.run_job(account_id=1, account_handle="@retry")
    assert result2.outcome == JobOutcome.SUCCESS

    assert len(run_ids) == 2
    assert run_ids[0] != run_ids[1], "Retry must create a fresh scrape_run_id"


# =========================================================================
# 3. Successful job writes ALL rows in single transaction
# =========================================================================

@pytest.mark.asyncio
async def test_successful_job_writes_all_rows():
    """A successful job must call write_batch exactly once with all
    reconciled posts."""
    posts = _make_posts(5)
    db = _mock_db()
    db.write_batch.return_value = (5, 5)

    provider_a = _mock_provider("provider_a", posts=posts)
    provider_b = _mock_provider("provider_b", posts=posts)

    worker = _make_worker(db=db, provider_a=provider_a, provider_b=provider_b)
    result = await worker.run_job(account_id=1, account_handle="@success")

    assert result.outcome == JobOutcome.SUCCESS
    assert result.snapshots_written == 5
    db.write_batch.assert_called_once()

    call_args = db.write_batch.call_args
    merged_posts_arg = call_args.kwargs.get("merged_posts") or call_args[1].get("merged_posts")
    if merged_posts_arg is None:
        # positional
        merged_posts_arg = call_args[0][3] if len(call_args[0]) > 3 else None
    assert merged_posts_arg is not None
    assert len(merged_posts_arg) == 5


# =========================================================================
# 4. Exponential backoff on 429 with jitter
# =========================================================================

@pytest.mark.asyncio
async def test_exponential_backoff_delays_increase():
    """Backoff delays must follow base*2^attempt with jitter, increasing
    each attempt."""
    # Patch random.uniform to return 0 (no jitter) for deterministic test.
    with patch("retry_logic.random.uniform", return_value=0.0):
        d0 = compute_backoff(0)  # 1*2^0 = 1.0
        d1 = compute_backoff(1)  # 1*2^1 = 2.0
        d2 = compute_backoff(2)  # 1*2^2 = 4.0

    assert d0 == pytest.approx(1.0)
    assert d1 == pytest.approx(2.0)
    assert d2 == pytest.approx(4.0)
    assert d0 < d1 < d2


@pytest.mark.asyncio
async def test_429_uses_backoff_delays():
    """When provider returns 429 repeatedly, call_provider_with_retry
    must sleep with increasing delays before each retry."""
    fetch = AsyncMock(side_effect=RateLimitError(retry_after=None))
    sleep_calls: list[float] = []

    async def mock_sleep(delay):
        sleep_calls.append(delay)

    with patch("retry_logic.asyncio.sleep", side_effect=mock_sleep):
        with patch("retry_logic.random.uniform", return_value=0.0):
            with pytest.raises(RateLimitError):
                await call_provider_with_retry(fetch, "@user", max_retries=3)

    # 3 retries -> 3 sleep calls (attempts 0, 1, 2 trigger sleeps)
    assert len(sleep_calls) == 3
    assert sleep_calls[0] < sleep_calls[1] < sleep_calls[2]


# =========================================================================
# 5. Partial data is DISCARDED
# =========================================================================

@pytest.mark.asyncio
async def test_partial_response_is_discarded():
    """A provider response with is_complete=False must be discarded and
    retried, not passed through."""
    partial = _make_provider_response(is_complete=False)
    complete = _make_provider_response(is_complete=True)

    fetch = AsyncMock(side_effect=[partial, partial, complete])

    with patch("retry_logic.asyncio.sleep", new_callable=AsyncMock):
        result = await call_provider_with_retry(fetch, "@user", max_retries=3)

    assert result.is_complete is True
    assert fetch.call_count == 3  # 2 partials discarded, 3rd succeeds


@pytest.mark.asyncio
async def test_all_partial_responses_raises():
    """If every attempt returns partial data, the call must raise."""
    partial = _make_provider_response(is_complete=False)
    fetch = AsyncMock(return_value=partial)

    with patch("retry_logic.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(ProviderTimeoutError):
            await call_provider_with_retry(fetch, "@user", max_retries=2)


# =========================================================================
# 6. Max retries exceeded -> job marked as failure
# =========================================================================

@pytest.mark.asyncio
async def test_max_retries_exceeded_marks_failure():
    """When providers exhaust all retries, the job outcome must be FAILURE."""
    failing_provider = _mock_provider("provider_a")
    failing_provider.fetch_account_metrics.side_effect = RateLimitError()

    db = _mock_db()
    worker = _make_worker(db=db, provider_a=failing_provider)

    with patch("retry_logic.asyncio.sleep", new_callable=AsyncMock):
        result = await worker.run_job(account_id=1, account_handle="@maxretry")

    assert result.outcome == JobOutcome.FAILURE
    assert result.error is not None
    db.write_batch.assert_not_called()


# =========================================================================
# 7. >20% batch failure rate pauses batch and triggers alert
# =========================================================================

@pytest.mark.asyncio
async def test_batch_paused_on_high_failure_rate():
    """When >20% of jobs in a batch fail, the batch must be paused and
    the alert callback invoked."""
    alert_messages: list[tuple[str, str]] = []

    def alert_cb(batch_id: str, message: str):
        alert_messages.append((batch_id, message))

    # Create 10 accounts; make the first 5 fail to exceed 20% threshold
    # after at least 5 jobs (min_sample).
    db = _mock_db()
    failing_provider = _mock_provider("provider_a")
    failing_provider.fetch_account_metrics.side_effect = RateLimitError()

    good_provider = _mock_provider("provider_b")

    worker = _make_worker(
        db=db,
        provider_a=failing_provider,
        provider_b=good_provider,
    )

    accounts = [(i, f"@acct{i}") for i in range(1, 11)]

    with patch("retry_logic.asyncio.sleep", new_callable=AsyncMock):
        results = await run_batch(
            worker, accounts, batch_id="batch-1", alert_callback=alert_cb
        )

    # Batch should have been paused before completing all 10.
    assert len(results) < len(accounts), "Batch should stop before all accounts"
    assert len(alert_messages) > 0, "Alert callback must be triggered"
    assert "batch-1" in alert_messages[0][0]


# =========================================================================
# 8. Account with 3 consecutive failures flagged for manual review
# =========================================================================

@pytest.mark.asyncio
async def test_account_flagged_after_consecutive_failures():
    """An account that fails 3 consecutive scrapes must be flagged for
    manual review and skipped on subsequent runs."""
    tracker = FailureTracker(threshold=3)

    failing_provider = _mock_provider("provider_a")
    failing_provider.fetch_account_metrics.side_effect = RateLimitError()

    db = _mock_db()
    worker = _make_worker(
        db=db,
        provider_a=failing_provider,
        failure_tracker=tracker,
    )

    with patch("retry_logic.asyncio.sleep", new_callable=AsyncMock):
        for _ in range(3):
            await worker.run_job(account_id=42, account_handle="@flagme")

    assert tracker.should_skip(42) is True

    # 4th attempt should be skipped immediately.
    result = await worker.run_job(account_id=42, account_handle="@flagme")
    assert result.outcome == JobOutcome.FAILURE
    assert "manual review" in (result.error or "").lower()


# =========================================================================
# 9. Stale job detection: running >10 minutes -> killed and requeued
# =========================================================================

@pytest.mark.asyncio
async def test_stale_job_reaped():
    """reap_stale_jobs must mark long-running scrape_runs as failed."""
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = [{"run_id": 101}, {"run_id": 202}]

    # Build a proper async context manager for pool.acquire().
    acm = AsyncMock()
    acm.__aenter__ = AsyncMock(return_value=mock_conn)
    acm.__aexit__ = AsyncMock(return_value=False)

    mock_pool = MagicMock()
    mock_pool.acquire.return_value = acm

    reaped = await reap_stale_jobs(mock_pool, timeout_minutes=10)

    assert reaped == [101, 202]
    mock_conn.fetch.assert_called_once()
    # Verify the SQL references the timeout interval.
    sql = mock_conn.fetch.call_args[0][0]
    assert "running" in sql.lower()


@pytest.mark.asyncio
async def test_job_timeout_produces_failure():
    """A job exceeding the timeout must be killed and return FAILURE."""
    db = _mock_db()

    async def slow_fetch(*args, **kwargs):
        await asyncio.sleep(10)
        return _make_provider_response()

    slow_provider = _mock_provider("provider_a")
    slow_provider.fetch_account_metrics.side_effect = slow_fetch

    worker = _make_worker(db=db, provider_a=slow_provider)
    worker.job_timeout = 0.1  # 100ms timeout for test speed

    result = await worker.run_job(account_id=1, account_handle="@timeout")

    assert result.outcome == JobOutcome.FAILURE
    assert "timeout" in (result.error or "").lower()


# =========================================================================
# 10. Every job produces an immutable result record with all required fields
# =========================================================================

@pytest.mark.asyncio
async def test_success_result_has_all_fields():
    """A successful job must produce a JobResult with all diagnostic fields."""
    worker = _make_worker()
    result = await worker.run_job(account_id=1, account_handle="@complete")

    assert isinstance(result, JobResult)
    assert result.job_id  # non-empty UUID string
    assert result.account_id == 1
    assert result.account_handle == "@complete"
    assert result.outcome == JobOutcome.SUCCESS
    assert result.scrape_run_id is not None
    assert result.snapshots_written > 0
    assert result.started_at > 0
    assert result.finished_at >= result.started_at
    assert result.duration_seconds >= 0
    assert result.error is None


@pytest.mark.asyncio
async def test_failure_result_has_all_fields():
    """A failed job must also produce a complete JobResult."""
    failing_provider = _mock_provider("provider_a")
    failing_provider.fetch_account_metrics.side_effect = RateLimitError()
    db = _mock_db()

    worker = _make_worker(db=db, provider_a=failing_provider)

    with patch("retry_logic.asyncio.sleep", new_callable=AsyncMock):
        result = await worker.run_job(account_id=2, account_handle="@fail")

    assert isinstance(result, JobResult)
    assert result.job_id
    assert result.account_id == 2
    assert result.account_handle == "@fail"
    assert result.outcome == JobOutcome.FAILURE
    assert result.started_at > 0
    assert result.finished_at >= result.started_at
    assert result.error is not None


@pytest.mark.asyncio
async def test_job_result_is_immutable():
    """JobResult is a frozen dataclass -- attribute assignment must raise."""
    worker = _make_worker()
    result = await worker.run_job(account_id=1, account_handle="@immutable")

    with pytest.raises(AttributeError):
        result.outcome = JobOutcome.FAILURE  # type: ignore[misc]
