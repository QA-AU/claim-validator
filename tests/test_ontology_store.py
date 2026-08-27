"""Tests for the ontology store — ontologies as entities, runs as versions."""

import pytest

from phases.ontology_store import OntologyStore, slugify


@pytest.fixture
def store(tmp_path):
    return OntologyStore(str(tmp_path / "ontologies"))


class TestIdentity:
    def test_key_is_name_plus_short_id(self, store):
        meta = store.create("Orders API")
        assert meta.key.startswith("orders-api-")
        assert len(meta.short_id) == 4

    def test_same_name_gets_distinct_keys(self, store):
        """Two projects may share a name; their stores must not collide."""
        a = store.create("Orders API")
        b = store.create("Orders API")
        assert a.key != b.key


class TestPathTraversal:
    """`path_for` is the single choke point every lookup goes through
    (load_meta, load_current, load_index, has_index...), and `key` reaches
    it straight from an API caller on two routes (GET /api/ontologies/{key},
    and ValidationRequest.ontology_key) with no other validation in front
    of it — found in a vulnerability scan."""

    def test_rejects_a_key_containing_path_separators(self, store):
        with pytest.raises(ValueError):
            store.path_for("../../etc")

    def test_rejects_an_absolute_path_as_a_key(self, store):
        with pytest.raises(ValueError):
            store.path_for("/etc/passwd")

    def test_accepts_a_real_generated_key(self, store):
        meta = store.create("Orders API")
        assert store.path_for(meta.key) == store.root / meta.key

    def test_name_normalisation(self):
        assert slugify("Orders API") == slugify("orders   api") == "orders-api"
        assert slugify("") == "ontology"

    def test_blank_name_rejected(self, store):
        """The name keys the assertion store, so it cannot be empty."""
        with pytest.raises(ValueError):
            store.create("   ")

    def test_get_or_create_is_stable_across_runs(self, store):
        """Re-running the same project must resolve to the same ontology."""
        first = store.get_or_create("Orders API", "REST docs")
        second = store.get_or_create("orders api")
        assert first.key == second.key

    def test_list_skips_legacy_run_directories(self, store, tmp_path):
        store.create("Real One")
        (tmp_path / "ontologies" / "da32d077").mkdir(parents=True)  # old run-keyed dir
        assert [m.name for m in store.list()] == ["Real One"]


class TestVersions:
    def test_saving_a_version_never_overwrites_history(self, store):
        meta = store.create("Docs")
        store.save_version(meta.key, {"name": "Docs", "v": 1}, "wf-001")
        store.save_version(meta.key, {"name": "Docs", "v": 2}, "wf-002")

        assert len(store.list_versions(meta.key)) == 2
        assert store.load_current(meta.key)["v"] == 2

    def test_workflow_id_stays_traceable_in_the_filename(self, store):
        meta = store.create("Docs")
        path = store.save_version(meta.key, {"name": "Docs"}, "wf-abc123")
        assert "wf-abc123" in path.name

    def test_rollback_to_an_earlier_version(self, store):
        meta = store.create("Docs")
        first = store.save_version(meta.key, {"v": 1}, "wf-001")
        store.save_version(meta.key, {"v": 2}, "wf-002")

        store.set_current(meta.key, first.name)

        assert store.load_current(meta.key)["v"] == 1
        assert len(store.list_versions(meta.key)) == 2, "rollback must not delete history"


class TestPinnedSchema:
    def test_pinning_survives_reload(self, store):
        meta = store.create("Docs")
        store.pin_schema(meta.key, [{"name": "biomarker", "surface_terms": ["PD-L1"]}])

        reloaded = store.load_meta(meta.key)
        assert [c["name"] for c in reloaded.pinned_concept_types] == ["biomarker"]

    def test_clearing_allows_rediscovery(self, store):
        meta = store.create("Docs")
        store.pin_schema(meta.key, [{"name": "biomarker"}])
        store.clear_pinned_schema(meta.key)
        assert store.load_meta(meta.key).pinned_concept_types == []


class TestAssertionsSurviveExtraction:
    def test_reset_keeps_user_answers_by_default(self, store):
        """Extraction is cheap to redo; human input is not."""
        meta = store.create("Docs")
        store.save_assertions(meta.key, [{"name": "rate limit", "source": "user_assertion"}])
        store.save_version(meta.key, {"v": 1}, "wf-001")

        store.reset(meta.key)

        assert store.list_versions(meta.key) == []
        assert store.load_current(meta.key) is None
        assert len(store.load_assertions(meta.key)) == 1, "answers must survive a reset"

    def test_reset_can_discard_answers_explicitly(self, store):
        meta = store.create("Docs")
        store.save_assertions(meta.key, [{"name": "rate limit"}])

        store.reset(meta.key, keep_assertions=False)

        assert store.load_assertions(meta.key) == []

    def test_reset_clears_the_pinned_schema(self, store):
        meta = store.create("Docs")
        store.pin_schema(meta.key, [{"name": "x"}])
        store.reset(meta.key)
        assert store.load_meta(meta.key).pinned_concept_types == []

    def test_reset_cost_names_the_human_work(self, store):
        """A confirmation must state what it destroys, not ask 'are you sure?'."""
        meta = store.create("Docs")
        store.save_version(meta.key, {"v": 1}, "wf-001")
        store.save_assertions(meta.key, [{"a": 1}, {"a": 2}])
        store.save_checklist(meta.key, {"issues": [{"i": 1}, {"i": 2}, {"i": 3}]})

        cost = store.describe_cost_of_reset(meta.key)

        assert cost == {"versions": 1, "assertions": 2, "checklist_decisions": 3}

    def test_delete_removes_everything(self, store):
        meta = store.create("Docs")
        store.save_assertions(meta.key, [{"a": 1}])
        store.delete(meta.key)
        assert store.list() == []


class TestPinnedSchemaAcrossRuns:
    """The reason pinning exists: a re-run must produce the same concept names."""

    def test_second_run_reuses_the_first_run_schema(self, store, tmp_path):
        import json
        from phases.phase1_orchestrator import run_phase1

        class DriftingLLM:
            """Names the same idea differently each run — the real observed behaviour."""

            def __init__(self, concept_name):
                self.concept_name = concept_name
                self.discovery_calls = 0

            def generate(self, prompt):
                low = prompt.lower()
                if "concept types" in low and "decide what kinds of" in low:
                    self.discovery_calls += 1
                    return json.dumps([{
                        "name": self.concept_name,
                        "description": "a thing",
                        "surface_terms": ["token", "bearer"],
                        "attributes": ["scheme"],
                    }])
                if prompt.strip().startswith("Extract every instance"):
                    return json.dumps([{"name": "bearer_token", "attributes": {}}])
                return "[]"

        doc = tmp_path / "spec.txt"
        doc.write_text("Authentication uses a bearer token in the Authorization header.\n" * 20)

        meta = store.get_or_create("Auth Docs", "API documentation")

        first = DriftingLLM("authentication")
        run_phase1(workflow_id="wf-1", name=meta.name, document_paths=[str(doc)],
                   llm_client=first, output_dir=str(tmp_path / "out"),
                   background_description="API documentation",
                   ontology_key=meta.key, store=store)

        # A second run whose model would have chosen a different name
        second = DriftingLLM("authentication_mechanism")
        run_phase1(workflow_id="wf-2", name=meta.name, document_paths=[str(doc)],
                   llm_client=second, output_dir=str(tmp_path / "out"),
                   background_description="API documentation",
                   ontology_key=meta.key, store=store)

        assert first.discovery_calls == 1, "first run must discover the schema"
        assert second.discovery_calls == 0, "second run must reuse it, not re-discover"

        names = {c["name"] for c in store.load_current(meta.key)["concept_types"]}
        assert names == {"authentication"}, f"schema drifted: {names}"
        assert len(store.list_versions(meta.key)) == 2, "each run is a version"


def test_discarding_a_census_says_which_ones_went(tmp_path):
    """A census is the most expensive thing this pipeline buys. Losing one in
    silence is how a run reports completeness as unestablished with no hint it
    was established before the chunk stream was renumbered."""
    from phases.ontology_store import OntologyStore

    store = OntologyStore(str(tmp_path))
    store.save_census("k", "endpoint", {"count": 34})
    store.save_census("k", "error_code", {"count": 32})

    dropped = store.clear_censuses("k")

    assert dropped == ["endpoint", "error_code"]
    assert store.load_censuses("k") == {}


def test_rebuilding_the_index_reports_what_it_dropped(tmp_path):
    from phases.ontology_store import OntologyStore

    store = OntologyStore(str(tmp_path))
    store.save_census("k", "endpoint", {"count": 34})

    store.save_index("k", ["a chunk"], [{}])

    assert store.dropped_censuses == ["endpoint"]


def test_dropping_nothing_reports_nothing(tmp_path):
    from phases.ontology_store import OntologyStore

    store = OntologyStore(str(tmp_path))

    store.save_index("k", ["a chunk"], [{}])

    assert store.dropped_censuses == []
