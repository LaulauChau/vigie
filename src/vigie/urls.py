"""URL normalization, so one Item reached through two Sources is stored once."""

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Besides every utm_* parameter.
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "ref",
    "ref_src",
    "_hsenc",
    "_hsmi",
    "mkt_tok",
}


def normalize_url(url: str) -> str:
    """Lowercase scheme and host; drop www., fragment, tracking parameters, trailing slash."""
    parts = urlsplit(url.strip())
    netloc = parts.netloc.lower().removeprefix("www.")
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), netloc, path, urlencode(query), ""))


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode()).hexdigest()
