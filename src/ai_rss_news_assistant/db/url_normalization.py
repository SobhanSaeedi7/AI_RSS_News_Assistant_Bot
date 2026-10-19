"""URL normalization helpers for news deduplication.

Canonicalizes article URLs so that the same article arriving through
different feeds can be detected as a duplicate, without collapsing
potentially distinct article URLs.

Normalization is conservative:
* Scheme and hostname are lowercased.
* Default ports (http:80, https:443) are removed.
* Fragments are removed.
* Tracking-only query parameters are removed.
* All other query parameters, path, and hostname are preserved.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that are tracking-only and safe to remove for dedup.
# These are never part of a page's canonical identity.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
    "_ga",
    "_gl",
    "mc_cid",
    "mc_eid",
}

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def normalize_url(url: str) -> str:
    """Return a canonical form of ``url`` for deduplication.

    Raises ``ValueError`` if ``url`` is empty or not an absolute URL.
    """
    if not url or not url.strip():
        raise ValueError("url must not be empty")

    parts = urlsplit(url.strip())
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"url must be absolute: {url!r}")

    scheme = parts.scheme.lower()
    hostname = parts.hostname.lower()

    port = parts.port
    if port is not None:
        default_port = _DEFAULT_PORTS.get(scheme)
        if default_port is not None and str(port) == default_port:
            port = None

    netloc = hostname
    if port is not None:
        netloc = f"{hostname}:{port}"

    # Remove tracking-only query parameters; preserve everything else.
    params = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    query = urlencode(params)

    return urlunsplit((scheme, netloc, parts.path, query, ""))
