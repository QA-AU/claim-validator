"""claimvalidator/claim_retrieval.py::_split_claim_into_clauses — the
compound-claim detector for issue #3. Pure function, no searcher/LLM
needed. See test_claim_retrieval.py for how this feeds into retrieval.
"""
from claimvalidator.claim_retrieval import _split_claim_into_clauses


def test_a_simple_claim_does_not_split():
    assert _split_claim_into_clauses("The verifier reads YAML or JSON.") == []


def test_a_short_and_phrase_does_not_split():
    # "X and Y" as a plain noun phrase, no comma/semicolon before it — not
    # the "this is actually two facts" signal this looks for.
    assert _split_claim_into_clauses("The request and response are logged.") == []


def test_comma_and_splits_into_two_clauses():
    claim = "The gate waits for the network to be idle, and it waits for fonts to load."
    assert _split_claim_into_clauses(claim) == [
        "The gate waits for the network to be idle",
        "it waits for fonts to load",
    ]


def test_semicolon_and_splits_into_two_clauses():
    claim = "Retry once on a connection error; and mark the operation failed otherwise."
    assert _split_claim_into_clauses(claim) == [
        "Retry once on a connection error",
        "mark the operation failed otherwise",
    ]


def test_a_conjunction_inside_a_backtick_span_is_not_a_split_point():
    claim = "The header is `Content-Type: application/json, and charset=utf-8`."
    assert _split_claim_into_clauses(claim) == []


def test_a_conjunction_inside_a_quoted_string_is_not_a_split_point():
    claim = 'The response body is "success, and complete".'
    assert _split_claim_into_clauses(claim) == []


def test_a_real_split_survives_alongside_an_unrelated_protected_span():
    # One conjunction is inside a quoted string (protected), the other is a
    # genuine clause boundary — only the real one should split.
    claim = 'The status is "ok, and done", and the operation is marked complete.'
    result = _split_claim_into_clauses(claim)
    assert len(result) == 2
    assert result[0] == 'The status is "ok, and done"'
    assert result[1] == "the operation is marked complete"


def test_multiple_sentences_each_contribute_their_own_clauses():
    claim = (
        "Wash hands with soap and water for 20 seconds, and dry them "
        "completely. Then discard the paper towel, and close the tap "
        "with it."
    )
    result = _split_claim_into_clauses(claim)
    assert len(result) == 4


def test_three_way_split_on_one_sentence():
    claim = "Do A, and do B; and do C."
    assert _split_claim_into_clauses(claim) == ["Do A", "do B", "do C"]


def test_trailing_terminal_punctuation_is_trimmed_from_the_last_clause():
    claim = "Wait for the page, and capture the screenshot!"
    assert _split_claim_into_clauses(claim)[-1] == "capture the screenshot"
