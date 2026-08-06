"""Deduplication helpers for the research engine.

Two distinct kinds of duplicate cost money on a run:

* the *same URL* surfacing in the SERPs of neighbouring branches — a second
  Chromium tab and a second ~2000-token page for content already in hand;
* *different URLs carrying the same text* — a wire story republished by five
  outlets, each one sent to the model in full.

`normalize_url` addresses the first (URLs that differ only by tracking noise are
the same page), `dedupe_contents` the second (shingle similarity).
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never change which page you land on.
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "msclkid", "ref", "ref_src", "mc_cid"})
_TRACKING_PREFIXES = ("utm_",)


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in _TRACKING_PARAMS or lowered.startswith(_TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """Canonical form used to decide whether two URLs are the same page.

    Lowercases the scheme and host, drops the fragment, drops tracking
    parameters and strips the trailing slash of the path. Deliberately
    conservative: everything else (query order, case of the path, port) is left
    alone, because those *can* select a different document.
    """
    raw = url.strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if not parts.netloc:  # not an absolute URL; nothing safe to normalize
        return raw.rstrip("/")

    pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = urlencode([(k, v) for k, v in pairs if not _is_tracking_param(k)])
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


# -- Near-duplicate page detection -----------------------------------------

_SHINGLE_SIZE = 5


def _shingles(text: str, size: int = _SHINGLE_SIZE) -> frozenset[int]:
    """Hashed sliding word n-grams of a whitespace-collapsed, lowercased text."""
    words = text.lower().split()
    if len(words) < size:
        # Too short to shingle: hash the whole thing so identical stubs still match.
        return frozenset({hash(" ".join(words))}) if words else frozenset()
    return frozenset(hash(" ".join(words[i : i + size])) for i in range(len(words) - size + 1))


def _jaccard(a: frozenset[int], b: frozenset[int]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def dedupe_contents(contents: list[str], *, threshold: float = 0.8) -> tuple[list[int], list[int]]:
    """Split page contents into (kept indices, dropped near-duplicate indices).

    Greedy and order-preserving: the first occurrence wins, later pages sharing
    more than ``threshold`` of their shingles with a kept page are dropped.
    Indices are returned rather than the texts so the caller can filter the
    parallel URL list the same way.
    """
    kept: list[int] = []
    dropped: list[int] = []
    kept_shingles: list[frozenset[int]] = []

    for index, content in enumerate(contents):
        fingerprint = _shingles(content)
        duplicate = fingerprint and any(
            _jaccard(fingerprint, other) >= threshold for other in kept_shingles
        )
        if duplicate:
            dropped.append(index)
            continue
        kept.append(index)
        kept_shingles.append(fingerprint)
    return kept, dropped
