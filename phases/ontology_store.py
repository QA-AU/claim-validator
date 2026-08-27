"""Ontology store — an ontology is a named entity, runs are versions inside it.

Before this, an ontology was a by-product of a run: the output directory was
keyed by `workflow_id`, so re-processing the same material produced an unrelated
directory with no link to what came before. There was no way to list what
existed, switch between them, or re-extract *into* one — and user assertions
(which must survive re-extraction) had nowhere to live.

Layout:

    ontologies/
      orders-api-7f3a/            <- the ontology. stable, switchable.
        meta.json                 <- name, short_id, description, pinned schema
        current.json              <- the active version
        assertions.json           <- user answers, survive re-extraction
        checklist.json            <- checklist state
        versions/
          2026-08-14T10-02_da32d077.json

Directory is identity; files inside are history. `workflow_id` stays in each
version filename so a run remains traceable.

See todo/08-ontology-lifecycle-and-switching.md for the agreed design.
"""

import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Re-exported: callers have always imported `slugify` from here, and the ontology
# key and instance ids must be normalised the same way or assertions stop
# matching their instances.
from phases.phase1_models import slugify

logger = logging.getLogger(__name__)

META_FILE = "meta.json"
CURRENT_FILE = "current.json"
ASSERTIONS_FILE = "assertions.json"
CHECKLIST_FILE = "checklist.json"
INDEX_FILE = "index.json"
REQUIREMENTS_FILE = "requirements.json"
# Phase 3's suite, kept beside the requirements for the same reason they are
# kept beside the ontology: a person looking at an ontology wants the tests that
# came out of it, not a directory of run ids to search through.
TESTS_FILE = "tests.json"
EMBEDDINGS_FILE = "embeddings.npy"
# Census results, keyed by concept. Persisted because a census is the expensive
# half of a coverage measurement and its answer does not change while the
# document does not — it is the denominator for every later run.
CENSUS_FILE = "census.json"
VERSIONS_DIR = "versions"

# What OntologyMeta.key ever actually looks like: slugify() output
# ([a-z0-9-]+) plus a hex short_id. Kept permissive of case/underscore
# rather than pinned to that exact shape, since the point is blocking path
# separators and ".." — not re-deriving the key format here.
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_SHORT_ID_BYTES = 2  # 4 hex chars — enough to separate same-named projects


@dataclass
class OntologyMeta:
    """Identity and settings for one ontology, persisted as meta.json."""

    name: str
    short_id: str
    background_description: str = ""
    # Pinned concept schema (todo/01). Discovered once, reused on re-extraction
    # so diffs reflect content changes rather than renames, and assertions keep
    # matching. Empty until the first successful run.
    pinned_concept_types: List[Dict[str, Any]] = field(default_factory=list)
    # Which domain pack Phase 2 reads this ontology through (todo/02). Data, not
    # code: "generic" asserts nothing, which is the right default for material
    # nobody has said is an API spec.
    profile: str = "generic"
    # Optional human-written note about what the document contains (see
    # phases/brief.py). Stored on the ontology so every re-extraction reuses it
    # rather than depending on whoever kicked off the run remembering to attach
    # it again.
    brief: Dict[str, Any] = field(default_factory=dict)
    # Added in claim-validator, not present in the repo this file was copied
    # from. Separate from `key`/`short_id`: those exist so a re-extraction of
    # a *revised* document keeps carrying its assertions forward, which is
    # exactly the case a content hash must not collapse into "same ontology"
    # (see the comment on `create()`). `content_hash` answers a different
    # question — "have I already built this, byte-for-byte" — for automatic
    # cache reuse, never for identity.
    content_hash: str = ""
    # Added for the shared multi-user model — who built this ontology.
    # Set once, at creation, and never overwritten by a later reuse (see
    # get_or_create()): the ontology is immutable and shared, but who
    # originally paid to build it stays attributable.
    created_by: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    @property
    def key(self) -> str:
        return f"{slugify(self.name)}-{self.short_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "short_id": self.short_id,
            "key": self.key,
            "background_description": self.background_description,
            "pinned_concept_types": self.pinned_concept_types,
            "profile": self.profile,
            "brief": self.brief,
            "content_hash": self.content_hash,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OntologyMeta":
        return cls(
            name=data["name"],
            short_id=data["short_id"],
            background_description=data.get("background_description", ""),
            pinned_concept_types=data.get("pinned_concept_types", []),
            profile=data.get("profile", "generic"),
            brief=data.get("brief", {}) or {},
            content_hash=data.get("content_hash", ""),
            created_by=data.get("created_by", ""),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
        )


class OntologyStore:
    """Filesystem store for ontologies as first-class entities."""

    def __init__(self, root: str):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- identity ---------------------------------------------------------

    def path_for(self, key: str) -> Path:
        # `key` always comes out of OntologyMeta.key (slugify() + a hex
        # short_id — see the class docstring below) for anything this store
        # created itself, but it also arrives here straight from an API
        # caller: GET /api/ontologies/{key} and POST /api/validations'
        # ontology_key both reach this unchanged. Reject anything outside
        # that safe charset before it becomes a path, so a key like
        # "../../etc" can't walk this lookup outside self.root.
        if not _SAFE_KEY.match(key):
            raise ValueError(f"Invalid ontology key: {key!r}")
        return self.root / key

    def create(
        self, name: str, background_description: str = "", created_by: str = ""
    ) -> OntologyMeta:
        """Create a new ontology. The short_id is assigned once, here.

        Deliberately not derived from content: a content hash would change on
        every document edit, which is precisely when carrying assertions forward
        matters most.
        """
        if not name or not name.strip():
            raise ValueError("Ontology name is required")

        meta = OntologyMeta(
            name=name.strip(),
            short_id=secrets.token_hex(_SHORT_ID_BYTES),
            background_description=background_description,
            created_by=created_by,
        )
        directory = self.path_for(meta.key)
        (directory / VERSIONS_DIR).mkdir(parents=True, exist_ok=True)
        self._write_meta(meta)
        logger.info(f"Created ontology '{meta.name}' ({meta.key})")
        return meta

    def find_by_name(self, name: str) -> Optional[OntologyMeta]:
        """Find an existing ontology by user-facing name (case-insensitive)."""
        target = slugify(name)
        for meta in self.list():
            if slugify(meta.name) == target:
                return meta
        return None

    def find_by_content_hash(self, content_hash: str) -> Optional[OntologyMeta]:
        """Find an ontology already built from this exact document content.

        Added in claim-validator for automatic cache reuse — a validation job
        against byte-identical document content should never pay to rebuild
        the ontology, whatever name or document_id the caller happens to use
        this time. Empty hash never matches, so metas predating this field
        (content_hash == "") don't collide with each other.
        """
        if not content_hash:
            return None
        for meta in self.list():
            if meta.content_hash == content_hash:
                return meta
        return None

    def get_or_create(
        self, name: str, background_description: str = "", created_by: str = ""
    ) -> OntologyMeta:
        """Resolve a name to an ontology, creating it on first use.

        `created_by` only ever applies to a fresh creation — reusing an
        existing ontology never reassigns who originally built it, even
        when a different user's request is what resolved to it.
        """
        existing = self.find_by_name(name)
        if existing:
            if background_description and background_description != existing.background_description:
                existing.background_description = background_description
                self._write_meta(existing)
            return existing
        return self.create(name, background_description, created_by=created_by)

    def list(self) -> List[OntologyMeta]:
        """Every ontology that exists, newest first."""
        metas = []
        for directory in self.root.iterdir():
            if not directory.is_dir():
                continue
            meta_path = directory / META_FILE
            if not meta_path.exists():
                continue  # a legacy run-keyed directory, not an ontology
            try:
                metas.append(OntologyMeta.from_dict(json.loads(meta_path.read_text())))
            except Exception as e:
                logger.warning(f"Skipping unreadable ontology at {directory}: {e}")
        return sorted(metas, key=lambda m: m.created_at, reverse=True)

    def load_meta(self, key: str) -> Optional[OntologyMeta]:
        meta_path = self.path_for(key) / META_FILE
        if not meta_path.exists():
            return None
        return OntologyMeta.from_dict(json.loads(meta_path.read_text()))

    def _write_meta(self, meta: OntologyMeta) -> None:
        meta.updated_at = datetime.now().isoformat()
        path = self.path_for(meta.key)
        path.mkdir(parents=True, exist_ok=True)
        (path / META_FILE).write_text(json.dumps(meta.to_dict(), indent=2))

    # -- schema pinning ---------------------------------------------------

    def pin_schema(self, key: str, concept_types: List[Dict[str, Any]]) -> None:
        """Store the discovered concept schema for reuse on later runs."""
        meta = self.load_meta(key)
        if meta is None:
            raise ValueError(f"No such ontology: {key}")
        meta.pinned_concept_types = concept_types
        self._write_meta(meta)
        logger.info(
            f"Pinned {len(concept_types)} concept types for '{meta.name}' — "
            f"later runs reuse them instead of re-discovering"
        )

    def set_profile(self, key: str, profile: str) -> OntologyMeta:
        """Choose which domain pack Phase 2 reads this ontology through."""
        from phases.profiles import get_profile

        meta = self.load_meta(key)
        if meta is None:
            raise ValueError(f"No such ontology: {key}")

        # Resolved through get_profile so an unknown key falls back to generic
        # here rather than at read time, when it would be far less visible.
        meta.profile = get_profile(profile).key
        self._write_meta(meta)
        logger.info(f"Ontology '{meta.name}' now uses the {meta.profile!r} profile")
        return meta

    def set_brief(self, key: str, brief) -> OntologyMeta:
        """Attach a brief to an ontology, so every later run reuses it."""
        meta = self.load_meta(key)
        if meta is None:
            raise ValueError(f"No such ontology: {key}")
        meta.brief = brief.to_dict() if brief is not None else {}
        self._write_meta(meta)
        logger.info(f"Brief attached to '{meta.name}' ({len(meta.brief.get('raw', ''))} chars)")
        return meta

    def load_brief(self, key: str):
        """The stored brief, or an empty one."""
        from phases.brief import Brief

        meta = self.load_meta(key)
        return Brief.from_dict(meta.brief if meta else None)

    def clear_pinned_schema(self, key: str) -> None:
        """Allow the next run to re-discover concept types from scratch."""
        meta = self.load_meta(key)
        if meta is None:
            raise ValueError(f"No such ontology: {key}")
        meta.pinned_concept_types = []
        self._write_meta(meta)

    # -- versions ---------------------------------------------------------

    def save_version(
        self, key: str, ontology_dict: Dict[str, Any], workflow_id: str, make_current: bool = True
    ) -> Path:
        """Write a new version. Never overwrites history.

        `make_current=False` stores the version without promoting it, which is
        what a reviewed re-extraction needs: the new version has to be diffable
        and checkable *before* it becomes the thing Phase 2 reads.
        """
        directory = self.path_for(key)
        versions = directory / VERSIONS_DIR
        versions.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        version_path = versions / f"{stamp}_{workflow_id}.json"
        payload = json.dumps(ontology_dict, indent=2)
        version_path.write_text(payload)

        if make_current:
            (directory / CURRENT_FILE).write_text(payload)
            logger.info(f"Saved version {version_path.name} and set as current")
        else:
            logger.info(f"Saved version {version_path.name}, current unchanged (awaiting review)")

        return version_path

    def list_versions(self, key: str) -> List[Path]:
        """Version files, oldest first."""
        versions = self.path_for(key) / VERSIONS_DIR
        if not versions.exists():
            return []
        return sorted(versions.glob("*.json"))

    def load_version(self, key: str, version_file: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(key) / VERSIONS_DIR / version_file
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def diff_versions(self, key: str, before: str, after: str):
        """Compare two stored versions of this ontology.

        Names are version filenames as returned by `list_versions`. Imported
        locally so the store keeps no import-time dependency on the diff.
        """
        from phases.ontology_diff import diff_ontologies

        before_payload = self.load_version(key, before)
        after_payload = self.load_version(key, after)
        if before_payload is None:
            raise ValueError(f"No such version: {before}")
        if after_payload is None:
            raise ValueError(f"No such version: {after}")
        return diff_ontologies(before_payload, after_payload)

    def diff_latest(self, key: str):
        """Compare the two most recent versions, or None when there is only one."""
        versions = self.list_versions(key)
        if len(versions) < 2:
            return None
        return self.diff_versions(key, versions[-2].name, versions[-1].name)

    def load_current(self, key: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(key) / CURRENT_FILE
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def set_current(self, key: str, version_file: str) -> None:
        """Roll back (or forward) to a stored version."""
        source = self.path_for(key) / VERSIONS_DIR / version_file
        if not source.exists():
            raise ValueError(f"No such version: {version_file}")
        (self.path_for(key) / CURRENT_FILE).write_text(source.read_text())
        logger.info(f"Set current to {version_file}")

    # -- assertions and checklist ----------------------------------------

    def load_assertions(self, key: str) -> List[Dict[str, Any]]:
        """User answers. Kept outside the ontology so re-extraction can't lose them."""
        path = self.path_for(key) / ASSERTIONS_FILE
        if not path.exists():
            return []
        return json.loads(path.read_text())

    def save_assertions(self, key: str, assertions: List[Dict[str, Any]]) -> None:
        directory = self.path_for(key)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ASSERTIONS_FILE).write_text(json.dumps(assertions, indent=2))

    def load_checklist(self, key: str) -> Dict[str, Any]:
        path = self.path_for(key) / CHECKLIST_FILE
        if not path.exists():
            return {}
        return json.loads(path.read_text())

    def save_checklist(self, key: str, checklist: Dict[str, Any]) -> None:
        directory = self.path_for(key)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / CHECKLIST_FILE).write_text(json.dumps(checklist, indent=2))

    # Typed accessors. The raw dict forms above stay for anything that just wants
    # to read the file; these are what the gap-resolution flow uses.

    def load_assertion_objects(self, key: str) -> List["Assertion"]:
        from phases.gap_resolution import Assertion

        return [Assertion.from_dict(d) for d in self.load_assertions(key)]

    def save_assertion_objects(self, key: str, assertions: List["Assertion"]) -> None:
        self.save_assertions(key, [a.to_dict() for a in assertions])

    def load_checklist_items(self, key: str) -> List["ChecklistItem"]:
        from phases.gap_resolution import checklist_from_dict

        return checklist_from_dict(self.load_checklist(key))

    def save_checklist_items(self, key: str, items: List["ChecklistItem"]) -> None:
        from phases.gap_resolution import checklist_to_dict

        self.save_checklist(key, checklist_to_dict(items))

    def build_semantic_index(self, key: str):
        """Embed this ontology's chunks and cache them. Slow; always opt-in.

        Roughly 24 chunks/second, so instant on a contract and about ten minutes
        on a 14,000-chunk spec — which is why nothing calls this automatically.
        """
        from phases.semantic_index import build

        stored = self.load_index(key)
        if not stored or not stored.get("chunks"):
            return None

        index = build(stored["chunks"])
        if index is not None:
            index.save(self.path_for(key) / EMBEDDINGS_FILE)
        return index

    def load_censuses(self, key: str) -> Dict[str, Any]:
        """Saved census results for this ontology, keyed by concept name."""
        path = self.path_for(key) / CENSUS_FILE
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.warning(f"Census file for {key} is unreadable ({e}); treating as absent")
            return {}

    def save_census(self, key: str, concept: str, result: Dict[str, Any]) -> None:
        """Store one concept's census, leaving the others untouched."""
        directory = self.path_for(key)
        directory.mkdir(parents=True, exist_ok=True)
        stored = self.load_censuses(key)
        stored[concept] = result
        (directory / CENSUS_FILE).write_text(json.dumps(stored, indent=2))

    def clear_censuses(self, key: str) -> List[str]:
        """Drop saved censuses — they describe a chunk stream that has changed.

        Called when the index is rebuilt. A census records chunk numbers, so one
        taken against a different chunk stream points at the wrong passages while
        looking perfectly valid.
        """
        dropped = sorted(self.load_censuses(key))
        path = self.path_for(key) / CENSUS_FILE
        if path.exists():
            path.unlink()
            logger.info(
                f"Cleared {len(dropped)} saved census(es) for {key} — the chunk stream "
                f"changed and their chunk numbers no longer point anywhere real: "
                f"{', '.join(dropped)}"
            )
        return dropped

    def load_semantic_index(self, key: str):
        """Cached embeddings, or None. Refused if they no longer match the chunks."""
        from phases.semantic_index import SemanticIndex

        stored = self.load_index(key)
        if not stored or not stored.get("chunks"):
            return None
        return SemanticIndex.load(
            self.path_for(key) / EMBEDDINGS_FILE, expected_chunks=len(stored["chunks"])
        )

    def has_semantic_index(self, key: str) -> bool:
        return (self.path_for(key) / EMBEDDINGS_FILE).exists()

    def searcher_for(self, key: str):
        """A searcher over this ontology's persisted chunks, or None if it has no index.

        This is what makes "retrieve before asking" possible after a run has
        finished — by then the uploaded documents are gone.
        """
        from phases.phase1_rag_indexer import RAGIndexSearcher, rebuild_index

        stored = self.load_index(key)
        if not stored or not stored.get("chunks"):
            return None

        searcher = RAGIndexSearcher(rebuild_index(stored["chunks"], stored.get("metadata", [])))
        # Attached when it exists and still matches; a stale one is refused by
        # the loader rather than silently citing the wrong passages.
        semantic = self.load_semantic_index(key)
        if semantic is not None:
            searcher.attach_semantic(semantic)
        return searcher

    # -- migration --------------------------------------------------------

    def find_legacy(self) -> List[Dict[str, Any]]:
        """Run-keyed directories from before ontologies were entities.

        `list()` skips anything without a `meta.json`, so these are invisible
        rather than broken. That is the safe default and also means a directory
        of real work can sit there unnoticed, so they can be found and adopted.

        Layout: `ontologies/<workflow_id>/ontology_<slug>_<workflow_id>.json`
        """
        found = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or (directory / META_FILE).exists():
                continue

            candidates = sorted(directory.glob("ontology_*.json"))
            if not candidates:
                continue

            payload = None
            try:
                payload = json.loads(candidates[0].read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Legacy directory {directory.name} is unreadable: {e}")

            found.append(
                {
                    "directory": directory.name,
                    "file": candidates[0].name,
                    # Prefer the name recorded inside; the filename slug has
                    # already lost the original capitalisation and spacing.
                    "name": (payload or {}).get("name")
                    or (payload or {}).get("api_name")
                    or directory.name,
                    "readable": payload is not None,
                }
            )
        return found

    def migrate_legacy(self, dry_run: bool = True) -> List[Dict[str, Any]]:
        """Adopt run-keyed directories as the first version of a named ontology.

        Defaults to a dry run: this reads directories written by an older layout,
        and reporting what it *would* do before doing it is cheaper than
        discovering the name inference was wrong afterwards. Nothing is deleted
        either way — the original directory is left in place.
        """
        report = []
        for legacy in self.find_legacy():
            entry = dict(legacy, migrated=False, key=None, reason="")

            if not legacy["readable"]:
                entry["reason"] = "could not be read"
                report.append(entry)
                continue

            if dry_run:
                entry["reason"] = "dry run"
                report.append(entry)
                continue

            try:
                meta = self.get_or_create(legacy["name"])
                payload = json.loads((self.root / legacy["directory"] / legacy["file"]).read_text())
                self.save_version(meta.key, payload, legacy["directory"], make_current=True)
                entry.update(migrated=True, key=meta.key, reason="adopted as a version")
            except Exception as e:
                entry["reason"] = f"failed: {e}"

            report.append(entry)

        logger.info(
            f"Legacy migration: {sum(1 for r in report if r['migrated'])} adopted of "
            f"{len(report)} found{' (dry run)' if dry_run else ''}"
        )
        return report

    # -- Phase 2 requirements ---------------------------------------------

    def save_requirements(self, key: str, requirements: Dict[str, Any]) -> Path:
        """Store the latest Phase 2 output for this ontology.

        Kept beside the ontology rather than in a run-keyed directory, for the
        same reason versions are: requirements are *about* an ontology, and a
        person looking at one wants the requirements that came from it.
        """
        directory = self.path_for(key)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / REQUIREMENTS_FILE
        path.write_text(json.dumps(requirements, indent=2))
        logger.info(
            f"Saved {requirements.get('total_requirements', 0)} requirements for {key}"
        )
        return path

    def load_requirements(self, key: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(key) / REQUIREMENTS_FILE
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.warning(f"Requirements for {key} are unreadable ({e}); treating as absent")
            return None

    def has_requirements(self, key: str) -> bool:
        return (self.path_for(key) / REQUIREMENTS_FILE).exists()

    # -- phase 3 ----------------------------------------------------------

    def save_tests(self, key: str, suite: Dict[str, Any]) -> Path:
        """Store the latest Phase 3 output for this ontology.

        The generated code goes in here as text and is never executed from
        here — nothing in this class imports, execs or subprocesses it. It is
        stored so a person can read it in the browser without going to find the
        file the run wrote.
        """
        directory = self.path_for(key)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / TESTS_FILE
        path.write_text(json.dumps(suite, indent=2))
        logger.info(f"Saved {suite.get('tests_generated', 0)} generated test(s) for {key}")
        return path

    def load_tests(self, key: str) -> Optional[Dict[str, Any]]:
        path = self.path_for(key) / TESTS_FILE
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as e:
            logger.warning(f"Tests for {key} are unreadable ({e}); treating as absent")
            return None

    def has_tests(self, key: str) -> bool:
        return (self.path_for(key) / TESTS_FILE).exists()

    # -- retrieval index --------------------------------------------------

    def save_index(self, key: str, chunks: List[str], metadata: List[Dict[str, Any]]) -> Path:
        """Persist the chunk stream so retrieval is possible after the run ends.

        Gap resolution has to retrieve for one specific missing item *after* the
        extraction, to tell an extraction gap from a document gap. Without the
        chunks on disk that check would need the original uploads, which the web
        app deletes from its temp directory as soon as a run finishes.

        Only the chunks and their metadata are stored — the TF-IDF model is
        refitted on load. It is local, deterministic and fast, so persisting the
        fitted vectoriser would add a pickle-compatibility problem for no gain.

        This roughly doubles the disk an ontology occupies, since the chunk
        stream is the document text. That is the price of being able to ask
        "does the document say this?" without the document.
        """
        directory = self.path_for(key)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / INDEX_FILE
        path.write_text(json.dumps({"chunks": chunks, "metadata": metadata}))
        logger.info(f"Saved retrieval index for {key}: {len(chunks)} chunks")
        # A saved census records chunk numbers against the stream it read. A new
        # stream renumbers everything, so keeping the old census would point
        # targeted extraction at the wrong passages while looking valid — the
        # same reasoning that discards stale embeddings.
        # Returned to the caller rather than only logged: a census is the most
        # expensive thing this pipeline buys, and losing one silently is how a
        # run reports "completeness not established" with no hint that it was
        # established an hour ago.
        self.dropped_censuses = self.clear_censuses(key)
        return path

    def load_index(self, key: str) -> Optional[Dict[str, Any]]:
        """The persisted chunk stream, or None when this ontology has no index yet."""
        path = self.path_for(key) / INDEX_FILE
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as e:
            # A truncated index must not take the whole ontology down with it.
            logger.warning(f"Retrieval index for {key} is unreadable ({e}); treating as absent")
            return None

    def has_index(self, key: str) -> bool:
        return (self.path_for(key) / INDEX_FILE).exists()

    # -- destructive operations ------------------------------------------

    def describe_cost_of_reset(self, key: str) -> Dict[str, int]:
        """What a reset would destroy, so the confirmation can name it.

        Destructive actions must state the human work at stake rather than
        asking a generic "are you sure?".
        """
        checklist = self.load_checklist(key)
        # Two shapes exist: `items` from the gap-resolution checklist, `issues`
        # from the Phase 2 quality checklist. Reading only one would report zero
        # decisions at stake while deleting them — the confirmation has to name
        # the human work accurately or it is worse than no confirmation.
        entries = checklist.get("items") or checklist.get("issues") or []
        return {
            "versions": len(self.list_versions(key)),
            "assertions": len(self.load_assertions(key)),
            "checklist_decisions": len(entries),
        }

    def reset(self, key: str, keep_assertions: bool = True) -> None:
        """Clear versions and current. Assertions survive unless asked otherwise.

        Extraction is cheap to redo; user answers are not. The common case is a
        bad extraction whose answers — about what the document *doesn't* say —
        remain valid.
        """
        directory = self.path_for(key)
        if not directory.exists():
            raise ValueError(f"No such ontology: {key}")

        versions = directory / VERSIONS_DIR
        if versions.exists():
            for f in versions.glob("*.json"):
                f.unlink()
        current = directory / CURRENT_FILE
        if current.exists():
            current.unlink()

        # The index describes the documents of the run being cleared, so keeping
        # it would let a later gap check retrieve against material this ontology
        # no longer claims to be built from. It is rebuilt by the next run.
        index = directory / INDEX_FILE
        if index.exists():
            index.unlink()

        # Requirements describe the ontology version being cleared, so keeping
        # them would leave a test plan pointing at instances that no longer exist.
        requirements = directory / REQUIREMENTS_FILE
        if requirements.exists():
            requirements.unlink()

        # Embeddings are row-aligned to the chunk stream being cleared; keeping
        # them would point every vector at a different passage.
        embeddings = directory / EMBEDDINGS_FILE
        if embeddings.exists():
            embeddings.unlink()

        if not keep_assertions:
            for name in (ASSERTIONS_FILE, CHECKLIST_FILE):
                path = directory / name
                if path.exists():
                    path.unlink()

        self.clear_pinned_schema(key)
        logger.info(
            f"Reset ontology {key} (assertions {'kept' if keep_assertions else 'deleted'})"
        )

    def delete(self, key: str) -> None:
        """Remove the ontology entirely, including user answers."""
        import shutil

        directory = self.path_for(key)
        if not directory.exists():
            raise ValueError(f"No such ontology: {key}")
        shutil.rmtree(directory)
        logger.info(f"Deleted ontology {key}")
