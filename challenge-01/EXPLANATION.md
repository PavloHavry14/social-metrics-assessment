# Challenge 01 -- Reconciliation Engine: Written Explanation

## 1. Share URL Resolution Without HTTP Requests

Share URLs like `https://vm.tiktok.com/ZMrABC123/` are opaque short-links. The video ID is not embedded in the URL path -- it only exists on the other side of an HTTP 301 redirect. We explicitly chose not to follow that redirect because the assessment prohibits network I/O, and because adding HTTP calls introduces latency, rate-limit risk, and a hard external dependency.

**Pattern extraction approach.** In `url_normalizer.py`, we classify every URL into one of three kinds using two compiled regexes:

- `_CANONICAL_RE` matches paths like `/@user/video/7321456789` and captures the numeric video ID from the final path segment.
- `_SHARE_RE` matches known short-link domains (`vm.tiktok.com`, `vt.tiktok.com`). These are classified as `URLKind.SHARE` with `video_id=None`.

When a post carries a share URL, it lands in the `share_url_posts` list in `reconciler.py` (line 438). The reconciler then calls `_try_match_share_post`, which attempts to find the corresponding canonical post from the other provider using three secondary signals checked in conjunction:

1. **Account match** -- the share-URL post and the candidate must have the same `@account`.
2. **Timestamp proximity** -- `_timestamps_close` requires the two `posted_at` values to be within 3600 seconds of each other (generous, to absorb timezone and ingestion-lag differences).
3. **Caption similarity** -- `_captions_match` performs truncation-aware prefix comparison (detailed in section 2 below).

A match is only accepted when exactly one candidate satisfies all three conditions. If zero or multiple candidates match, the post is left unresolved.

**What cannot be resolved.** If a share-URL post has no corresponding canonical post in either provider's data (e.g., it was only scraped by one provider and that provider used the share URL), there is nothing to match against. The post is emitted with `status=UNRESOLVED_URL`, `platform_id=None`, and an `UNRESOLVED_SHARE_URL` anomaly. This makes the gap visible to downstream consumers rather than silently dropping the data.

In the sample data, Provider A's first entry (`url: vm.tiktok.com/ZMrABC123/`) is a share URL. The reconciler matches it to Provider B's `7321456789` entry because both belong to `@creator1`, their timestamps are close (after UTC normalisation `2025-03-14T15:30:00Z` vs `2025-03-14T15:30:00Z`), and the captions share a common prefix (Provider B's caption is truncated with `...` but its prefix matches Provider A's full caption). An `UNRESOLVED_SHARE_URL` anomaly is still logged to record the heuristic nature of the match.


## 2. Caption Matching and Truncation-Aware Comparison

Provider B truncates captions by cutting them at a character limit and appending `"..."`. The `_captions_match` function in `reconciler.py` (line 192) handles four cases:

| Caption A | Caption B | Logic |
|-----------|-----------|-------|
| Not truncated | Not truncated | Exact string equality |
| Truncated | Not truncated | Strip the `...` suffix to get the prefix. Accept if B starts with that prefix and B is at least as long as the prefix. |
| Not truncated | Truncated | Mirror of above |
| Both truncated | Both truncated | Strip `...` from both, require exact prefix equality |

**Why this approach rather than fuzzy matching (e.g., Levenshtein, cosine similarity).** Fuzzy matching introduces a threshold parameter that risks false merges. Two genuinely different posts by the same creator can start with identical words ("This brand changed my entire morning routine...") but diverge after the truncation point. The prefix approach is deterministic and only matches when the truncated text is provably the start of the full text.

**Edge cases it misses:**

- **Same prefix, different posts.** If Creator1 publishes two videos on the same day with captions that share the first 80 characters but differ after that, and Provider B truncates both at character 78, the prefixes will be identical. `_captions_match` would return `True` for both pairs, producing two candidates. `_try_match_share_post` returns `None` when there are multiple candidates, so no false merge occurs, but both posts lose the cross-provider match and their metrics are not reconciled.
- **Whitespace / encoding differences.** We only call `.rstrip()` on captions. If providers normalise Unicode differently (e.g., smart quotes vs straight quotes), the prefix check would fail even on a true match.
- **Both providers truncate at different lengths.** If A truncates at 80 chars and B truncates at 60 chars, `_captions_match` with both-truncated logic requires exact prefix equality. The shorter prefix will not equal the longer prefix, so the match fails. This is a conservative miss rather than a false merge.


## 3. Post 7320111222 -- Disappeared Post Handling

Post `7320111222` appears in `PREVIOUS_SNAPSHOT` (views=22400, likes=900, scraped yesterday by `provider_b`) but is absent from both `PROVIDER_A_DATA` and `PROVIDER_B_DATA` today.

**What the code does.** After reconciling all posts present in today's provider data (Step 3 in `reconcile()`), the engine computes `disappeared_ids` (line 608): every `platform_id` in the previous snapshot that was not seen in today's data. `7320111222` falls into this set.

Two things happen:

1. **Outage check.** If the number of disappeared posts meets or exceeds `outage_threshold` (default 3), the engine flags a `POSSIBLE_OUTAGE` anomaly -- indicating that bulk disappearance is more likely a provider API failure than real deletions. In the sample data, only one post disappeared (below the threshold of 3), so no outage is flagged.

2. **Preservation with MISSING status.** The post is added to the reconciled output with `status=PostStatus.MISSING`, carrying its last-known metrics from the snapshot (views=22400, likes=900). A `DISAPPEARED_POST` anomaly is logged with the message: _"Post 7320111222 was in the previous snapshot but is absent from both providers today. Retaining last-known metrics."_

This design prevents data loss. If the post was genuinely deleted by the creator or removed by the platform, it still appears in the output flagged as MISSING so downstream dashboards can decide how to handle it (e.g., grey it out, exclude from active totals). If it reappears in tomorrow's scrape, the high-water-mark logic will reconcile it normally.


## 4. View Count for Post 7321456789 -- High Water Mark Resolution

The three sources report:
- Provider A: 45,200 views
- Provider B: 44,800 views
- Previous snapshot: 46,000 views

**Which wins: the previous snapshot's 46,000.** The `_resolve_metric` function (line 264) computes `high_water = max(candidates.values())`. With candidates `{"provider_a": 45200, "provider_b": 44800, "previous_snapshot": 46000}`, the max is 46,000.

**Why.** View counts on social platforms are monotonically non-decreasing under normal operation. A current value lower than a previously observed value indicates either a provider-side caching inconsistency, an API bug, or a platform audit that adjusted counts. Rather than trusting a potentially stale or buggy current reading, we take the highest value ever observed. This is the "high water mark" strategy.

**Anomalies logged.** Two anomalies are emitted:

- `VIEW_DECREASE`: Provider A's 45,200 is lower than the snapshot's 46,000.
- `VIEW_DECREASE`: Provider B's 44,800 is lower than the snapshot's 46,000.
- `METRIC_CONFLICT`: Provider A (45,200) and Provider B (44,800) disagree.

All three anomalies are attached to the post so downstream systems can audit the decision.

**What if yesterday's snapshot was itself wrong?** This is the fundamental limitation of the high-water-mark strategy: it can only ratchet upward. If yesterday's snapshot recorded 46,000 due to a provider bug (e.g., double-counting, a stale cache that inflated the number), that inflated value becomes the floor forever. Every future reconciliation will resolve to at least 46,000 even if the true count is lower.

Mitigations the code provides:

1. **Full provenance tracking.** The `MetricProvenance` object stores `provider_a_value`, `provider_b_value`, and `snapshot_value` alongside the `resolved_value`. A human or automated auditor can inspect these and manually override.
2. **Anomaly trail.** The `VIEW_DECREASE` anomalies explicitly note the discrepancy and the resolved value, making it easy to query for posts where the snapshot drove the resolution rather than a current provider.
3. **Potential extension.** In a production system, if both current providers agree within a tight tolerance and both are below the snapshot by a significant margin, that pattern could trigger a `SNAPSHOT_SUSPECT` anomaly and optionally prefer the current consensus. The current code does not implement this, which is a deliberate trade-off toward data preservation over correction.
