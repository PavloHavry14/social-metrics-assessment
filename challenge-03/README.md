# Challenge 03: Crash-Safe Queue Worker

Async queue worker that scrapes social media metrics from two providers,
reconciles results, and writes them atomically to PostgreSQL. Designed so
that a crash at any point leaves zero partial data in the database.

---

## 1. System Architecture

```
  +------------------+        +-------------------+
  | Job Queue        |        | Provider A (API)  |
  | (Redis/SQS/etc.) |        +-------------------+
  +--------+---------+                |
           |                          | async fetch
           | dequeue job              | with retry
           v                          |
  +--------+---------+        +-------+-----------+
  |                   |------->| Provider B (API)  |
  |     Worker        |        +-------------------+
  |                   |                |
  |  - run_job()      |<---------------+
  |  - _execute_job() |
  |  - _finalize_     |
  |    failure()      |
  +--------+----------+
           |
           | atomic write
           v
  +--------+----------+        +---------------------+
  |   PostgreSQL      |        | FailureTracker      |
  |                   |        | (per-account)        |
  |  - scrape_runs    |        +---------------------+
  |  - posts          |        | consecutive failures |
  |  - metric_        |        | flagged_for_review   |
  |    snapshots      |        +---------------------+
  |  - scrape_run_    |
  |    accounts       |        +---------------------+
  +-------------------+        | BatchMonitor        |
                               | (per-batch)          |
                               +---------------------+
                               | failure rate check   |
                               | pause if > 20%       |
                               +---------------------+
```

---

## 2. Job Lifecycle

```
  Queue delivers: (account_id, account_handle)
    |
    v
  +--------------------------------------------+
  | Check FailureTracker                        |
  |   should_skip(account_id)?                  |
  +------+-----------------------------+-------+
         |                             |
        YES                            NO
   (flagged for                        |
    manual review)                     v
         |                  +----------+-----------+
         v                  | Create scrape_run    |
   Return JobResult         | status = 'running'   |
   outcome = FAILURE        | (OUTSIDE transaction)|
   "skipping"               +----------+-----------+
                                       |
                                       v
                            +----------+-----------+
                            | Fetch providers      |
                            | concurrently         |
                            | (asyncio.gather)     |
                            |                      |
                            |  Provider A ---------+---> call_provider_with_retry()
                            |  Provider B ---------+---> call_provider_with_retry()
                            +----------+-----------+
                                       |
                                       v
                            +----------+-----------+
                            | Reconcile responses   |
                            | (merge overlapping    |
                            |  posts, take MAX)     |
                            +----------+-----------+
                                       |
                                       v
                            +----------+-----------+
                            | db.write_batch()     |
                            | (single atomic txn)  |
                            +----------+-----------+
                                       |
                            +----------+-----------+
                            |                      |
                         SUCCESS                EXCEPTION
                            |                      |
                            v                      v
                     +-----------+         +--------------+
                     | Record    |         | Transaction  |
                     | success   |         | rolls back   |
                     | in        |         | (zero rows)  |
                     | tracker   |         |              |
                     |           |         | Mark run as  |
                     | Return    |         | 'failed'     |
                     | JobResult |         |              |
                     | SUCCESS   |         | Track failure|
                     +-----------+         |              |
                                           | Return       |
                                           | JobResult    |
                                           | FAILURE      |
                                           +--------------+
```

---

## 3. Crash-Safe Write Pattern

```
  TIME
   |
   |  +------------------------------------------------------+
   |  | Step 1: INSERT scrape_run (status='running')          |
   |  |   -- Committed immediately in its own connection      |
   |  |   -- Survives any later crash                         |
   |  +------------------------------------------------------+
   |
   |  +------------------------------------------------------+
   |  | Step 2: Fetch from providers (network I/O)            |
   |  |   -- No database writes here                          |
   |  +------------------------------------------------------+
   |
   |  +------------------------------------------------------+
   |  | Step 3: Reconcile (pure computation)                  |
   |  |   -- No database writes here                          |
   |  +------------------------------------------------------+
   |
   |  +===================  BEGIN  ==========================+
   |  |                                                      |
   |  |  Step 4a: INSERT posts (ON CONFLICT DO NOTHING)      |
   |  |  Step 4b: INSERT metric_snapshots (all rows)         |
   |  |  Step 4c: INSERT scrape_run_accounts                 |
   |  |  Step 4d: UPDATE scrape_run status='completed'       |
   |  |                                                      |
   |  +===================  COMMIT  =========================+
   |
   v

  CRASH SCENARIOS:
  +-----------------------------------------------------------------+
  |                                                                 |
  |  Crash during Step 1:                                           |
  |    scrape_run may or may not exist.                             |
  |    If it exists: stale job reaper marks it 'failed' after 10m. |
  |    If it doesn't: nothing to clean up.                          |
  |                                                                 |
  |  Crash during Step 2 or 3:                                      |
  |    scrape_run exists with status='running'.                    |
  |    No data rows written. Nothing to roll back.                 |
  |    Reaper marks it 'failed' after 10 minutes.                  |
  |                                                                 |
  |  Crash during Step 4 (inside transaction):                      |
  |    PostgreSQL automatically rolls back the transaction.         |
  |    ZERO rows in posts, metric_snapshots, scrape_run_accounts.  |
  |    scrape_run remains status='running'.                        |
  |    Reaper marks it 'failed' after 10 minutes.                  |
  |                                                                 |
  |  Crash after COMMIT:                                            |
  |    All data persisted. Job is complete.                         |
  |    Retry would create a NEW scrape_run_id (no duplicates).     |
  |                                                                 |
  +-----------------------------------------------------------------+

  STALE JOB REAPER:
  +---------------------------------------------+
  | Periodic process (e.g. every 5 minutes):    |
  |                                             |
  |   UPDATE scrape_runs                        |
  |   SET status = 'failed',                    |
  |       ended_at = now(),                     |
  |       error_message = 'Reaped: exceeded     |
  |         10 minute timeout'                  |
  |   WHERE status = 'running'                  |
  |     AND started_at < now() - 10 minutes     |
  +---------------------------------------------+
```

---

## 4. Retry Decision Tree

```
  call_provider_with_retry(fetch_fn, *args)
    |
    |  attempt = 0
    v
  +--------------------------------------------+
  | Call fetch_fn(*args)                        |<-----------+
  +------+-----+--------+--------+------------+             |
         |     |        |        |                           |
      SUCCESS  |   RateLimit  Timeout/     Other             |
         |     |   Error     Unavailable   Exception         |
         |     |        |        |              |            |
         v     |        v        v              v            |
  +------+--+  |  +-----+--+ +--+-----+  +-----+------+    |
  | Check   |  |  | Has    | | Compute|  | Raise      |    |
  | complete|  |  | Retry- | | backoff|  | immediately|    |
  | flag    |  |  | After? | |        |  | (fatal)    |    |
  +--+---+--+  |  +--+--+-+ +--+-----+  +------------+    |
     |   |     |     |  |      |                            |
    YES  NO    |    YES  NO    |                            |
     |   |     |     |  |     |                            |
     v   v     |     v  v     v                            |
  Return |     |  Use  Use  delay = min(                   |
  response|    |  header backoff  base * 2^attempt         |
         |    |  delay         + jitter,                   |
         v    |     |     |    max_delay)                   |
  +------+--+ |     +-----+----+                           |
  | Partial  | |          |                                 |
  | data!    | |          v                                 |
  | DISCARD  | |    +-----+--------+                        |
  | (worse   | |    | attempt      |                        |
  | than no  | |    | < max_retries|                        |
  | data)    | |    +--+--------+--+                        |
  +----+-----+ |       |        |                           |
       |       |      YES       NO                          |
       v       |       |        |                           |
  Treat as     |       v        v                           |
  Timeout      |    sleep(   Raise last                     |
  Error        |    delay)   error                          |
       |       |       |                                    |
       +-------+-------+------>-----------------------------+
               |
               v
         attempt++

  Backoff schedule (base=1s, jitter up to 1s, max=30s):
    Attempt 0: 1s  + jitter  =  ~1-2s
    Attempt 1: 2s  + jitter  =  ~2-3s
    Attempt 2: 4s  + jitter  =  ~4-5s
    Attempt 3: 8s  + jitter  =  ~8-9s
    ...capped at 30s

  PARTIAL DATA POLICY:
  +-------------------------------------------------------+
  | A response with is_complete=False (e.g. 30 of 47      |
  | posts) is DISCARDED and treated as a timeout error.   |
  |                                                       |
  | Reason: partial data creates false "disappeared post" |
  | signals downstream. Better to fail cleanly and retry  |
  | than to persist incomplete data.                      |
  +-------------------------------------------------------+
```

---

## 5. Batch Monitoring Flow

```
  run_batch(worker, accounts, batch_id)
    |
    v
  Create BatchMonitor(total_jobs=len(accounts))
    |
    v
  For each (account_id, handle) in accounts:
    |
    +---> Is batch paused? ----YES----> Skip remaining accounts
    |                                   Log warning
    |         NO                        Break loop
    |          |
    v          v
    worker.run_job(account_id, handle, batch_monitor)
    |
    +---> job succeeded?
    |        |
    |       YES ----> batch_monitor.record_success()
    |        |          _succeeded += 1
    |        |
    |        NO -----> batch_monitor.record_failure(error)
    |                    _failed += 1
    |                    |
    |                    v
    |              +-----+--------------------------+
    |              | _check_threshold()             |
    |              |                                |
    |              | completed < min_sample?        |
    |              |   min_sample = max(5, 10%)     |
    |              |     |          |               |
    |              |    YES         NO              |
    |              |     |          |               |
    |              |   skip       failure_rate      |
    |              |   check      > 20%?            |
    |              |              |       |         |
    |              |             YES      NO        |
    |              |              |       |         |
    |              |              v       v         |
    |              |         PAUSE     continue     |
    |              |         batch                  |
    |              |           |                    |
    |              |           v                    |
    |              |     alert_callback(            |
    |              |       batch_id, message)       |
    |              +--------------------------------+
    |
    +---> Append JobResult to results[]
    |
    v (next account)

  ACCOUNT-LEVEL FAILURE TRACKING (FailureTracker):
  +-------------------------------------------------------+
  |                                                       |
  |  On failure:                                          |
  |    consecutive_failures += 1                          |
  |                                                       |
  |    if consecutive_failures >= 3:                      |
  |      flagged_for_review = True                        |
  |      Account SKIPPED on all future batches            |
  |      (until manual unflag)                            |
  |                                                       |
  |  On success:                                          |
  |    consecutive_failures = 0                           |
  |    (flagged_for_review stays True if set --           |
  |     requires manual unflag)                           |
  |                                                       |
  +-------------------------------------------------------+

  FAILURE ESCALATION PATH:
  +-------------------------------------------------------------+
  |                                                             |
  |  Single failure                                             |
  |    --> retry with backoff (up to 3 retries per provider)    |
  |                                                             |
  |  Job failure (all retries exhausted)                        |
  |    --> FailureTracker increments consecutive count           |
  |    --> BatchMonitor increments batch failure count           |
  |                                                             |
  |  3 consecutive job failures for same account                |
  |    --> Account flagged for manual review                    |
  |    --> Skipped in all future batches                        |
  |                                                             |
  |  > 20% of batch jobs failing                                |
  |    --> Entire batch paused                                  |
  |    --> Alert callback fired                                 |
  |    --> Remaining accounts skipped                           |
  |                                                             |
  +-------------------------------------------------------------+
```

---

## Key Configuration

| Constant                       | Value   | Purpose                              |
|--------------------------------|---------|--------------------------------------|
| `MAX_RETRIES`                  | 3       | Per-provider retry attempts          |
| `BASE_DELAY`                   | 1.0s    | Backoff base delay                   |
| `MAX_DELAY`                    | 30.0s   | Backoff ceiling                      |
| `JITTER_MAX`                   | 1.0s    | Random jitter range                  |
| `CONSECUTIVE_FAILURE_THRESHOLD`| 3       | Failures before flagging account     |
| `BATCH_FAILURE_RATE_THRESHOLD` | 20%     | Failure rate to pause batch          |
| `JOB_TIMEOUT_SECONDS`         | 600s    | Per-job timeout (10 minutes)         |
| Stale job reaper timeout       | 10 min  | Running jobs older than this reaped  |

## Source Files

| File             | Responsibility                               |
|------------------|----------------------------------------------|
| `worker.py`      | Worker, DatabaseGateway, batch runner, reaper |
| `retry_logic.py` | Backoff, retry wrapper, FailureTracker, BatchMonitor |
| `providers.py`   | Provider interfaces and data models          |
| `test_worker.py` | Test suite                                   |
