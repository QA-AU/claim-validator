"""Request/response shapes for the HTTP API."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ClaimInput(BaseModel):
    id: str
    text: str


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
