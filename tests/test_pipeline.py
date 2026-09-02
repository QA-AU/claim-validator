"""run_validation's ontology_key bypass — the "pick from the shared list"
path. Only the resolution branch itself is tested here: a full run against
a real, populated ontology needs a real (or heavily stubbed) LLM client and
is covered by scripts/validate_claims.py and manual verification instead.
"""

import pytest

from claimvalidator.pipeline import ClaimResult, run_validation


def test_claim_result_serializes_source_ref():
    result = ClaimResult(
        id="C1", text="a claim", shape_ok=True, shape_reason=None,
        verdict="entails", judged=True, agreement="3/3", cited_chunks=[0],
        reason="r", source_ref="chatbot answer, sentence 2",
    )
    assert result.to_dict()["source_ref"] == "chatbot answer, sentence 2"


def test_claim_result_source_ref_defaults_to_none_in_serialization():
    result = ClaimResult(
        id="C1", text="a claim", shape_ok=True, shape_reason=None,
        verdict="entails", judged=True, agreement="3/3", cited_chunks=[0],
        reason="r",
    )
    assert result.to_dict()["source_ref"] is None


def test_ontology_key_for_a_nonexistent_ontology_raises_clearly(tmp_path):
    with pytest.raises(ValueError, match="No such ontology"):
        run_validation(
            workflow_id="wf-test",
            document_paths=[],
            claims_input=[{"id": "C1", "text": "a claim"}],
            llm_client=object(),  # never reached — resolution fails first
            store_root=str(tmp_path),
            ontology_key="does-not-exist",
        )
