# Challenge 07 — The 1.3M View Gap: Written Explanation

## 1. Methodology: Presentation Layer First, Then Data Layer

The very first thing to run is a single SQL query that computes the canonical total — the correct view count derived directly from `metric_snapshots` with proper ownership attribution:

```sql
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

Compare this number against the dashboard API response (2.1M). This single comparison, which takes about 30 seconds, immediately tells you which half of the system is broken:

- If `canonical_total` is approximately 3.4M: the underlying data is correct, the dashboard query is wrong. The fix is a revert of the query change. Investigation over.
- If `canonical_total` is approximately 2.1M: the data itself is stale or incomplete. The dashboard is faithfully reporting bad data. Investigation moves to scraping and ingestion (causes #2, #3).

This is not a checklist approach. It is a binary partition of the problem space. Every minute spent investigating the wrong layer is wasted.

## 2. Why #1 (Dashboard Query Optimization) Is Top-Ranked

The developer "optimized" the dashboard views query last week. This is the most recent change, and it directly modifies the exact number being displayed. In any incident investigation, the most recent change to the affected code path is the top suspect until eliminated.

"Optimization" on aggregation queries is the single most common source of silent data bugs. The word "optimization" usually means one of:

- **Switching from `MAX(views)` to the latest snapshot value.** If the query now takes the most recent `metric_snapshots` row per post instead of the maximum across all snapshots, any post whose views temporarily dipped (platform moderation, API inconsistency) will underreport.
- **Adding `DISTINCT` that deduplicates legitimate rows.** If a post has multiple snapshots and the optimization adds a `DISTINCT` on `post_id` without an explicit `MAX`, it picks an arbitrary row.
- **Changing JOIN order or type.** Switching from a LEFT JOIN to an INNER JOIN on `account_ownership` would silently drop posts from accounts that were reassigned and have a `valid_to` set.
- **Adding WHERE clauses that filter edge cases.** An "optimization" that filters out posts with zero views, or posts older than some date, will silently shrink the total.

The "growing" gap is the signature: each new scrape that captures a view count lower than the historical peak widens the discrepancy under a broken query. Under a correct `MAX(views)` query, this would not happen because the historical peak is preserved. Under a broken "latest value" query, every dip accumulates.

## 3. Why #2 (Cron Migration) Is Ranked Second, Not First

The cron server was migrated 4 weeks ago. The gap has been growing for weeks. The timeline matches perfectly, and cron failures are silent — if the job never fires on the new server (wrong timezone, missing environment variables, systemd service not enabled), there are no errors to alert on. The job simply does not run.

But a completely broken cron would likely produce a much larger gap. If all 38 accounts stopped being scraped 4 weeks ago, the gap would be enormous — not 1.3M out of 3.4M. This suggests one of two scenarios:

1. **The cron partially works.** Some accounts refresh, others do not. Perhaps the migration broke the BullMQ connection for a subset of workers, or the new cron server's timezone offset causes it to fire during a window when some platform APIs rate-limit.
2. **The cron is a contributing factor alongside another cause.** The 1.3M gap may be the sum of stale data (from cron) and a query bug (from the optimization). Multiple causes can compound.

The diagnostic query in the investigation checks scrape frequency per account per week over the last 5 weeks:

```sql
SELECT a.platform_handle, DATE_TRUNC('week', sr.started_at) AS week,
       COUNT(sr.run_id) AS scrape_count
FROM scrape_runs sr
JOIN scrape_run_accounts sra ON sra.run_id = sr.run_id
JOIN accounts a ON a.account_id = sra.account_id
WHERE sr.started_at >= now() - INTERVAL '5 weeks'
GROUP BY a.platform_handle, DATE_TRUNC('week', sr.started_at)
ORDER BY a.platform_handle, week;
```

This immediately reveals which accounts (if any) went dark after the migration date. If all 38 accounts show consistent weekly scrape counts across the migration boundary, the cron is eliminated as a cause. If some accounts drop to zero post-migration, those accounts' stale data can be quantified and compared to the 1.3M gap.

## 4. Why #3 (Banned Accounts) Is Plausible

Two accounts were flagged "banned" 3 weeks ago but have since been unbanned on the platform. If the scraper skips accounts marked banned in the database (a common pattern: `WHERE NOT banned`), those accounts' metrics are frozen at their 3-week-old values while the real platform counts keep climbing.

2 out of 38 accounts is roughly 5% of the portfolio. Three weeks of frozen metrics on active accounts — accounts that are presumably still generating views — could easily account for hundreds of thousands of views. The gap is growing weekly because the real view counts keep rising while the database values stay static.

The diagnostic is straightforward: check the `last_scraped` timestamp for posts belonging to those two accounts. If `last_scraped` is approximately 3 weeks old, the accounts are frozen. The total views from those accounts' posts (difference between current platform count and the frozen database value) can be directly compared to the 1.3M gap to determine how much of it this cause explains.

The structural fix is important: replace the hard `banned` boolean with a `status` enum (`active`, `suspended`, `under_review`). The scraper should attempt all non-deleted accounts. If a scrape fails because the platform actually banned the account, log the error in `scrape_run_accounts.error_message` and retry next cycle. This way, when a platform unbans an account, scraping resumes automatically without manual database intervention.

## 5. Why Causes #4–#7 Are Ranked Lower

**#4 (Account reassignment / broken attribution):** 3 accounts were reassigned 6 weeks ago. If the schema lacks SCD Type 2 ownership tracking (the `account_ownership` table with `valid_from`/`valid_to`), posts published before reassignment may be attributed to the wrong client, or dropped entirely during the transition. This is ranked medium because the `account_ownership` table with temporal ranges already exists in the canonical query — the question is whether the dashboard's "optimized" query still uses it correctly. If the optimization dropped the ownership join conditions, this cause collapses into cause #1.

**#5 (No high water marks for disappeared posts):** If posts temporarily vanish from the platform API (common on TikTok during moderation review) and the dashboard uses the latest snapshot instead of `MAX(views)`, those posts contribute zero during the disappearance window. This is ranked medium because it would produce a smaller, more erratic gap — posts appear and disappear unpredictably, so the gap would fluctuate rather than grow steadily. A steadily growing gap points to a systematic cause, not intermittent post visibility issues.

**#6 (Reconciliation picking MIN across providers):** If two scraping providers disagree on a view count and reconciliation takes the minimum, the database systematically underreports. This is a real concern but would produce a consistent percentage undercount, not a gap that grows over time. The gap grows because the error compounds with each new scrape cycle — which is more consistent with stale data or a broken query than with a systematic per-scrape discount.

**#7 (Redis/BullMQ backlog):** If BullMQ workers lost their Redis connection after the cron migration, jobs would be enqueued but never processed. This is ranked lowest because it would manifest as completely missing scrapes — the same symptom as a broken cron. The scrape frequency query from cause #2 would catch this simultaneously. If scrapes are firing according to cron logs but `scrape_runs` shows no completions, that points to a queue/worker disconnect rather than a cron failure. Either way, cause #2's diagnostic covers it.

## 6. The Investigation as a Decision Tree, Not a Checklist

The investigation is structured so that each query's result determines the next step. This is how real debugging works — you eliminate hypotheses, you do not enumerate them sequentially.

**Branch 1:** Run the canonical total query.
- If `canonical_total` is approximately 3.4M → the data is fine, the dashboard query is broken. Revert the query optimization, add an integration test, done.
- If `canonical_total` is approximately 2.1M → the data itself is incomplete. Proceed to Branch 2.

**Branch 2:** Run the scrape frequency query (cause #2) and the banned-account staleness query (cause #3) in parallel. These are independent diagnostics.
- If scrape frequency drops to zero for some accounts after the migration → fix the cron, backfill the missing scrapes.
- If banned accounts have `last_scraped` approximately 3 weeks old → unban them in the database, trigger immediate scrape, reconsider the banned-account exclusion logic.
- If both are clean → the data should be approximately 3.4M but it is not. Re-examine the canonical query itself — maybe the `account_ownership` table has incorrect `valid_from`/`valid_to` ranges from the reassignment (cause #4).

**Branch 3:** If the gap is partially explained but a residual remains, the causes are compounding. Quantify each contributing factor: stale accounts contribute X views, banned accounts contribute Y views, X + Y + dashboard total should approximately equal the platform total.

This tree structure means you reach the answer in 3-5 queries and under 10 minutes, regardless of which cause is the actual root. A checklist approach would run all 7 diagnostics sequentially, wasting time on causes already eliminated by earlier results.

## 7. Permanent Fixes Beyond the Immediate Bug

Each root cause gets a structural fix, not just a hotfix. The goal is to make each class of bug impossible to recur silently.

**Dashboard query (cause #1):** Beyond reverting the query, add an integration test that asserts the dashboard endpoint's total equals the canonical aggregation computed directly from `metric_snapshots`. Run this test on every deployment. The canonical query is the source of truth; any dashboard query that diverges from it is, by definition, wrong.

**Cron scheduling (cause #2):** Beyond fixing the crontab, add a heartbeat monitor. If no `scrape_run` completes within 8 hours, fire a PagerDuty alert. Cron failures are silent by nature — the only way to detect them is to monitor for the absence of expected events, not the presence of errors.

**Banned accounts (cause #3):** Beyond unbanning the two accounts, replace the hard `banned` flag with a status enum. The scraper should attempt all non-deleted accounts and handle platform-side bans as transient errors, not permanent exclusions. Add a weekly reconciliation job that checks each account's actual platform status against the database flag.

**Account reassignment (cause #4):** Ensure the `account_ownership` table uses SCD Type 2 with `valid_from`/`valid_to` ranges, and that every query that touches account-level data joins on the ownership period active at the relevant timestamp (`post.published_at`), not the current owner.

**High water marks (cause #5):** The canonical aggregation must always use `MAX(views)` across all snapshots per post. Document this as a non-negotiable rule. Any query that uses "latest" instead of "maximum" is a bug.

The common thread: each fix includes a monitoring or testing mechanism that would catch the bug automatically if it were reintroduced. Fixing the immediate gap is a 10-minute task. Preventing the next gap is the real work.
