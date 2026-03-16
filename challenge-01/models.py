"""
Data models for the reconciliation engine.

All domain objects are plain dataclasses so the module has zero
third-party dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class PostStatus(Enum):
    """Lifecycle status of a reconciled post."""
    ACTIVE = "active"
    MISSING = "missing"           # Was in a previous snapshot but absent today
    UNRESOLVED_URL = "unresolved" # Share URL that could not be canonicalized


class AnomalyKind(Enum):
    """Categories of anomalies the engine can detect."""
    VIEW_DECREASE = "view_decrease"
    METRIC_CONFLICT = "metric_conflict"
    UNRESOLVED_SHARE_URL = "unresolved_share_url"
    DISAPPEARED_POST = "disappeared_post"
    POSSIBLE_OUTAGE = "possible_outage"
    CAPTION_TRUNCATION_MATCH = "caption_truncation_match"
    CAPTION_AMBIGUITY = "caption_ambiguity"


@dataclass
class ProviderPost:
    """A post as reported by a single provider (A or B)."""
    id: Optional[str]
    url: str
    views: int
    likes: int
    comments: int
    caption: str
    posted_at: str          # ISO-8601 string, parsed later
    account: str
    source: str = ""        # "provider_a" | "provider_b"


@dataclass
class SnapshotRecord:
    """A metric snapshot from a previous reconciliation run."""
    platform_id: str
    views: int
    likes: int
    scraped_at: str
    source: str


@dataclass
class AccountAssignment:
    """Maps an account to a client for a time range."""
    account: str
    client: str
    valid_from: datetime
    valid_to: Optional[datetime]  # None means "current / open-ended"


@dataclass
class Anomaly:
    """A single anomaly detected during reconciliation."""
    kind: AnomalyKind
    post_id: Optional[str]
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class MetricProvenance:
    """Tracks which provider reported which value for a given metric."""
    metric: str              # "views", "likes", "comments"
    provider_a_value: Optional[int] = None
    provider_b_value: Optional[int] = None
    snapshot_value: Optional[int] = None
    resolved_value: int = 0  # The high-water-mark winner


@dataclass
class ReconciledPost:
    """The final reconciled representation of a post."""
    platform_id: Optional[str]
    canonical_url: Optional[str]
    views: int
    likes: int
    comments: int
    caption: str                        # Best (longest) caption available
    posted_at: datetime
    account: str
    client: str                         # Attributed client
    status: PostStatus
    provenance: list[MetricProvenance] = field(default_factory=list)
    anomalies: list[Anomaly] = field(default_factory=list)
    raw_urls: list[str] = field(default_factory=list)


@dataclass
class ReconciliationReport:
    """Top-level output of a reconciliation run."""
    posts: list[ReconciledPost]
    anomalies: list[Anomaly]
    unresolved_share_urls: list[str]
