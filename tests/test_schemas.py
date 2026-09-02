"""ClaimInput.source_ref — optional provenance for a claim, so a caller
that extracted claims from some other document (a chatbot answer, a
README, a marketing page) can trace a flagged one back to where it came
from. Pure schema-level: never read by validation logic itself.
"""

from claimvalidator.schemas import ClaimInput


def test_source_ref_defaults_to_none():
    claim = ClaimInput(id="C1", text="a claim")
    assert claim.source_ref is None


def test_source_ref_accepts_an_explicit_value():
    claim = ClaimInput(id="C1", text="a claim", source_ref="chatbot answer, sentence 2")
    assert claim.source_ref == "chatbot answer, sentence 2"
