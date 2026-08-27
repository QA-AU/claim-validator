"""Best-effort webhook delivery — same guarded philosophy as `RunTracker`:
a delivery failure must never take down the job it's reporting on.

`webhook_url` is caller-supplied (ValidationRequest.webhook_url) and this
is an outbound request the server makes on the caller's behalf — an SSRF
surface. Two checks run before every attempt:

- Scheme: only http/https. Rules out the file://, gopher://, and similar
  tricks that turn "post my job result somewhere" into "read a local
  file" or "speak a raw protocol at whatever's listening on some port."
- Target address: the hostname is resolved and every IP it comes back
  with is checked against the private/loopback/link-local/reserved
  ranges — the ones that matter most here being anything that could
  reach the Container App's own instance metadata endpoint or another
  service inside the same environment.

### What this doesn't cover

The address check re-resolves on every retry (cheap, and it narrows the
window), but `requests.post()` still does its own resolution when it
actually connects a moment later — a DNS answer that changes between our
check and that connection (classic DNS rebinding) would slip through. Full
protection against that needs pinning the connection to the exact IP this
function validated, which means a custom transport adapter — real added
complexity for a threat model where the far more common case is simply "a
caller pastes a literal internal URL," which this already stops cold.
"""

import ipaddress
import logging
import socket
from typing import Any, Dict, Union
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

TIMEOUT_S = 10
MAX_ATTEMPTS = 3
ALLOWED_SCHEMES = frozenset({"http", "https"})

IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]


def _is_disallowed_ip(ip: IPAddress) -> bool:
    # An IPv4-mapped IPv6 address (::ffff:127.0.0.1) reads as a normal,
    # non-loopback IPv6Address unless it's unwrapped first — .is_loopback
    # etc. only recognise ::1, not the IPv4 address it's carrying.
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _rejection_reason(url: str) -> str:
    """Empty string if `url` is safe to request; otherwise why it isn't."""
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return f"scheme {parts.scheme!r} is not allowed (only http/https)"

    hostname = parts.hostname
    if not hostname:
        return "no hostname in URL"

    try:
        resolved = {info[4][0] for info in socket.getaddrinfo(hostname, None)}
    except socket.gaierror as e:
        return f"could not resolve host {hostname!r}: {e}"

    for ip_str in resolved:
        if _is_disallowed_ip(ipaddress.ip_address(ip_str)):
            return f"host {hostname!r} resolves to a private/reserved address ({ip_str})"

    return ""


def deliver(url: str, payload: Dict[str, Any]) -> bool:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        reason = _rejection_reason(url)
        if reason:
            logger.error(f"Webhook {url!r} rejected: {reason}")
            return False

        try:
            response = requests.post(url, json=payload, timeout=TIMEOUT_S)
            if response.ok:
                return True
            logger.warning(f"Webhook {url} returned {response.status_code} (attempt {attempt})")
        except requests.RequestException as e:
            logger.warning(f"Webhook {url} failed (attempt {attempt}): {e}")
    logger.error(f"Webhook {url} gave up after {MAX_ATTEMPTS} attempts")
    return False
