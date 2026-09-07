"""Proves infer_shape_profile clamps a model's proposal down to what's
actually safe to apply against a bare id+text claim (see the module's own
docstring for exactly why require_subject/id_pattern can't be trusted
verbatim), and never lets a malformed response escape as an exception except
the one documented case (nothing usable at all).
"""

import json

import pytest

from phases.shape_profile_inference import ShapeProfileInferenceError, infer_shape_profile
from tests.conftest import FakeLLMClient


def test_require_subject_is_always_forced_off():
    client = FakeLLMClient([json.dumps({"require_subject": True, "description": "x"})])
    result = infer_shape_profile([], "an API spec", client)
    assert result.rules["requirement"]["require_subject"] is False
    assert any("require_subject" in n for n in result.notes)


def test_require_any_of_gets_expected_behavior_injected():
    client = FakeLLMClient([json.dumps({"require_any_of": ["criteria"]})])
    result = infer_shape_profile([], "", client)
    assert "expected_behavior" in result.rules["requirement"]["require_any_of"]
    assert any("expected_behavior" in n for n in result.notes)


def test_require_any_of_not_duplicated_when_model_already_included_it():
    client = FakeLLMClient([json.dumps({"require_any_of": ["expected_behavior", "criteria"]})])
    result = infer_shape_profile([], "", client)
    assert result.rules["requirement"]["require_any_of"].count("expected_behavior") == 1


def test_id_pattern_is_always_dropped():
    client = FakeLLMClient([json.dumps({"id_pattern": r"^REQ-\d+$"})])
    result = infer_shape_profile([], "", client)
    assert "id_pattern" not in result.rules["requirement"]
    assert any("id_pattern" in n for n in result.notes)


def test_invalid_subject_pattern_is_dropped_not_raised():
    client = FakeLLMClient([json.dumps({
        "wants_subject_check": True,
        "subject_pattern": "(unclosed",
    })])
    result = infer_shape_profile([], "", client)
    assert "subject_pattern" not in result.rules["requirement"]
    assert any("not a valid regex" in n for n in result.notes)


def test_valid_subject_pattern_is_kept_as_informational():
    client = FakeLLMClient([json.dumps({
        "wants_subject_check": True,
        "subject_pattern": "^(GET|POST|PUT|DELETE)\\s+/",
    })])
    result = infer_shape_profile([], "", client)
    assert result.rules["requirement"]["subject_pattern"] == "^(GET|POST|PUT|DELETE)\\s+/"
    # Still forced off — the pattern is stored, never enforced, in this version.
    assert result.rules["requirement"]["require_subject"] is False


def test_require_fields_drops_anything_outside_the_safe_set():
    client = FakeLLMClient([json.dumps({"require_fields": ["title", "description", "id"]})])
    result = infer_shape_profile([], "", client)
    assert set(result.rules["requirement"]["require_fields"]) == {"title", "id"}
    assert any("description" in n for n in result.notes)


def test_unparseable_response_raises():
    client = FakeLLMClient(["not json at all"])
    with pytest.raises(ShapeProfileInferenceError):
        infer_shape_profile([], "", client)


def test_empty_object_raises():
    client = FakeLLMClient(["{}"])
    with pytest.raises(ShapeProfileInferenceError):
        infer_shape_profile([], "", client)


def test_json_array_instead_of_object_raises():
    client = FakeLLMClient(["[1, 2, 3]"])
    with pytest.raises(ShapeProfileInferenceError):
        infer_shape_profile([], "", client)


def test_fenced_json_is_parsed():
    client = FakeLLMClient(["```json\n" + json.dumps({"description": "fenced"}) + "\n```"])
    result = infer_shape_profile([], "", client)
    assert result.rules["requirement"]["description"] == "fenced"


def test_oversized_description_is_truncated_not_rejected():
    client = FakeLLMClient([json.dumps({"description": "x" * 5000})])
    result = infer_shape_profile([], "", client)
    assert len(result.rules["requirement"]["description"]) <= 500


def test_only_one_llm_call_made():
    client = FakeLLMClient([json.dumps({"description": "one call"})])
    infer_shape_profile([{"name": "Grant", "description": "..."}], "an API spec", client)
    assert client.call_count == 1
