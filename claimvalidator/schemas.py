"""Request/response shapes for the HTTP API."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


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


class ValidationOptions(BaseModel):
    force_census: bool = False
    census_max_chunks: int = 200
    shape_rules: Optional[Dict[str, Any]] = None


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
