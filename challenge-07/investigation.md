# Challenge 07: The 1.3M View Gap Investigation

**Observed:** Dashboard shows 2.1M views. Manual platform check shows ~3.4M. Gap is 1.3M and growing weekly.

---

## Root Causes Ranked by Probability

### 1. (HIGH) Dashboard query "optimization" broke view aggregation

**Clue:** Developer "optimized" the dashboard views query *last week*. The gap is *growing*, meaning recent data exacerbates it.

**Why top-ranked:** This is the most recent change, directly touches the number being displayed, and "optimization" on aggregation queries often introduces subtle bugs — e.g., switching from best-known (high water mark) to latest snapshot, dropping a JOIN that handled reassigned accounts, or adding a `LIMIT` / pagination bug that silently truncates results.

**Confirm/Eliminate:**

```sql
-- Compare the optimized query's output against a known-correct baseline.
-- Run both and diff. The correct total uses high water marks with ownership attribution:
SELECT
    SUM(hwm.max_views) AS correct_total_views
FROM (
    SELECT post_id, MAX(views) AS max_views
    FROM metric_snapshots
    GROUP BY post_id
) hwm
JOIN posts p              ON p.post_id = hwm.post_id
JOIN account_ownership ao ON ao.account_id = p.account_id
                          AND ao.client_id = :client_id
                          AND p.published_at >= ao.valid_from
                          AND (p.published_at < ao.valid_to OR ao.valid_to IS NULL);
```

Compare this number against what the dashboard API endpoint returns. If they diverge, the query rewrite is the cause.

**Fix:** Revert the query change immediately. Require that any dashboard query modification includes a before/after comparison test against the materialized high-water-mark view. Add an integration test that asserts dashboard total equals the canonical aggregation.

---

### 2. (HIGH) Cron migration broke scrape scheduling — stale data for weeks

**Clue:** Cron server migrated 4 weeks ago. Gap has been *growing for weeks* — timeline matches. If the cron job didn't survive migration (wrong timezone, missing env vars, service not enabled), some or all accounts stopped refreshing.

**Why ranked #2:** A broken cron is silent — no errors if the job simply never fires. Weeks of missing scrapes means the DB has stale snapshots, and the dashboard underreports because it never saw the real growth.

**Confirm/Eliminate:**

```sql
-- Check scrape run frequency per account over the last 5 weeks.
-- Look for accounts with zero runs after the migration date.
SELECT
    a.platform_handle,
    DATE_TRUNC('week', sr.started_at) AS week,
    COUNT(sr.run_id) AS scrape_count
FROM scrape_runs sr
JOIN scrape_run_accounts sra ON sra.run_id = sr.run_id
JOIN accounts a              ON a.account_id = sra.account_id
WHERE sr.started_at >= now() - INTERVAL '5 weeks'
GROUP BY a.platform_handle, DATE_TRUNC('week', sr.started_at)
ORDER BY a.platform_handle, week;
```

If any accounts show zero scrapes after the migration date, the cron is broken or misconfigured for those accounts.

**Fix:** Verify crontab on the new instance (`crontab -l`). Ensure timezone, environment variables (`REDIS_URL`, `DATABASE_URL`), and BullMQ connection are correctly configured. Add a heartbeat monitor: if no scrape run completes within 8 hours, fire a PagerDuty alert.

---

### 3. (HIGH) Banned accounts excluded from scraping — 2 accounts with real views ignored

**Clue:** 2 accounts flagged "banned" 3 weeks ago, since unbanned on the platform. If the scraper skips accounts marked banned, their views are frozen at the 3-week-old value while the real platform counts keep climbing.

**Why ranked #3:** Two accounts could easily hold hundreds of thousands of views. Three weeks of growth on active accounts is a significant and *growing* gap — which matches the symptom.

**Confirm/Eliminate:**

```sql
-- Find the two banned accounts and check their latest snapshot age.
SELECT
    a.platform_handle,
    a.account_id,
    MAX(ms.scraped_at) AS last_scraped,
    now() - MAX(ms.scraped_at) AS staleness,
    MAX(ms.views) AS last_known_views
FROM accounts a
JOIN posts p              ON p.account_id = a.account_id
JOIN metric_snapshots ms  ON ms.post_id = p.post_id
WHERE a.account_id IN (
    -- Replace with actual banned account IDs, or:
    SELECT account_id FROM accounts WHERE account_id IN (:banned_account_ids)
)
GROUP BY a.platform_handle, a.account_id
ORDER BY staleness DESC;
```

If `last_scraped` is ~3 weeks ago, these accounts are frozen.

**Fix:** Remove the hard ban flag; replace with a `status` enum (`active`, `suspended`, `under_review`). The scraper should attempt all non-deleted accounts. If a scrape fails due to platform ban, log the error in `scrape_run_accounts.error_message` but retry next cycle. Add a reconciliation job that checks platform status weekly.

---

### 4. (MEDIUM) Reassigned accounts' historical posts attributed to wrong client

**Clue:** 3 accounts reassigned 6 weeks ago. If the schema lacks proper historical ownership tracking (SCD Type 2), posts published before reassignment may be attributed to the new client, or — worse — dropped from both clients' totals during the transition.

**Impact estimate:** 3 of 38 accounts is ~8% of the portfolio. If attribution is broken, their older posts' views vanish from the target client's total.

**Fix:** Implement SCD Type 2 ownership with `valid_from`/`valid_to` ranges. Attribution queries must join on the ownership period active at `post.published_at`, not the current owner.

---

### 5. (MEDIUM) Disappeared posts not using high water marks

If posts temporarily vanish from the platform API (common on TikTok during moderation review), and the dashboard query uses the *latest* snapshot instead of the *maximum*, those posts contribute zero views during the disappearance window.

**Fix:** Dashboard must always aggregate `MAX(views)` across all snapshots per post, not just the most recent observation.

---

### 6. (LOW-MEDIUM) Provider reconciliation discarding valid data

Two providers scrape results are "reconciled." If reconciliation picks the *lower* value when providers disagree (conservative approach), the DB systematically underreports. Platform shows the real number; we show the minimum.

**Fix:** Reconciliation should take `MAX` per metric across providers, not average or minimum. Log discrepancies for provider quality monitoring.

---

### 7. (LOW) Redis/BullMQ job queue backlog or silent failures

After the cron migration, if BullMQ workers lost their Redis connection or the queue name changed, jobs may be enqueued but never processed. The cron fires, pushes to Redis, but nothing consumes the jobs.

**Fix:** Monitor BullMQ queue depth. Alert if pending jobs exceed a threshold or if the oldest job is more than 1 hour old. Check `REDIS_URL` on the new cron server matches the workers' Redis instance.

---

## First Thing I'd Actually Run

```sql
-- Step 1: Check if the dashboard query matches the canonical total.
-- This takes 30 seconds and eliminates or confirms the #1 suspect.
SELECT SUM(max_views) AS canonical_total
FROM (
    SELECT post_id, MAX(views) AS max_views
    FROM metric_snapshots ms
    JOIN posts p ON p.post_id = ms.post_id
    JOIN account_ownership ao ON ao.account_id = p.account_id
        AND ao.client_id = :target_client_id
        AND p.published_at >= ao.valid_from
        AND (p.published_at < ao.valid_to OR ao.valid_to IS NULL)
    GROUP BY ms.post_id
) per_post;
```

If this returns ~3.4M, the data is fine and the dashboard query is broken — fix is a revert. If this returns ~2.1M, the data itself is stale, and the investigation moves to causes #2 and #3 (scrape freshness).

After that, I'd immediately run the scrape frequency query (cause #2) and the banned-account staleness query (cause #3) in parallel. Between these three queries, we will have isolated the gap's origin within 5 minutes.
