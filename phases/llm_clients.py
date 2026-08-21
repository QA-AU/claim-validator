"""Provider registry for LLM clients.

`phase1_model_config` accepts any model identifier, including non-Claude ones —
verified with `my-org/custom-llm-v3` flowing through to the UI. But only one
client implementation exists, so such an identifier resolved happily and then
failed at call time, thirty seconds into a run, with an error about the model
rather than about the missing provider.

This makes the client selectable by configuration too, and — more importantly —
makes an unsupported provider fail *immediately*, naming what is registered.

The one client contract is:

    generate(prompt: str, system_prompt: str | None = None) -> str

plus an optional `describe_image`. Phase 2 previously called
`generate(system_prompt=..., messages=[...])`, which no real client accepted —
every test stub implemented whichever signature its own phase used, so nothing
caught it until the first live Phase 2 run. One contract, so a second provider
is a registration rather than a refactor:

    from phases.llm_clients import register

    register("myprovider", lambda api_key, model: MyClient(api_key, model))
"""

import logging
import os
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)

PROVIDER_ENV = "ONTOLOGY_PROVIDER"
DEFAULT_PROVIDER = "anthropic"

_REGISTRY: Dict[str, Callable[..., Any]] = {}


def register(provider: str, factory: Callable[..., Any]) -> None:
    """Register a client factory: `factory(api_key, model) -> client`."""
    key = (provider or "").strip().lower()
    if not key:
        raise ValueError("A provider needs a name")
    _REGISTRY[key] = factory
    logger.debug(f"Registered LLM provider {key!r}")


def available() -> List[str]:
    return sorted(_REGISTRY)


def is_registered(provider: str) -> bool:
    return (provider or "").strip().lower() in _REGISTRY


def configured_provider() -> str:
    """Which provider this deployment uses. One env var, no code change."""
    return (os.environ.get(PROVIDER_ENV, "") or DEFAULT_PROVIDER).strip().lower()


def build(api_key: str, model: str, provider: str = "") -> Any:
    """Construct the configured client.

    Raises immediately for an unregistered provider rather than letting a model
    id resolve and fail mid-run. The error names what *is* available, because
    "unsupported provider" without a list is a guessing game.
    """
    name = (provider or configured_provider()).strip().lower()

    factory = _REGISTRY.get(name)
    if factory is None:
        raise ValueError(
            f"No LLM client registered for provider {name!r}. "
            f"Registered: {', '.join(available()) or 'none'}. "
            f"Set {PROVIDER_ENV} to one of these, or register a client for {name!r}."
        )

    return factory(api_key, model)
