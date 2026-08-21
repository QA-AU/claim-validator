"""The model client the CLIs share.

There were three near-identical copies of this wrapper — one per phase — and
both bugs in it had been copied along with it:

  * `default_model(cheap_tier())` was called without importing either name, so
    every CLI raised `NameError` unless `ANTHROPIC_MODEL` happened to be set.
  * `generate()` declared `messages: list[dict]`, while every caller in
    `phases/` passes a prompt **string**. The wrappers had drifted from the
    interface the pipeline settled on, and nothing caught it because no test
    exercises a CLI wrapper against a real call.

That is the same failure as the first live Phase 2 run: an interface mismatch
that unit tests cannot see, because each side is internally consistent. One
shared implementation means the next such fix happens once.
"""

import logging
import os

from phase1_model_config import cheap_tier, default_model
from phases.llm_usage import UsageTrackingMixin

logger = logging.getLogger(__name__)

# Raised from 4096 after the first live Phase 3 run: generating several test
# bodies in one reply ran past the old ceiling and the JSON came back cut in
# half, losing the whole batch. Phase 3 also batches its calls — this is the
# second line of defence, not the fix.
MAX_TOKENS = 8192


def _text_of(response) -> str:
    """The text of a reply, ignoring blocks that are not text.

    `response.content[0].text` was fine until the first call to a model that
    returns extended thinking: block 0 is then a `ThinkingBlock`, which has no
    `.text`, and every call raised `'ThinkingBlock' object has no attribute
    'text'`. It surfaced when the entailment judge escalated to a stronger tier
    and lost all three of its escalation batches — but nothing about it is
    specific to that phase. Any phase pointed at a thinking-capable model would
    have failed the same way, on every call.

    Text blocks are joined rather than taking the first, since a reply split
    across blocks would otherwise be silently truncated.
    """
    blocks = getattr(response, "content", None) or []
    text = "".join(
        block.text
        for block in blocks
        if getattr(block, "type", None) == "text" and hasattr(block, "text")
    )
    if text:
        return text

    # No text at all. Raising makes this a counted, visible failure; returning
    # "" would drop the batch's requirements out of the report without saying so.
    kinds = ", ".join(sorted({getattr(b, "type", "?") for b in blocks})) or "none"
    raise ValueError(f"reply carried no text block (blocks: {kinds})")


class AnthropicClient(UsageTrackingMixin):
    """Wraps the Anthropic SDK in the `generate(prompt, system_prompt=None)`
    interface that every module in `phases/` calls.

    Tracks tokens because call counts are a poor proxy for spend: a census call
    returns a list of names while an extraction call returns full instances with
    attributes, so two calls can differ several-fold in output tokens. Comparing
    approaches on call count alone produced a wrong conclusion once already
    (todo/14), and the fix was to measure.
    """

    def __init__(self, model: str = None, max_tokens: int = MAX_TOKENS):
        from anthropic import Anthropic

        self.client = Anthropic()
        # No model id is hardcoded here — the tier is resolved by
        # phase1_model_config, so changing models is a config edit.
        self.model = model or os.getenv("ANTHROPIC_MODEL") or default_model(cheap_tier())
        self.max_tokens = max_tokens

    def generate(self, prompt: str, system_prompt: str = None) -> str:
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            kwargs["system"] = system_prompt

        response = self.client.messages.create(**kwargs)
        # Recorded from the provider's own figures. A response that carries none
        # is counted as uncounted rather than as zero — see phases/llm_usage.py.
        self.usage.record_response(response)
        return _text_of(response)


def build_client(model: str = None, provider: str = None):
    """The configured client for a CLI run, selected by `--provider`.

    Kept here rather than in each CLI for the same reason the class above is:
    three copies of a provider switch would drift exactly like three copies of
    the Anthropic wrapper already had.
    """
    provider = (provider or os.getenv("ONTOLOGY_PROVIDER") or "anthropic").strip().lower()

    if provider == "anthropic":
        return AnthropicClient(model=model)

    if provider == "ollama":
        if not model:
            raise ValueError(
                "--model is required with --provider ollama (e.g. qwen3.8:latest) "
                "— there is no cheap-tier fallback outside Anthropic's model ids."
            )
        from phases.ollama_client import OllamaClient

        return OllamaClient(model=model)

    raise ValueError(f"Unknown provider {provider!r}; expected 'anthropic' or 'ollama'")


def open_db_session():
    """A database session for a command-line run, or None if unavailable.

    Runs from the shell previously wrote nothing to the audit trail: the tracker
    takes a session and the CLIs never supplied one, so every step event, review
    flag and shape violation existed only in terminal output. A run nobody can
    query afterwards is a run that was not really recorded.

    Failure to connect is not fatal. The pipeline's job is to extract, and a
    missing database should cost the audit trail rather than the run — but it is
    reported, because silently losing the record is how you come to believe you
    have one.
    """
    try:
        from db.database import init_database

        factory, _ = init_database()
        return factory()
    except Exception as e:
        logger.warning(f"No database session ({e}); this run will not be recorded")
        return None
