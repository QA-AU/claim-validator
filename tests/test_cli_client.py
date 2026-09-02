"""AnthropicClient.generate()'s temperature handling.

The installed anthropic SDK (1.0.0+) dropped `temperature` from
Messages.create()'s typed signature — confirmed by introspecting the real
method, not assumed. The first fix for that (warn once, silently drop the
request) was itself wrong: the underlying Messages API still accepts
temperature, and the SDK's own `extra_body` passes arbitrary fields
straight into the request body, bypassing the incomplete typed parameter
list. Verified live against the real API — a request with
extra_body={"temperature": ...} succeeds normally — before relying on it
here. Mocks anthropic.Anthropic directly, since AnthropicClient imports it
locally inside __init__.
"""

from unittest.mock import MagicMock, patch

from phases.cli_client import AnthropicClient


def _mock_response(text="ok"):
    response = MagicMock()
    block = MagicMock()
    block.type = "text"
    block.text = text
    response.content = [block]
    response.usage = None
    return response


def test_a_requested_temperature_reaches_the_real_call_via_extra_body():
    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MockAnthropic.return_value
        mock_client.messages.create.return_value = _mock_response()

        client = AnthropicClient(model="claude-haiku-4-5")
        client.generate("prompt", temperature=0.01)

        _, kwargs = mock_client.messages.create.call_args
        # Not a top-level kwarg — the SDK's typed signature doesn't have
        # one — but present inside extra_body, which does reach the API.
        assert "temperature" not in kwargs
        assert kwargs["extra_body"] == {"temperature": 0.01}


def test_no_temperature_requested_sends_no_extra_body():
    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MockAnthropic.return_value
        mock_client.messages.create.return_value = _mock_response()

        client = AnthropicClient(model="claude-haiku-4-5")
        text = client.generate("prompt")

        assert text == "ok"
        _, kwargs = mock_client.messages.create.call_args
        assert "extra_body" not in kwargs
