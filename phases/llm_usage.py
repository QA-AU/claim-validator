"""Token and cost accounting for LLM calls.

The extractor's only contract with a client is `generate(prompt) -> str`, which
deliberately hides everything about the provider. Usage therefore cannot be
returned through that call — the client accumulates it instead, on a `usage`
attribute this module defines, and the orchestrator reads it once at the end.

**Tokens are measured; cost is only estimated when rates are configured.**
Token counts come from the provider's own response and are exact. Prices are not
knowable from inside the code and change without notice, so an unconfigured
deployment reports `None` for cost rather than a plausible-looking number. A
fabricated cost is worse than no cost: it gets quoted.
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class Usage:
    """Accumulated token usage across every call a client made."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    # Calls whose response carried no usage figures, so the totals above are a
    # floor rather than a total. Tracked because silently under-reporting spend
    # is the same class of error as silently under-reporting coverage.
    uncounted_calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def is_complete(self) -> bool:
        return self.uncounted_calls == 0

    def record(self, input_tokens: Optional[int], output_tokens: Optional[int]) -> None:
        self.calls += 1
        if input_tokens is None and output_tokens is None:
            self.uncounted_calls += 1
            return
        self.input_tokens += int(input_tokens or 0)
        self.output_tokens += int(output_tokens or 0)

    def record_response(self, response: Any) -> None:
        """Record from a provider response object with a `usage` attribute."""
        usage = getattr(response, "usage", None)
        self.record(
            getattr(usage, "input_tokens", None) if usage else None,
            getattr(usage, "output_tokens", None) if usage else None,
        )

    def cost_cents(self, rates: Optional["TokenRates"]) -> Optional[float]:
        """Estimated spend, or None when no rates are configured."""
        if rates is None:
            return None
        return rates.cost_cents(self.input_tokens, self.output_tokens)

    def to_dict(self, rates: Optional["TokenRates"] = None) -> Dict[str, Any]:
        cost = self.cost_cents(rates)
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "uncounted_calls": self.uncounted_calls,
            "counts_complete": self.is_complete,
            "cost_cents": round(cost, 4) if cost is not None else None,
            # Says plainly why cost is absent, so the UI can distinguish
            # "this run was free" from "nobody configured prices".
            "cost_available": cost is not None,
        }


@dataclass
class TokenRates:
    """Price per million tokens, in cents. Supplied by configuration only."""

    input_per_mtok_cents: float
    output_per_mtok_cents: float

    def cost_cents(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens * self.input_per_mtok_cents / 1_000_000
            + output_tokens * self.output_per_mtok_cents / 1_000_000
        )


def usage_of(llm_client) -> Usage:
    """The usage a client accumulated, or an empty record if it tracks none.

    Clients are not required to track usage — the stub clients in the test suite
    do not — so this never raises. An empty `Usage` reports zero calls, which is
    distinguishable from a real run that made calls it could not count.
    """
    usage = getattr(llm_client, "usage", None)
    return usage if isinstance(usage, Usage) else Usage()


class UsageTrackingMixin:
    """Gives a client a `usage` record. Mix in beside a `generate()` implementation."""

    @property
    def usage(self) -> Usage:
        existing = self.__dict__.get("_usage")
        if existing is None:
            existing = Usage()
            self.__dict__["_usage"] = existing
        return existing
