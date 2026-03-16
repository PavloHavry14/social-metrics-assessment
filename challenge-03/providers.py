"""Provider API client abstraction.

Each provider returns a list of post metrics for an account. Providers may
raise rate-limit errors (HTTP 429), time out, or return partial data. The
caller is responsible for retry orchestration — this module only handles
single-call semantics.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class ProviderError(Exception):
    """Base exception for provider-related failures."""


class RateLimitError(ProviderError):
    """The provider returned HTTP 429 — back off and retry."""

    def __init__(self, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(
            f"Rate limited (retry_after={retry_after}s)"
            if retry_after
            else "Rate limited"
        )


class ProviderTimeoutError(ProviderError):
    """The provider did not respond within the configured timeout."""


class ProviderUnavailableError(ProviderError):
    """The provider returned a 5xx or is otherwise unreachable."""


# -------------------------------------------------------------------------
# Data transfer objects
# -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PostMetrics:
    """Immutable snapshot of metrics for a single post as returned by a provider."""

    platform_post_id: str
    views: int
    likes: int
    comments: int
    shares: int
    published_at: str | None = None  # ISO-8601, if the provider supplies it


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    """Complete response from a single provider call."""

    provider_name: str
    account_handle: str
    posts: list[PostMetrics]
    fetched_at: float = field(default_factory=time.time)
    is_complete: bool = True  # False if the provider timed out mid-page


# -------------------------------------------------------------------------
# Provider protocol and implementations
# -------------------------------------------------------------------------


class MetricsProvider(Protocol):
    """Protocol that every concrete provider adapter must satisfy."""

    @property
    def name(self) -> str:
        """Unique provider identifier (e.g. 'provider_a')."""
        ...

    async def fetch_account_metrics(
        self,
        account_handle: str,
        *,
        timeout: float = 30.0,
    ) -> ProviderResponse:
        """Fetch all post metrics for *account_handle*.

        Raises:
            RateLimitError: on HTTP 429.
            ProviderTimeoutError: if the call exceeds *timeout* seconds.
            ProviderUnavailableError: on 5xx or connection failure.
        """
        ...


class ProviderA:
    """Adapter for Provider A's API."""

    name: str = "provider_a"

    def __init__(self, base_url: str = "https://api.provider-a.example.com") -> None:
        self.base_url = base_url

    async def fetch_account_metrics(
        self,
        account_handle: str,
        *,
        timeout: float = 30.0,
    ) -> ProviderResponse:
        """Fetch post metrics from Provider A.

        In production this would use aiohttp/httpx. Here we define the
        contract; the actual HTTP call is a thin wrapper around the
        provider's REST endpoint.

        Raises:
            RateLimitError: on HTTP 429.
            ProviderTimeoutError: on timeout.
            ProviderUnavailableError: on 5xx.
        """
        # Production implementation sketch:
        #
        # async with httpx.AsyncClient(timeout=timeout) as client:
        #     resp = await client.get(
        #         f"{self.base_url}/accounts/{account_handle}/posts"
        #     )
        #     if resp.status_code == 429:
        #         raise RateLimitError(
        #             retry_after=float(resp.headers.get("Retry-After", 1))
        #         )
        #     if resp.status_code >= 500:
        #         raise ProviderUnavailableError(resp.text)
        #     resp.raise_for_status()
        #     data = resp.json()
        #     posts = [PostMetrics(**p) for p in data["posts"]]
        #     return ProviderResponse(
        #         provider_name=self.name,
        #         account_handle=account_handle,
        #         posts=posts,
        #     )
        raise NotImplementedError("Wire up real HTTP client in production")


class ProviderB:
    """Adapter for Provider B's API."""

    name: str = "provider_b"

    def __init__(self, base_url: str = "https://api.provider-b.example.com") -> None:
        self.base_url = base_url

    async def fetch_account_metrics(
        self,
        account_handle: str,
        *,
        timeout: float = 30.0,
    ) -> ProviderResponse:
        """Fetch post metrics from Provider B.

        Same contract as ProviderA — see its docstring for error semantics.
        """
        raise NotImplementedError("Wire up real HTTP client in production")


# -------------------------------------------------------------------------
# Reconciliation
# -------------------------------------------------------------------------


class ReconciliationStrategy(str, Enum):
    """How to merge results when both providers report metrics for the
    same platform_post_id."""

    MAX = "max"      # Take the higher value per metric field.
    LATEST = "latest"  # Prefer the response fetched more recently.


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Outcome of merging two provider responses."""

    merged_posts: list[PostMetrics]
    only_in_a: list[str]  # platform_post_ids exclusive to provider A
    only_in_b: list[str]  # platform_post_ids exclusive to provider B
    overlap_count: int


def reconcile(
    response_a: ProviderResponse,
    response_b: ProviderResponse,
    strategy: ReconciliationStrategy = ReconciliationStrategy.MAX,
) -> ReconciliationResult:
    """Merge two provider responses for the same account.

    For posts seen by both providers the *strategy* determines which metric
    values win. Posts seen by only one provider are included as-is.

    Args:
        response_a: Response from provider A.
        response_b: Response from provider B.
        strategy: Merge strategy for overlapping posts.

    Returns:
        A ReconciliationResult with the merged post list and set-difference
        diagnostics.
    """
    index_a: dict[str, PostMetrics] = {p.platform_post_id: p for p in response_a.posts}
    index_b: dict[str, PostMetrics] = {p.platform_post_id: p for p in response_b.posts}

    all_ids = set(index_a) | set(index_b)
    only_a = sorted(set(index_a) - set(index_b))
    only_b = sorted(set(index_b) - set(index_a))
    overlap = set(index_a) & set(index_b)

    merged: list[PostMetrics] = []
    for pid in sorted(all_ids):
        pa = index_a.get(pid)
        pb = index_b.get(pid)

        if pa and not pb:
            merged.append(pa)
        elif pb and not pa:
            merged.append(pb)
        else:
            # Both providers have this post — merge according to strategy.
            assert pa is not None and pb is not None
            if strategy == ReconciliationStrategy.MAX:
                merged.append(
                    PostMetrics(
                        platform_post_id=pid,
                        views=max(pa.views, pb.views),
                        likes=max(pa.likes, pb.likes),
                        comments=max(pa.comments, pb.comments),
                        shares=max(pa.shares, pb.shares),
                        published_at=pa.published_at or pb.published_at,
                    )
                )
            elif strategy == ReconciliationStrategy.LATEST:
                winner = pa if response_a.fetched_at >= response_b.fetched_at else pb
                merged.append(winner)

    return ReconciliationResult(
        merged_posts=merged,
        only_in_a=only_a,
        only_in_b=only_b,
        overlap_count=len(overlap),
    )
