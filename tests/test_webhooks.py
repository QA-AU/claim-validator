"""claimvalidator/webhooks.py's scheme allowlist. webhook_url is
caller-supplied and this module makes an outbound request to it on the
caller's behalf — found unguarded (any scheme accepted) in a vulnerability
scan. Only the scheme is checked here; see webhooks.py's module docstring
for what this deliberately doesn't cover.
"""

from unittest.mock import patch

from claimvalidator.webhooks import deliver


def test_a_file_url_is_rejected_without_any_request_being_made():
    with patch("claimvalidator.webhooks.requests.post") as post:
        result = deliver("file:///etc/passwd", {"job_id": "j1"})
    assert result is False
    post.assert_not_called()


def test_a_gopher_url_is_rejected():
    with patch("claimvalidator.webhooks.requests.post") as post:
        deliver("gopher://internal-host:70/x", {"job_id": "j1"})
    post.assert_not_called()


def test_an_https_url_is_allowed_through():
    with patch("claimvalidator.webhooks.requests.post") as post:
        post.return_value.ok = True
        result = deliver("https://example.com/hook", {"job_id": "j1"})
    assert result is True
    post.assert_called_once()


def test_an_http_url_is_allowed_through():
    with patch("claimvalidator.webhooks.requests.post") as post:
        post.return_value.ok = True
        result = deliver("http://example.com/hook", {"job_id": "j1"})
    assert result is True
    post.assert_called_once()
