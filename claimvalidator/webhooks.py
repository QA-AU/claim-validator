"""Best-effort webhook delivery — same guarded philosophy as `RunTracker`:
a delivery failure must never take down the job it's reporting on.

`webhook_url` is caller-supplied (ValidationRequest.webhook_url) and this
is an outbound request the server makes on the caller's behalf — an SSRF
surface. ALLOWED_SCHEMES rules out the non-http(s) tricks (file://,
gopher://, and friends being the classic ones) that turn "post my job
result somewhere" into "read a local file" or "speak a raw protocol at
whatever's listening on some port." It does not rule out an http(s) URL
that targets a private/link-local address — see the module docstring note
below `_is_disallowed_scheme` for why that's a separate, larger decision
this doesn't make on its own.
"""

import logging
from typing import Any, Dict
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

TIMEOUT_S = 10
MAX_ATTEMPTS = 3
ALLOWED_SCHEMES = frozenset({"http", "https"})


def _is_disallowed_scheme(url: str) -> bool:
    return urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES


def deliver(url: str, payload: Dict[str, Any]) -> bool:
    if _is_disallowed_scheme(url):
        logger.error(f"Webhook {url!r} rejected: only http/https URLs are allowed")
        return False

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.post(url, json=payload, timeout=TIMEOUT_S)
            if response.ok:
                return True
            logger.warning(f"Webhook {url} returned {response.status_code} (attempt {attempt})")
        except requests.RequestException as e:
            logger.warning(f"Webhook {url} failed (attempt {attempt}): {e}")
    logger.error(f"Webhook {url} gave up after {MAX_ATTEMPTS} attempts")
    return False
