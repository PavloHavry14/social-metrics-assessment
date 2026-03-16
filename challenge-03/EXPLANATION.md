# Challenge 03 — Queue Worker: Written Explanation

## 1. The Crash-Safe Write Pattern

The worker must insert metric snapshots into an append-only table where UPDATE and DELETE are prohibited by triggers. This constraint eliminates every standard idempotency technique:

**Plain INSERT** produces duplicates on retry. If the process crashes after inserting 40 of 100 snapshots, retrying inserts all 100 again — the first 40 are now doubled. Since `metric_snapshots` has no natural unique constraint on `(post_id, run_id)` by design (a post could theoretically appear in multiple snapshots per run in a different schema), the database cannot block these duplicates.

**UPSERT (INSERT ... ON CONFLICT DO UPDATE)** is blocked by the BEFORE UPDATE trigger `trg_no_update_snapshots`, which raises an exception on any UPDATE to `metric_snapshots`. Even if it worked, upsert semantics would overwrite the original observation — violating the append-only invariant.

**Skip existing (INSERT ... ON CONFLICT DO NOTHING)** silently drops rows. If the first attempt partially committed 40 rows and then crashed, the retry would skip those 40 and only insert the remaining 60. But if the provider returns slightly different data on retry (metrics changed between calls), we lose the original 40 observations entirely.

**Delete-and-reinsert** is blocked by the BEFORE DELETE trigger `trg_no_delete_snapshots`.

The solution is **atomic transaction batching** in `DatabaseGateway.write_batch()` (worker.py lines 163–276). All operations — ensuring posts exist, inserting every `metric_snapshot` row, writing the `scrape_run_accounts` summary, and marking the scrape run as completed — execute inside a single `async with conn.transaction()` block. If the process crashes at any point within this block, PostgreSQL rolls back the entire transaction. Zero rows are persisted. On retry, a fresh `scrape_run_id` is created and the entire batch is written cleanly. The `run_id` foreign key on each snapshot row tags every observation with its originating scrape run, making it trivial to audit which run produced which data.

## 2. Why the Scrape Run Is Created OUTSIDE the Main Transaction

In `_execute_job()` (worker.py line 411), the call `await self.db.create_scrape_run(self.provider_id)` executes as an independent statement using its own connection from the pool (`async with self._pool.acquire() as conn` in `create_scrape_run()`). This is deliberate: the scrape_run record must exist in the database even if the subsequent data transaction rolls back.

If `create_scrape_run` were inside the same transaction as `write_batch`, a crash would roll back both the data and the scrape_run record. The stale job reaper (`reap_stale_jobs()`, worker.py lines 500–538) works by querying `WHERE status='running' AND started_at < threshold`. If the scrape_run row was rolled back, the reaper would never find it — the crashed job would be invisible. No cleanup would occur, and the queue message might be retried indefinitely without any record of prior attempts.

By creating the scrape_run outside the data transaction, the reaper can always find running jobs that have exceeded the timeout. The `mark_scrape_run()` method (lines 138–161) similarly operates outside the main transaction, using its own connection, so that failure status is recorded even when `write_batch` has already rolled back.

## 3. Partial Provider Response Handling

In `call_provider_with_retry()` (retry_logic.py lines 104–126), when a provider response has `is_complete=False`, the response is discarded and the call is retried:

```python
if not response.is_complete:
    # ... logging ...
    last_error = ProviderTimeoutError(...)
    if attempt < max_retries:
        await asyncio.sleep(compute_backoff(attempt))
    continue
```

The `ProviderResponse.is_complete` flag (providers.py line 67) is set to `False` when the provider timed out mid-pagination — it returned some posts but not all of them.

**Why partial data is worse than no data:** The downstream reconciliation and disappeared-post detection (Query 3 in the schema) compares the set of posts in the latest scrape run against the previous run. If Provider A has 47 posts for an account but only returns 30 before timing out, those missing 17 posts would appear in `vw_disappeared_since_last_run` as disappeared posts. They are not actually gone — the provider just did not finish delivering them. These false disappearances would trigger incorrect `is_disappeared` flags, corrupt metric regression analysis, and potentially alert operators about a non-existent problem.

By discarding partial data and retrying, we ensure that every successful scrape run represents a complete snapshot of the account. If all retries fail, the entire job fails — the scrape_run is marked `'failed'` and no partial data reaches the database.

## 4. Exponential Backoff with Jitter

The `compute_backoff()` function (retry_logic.py lines 52–64) implements the formula:

```python
delay = BASE_DELAY * (2 ** attempt) + random.uniform(0, JITTER_MAX)
return min(delay, MAX_DELAY)
```

With `BASE_DELAY=1.0`, `JITTER_MAX=1.0`, and `MAX_DELAY=30.0`, the delays are:
- Attempt 0: 1*1 + jitter = 1.0–2.0s
- Attempt 1: 1*2 + jitter = 2.0–3.0s
- Attempt 2: 1*4 + jitter = 4.0–5.0s
- Attempt 3: 1*8 + jitter = 8.0–9.0s
- ...capped at 30.0s

**Why jitter is critical:** Without jitter, all workers that hit a provider rate limit (HTTP 429) at the same moment would retry at the same moment — 1s later, then 2s later, then 4s later. This is the thundering herd problem: the synchronized retries create a spike that triggers another 429, creating a feedback loop. The random jitter (`random.uniform(0, JITTER_MAX)`) desynchronizes the retries, spreading them across a 1-second window so the provider sees a gradual ramp instead of a spike.

For `RateLimitError` specifically (retry_logic.py lines 128–143), the retry logic respects the provider's `Retry-After` header when present (`exc.retry_after`), falling back to `compute_backoff()` only when the provider does not specify a wait time. This is important because the provider knows its own rate-limit window better than our formula can estimate.

## 5. Batch Monitoring and Account Flagging

**Batch monitoring (>20% threshold):** The `BatchMonitor` class (retry_logic.py lines 246–318) tracks successes and failures across a batch of scrape jobs. When `failure_rate` exceeds `BATCH_FAILURE_RATE_THRESHOLD` (0.20), the batch is paused by setting `_paused = True`. The `run_batch()` function (worker.py lines 546–608) checks `monitor.is_paused` before each job and breaks the loop if True, skipping remaining accounts.

The 20% threshold is evaluated only after a minimum sample size: `max(5, int(self.total_jobs * 0.10))` (line 304). This prevents false positives — if the first 2 of 200 jobs both fail, that is a 100% failure rate but only 2 data points. By requiring at least 5 completions (or 10% of total, whichever is larger), the monitor avoids halting a batch based on coincidental early failures.

The purpose of batch pausing is to catch systemic issues — a provider outage, a network partition, an expired API key — where continuing to process jobs would waste resources and accumulate failures. Isolated account-level problems (private account, deleted account) would not push the overall rate above 20% in a batch of hundreds.

**Account flagging (3 consecutive failures):** The `FailureTracker` class (retry_logic.py lines 168–238) maintains per-account `AccountFailureRecord` objects. Each failure increments `consecutive_failures`; each success resets it to zero. When the counter reaches `CONSECUTIVE_FAILURE_THRESHOLD` (3), `flagged_for_review` is set to `True`.

Once flagged, `Worker.run_job()` calls `self.failure_tracker.should_skip(account_id)` (worker.py line 332) at the top of every job execution. Flagged accounts are immediately skipped with a FAILURE result, preventing infinite retry loops. The flag persists even after a success (`record_success` resets `consecutive_failures` to 0 but does not clear `flagged_for_review`). An explicit `unflag()` call is required — this forces an operator to investigate why the account failed before re-enabling it.

Three consecutive failures (not three total, not three in a window) is the threshold because it distinguishes persistent account-level problems (account deleted, went private, banned) from transient errors (network blip, provider hiccup). A transient error would succeed on retry, resetting the counter. Three in a row without any success strongly suggests the problem is with the account itself.

## 6. Stale Job Detection

The `reap_stale_jobs()` function (worker.py lines 500–538) runs periodically (typically via cron or a scheduler) and executes:

```sql
UPDATE scrape_runs
SET status = 'failed', ended_at = now(),
    error_message = 'Reaped: exceeded N minute timeout'
WHERE status = 'running'
  AND started_at < now() - (N || ' minutes')::interval
RETURNING run_id
```

The default timeout is 10 minutes (`timeout_minutes=10`). This matches the `JOB_TIMEOUT_SECONDS = 600.0` constant used by `asyncio.wait_for()` in `run_job()` (worker.py line 352).

**Why this is necessary:** Application-level cleanup (the `except` blocks in `run_job`, the `_finalize_failure` method) only runs if the Python process is still alive. If the worker process is OOM-killed by the kernel, terminated by a container orchestrator, or the server loses power, no Python cleanup code executes. The scrape_run record remains in `status='running'` indefinitely. Without the reaper, these orphaned records would accumulate, and any monitoring that counts "running" jobs would report phantom work.

The reaper is safe because it operates on scrape_runs only — it never touches `metric_snapshots`. Since the data transaction in `write_batch()` either fully committed (status already set to 'completed' inside the transaction) or fully rolled back (no snapshot rows exist), the reaper's UPDATE from 'running' to 'failed' simply acknowledges what already happened: the job died without completing its data write.
