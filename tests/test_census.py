"""Censusing every concept in one pass over the document.

`census` (tested in test_expansion_and_census.py) reads the whole document once
per concept, so an eight-concept ontology reads it eight times and cost is
chunks × concepts. `census_many` reads it once for all of them.

The saving is obvious; the question is whether it costs recall, because a census
exists to be ground truth and one that quietly misses instances is worse than
none — it produces a confident wrong denominator. Measured on the fulfilment
API: 8× fewer calls, 7.3× less input, and nothing lost. See todo/14 for the
comparison and the caveat that came with it.
"""

import json

# --- one pass for every concept ---------------------------------------------
#
# `census` reads the whole document once per concept, so cost is chunks ×
# concepts. `census_many` reads it once for all of them. Measured on the
# fulfilment API: 8× fewer calls, 7.3× less input, and nothing lost — see
# todo/14 for the recall comparison and its caveat.


class MultiClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def generate(self, prompt, system_prompt=None, temperature=None):
        self.prompts.append(prompt)
        return self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]


def _sighting(concept, name, chunk):
    return {"concept": concept, "name": name, "chunk": chunk}


def test_every_concept_is_censused_in_one_pass():
    from phases.census import census_many

    client = MultiClient(json.dumps([
        _sighting("endpoint", "GET /orders", 0),
        _sighting("error_code", "not_found", 1),
    ]))

    results = census_many([("endpoint", "an operation"), ("error_code", "an error")],
                          ["a", "b"], client, batch_size=10)

    assert len(client.prompts) == 1          # one call, not one per concept
    assert results["endpoint"].count == 1
    assert results["error_code"].count == 1


def test_a_concept_with_nothing_in_the_document_is_still_reported():
    """Absent is a finding. Omitting the key would look like it was never asked."""
    from phases.census import census_many

    client = MultiClient(json.dumps([_sighting("endpoint", "GET /orders", 0)]))

    results = census_many([("endpoint", ""), ("nowhere", "")], ["a"], client)

    assert results["nowhere"].count == 0
    assert results["nowhere"].complete is True


def test_a_concept_the_model_invented_is_ignored():
    from phases.census import census_many

    client = MultiClient(json.dumps([
        _sighting("endpoint", "GET /orders", 0),
        _sighting("something_else", "made up", 0),
    ]))

    results = census_many([("endpoint", "")], ["a"], client)

    assert list(results) == ["endpoint"]
    assert results["endpoint"].count == 1


def test_the_concept_name_is_matched_case_insensitively():
    """The model echoes the name back and capitalisation drifts."""
    from phases.census import census_many

    client = MultiClient(json.dumps([_sighting("Endpoint", "GET /orders", 0)]))

    assert census_many([("endpoint", "")], ["a"], client)["endpoint"].count == 1


def test_a_citation_outside_the_batch_is_dropped_not_stored():
    """Same rule as extraction: an invented location sends targeted extraction
    to the wrong passage, which is worse than having no location."""
    from phases.census import census_many

    client = MultiClient(json.dumps([_sighting("endpoint", "GET /orders", 99)]))

    result = census_many([("endpoint", "")], ["a"], client, batch_size=10)["endpoint"]

    assert result.count == 1
    assert result.chunk_of == {}


def test_the_same_instance_twice_is_one_instance():
    from phases.census import census_many

    client = MultiClient(json.dumps([
        _sighting("endpoint", "GET /orders", 0),
        _sighting("endpoint", "get /orders", 0),
    ]))

    assert census_many([("endpoint", "")], ["a"], client)["endpoint"].count == 1


def test_a_failed_batch_fails_it_for_every_concept():
    """One call covered them all, so none of them read the whole document."""
    from phases.census import census_many

    class Exploding:
        def generate(self, prompt, system_prompt=None, temperature=None):
            raise RuntimeError("overloaded")

    results = census_many([("endpoint", ""), ("error_code", "")], ["a"], Exploding())

    assert all(r.failed_batches == 1 for r in results.values())
    assert all(r.complete is False for r in results.values())


# --- repeating a census, because one run is not a denominator ----------------


def test_a_census_is_reported_as_a_range_not_a_count():
    """Two identical census runs on 182 chunks of prose returned 294 and 342."""
    from phases.census import census_repeated

    class Drifting:
        def __init__(self):
            self.n = 0

        def generate(self, prompt, system_prompt=None, temperature=None):
            self.n += 1
            names = ["a", "b"] if self.n == 1 else ["a"]
            return json.dumps([_sighting("endpoint", x, 0) for x in names])

    spread = census_repeated([("endpoint", "")], ["chunk"], Drifting(), runs=3)["endpoint"]

    assert spread.low == 1 and spread.high == 2
    assert "between 1 and 2" in spread.spread


def test_a_stable_census_says_so_rather_than_implying_a_range():
    from phases.census import census_repeated

    client = MultiClient(json.dumps([_sighting("endpoint", "GET /orders", 0)]))

    spread = census_repeated([("endpoint", "")], ["chunk"], client, runs=3)["endpoint"]

    assert "in every one of 3 run(s)" in spread.spread


def test_names_are_split_by_how_many_runs_saw_them():
    """One sighting is a lead; a repeated one is a finding — the judge's rule."""
    from phases.census import census_repeated

    class Drifting:
        def __init__(self):
            self.n = 0

        def generate(self, prompt, system_prompt=None, temperature=None):
            self.n += 1
            names = ["solid", "flaky"] if self.n < 3 else ["solid"]
            return json.dumps([_sighting("endpoint", x, 0) for x in names])

    spread = census_repeated([("endpoint", "")], ["c"], Drifting(), runs=3)["endpoint"]

    assert spread.agreed == ["solid"]
    assert "flaky" in spread.probable          # 2 of 3 is a majority
    assert spread.once_only == []


def test_capture_is_an_interval_when_the_census_moved():
    from phases.census import CensusSpread

    spread = CensusSpread(concept="c", counts=[294, 342], runs=2)

    worst, best = spread.capture_range(200)

    assert round(worst, 2) == 0.58
    assert round(best, 2) == 0.68


def test_capture_never_exceeds_one():
    """Extraction finding more than the census is a naming disagreement, not
    better-than-complete coverage."""
    from phases.census import CensusSpread

    assert CensusSpread(concept="c", counts=[5, 5], runs=2).capture_range(9) == (1.0, 1.0)
