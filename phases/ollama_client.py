"""A local Ollama-backed client, matching the one contract every phase module
calls: `generate(prompt, system_prompt=None) -> str`. See phases/llm_clients.py
for the provider registry this can be registered into, and phases/cli_client.py
for the Anthropic client it stands in for.

Ollama's HTTP API needs no key — it's a local server, default
http://localhost:11434. Verify it's up and see what's pulled with:

    curl http://localhost:11434/api/tags
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List

import requests

from phases.llm_usage import UsageTrackingMixin

logger = logging.getLogger(__name__)

DEFAULT_HOST = "http://localhost:11434"
# Ollama has no request timeout of its own; a cold model load can take minutes.
REQUEST_TIMEOUT_S = 600


@dataclass
class LogprobResponse:
    """One generate_with_logprobs() call: the text Ollama sampled, plus the
    probability distribution it actually drew from at each output position.

    `tokens` is Ollama's own response shape verbatim — a list of
    {token, logprob, top_logprobs: [{token, logprob}, ...]}, one entry per
    output token — so a caller reading confidence out of a specific position
    does not need this module to reshape it first.
    """

    text: str
    tokens: List[Dict[str, Any]] = field(default_factory=list)


class OllamaClient(UsageTrackingMixin):
    """Wraps a local Ollama model in the interface phases/ already depends on."""

    def __init__(self, model: str, host: str = None):
        self.model = model
        self.host = (host or os.getenv("OLLAMA_HOST") or DEFAULT_HOST).rstrip("/")

    def generate(self, prompt: str, system_prompt: str = None, temperature: float = None) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if temperature is not None:
            payload["options"] = {"temperature": temperature}

        response = requests.post(
            f"{self.host}/api/generate", json=payload, timeout=REQUEST_TIMEOUT_S
        )
        response.raise_for_status()
        data = response.json()

        # Same fields Anthropic reports as input/output tokens, named
        # differently here. Recorded through `record`, not `record_response`,
        # since that method expects an object with a `.usage` attribute rather
        # than this dict shape.
        self.usage.record(data.get("prompt_eval_count"), data.get("eval_count"))

        text = data.get("response", "")
        if not text:
            raise ValueError(f"reply carried no text (keys: {sorted(data.keys())})")
        return text

    def generate_with_logprobs(
        self,
        prompt: str,
        system_prompt: str = None,
        temperature: float = None,
        top_logprobs: int = 5,
    ) -> LogprobResponse:
        """Same call as `generate`, but also asks for the probability
        distribution behind each output token, not just the text it sampled.

        No Anthropic counterpart: the Messages API the other provider in
        this project calls does not expose token probabilities as of this
        writing. Callers that want this as an optional upgrade check
        `hasattr(llm_client, "generate_with_logprobs")` rather than the
        provider name, so an unsupported client is skipped, not broken.
        """
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "logprobs": True,
            "top_logprobs": top_logprobs,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if temperature is not None:
            payload["options"] = {"temperature": temperature}

        response = requests.post(
            f"{self.host}/api/generate", json=payload, timeout=REQUEST_TIMEOUT_S
        )
        response.raise_for_status()
        data = response.json()

        self.usage.record(data.get("prompt_eval_count"), data.get("eval_count"))

        text = data.get("response", "")
        if not text:
            raise ValueError(f"reply carried no text (keys: {sorted(data.keys())})")
        return LogprobResponse(text=text, tokens=data.get("logprobs") or [])
