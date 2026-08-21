"""Model configuration — the single place model identifiers are named.

No model ID belongs anywhere else in the codebase. Everything selects a *tier*
("s", "m", "p"); which model backs a tier is configuration, resolved at runtime
in this order:

  1. Environment variable       ONTOLOGY_MODEL_S / _M / _P
  2. The system keyring         (set via the web UI or setup_keyring.py)
  3. The fallback below

The fallbacks exist so the project runs out of the box; they are not a
dependency. Point the env vars at any model — a different Claude version, or a
different provider entirely if the LLM client is swapped — without editing code.

Tests and any batch/eval work should call `cheap_tier()` rather than naming a
model, so cost tracks the tier policy instead of scattered literals.
"""

import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Tier identifiers used throughout the codebase.
TIER_SMALL = "s"
TIER_MEDIUM = "m"
TIER_POWER = "p"

TIER_LABELS: Dict[str, str] = {
    TIER_SMALL: "Small (Fast & Cost-Effective)",
    TIER_MEDIUM: "Medium (Balanced)",
    TIER_POWER: "Power (Most Capable)",
}

# Last-resort defaults. Override per deployment with the env vars below rather
# than editing these — they are a starting point, not a pinned requirement.
_FALLBACK_MODELS: Dict[str, str] = {
    TIER_SMALL: "claude-haiku-4-5",
    TIER_MEDIUM: "claude-sonnet-5",
    TIER_POWER: "claude-opus-5",
}

_ENV_VARS: Dict[str, str] = {
    TIER_SMALL: "ONTOLOGY_MODEL_S",
    TIER_MEDIUM: "ONTOLOGY_MODEL_M",
    TIER_POWER: "ONTOLOGY_MODEL_P",
}

# Which tier cost-sensitive work uses. Tests, batch runs, and smoke checks
# should resolve their model through this rather than naming one.
CHEAP_TIER_ENV = "ONTOLOGY_CHEAP_TIER"
_DEFAULT_CHEAP_TIER = TIER_SMALL


def cheap_tier() -> str:
    """The tier to use where cost matters more than capability.

    Tests, evaluation sweeps, and smoke checks call this instead of naming a
    model, so raising or lowering test spend is one env var, not a code change.
    """
    tier = os.environ.get(CHEAP_TIER_ENV, _DEFAULT_CHEAP_TIER).strip().lower()
    if tier not in _FALLBACK_MODELS:
        logger.warning(
            f"{CHEAP_TIER_ENV}={tier!r} is not a known tier; using {_DEFAULT_CHEAP_TIER!r}"
        )
        return _DEFAULT_CHEAP_TIER
    return tier


def default_model(tier: str) -> str:
    """Resolve a tier to a model id, without touching the keyring.

    Used for UI defaults and for callers that only need a starting value.
    """
    tier = (tier or "").strip().lower()
    if tier not in _FALLBACK_MODELS:
        raise ValueError(f"Unknown tier {tier!r}; expected one of {sorted(_FALLBACK_MODELS)}")

    override = os.environ.get(_ENV_VARS[tier], "").strip()
    return override or _FALLBACK_MODELS[tier]


def resolve_model(tier: str, stored_name: Optional[str] = None) -> str:
    """Resolve a tier to a model id, preferring a stored/configured value.

    `stored_name` is whatever the keyring or saved configuration holds; an
    explicit env var still wins so a deployment can force a model without
    rewriting stored settings.
    """
    tier = (tier or "").strip().lower()
    if tier not in _FALLBACK_MODELS:
        raise ValueError(f"Unknown tier {tier!r}; expected one of {sorted(_FALLBACK_MODELS)}")

    override = os.environ.get(_ENV_VARS[tier], "").strip()
    if override:
        return override
    if stored_name and stored_name.strip():
        return stored_name.strip()
    return _FALLBACK_MODELS[tier]


def all_defaults() -> Dict[str, str]:
    """Tier -> model id for every tier, applying env overrides."""
    return {tier: default_model(tier) for tier in _FALLBACK_MODELS}


# --- pricing -------------------------------------------------------------
#
# Deliberately without fallbacks. Token counts come from the provider and are
# exact; prices do not and change without notice, so an unconfigured deployment
# reports no cost rather than a plausible-looking invention. A made-up cost is
# worse than none, because it ends up quoted.
#
#   ONTOLOGY_PRICE_S="<input cents per Mtok>,<output cents per Mtok>"
#   e.g. ONTOLOGY_PRICE_M="300,1500"

_PRICE_ENV_VARS: Dict[str, str] = {
    TIER_SMALL: "ONTOLOGY_PRICE_S",
    TIER_MEDIUM: "ONTOLOGY_PRICE_M",
    TIER_POWER: "ONTOLOGY_PRICE_P",
}


def token_rates(tier: str):
    """Configured price for a tier, or None when none is set.

    Returns a `TokenRates`; None means "nobody told us what this costs", which
    callers must surface as unknown rather than as zero.
    """
    from phases.llm_usage import TokenRates

    tier = (tier or "").strip().lower()
    raw = os.environ.get(_PRICE_ENV_VARS.get(tier, ""), "").strip()
    if not raw:
        return None

    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 2:
        logger.warning(
            f"{_PRICE_ENV_VARS[tier]}={raw!r} is not '<input>,<output>' cents per "
            f"million tokens; ignoring and reporting cost as unknown"
        )
        return None

    try:
        return TokenRates(float(parts[0]), float(parts[1]))
    except ValueError:
        logger.warning(f"{_PRICE_ENV_VARS[tier]}={raw!r} is not numeric; cost will be unknown")
        return None


def describe() -> str:
    """Human-readable summary of the resolved configuration."""
    lines = ["Model configuration (tier -> model):"]
    for tier, model in all_defaults().items():
        source = "env" if os.environ.get(_ENV_VARS[tier], "").strip() else "fallback"
        marker = "  <- cheap tier" if tier == cheap_tier() else ""
        lines.append(f"  {tier}  {TIER_LABELS[tier]:32} {model:28} ({source}){marker}")
    lines.append(f"Override with: {', '.join(_ENV_VARS.values())}, {CHEAP_TIER_ENV}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
