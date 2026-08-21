"""Count instances by reading every chunk, so completeness can be measured.

Extraction samples: retrieval picks the chunks that look relevant and the rest
of the document is never read. Nothing stops a deliberate pass that reads all of
it and counts what is there, and that is what this is.

### What it gives you, and what it does not

Run it and the sampling ratio stops being a guess:

    sampled extraction found 4 obligations
    census read every chunk and found 37
    -> extraction captured roughly 11% of this concept

**"Roughly" is doing real work in that sentence.** This module was written under
the heading "so completeness has ground truth", and that was wrong. Two
identical census runs on 182 chunks of prose returned **294 and 342** instances,
one concept coming back 50 and 78. A census is a measurement with an error bar,
not an establishment of fact — so `census_repeated` runs it several times and
reports a **range**, and nothing here should ever be quoted as a single count.

It is still the only *direct* measure of completeness the pipeline has. Chunk
reach is a ceiling — at 100% reach, true capture has ranged from 0% to 100%
across six documents — and everything else is a sample. Direct and imprecise
beats indirect and misleading, provided the imprecision is reported.

### Cost, and when it runs

`census` sweeps the document once per concept, so cost is chunks × concepts.
`census_many` reads each chunk once and asks about every concept in the same
pass, making it chunks × 1 — measured at 8× fewer calls and 6.3× fewer tokens,
losing no content. Phase 1 runs the repeated multi-concept form automatically
below a chunk limit (`phase1.census_max_chunks`), and above it reports what a
census would cost rather than spending it unasked.

**A census with failed batches is not a census.** It read part of the document,
so its counts are a floor with no ceiling; `complete` says so, and callers must
consult it. Found live: 17 of 19 batches failed and the run reported
"16–162 instance(s) exist" as though that were a measurement.

### What it is not

Not a better extractor. It answers "how many are there", not "what are they in
context" — the same model reading 10 chunks at a time has less context than one
reading a focused retrieval, and its output is a list of names, not a populated
ontology. It exists to measure the sampler, not to replace it.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Chunks per call. Larger batches cost fewer calls but give each chunk less
# attention; 10 keeps the prompt well inside a comfortable window.
CENSUS_BATCH = 10

# The one knob a census has, and the one worth recording: a census is the
# expensive path, and "how many calls did that cost" is unanswerable afterwards
# without knowing the batch size that was in force.
SETTINGS_PROCESS = "census"
DEFAULT_SETTINGS = {"census_batch": CENSUS_BATCH}


@dataclass
class CensusResult:
    """Every instance of one concept found by reading the whole document."""

    concept: str
    names: List[str] = field(default_factory=list)
    # slug -> the chunk the instance was found in. Recorded so a census is a
    # *work list* and not only a count: targeted extraction (todo/14) needs to
    # know where a missed instance lives before it can go and extract it
    # properly. A name whose citation could not be verified is absent here
    # rather than guessed at — the same rule extraction uses.
    chunk_of: Dict[str, int] = field(default_factory=dict)
    chunks_read: int = 0
    chunks_total: int = 0
    calls_made: int = 0
    failed_batches: int = 0

    @property
    def count(self) -> int:
        return len(self.names)

    @property
    def complete(self) -> bool:
        """Whether every chunk was actually read.

        A census with failed batches is not ground truth, and must not be used
        as a denominator as though it were.
        """
        return self.failed_batches == 0 and self.chunks_read >= self.chunks_total

    def capture_ratio(self, sampled_count: int) -> Optional[float]:
        """What share of the real total the sampled extraction found."""
        if not self.complete or not self.count:
            return None
        return min(sampled_count / self.count, 1.0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "count": self.count,
            "names": self.names,
            "chunk_of": self.chunk_of,
            "located": len(self.chunk_of),
            "chunks_read": self.chunks_read,
            "chunks_total": self.chunks_total,
            "calls_made": self.calls_made,
            "failed_batches": self.failed_batches,
            "complete": self.complete,
            "kind": "census",
        }


def estimate_calls(chunks_total: int, batch_size: int = CENSUS_BATCH) -> int:
    """How many model calls a census would take. Always shown before running."""
    if chunks_total <= 0:
        return 0
    return (chunks_total + batch_size - 1) // batch_size


def _parse_sightings(response: str) -> List[Tuple[str, Optional[int]]]:
    """`(name, chunk)` pairs from one batch.

    Tolerates a bare list of strings, which is what the census asked for before
    it recorded locations — an older stored response must still parse, it simply
    yields no chunk.
    """
    if not response:
        return []
    fenced = re.search(r"```(?:json)?\s*(.+?)```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)
    match = re.search(r"\[.*\]", response, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    sightings: List[Tuple[str, Optional[int]]] = []
    for item in data:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            raw = item.get("chunk")
            try:
                chunk = int(str(raw).strip()) if raw is not None else None
            except (TypeError, ValueError):
                chunk = None
        else:
            name, chunk = str(item).strip(), None
        if name:
            sightings.append((name, chunk))
    return sightings


def census(
    concept_name: str,
    concept_description: str,
    chunks: List[str],
    llm_client,
    batch_size: Optional[int] = None,
    on_batch: Optional[Callable[[int, int], None]] = None,
    settings=None,
    db_session=None,
) -> CensusResult:
    """Read every chunk and count distinct instances of one concept.

    A failed batch is recorded rather than retried or hidden: a census with gaps
    is not ground truth, and `complete` says so, so it cannot quietly become a
    denominator.
    """
    from phases.phase1_models import slugify
    from phases.settings_registry import settings_for

    if batch_size is None:
        resolved = settings_for(SETTINGS_PROCESS, DEFAULT_SETTINGS, settings, db_session)
        batch_size = resolved.get("census_batch", CENSUS_BATCH)

    result = CensusResult(concept=concept_name, chunks_total=len(chunks))
    if not chunks:
        return result

    seen = set()
    batches = estimate_calls(len(chunks), batch_size)

    for n in range(batches):
        window = chunks[n * batch_size : (n + 1) * batch_size]
        if not window:
            continue

        numbered = "\n\n".join(
            f"[chunk {n * batch_size + i}]\n{text}" for i, text in enumerate(window)
        )

        prompt = f"""List every distinct instance of one concept that appears in the text below.

CONCEPT: {concept_name}
MEANING: {concept_description or concept_name}

TEXT:
{numbered}

Return a JSON array. Each element is an object with:
- name: a short identifier for this instance
- chunk: the number from the [chunk N] marker above the passage that states it

One element per distinct instance actually stated in this text. The same thing
mentioned twice is one instance. If there are none, return [].

Return ONLY the JSON array."""

        try:
            sightings = _parse_sightings(llm_client.generate(prompt))
        except Exception as e:
            result.failed_batches += 1
            logger.error(f"[Census] Batch {n + 1}/{batches} failed: {e}")
            continue
        finally:
            result.calls_made += 1

        # A citation is only accepted if it names a chunk that was actually in
        # this batch. Same rule as extraction: a location the model invented is
        # worse than no location, because targeted extraction would then read
        # the wrong passage and conclude the instance is not there.
        offered = set(range(n * batch_size, n * batch_size + len(window)))

        for name, chunk in sightings:
            key = slugify(name)
            if not key:
                continue

            if key not in seen:
                seen.add(key)
                result.names.append(name)

            # Locating is separate from counting. A name first sighted in a
            # batch that mis-cited it would otherwise be locked in without a
            # location, and a later batch that *can* place it correctly would be
            # skipped — leaving targeted extraction with nowhere to look for an
            # instance whose home chunk is perfectly well known.
            if key in result.chunk_of:
                continue
            if chunk in offered:
                result.chunk_of[key] = chunk
            elif chunk is not None:
                logger.debug(
                    f"[Census] {name!r} cited chunk {chunk}, not in this batch — "
                    f"dropping the location"
                )

        result.chunks_read += len(window)
        if on_batch:
            on_batch(n + 1, batches)

    logger.info(
        f"[Census] {concept_name}: {result.count} distinct instance(s) across "
        f"{result.chunks_read}/{result.chunks_total} chunks in {result.calls_made} call(s)"
        + (f", {result.failed_batches} batch(es) failed" if result.failed_batches else "")
    )
    return result


def compare_to_sample(result: CensusResult, ontology) -> Dict[str, Any]:
    """What the sampled extraction actually captured, against the census.

    This is the number `completeness` could only estimate for prose.
    """
    concept = ontology.concept(result.concept)
    sampled = len(concept.instances) if concept else 0
    ratio = result.capture_ratio(sampled)

    missed = []
    if result.complete and concept:
        from phases.phase1_models import slugify

        found = {slugify(i.name) for i in concept.instances}
        missed = [n for n in result.names if slugify(n) not in found]

    return {
        "concept": result.concept,
        "sampled": sampled,
        "census": result.count,
        "capture_ratio": round(ratio, 4) if ratio is not None else None,
        "census_complete": result.complete,
        "missed_examples": missed[:10],
        "note": (
            "Ground truth from reading every chunk."
            if result.complete
            else "Census incomplete — not usable as a denominator."
        ),
    }


# ---------------------------------------------------------------------------
# Reading the document once for every concept at the same time
# ---------------------------------------------------------------------------
#
# `census` above reads the whole document for *one* concept, so censusing an
# eight-concept ontology reads the document eight times. Cost is chunks ×
# concepts, and on a 14,356-chunk specification that is roughly 31M input tokens
# — the reason a census is opt-in and rarely run.
#
# This reads each chunk once and asks for every concept in the same pass, which
# makes cost chunks × 1. The saving is real and large. Whether it is *safe* is
# an empirical question and not an obvious one: a census exists to be ground
# truth, and a model asked about eight kinds of thing at once may attend less to
# each than one asked about a single kind. A census that quietly misses
# instances is worse than no census, because it produces a confident wrong
# denominator that everything downstream then trusts.
#
# So this ships beside the single-concept census rather than replacing it, and
# the choice between them is a measurement — see todo/14.


def _parse_multi_sightings(response: str) -> List[Tuple[str, str, Optional[int]]]:
    """`(concept, name, chunk)` triples from one batch."""
    if not response:
        return []
    fenced = re.search(r"```(?:json)?\s*(.+?)```", response, re.DOTALL)
    if fenced:
        response = fenced.group(1)
    match = re.search(r"\[.*\]", response, re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []

    out: List[Tuple[str, str, Optional[int]]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        concept = str(item.get("concept", "")).strip()
        name = str(item.get("name", "")).strip()
        raw = item.get("chunk")
        try:
            chunk = int(str(raw).strip()) if raw is not None else None
        except (TypeError, ValueError):
            chunk = None
        if concept and name:
            out.append((concept, name, chunk))
    return out


def census_many(
    concepts: List[Tuple[str, str]],
    chunks: List[str],
    llm_client,
    batch_size: Optional[int] = None,
    on_batch: Optional[Callable[[int, int], None]] = None,
    settings=None,
    db_session=None,
) -> Dict[str, CensusResult]:
    """Census every concept in one pass over the document.

    `concepts` is `[(name, description), ...]`. Returns the same `CensusResult`
    per concept that `census` returns, so every caller downstream — capture
    ratios, reconciliation, targeted extraction — works unchanged.

    Costs one read of the document regardless of how many concepts are asked
    for, against one read *per concept* for `census`.
    """
    from phases.phase1_models import slugify
    from phases.settings_registry import settings_for

    if batch_size is None:
        resolved = settings_for(SETTINGS_PROCESS, DEFAULT_SETTINGS, settings, db_session)
        batch_size = resolved.get("census_batch", CENSUS_BATCH)

    results = {
        name: CensusResult(concept=name, chunks_total=len(chunks))
        for name, _ in concepts
    }
    if not chunks or not concepts:
        return results

    # Matched case-insensitively: the model echoes the concept name back and
    # capitalisation drifts. An unrecognised concept is dropped rather than
    # invented as a new one.
    by_key = {name.strip().casefold(): name for name, _ in concepts}
    seen: Dict[str, set] = {name: set() for name, _ in concepts}

    listing = "\n".join(
        f"- {name}: {description or name}" for name, description in concepts
    )
    batches = estimate_calls(len(chunks), batch_size)

    for n in range(batches):
        window = chunks[n * batch_size : (n + 1) * batch_size]
        if not window:
            continue

        numbered = "\n\n".join(
            f"[chunk {n * batch_size + i}]\n{text}" for i, text in enumerate(window)
        )

        prompt = f"""List every instance of the concepts below that this text states.

CONCEPTS:
{listing}

TEXT:
{numbered}

Return a JSON array. Each element is an object with:
- concept: exactly one of the concept names listed above
- name: a short identifier for this instance
- chunk: the number from the [chunk N] marker above the passage that states it

One element per distinct instance actually stated in this text. The same thing
mentioned twice is one instance. Work through the concepts one at a time so none
is overlooked. If a concept has no instances here, simply omit it.

Return ONLY the JSON array."""

        try:
            sightings = _parse_multi_sightings(llm_client.generate(prompt))
        except Exception as e:
            # A failed batch fails it for every concept, since one call covered
            # them all. `complete` then reports False for each, which is right:
            # none of them read the whole document.
            for result in results.values():
                result.failed_batches += 1
            logger.error(f"[Census] Batch {n + 1}/{batches} failed: {e}")
            continue
        finally:
            for result in results.values():
                result.calls_made += 1

        offered = set(range(n * batch_size, n * batch_size + len(window)))

        for concept_raw, name, chunk in sightings:
            concept = by_key.get(concept_raw.strip().casefold())
            if concept is None:
                logger.debug(f"[Census] Ignoring unlisted concept {concept_raw!r}")
                continue

            slug = slugify(name)
            if slug in seen[concept]:
                continue
            seen[concept].add(slug)
            results[concept].names.append(name)
            # Same rule as the single-concept census and as extraction: a
            # location the model invented is worse than no location.
            if chunk in offered:
                results[concept].chunk_of[slug] = chunk

        for result in results.values():
            result.chunks_read += len(window)

        if on_batch:
            on_batch(n + 1, batches)

    for name, result in results.items():
        logger.info(
            f"[Census] {name}: {result.count} instance(s) across {result.chunks_read} "
            f"chunk(s) in {result.calls_made} call(s)"
        )
    return results


# ---------------------------------------------------------------------------
# Repeating a census, because one run of one is not a denominator
# ---------------------------------------------------------------------------
#
# Measured on RFC 6749 (182 chunks of prose): two identical runs of the
# single-concept census returned **294 and 342** instances, one concept coming
# back 50 and 78. The one-pass census moved less but still moved. A census is a
# measurement with an error bar, not ground truth, and the error bar had never
# been reported.
#
# So this repeats and returns the spread. Nothing here ever produces a single
# count, because a single count is what invited the pipeline to treat a census
# as exact in the first place.

# Names must recur in at least this share of runs to be reported as found. Two
# of three, matching the entailment judge's consensus rule (todo/13) — one
# sighting is a lead, a repeated one is a finding.
CENSUS_CONSENSUS = 0.5


@dataclass
class CensusSpread:
    """One concept, censused several times. A range, never a number."""

    concept: str
    counts: List[int] = field(default_factory=list)
    # slug -> how many runs saw it. Slugged so "GET /orders" and "get /orders"
    # are one instance across runs, with the first spelling kept for display —
    # reconciliation matches on real names, and a slug reads as a different
    # vocabulary again.
    seen_in: Dict[str, int] = field(default_factory=dict)
    display: Dict[str, str] = field(default_factory=dict)
    runs: int = 0
    chunks_total: int = 0
    complete: bool = True

    @property
    def low(self) -> int:
        return min(self.counts) if self.counts else 0

    @property
    def high(self) -> int:
        return max(self.counts) if self.counts else 0

    @property
    def agreed(self) -> List[str]:
        """Names every run found. The part of the answer that is not in doubt."""
        return sorted(self.display.get(n, n) for n, c in self.seen_in.items()
                      if c == self.runs)

    @property
    def probable(self) -> List[str]:
        """Names a majority of runs found, including the unanimous ones."""
        need = max(1, round(self.runs * CENSUS_CONSENSUS))
        return sorted(self.display.get(n, n) for n, c in self.seen_in.items()
                      if c >= need)

    @property
    def once_only(self) -> List[str]:
        """Seen by exactly one run. Leads, not findings."""
        return sorted(self.display.get(n, n) for n, c in self.seen_in.items()
                      if c == 1)

    @property
    def spread(self) -> str:
        """How this concept's count should be written down, in words."""
        if not self.counts:
            return "not measured"
        if self.low == self.high:
            return f"{self.low} in every one of {self.runs} run(s)"
        return f"between {self.low} and {self.high} across {self.runs} runs"

    def capture_range(self, sampled_count: int) -> Optional[Tuple[float, float]]:
        """What share of the real total extraction found — as a range.

        `capture_ratio` on a single census returned one number and implied a
        precision the measurement does not have. Dividing by the smallest and
        largest counts seen gives the interval the evidence actually supports.
        """
        if not self.counts or not self.high:
            return None
        best = min(sampled_count / self.low, 1.0) if self.low else 1.0
        worst = min(sampled_count / self.high, 1.0)
        return (round(worst, 4), round(best, 4))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "concept": self.concept,
            "runs": self.runs,
            "counts": self.counts,
            "low": self.low,
            "high": self.high,
            "spread": self.spread,
            "agreed": len(self.agreed),
            "probable": len(self.probable),
            "once_only": len(self.once_only),
            "names_probable": self.probable,
            "chunks_total": self.chunks_total,
            "complete": self.complete,
            "kind": "census_spread",
        }


def census_repeated(
    concepts: List[Tuple[str, str]],
    chunks: List[str],
    llm_client,
    runs: int = 3,
    batch_size: Optional[int] = None,
    settings=None,
    db_session=None,
) -> Dict[str, CensusSpread]:
    """Census every concept `runs` times and report the spread.

    Uses `census_many`, so the whole document is read once per repeat rather
    than once per concept per repeat: three repeats of all concepts cost about
    half of one single-concept sweep, and unlike it they carry an error bar.
    """
    from phases.phase1_models import slugify

    runs = max(1, int(runs))
    spreads = {
        name: CensusSpread(concept=name, runs=runs, chunks_total=len(chunks))
        for name, _ in concepts
    }

    for _ in range(runs):
        results = census_many(concepts, chunks, llm_client, batch_size=batch_size,
                              settings=settings, db_session=db_session)
        for name, result in results.items():
            spread = spreads[name]
            spread.counts.append(result.count)
            if not result.complete:
                spread.complete = False
            for instance in result.names:
                slug = slugify(instance)
                spread.seen_in[slug] = spread.seen_in.get(slug, 0) + 1
                spread.display.setdefault(slug, instance)

    for name, spread in spreads.items():
        logger.info(f"[Census] {name}: {spread.spread}, {len(spread.agreed)} name(s) "
                    f"in every run")
    return spreads
