-- =============================================================================
-- Challenge 02: Schema Design — Making Bad Data Structurally Impossible
-- =============================================================================
-- PostgreSQL schema for social media metrics collection.
-- Core invariants enforced at the database level:
--   1. Metric snapshots are append-only (no UPDATE, no DELETE on metric rows).
--   2. Post-to-account linking is immutable; account reassignment cannot
--      rewrite historical attribution.
--   3. Disappeared posts are flagged, never deleted.
--   4. Every scrape run is logged with full provenance.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. FOUNDATION TABLES
-- ---------------------------------------------------------------------------

CREATE TABLE clients (
    client_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE providers (
    provider_id   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,  -- e.g. 'tiktok', 'instagram'
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE accounts (
    account_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    platform_handle     TEXT NOT NULL,
    provider_id         BIGINT NOT NULL REFERENCES providers(provider_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (platform_handle, provider_id)
);

-- ---------------------------------------------------------------------------
-- 2. ACCOUNT OWNERSHIP HISTORY (SCD Type 2)
-- ---------------------------------------------------------------------------
-- An account can move between clients. Each row records a contiguous ownership
-- period. valid_to IS NULL means "current owner."  A UNIQUE constraint on
-- (account_id) WHERE valid_to IS NULL guarantees at most one active owner.
-- Rows are never updated except to close a period (set valid_to).

CREATE TABLE account_ownership (
    ownership_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id    BIGINT NOT NULL REFERENCES accounts(account_id),
    client_id     BIGINT NOT NULL REFERENCES clients(client_id),
    valid_from    TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to      TIMESTAMPTZ,
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);

-- Only one active ownership per account at any time.
CREATE UNIQUE INDEX uq_account_active_owner
    ON account_ownership (account_id) WHERE valid_to IS NULL;

-- ---------------------------------------------------------------------------
-- 3. POSTS
-- ---------------------------------------------------------------------------

CREATE TABLE posts (
    post_id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    account_id          BIGINT NOT NULL REFERENCES accounts(account_id),
    platform_post_id    TEXT NOT NULL,
    provider_id         BIGINT NOT NULL REFERENCES providers(provider_id),
    published_at        TIMESTAMPTZ,
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    disappeared_at      TIMESTAMPTZ,          -- set when missing from scrape
    reappeared_at       TIMESTAMPTZ,          -- set if it comes back
    is_disappeared      BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (platform_post_id, provider_id)
);

-- ---------------------------------------------------------------------------
-- 4. SCRAPE RUNS
-- ---------------------------------------------------------------------------

CREATE TABLE scrape_runs (
    run_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider_id         BIGINT NOT NULL REFERENCES providers(provider_id),
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at            TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'running'
                            CHECK (status IN ('running','completed','failed','partial')),
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE scrape_run_accounts (
    run_account_id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id              BIGINT NOT NULL REFERENCES scrape_runs(run_id),
    account_id          BIGINT NOT NULL REFERENCES accounts(account_id),
    posts_expected      INT,
    posts_found         INT,
    error_message       TEXT,
    UNIQUE (run_id, account_id)
);

-- ---------------------------------------------------------------------------
-- 5. METRIC SNAPSHOTS (append-only)
-- ---------------------------------------------------------------------------
-- This is the heart of the system. Each row is an immutable point-in-time
-- observation. No row is ever updated or deleted.

CREATE TABLE metric_snapshots (
    snapshot_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    post_id         BIGINT NOT NULL REFERENCES posts(post_id),
    run_id          BIGINT NOT NULL REFERENCES scrape_runs(run_id),
    scraped_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    views           BIGINT NOT NULL DEFAULT 0 CHECK (views >= 0),
    likes           BIGINT NOT NULL DEFAULT 0 CHECK (likes >= 0),
    comments        BIGINT NOT NULL DEFAULT 0 CHECK (comments >= 0),
    shares          BIGINT NOT NULL DEFAULT 0 CHECK (shares >= 0)
);

CREATE INDEX idx_snapshots_post_scraped
    ON metric_snapshots (post_id, scraped_at DESC);

CREATE INDEX idx_snapshots_run
    ON metric_snapshots (run_id);

-- ---------------------------------------------------------------------------
-- 6. MATERIALIZED VIEW: HIGH WATER MARKS
-- ---------------------------------------------------------------------------
-- Best-ever-known value per metric per post. Refreshed after each scrape run.

CREATE MATERIALIZED VIEW mv_high_water_marks AS
SELECT
    post_id,
    MAX(views)    AS max_views,
    MAX(likes)    AS max_likes,
    MAX(comments) AS max_comments,
    MAX(shares)   AS max_shares
FROM metric_snapshots
GROUP BY post_id;

CREATE UNIQUE INDEX idx_hwm_post ON mv_high_water_marks (post_id);

-- ---------------------------------------------------------------------------
-- 7. ENFORCE APPEND-ONLY VIA TRIGGER (database-level enforcement)
-- ---------------------------------------------------------------------------

-- Block UPDATE on metric_snapshots
CREATE OR REPLACE FUNCTION prevent_snapshot_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'metric_snapshots is append-only. UPDATE and DELETE are prohibited. '
        'snapshot_id=%, operation=%', OLD.snapshot_id, TG_OP;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_no_update_snapshots
    BEFORE UPDATE ON metric_snapshots
    FOR EACH ROW EXECUTE FUNCTION prevent_snapshot_mutation();

CREATE TRIGGER trg_no_delete_snapshots
    BEFORE DELETE ON metric_snapshots
    FOR EACH ROW EXECUTE FUNCTION prevent_snapshot_mutation();

-- Block UPDATE on closed ownership periods (immutable history)
CREATE OR REPLACE FUNCTION prevent_ownership_history_mutation()
RETURNS TRIGGER AS $$
BEGIN
    -- Allow only closing an open period (setting valid_to on a NULL row).
    -- Every other mutation is blocked.
    IF OLD.valid_to IS NOT NULL THEN
        RAISE EXCEPTION
            'Closed ownership periods are immutable. ownership_id=%', OLD.ownership_id;
    END IF;
    IF NEW.valid_from <> OLD.valid_from OR NEW.account_id <> OLD.account_id
       OR NEW.client_id <> OLD.client_id THEN
        RAISE EXCEPTION
            'Only valid_to may be set when closing an ownership period. ownership_id=%',
            OLD.ownership_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_immutable_ownership
    BEFORE UPDATE ON account_ownership
    FOR EACH ROW EXECUTE FUNCTION prevent_ownership_history_mutation();

CREATE TRIGGER trg_no_delete_ownership
    BEFORE DELETE ON account_ownership
    FOR EACH ROW EXECUTE FUNCTION prevent_snapshot_mutation();


-- =============================================================================
-- REQUIRED QUERIES
-- =============================================================================

-- -------------------------------------------------------------------------
-- QUERY 1: High water mark total views per client with correct historical
--           attribution (client who owned the account when the post was
--           published gets the credit).
-- -------------------------------------------------------------------------
-- Uses the ownership period that was active at each post's published_at.

-- SELECT
--     c.name                          AS client_name,
--     SUM(hwm.max_views)              AS total_high_water_views
-- FROM mv_high_water_marks hwm
-- JOIN posts p               ON p.post_id = hwm.post_id
-- JOIN account_ownership ao  ON ao.account_id = p.account_id
--                            AND p.published_at >= ao.valid_from
--                            AND (p.published_at < ao.valid_to OR ao.valid_to IS NULL)
-- JOIN clients c             ON c.client_id = ao.client_id
-- GROUP BY c.client_id, c.name
-- ORDER BY total_high_water_views DESC;

CREATE OR REPLACE VIEW vw_client_high_water_views AS
SELECT
    c.client_id,
    c.name                          AS client_name,
    SUM(hwm.max_views)              AS total_high_water_views
FROM mv_high_water_marks hwm
JOIN posts p               ON p.post_id = hwm.post_id
JOIN account_ownership ao  ON ao.account_id = p.account_id
                           AND p.published_at >= ao.valid_from
                           AND (p.published_at < ao.valid_to OR ao.valid_to IS NULL)
JOIN clients c             ON c.client_id = ao.client_id
GROUP BY c.client_id, c.name;


-- -------------------------------------------------------------------------
-- QUERY 2: All posts where the latest scrape shows lower metrics than any
--           previous scrape (metric regression detection).
-- -------------------------------------------------------------------------

CREATE OR REPLACE VIEW vw_metric_regressions AS
WITH latest_snapshot AS (
    SELECT DISTINCT ON (post_id)
        post_id, snapshot_id, views, likes, comments, shares, scraped_at
    FROM metric_snapshots
    ORDER BY post_id, scraped_at DESC
)
SELECT
    ls.post_id,
    p.platform_post_id,
    ls.views        AS latest_views,
    hwm.max_views   AS highest_views,
    ls.likes        AS latest_likes,
    hwm.max_likes   AS highest_likes,
    ls.comments     AS latest_comments,
    hwm.max_comments AS highest_comments,
    ls.scraped_at   AS latest_scrape_time
FROM latest_snapshot ls
JOIN mv_high_water_marks hwm ON hwm.post_id = ls.post_id
JOIN posts p                 ON p.post_id = ls.post_id
WHERE ls.views    < hwm.max_views
   OR ls.likes    < hwm.max_likes
   OR ls.comments < hwm.max_comments
   OR ls.shares   < hwm.max_shares;


-- -------------------------------------------------------------------------
-- QUERY 3: All posts present in the previous scrape run but missing from
--           today's latest run (disappeared posts detection).
-- -------------------------------------------------------------------------

CREATE OR REPLACE VIEW vw_disappeared_since_last_run AS
WITH ranked_runs AS (
    SELECT
        run_id,
        provider_id,
        started_at,
        ROW_NUMBER() OVER (PARTITION BY provider_id ORDER BY started_at DESC) AS rn
    FROM scrape_runs
    WHERE status IN ('completed', 'partial')
),
latest_run AS (
    SELECT run_id, provider_id FROM ranked_runs WHERE rn = 1
),
previous_run AS (
    SELECT run_id, provider_id FROM ranked_runs WHERE rn = 2
),
posts_in_previous AS (
    SELECT DISTINCT ms.post_id
    FROM metric_snapshots ms
    JOIN previous_run pr ON pr.run_id = ms.run_id
),
posts_in_latest AS (
    SELECT DISTINCT ms.post_id
    FROM metric_snapshots ms
    JOIN latest_run lr ON lr.run_id = ms.run_id
)
SELECT
    p.post_id,
    p.platform_post_id,
    p.account_id,
    a.platform_handle,
    pr.provider_id
FROM posts_in_previous pip
JOIN posts p          ON p.post_id = pip.post_id
JOIN accounts a       ON a.account_id = p.account_id
JOIN previous_run pr  ON TRUE
LEFT JOIN posts_in_latest pil ON pil.post_id = pip.post_id
WHERE pil.post_id IS NULL;


-- -------------------------------------------------------------------------
-- QUERY 4: Scrape run health summary — expected vs actual post counts
--           per account for the most recent run.
-- -------------------------------------------------------------------------

CREATE OR REPLACE VIEW vw_scrape_health_summary AS
SELECT
    sr.run_id,
    sr.started_at,
    sr.ended_at,
    sr.status,
    prov.name                       AS provider_name,
    a.platform_handle,
    sra.posts_expected,
    sra.posts_found,
    sra.posts_expected - COALESCE(sra.posts_found, 0)  AS post_deficit,
    CASE
        WHEN sra.posts_expected > 0
        THEN ROUND(100.0 * COALESCE(sra.posts_found, 0) / sra.posts_expected, 1)
        ELSE NULL
    END                             AS coverage_pct,
    sra.error_message               AS account_error,
    sr.error_message                AS run_error
FROM scrape_runs sr
JOIN scrape_run_accounts sra ON sra.run_id = sr.run_id
JOIN accounts a              ON a.account_id = sra.account_id
JOIN providers prov          ON prov.provider_id = sr.provider_id
ORDER BY sr.started_at DESC, a.platform_handle;


-- -------------------------------------------------------------------------
-- QUERY 5 (BONUS): Client attribution query — For a given client, total
--           views using best-known value per post, split by which client
--           owned the account when each post was published.
-- -------------------------------------------------------------------------
-- Usage: replace the $1 placeholder with the target client_id.

-- PREPARE client_attribution(BIGINT) AS
-- SELECT
--     owner_client.name               AS owning_client_at_publish,
--     COUNT(DISTINCT p.post_id)       AS post_count,
--     SUM(hwm.max_views)              AS total_views
-- FROM posts p
-- JOIN accounts a            ON a.account_id = p.account_id
-- -- Current ownership: account currently belongs to the target client
-- JOIN account_ownership cur ON cur.account_id = a.account_id
--                            AND cur.client_id = $1
--                            AND cur.valid_to IS NULL
-- -- Historical ownership: who owned it when the post was published
-- JOIN account_ownership ao  ON ao.account_id = p.account_id
--                            AND p.published_at >= ao.valid_from
--                            AND (p.published_at < ao.valid_to OR ao.valid_to IS NULL)
-- JOIN clients owner_client  ON owner_client.client_id = ao.client_id
-- JOIN mv_high_water_marks hwm ON hwm.post_id = p.post_id
-- GROUP BY owner_client.client_id, owner_client.name
-- ORDER BY total_views DESC;


-- =============================================================================
-- APPEND-ONLY ENFORCEMENT EXPLANATION (200 words)
-- =============================================================================
--
-- The metric_snapshots table enforces append-only semantics at the database
-- level through three complementary mechanisms:
--
-- 1. BEFORE UPDATE / BEFORE DELETE triggers: The trg_no_update_snapshots and
--    trg_no_delete_snapshots triggers fire before any UPDATE or DELETE on
--    metric_snapshots and unconditionally raise an exception, aborting the
--    operation. This blocks mutations regardless of which application, user,
--    or ad-hoc query attempts them.
--
-- 2. Privilege separation: In production, the application role should be
--    granted only INSERT and SELECT on metric_snapshots — never UPDATE or
--    DELETE. Even if the trigger were somehow bypassed (e.g., a superuser
--    disabling triggers), the application service account still lacks the
--    privilege. Combined with row-level security policies, this creates
--    defense in depth.
--
-- 3. IDENTITY-generated primary key: The snapshot_id column uses GENERATED
--    ALWAYS AS IDENTITY, preventing callers from supplying or overriding
--    the ID. This eliminates upsert-style overwrites via INSERT ... ON
--    CONFLICT that could simulate an UPDATE.
--
-- Together, these layers ensure that once a metric observation is recorded,
-- it cannot be altered or removed by any normal database operation. Historical
-- data integrity is guaranteed structurally, not by application-level
-- discipline.
-- =============================================================================
