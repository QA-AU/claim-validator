"""content_hash + resolve_ontology_key — the automatic cache-by-content layer.

The property that matters: resubmitting the identical document, under a
different (or missing) document_id, still hits the cache. That's the whole
point of making this automatic rather than something a caller manages.
"""

import pytest

from claimvalidator.document_identity import content_hash, resolve_ontology_key
from phases.ontology_store import OntologyStore


@pytest.fixture
def store(tmp_path):
    return OntologyStore(str(tmp_path / "ontologies"))


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_content_hash_is_order_independent(tmp_path):
    a = _write(tmp_path, "a.txt", "hello")
    b = _write(tmp_path, "b.txt", "world")
    assert content_hash([a, b]) == content_hash([b, a])


def test_content_hash_differs_for_different_content(tmp_path):
    a = _write(tmp_path, "a.txt", "hello")
    b = _write(tmp_path, "b.txt", "different")
    assert content_hash([a]) != content_hash([b])


def test_first_submission_creates_and_is_not_reused(store, tmp_path):
    doc = _write(tmp_path, "doc.txt", "some document text")
    key, reused = resolve_ontology_key(store, [doc], document_id="my-doc")
    assert reused is False
    meta = store.load_meta(key)
    assert meta.content_hash == content_hash([doc])


def test_identical_content_reused_even_under_a_different_document_id(store, tmp_path):
    doc = _write(tmp_path, "doc.txt", "identical bytes")
    key1, reused1 = resolve_ontology_key(store, [doc], document_id="name-one")
    assert reused1 is False

    key2, reused2 = resolve_ontology_key(store, [doc], document_id="name-two")
    assert reused2 is True
    assert key2 == key1  # same ontology, regardless of the label this time


def test_identical_content_reused_with_no_document_id_at_all(store, tmp_path):
    doc = _write(tmp_path, "doc.txt", "identical bytes again")
    key1, _ = resolve_ontology_key(store, [doc], document_id="named")
    key2, reused2 = resolve_ontology_key(store, [doc], document_id=None)
    assert reused2 is True
    assert key2 == key1


def test_revised_document_under_the_same_id_is_a_content_cache_miss_but_same_key(store, tmp_path):
    # Per the design this repo's plan states explicitly: a revision under the
    # same document_id becomes a new *version* of the same ontology (so
    # pinned concepts and assertions carry forward), not a different one —
    # `reused=False` because the content changed and needs re-extracting,
    # but the key stays the same since `get_or_create` resolves by name.
    doc = _write(tmp_path, "doc.txt", "version one")
    key1, _ = resolve_ontology_key(store, [doc], document_id="doc")

    doc = _write(tmp_path, "doc.txt", "version two, materially different")
    key2, reused2 = resolve_ontology_key(store, [doc], document_id="doc")
    assert reused2 is False
    assert key2 == key1
    assert store.load_meta(key1).content_hash == content_hash([doc])


def test_missing_document_id_gets_a_generated_name(store, tmp_path):
    doc = _write(tmp_path, "doc.txt", "no name supplied")
    key, _ = resolve_ontology_key(store, [doc], document_id=None)
    meta = store.load_meta(key)
    assert meta.name.startswith("doc-")
