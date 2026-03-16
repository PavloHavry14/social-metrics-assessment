"""
URL normalization utilities for TikTok post URLs.

Key design decision — share URLs vs canonical URLs:

    Canonical URLs contain the video ID in the path:
        https://www.tiktok.com/@user/video/7321456789
        https://tiktok.com/@user/video/7321456789

    Share URLs are short-links that *redirect* to canonical URLs:
        https://vm.tiktok.com/ZMrABC123/
        https://vt.tiktok.com/ZSrXYZ789/

    Share URLs do NOT contain the video ID. Resolving them would
    require an HTTP request (following the redirect), which this
    module explicitly avoids.  Instead, share URLs are flagged as
    "unresolved" and the reconciler attempts to match them to
    canonical posts via secondary signals (account, timestamp,
    caption similarity).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class URLKind(Enum):
    CANONICAL = "canonical"
    SHARE = "share"
    UNKNOWN = "unknown"


# Patterns for canonical TikTok video URLs.
# Captures the numeric video ID from paths like /video/7321456789
_CANONICAL_RE = re.compile(
    r"(?:https?://)?(?:www\.)?tiktok\.com/@[\w.]+/video/(\d+)",
    re.IGNORECASE,
)

# Patterns for known share/short-link domains.
_SHARE_DOMAINS = {"vm.tiktok.com", "vt.tiktok.com"}
_SHARE_RE = re.compile(
    r"(?:https?://)?(vm|vt)\.tiktok\.com/[\w]+/?",
    re.IGNORECASE,
)


@dataclass
class NormalizedURL:
    """Result of normalizing a TikTok URL."""
    original: str
    kind: URLKind
    video_id: Optional[str]       # Extracted numeric ID, if canonical
    canonical_url: Optional[str]  # Rebuilt canonical URL, if possible


def normalize_url(url: str) -> NormalizedURL:
    """Normalize a TikTok URL, extracting the video ID when possible.

    For canonical URLs the video ID is extracted via regex.
    For share URLs the video ID is NOT available without an HTTP
    redirect, so we return kind=SHARE with video_id=None.

    Returns:
        NormalizedURL with classification and extracted data.
    """
    url = url.strip()

    # Try canonical first.
    m = _CANONICAL_RE.search(url)
    if m:
        video_id = m.group(1)
        return NormalizedURL(
            original=url,
            kind=URLKind.CANONICAL,
            video_id=video_id,
            canonical_url=_build_canonical(url, video_id),
        )

    # Try share URL.
    if _SHARE_RE.match(url):
        return NormalizedURL(
            original=url,
            kind=URLKind.SHARE,
            video_id=None,
            canonical_url=None,
        )

    return NormalizedURL(
        original=url,
        kind=URLKind.UNKNOWN,
        video_id=None,
        canonical_url=None,
    )


def extract_video_id(url: str) -> Optional[str]:
    """Extract the numeric video ID from a canonical TikTok URL.

    Returns None if the URL is a share link or unrecognised format.
    """
    m = _CANONICAL_RE.search(url)
    return m.group(1) if m else None


def is_share_url(url: str) -> bool:
    """Return True if the URL is a known TikTok share/short-link."""
    return bool(_SHARE_RE.match(url.strip()))


def _build_canonical(original_url: str, video_id: str) -> str:
    """Rebuild a consistent canonical URL from a matched canonical URL.

    Extracts the @username and constructs a normalized form:
        https://www.tiktok.com/@user/video/<id>
    """
    user_match = re.search(r"@([\w.]+)", original_url)
    if user_match:
        username = user_match.group(1)
        return f"https://www.tiktok.com/@{username}/video/{video_id}"
    # Fallback: return the original if we can't extract the username.
    return original_url
