"""run_validation's ontology_key bypass — the "pick from the shared list"
path. Only the resolution branch itself is tested here: a full run against
a real, populated ontology needs a real (or heavily stubbed) LLM client and
is covered by scripts/validate_claims.py and manual verification instead.
"""

import pytest

from claimvalidator.pipeline import run_validation


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
