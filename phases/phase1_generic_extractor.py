"""Phase 1: Generic Extractor - Build a domain-agnostic ontology from documents.

Two passes:

  A. Schema discovery. Given the user's background description and a sample of
     the document, ask the model which concept types this material contains and
     which surface terms each appears under.
  B. Population. For each concept type, retrieve using its surface terms and
     extract instances from the retrieved context.

Pass A is what makes lexical retrieval viable across domains: the probe
vocabulary is produced per-document by the model rather than hand-written, so an
OpenAPI spec yields "endpoint / route / path" and a clinical paper yields
"endpoint / outcome measure / PFS" without any domain code.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from phases.phase1_models import (
    LOW_CHUNK_REACH,
    ConceptInstance,
    ConceptType,
    Coverage,
    DocumentContent,
    Ontology,
    RAGIndex,
    Relation,
)
from phases.phase1_rag_indexer import RAGIndexSearcher

logger = logging.getLogger(__name__)

# How much of the document to show the model during schema discovery.
DISCOVERY_SAMPLE_CHARS = 6000
MAX_CONCEPT_TYPES = 8
INSTANCES_TOP_K = 8

# The three numbers that decide how much of a document Phase 1 ever sees, which
# makes them the ones behind every coverage figure this project reports. A
# 0.5% chunk reach is a fact about these values as much as about the document,
# so a run records which ones applied.
SETTINGS_PROCESS = "phase1"
DEFAULT_SETTINGS = {
    "discovery_sample_chars": DISCOVERY_SAMPLE_CHARS,
    "max_concept_types": MAX_CONCEPT_TYPES,
    "instances_top_k": INSTANCES_TOP_K,
    # Below this share of chunks consulted, the run says so. Lives here rather
    # than with the Coverage model because this is where the sampling happens.
    "low_chunk_reach": 0.05,

    # --- completeness -----------------------------------------------------
    # Whether to measure completeness at the end of a run rather than leaving
    # it unestablished. Off would leave every report saying "not established",
    # which is honest and useless.
    "census_on_completion": True,
    # Above this many chunks the census is not run automatically. A census
    # reads the whole document, so cost is proportional to it: at 182 chunks
    # three repeats cost ~186K tokens, roughly six times the extraction itself.
    # Beyond this the run reports what it would cost and how to run it, rather
    # than spending the money without being asked.
    "census_max_chunks": 200,
    # Repeats. One census run is not a denominator: on 182 chunks of prose two
    # identical runs returned 294 and 342 instances. Three runs give a range,
    # which is the only form this measurement can honestly take.
    "census_runs": 3,
}

# `constraints` and `critical_areas` are first-class Ontology fields with their
# own extraction steps. A concept type that duplicates one of them costs a
# populate call, splits the same data across two places, and burns a slot a
# distinct concept could have used — so drop them even if the model proposes
# them anyway.
_RESERVED_CONCEPT_NAMES = {
    "constraint", "constraints",
    "requirement", "requirements",
    "rule", "rules",
    "limit", "limits",
    "restriction", "restrictions",
    "critical_area", "critical_areas",
    "risk", "risks",
}


def _is_reserved(name: str) -> bool:
    """True when a proposed concept duplicates a first-class ontology field."""
    normalised = name.strip().lower().replace(" ", "_").replace("-", "_")
    return normalised in _RESERVED_CONCEPT_NAMES


def _normalise(name: str) -> str:
    return name.strip().lower().replace(" ", "_").replace("-", "_")


def _is_ignored(name: str, ignore_terms: List[str]) -> bool:
    """True when a brief asked for this kind of concept to be left out.

    Matched loosely in both directions: a brief saying "ignore `schema fields`"
    should drop a concept the model called `data_field`, since the person
    writing the brief cannot know what name the model will invent.
    """
    if not ignore_terms:
        return False

    normalised = _normalise(name)
    concept_words = [w for w in normalised.split("_") if len(w) >= 4]

    for term in ignore_terms:
        candidate = _normalise(term)
        if not candidate:
            continue
        if candidate == normalised:
            return True

        # Word-level prefix overlap, so "schema fields" matches "data_field" and
        # "response properties" matches "response_property". Plural and
        # singular forms differ in the tail, which is why this compares
        # prefixes rather than whole words — and why a naive rstrip("s") did
        # not work ("properties" vs "property").
        for word in (w for w in candidate.split("_") if len(w) >= 4):
            for concept_word in concept_words:
                shared = min(len(word), len(concept_word))
                if shared >= 4 and word[:shared] == concept_word[:shared]:
                    return True
    return False


# ---------------------------------------------------------------------------
# JSON helpers - LLM output is untrusted and frequently off-schema
# ---------------------------------------------------------------------------


def _load_json(response: str, expect: str = "any") -> Any:
    """Pull the first JSON array/object out of a model response.

    Returns None when nothing usable is found. `expect` may be "array",
    "object" or "any".
    """
    if not response:
        return None

    # Strip ```json fences if present
    fenced = re.search(r"```(?:json)?\s*(.+?)```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)

    patterns = []
    if expect in ("array", "any"):
        patterns.append(r"\[.*\]")
    if expect in ("object", "any"):
        patterns.append(r"\{.*\}")

    for pattern in patterns:
        match = re.search(pattern, response, re.DOTALL)
        if not match:
            continue
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue

    logger.warning(f"No parseable JSON in response: {response[:120]!r}")
    return None


def _as_str_list(value: Any) -> List[str]:
    """Coerce whatever the model returned into a list of non-empty strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return [str(value)]
    return [str(v).strip() for v in value if v is not None and str(v).strip()]


# ---------------------------------------------------------------------------
# Pass A: schema discovery
# ---------------------------------------------------------------------------


def discover_concept_types(
    documents: List[DocumentContent],
    llm_client,
    background_description: str,
    max_types: int = MAX_CONCEPT_TYPES,
    brief=None,
    sample_chars: int = DISCOVERY_SAMPLE_CHARS,
) -> List[ConceptType]:
    """Ask the model what concept types this material contains.

    `brief` is an optional human-written note about the document. Without one,
    this decision rests entirely on a blind slice of the text — which on a
    machine-generated spec can contain no subject matter at all. The brief is
    guidance about what to look for, never evidence about what the document
    says.
    """
    logger.info("[Generic] Pass A: discovering concept types...")

    sample = _build_sample(documents, sample_chars)

    guidance = ""
    if brief is not None and not brief.is_empty:
        vocabulary = brief.vocabulary()
        guidance = f"""
A PERSON WHO KNOWS THIS MATERIAL HAS WRITTEN THE FOLLOWING BRIEF. Prefer it over
the excerpt when the two disagree about what matters — the excerpt is a blind
slice of the document and may have missed the subject entirely. The brief
describes what to look for; it is NOT evidence about what the document says, so
never take a fact from it.

{brief.guidance()}
"""
        if vocabulary:
            guidance += f"""
The brief lists these terms as the document's own wording. Assign each to the
concept it belongs to and include it in that concept's surface_terms:
{", ".join(vocabulary)}
"""
        logger.info(
            f"[Generic] Pass A using a brief ({len(brief.guidance())} chars, "
            f"{len(vocabulary)} vocabulary terms)"
        )

    prompt = f"""You are building an ontology of a document. Decide what kinds of
things (concept types) this ontology should contain, based on the material itself.

WHAT THIS MATERIAL IS (provided by the user):
{background_description or "(no description provided)"}
{guidance}
EXCERPT FROM THE DOCUMENT:
{sample}

Return a JSON array of at most {max_types} concept types. For each one:
- name: short snake_case identifier for the kind of thing (e.g. "endpoint", "biomarker", "obligation")
- description: one sentence on what it is in this material
- surface_terms: 6-12 words or phrases this concept actually appears under in
  this text, including synonyms, abbreviations, and the *verb* forms the document
  uses (e.g. "throttled", "quota exceeded" alongside "rate limit"). These are
  used to search the document by literal word match, so a concept the text
  paraphrases will be missed unless its own wording is listed here. Prefer terms
  that literally occur in the excerpt.
- attributes: the field names worth capturing for each instance

Choose concept types that fit THIS material. Do not assume it is software
documentation unless the excerpt shows that it is.

Do NOT propose concept types for constraints, rules, limits, requirements,
critical areas, or risks — those are captured separately. Every slot you spend
on one is a distinct concept the ontology loses.

Return ONLY the JSON array, no other text."""

    response = llm_client.generate(prompt)
    data = _load_json(response, expect="array")

    if not isinstance(data, list):
        logger.warning("[Generic] Schema discovery returned no usable array")
        return []

    concept_types = []
    for item in data:
        if not isinstance(item, dict):
            logger.warning(f"[Generic] Skipping non-object concept type: {str(item)[:60]}")
            continue

        name = str(item.get("name", "")).strip()
        if not name:
            continue

        if _is_reserved(name):
            logger.info(
                f"[Generic] Dropping concept type '{name}' — captured separately "
                f"as a first-class ontology field"
            )
            continue

        if brief is not None and _is_ignored(name, brief.ignore_terms()):
            # The brief explicitly asked for this to be left out. Measured
            # motivation: on the GitHub spec, `data_field` and `data_schema`
            # consumed two of eight concept slots on JSON plumbing.
            logger.info(f"[Generic] Dropping concept type '{name}' — the brief asks to ignore it")
            continue

        surface_terms = _as_str_list(item.get("surface_terms"))
        # Always include the concept name itself as a probe term.
        if name.replace("_", " ") not in surface_terms:
            surface_terms.append(name.replace("_", " "))

        concept_types.append(
            ConceptType(
                name=name,
                description=str(item.get("description", "")).strip(),
                surface_terms=surface_terms,
                attributes=_as_str_list(item.get("attributes")),
            )
        )

    logger.info(
        f"[Generic] Discovered {len(concept_types)} concept types: "
        f"{[c.name for c in concept_types]}"
    )
    return concept_types[:max_types]


def _build_sample(documents: List[DocumentContent], sample_chars: int = DISCOVERY_SAMPLE_CHARS) -> str:
    """Take a bounded excerpt spread across the supplied documents."""
    if not documents:
        return ""

    per_doc = max(sample_chars // len(documents), 500)
    parts = []
    for doc in documents:
        text = doc.raw_text or ""
        # Head plus a middle slice: structured specs front-load metadata, so the
        # head alone can miss the body entirely.
        head = text[: per_doc // 2]
        midpoint = len(text) // 2
        middle = text[midpoint : midpoint + per_doc // 2]
        parts.append(f"[{doc.file_name}]\n{head}\n...\n{middle}")

    return "\n\n".join(parts)[:sample_chars]


# ---------------------------------------------------------------------------
# Pass B: population
# ---------------------------------------------------------------------------


def _instance_requirements(concept_type: ConceptType, profile=None) -> str:
    """What an instance of this concept must be, stated for the extractor.

    Two clauses, and the first is the one that matters most.

    **Categories are not instances.** Measured on the GitHub specification: every
    tag in that document is described as "Endpoints to manage campaigns via the
    REST API", so the word *endpoints* appears more densely in the tag list than
    anywhere near the actual paths. Retrieval therefore handed the extractor the
    tag array, and it extracted `campaigns`, `projects`, `agents` — 19 of 21
    "endpoints" were category names. The text was not misread; a chunk saying
    "Endpoints to manage campaigns" really is about endpoints. What was missing
    was the instruction that a *grouping of* things is not one of those things.
    That confusion is domain-independent, so this clause is always sent.

    **The declared shape**, when a profile supplies one. The rules already exist
    as data for the shape check that catches this after the fact; sending them
    to the extractor turns a post-hoc report into a prevention.
    """
    lines = [
        f"An instance is one specific {concept_type.name}, not a category, "
        f"grouping, or section heading that organises them. Text describing a "
        f"collection — \"endpoints for managing X\", \"the section on Y\" — names "
        f"the collection, not a member of it.",
    ]

    rule = None
    if profile is not None:
        from phases.type_check import _bucket_for

        bucket = _bucket_for(concept_type.name, profile)
        rule = (getattr(profile, "shape_rules", None) or {}).get(bucket) if bucket else None

    if rule:
        if rule.get("description"):
            lines.append(f"In this domain: {rule['description']}.")
        identifying = rule.get("satisfied_by_attributes") or []
        if identifying:
            lines.append(
                f"If you cannot fill at least one of {', '.join(identifying[:6])} from the "
                f"text, it is not an instance — leave it out rather than recording a name "
                f"with no identifying detail."
            )

    return "\n".join(lines)


def populate_concept_type(
    concept_type: ConceptType,
    searcher: RAGIndexSearcher,
    llm_client,
    background_description: str = "",
    profile=None,
    top_k: int = INSTANCES_TOP_K,
) -> ConceptType:
    """Retrieve context for one concept type and extract its instances."""
    # The probe is built from the model-supplied surface terms, so retrieval
    # matches the document's own vocabulary rather than ours.
    query = " ".join(concept_type.surface_terms) or concept_type.name
    retrieval = searcher.retrieve(query, top_k=top_k)
    context, chunk_indices = retrieval.context, retrieval.indices
    # Recorded before the call, so a concept that fails to parse still shows
    # which part of the document was searched for it.
    concept_type.chunks_consulted = chunk_indices
    concept_type.retrieval_score = round(retrieval.max_score, 4)
    concept_type.term_overlap = round(retrieval.term_overlap, 4)

    if retrieval.weakly_matched:
        # Scored above zero, but on one or two incidental words. Measured on a
        # contract: a payment probe matched the *termination* clause because
        # both mention "thirty". No score threshold separates that cleanly.
        logger.warning(
            f"[Generic] '{concept_type.name}': only "
            f"{retrieval.terms_matched}/{retrieval.terms_in_probe} probe terms appear in the "
            f"best chunk — the match may be incidental rather than about this concept"
        )

    if retrieval.found_nothing:
        # TF-IDF matches words, not meaning, so a probe sharing no terms with the
        # document scores exactly 0.0 — indistinguishable from irrelevance. The
        # chunks below were picked arbitrarily; any instances extracted from them
        # are not grounded in a match.
        logger.warning(
            f"[Generic] '{concept_type.name}': no chunk shared a term with its probe "
            f"({query[:60]!r}) — retrieved text was selected arbitrarily"
        )

    attribute_hint = ", ".join(concept_type.attributes) if concept_type.attributes else "any relevant fields"
    requirements = _instance_requirements(concept_type, profile)

    prompt = f"""Extract every instance of one concept from the context below.

CONCEPT: {concept_type.name}
MEANING: {concept_type.description or concept_type.name}
CONTEXT OF THE MATERIAL: {background_description or "(not provided)"}

RETRIEVED TEXT:
{context}

Return a JSON array. Each element is an object with:
- name: a short identifier for this instance
- description: one sentence
- attributes: an object with these fields where available: {attribute_hint}
- source_chunk: the number from the [chunk N] marker above the passage that
  states this instance. Cite the passage you actually read it in.

WHAT COUNTS AS AN INSTANCE:
{requirements}

Only include instances actually supported by the retrieved text. If there are
none, return [].

Return ONLY the JSON array, no other text."""

    response = llm_client.generate(prompt)
    data = _load_json(response, expect="array")

    if not isinstance(data, list):
        logger.warning(f"[Generic] No instances parsed for concept '{concept_type.name}'")
        return concept_type

    retrieved = set(chunk_indices)
    instances = []
    seen = set()
    uncited = 0
    for item in data:
        if not isinstance(item, dict):
            # Tolerate a bare list of names.
            name = str(item).strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                instances.append(ConceptInstance(name=name))
                uncited += 1
            continue

        name = str(item.get("name", "")).strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())

        attributes = item.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}

        source_chunk = _cited_chunk(item.get("source_chunk"), retrieved, concept_type.name, name)
        if source_chunk is None:
            uncited += 1

        instances.append(
            ConceptInstance(
                name=name,
                description=str(item.get("description", "")).strip(),
                attributes=attributes,
                source_chunk=source_chunk,
                source_document=searcher.source_of(source_chunk) if source_chunk is not None else "",
            )
        )

    concept_type.adopt(instances)
    logger.info(
        f"[Generic] {concept_type.name}: {len(instances)} instances"
        + (f" ({uncited} without a usable citation)" if uncited else "")
    )
    return concept_type


def _cited_chunk(value: Any, retrieved: set, concept_name: str, instance_name: str) -> Optional[int]:
    """Accept a model-supplied chunk citation only if it was actually retrieved.

    An unverified citation is worse than none: it looks like evidence and points
    at a passage the extractor never showed the model. A citation outside the
    retrieved set is therefore dropped rather than trusted or guessed at.
    """
    if value is None:
        return None

    try:
        chunk = int(str(value).strip())
    except (TypeError, ValueError):
        logger.debug(f"[Generic] {concept_name}/{instance_name}: uninterpretable citation {value!r}")
        return None

    if chunk not in retrieved:
        logger.debug(
            f"[Generic] {concept_name}/{instance_name}: cited chunk {chunk}, which was "
            f"not retrieved for this concept — dropping the citation"
        )
        return None

    return chunk


def extract_relations(
    concept_types: List[ConceptType], llm_client, background_description: str = ""
) -> List[Relation]:
    """Ask the model how the discovered concepts relate."""
    if len(concept_types) < 2:
        return []

    summary = "\n".join(
        f"- {ct.name}: {ct.description or ct.name} "
        f"(examples: {', '.join(i.name for i in ct.instances[:3]) or 'none found'})"
        for ct in concept_types
    )

    prompt = f"""Given these concept types from one body of material, state how they relate.

CONTEXT OF THE MATERIAL: {background_description or "(not provided)"}

CONCEPT TYPES:
{summary}

Return a JSON array of relationships, each an object with:
- subject: concept type name
- predicate: the relationship (e.g. "requires", "measured_by", "contains")
- object: concept type name

Only include relationships supported by the concepts listed. Return ONLY the
JSON array, no other text."""

    data = _load_json(llm_client.generate(prompt), expect="array")
    if not isinstance(data, list):
        return []

    valid_names = {ct.name.lower() for ct in concept_types}
    relations = []
    for item in data:
        if not isinstance(item, dict):
            continue
        subject = str(item.get("subject", "")).strip()
        predicate = str(item.get("predicate", "")).strip()
        obj = str(item.get("object", "")).strip()
        if not (subject and predicate and obj):
            continue
        # Drop relations referring to concepts we didn't discover.
        if subject.lower() not in valid_names or obj.lower() not in valid_names:
            logger.debug(f"[Generic] Dropping relation with unknown concept: {subject}->{obj}")
            continue
        relations.append(Relation(subject=subject, predicate=predicate, object=obj))

    logger.info(f"[Generic] Extracted {len(relations)} relations")
    return relations


def extract_constraints(searcher: RAGIndexSearcher, llm_client, background_description: str = "") -> List[str]:
    """Extract rules, limits and requirements stated by the material."""
    query = (
        "limit maximum minimum required must constraint rule threshold quota "
        "restriction eligibility requirement mandatory"
    )
    context = searcher.get_context(query, top_k=6)

    prompt = f"""List the constraints, limits and hard requirements stated in this text.

CONTEXT OF THE MATERIAL: {background_description or "(not provided)"}

RETRIEVED TEXT:
{context}

Return a JSON array of short strings, each one constraint (e.g. "5000 requests
per hour per token", "patients must be over 18"). Return [] if none are stated.

Return ONLY the JSON array, no other text."""

    data = _load_json(llm_client.generate(prompt), expect="array")
    constraints = _as_str_list(data)
    logger.info(f"[Generic] Extracted {len(constraints)} constraints")
    return constraints


def extract_critical_areas(
    concept_types: List[ConceptType], searcher: RAGIndexSearcher, llm_client, background_description: str = ""
) -> List[str]:
    """Identify the high-risk areas Phase 2 should focus on."""
    query = "critical important risk security safety failure error warning caution required"
    context = searcher.get_context(query, top_k=5)
    names = ", ".join(ct.name for ct in concept_types) or "(none)"

    prompt = f"""Identify the areas of this material that most warrant careful scrutiny.

CONTEXT OF THE MATERIAL: {background_description or "(not provided)"}
CONCEPT TYPES FOUND: {names}

RETRIEVED TEXT:
{context}

Return a JSON array of short strings naming the high-risk or high-importance
areas. Return ONLY the JSON array, no other text."""

    data = _load_json(llm_client.generate(prompt), expect="array")
    areas = _as_str_list(data)
    logger.info(f"[Generic] Extracted {len(areas)} critical areas")
    return areas


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def extract_ontology_generic(
    documents: List[DocumentContent],
    rag_index: RAGIndex,
    llm_client,
    name: str,
    background_description: str = "",
    pinned_concept_types: Optional[List[dict]] = None,
    brief=None,
    profile=None,
    settings=None,
    db_session=None,
) -> Ontology:
    """Build a domain-agnostic ontology from the documents.

    `pinned_concept_types` reuses a previously discovered schema instead of
    running pass A. Pinning keeps diffs meaningful (a rename would otherwise read
    as "concept removed, concept added"), keeps user assertions matching, and
    skips a model call.
    """
    logger.info(f"[Generic] Building ontology '{name}'")

    # Resolved once for the whole extraction rather than per concept, so every
    # concept in one ontology was built under the same rules — a mid-run change
    # would make the coverage figure describe two different runs.
    from phases.settings_registry import settings_for

    resolved = settings_for(SETTINGS_PROCESS, DEFAULT_SETTINGS, settings, db_session)
    max_types = resolved.get("max_concept_types")
    top_k = resolved.get("instances_top_k")
    sample_chars = resolved.get("discovery_sample_chars")

    searcher = RAGIndexSearcher(rag_index)

    if pinned_concept_types:
        concept_types = [
            ConceptType(
                name=ct["name"],
                description=ct.get("description", ""),
                surface_terms=ct.get("surface_terms", []),
                attributes=ct.get("attributes", []),
            )
            for ct in pinned_concept_types
        ]
        logger.info(
            f"[Generic] Reusing {len(concept_types)} pinned concept types "
            f"(pass A skipped): {[c.name for c in concept_types]}"
        )
    else:
        # Pass A - decide what this ontology should contain.
        concept_types = discover_concept_types(
            documents,
            llm_client,
            background_description,
            max_types=max_types,
            brief=brief,
            sample_chars=sample_chars,
        )

    if not concept_types and not pinned_concept_types:
        # Observed twice live: discovery returned nothing and the run completed
        # with status "success", a structure score of 1.0 (a well-formed empty
        # ontology is well-formed) and a citation rate of 1.0 (zero of zero
        # instances are cited). Retried by hand both times, both retries
        # produced the usual eight concepts — so it is a transient failure of a
        # single call, and one retry is the proportionate answer.
        logger.warning("[Generic] No concept types discovered; retrying once")
        concept_types = discover_concept_types(
            documents,
            llm_client,
            background_description,
            max_types=max_types,
            brief=brief,
            sample_chars=sample_chars,
        )

    if not concept_types:
        logger.warning(
            "[Generic] No concept types discovered after a retry; the ontology will be "
            "EMPTY. This is not a sparse result — the run found nothing at all"
        )

    # Pass B - populate each concept type using its own surface terms.
    populated = []
    for concept_type in concept_types:
        try:
            populated.append(
                populate_concept_type(
                    concept_type,
                    searcher,
                    llm_client,
                    background_description,
                    profile=profile,
                    top_k=top_k,
                )
            )
        except Exception as e:
            # One bad concept must not lose the whole ontology.
            logger.error(f"[Generic] Failed to populate '{concept_type.name}': {e}")
            populated.append(concept_type)

    relations = extract_relations(populated, llm_client, background_description)
    constraints = extract_constraints(searcher, llm_client, background_description)
    critical_areas = extract_critical_areas(populated, searcher, llm_client, background_description)

    ontology = Ontology(
        name=name,
        domain=background_description or "",
        concept_types=populated,
        relations=relations,
        constraints=constraints,
        critical_areas=critical_areas,
        extracted_from=[doc.file_name for doc in documents],
        # Read from the searcher after every retrieval has happened — this is the
        # only moment the union is known, and it is gone once the run ends.
        coverage=Coverage(
            chunks_total=searcher.chunks_total,
            chunks_consulted=set(searcher.chunks_consulted),
            images_found=sum(int(d.metadata.get("images_found", 0)) for d in documents),
            images_captioned=sum(int(d.metadata.get("images_captioned", 0)) for d in documents),
            # Stamped onto the record, so a coverage report states the threshold
            # it was judged against rather than deferring to whatever the
            # constant says when someone reads it later.
            low_reach_threshold=resolved.get("low_chunk_reach", LOW_CHUNK_REACH),
        ),
    )

    logger.info(
        f"[Generic] Ontology '{name}': {len(populated)} concept types, "
        f"{ontology.instance_count()} instances, {len(relations)} relations"
    )
    logger.info(
        f"[Generic] Coverage: {len(ontology.coverage.chunks_consulted)}/"
        f"{ontology.coverage.chunks_total} chunks consulted "
        f"({ontology.coverage.chunk_reach:.1%}), "
        f"{ontology.concept_yield():.1f} instances per concept"
    )
    return ontology
