# Challenge 02: Schema Design -- Making Bad Data Structurally Impossible

PostgreSQL schema for social media metrics collection with append-only
enforcement, SCD Type 2 account ownership, and immutable metric history.

---

## 1. Entity Relationship Diagram

```
  +----------------+          +------------------+
  |    clients     |          |    providers     |
  +----------------+          +------------------+
  | client_id (PK) |          | provider_id (PK) |
  | name           |          | name (UNIQUE)    |
  | created_at     |          | created_at       |
  +-------+--------+          +--------+---------+
          |                            |
          | 1                          | 1
          |                            |
          | N                          | N
  +-------+----------------------------+---------+
  |            account_ownership                  |      +------------------+
  |              (SCD Type 2)                     |      |     accounts     |
  +--------------+--------------------------------+      +------------------+
  | ownership_id (PK)                             |      | account_id (PK)  |
  | account_id   (FK) ---------------------------+------>| platform_handle  |
  | client_id    (FK)                             |      | provider_id (FK) |
  | valid_from                                    |      | created_at       |
  | valid_to     (NULL = current)                 |      +--------+---------+
  +---------+----+--------------------------------+               |
            |    |                                                |
            |    | Partial UNIQUE index:                          | 1
            |    | (account_id) WHERE valid_to IS NULL            |
            |    | --> at most ONE active owner per account       |
            |                                                     | N
            |                                            +--------+---------+
            |                                            |      posts       |
            |                                            +------------------+
            |                                            | post_id (PK)     |
            |                                            | account_id (FK)  |
            |                                            | platform_post_id |
            |                                            | provider_id (FK) |
            |                                            | published_at     |
            |                                            | first_seen_at    |
            |                                            | disappeared_at   |
            |                                            | reappeared_at    |
            |                                            | is_disappeared   |
            |                                            +--------+---------+
            |                                                     |
            |                                                     | 1
            |                                                     |
            |                                                     | N
  +---------+------+                                     +--------+---------+
  |  scrape_runs   |                                     | metric_snapshots |
  +----------------+                                     | (APPEND-ONLY)    |
  | run_id (PK)    |                                     +------------------+
  | provider_id(FK)|     +----------------------+        | snapshot_id (PK) |
  | started_at     |     | scrape_run_accounts  |        | post_id (FK)     |
  | ended_at       |     +----------------------+        | run_id (FK)      |
  | status         |     | run_account_id (PK)  |        | scraped_at       |
  | error_message  |---->| run_id (FK)          |        | views   (>=0)    |
  | created_at     |     | account_id (FK)      |        | likes   (>=0)    |
  +-------+--------+     | posts_expected       |        | comments (>=0)   |
          |               | posts_found          |        | shares  (>=0)    |
          |               | error_message        |        +--------+---------+
          |               +----------------------+                 |
          |                                                        |
          +--------------------------------------------------------+
          |  (run_id FK from metric_snapshots to scrape_runs)
          |
          |                   +-------------------------+
          +------------------>| mv_high_water_marks     |
                              | (MATERIALIZED VIEW)     |
                              +-------------------------+
                              | post_id (UNIQUE INDEX)  |
                              | max_views               |
                              | max_likes               |
                              | max_comments            |
                              | max_shares              |
                              +-------------------------+
                              | SELECT post_id,         |
                              |   MAX(views),           |
                              |   MAX(likes), ...       |
                              | FROM metric_snapshots   |
                              | GROUP BY post_id        |
                              +-------------------------+
```

---

## 2. Append-Only Enforcement Layers

```
  +===================================================================+
  |                    APPEND-ONLY ENFORCEMENT                        |
  |                    (Defense in Depth)                              |
  +===================================================================+

  Layer 1: BEFORE TRIGGERS
  +---------------------------------------------------------+
  |                                                         |
  |  metric_snapshots table                                 |
  |                                                         |
  |  +---------------------------------------------------+  |
  |  | trg_no_update_snapshots                           |  |
  |  |   BEFORE UPDATE --> RAISE EXCEPTION               |  |
  |  |   "metric_snapshots is append-only"               |  |
  |  +---------------------------------------------------+  |
  |  | trg_no_delete_snapshots                           |  |
  |  |   BEFORE DELETE --> RAISE EXCEPTION               |  |
  |  |   "metric_snapshots is append-only"               |  |
  |  +---------------------------------------------------+  |
  |                                                         |
  |  account_ownership table                                |
  |                                                         |
  |  +---------------------------------------------------+  |
  |  | trg_immutable_ownership                           |  |
  |  |   BEFORE UPDATE -->                               |  |
  |  |     if OLD.valid_to IS NOT NULL: RAISE            |  |
  |  |     if changing valid_from/account/client: RAISE  |  |
  |  |     only setting valid_to on open period allowed  |  |
  |  +---------------------------------------------------+  |
  |  | trg_no_delete_ownership                           |  |
  |  |   BEFORE DELETE --> RAISE EXCEPTION               |  |
  |  +---------------------------------------------------+  |
  +---------------------------------------------------------+

  Layer 2: PRIVILEGE SEPARATION (production deployment)
  +---------------------------------------------------------+
  |  Application role granted ONLY:                         |
  |    - INSERT on metric_snapshots                         |
  |    - SELECT on metric_snapshots                         |
  |  Never granted:                                         |
  |    - UPDATE on metric_snapshots                         |
  |    - DELETE on metric_snapshots                         |
  +---------------------------------------------------------+

  Layer 3: IDENTITY-GENERATED PRIMARY KEY
  +---------------------------------------------------------+
  |  snapshot_id BIGINT GENERATED ALWAYS AS IDENTITY        |
  |                                                         |
  |  - Callers CANNOT supply or override the ID             |
  |  - Prevents INSERT ... ON CONFLICT upsert tricks        |
  |  - Eliminates silent overwrite via duplicate IDs        |
  +---------------------------------------------------------+

  Layer 4: CHECK CONSTRAINTS
  +---------------------------------------------------------+
  |  views    >= 0                                          |
  |  likes    >= 0                                          |
  |  comments >= 0                                          |
  |  shares   >= 0                                          |
  |                                                         |
  |  Prevents negative metric values at the database level  |
  +---------------------------------------------------------+
```

---

## 3. Data Flow: How a Scrape Run Flows Through the Tables

```
  Queue delivers job for account_handle="@creator1"
    |
    v
  +--------------------------------------------------+
  | 1. INSERT INTO scrape_runs                        |
  |      (provider_id, status='running')              |
  |    RETURNING run_id                               |
  |    -- Done OUTSIDE the main transaction           |
  +----------------------------+---------------------+
                               |
                               v
  +--------------------------------------------------+
  | 2. Fetch metrics from Provider A & Provider B     |
  |    (with retry + backoff)                         |
  +----------------------------+---------------------+
                               |
                               v
  +--------------------------------------------------+
  | 3. Reconcile provider responses                   |
  |    (merge posts, resolve conflicts)               |
  +----------------------------+---------------------+
                               |
                               v
  +=============  BEGIN TRANSACTION  =================+
  |                                                   |
  |  4a. For each reconciled post:                    |
  |      INSERT INTO posts (...) ON CONFLICT DO NOTHING|
  |      -- Creates post row if first time seen       |
  |                                                   |
  |  4b. INSERT INTO metric_snapshots                 |
  |      (post_id, run_id, views, likes, ...)         |
  |      -- One row per post per scrape               |
  |      -- APPEND-ONLY: never updated or deleted     |
  |                                                   |
  |  4c. INSERT INTO scrape_run_accounts              |
  |      (run_id, account_id, posts_expected,         |
  |       posts_found)                                |
  |                                                   |
  |  4d. UPDATE scrape_runs                           |
  |      SET status='completed', ended_at=now()       |
  |                                                   |
  +=============  COMMIT  ============================+
    |
    v
  +--------------------------------------------------+
  | 5. REFRESH MATERIALIZED VIEW mv_high_water_marks  |
  |    -- Recomputes MAX(views), MAX(likes), etc.     |
  +--------------------------------------------------+
```

---

## 4. Query Flow: How the 4 Required Views Compute Results

```
  +====================================================================+
  | VIEW 1: vw_client_high_water_views                                 |
  | "High water mark total views per client"                           |
  +====================================================================+
  |                                                                    |
  |  mv_high_water_marks ---+                                          |
  |    (max_views per post) |                                          |
  |                         +---> JOIN posts (get account_id)          |
  |                         |                                          |
  |                         +---> JOIN account_ownership               |
  |                         |       WHERE published_at IN [from, to)   |
  |                         |       (historical attribution)           |
  |                         |                                          |
  |                         +---> JOIN clients (get client name)       |
  |                         |                                          |
  |                         +---> GROUP BY client --> SUM(max_views)   |
  +====================================================================+

  +====================================================================+
  | VIEW 2: vw_metric_regressions                                      |
  | "Posts where latest scrape < previous high water mark"             |
  +====================================================================+
  |                                                                    |
  |  metric_snapshots                                                  |
  |    |                                                               |
  |    +--> CTE: latest_snapshot                                       |
  |    |      DISTINCT ON (post_id)                                    |
  |    |      ORDER BY scraped_at DESC                                 |
  |    |      (most recent observation per post)                       |
  |    |                                                               |
  |    +--> JOIN mv_high_water_marks                                   |
  |    |      (best-ever values)                                       |
  |    |                                                               |
  |    +--> WHERE latest.views < max_views                             |
  |              OR latest.likes < max_likes                           |
  |              OR latest.comments < max_comments                     |
  |              OR latest.shares < max_shares                         |
  +====================================================================+

  +====================================================================+
  | VIEW 3: vw_disappeared_since_last_run                              |
  | "Posts in previous run but missing from latest run"                |
  +====================================================================+
  |                                                                    |
  |  scrape_runs                                                       |
  |    |                                                               |
  |    +--> CTE: ranked_runs                                           |
  |    |      ROW_NUMBER() OVER (PARTITION BY provider_id              |
  |    |                         ORDER BY started_at DESC)             |
  |    |                                                               |
  |    +--> latest_run  (rn = 1)                                       |
  |    +--> previous_run (rn = 2)                                      |
  |    |                                                               |
  |    +--> posts_in_previous = snapshots linked to previous_run       |
  |    +--> posts_in_latest   = snapshots linked to latest_run         |
  |    |                                                               |
  |    +--> LEFT JOIN: previous - latest                               |
  |         WHERE posts_in_latest.post_id IS NULL                      |
  |         (present before, absent now)                               |
  +====================================================================+

  +====================================================================+
  | VIEW 4: vw_scrape_health_summary                                   |
  | "Expected vs actual post counts per account per run"               |
  +====================================================================+
  |                                                                    |
  |  scrape_runs                                                       |
  |    |                                                               |
  |    +--> JOIN scrape_run_accounts                                   |
  |    |      (posts_expected, posts_found per account)                |
  |    |                                                               |
  |    +--> JOIN accounts (platform_handle)                            |
  |    +--> JOIN providers (provider name)                             |
  |    |                                                               |
  |    +--> Computed columns:                                          |
  |           post_deficit  = expected - found                         |
  |           coverage_pct  = 100 * found / expected                   |
  |                                                                    |
  |    +--> ORDER BY started_at DESC, platform_handle                  |
  +====================================================================+
```

---

## Key Constraints Summary

| Constraint                         | Mechanism                          | Purpose                              |
|------------------------------------|------------------------------------|--------------------------------------|
| No UPDATE on metric_snapshots      | BEFORE UPDATE trigger              | Append-only metric history           |
| No DELETE on metric_snapshots      | BEFORE DELETE trigger              | Append-only metric history           |
| One active owner per account       | Partial UNIQUE index (WHERE NULL)  | SCD Type 2 integrity                 |
| Closed ownership is immutable      | BEFORE UPDATE trigger              | Historical attribution preserved     |
| Metrics >= 0                       | CHECK constraints                  | No negative metric values            |
| No ID override                     | GENERATED ALWAYS AS IDENTITY       | Prevents upsert-style overwrites     |
| One snapshot per post per run      | post_id + run_id relationship      | Scrape provenance tracking           |

## Source Files

| File             | Responsibility                             |
|------------------|--------------------------------------------|
| `schema.sql`     | Full DDL, triggers, views, materialized view |
| `test_schema.py` | Test suite for schema constraints           |
