# Challenge 07: The 1.3M View Gap Investigation

**Observed:** Dashboard shows 2.1M views. Manual platform check shows ~3.4M.
Gap of 1.3M views, growing weekly.

**Portfolio:** 38 social media accounts managed for a client.

---

## 1. Investigation Decision Tree

```
        Dashboard shows 2.1M, platform shows 3.4M
        Gap = 1.3M and GROWING weekly
                        |
                        v
        +-------------------------------+
        | Gather clues from operations  |
        +-------------------------------+
                        |
        +---------------+---------------+---------------+
        |               |               |               |
        v               v               v               v
  "query optimized" "cron migrated" "2 accounts    "3 accounts
   last week"        4 weeks ago"    were banned"   reassigned
                                                     6 weeks ago"
        |               |               |               |
        v               v               v               v
   Suspect #1       Suspect #2      Suspect #3      Suspect #4
   Dashboard        Broken          Frozen          Attribution
   query bug        scheduler       ban data        bug
        |               |               |               |
        v               v               v               v
  Run canonical    Check scrape     Check last      Check ownership
  total query      frequency        scraped_at      SCD Type 2
  vs dashboard     per account      for banned      join logic
                   per week         accounts
        |               |               |               |
        v               v               v               v
  Match 3.4M?      Zero scrapes     Stale by        Posts missing
  YES->revert      post-migration?  3 weeks?        from totals?
  NO ->data layer  YES->fix cron    YES->unfreeze   YES->fix joins
```

---

## 2. System Architecture Under Investigation

```
+------------------+
|    Cron Server   |  <-- migrated 4 weeks ago
| (schedule-based) |
+--------+---------+
         |
         | enqueue scrape jobs
         v
+------------------+
| Redis / BullMQ   |  <-- job queue
| (message broker) |
+--------+---------+
         |
         | dequeue + process
         v
+------------------+
|    Workers       |  <-- scrape social platform APIs
| (scrape engines) |
+--------+---------+
         |
         | INSERT metric_snapshots
         v
+------------------+
|   PostgreSQL     |
|                  |
|  +------------+  |       +---------------------+
|  | accounts   |  |       |    REST API          |
|  +------------+  |       | (query optimized     |
|  | posts      |  |       |  last week)          |
|  +------------+  |  <--  +----------+----------+
|  | metric_    |  |                  |
|  | snapshots  |  |                  v
|  +------------+  |       +---------------------+
|  | account_   |  |       |  React Dashboard    |
|  | ownership  |  |       |  (shows 2.1M)       |
|  +------------+  |       +---------------------+
|  | scrape_runs|  |
|  +------------+  |
+------------------+

         vs.

+---------------------+
| Platform (TikTok)   |
| Manual check: ~3.4M |
+---------------------+
```

---

## 3. Root Cause Probability Ranking

```
Probability    Suspect
    |
    |  #1 [==============================] HIGH
    |      Dashboard query "optimization" broke aggregation
    |      Clue: optimized LAST WEEK, gap is GROWING
    |
    |  #2 [============================  ] HIGH
    |      Cron migration broke scrape scheduling
    |      Clue: migrated 4 WEEKS AGO, timeline matches
    |
    |  #3 [==========================    ] HIGH
    |      Banned accounts frozen -- not scraping 2 accounts
    |      Clue: banned 3 weeks ago, since UNBANNED
    |
    |  #4 [==================            ] MEDIUM
    |      Reassigned accounts -- attribution bug
    |      Clue: 3 accounts reassigned 6 weeks ago
    |
    |  #5 [===============               ] MEDIUM
    |      Disappeared posts -- not using high water marks
    |      Uses latest snapshot instead of MAX(views)
    |
    |  #6 [==========                    ] LOW-MEDIUM
    |      Provider reconciliation picks MIN not MAX
    |      Systematic undercount when providers disagree
    |
    |  #7 [=======                       ] LOW
    |      Redis/BullMQ queue backlog or silent failures
    |      Jobs enqueued but never consumed
    |
    +----+----+----+----+----+----+----+----+
         0%       25%       50%       75%
```

---

## 4. Diagnostic Flow

The first query determines whether the problem is in the data layer or
the presentation layer. This branches the entire investigation.

```
+---------------------------------------------------+
| STEP 1: Run canonical total query                 |
|                                                   |
|   SELECT SUM(max_views) FROM (                    |
|     SELECT post_id, MAX(views) AS max_views       |
|     FROM metric_snapshots ms                      |
|     JOIN posts p ON p.post_id = ms.post_id        |
|     JOIN account_ownership ao                     |
|       ON ao.account_id = p.account_id             |
|       AND ao.client_id = :target_client_id        |
|       AND p.published_at >= ao.valid_from          |
|       AND (p.published_at < ao.valid_to            |
|            OR ao.valid_to IS NULL)                 |
|     GROUP BY ms.post_id                           |
|   ) per_post;                                     |
+---------------------------------------------------+
                        |
           +------------+------------+
           |                         |
           v                         v
    Result ~ 3.4M              Result ~ 2.1M
    (matches platform)         (matches dashboard)
           |                         |
           v                         v
+---------------------+   +-------------------------+
| PRESENTATION LAYER  |   | DATA LAYER              |
| Data is correct,    |   | Data itself is stale    |
| dashboard query is  |   | or incomplete           |
| broken              |   |                         |
+---------------------+   +-------------------------+
           |                         |
           v                         v
+---------------------+   +-------------------------+
| Investigate #1:     |   | Run BOTH in parallel:   |
| Diff the optimized  |   |                         |
| query against the   |   | A. Scrape frequency     |
| canonical query     |   |    per account per week |
|                     |   |    (Suspect #2)         |
| Common bugs:        |   |                         |
| - Dropped JOIN      |   | B. Last scraped_at for  |
| - LIMIT truncation  |   |    banned accounts      |
| - Latest vs MAX     |   |    (Suspect #3)         |
| - Missing accounts  |   |                         |
+---------------------+   +-------------------------+
           |                    |             |
           v                    v             v
+---------------------+  +-----------+  +-----------+
| FIX: Revert query   |  | Zero      |  | Stale by  |
| Add regression test |  | scrapes   |  | 3 weeks?  |
| canonical == API    |  | post-     |  |           |
+---------------------+  | migration?|  | Unfreeze  |
                          |           |  | accounts, |
                          | Fix cron, |  | force     |
                          | add       |  | re-scrape |
                          | heartbeat |  +-----------+
                          +-----------+

                       Then also check:

+---------------------+   +---------------------+
| Suspect #4:         |   | Suspect #5:         |
| Reassigned accounts |   | Disappeared posts   |
| 3 accts, 6 wks ago  |   | Using latest vs MAX |
|                     |   |                     |
| Check: are posts    |   | Check: any post     |
| from pre-assignment |   | where latest views  |
| period missing from |   | < MAX(views)?       |
| client total?       |   |                     |
|                     |   | Fix: always use     |
| Fix: SCD Type 2     |   | MAX(views) per post |
| with valid_from/to  |   | (high water mark)   |
+---------------------+   +---------------------+
```

---

## Clue-to-Suspect Mapping

```
+---------------------------------------------+----------------------------+
| Clue                                        | Primary Suspect            |
+---------------------------------------------+----------------------------+
| "Optimized query last week"                 | #1 Dashboard query bug     |
|   Most recent change, directly touches the  |    Aggregation broken by   |
|   displayed number, gap is growing          |    "optimization"          |
+---------------------------------------------+----------------------------+
| "Cron migrated 4 weeks ago"                 | #2 Broken scheduler        |
|   Silent failure -- no errors if job never  |    Wrong TZ, missing env   |
|   fires. Timeline matches gap growth.       |    vars, service disabled  |
+---------------------------------------------+----------------------------+
| "2 banned accounts, since unbanned"         | #3 Frozen data             |
|   Scraper skips banned accounts. Views      |    Ban flag prevents       |
|   frozen at 3-week-old values. Platform     |    scraping even after     |
|   counts keep climbing.                     |    platform unban          |
+---------------------------------------------+----------------------------+
| "3 accounts reassigned 6 weeks ago"         | #4 Attribution bug         |
|   Without SCD Type 2 ownership tracking,    |    Posts fall through      |
|   posts from before reassignment may be     |    ownership gap during    |
|   attributed to wrong client or dropped     |    reassignment            |
+---------------------------------------------+----------------------------+
```

---

## Key Investigation Principles

- **Start with the canonical total query.** It takes 30 seconds and splits
  the investigation into two clean branches (data-layer vs presentation-layer).
- **Run scrape-frequency and banned-account queries in parallel** if the
  data layer is the problem. Between three queries, the gap's origin is
  isolated within 5 minutes.
- **Multiple root causes may coexist.** The 1.3M gap could be the sum of
  several smaller gaps (e.g., 800K from stale scrapes + 300K from frozen
  bans + 200K from attribution). Each suspect should be quantified
  independently.
- **"Growing weekly" is the strongest signal.** It eliminates one-time
  causes and points to ongoing processes: either data is not being collected
  (suspects #2, #3) or data is being systematically undercounted (suspects
  #1, #5, #6).
