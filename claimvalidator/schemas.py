"""Request/response shapes for the HTTP API."""

import re
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class ClaimInput(BaseModel):
    id: str
    text: str
    # Optional provenance: where this claim's text actually came from, when
    # it wasn't hand-written for this submission — e.g. "README.md, para 3"
    # or "chatbot response, sentence 2". Purely a pass-through: never read
    # by validation logic, only carried into the report so a claim flagged
    # contradicts/mentions_only/no_evidence can be traced back to exactly
    # where in some other document it was extracted from, without the
    # caller having to keep that mapping themselves.
    source_ref: Optional[str] = None


class DocumentRef(BaseModel):
    document_id: Optional[str] = None
    files: List[str] = Field(default_factory=list)


class ShapeRuleOverrides(BaseModel):
    """A caller's own shape-check overrides, structurally validated at the
    request boundary rather than accepted as an arbitrary dict.

    Was `Optional[Dict[str, Any]]` — completely unvalidated, and
    `phases/requirement_shapes.py::_evaluate()` compiles `subject_pattern`/
    `id_pattern` with an unguarded `re.search()`, so a bad regex used to
    reach that code and raise, failing the job (not the server — jobs.py's
    outer try/except catches it — but a 500-shaped failure well after the
    caller could have been told their input was invalid). Validating here
    means a bad regex gets a clean 422 before a job is ever created.
    """

    description: Optional[str] = None
    require_fields: Optional[List[str]] = None
    require_any_of: Optional[List[str]] = None
    require_subject: Optional[bool] = None
    subject_pattern: Optional[str] = None
    id_pattern: Optional[str] = None

    @field_validator("subject_pattern", "id_pattern")
    @classmethod
    def _valid_regex(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if len(v) > 500:
            raise ValueError("pattern exceeds 500 characters")
        try:
            re.compile(v)
        except re.error as e:
            raise ValueError(f"not a valid regular expression: {e}")
        return v

    @field_validator("require_fields", "require_any_of")
    @classmethod
    def _bounded_list(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is None:
            return v
        if len(v) > 20:
            raise ValueError("at most 20 entries")
        return [str(x)[:100] for x in v]


class ValidationOptions(BaseModel):
    force_census: bool = False
    census_max_chunks: int = 200
    shape_rules: Optional[ShapeRuleOverrides] = None


class ValidationRequest(BaseModel):
    document: DocumentRef
    claims: List[ClaimInput]
    webhook_url: Optional[str] = None
    options: ValidationOptions = Field(default_factory=ValidationOptions)
    # Picks an existing, already-built ontology directly — the shared-list
    # reuse path. When set, document.files may be left empty; the API
    # rejects a request that has neither this nor document.files (see
    # api.py::submit_validation).
    ontology_key: Optional[str] = None


class OntologyBuildRequest(BaseModel):
    document_id: Optional[str] = None
    files: List[str] = Field(default_factory=list)
    background_description: str = ""
    # Opt-in backfill trigger for an ontology that already exists (the
    # `reused` path in api.py::build_ontology) and doesn't have a shape
    # profile yet. Has no effect on a fresh build — that always infers one
    # automatically, once, as part of building. See
    # phases/shape_profile_inference.py.
    infer_shape_profile: bool = False
