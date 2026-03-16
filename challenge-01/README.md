# Challenge 01: Reconciliation Engine

Reconciles social media post metrics from two independent API providers,
a previous snapshot, and account assignment history into a single
authoritative report.

---

## 1. System Architecture

```
 +---------------------+    +---------------------+    +--------------------+
 |    Provider A        |    |    Provider B        |    | Previous Snapshot   |
 |  (raw post dicts)    |    |  (raw post dicts)    |    | (last-known metrics)|
 +----------+----------+    +----------+----------+    +---------+----------+
            |                          |                          |
            v                          v                          v
 +----------+----------+    +----------+----------+    +---------+----------+
 | _to_provider_posts() |    | _to_provider_posts() |    | _to_snapshot_records |
 +----------+----------+    +----------+----------+    +---------+----------+
            |                          |                          |
            +------------+-------------+                          |
                         |                                        |
                         v                                        |
            +------------+-------------+                          |
            |  reconcile()             |<-------------------------+
            |  5-step pipeline         |
            +------------+-------------+    +---------------------+
                         |                  | Account Assignments  |
                         |<-----------------| (client attribution) |
                         |                  +---------------------+
                         v
            +------------+-------------+
            | ReconciliationReport     |
            |  - posts[]               |
            |  - anomalies[]           |
            |  - unresolved_share_urls |
            +--------------------------+
```

---

## 2. Reconciliation Pipeline (5-Step Decision Engine)

```
  START
    |
    v
+-----------------------------------------------------------+
| STEP 1: Normalize URLs & Index by Video ID                |
|                                                           |
|   For each post in Provider A + Provider B:               |
|     normalize_url(post.url)                               |
|       |                                                   |
|       +-- CANONICAL --> extract video_id                  |
|       |                 add to id_to_posts[video_id]      |
|       |                                                   |
|       +-- SHARE ------> add to share_url_posts[]          |
|       |                                                   |
|       +-- UNKNOWN ----> if post.id exists, index by id    |
|                         else add to share_url_posts[]     |
+----------------------------+------------------------------+
                             |
                             v
+-----------------------------------------------------------+
| STEP 1b: Match Share URLs via Secondary Signals           |
|                                                           |
|   For each share_url_post:                                |
|     _try_match_share_post(share, canonical_posts)         |
|       Match on: account + timestamp(+/-1hr) + caption     |
|                                                           |
|     +-- MATCH (exactly 1 candidate)                       |
|     |     Merge into id_to_posts[matched_id]              |
|     |     Log UNRESOLVED_SHARE_URL anomaly (heuristic)    |
|     |                                                     |
|     +-- NO MATCH / AMBIGUOUS                              |
|           Add URL to unresolved_urls[]                    |
|           Log UNRESOLVED_SHARE_URL anomaly                |
+----------------------------+------------------------------+
                             |
                             v
+-----------------------------------------------------------+
| STEP 2: Build Snapshot Lookup                             |
|                                                           |
|   snapshot_by_id = { platform_id: SnapshotRecord }        |
+----------------------------+------------------------------+
                             |
                             v
+-----------------------------------------------------------+
| STEP 3: Reconcile Each Video ID                          |
|                                                           |
|   For each (video_id, post_group) in id_to_posts:        |
|                                                           |
|     a) Resolve metrics (high water mark)                  |
|        views  = MAX(prov_a, prov_b, snapshot)             |
|        likes  = MAX(prov_a, prov_b, snapshot)             |
|        comments = MAX(prov_a, prov_b)                     |
|                                                           |
|     b) Pick best caption (longest non-truncated)          |
|                                                           |
|     c) Attribute client via account assignment log        |
|        _attribute_client(account, posted_at, assignments) |
|                                                           |
|     d) Emit ReconciledPost (status=ACTIVE)                |
+----------------------------+------------------------------+
                             |
                             v
+-----------------------------------------------------------+
| STEP 4: Handle Unresolved Share-URL Posts                 |
|                                                           |
|   For each truly unresolved share URL:                    |
|     Emit ReconciledPost (status=UNRESOLVED_URL)           |
|     Use single-provider metrics as-is (no merge)          |
+----------------------------+------------------------------+
                             |
                             v
+-----------------------------------------------------------+
| STEP 5: Detect Disappeared Posts                          |
|                                                           |
|   disappeared = snapshot IDs not in seen_ids              |
|                                                           |
|   if count(disappeared) >= outage_threshold (default 3):  |
|     Log POSSIBLE_OUTAGE anomaly                           |
|                                                           |
|   For each disappeared post:                              |
|     Log DISAPPEARED_POST anomaly                          |
|     Preserve with last-known metrics (status=MISSING)     |
+-----------------------------------------------------------+
    |
    v
  RETURN ReconciliationReport
```

---

## 3. URL Normalization Decision Tree

```
  normalize_url(url)
    |
    v
  +-------------------------------------------+
  | Match against CANONICAL regex?             |
  | tiktok.com/@user/video/<digits>            |
  +-----+-------------------+-----------------+
        |                   |
       YES                  NO
        |                   |
        v                   v
  +-----------+       +----------------------------------+
  | CANONICAL |       | Match against SHARE regex?       |
  | Extract   |       | vm.tiktok.com/* or               |
  | video_id  |       | vt.tiktok.com/*                  |
  | Build     |       +-----+------------------+--------+
  | canonical |             |                  |
  | URL       |            YES                 NO
  +-----------+             |                  |
                            v                  v
                      +-----------+      +-----------+
                      |   SHARE   |      |  UNKNOWN   |
                      | video_id  |      | video_id   |
                      |  = None   |      |  = None    |
                      | Cannot    |      | canonical  |
                      | resolve   |      |  = None    |
                      | without   |      +-----------+
                      | HTTP call |
                      +-----------+

  Examples:
    "https://www.tiktok.com/@creator1/video/7322789456"
        --> CANONICAL, video_id="7322789456"

    "https://vm.tiktok.com/ZMrABC123/"
        --> SHARE, video_id=None

    "https://example.com/something"
        --> UNKNOWN, video_id=None
```

---

## 4. Metric Resolution (High Water Mark)

```
  _resolve_metric(name, prov_a_val, prov_b_val, snapshot_val)
    |
    v
  Collect non-None candidates into { source: value }
    |
    v
  +-----------------------------------+
  | Any candidates?                    |
  +------+-------------------+--------+
         |                   |
         NO                 YES
         |                   |
         v                   v
  +-------------+    resolved = MAX(all candidate values)
  | Return 0    |           |
  +-------------+           v
                 +-----------------------------------+
                 | Snapshot value exists?             |
                 +------+-------------------+--------+
                        |                   |
                       YES                  NO
                        |                   |
                        v                   |
                 For each current provider: |
                   if value < snapshot:     |
                     Log VIEW_DECREASE      |
                     anomaly                |
                        |                   |
                        +--------+----------+
                                 |
                                 v
                 +-----------------------------------+
                 | Both providers reported?          |
                 +------+-------------------+--------+
                        |                   |
                       YES                  NO
                        |                   |
                        v                   |
                 if prov_a != prov_b:       |
                   Log METRIC_CONFLICT      |
                   anomaly                  |
                        |                   |
                        +--------+----------+
                                 |
                                 v
                    Return MetricProvenance(
                      resolved_value = high_water_mark
                    )

  Example:
    views: prov_a=45200, prov_b=44800, snapshot=46000
    resolved = MAX(45200, 44800, 46000) = 46000
    anomalies: VIEW_DECREASE (both current < snapshot)
               METRIC_CONFLICT (45200 != 44800)
```

---

## 5. Caption Matching Decision Tree

```
  _captions_match(caption_a, caption_b)
    |
    v
  Strip trailing whitespace from both
    |
    v
  +----------------------------+
  | Exact match?  (a == b)     |
  +------+-----------+---------+
         |           |
        YES          NO
         |           |
         v           v
     Return      +-----------------------------------+
      True       | Check truncation status            |
                 |   a_trunc = ends with "..."        |
                 |   b_trunc = ends with "..."        |
                 +------+--------+--------+----------+
                        |        |        |
                   A only   B only   Both truncated
                   trunc    trunc
                        |        |        |
                        v        v        v
                +--------+ +--------+ +----------+
                | prefix | | prefix | | prefix_a  |
                | = a    | | = b    | |  = a[:-3] |
                | minus  | | minus  | | prefix_b  |
                | "..."  | | "..."  | |  = b[:-3] |
                +---+----+ +---+----+ +-----+----+
                    |          |            |
                    v          v            v
              b starts   a starts    prefix_a
              with       with        ==
              prefix?    prefix?     prefix_b?
              AND        AND              |
              len(b)     len(a)           v
              >= len     >= len      Return result
              (prefix)?  (prefix)?
                    |          |
                    v          v
              Return       Return
              result       result

  Example:
    A: "This brand changed...their prod..."  (truncated)
    B: "This brand changed...their products #ad #sponsored"  (full)
    --> prefix = "This brand changed...their prod"
    --> B starts with prefix and len(B) >= len(prefix)
    --> MATCH = True
```

---

## Key Data Models

| Model                 | Purpose                                        |
|-----------------------|------------------------------------------------|
| `ProviderPost`        | Raw post from a single provider                |
| `SnapshotRecord`      | Previous metric snapshot for comparison         |
| `AccountAssignment`   | Maps account to client for a time range         |
| `ReconciledPost`      | Final merged post with resolved metrics         |
| `MetricProvenance`    | Tracks per-metric source values and resolution  |
| `Anomaly`             | Detected data quality issue                    |
| `ReconciliationReport`| Top-level output with posts and anomalies      |

## Source Files

| File               | Responsibility                              |
|--------------------|---------------------------------------------|
| `reconciler.py`    | 5-step reconciliation pipeline              |
| `url_normalizer.py`| URL classification and video ID extraction  |
| `models.py`        | All dataclass and enum definitions          |
| `test_reconciler.py`| Test suite                                 |
