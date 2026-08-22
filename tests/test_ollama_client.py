"""OllamaClient's rate-limit retry — added after a real run tripped a 429
from Ollama's cloud proxy on a dense sequence of census batches and had no
way to recover, silently corrupting the census spread those batches fed
(see phases/census.py's own test for the other half of that fix).

Mocks requests.post and time.sleep directly rather than a real server, since
what's under test is the retry/backoff logic itself, not the HTTP layer.
"""

import json
from unittest.mock import Mock, patch

import pytest

from phases.ollama_client import OllamaClient, RATE_LIMIT_MAX_RETRIES


def _response(status_code, body=None, headers=None):
    resp = Mock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = body or {"response": "ok"}
    if status_code == 429:
        import requests
        resp.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"{status_code} Client Error"
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


def test_a_single_429_recovers_on_retry():
    responses = [_response(429), _response(200, {"response": "ENTAILS"})]

    with patch("phases.ollama_client.requests.post", side_effect=responses) as post, \
         patch("phases.ollama_client.time.sleep") as sleep:
        client = OllamaClient(model="test-model")
        text = client.generate("prompt")

    assert text == "ENTAILS"
    assert post.call_count == 2
    sleep.assert_called_once()


def test_exhausting_every_retry_still_raises():
    import requests as requests_module

    responses = [_response(429)] * (RATE_LIMIT_MAX_RETRIES + 1)

    with patch("phases.ollama_client.requests.post", side_effect=responses) as post, \
         patch("phases.ollama_client.time.sleep"):
        client = OllamaClient(model="test-model")
        with pytest.raises(requests_module.exceptions.HTTPError):
            client.generate("prompt")

    assert post.call_count == RATE_LIMIT_MAX_RETRIES + 1


def test_a_non_rate_limit_error_raises_immediately_without_retrying():
    resp = Mock()
    resp.status_code = 500
    resp.headers = {}
    import requests as requests_module
    resp.raise_for_status.side_effect = requests_module.exceptions.HTTPError("500 Server Error")

    with patch("phases.ollama_client.requests.post", return_value=resp) as post, \
         patch("phases.ollama_client.time.sleep") as sleep:
        client = OllamaClient(model="test-model")
        with pytest.raises(requests_module.exceptions.HTTPError):
            client.generate("prompt")

    assert post.call_count == 1   # no retry for a non-429 failure
    sleep.assert_not_called()


def test_a_retry_after_header_is_honoured_over_the_default_backoff():
    responses = [_response(429, headers={"Retry-After": "17"}),
                 _response(200, {"response": "ok"})]

    with patch("phases.ollama_client.requests.post", side_effect=responses), \
         patch("phases.ollama_client.time.sleep") as sleep:
        client = OllamaClient(model="test-model")
        client.generate("prompt")

    sleep.assert_called_once_with(17.0)


def test_success_on_the_first_try_never_sleeps():
    with patch("phases.ollama_client.requests.post", return_value=_response(200, {"response": "ok"})), \
         patch("phases.ollama_client.time.sleep") as sleep:
        client = OllamaClient(model="test-model")
        client.generate("prompt")

    sleep.assert_not_called()
