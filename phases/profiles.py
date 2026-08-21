"""Domain profiles — the specialist half of a generic Phase 2.

DECIDED 2026-08-14 (todo/02): Phase 2 stays generic and takes **domain packs**
as input. API requirements and test-case generation become *one profile*, not
the architecture.

The load-bearing consequence: **a profile is data, not code.** Adding support
for clinical protocols or contracts must not mean editing Phase 2 — it means
adding a JSON file under `profiles/`. Anything that would have to be written in
Python to add a domain belongs in the generic core instead.

The ontology is an input to a profile, never shaped by one. A profile says how
to *read* a generic ontology (which concept names mean "an operation you can
call") and what to *ask about it* (checklist templates); it never changes what
Phase 1 extracts.

This also closes the sharp edge noted in todo/02: a trial protocol's outcome
measures used to project into `endpoints` unconditionally, so Phase 2 would have
tried to generate API tests for "Overall Survival". Bucket mapping now comes
from the selected profile, and the generic profile maps nothing.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).parent.parent / "profiles"

GENERIC_PROFILE_KEY = "generic"


@dataclass
class Profile:
    """A domain pack: how to read an ontology, and what to ask about it."""

    key: str
    name: str
    description: str = ""
    # concept bucket name -> alias fragments matched against discovered concepts.
    # This is what makes the Phase 2 projection domain-specific without any
    # domain vocabulary in the code.
    buckets: Dict[str, List[str]] = field(default_factory=dict)
    # Questions worth asking of any ontology in this domain. `{concept}` is
    # expanded per matching concept type.
    checklist_templates: List[Dict[str, str]] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)
    # bucket -> what an instance of it must look like. Deterministic type
    # checking, expressed as data so a domain's rules are added with the domain.
    shape_rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # How Phase 2 should write requirements for this domain: the expert role, what
    # a requirement is *for*, and the categories to cover. This is the last piece
    # of "API testing is one profile, not the architecture" — without it the
    # generator prompt is hardcoded to HTTP testing whatever the document was.
    requirements: Dict[str, Any] = field(default_factory=dict)
    # bucket -> the parsed total it should be measured against, for documents
    # that can be counted exactly. See phases/completeness.py.
    completeness: Dict[str, str] = field(default_factory=dict)
    # What a well-formed *requirement* looks like in this domain, mirroring
    # `shape_rules` for instances. Kept as data for the same reason: a domain
    # that means something else by "requirement" edits a file, not a module.
    requirement_rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # How Phase 3 should write test code for this domain — the same argument as
    # `requirements` one phase later. See test_generator.DEFAULT_TEST_GENERATION
    # for the parts, and note the JSON output contract is deliberately not among
    # them: an edit that renamed a field would parse to zero tests.
    test_generation: Dict[str, Any] = field(default_factory=dict)
    # What a well-formed generated *test* looks like here. See test_shapes.
    test_rules: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "description": self.description,
            "buckets": self.buckets,
            "checklist_templates": self.checklist_templates,
            "skills": self.skills,
            "shape_rules": self.shape_rules,
            "requirements": self.requirements,
            "completeness": self.completeness,
            "requirement_rules": self.requirement_rules,
            "test_generation": self.test_generation,
            "test_rules": self.test_rules,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Profile":
        return cls(
            key=data.get("key", ""),
            name=data.get("name", data.get("key", "")),
            description=data.get("description", ""),
            buckets=data.get("buckets", {}) or {},
            checklist_templates=data.get("checklist_templates", []) or [],
            skills=data.get("skills", []) or [],
            shape_rules=data.get("shape_rules", {}) or {},
            requirements=data.get("requirements", {}) or {},
            completeness=data.get("completeness", {}) or {},
            requirement_rules=data.get("requirement_rules", {}) or {},
            test_generation=data.get("test_generation", {}) or {},
            test_rules=data.get("test_rules", {}) or {},
        )


def _generic() -> Profile:
    """The fallback. Maps no buckets on purpose.

    An unknown domain must not be read as an API. Producing an empty `endpoints`
    list is the honest answer for a document nobody has said is an API spec.
    """
    return Profile(
        key=GENERIC_PROFILE_KEY,
        name="Generic",
        description="No domain assumptions. Concepts are passed through as they were discovered.",
        checklist_templates=[
            {
                "question": "Are all instances of {concept} captured, or only a sample?",
                "concept_type": "{concept}",
            }
        ],
        requirements={
            "role": "You are an analyst who writes verifiable requirements from source documents.",
            "subject": "the material described by this ontology",
            "goal": (
                "For each concept, write requirements that could be checked against the "
                "source document — what must be true, and how someone would confirm it."
            ),
            "categories": [
                "completeness",
                "consistency",
                "clarity",
                "verifiability",
            ],
            "id_format": "CONCEPT-CATEGORY-001",
        },
    )


def load_profiles(directory: Optional[Path] = None) -> Dict[str, Profile]:
    """Every profile on disk, plus the built-in generic one."""
    directory = Path(directory) if directory else PROFILES_DIR
    profiles = {GENERIC_PROFILE_KEY: _generic()}

    if not directory.exists():
        logger.debug(f"No profiles directory at {directory}; generic only")
        return profiles

    for path in sorted(directory.glob("*.json")):
        try:
            profile = Profile.from_dict(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError) as e:
            # One malformed pack must not remove every other domain.
            logger.error(f"Profile {path.name} is unreadable and was skipped: {e}")
            continue

        if not profile.key:
            logger.error(f"Profile {path.name} has no key and was skipped")
            continue
        profiles[profile.key] = profile

    return profiles


def get_profile(key: str, directory: Optional[Path] = None) -> Profile:
    """Resolve a profile key, falling back to generic rather than failing.

    An unknown key is a configuration mistake, not a reason to lose a run — and
    generic is the safe reading, since it asserts nothing about the domain.
    """
    profiles = load_profiles(directory)
    profile = profiles.get((key or "").strip().lower())
    if profile is None:
        if key:
            logger.warning(f"Unknown profile {key!r}; using the generic profile")
        return profiles[GENERIC_PROFILE_KEY]
    return profile


def checklist_for(profile: Profile, ontology) -> List[Dict[str, str]]:
    """Expand a profile's templates against the concepts this ontology actually has.

    Templates naming `{concept}` are expanded once per concept type, so the
    questions asked are about what was found rather than what the domain
    generally contains.
    """
    items: List[Dict[str, str]] = []
    concept_names = [ct.name for ct in ontology.concept_types]

    for template in profile.checklist_templates:
        question = template.get("question", "")
        concept_slot = template.get("concept_type", "")

        if "{concept}" not in question and "{concept}" not in concept_slot:
            items.append({"question": question, "concept_type": concept_slot})
            continue

        for name in concept_names:
            items.append(
                {
                    "question": question.replace("{concept}", name),
                    "concept_type": concept_slot.replace("{concept}", name) or name,
                }
            )

    return items
