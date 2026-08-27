"""claimvalidator/webhooks.py's SSRF guards: the scheme allowlist and the
resolved-IP check. webhook_url is caller-supplied and this module makes an
outbound request to it on the caller's behalf — found unguarded (any
scheme, any target) in a vulnerability scan. DNS is mocked throughout so
these stay hermetic; see webhooks.py's module docstring for what the IP
check deliberately doesn't cover (DNS rebinding against the later, real
connection).
"""

from unittest.mock import patch

from claimvalidator.webhooks import deliver


def _addrinfo(ip: str, family=2):
    # Real getaddrinfo() tuples: (family, type, proto, canonname, sockaddr).
    # IPv4 sockaddr is (ip, port); this fixture only exercises IPv4 targets.
    return [(family, 1, 6, "", (ip, 0))]


def test_a_file_url_is_rejected_without_any_request_or_lookup():
    with patch("claimvalidator.webhooks.socket.getaddrinfo") as lookup, \
         patch("claimvalidator.webhooks.requests.post") as post:
        result = deliver("file:///etc/passwd", {"job_id": "j1"})
    assert result is False
    lookup.assert_not_called()
    post.assert_not_called()


def test_a_gopher_url_is_rejected():
    with patch("claimvalidator.webhooks.requests.post") as post:
        deliver("gopher://internal-host:70/x", {"job_id": "j1"})
    post.assert_not_called()


def test_an_https_url_resolving_to_a_public_ip_is_allowed_through():
    with patch("claimvalidator.webhooks.socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")), \
         patch("claimvalidator.webhooks.requests.post") as post:
        post.return_value.ok = True
        result = deliver("https://example.com/hook", {"job_id": "j1"})
    assert result is True
    post.assert_called_once()


def test_a_url_resolving_to_the_azure_imds_address_is_rejected():
    with patch("claimvalidator.webhooks.socket.getaddrinfo", return_value=_addrinfo("169.254.169.254")), \
         patch("claimvalidator.webhooks.requests.post") as post:
        result = deliver("http://metadata.internal/steal", {"job_id": "j1"})
    assert result is False
    post.assert_not_called()


def test_a_url_resolving_to_loopback_is_rejected():
    with patch("claimvalidator.webhooks.socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")), \
         patch("claimvalidator.webhooks.requests.post") as post:
        result = deliver("http://localhost.attacker.example/x", {"job_id": "j1"})
    assert result is False
    post.assert_not_called()


def test_a_url_resolving_to_a_private_range_is_rejected():
    with patch("claimvalidator.webhooks.socket.getaddrinfo", return_value=_addrinfo("10.0.0.5")), \
         patch("claimvalidator.webhooks.requests.post") as post:
        result = deliver("http://internal-svc.example/x", {"job_id": "j1"})
    assert result is False
    post.assert_not_called()


def test_an_ipv4_mapped_ipv6_loopback_is_still_caught():
    with patch("claimvalidator.webhooks.socket.getaddrinfo",
               return_value=_addrinfo("::ffff:127.0.0.1", family=10)), \
         patch("claimvalidator.webhooks.requests.post") as post:
        result = deliver("http://sneaky.example/x", {"job_id": "j1"})
    assert result is False
    post.assert_not_called()


def test_an_unresolvable_host_is_rejected_not_left_to_requests():
    import socket as socket_module

    with patch("claimvalidator.webhooks.socket.getaddrinfo",
               side_effect=socket_module.gaierror("nope")), \
         patch("claimvalidator.webhooks.requests.post") as post:
        result = deliver("http://this-does-not-resolve.invalid/x", {"job_id": "j1"})
    assert result is False
    post.assert_not_called()
