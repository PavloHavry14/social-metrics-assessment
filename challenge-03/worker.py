"""Queue worker for refreshing social-media metrics.

Implements the scrape-run batching pattern with full transaction isolation:
all metric snapshots for a single job are written in ONE atomic transaction
tagged with a scrape_run_id. If the process crashes mid-write, the
transaction rolls back and zero rows are persisted. On retry, a fresh
scrape_run_id is created — no duplicates, no stale overwrites, no partial
writes.

Architecture:
    Queue (Redis / SQS / etc.)
        -> Worker.run_job()
            -> create scrape_run (status='running')
            -> call providers with retry
            -> reconcile
            -> BEGIN single transaction
                 INSERT posts (if new)
                 INSERT metric_snapshots (all rows)
                 INSERT scrape_run_accounts
                 UPDATE scrape_run status='completed'
               COMMIT
            -> on failure: rollback, mark scrape_run as 'failed'
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from providers import (
    MetricsProvider,
    PostMetrics,
    ProviderError,
    ProviderResponse,
    ReconciliationResult,
    reconcile,
)
from retry_logic import (
    BatchMonitor,
    FailureTracker,
    JOB_TIMEOUT_SECONDS,
    call_provider_with_retry,
)

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------------
# Job result record (immutable)
# -------------------------------------------------------------------------


class JobOutcome(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class JobResult:
    """Immutable record produced by every job execution.

    This is the single source of truth for what happened during a scrape job.
    It is written to a persistent store (or returned to the caller) regardless
    of whether the job succeeded or failed.
    """

    job_id: str
    account_id: int
    account_handle: str
    outcome: JobOutcome
    scrape_run_id: int | None

    # Provider diagnostics
    provider_a_posts: int | None = None
    provider_b_posts: int | None = None
    reconciled_posts: int | None = None
    overlap_count: int | None = None
    only_in_a: int | None = None
    only_in_b: int | None = None

    # Write stats
    snapshots_written: int = 0
    new_posts_created: int = 0

    # Timing
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def duration_seconds(self) -> float:
        return self.finished_at - self.started_at

    # Error details (populated on partial/failure)
    error: str | None = None
    error_detail: str | None = None


# -------------------------------------------------------------------------
# Database gateway (abstracts asyncpg / psycopg connection details)
# -------------------------------------------------------------------------


class DatabaseGateway:
    """Async interface to PostgreSQL.

    In production, construct with an asyncpg.Pool or psycopg async pool.
    The methods below show the exact SQL executed; the connection and
    transaction lifecycle is managed by the caller (Worker).
    """

    def __init__(self, pool: Any) -> None:
        """
        Args:
            pool: An asyncpg.Pool or equivalent async connection pool.
        """
        self._pool = pool

    async def create_scrape_run(self, provider_id: int) -> int:
        """Insert a new scrape_run with status='running' and return its run_id."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO scrape_runs (provider_id, status)
                VALUES ($1, 'running')
                RETURNING run_id
                """,
                provider_id,
            )
            return row["run_id"]

    async def mark_scrape_run(
        self,
        run_id: int,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Update a scrape_run's status and ended_at timestamp.

        Called OUTSIDE the main data transaction so that failure status is
        recorded even when the data transaction rolls back.
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE scrape_runs
                SET status = $2,
                    ended_at = now(),
                    error_message = $3
                WHERE run_id = $1
                """,
                run_id,
                status,
                error_message,
            )

    async def write_batch(
        self,
        run_id: int,
        provider_id: int,
        account_id: int,
        merged_posts: list[PostMetrics],
        reconciliation: ReconciliationResult,
    ) -> tuple[int, int]:
        """Write all results in a single atomic transaction.

        Steps within the transaction:
            1. Ensure each post exists in the posts table (INSERT ... ON
               CONFLICT DO NOTHING — the post row is mutable metadata, not
               a metric observation, so an upsert here is safe).
            2. INSERT one metric_snapshot row per post.
            3. INSERT a scrape_run_accounts summary row.
            4. UPDATE the scrape_run status to 'completed'.

        If any step fails, the entire transaction rolls back and the
        scrape_run remains in 'running' state (to be cleaned up by the
        stale-job reaper).

        Args:
            run_id: The scrape_run_id for this job.
            provider_id: FK to the providers table.
            account_id: FK to the accounts table.
            merged_posts: Reconciled post metrics to persist.
            reconciliation: Reconciliation diagnostics.

        Returns:
            Tuple of (snapshots_written, new_posts_created).
        """
        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # --- 1. Ensure posts exist ------------------------------------
                new_posts = 0
                post_id_map: dict[str, int] = {}

                for pm in merged_posts:
                    # Try to insert; if the post already exists, just fetch its id.
                    row = await conn.fetchrow(
                        """
                        INSERT INTO posts (account_id, platform_post_id,
                                           provider_id, published_at)
                        VALUES ($1, $2, $3, $4::timestamptz)
                        ON CONFLICT (platform_post_id, provider_id) DO NOTHING
                        RETURNING post_id
                        """,
                        account_id,
                        pm.platform_post_id,
                        provider_id,
                        pm.published_at,
                    )
                    if row:
                        post_id_map[pm.platform_post_id] = row["post_id"]
                        new_posts += 1
                    else:
                        # Already existed — look up the id.
                        existing = await conn.fetchrow(
                            """
                            SELECT post_id FROM posts
                            WHERE platform_post_id = $1 AND provider_id = $2
                            """,
                            pm.platform_post_id,
                            provider_id,
                        )
                        post_id_map[pm.platform_post_id] = existing["post_id"]

                # --- 2. Insert metric snapshots (bulk) ------------------------
                snapshot_rows = [
                    (
                        post_id_map[pm.platform_post_id],
                        run_id,
                        pm.views,
                        pm.likes,
                        pm.comments,
                        pm.shares,
                    )
                    for pm in merged_posts
                ]

                await conn.executemany(
                    """
                    INSERT INTO metric_snapshots
                        (post_id, run_id, views, likes, comments, shares)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    snapshot_rows,
                )

                # --- 3. Scrape-run account summary ----------------------------
                await conn.execute(
                    """
                    INSERT INTO scrape_run_accounts
                        (run_id, account_id, posts_expected, posts_found)
                    VALUES ($1, $2, $3, $4)
                    """,
                    run_id,
                    account_id,
                    len(merged_posts),
                    len(merged_posts),
                )

                # --- 4. Mark scrape run completed (inside txn) ----------------
                await conn.execute(
                    """
                    UPDATE scrape_runs
                    SET status = 'completed', ended_at = now()
                    WHERE run_id = $1
                    """,
                    run_id,
                )

                return len(snapshot_rows), new_posts


# -------------------------------------------------------------------------
# Worker
# -------------------------------------------------------------------------


@dataclass
class Worker:
    """Processes scrape jobs for a single account.

    Lifecycle of a single job:
        1. Create a scrape_run record (status='running').
        2. Call both providers with retry logic.
        3. Reconcile responses (take max per metric for overlapping posts).
        4. Write ALL results in one atomic transaction.
        5. If anything fails, the transaction never commits — retry is clean.

    Attributes:
        provider_a: First metrics provider.
        provider_b: Second metrics provider.
        db: Database gateway.
        failure_tracker: Per-account consecutive failure tracker.
        provider_id: FK into the providers table for this worker's platform.
    """

    provider_a: MetricsProvider
    provider_b: MetricsProvider
    db: DatabaseGateway
    failure_tracker: FailureTracker
    provider_id: int
    job_timeout: float = JOB_TIMEOUT_SECONDS

    async def run_job(
        self,
        account_id: int,
        account_handle: str,
        batch_monitor: BatchMonitor | None = None,
    ) -> JobResult:
        """Execute a single scrape job for one account.

        This is the main entry point called by the queue consumer.

        Args:
            account_id: Database PK for the account.
            account_handle: Platform handle (e.g. '@example').
            batch_monitor: Optional batch-level failure monitor.

        Returns:
            An immutable JobResult describing exactly what happened.
        """
        job_id = str(uuid.uuid4())
        started_at = time.time()

        # Check if this account has been flagged for manual review.
        if self.failure_tracker.should_skip(account_id):
            logger.info(
                "Skipping account %d (%s) — flagged for manual review",
                account_id,
                account_handle,
            )
            return JobResult(
                job_id=job_id,
                account_id=account_id,
                account_handle=account_handle,
                outcome=JobOutcome.FAILURE,
                scrape_run_id=None,
                started_at=started_at,
                finished_at=time.time(),
                error="Account flagged for manual review — skipping",
            )

        scrape_run_id: int | None = None

        try:
            result = await asyncio.wait_for(
                self._execute_job(
                    job_id, account_id, account_handle, started_at
                ),
                timeout=self.job_timeout,
            )

            # Record success.
            self.failure_tracker.record_success(account_id)
            if batch_monitor:
                batch_monitor.record_success()

            return result

        except asyncio.TimeoutError:
            error_msg = (
                f"Job {job_id} for account {account_id} exceeded "
                f"{self.job_timeout}s timeout — killed and requeued"
            )
            logger.error(error_msg)
            return await self._finalize_failure(
                job_id=job_id,
                account_id=account_id,
                account_handle=account_handle,
                started_at=started_at,
                scrape_run_id=scrape_run_id,
                error_msg=error_msg,
                batch_monitor=batch_monitor,
            )

        except Exception as exc:
            error_msg = f"{type(exc).__name__}: {exc}"
            logger.exception("Job %s failed: %s", job_id, error_msg)
            return await self._finalize_failure(
                job_id=job_id,
                account_id=account_id,
                account_handle=account_handle,
                started_at=started_at,
                scrape_run_id=scrape_run_id,
                error_msg=error_msg,
                batch_monitor=batch_monitor,
            )

    async def _execute_job(
        self,
        job_id: str,
        account_id: int,
        account_handle: str,
        started_at: float,
    ) -> JobResult:
        """Core job logic, separated so it can be wrapped in asyncio.wait_for.

        Steps:
            1. Create scrape_run.
            2. Fetch from both providers concurrently with retry.
            3. Reconcile.
            4. Atomic write.
        """
        # 1. Create scrape_run ------------------------------------------------
        run_id = await self.db.create_scrape_run(self.provider_id)

        # 2. Fetch from providers concurrently --------------------------------
        response_a, response_b = await asyncio.gather(
            call_provider_with_retry(
                self.provider_a.fetch_account_metrics, account_handle
            ),
            call_provider_with_retry(
                self.provider_b.fetch_account_metrics, account_handle
            ),
        )

        # 3. Reconcile --------------------------------------------------------
        recon = reconcile(response_a, response_b)

        # 4. Atomic write -----------------------------------------------------
        snapshots_written, new_posts = await self.db.write_batch(
            run_id=run_id,
            provider_id=self.provider_id,
            account_id=account_id,
            merged_posts=recon.merged_posts,
            reconciliation=recon,
        )

        return JobResult(
            job_id=job_id,
            account_id=account_id,
            account_handle=account_handle,
            outcome=JobOutcome.SUCCESS,
            scrape_run_id=run_id,
            provider_a_posts=len(response_a.posts),
            provider_b_posts=len(response_b.posts),
            reconciled_posts=len(recon.merged_posts),
            overlap_count=recon.overlap_count,
            only_in_a=len(recon.only_in_a),
            only_in_b=len(recon.only_in_b),
            snapshots_written=snapshots_written,
            new_posts_created=new_posts,
            started_at=started_at,
            finished_at=time.time(),
        )

    async def _finalize_failure(
        self,
        *,
        job_id: str,
        account_id: int,
        account_handle: str,
        started_at: float,
        scrape_run_id: int | None,
        error_msg: str,
        batch_monitor: BatchMonitor | None,
    ) -> JobResult:
        """Handle failure bookkeeping: mark scrape_run, track failures, alert."""
        # Mark the scrape_run as failed (if one was created).
        if scrape_run_id is not None:
            try:
                await self.db.mark_scrape_run(
                    scrape_run_id, "failed", error_message=error_msg
                )
            except Exception:
                logger.exception(
                    "Failed to mark scrape_run %d as failed", scrape_run_id
                )

        # Track consecutive failures.
        self.failure_tracker.record_failure(account_id, error_msg)

        # Notify batch monitor.
        if batch_monitor:
            batch_monitor.record_failure(error_msg)

        return JobResult(
            job_id=job_id,
            account_id=account_id,
            account_handle=account_handle,
            outcome=JobOutcome.FAILURE,
            scrape_run_id=scrape_run_id,
            started_at=started_at,
            finished_at=time.time(),
            error=error_msg,
        )


# -------------------------------------------------------------------------
# Stale job reaper
# -------------------------------------------------------------------------


async def reap_stale_jobs(
    pool: Any,
    timeout_minutes: int = 10,
) -> list[int]:
    """Find and kill scrape_runs that have been 'running' for too long.

    Any scrape_run with status='running' and started_at older than
    *timeout_minutes* is marked as 'failed' with an explanatory error.

    In a full system the corresponding queue message would also be made
    visible again for re-processing (requeue).

    Args:
        pool: asyncpg connection pool.
        timeout_minutes: Threshold beyond which a running job is considered
            stale.

    Returns:
        List of run_ids that were reaped.
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE scrape_runs
            SET status = 'failed',
                ended_at = now(),
                error_message = 'Reaped: exceeded ' || $1 || ' minute timeout'
            WHERE status = 'running'
              AND started_at < now() - ($1 || ' minutes')::interval
            RETURNING run_id
            """,
            str(timeout_minutes),
        )
        reaped = [r["run_id"] for r in rows]

    if reaped:
        logger.warning("Reaped %d stale scrape_runs: %s", len(reaped), reaped)

    return reaped


# -------------------------------------------------------------------------
# Batch runner
# -------------------------------------------------------------------------


async def run_batch(
    worker: Worker,
    accounts: list[tuple[int, str]],
    batch_id: str | None = None,
    alert_callback: Any | None = None,
) -> list[JobResult]:
    """Run a batch of scrape jobs with failure-rate monitoring.

    Jobs are executed sequentially to respect provider rate limits. If
    >20% of completed jobs fail, the batch is paused and remaining jobs
    are skipped.

    Args:
        worker: Configured Worker instance.
        accounts: List of (account_id, account_handle) tuples.
        batch_id: Optional identifier for the batch. Auto-generated if None.
        alert_callback: Optional callable(batch_id, message) invoked on
            batch pause.

    Returns:
        List of JobResult for every attempted job.
    """
    bid = batch_id or str(uuid.uuid4())
    monitor = BatchMonitor(batch_id=bid, total_jobs=len(accounts))
    if alert_callback:
        monitor.set_alert_callback(alert_callback)

    results: list[JobResult] = []

    for account_id, handle in accounts:
        if monitor.is_paused:
            logger.warning(
                "Batch %s paused — skipping remaining %d accounts",
                bid,
                len(accounts) - len(results),
            )
            break

        result = await worker.run_job(
            account_id=account_id,
            account_handle=handle,
            batch_monitor=monitor,
        )
        results.append(result)
        logger.info(
            "Job %s for %s: %s (%.1fs)",
            result.job_id,
            handle,
            result.outcome.value,
            result.duration_seconds,
        )

    logger.info(
        "Batch %s finished: %d/%d jobs completed, %d skipped, "
        "failure rate %.0f%%",
        bid,
        monitor.completed,
        len(accounts),
        len(accounts) - monitor.completed,
        monitor.failure_rate * 100,
    )

    return results
