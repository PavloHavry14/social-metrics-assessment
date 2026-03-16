"""
Reconciliation Engine — main module.

Reconciles social media post metrics from two independent API
providers, handling five known trap scenarios:

    1. Share URL resolution (pattern extraction, no HTTP)
    2. View count conflicts (high water mark)
    3. Client attribution (account reassignment timeline)
    4. Truncated caption matching (truncation-aware comparison)
    5. Disappeared posts (missing vs outage detection)

Usage:
    from reconciler import reconcile, PROVIDER_A_DATA, PROVIDER_B_DATA, ...
    report = reconcile(PROVIDER_A_DATA, PROVIDER_B_DATA, PREVIOUS_SNAPSHOT, ACCOUNT_ASSIGNMENTS)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import (
    AccountAssignment,
    Anomaly,
    AnomalyKind,
    MetricProvenance,
    PostStatus,
    ProviderPost,
    ReconciledPost,
    ReconciliationReport,
    SnapshotRecord,
)
from .url_normalizer import NormalizedURL, URLKind, normalize_url


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

PROVIDER_A_DATA = [
    {
        "id": None,
        "url": "https://vm.tiktok.com/ZMrABC123/",
        "views": 45200, "likes": 1800, "comments": 94,
        "caption": (
            "This brand changed my entire morning routine and I can't believe "
            "how much better I feel since switching to their products #ad "
            "#sponsored #wellness"
        ),
        "posted_at": "2025-03-14T15:30:00Z",
        "account": "@creator1",
    },
    {
        "id": "7322789456",
        "url": "https://www.tiktok.com/@creator1/video/7322789456",
        "views": 12000, "likes": 450, "comments": 28,
        "caption": (
            "This brand changed my entire morning routine and I can't believe "
            "how much better I feel since switching to their amazing new line "
            "of supplements"
        ),
        "posted_at": "2025-03-14T09:15:00Z",
        "account": "@creator1",
    },
]

PROVIDER_B_DATA = [
    {
        "id": "7321456789",
        "url": "https://tiktok.com/@creator1/video/7321456789",
        "views": 44800, "likes": 1850, "comments": 91,
        "caption": (
            "This brand changed my entire morning routine and I can't believe "
            "how much better I feel since switching to their prod..."
        ),
        "posted_at": "2025-03-14T10:30:00-05:00",
        "account": "@creator1",
    },
    {
        "id": "7322789456",
        "url": "https://tiktok.com/@creator1/video/7322789456",
        "views": 11800, "likes": 445, "comments": 27,
        "caption": (
            "This brand changed my entire morning routine and I can't believe "
            "how much better I feel since switching to their amaz..."
        ),
        "posted_at": "2025-03-14T09:15:00Z",
        "account": "@creator1",
    },
]

PREVIOUS_SNAPSHOT = [
    {
        "platform_id": "7321456789",
        "views": 46000, "likes": 1780,
        "scraped_at": "2025-03-13T12:00:00Z", "source": "provider_a",
    },
    {
        "platform_id": "7320111222",
        "views": 22400, "likes": 900,
        "scraped_at": "2025-03-13T12:00:00Z", "source": "provider_b",
    },
]

ACCOUNT_ASSIGNMENTS = [
    {
        "account": "@creator1", "client": "client_a",
        "from": "2025-01-01T00:00:00Z", "to": "2025-03-14T15:00:00Z",
    },
    {
        "account": "@creator1", "client": "client_b",
        "from": "2025-03-14T15:00:00Z", "to": None,
    },
]


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_iso(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp to a timezone-aware datetime.

    Handles both 'Z' suffixed strings and explicit offsets like
    '-05:00'.  All results are normalised to UTC.
    """
    ts = ts.strip()
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_provider_posts(raw_list: list[dict], source: str) -> list[ProviderPost]:
    """Convert raw dicts to ProviderPost objects."""
    return [
        ProviderPost(
            id=r.get("id"),
            url=r["url"],
            views=r["views"],
            likes=r["likes"],
            comments=r["comments"],
            caption=r["caption"],
            posted_at=r["posted_at"],
            account=r["account"],
            source=source,
        )
        for r in raw_list
    ]


def _to_snapshot_records(raw_list: list[dict]) -> list[SnapshotRecord]:
    return [
        SnapshotRecord(
            platform_id=r["platform_id"],
            views=r["views"],
            likes=r["likes"],
            scraped_at=r["scraped_at"],
            source=r["source"],
        )
        for r in raw_list
    ]


def _to_assignments(raw_list: list[dict]) -> list[AccountAssignment]:
    return [
        AccountAssignment(
            account=r["account"],
            client=r["client"],
            valid_from=_parse_iso(r["from"]),
            valid_to=_parse_iso(r["to"]) if r["to"] else None,
        )
        for r in raw_list
    ]


# ---------------------------------------------------------------------------
# Trap 4: Truncation-aware caption matching
# ---------------------------------------------------------------------------

_TRUNCATION_SUFFIX = "..."


def _is_truncated(caption: str) -> bool:
    """Detect provider-truncated captions (ending with '...')."""
    return caption.rstrip().endswith(_TRUNCATION_SUFFIX)


def _captions_match(caption_a: str, caption_b: str) -> bool:
    """Determine whether two captions refer to the same post.

    Rules:
        - If neither is truncated, require exact match.
        - If one is truncated, compare up to the truncation point ONLY
          if the non-truncated caption is longer than the truncated one.
          This prevents false merges when two different full captions
          share the same prefix.
        - If both are truncated, compare the truncated prefixes.

    Returns True only when we can confidently say the captions match.
    """
    a = caption_a.rstrip()
    b = caption_b.rstrip()

    if a == b:
        return True

    a_trunc = _is_truncated(a)
    b_trunc = _is_truncated(b)

    if a_trunc and not b_trunc:
        prefix = a[: -len(_TRUNCATION_SUFFIX)]
        # Only match if the full caption (b) is longer than the
        # truncated text and starts with the same prefix.
        return len(b) >= len(prefix) and b.startswith(prefix)

    if b_trunc and not a_trunc:
        prefix = b[: -len(_TRUNCATION_SUFFIX)]
        return len(a) >= len(prefix) and a.startswith(prefix)

    if a_trunc and b_trunc:
        # Both truncated — compare the prefix portions.
        prefix_a = a[: -len(_TRUNCATION_SUFFIX)]
        prefix_b = b[: -len(_TRUNCATION_SUFFIX)]
        return prefix_a == prefix_b

    return False


# ---------------------------------------------------------------------------
# Trap 3: Client attribution
# ---------------------------------------------------------------------------

def _attribute_client(
    account: str,
    posted_at: datetime,
    assignments: list[AccountAssignment],
) -> str:
    """Determine the client a post belongs to based on the account
    assignment log and the post's publication timestamp.

    The assignment windows are treated as [from, to) — i.e. the
    'to' boundary is exclusive.  A post published exactly at the
    reassignment moment belongs to the NEW client.
    """
    for a in assignments:
        if a.account != account:
            continue
        if posted_at < a.valid_from:
            continue
        if a.valid_to is not None and posted_at >= a.valid_to:
            continue
        return a.client
    return "unattributed"


# ---------------------------------------------------------------------------
# Trap 2: High water mark metric resolution
# ---------------------------------------------------------------------------

def _resolve_metric(
    name: str,
    provider_a_val: Optional[int],
    provider_b_val: Optional[int],
    snapshot_val: Optional[int],
    anomalies: list[Anomaly],
    post_id: Optional[str],
) -> MetricProvenance:
    """Apply the high water mark strategy for a single metric.

    The highest value ever observed from any source wins.  When a
    current value is lower than a previous snapshot, we flag it
    (the provider may have had a bug, or the platform recounted).
    """
    candidates: dict[str, int] = {}
    if provider_a_val is not None:
        candidates["provider_a"] = provider_a_val
    if provider_b_val is not None:
        candidates["provider_b"] = provider_b_val
    if snapshot_val is not None:
        candidates["previous_snapshot"] = snapshot_val

    if not candidates:
        return MetricProvenance(metric=name, resolved_value=0)

    high_water = max(candidates.values())

    # Flag decreases relative to the snapshot.
    if snapshot_val is not None:
        for source, val in candidates.items():
            if source == "previous_snapshot":
                continue
            if val < snapshot_val:
                anomalies.append(Anomaly(
                    kind=AnomalyKind.VIEW_DECREASE,
                    post_id=post_id,
                    message=(
                        f"{name} from {source} ({val}) is lower than "
                        f"previous snapshot ({snapshot_val}). "
                        f"Using high water mark: {high_water}."
                    ),
                    details={
                        "metric": name,
                        "source": source,
                        "current_value": val,
                        "snapshot_value": snapshot_val,
                        "resolved_value": high_water,
                    },
                ))

    # Flag conflicts between current providers.
    if provider_a_val is not None and provider_b_val is not None:
        if provider_a_val != provider_b_val:
            anomalies.append(Anomaly(
                kind=AnomalyKind.METRIC_CONFLICT,
                post_id=post_id,
                message=(
                    f"{name} conflict: provider_a={provider_a_val}, "
                    f"provider_b={provider_b_val}. "
                    f"Resolved to high water mark: {high_water}."
                ),
                details={
                    "metric": name,
                    "provider_a": provider_a_val,
                    "provider_b": provider_b_val,
                    "resolved_value": high_water,
                },
            ))

    return MetricProvenance(
        metric=name,
        provider_a_value=provider_a_val,
        provider_b_value=provider_b_val,
        snapshot_value=snapshot_val,
        resolved_value=high_water,
    )


# ---------------------------------------------------------------------------
# Trap 1: Matching share-URL posts via secondary signals
# ---------------------------------------------------------------------------

def _timestamps_close(a: datetime, b: datetime, tolerance_seconds: int = 3600) -> bool:
    """Check whether two timestamps are within tolerance.

    Provider A and B may report slightly different timestamps due to
    timezone handling or ingestion lag. We use a generous default of
    one hour.
    """
    return abs((a - b).total_seconds()) <= tolerance_seconds


def _try_match_share_post(
    share_post: ProviderPost,
    canonical_posts: list[ProviderPost],
    already_matched_ids: set[str],
) -> Optional[ProviderPost]:
    """Attempt to match a share-URL post to a canonical post using
    account + timestamp proximity + caption similarity.

    Returns the matched canonical post, or None.
    """
    share_ts = _parse_iso(share_post.posted_at)

    candidates = []
    for cp in canonical_posts:
        if cp.id in already_matched_ids:
            continue
        if cp.account != share_post.account:
            continue
        cp_ts = _parse_iso(cp.posted_at)
        if not _timestamps_close(share_ts, cp_ts):
            continue
        if _captions_match(share_post.caption, cp.caption):
            candidates.append(cp)

    if len(candidates) == 1:
        return candidates[0]
    # Ambiguous or no match — do not merge.
    return None


# ---------------------------------------------------------------------------
# Core reconciliation
# ---------------------------------------------------------------------------

def reconcile(
    provider_a_raw: list[dict],
    provider_b_raw: list[dict],
    snapshot_raw: list[dict],
    assignments_raw: list[dict],
    *,
    outage_threshold: int = 3,
) -> ReconciliationReport:
    """Run the full reconciliation pipeline.

    Args:
        provider_a_raw: Posts from Provider A.
        provider_b_raw: Posts from Provider B.
        snapshot_raw:   Previous day's snapshot records.
        assignments_raw: Account-to-client assignment log.
        outage_threshold: If at least this many previously-known posts
            disappear from BOTH providers, flag a possible outage
            rather than individual deletions.

    Returns:
        A ReconciliationReport with reconciled posts and anomalies.
    """
    posts_a = _to_provider_posts(provider_a_raw, "provider_a")
    posts_b = _to_provider_posts(provider_b_raw, "provider_b")
    snapshots = _to_snapshot_records(snapshot_raw)
    assignments = _to_assignments(assignments_raw)

    all_anomalies: list[Anomaly] = []
    unresolved_urls: list[str] = []

    # --- Step 1: Normalize URLs and index by video ID ---

    # Build lookup: video_id -> list of ProviderPosts
    id_to_posts: dict[str, list[ProviderPost]] = {}
    share_url_posts: list[ProviderPost] = []  # Posts we can't resolve by URL

    for post in posts_a + posts_b:
        norm = normalize_url(post.url)

        if norm.kind == URLKind.CANONICAL and norm.video_id:
            # Use the video ID from the URL; override the post's id if
            # it was missing.
            vid = norm.video_id
            if post.id is None:
                post.id = vid
            id_to_posts.setdefault(vid, []).append(post)

        elif norm.kind == URLKind.SHARE:
            share_url_posts.append(post)

        elif post.id:
            # URL wasn't recognised but we have an explicit ID.
            id_to_posts.setdefault(post.id, []).append(post)
        else:
            share_url_posts.append(post)

    # --- Step 1b: Attempt to match share-URL posts via secondary signals ---

    # Collect canonical posts from the other provider to match against.
    all_canonical = [p for p in posts_a + posts_b
                     if normalize_url(p.url).kind == URLKind.CANONICAL and p.id]

    matched_share_ids: set[str] = set()

    for sp in share_url_posts:
        match = _try_match_share_post(sp, all_canonical, matched_share_ids)
        if match and match.id:
            matched_share_ids.add(match.id)
            id_to_posts.setdefault(match.id, []).append(sp)
            all_anomalies.append(Anomaly(
                kind=AnomalyKind.UNRESOLVED_SHARE_URL,
                post_id=match.id,
                message=(
                    f"Share URL '{sp.url}' matched to video {match.id} via "
                    f"account + timestamp + caption similarity. "
                    f"Confidence: heuristic (no HTTP resolution)."
                ),
                details={
                    "share_url": sp.url,
                    "matched_video_id": match.id,
                    "matching_signals": ["account", "timestamp", "caption_similarity"],
                },
            ))
        else:
            unresolved_urls.append(sp.url)
            all_anomalies.append(Anomaly(
                kind=AnomalyKind.UNRESOLVED_SHARE_URL,
                post_id=None,
                message=(
                    f"Share URL '{sp.url}' could not be resolved to a "
                    f"canonical video ID. No HTTP requests attempted. "
                    f"No confident secondary-signal match found."
                ),
                details={"share_url": sp.url, "account": sp.account},
            ))

    # --- Step 2: Build snapshot lookup ---
    snapshot_by_id: dict[str, SnapshotRecord] = {
        s.platform_id: s for s in snapshots
    }

    # --- Step 3: Reconcile each video ID ---
    reconciled: list[ReconciledPost] = []
    seen_ids: set[str] = set()

    for vid, post_group in id_to_posts.items():
        seen_ids.add(vid)
        post_anomalies: list[Anomaly] = []
        snap = snapshot_by_id.get(vid)

        # Separate by source.
        a_posts = [p for p in post_group if p.source == "provider_a"]
        b_posts = [p for p in post_group if p.source == "provider_b"]

        val_a = lambda attr: getattr(a_posts[0], attr) if a_posts else None  # noqa: E731
        val_b = lambda attr: getattr(b_posts[0], attr) if b_posts else None  # noqa: E731

        # Resolve metrics via high water mark (Trap 2).
        views_prov = _resolve_metric(
            "views",
            val_a("views"), val_b("views"),
            snap.views if snap else None,
            post_anomalies, vid,
        )
        likes_prov = _resolve_metric(
            "likes",
            val_a("likes"), val_b("likes"),
            snap.likes if snap else None,
            post_anomalies, vid,
        )
        comments_prov = _resolve_metric(
            "comments",
            val_a("comments"), val_b("comments"),
            None,  # snapshot doesn't have comments in sample data
            post_anomalies, vid,
        )

        # Caption: pick the longest non-truncated version (Trap 4).
        captions = [p.caption for p in post_group]
        non_truncated = [c for c in captions if not _is_truncated(c)]
        best_caption = max(non_truncated, key=len) if non_truncated else max(captions, key=len)

        # Check if truncated captions were involved in the match.
        if any(_is_truncated(c) for c in captions) and non_truncated:
            post_anomalies.append(Anomaly(
                kind=AnomalyKind.CAPTION_TRUNCATION_MATCH,
                post_id=vid,
                message=(
                    "One or more providers returned a truncated caption. "
                    "Matched via truncation-aware comparison; using "
                    "longest non-truncated version."
                ),
                details={
                    "truncated_captions": [c for c in captions if _is_truncated(c)],
                    "full_caption": best_caption,
                },
            ))

        # Timestamp: pick the first available.
        posted_at_str = post_group[0].posted_at
        posted_at = _parse_iso(posted_at_str)

        # Account: should be consistent across providers.
        account = post_group[0].account

        # Client attribution (Trap 3).
        client = _attribute_client(account, posted_at, assignments)

        # Collect all raw URLs for the record.
        raw_urls = list({p.url for p in post_group})

        # Build canonical URL.
        canonical_urls = [
            normalize_url(p.url) for p in post_group
            if normalize_url(p.url).kind == URLKind.CANONICAL
        ]
        canonical_url = canonical_urls[0].canonical_url if canonical_urls else None

        reconciled.append(ReconciledPost(
            platform_id=vid,
            canonical_url=canonical_url,
            views=views_prov.resolved_value,
            likes=likes_prov.resolved_value,
            comments=comments_prov.resolved_value,
            caption=best_caption,
            posted_at=posted_at,
            account=account,
            client=client,
            status=PostStatus.ACTIVE,
            provenance=[views_prov, likes_prov, comments_prov],
            anomalies=post_anomalies,
            raw_urls=raw_urls,
        ))

        all_anomalies.extend(post_anomalies)

    # --- Step 4: Handle unresolved share-URL posts that had no match ---
    for sp in share_url_posts:
        if sp.url in unresolved_urls:
            posted_at = _parse_iso(sp.posted_at)
            client = _attribute_client(sp.account, posted_at, assignments)
            reconciled.append(ReconciledPost(
                platform_id=None,
                canonical_url=None,
                views=sp.views,
                likes=sp.likes,
                comments=sp.comments,
                caption=sp.caption,
                posted_at=posted_at,
                account=sp.account,
                client=client,
                status=PostStatus.UNRESOLVED_URL,
                provenance=[],
                anomalies=[],
                raw_urls=[sp.url],
            ))

    # --- Step 5: Detect disappeared posts (Trap 5) ---
    disappeared_ids = [
        s.platform_id for s in snapshots if s.platform_id not in seen_ids
    ]

    if len(disappeared_ids) >= outage_threshold:
        # Many posts vanished — likely a provider outage, not deletions.
        all_anomalies.append(Anomaly(
            kind=AnomalyKind.POSSIBLE_OUTAGE,
            post_id=None,
            message=(
                f"{len(disappeared_ids)} posts from the previous snapshot are "
                f"missing from both providers. This exceeds the outage "
                f"threshold ({outage_threshold}). Possible provider outage."
            ),
            details={"missing_ids": disappeared_ids},
        ))

    for did in disappeared_ids:
        snap = snapshot_by_id[did]
        all_anomalies.append(Anomaly(
            kind=AnomalyKind.DISAPPEARED_POST,
            post_id=did,
            message=(
                f"Post {did} was in the previous snapshot "
                f"(views={snap.views}, likes={snap.likes}) but is absent "
                f"from both providers today. Retaining last-known metrics."
            ),
            details={
                "last_views": snap.views,
                "last_likes": snap.likes,
                "last_scraped_at": snap.scraped_at,
                "last_source": snap.source,
            },
        ))

        # Preserve the post with its last-known metrics.
        reconciled.append(ReconciledPost(
            platform_id=did,
            canonical_url=None,
            views=snap.views,
            likes=snap.likes,
            comments=0,
            caption="",
            posted_at=_parse_iso(snap.scraped_at),
            account="",
            client="unknown",
            status=PostStatus.MISSING,
            provenance=[],
            anomalies=[],
            raw_urls=[],
        ))

    return ReconciliationReport(
        posts=reconciled,
        anomalies=all_anomalies,
        unresolved_share_urls=unresolved_urls,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run reconciliation with sample data and print the report."""
    report = reconcile(
        PROVIDER_A_DATA,
        PROVIDER_B_DATA,
        PREVIOUS_SNAPSHOT,
        ACCOUNT_ASSIGNMENTS,
    )

    print("=" * 72)
    print("RECONCILIATION REPORT")
    print("=" * 72)

    for post in report.posts:
        print(f"\n--- Post: {post.platform_id or '(unresolved)'} ---")
        print(f"  Status:    {post.status.value}")
        print(f"  Account:   {post.account}")
        print(f"  Client:    {post.client}")
        print(f"  Views:     {post.views}")
        print(f"  Likes:     {post.likes}")
        print(f"  Comments:  {post.comments}")
        print(f"  Posted:    {post.posted_at.isoformat()}")
        print(f"  Caption:   {post.caption[:80]}{'...' if len(post.caption) > 80 else ''}")
        print(f"  URLs:      {post.raw_urls}")
        if post.provenance:
            print("  Provenance:")
            for prov in post.provenance:
                print(
                    f"    {prov.metric}: A={prov.provider_a_value}, "
                    f"B={prov.provider_b_value}, snap={prov.snapshot_value} "
                    f"-> {prov.resolved_value}"
                )

    print(f"\n{'=' * 72}")
    print(f"ANOMALIES ({len(report.anomalies)})")
    print("=" * 72)
    for a in report.anomalies:
        print(f"\n  [{a.kind.value}] post={a.post_id}")
        print(f"    {a.message}")

    if report.unresolved_share_urls:
        print(f"\n{'=' * 72}")
        print("UNRESOLVED SHARE URLs")
        print("=" * 72)
        for url in report.unresolved_share_urls:
            print(f"  - {url}")


if __name__ == "__main__":
    main()
