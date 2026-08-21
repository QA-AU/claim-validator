"""Test fixtures and utilities."""

import pytest


@pytest.fixture(autouse=True)
def _fixed_api_token(monkeypatch):
    monkeypatch.setenv("CLAIMVAL_API_TOKEN", "test-suite-token")
    from phases.api_auth import reset_token
    reset_token("test-suite-token")
    yield
    reset_token(None)


class FakeLLMClient:
    """Fake LLM client for deterministic testing.

    `generate(prompt: str, system_prompt: str = None)` — the actual contract
    every real client in this repo implements. The source repo's own fixture
    of this name typed the first parameter `messages: list[dict]`, a stale
    name from before the pipeline settled on a single prompt string; fixed
    here since positional calls masked it there but there's no reason to
    carry the confusing name forward into a fresh repo.
    """

    def __init__(self, responses: list[str]):
        self.responses = responses
        self.call_count = 0

    def generate(self, prompt: str, system_prompt: str = None) -> str:
        if self.call_count >= len(self.responses):
            raise RuntimeError(f"No more scripted responses (tried {self.call_count + 1})")
        response = self.responses[self.call_count]
        self.call_count += 1
        return response
