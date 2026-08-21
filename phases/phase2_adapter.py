"""Adapter: a generic Ontology -> the dict a Phase 2 profile reads.

**Permanent, not a shim** (DECIDED 2026-08-14, todo/02). Phase 2 is a generic
core that takes domain packs as input; API test generation is one profile rather
than the architecture. This module is that profile's *view* of a generic
ontology — the boundary itself, which is why it stays.

Phase 1 is domain-agnostic and knows nothing about APIs. Phase 2's
`requirements_generator` reads API-shaped keys (`endpoints`,
`entities.authentication`, `entities.rate_limits`, `api_name`), so the mapping
lives here — in one file at the boundary — rather than being smeared through the
ontology model.

Which concepts map to which bucket now comes from `profiles/*.json`, so adding a
domain is a data change. The aliases below are the fallback used when no profile
is supplied, kept so existing callers behave exactly as before.
"""

import logging
from typing import Any, Dict, List

from phases.phase1_models import Ontology

logger = logging.getLogger(__name__)

# Concept names that mean "an operation you can call". Matched loosely, because
# the name is chosen by the model per document and drifts between runs.
# Superseded by `profiles/api.json`; retained as the no-profile fallback.
_ENDPOINT_ALIASES = ("endpoint", "operation", "route", "path")
_AUTH_ALIASES = ("authentication", "auth", "authorization", "token", "credential")
_RATE_LIMIT_ALIASES = ("rate_limit", "throttl", "quota")


def _match_buckets(entities: Dict[str, List[Any]], aliases: tuple) -> List[Any]:
    """Collect entity buckets matching any alias.

    Exact match wins; otherwise every substring match is merged, so a concept
    split across names (`auth_scheme` + `auth_flow`) isn't half-dropped. Exact
    matching alone silently returns nothing when the model picks a near-miss
    name such as `authentication_mechanism` — an invisible data loss.
    """
    normalised = {
        k.strip().lower().replace(" ", "_").replace("-", "_"): v
        for k, v in entities.items()
    }
    needles = [a.strip().lower() for a in aliases]

    for needle in needles:
        if normalised.get(needle):
            return normalised[needle]

    merged: List[Any] = []
    for key, values in normalised.items():
        if values and any(needle in key or key in needle for needle in needles):
            merged.extend(values)
    return merged


def to_phase2_dict(ontology: Ontology, profile=None) -> Dict[str, Any]:
    """Project a generic ontology into the shape a Phase 2 profile reads.

    `profile` is a `phases.profiles.Profile`. Its `buckets` decide which
    discovered concepts map to which domain names — so the API-specific
    knowledge is data, not this function.

    Defaults to the API profile, because that is what every existing caller
    means and what Phase 2 reads today. Pass the generic profile for material
    nobody has said is an API: a trial protocol's outcome measures would
    otherwise land in `endpoints`, and Phase 2 would generate API tests for
    "Overall Survival".
    """
    from phases.profiles import get_profile

    if profile is None:
        profile = get_profile("api")

    payload = ontology.to_dict()
    entities: Dict[str, List[Any]] = {
        ct.name: [i.to_dict() for i in ct.instances] for ct in ontology.concept_types
    }

    payload["entities"] = entities
    # Phase 2 reads `api_name`; the ontology's own field is domain-neutral.
    payload["api_name"] = ontology.name
    payload["profile"] = profile.key

    for bucket, aliases in profile.buckets.items():
        matched = _match_buckets(entities, tuple(aliases))
        if bucket == "endpoints":
            # Phase 2 reads `endpoints` at the top level, not under entities.
            payload["endpoints"] = matched
        else:
            entities.setdefault(bucket, matched)

    # Always present, so a consumer never has to distinguish "no endpoints" from
    # "this profile does not define endpoints".
    payload.setdefault("endpoints", [])

    if not payload["endpoints"]:
        logger.debug(
            f"Profile {profile.key!r} produced no endpoints. Expected for material "
            f"that is not API documentation."
        )

    return payload
