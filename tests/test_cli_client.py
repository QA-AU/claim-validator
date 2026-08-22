"""AnthropicClient.generate()'s temperature handling — found live that the
installed anthropic SDK (1.0.0+) removed `temperature` from
Messages.create() entirely, so every call that passed one (census.py pins
one to cut sampling variance) failed outright with
"Messages.create() got an unexpected keyword argument 'temperature'"
before ever reaching the network. Mocks anthropic.Anthropic directly,
since AnthropicClient imports it locally inside __init__.
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


def test_temperature_is_never_passed_to_the_real_sdk_call():
    """The installed SDK rejects it outright — passing it through would
    make every census call fail before reaching the network."""
    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MockAnthropic.return_value
        mock_client.messages.create.return_value = _mock_response()

        client = AnthropicClient(model="claude-haiku-4-5")
        client.generate("prompt", temperature=0.01)

        _, kwargs = mock_client.messages.create.call_args
        assert "temperature" not in kwargs


def test_no_temperature_requested_behaves_exactly_as_before():
    with patch("anthropic.Anthropic") as MockAnthropic:
        mock_client = MockAnthropic.return_value
        mock_client.messages.create.return_value = _mock_response()

        client = AnthropicClient(model="claude-haiku-4-5")
        text = client.generate("prompt")

        assert text == "ok"
        _, kwargs = mock_client.messages.create.call_args
        assert "temperature" not in kwargs


def test_a_dropped_temperature_is_logged_not_silent():
    AnthropicClient._warned_temperature_unsupported = False  # isolate from other tests

    with patch("anthropic.Anthropic") as MockAnthropic, \
         patch("phases.cli_client.logger") as mock_logger:
        mock_client = MockAnthropic.return_value
        mock_client.messages.create.return_value = _mock_response()

        client = AnthropicClient(model="claude-haiku-4-5")
        client.generate("prompt", temperature=0.01)

        mock_logger.warning.assert_called_once()

    AnthropicClient._warned_temperature_unsupported = False  # leave clean for other tests


def test_the_warning_fires_once_per_process_not_once_per_call():
    AnthropicClient._warned_temperature_unsupported = False

    with patch("anthropic.Anthropic") as MockAnthropic, \
         patch("phases.cli_client.logger") as mock_logger:
        mock_client = MockAnthropic.return_value
        mock_client.messages.create.return_value = _mock_response()

        client = AnthropicClient(model="claude-haiku-4-5")
        client.generate("prompt", temperature=0.01)
        client.generate("prompt", temperature=0.01)
        client.generate("prompt", temperature=0.01)

        mock_logger.warning.assert_called_once()

    AnthropicClient._warned_temperature_unsupported = False
