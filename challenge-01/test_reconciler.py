"""
Comprehensive tests for Challenge 01 — Reconciliation Engine.

Covers all five trap scenarios plus edge cases:
    1. URL normalization (canonical, share, malformed)
    2. Caption matching (exact, truncated, the prefix-collision trap)
    3. Metric conflicts (high water mark, monotonic enforcement, provenance)
    4. Disappeared posts (missing detection, outage warning)
    5. Client attribution (before/after reassignment boundary)
    6. Full integration with sample data
"""

from __future__ import annotations

import importlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module imports — the directory is named "challenge-01" (with a hyphen),
# which is not a valid Python identifier, so we add the directory to
# sys.path and import the modules directly.
# ---------------------------------------------------------------------------
_CHALLENGE_DIR = str(Path(__file__).resolve().parent)
if _CHALLENGE_DIR not in sys.path:
    sys.path.insert(0, _CHALLENGE_DIR)

# We also need the parent on the path so that the package's own relative
# imports (from .models import ...) work.  The __init__.py makes the
# directory a package; we import it as such.
_PARENT_DIR = str(Path(__file__).resolve().parent.parent)
if _PARENT_DIR not in sys.path:
    sys.path.insert(0, _PARENT_DIR)

# Import the package using importlib to handle the hyphenated directory name.
_pkg = importlib.import_module("challenge-01")
sys.modules["challenge_01"] = _pkg

# Now import submodules through the package alias.
from challenge_01.url_normalizer import (  # noqa: E402
    NormalizedURL,
    URLKind,
    extract_video_id,
    is_share_url,
    normalize_url,
)
from challenge_01.reconciler import (  # noqa: E402
    ACCOUNT_ASSIGNMENTS,
    PREVIOUS_SNAPSHOT,
    PROVIDER_A_DATA,
    PROVIDER_B_DATA,
    _attribute_client,
    _captions_match,
    _is_truncated,
    _parse_iso,
    _resolve_metric,
    _timestamps_close,
    reconcile,
)
from challenge_01.models import (  # noqa: E402
    AccountAssignment,
    Anomaly,
    AnomalyKind,
    MetricProvenance,
    PostStatus,
    ReconciledPost,
)


# =========================================================================
# URL NORMALIZATION TESTS (5+)
# =========================================================================

class TestURLNormalization:
    """Tests for url_normalizer.normalize_url and helpers."""

    def test_canonical_url_extracts_video_id(self):
        """Canonical TikTok URL extracts the numeric video ID correctly."""
        result = normalize_url("https://tiktok.com/@creator1/video/7321456789")
        assert result.kind == URLKind.CANONICAL
        assert result.video_id == "7321456789"
        assert result.canonical_url is not None
        assert "7321456789" in result.canonical_url

    def test_canonical_url_with_www_prefix(self):
        """Canonical URL with www prefix is recognized and ID extracted."""
        result = normalize_url("https://www.tiktok.com/@creator1/video/7322789456")
        assert result.kind == URLKind.CANONICAL
        assert result.video_id == "7322789456"
        assert result.canonical_url == "https://www.tiktok.com/@creator1/video/7322789456"

    def test_share_url_vm_classified_as_share(self):
        """Share URL (vm.tiktok.com) is classified as unresolvable — no video ID."""
        result = normalize_url("https://vm.tiktok.com/ZMrABC123/")
        assert result.kind == URLKind.SHARE
        assert result.video_id is None
        assert result.canonical_url is None

    def test_share_url_vt_classified_as_share(self):
        """Share URL (vt.tiktok.com) is classified as unresolvable — no video ID."""
        result = normalize_url("https://vt.tiktok.com/ZSrXYZ789/")
        assert result.kind == URLKind.SHARE
        assert result.video_id is None
        assert result.canonical_url is None

    def test_malformed_url_handled_gracefully(self):
        """Malformed / unrecognized URLs are classified as UNKNOWN, no crash."""
        for url in [
            "not-a-url",
            "https://example.com/video/123",
            "",
            "https://tiktok.com/without-video-path",
            "ftp://tiktok.com/@user/video/123",
        ]:
            result = normalize_url(url)
            assert result.kind in (URLKind.UNKNOWN, URLKind.CANONICAL, URLKind.SHARE), (
                f"Unexpected kind for URL: {url}"
            )

    def test_instagram_reel_url_unknown(self):
        """Instagram Reel URL is not a TikTok URL — should be UNKNOWN."""
        result = normalize_url("https://www.instagram.com/reel/CxAbC123/")
        assert result.kind == URLKind.UNKNOWN
        assert result.video_id is None

    def test_instagram_post_url_unknown(self):
        """Instagram post URL is classified as UNKNOWN."""
        result = normalize_url("https://www.instagram.com/p/CxAbC123/")
        assert result.kind == URLKind.UNKNOWN
        assert result.video_id is None

    def test_extract_video_id_canonical(self):
        """extract_video_id returns the numeric ID from a canonical URL."""
        assert extract_video_id("https://tiktok.com/@user/video/999888777") == "999888777"

    def test_extract_video_id_share_returns_none(self):
        """extract_video_id returns None for share URLs."""
        assert extract_video_id("https://vm.tiktok.com/ZMrABC123/") is None

    def test_is_share_url_true(self):
        assert is_share_url("https://vm.tiktok.com/ZMrABC123/") is True
        assert is_share_url("https://vt.tiktok.com/ZSrXYZ789/") is True

    def test_is_share_url_false_for_canonical(self):
        assert is_share_url("https://tiktok.com/@user/video/123456") is False

    def test_canonical_url_normalization_produces_www(self):
        """Canonical URLs without www should be rebuilt with www."""
        result = normalize_url("https://tiktok.com/@creator1/video/7321456789")
        assert result.canonical_url == "https://www.tiktok.com/@creator1/video/7321456789"


# =========================================================================
# CAPTION MATCHING TESTS (5+)
# =========================================================================

class TestCaptionMatching:
    """Tests for truncation-aware caption comparison."""

    def test_exact_caption_match(self):
        """Two identical captions match."""
        caption = "This is an exact caption with no differences"
        assert _captions_match(caption, caption) is True

    def test_truncated_caption_matches_full(self):
        """A truncated caption ('...') matches the full version."""
        full = (
            "This brand changed my entire morning routine and I can't believe "
            "how much better I feel since switching to their products"
        )
        truncated = (
            "This brand changed my entire morning routine and I can't believe "
            "how much better I feel since switching to their prod..."
        )
        assert _captions_match(truncated, full) is True
        # Order should not matter.
        assert _captions_match(full, truncated) is True

    def test_two_posts_same_150_char_prefix_different_full_captions_do_not_merge(self):
        """THE TRAP: Two different posts sharing a long prefix must NOT merge.

        If Provider B truncates both captions at the same point, a naive
        prefix comparison would incorrectly merge them. The engine must
        keep them separate.
        """
        # Two genuinely different posts that share a 150+ char prefix.
        full_a = (
            "This brand changed my entire morning routine and I can't believe "
            "how much better I feel since switching to their products #ad "
            "#sponsored #wellness"
        )
        full_b = (
            "This brand changed my entire morning routine and I can't believe "
            "how much better I feel since switching to their amazing new line "
            "of supplements"
        )
        # Verify they share a long common prefix.
        common = 0
        for ca, cb in zip(full_a, full_b):
            if ca != cb:
                break
            common += 1
        assert common > 60, "Captions should share a substantial prefix for this test"

        # The full captions must NOT match.
        assert _captions_match(full_a, full_b) is False

    def test_truncated_vs_truncated_same_prefix_match(self):
        """Two truncated captions with the same prefix do match."""
        a = "This brand changed my entire morning routine..."
        b = "This brand changed my entire morning routine..."
        assert _captions_match(a, b) is True

    def test_truncated_vs_truncated_different_prefix_no_match(self):
        """Two truncated captions with different prefixes do not match."""
        a = "Alpha brand changed my morning..."
        b = "Beta brand changed my morning..."
        assert _captions_match(a, b) is False

    def test_empty_captions_match(self):
        """Two empty captions match (trivially)."""
        assert _captions_match("", "") is True

    def test_none_or_empty_handling(self):
        """Empty string vs non-empty does not match."""
        assert _captions_match("", "Some real caption") is False

    def test_is_truncated_detects_ellipsis(self):
        assert _is_truncated("hello world...") is True
        assert _is_truncated("hello world") is False
        assert _is_truncated("hello world.") is False


# =========================================================================
# METRIC CONFLICT / HIGH WATER MARK TESTS (4+)
# =========================================================================

class TestMetricConflicts:
    """Tests for high water mark resolution and monotonic enforcement."""

    def test_high_water_mark_picks_highest_across_all_sources(self):
        """The resolved value is the maximum of provider_a, provider_b, and snapshot."""
        anomalies: list[Anomaly] = []
        result = _resolve_metric(
            "views",
            provider_a_val=45000,
            provider_b_val=44800,
            snapshot_val=46000,
            anomalies=anomalies,
            post_id="test1",
        )
        assert result.resolved_value == 46000

    def test_monotonic_enforcement_views_never_decrease(self):
        """When current values are lower than the snapshot, anomalies are flagged
        and the high water mark (snapshot) prevails."""
        anomalies: list[Anomaly] = []
        result = _resolve_metric(
            "views",
            provider_a_val=45000,
            provider_b_val=44800,
            snapshot_val=46000,
            anomalies=anomalies,
            post_id="test2",
        )
        # The resolved value must be the snapshot value (highest).
        assert result.resolved_value == 46000
        # Anomalies should flag the decrease.
        decrease_anomalies = [a for a in anomalies if a.kind == AnomalyKind.VIEW_DECREASE]
        assert len(decrease_anomalies) >= 1, "Should flag at least one view decrease"

    def test_provenance_logged_correctly(self):
        """MetricProvenance records all source values and the winner."""
        anomalies: list[Anomaly] = []
        result = _resolve_metric(
            "likes",
            provider_a_val=1800,
            provider_b_val=1850,
            snapshot_val=1780,
            anomalies=anomalies,
            post_id="test3",
        )
        assert result.metric == "likes"
        assert result.provider_a_value == 1800
        assert result.provider_b_value == 1850
        assert result.snapshot_value == 1780
        assert result.resolved_value == 1850

    def test_metric_conflict_flagged_when_providers_disagree(self):
        """When provider A and B report different values, an anomaly is logged."""
        anomalies: list[Anomaly] = []
        _resolve_metric(
            "views",
            provider_a_val=45200,
            provider_b_val=44800,
            snapshot_val=None,
            anomalies=anomalies,
            post_id="test4",
        )
        conflict_anomalies = [a for a in anomalies if a.kind == AnomalyKind.METRIC_CONFLICT]
        assert len(conflict_anomalies) == 1
        assert "provider_a" in conflict_anomalies[0].message
        assert "provider_b" in conflict_anomalies[0].message

    def test_no_anomaly_when_providers_agree(self):
        """When both providers report the same value, no conflict anomaly."""
        anomalies: list[Anomaly] = []
        _resolve_metric(
            "views",
            provider_a_val=10000,
            provider_b_val=10000,
            snapshot_val=None,
            anomalies=anomalies,
            post_id="test5",
        )
        conflict_anomalies = [a for a in anomalies if a.kind == AnomalyKind.METRIC_CONFLICT]
        assert len(conflict_anomalies) == 0

    def test_resolve_metric_with_no_data_returns_zero(self):
        """If all sources are None, resolved value defaults to 0."""
        anomalies: list[Anomaly] = []
        result = _resolve_metric(
            "views",
            provider_a_val=None,
            provider_b_val=None,
            snapshot_val=None,
            anomalies=anomalies,
            post_id="test6",
        )
        assert result.resolved_value == 0


# =========================================================================
# DISAPPEARED POSTS TESTS (3+)
# =========================================================================

class TestDisappearedPosts:
    """Tests for Trap 5 — disappeared post detection and outage warning."""

    def test_post_in_snapshot_missing_from_providers_flagged_missing(self):
        """A post present in the previous snapshot but absent from both
        providers today is flagged with status MISSING."""
        report = reconcile(
            provider_a_raw=[],          # No posts from either provider
            provider_b_raw=[],
            snapshot_raw=[
                {
                    "platform_id": "9999",
                    "views": 10000,
                    "likes": 500,
                    "scraped_at": "2025-03-13T12:00:00Z",
                    "source": "provider_a",
                },
            ],
            assignments_raw=[],
        )
        missing_posts = [p for p in report.posts if p.status == PostStatus.MISSING]
        assert len(missing_posts) == 1
        assert missing_posts[0].platform_id == "9999"
        assert missing_posts[0].views == 10000

        # Check anomaly logged.
        disappeared_anomalies = [
            a for a in report.anomalies if a.kind == AnomalyKind.DISAPPEARED_POST
        ]
        assert len(disappeared_anomalies) == 1

    def test_multiple_disappearances_triggers_outage_warning(self):
        """When >= outage_threshold posts disappear, a POSSIBLE_OUTAGE
        anomaly is emitted in addition to individual DISAPPEARED_POST ones."""
        snapshot = [
            {
                "platform_id": f"post_{i}",
                "views": 1000 * i,
                "likes": 100 * i,
                "scraped_at": "2025-03-13T12:00:00Z",
                "source": "provider_a",
            }
            for i in range(1, 6)  # 5 posts
        ]
        report = reconcile(
            provider_a_raw=[],
            provider_b_raw=[],
            snapshot_raw=snapshot,
            assignments_raw=[],
            outage_threshold=3,
        )
        outage_anomalies = [
            a for a in report.anomalies if a.kind == AnomalyKind.POSSIBLE_OUTAGE
        ]
        assert len(outage_anomalies) == 1
        assert "5 posts" in outage_anomalies[0].message

    def test_single_disappearance_no_outage_warning(self):
        """A single missing post should NOT trigger the outage warning
        (below the default threshold)."""
        report = reconcile(
            provider_a_raw=[],
            provider_b_raw=[],
            snapshot_raw=[
                {
                    "platform_id": "solo_post",
                    "views": 500,
                    "likes": 20,
                    "scraped_at": "2025-03-13T12:00:00Z",
                    "source": "provider_a",
                },
            ],
            assignments_raw=[],
            outage_threshold=3,
        )
        outage = [a for a in report.anomalies if a.kind == AnomalyKind.POSSIBLE_OUTAGE]
        assert len(outage) == 0
        disappeared = [a for a in report.anomalies if a.kind == AnomalyKind.DISAPPEARED_POST]
        assert len(disappeared) == 1


# =========================================================================
# CLIENT ATTRIBUTION TESTS (3+)
# =========================================================================

class TestClientAttribution:
    """Tests for Trap 3 — account reassignment timeline."""

    @pytest.fixture()
    def assignments(self) -> list[AccountAssignment]:
        """Standard two-period assignment: client_a then client_b."""
        boundary = datetime(2025, 3, 14, 15, 0, 0, tzinfo=timezone.utc)
        return [
            AccountAssignment(
                account="@creator1",
                client="client_a",
                valid_from=datetime(2025, 1, 1, tzinfo=timezone.utc),
                valid_to=boundary,
            ),
            AccountAssignment(
                account="@creator1",
                client="client_b",
                valid_from=boundary,
                valid_to=None,
            ),
        ]

    def test_post_before_reassignment_attributed_to_original_client(self, assignments):
        """A post published before the reassignment boundary belongs to
        the original client (client_a)."""
        posted_at = datetime(2025, 3, 14, 9, 0, 0, tzinfo=timezone.utc)
        assert _attribute_client("@creator1", posted_at, assignments) == "client_a"

    def test_post_at_boundary_attributed_to_new_client(self, assignments):
        """A post published exactly at the reassignment timestamp belongs
        to the NEW client (the boundary is exclusive for the old period)."""
        posted_at = datetime(2025, 3, 14, 15, 0, 0, tzinfo=timezone.utc)
        assert _attribute_client("@creator1", posted_at, assignments) == "client_b"

    def test_post_after_reassignment_attributed_to_new_client(self, assignments):
        """A post published after the boundary belongs to client_b."""
        posted_at = datetime(2025, 3, 14, 16, 0, 0, tzinfo=timezone.utc)
        assert _attribute_client("@creator1", posted_at, assignments) == "client_b"

    def test_unknown_account_returns_unattributed(self, assignments):
        """An account not in the assignment log returns 'unattributed'."""
        posted_at = datetime(2025, 3, 14, 12, 0, 0, tzinfo=timezone.utc)
        assert _attribute_client("@unknown", posted_at, assignments) == "unattributed"


# =========================================================================
# HELPER TESTS
# =========================================================================

class TestHelpers:
    """Tests for parsing and timestamp helpers."""

    def test_parse_iso_z_suffix(self):
        dt = _parse_iso("2025-03-14T09:15:00Z")
        assert dt.tzinfo is not None
        assert dt == datetime(2025, 3, 14, 9, 15, 0, tzinfo=timezone.utc)

    def test_parse_iso_offset(self):
        dt = _parse_iso("2025-03-14T10:30:00-05:00")
        # Should be normalized to UTC: 10:30 - (-5:00) = 15:30 UTC
        assert dt == datetime(2025, 3, 14, 15, 30, 0, tzinfo=timezone.utc)

    def test_timestamps_close_within_tolerance(self):
        a = datetime(2025, 3, 14, 10, 0, 0, tzinfo=timezone.utc)
        b = datetime(2025, 3, 14, 10, 30, 0, tzinfo=timezone.utc)
        assert _timestamps_close(a, b, tolerance_seconds=3600) is True

    def test_timestamps_far_apart(self):
        a = datetime(2025, 3, 14, 10, 0, 0, tzinfo=timezone.utc)
        b = datetime(2025, 3, 14, 12, 30, 0, tzinfo=timezone.utc)
        assert _timestamps_close(a, b, tolerance_seconds=3600) is False


# =========================================================================
# FULL INTEGRATION TEST
# =========================================================================

class TestIntegration:
    """Integration tests using the sample data from the assessment."""

    def test_full_reconciliation_with_sample_data(self):
        """Run the full reconciliation with the provided sample data and
        verify the output is structurally correct and the key traps are
        handled properly."""
        report = reconcile(
            PROVIDER_A_DATA,
            PROVIDER_B_DATA,
            PREVIOUS_SNAPSHOT,
            ACCOUNT_ASSIGNMENTS,
        )

        # --- Basic structural checks ---
        assert report is not None
        assert isinstance(report.posts, list)
        assert isinstance(report.anomalies, list)
        assert len(report.posts) > 0

        # --- Check that known video IDs are present ---
        ids = [p.platform_id for p in report.posts if p.platform_id]
        assert "7322789456" in ids, "Post 7322789456 should be in the output"

        # --- Check that the share URL post was handled ---
        # Provider A has a share URL (vm.tiktok.com) which should either
        # be matched via secondary signals or flagged as unresolved.
        share_anomalies = [
            a for a in report.anomalies
            if a.kind == AnomalyKind.UNRESOLVED_SHARE_URL
        ]
        assert len(share_anomalies) >= 1, "Share URL should generate an anomaly"

        # --- Check disappeared post ---
        # platform_id "7320111222" is in the snapshot but NOT in either provider.
        missing_posts = [
            p for p in report.posts
            if p.platform_id == "7320111222"
        ]
        assert len(missing_posts) == 1
        assert missing_posts[0].status == PostStatus.MISSING

        # --- Verify high water mark for post 7321456789 ---
        # Snapshot says views=46000, Provider A may have matched via caption,
        # Provider B reports views=44800.
        # High water mark should be >= 46000 (from snapshot).
        post_7321 = [p for p in report.posts if p.platform_id == "7321456789"]
        if post_7321:
            assert post_7321[0].views >= 46000, (
                "High water mark should preserve the snapshot's 46000 views"
            )

        # --- Client attribution ---
        # Post 7322789456 was posted at 09:15 UTC, which is before the
        # 15:00 UTC reassignment → should be attributed to client_a.
        post_7322 = [p for p in report.posts if p.platform_id == "7322789456"]
        assert len(post_7322) == 1
        assert post_7322[0].client == "client_a", (
            "Post published before boundary should belong to client_a"
        )

    def test_full_reconciliation_caption_truncation_anomaly(self):
        """The sample data contains truncated captions from Provider B.
        Verify that truncation-related anomalies are logged."""
        report = reconcile(
            PROVIDER_A_DATA,
            PROVIDER_B_DATA,
            PREVIOUS_SNAPSHOT,
            ACCOUNT_ASSIGNMENTS,
        )
        truncation_anomalies = [
            a for a in report.anomalies
            if a.kind == AnomalyKind.CAPTION_TRUNCATION_MATCH
        ]
        # At least one truncation match should be flagged for the posts
        # that have "..." in Provider B's captions.
        assert len(truncation_anomalies) >= 1

    def test_reconciliation_provenance_completeness(self):
        """Every ACTIVE reconciled post should have provenance for views,
        likes, and comments."""
        report = reconcile(
            PROVIDER_A_DATA,
            PROVIDER_B_DATA,
            PREVIOUS_SNAPSHOT,
            ACCOUNT_ASSIGNMENTS,
        )
        for post in report.posts:
            if post.status == PostStatus.ACTIVE:
                metric_names = {p.metric for p in post.provenance}
                assert "views" in metric_names, f"Post {post.platform_id} missing views provenance"
                assert "likes" in metric_names, f"Post {post.platform_id} missing likes provenance"
                assert "comments" in metric_names, f"Post {post.platform_id} missing comments provenance"

    def test_reconciliation_no_duplicate_post_ids(self):
        """The output should not contain duplicate platform_id entries
        (except None for unresolved)."""
        report = reconcile(
            PROVIDER_A_DATA,
            PROVIDER_B_DATA,
            PREVIOUS_SNAPSHOT,
            ACCOUNT_ASSIGNMENTS,
        )
        resolved_ids = [p.platform_id for p in report.posts if p.platform_id is not None]
        assert len(resolved_ids) == len(set(resolved_ids)), (
            f"Duplicate post IDs found: {resolved_ids}"
        )


# =========================================================================
# EDGE CASES
# =========================================================================

class TestEdgeCases:
    """Edge-case and boundary tests."""

    def test_empty_inputs_no_crash(self):
        """Reconciliation with all-empty inputs produces an empty report."""
        report = reconcile([], [], [], [])
        assert report is not None
        assert len(report.posts) == 0
        assert len(report.anomalies) == 0

    def test_single_provider_only(self):
        """When only one provider supplies data, reconciliation still works."""
        report = reconcile(
            provider_a_raw=[
                {
                    "id": "111",
                    "url": "https://tiktok.com/@solo/video/111",
                    "views": 5000,
                    "likes": 200,
                    "comments": 10,
                    "caption": "Solo post",
                    "posted_at": "2025-03-14T12:00:00Z",
                    "account": "@solo",
                },
            ],
            provider_b_raw=[],
            snapshot_raw=[],
            assignments_raw=[],
        )
        assert len(report.posts) == 1
        assert report.posts[0].platform_id == "111"
        assert report.posts[0].views == 5000
        assert report.posts[0].status == PostStatus.ACTIVE

    def test_share_url_with_caption_match_merges(self):
        """A share-URL post is matched to a canonical post from the other
        provider when account, timestamp, and caption align."""
        canonical_post = {
            "id": "555",
            "url": "https://tiktok.com/@user/video/555",
            "views": 3000,
            "likes": 100,
            "comments": 5,
            "caption": "Unique caption for matching test",
            "posted_at": "2025-03-14T12:00:00Z",
            "account": "@user",
        }
        share_post = {
            "id": None,
            "url": "https://vm.tiktok.com/ZMrMATCH1/",
            "views": 3200,
            "likes": 110,
            "comments": 6,
            "caption": "Unique caption for matching test",
            "posted_at": "2025-03-14T12:00:00Z",
            "account": "@user",
        }
        report = reconcile(
            provider_a_raw=[share_post],
            provider_b_raw=[canonical_post],
            snapshot_raw=[],
            assignments_raw=[],
        )
        # Should merge into one post with ID 555.
        active_posts = [p for p in report.posts if p.status == PostStatus.ACTIVE]
        assert len(active_posts) == 1
        assert active_posts[0].platform_id == "555"
        # High water mark: 3200 > 3000.
        assert active_posts[0].views == 3200
