"""Phase 1 Data Models - Document Ingestion & Ontology Extraction."""

import re
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any, Set
from datetime import datetime


def slugify(name: str) -> str:
    """Normalise a user-supplied name into a stable directory-safe slug.

    Lives here rather than in the ontology store because instance ids need it
    too, and instance ids are a property of the model, not of storage.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return slug or "ontology"


@dataclass
class DocumentContent:
    """Raw content extracted from a document file."""

    file_name: str
    raw_text: str
    tables: List[List[List[str]]] = field(default_factory=list)
    images: List[bytes] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    estimated_tokens: int = 0


SOURCE_DOCUMENT = "document"
SOURCE_USER_ASSERTION = "user_assertion"


@dataclass
class ConceptInstance:
    """A single occurrence of a concept type found in the documents.

    Carries how it came to exist. A document instance cites the chunk it was read
    from; a user assertion cites a person and a time instead. The harness treats
    them differently — document instances can be checked against their text,
    assertions are axioms — so an assertion must never be silently relabelled as
    document-sourced. That is the point where "traceable" would quietly become
    false.
    """

    name: str
    attributes: Dict[str, Any] = field(default_factory=dict)
    description: str = ""

    # Provenance
    concept_type: str = ""
    source: str = SOURCE_DOCUMENT
    source_chunk: Optional[int] = None
    source_document: str = ""
    asserted_by: str = ""
    asserted_at: str = ""
    question: str = ""
    checklist_item: str = ""

    @property
    def instance_id(self) -> str:
        """Deterministic id, so an assertion still matches its instance next run.

        Derived from names rather than randomly generated: a fresh UUID each run
        could not be matched across extractions, which would break revalidation
        entirely. When a name genuinely changes this surfaces as "assertion no
        longer matches an instance" — a review item, not silent loss.
        """
        return f"{slugify(self.concept_type)}:{slugify(self.name)}"

    def to_dict(self) -> Dict[str, Any]:
        # Attributes are flattened alongside name/description so downstream
        # consumers can read domain fields without knowing this wrapper exists.
        out = {"name": self.name}
        if self.description:
            out["description"] = self.description
        out.update(self.attributes)

        # Written after the attributes, never before: provenance is decided by
        # the pipeline, and a model that returns an attribute called "source"
        # must not be able to overwrite where its own output came from.
        out["id"] = self.instance_id
        out["source"] = self.source
        if self.source == SOURCE_USER_ASSERTION:
            out["asserted_by"] = self.asserted_by
            out["asserted_at"] = self.asserted_at
            if self.question:
                out["question"] = self.question
            if self.checklist_item:
                out["checklist_item"] = self.checklist_item
        else:
            out["source_chunk"] = self.source_chunk
            out["source_document"] = self.source_document
        return out


@dataclass
class ConceptType:
    """A kind of thing the ontology tracks, discovered per-document.

    `surface_terms` are the words this concept actually appears under in the
    text. They are produced during schema discovery and used as the retrieval
    probe, which is what makes lexical retrieval work across domains without a
    hand-written synonym table.
    """

    name: str
    description: str = ""
    surface_terms: List[str] = field(default_factory=list)
    attributes: List[str] = field(default_factory=list)
    instances: List[ConceptInstance] = field(default_factory=list)
    # Chunk indices this concept's instances were extracted from. Provenance at
    # concept granularity: it says which part of the document produced these
    # instances, which a finished ontology otherwise cannot tell you.
    chunks_consulted: List[int] = field(default_factory=list)
    # Best lexical similarity between this concept's probe and any chunk. Zero
    # means retrieval matched nothing at all and the context it was populated
    # from was chosen arbitrarily — which nothing else in the output reveals.
    retrieval_score: float = 0.0
    # Share of this concept's probe terms present in the best chunk. A non-zero
    # score carried by one incidental word looks identical to a real match by
    # score alone; this is what separates them.
    term_overlap: float = 1.0

    def adopt(self, instances: List[ConceptInstance]) -> None:
        """Take ownership of instances, stamping each with this concept's name.

        Instance ids are `concept:instance`, so an instance that does not know
        its concept cannot produce a stable id — and the assertion store matches
        on that id.
        """
        for instance in instances:
            instance.concept_type = self.name
        self.instances = instances

    def instance(self, instance_id: str) -> Optional[ConceptInstance]:
        for candidate in self.instances:
            if candidate.instance_id == instance_id:
                return candidate
        return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "surface_terms": self.surface_terms,
            "attributes": self.attributes,
            "instances": [i.to_dict() for i in self.instances],
            "chunks_consulted": self.chunks_consulted,
            "retrieval_score": self.retrieval_score,
            "term_overlap": self.term_overlap,
        }


@dataclass
class Relation:
    """A directed relationship between two concepts."""

    subject: str
    predicate: str
    object: str

    def to_dict(self) -> Dict[str, str]:
        return {"subject": self.subject, "predicate": self.predicate, "object": self.object}


# Below this share of the document, an extraction is a thin sample rather than a
# reading of the material. It lives here, next to the thing it judges, so the
# verdict travels inside the coverage report and no consumer — validator, API, or
# page — has to keep its own copy of the number and drift from it.
#
# See phase1_validator.coverage_review_flags for how it was calibrated.
LOW_CHUNK_REACH = 0.05


@dataclass
class Coverage:
    """How much of the source material the extraction actually looked at.

    Extraction samples rather than enumerates, which is an accepted trade-off —
    a 12.9 MB document costs the same as a 2.5 KB one because cost is bounded by
    retrieval, not document size. Sampling silently is the failure; sampling
    visibly is fine. This is the record that makes it visible.

    It cannot be recomputed from a finished ontology, because the artifact does
    not record what retrieval looked at. It has to be captured during the run.
    """

    chunks_total: int = 0
    chunks_consulted: Set[int] = field(default_factory=set)
    images_found: int = 0
    images_captioned: int = 0
    # Carried on the record rather than read from the module constant, so a
    # coverage report says what "low" meant when it was written. The default is
    # the constant, so nothing that does not set it changes behaviour.
    low_reach_threshold: float = LOW_CHUNK_REACH

    @property
    def chunk_reach(self) -> float:
        """Fraction of the indexed document the extraction consulted."""
        if not self.chunks_total:
            return 0.0
        return len(self.chunks_consulted) / self.chunks_total

    @property
    def reach_is_low(self) -> bool:
        """Whether this counts as a thin sample. Unmeasured reach is not low, it is unknown."""
        if not self.chunks_total:
            return False
        return self.chunk_reach < self.low_reach_threshold

    @property
    def image_coverage(self) -> float:
        """Fraction of embedded images whose content reached the text.

        A document with no images is fully covered — there is nothing to lose.
        """
        if not self.images_found:
            return 1.0
        return self.images_captioned / self.images_found

    def to_dict(self) -> Dict[str, Any]:
        # `chunks_consulted` is bounded by (retrieval calls x top_k), never by
        # document size, so serialising the indices stays cheap even on a
        # 14,000-chunk document — and they are what per-instance provenance and
        # version diffs will need.
        return {
            "chunks_total": self.chunks_total,
            "chunks_consulted": sorted(self.chunks_consulted),
            "chunks_consulted_count": len(self.chunks_consulted),
            "chunk_reach": round(self.chunk_reach, 4),
            "reach_is_low": self.reach_is_low,
            "images_found": self.images_found,
            "images_captioned": self.images_captioned,
            "image_coverage": round(self.image_coverage, 4),
        }


@dataclass
class Ontology:
    """Domain-agnostic ontology extracted from documents.

    Nothing here names a domain. What the ontology contains is decided by the
    concept types discovered from the document plus the user's background
    description, so the same pipeline handles an OpenAPI spec and a clinical
    paper.
    """

    name: str
    domain: str = ""  # from the user's mandatory Background Description
    concept_types: List[ConceptType] = field(default_factory=list)
    relations: List[Relation] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    critical_areas: List[str] = field(default_factory=list)

    # Metadata
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    extracted_from: List[str] = field(default_factory=list)
    confidence_score: float = 0.0
    coverage: Coverage = field(default_factory=Coverage)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Ontology":
        """Rebuild an ontology from its own JSON.

        Phase 1 could always write an ontology and never read one back, which
        was fine while everything downstream ran in the same process. It stops
        being fine the moment a later stage is run on its own (todo/07) — a
        census or a targeted extraction pass operates on a *stored* ontology,
        days after the run that produced it.

        `to_dict` flattens an instance's attributes alongside its name, so the
        inverse has to put them back: anything that is not a known field is an
        attribute. Provenance is read from its own keys and never from the
        attribute bag, which is the same separation `to_dict` enforces on the
        way out.
        """
        known = {
            "name", "description", "id", "source", "source_chunk", "source_document",
            "asserted_by", "asserted_at", "question", "checklist_item",
        }

        concept_types = []
        for raw_concept in data.get("concept_types") or []:
            concept = ConceptType(
                name=raw_concept.get("name", ""),
                description=raw_concept.get("description", ""),
                surface_terms=list(raw_concept.get("surface_terms") or []),
                attributes=list(raw_concept.get("attributes") or []),
                chunks_consulted=list(raw_concept.get("chunks_consulted") or []),
                retrieval_score=float(raw_concept.get("retrieval_score") or 0.0),
                term_overlap=float(raw_concept.get("term_overlap") or 1.0),
            )
            instances = []
            for raw in raw_concept.get("instances") or []:
                if not isinstance(raw, dict):
                    continue
                instances.append(
                    ConceptInstance(
                        name=raw.get("name", ""),
                        description=raw.get("description", ""),
                        attributes={k: v for k, v in raw.items() if k not in known},
                        source=raw.get("source", SOURCE_DOCUMENT),
                        source_chunk=raw.get("source_chunk"),
                        source_document=raw.get("source_document", "") or "",
                        asserted_by=raw.get("asserted_by", "") or "",
                        asserted_at=raw.get("asserted_at", "") or "",
                        question=raw.get("question", "") or "",
                        checklist_item=raw.get("checklist_item", "") or "",
                    )
                )
            concept.adopt(instances)
            concept_types.append(concept)

        coverage_data = data.get("coverage") or {}
        coverage = Coverage(
            chunks_total=int(coverage_data.get("chunks_total") or 0),
            chunks_consulted=set(coverage_data.get("chunks_consulted") or []),
            images_found=int(coverage_data.get("images_found") or 0),
            images_captioned=int(coverage_data.get("images_captioned") or 0),
        )

        ontology = cls(
            name=data.get("name", ""),
            domain=data.get("domain", ""),
            concept_types=concept_types,
            constraints=list(data.get("constraints") or []),
            critical_areas=list(data.get("critical_areas") or []),
            extracted_from=list(data.get("extracted_from") or []),
            confidence_score=float(data.get("confidence_score") or 0.0),
            coverage=coverage,
        )
        if data.get("created_at"):
            ontology.created_at = data["created_at"]

        for raw_relation in data.get("relations") or []:
            if isinstance(raw_relation, dict):
                ontology.relations.append(
                    Relation(
                        subject=raw_relation.get("subject", ""),
                        predicate=raw_relation.get("predicate", ""),
                        object=raw_relation.get("object", ""),
                    )
                )

        return ontology

    def concept(self, name: str) -> Optional[ConceptType]:
        """Look up a concept type by name, case-insensitively."""
        target = name.strip().lower()
        for ct in self.concept_types:
            if ct.name.strip().lower() == target:
                return ct
        return None

    def instance_count(self) -> int:
        return sum(len(ct.instances) for ct in self.concept_types)

    def all_instances(self) -> List[ConceptInstance]:
        return [i for ct in self.concept_types for i in ct.instances]

    def find_instance(self, instance_id: str) -> Optional[ConceptInstance]:
        """Look an instance up by its deterministic id, across every concept."""
        for concept_type in self.concept_types:
            found = concept_type.instance(instance_id)
            if found is not None:
                return found
        return None

    def cited_count(self) -> int:
        """Document instances that name the chunk they came from."""
        return sum(
            1
            for i in self.all_instances()
            if i.source == SOURCE_DOCUMENT and i.source_chunk is not None
        )

    def assertion_count(self) -> int:
        return sum(1 for i in self.all_instances() if i.source == SOURCE_USER_ASSERTION)

    def concept_yield(self) -> float:
        """Mean instances per concept type populated.

        The denominator is every concept type pass B ran, not just the ones that
        came back non-empty — a concept that yielded nothing is thin extraction,
        and hiding it in the denominator would flatter the number.
        """
        if not self.concept_types:
            return 0.0
        return self.instance_count() / len(self.concept_types)

    def citation_rate(self) -> float:
        """Share of document instances that name the passage they came from.

        Provenance depends on the model honouring the citation field. If it stops
        doing so the instances still look fine and traceability quietly vanishes,
        so the rate is measured rather than assumed.
        """
        document_instances = [i for i in self.all_instances() if i.source == SOURCE_DOCUMENT]
        if not document_instances:
            return 1.0
        return self.cited_count() / len(document_instances)

    def coverage_report(self) -> Dict[str, Any]:
        """The coverage numbers, ready to report or serialise."""
        report = self.coverage.to_dict()
        report["concept_yield"] = round(self.concept_yield(), 2)
        report["instances_cited"] = self.cited_count()
        report["instances_total"] = self.instance_count()
        report["citation_rate"] = round(self.citation_rate(), 4)
        report["user_assertions"] = self.assertion_count()
        return report

    def to_dict(self) -> Dict[str, Any]:
        """Full generic serialisation."""
        return {
            "name": self.name,
            "domain": self.domain,
            "concept_types": [ct.to_dict() for ct in self.concept_types],
            "relations": [r.to_dict() for r in self.relations],
            "constraints": self.constraints,
            "critical_areas": self.critical_areas,
            "created_at": self.created_at,
            "extracted_from": self.extracted_from,
            "confidence_score": self.confidence_score,
            "coverage": self.coverage_report(),
        }



@dataclass
class RAGIndex:
    """Searchable index created from document chunks."""

    chunks: List[str]
    embeddings: List[List[float]]
    metadata: List[Dict[str, Any]] = field(default_factory=list)

    def search(self, query_embedding: List[float], top_k: int = 5) -> List[tuple]:
        """Search index using embedding, return (chunk, similarity_score)."""
        pass


@dataclass
class ValidationResult:
    """Result of ontology validation.

    `confidence_score` is a claim about *structure* — is this ontology
    well-formed. `coverage` and `review_flags` are a separate claim about *reach*
    — how much of the document it saw. They are reported side by side and never
    folded together: a 1% sample can be perfectly well-formed, and that is
    exactly the case this separation exists to expose.
    """

    valid: bool
    issues: List[tuple] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    confidence_score: float = 0.0
    coverage: Dict[str, Any] = field(default_factory=dict)
    review_flags: List[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        """True when reach is low enough that the result should not be read as complete."""
        return bool(self.review_flags)


@dataclass
class Phase1Output:
    """Final output from Phase 1."""

    workflow_id: str
    name: str
    status: str  # "success", "partial", "failed"

    # The ontology — the main deliverable.
    ontology: Ontology

    # Validation
    validation: ValidationResult

    # Metadata
    documents_processed: int = 0
    total_tokens_used: int = 0
    # None means no token prices are configured — which is not the same as a run
    # that cost nothing, and must not be rendered as "$0.00".
    total_cost_cents: Optional[float] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    # How much of the document was *captured*, as distinct from how much was
    # touched. Exact where the document parses; an estimate otherwise, and
    # labelled as such. See phases/completeness.py.
    completeness: Dict[str, Any] = field(default_factory=dict)

    # Paths
    ontology_file: Optional[str] = None
    # Filename of the version this run wrote, so a reviewed re-extraction can
    # promote exactly this result rather than guessing at "the newest one".
    version_file: Optional[str] = None

    # What the shape check looked at and found. Structured because the CLI
    # previously recovered this by searching the review-flag text, and silently
    # reported "no violations" the moment the wording changed.
    shape_check: Dict[str, Any] = field(default_factory=dict)
    # Completeness measured directly, as a range. See phase1b_validation.run_census.
    census: Dict[str, Any] = field(default_factory=dict)
    # Whether each sampled instance is the KIND of thing it was filed as.
    type_judge: Dict[str, Any] = field(default_factory=dict)

    # Tracking
    duration_seconds: float = 0.0
    error_message: Optional[str] = None

    # Database
    execution_stage_id: Optional[str] = None
