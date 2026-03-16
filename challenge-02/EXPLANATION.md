# Challenge 02 — Schema Design: Written Explanation

## 1. Why Append-Only via Triggers Rather Than Just Application-Level Discipline

The schema enforces append-only semantics on `metric_snapshots` through three independent layers, each covering a gap the others leave open:

**Layer 1: BEFORE UPDATE / BEFORE DELETE triggers.** `trg_no_update_snapshots` and `trg_no_delete_snapshots` both call `prevent_snapshot_mutation()`, which unconditionally raises an exception including the `snapshot_id` and `TG_OP` for auditability. This catches any SQL that reaches the table — application code, ad-hoc queries from a developer's psql session, an ORM that generates unexpected UPDATE statements, a migration script that accidentally targets the wrong table. The trigger fires regardless of who issues the statement.

**Layer 2: Privilege separation.** The comment block at lines 378–381 prescribes granting the application role only INSERT and SELECT, never UPDATE or DELETE. This matters because a superuser (or anyone with `ALTER TABLE` privileges) can run `ALTER TABLE metric_snapshots DISABLE TRIGGER trg_no_update_snapshots`. If that happens, the privilege restriction on the application service account still blocks mutations. The trigger alone is insufficient because it can be disabled; privileges alone are insufficient because they don't protect against a DBA running an ad-hoc UPDATE while connected as a privileged role.

**Layer 3: GENERATED ALWAYS AS IDENTITY keys.** The `snapshot_id` column uses `BIGINT GENERATED ALWAYS AS IDENTITY`, which prevents callers from supplying their own ID value. This eliminates a subtle attack vector: without this constraint, a caller could use `INSERT ... ON CONFLICT (snapshot_id) DO UPDATE` to simulate an UPDATE through an upsert. With `GENERATED ALWAYS`, the caller cannot control the primary key, so they cannot target a specific existing row via an ON CONFLICT clause.

No single layer is sufficient. Triggers can be disabled. Privileges can be misconfigured or bypassed by elevated roles. Identity columns only block one specific vector (upsert-as-update). Together, they make mutation structurally impossible under normal operations.

## 2. SCD Type 2 Ownership Design

The `account_ownership` table implements Slowly Changing Dimension Type 2: each row records a `(account_id, client_id, valid_from, valid_to)` period. The active owner has `valid_to IS NULL`.

**Why not just store the current owner on the accounts table?** Because historical attribution would be impossible. When a post is published under Client A and the account later moves to Client B, Query 1 needs to credit those views to Client A — the client who owned the account at `published_at`. A single `current_owner_id` column on `accounts` would always point to Client B, retroactively misattributing every historical post.

**Why not a separate history table alongside a current-owner column?** That creates two sources of truth that can diverge. If application code updates the `current_owner_id` but fails before inserting the history row (or vice versa), the data is inconsistent. The SCD Type 2 pattern uses a single table where the current state is derived from the data itself (`WHERE valid_to IS NULL`), not from a redundant denormalized column.

**The partial unique index `uq_account_active_owner` on `(account_id) WHERE valid_to IS NULL`** guarantees exactly one active owner per account at the database level. During reassignment, the application closes the old period (`UPDATE account_ownership SET valid_to = now() WHERE account_id = $1 AND valid_to IS NULL`) and inserts a new row. The trigger `trg_immutable_ownership` permits this specific mutation — setting `valid_to` on an open row — but blocks any change to `valid_from`, `account_id`, or `client_id` on any row, and blocks all changes to already-closed rows (`OLD.valid_to IS NOT NULL` raises an exception). This makes history genuinely immutable while allowing the controlled close-and-reopen transition.

## 3. High Water Mark via Materialized View

`mv_high_water_marks` precomputes `MAX(views)`, `MAX(likes)`, `MAX(comments)`, and `MAX(shares)` per `post_id` across all snapshots. This is a materialized view rather than a regular view or inline subquery for one reason: the `metric_snapshots` table is append-only and grows monotonically. Computing `MAX()` across every snapshot for every post on every query is an O(snapshots) scan. For a system scraping thousands of accounts multiple times daily, this table reaches tens of millions of rows within months.

The materialized view is refreshed with `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_high_water_marks` after each scrape run completes. The `CONCURRENTLY` option (enabled by the unique index `idx_hwm_post` on `post_id`) allows reads during refresh. The tradeoff is staleness: between a scrape run completing and the refresh finishing, the high water marks are slightly behind. This is acceptable because the high water marks are used for regression detection (Query 2) and attribution reporting (Query 1) — both are analytical queries that tolerate seconds-old data. The alternative (a regular view recomputing on every SELECT) would make Query 1 and Query 2 unacceptably slow as data accumulates.

## 4. Disappeared Posts: Flag, Don't Delete

The `posts` table has three columns for disappearance tracking: `is_disappeared` (boolean, default FALSE), `disappeared_at` (timestamptz, nullable), and `reappeared_at` (timestamptz, nullable).

**Why not DELETE?** Deleting a post row would cascade-delete or orphan its `metric_snapshots` rows, destroying the historical record. The entire point of an append-only metrics system is that observations are permanent. If a TikTok post is taken down by the creator, the metrics we already collected are still valid historical data — they represent what the post achieved while it was live.

**Why `reappeared_at`?** Posts can come back. A creator might unpublish a post, then re-publish it hours later. A platform might temporarily hide content during a review and then restore it. Without `reappeared_at`, a post that disappeared and reappeared would be stuck with `is_disappeared = TRUE` forever (since we never delete or overwrite), or we would need to NULL out `disappeared_at` (a mutation that the append-only philosophy discourages). With `reappeared_at`, the full lifecycle is captured: first seen, disappeared, came back.

**Why this matters for metric integrity:** Query 3 (`vw_disappeared_since_last_run`) identifies posts present in the previous scrape run but missing from the latest run. If disappeared posts were deleted, this diff would be impossible — we would have no record that the post ever existed. The flag-based approach preserves the post row so that the LEFT JOIN / WHERE NULL anti-join pattern in Query 3 can detect the absence.

## 5. The Four Required Queries

### Query 1: `vw_client_high_water_views` — Client Attribution by Publish-Time Ownership

This view joins `mv_high_water_marks` to `posts` to `account_ownership` to `clients`. The critical join condition is `p.published_at >= ao.valid_from AND (p.published_at < ao.valid_to OR ao.valid_to IS NULL)`. This matches each post to the ownership period that was active when the post was published, not the current owner. If Client A owned an account when a viral post was published and Client B owns it now, Client A gets the credit. The `OR ao.valid_to IS NULL` handles the current ownership period (no closing date yet). Grouping by `client_id` with `SUM(hwm.max_views)` gives each client their total best-known views across all posts published during their ownership.

### Query 2: `vw_metric_regressions` — Metric Regression Detection

The CTE `latest_snapshot` uses `DISTINCT ON (post_id) ... ORDER BY post_id, scraped_at DESC` to get the single most recent snapshot per post. This is then joined to `mv_high_water_marks` and filtered with `WHERE ls.views < hwm.max_views OR ls.likes < hwm.max_likes ...`. A regression means the latest observed value is lower than the historical maximum — which can happen when a platform recounts engagement, removes bot likes, or the post is partially hidden. The view surfaces these for investigation without requiring a full scan of all snapshot pairs.

### Query 3: `vw_disappeared_since_last_run` — Disappeared Post Detection

This uses `ROW_NUMBER() OVER (PARTITION BY provider_id ORDER BY started_at DESC)` to identify the two most recent completed/partial scrape runs per provider. It then builds two sets: `posts_in_previous` (posts with snapshots in run N-1) and `posts_in_latest` (posts with snapshots in run N). The anti-join `LEFT JOIN posts_in_latest ... WHERE pil.post_id IS NULL` finds posts that were scraped last time but are missing now. Partitioning by `provider_id` is important — each provider has its own scrape cadence, so "latest" and "previous" are per-provider concepts.

### Query 4: `vw_scrape_health_summary` — Scrape Coverage Percentage

This joins `scrape_runs` to `scrape_run_accounts` to compute per-account diagnostics for each run. The key calculation is `coverage_pct`: `ROUND(100.0 * COALESCE(sra.posts_found, 0) / sra.posts_expected, 1)` with a `CASE` guard for division by zero. The `post_deficit` column (`posts_expected - posts_found`) gives an immediate count of missing posts. The view is ordered by `started_at DESC` so the most recent runs appear first, making it a natural dashboard query. Both run-level and account-level error messages are surfaced so operators can distinguish between a provider outage (run error) and a single-account problem (account error).
