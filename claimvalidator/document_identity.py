"""Automatic ontology reuse by document content, so a caller never has to
manage caching themselves — the whole point of "check if it exists, build if
not" being automatic rather than something every caller re-implements.
"""

import hashlib
from pathlib import Path
from typing import List, Optional, Tuple

from phases.ontology_store import OntologyStore


def content_hash(document_paths: List[str]) -> str:
    """Order-independent hash over a document set's actual bytes.

    Order-independent because the same two files submitted in a different
    order are still the same document set — sorting first means that doesn't
    produce a cache miss.
    """
    h = hashlib.sha256()
    for path in sorted(document_paths):
        h.update(Path(path).read_bytes())
    return h.hexdigest()[:16]


def resolve_ontology_key(
    store: OntologyStore,
    document_paths: List[str],
    document_id: Optional[str] = None,
) -> Tuple[str, bool]:
    """The ontology key for this document set, and whether it was reused.

    Content hash is the automatic, primary key. `document_id` is an optional
    human label layered on top for a fresh ontology's name — never required
    for reuse to work: resubmitting identical bytes under a different (or
    missing) document_id still hits the cache.
    """
    digest = content_hash(document_paths)
    existing = store.find_by_content_hash(digest)
    if existing:
        return existing.key, True

    name = document_id or f"doc-{digest[:8]}"
    meta = store.get_or_create(name)
    meta.content_hash = digest
    store._write_meta(meta)
    return meta.key, False
