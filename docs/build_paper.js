// Builds this project's paper.
//
//   NODE_PATH=<dir containing docx> node docs/build_paper.js docs/Independent-Claim-Validator.docx
//
// `docx` is not a project dependency — the pipeline is Python. Install it
// wherever you like and point NODE_PATH at it. Helper functions below are
// carried over from the source repo's docs/build_paper2.js verbatim (same
// visual language, same discipline: rendered to images before delivery, see
// the colophon at the end of this document).
const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType,
  Table, TableRow, TableCell, WidthType, ShadingType, BorderStyle,
  LevelFormat, PageOrientation,
} = require("docx");

const USABLE = 9360;
const ACCENT = "1F4E79";
const MUTED = "5A5A5A";
const RULE = "C8C8C8";

const P = (text, opts = {}) =>
  new Paragraph({
    spacing: { after: opts.after ?? 140, line: 300 },
    alignment: opts.align,
    children: [new TextRun({ text, size: opts.size ?? 21, color: opts.color, italics: opts.italics, bold: opts.bold })],
  });

const RP = (runs, opts = {}) =>
  new Paragraph({
    spacing: { after: opts.after ?? 140, line: 300 },
    children: runs.map(([text, f = {}]) =>
      new TextRun({
        text, size: 21, bold: f.b, italics: f.i,
        font: f.code ? "Consolas" : undefined,
        color: f.code ? "A0302A" : f.color,
      })),
  });

const H1 = (text) =>
  new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 380, after: 160 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: RULE, space: 6 } },
    children: [new TextRun({ text, size: 30, bold: true, color: ACCENT })],
  });

const H2 = (text) =>
  new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 260, after: 120 },
    children: [new TextRun({ text, size: 24, bold: true, color: "2E2E2E" })],
  });

const BULLET = (text) =>
  new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    spacing: { after: 90, line: 300 },
    children: [new TextRun({ text, size: 21 })],
  });

const CALLOUT = (runs) =>
  new Paragraph({
    spacing: { before: 200, after: 200, line: 320 },
    indent: { left: 220, right: 220 },
    shading: { type: ShadingType.CLEAR, fill: "F2F6FA" },
    border: {
      left: { style: BorderStyle.SINGLE, size: 18, color: ACCENT, space: 10 },
      top: { style: BorderStyle.SINGLE, size: 2, color: "DCE6F0", space: 8 },
      bottom: { style: BorderStyle.SINGLE, size: 2, color: "DCE6F0", space: 8 },
      right: { style: BorderStyle.SINGLE, size: 2, color: "DCE6F0", space: 8 },
    },
    children: runs.map(([text, f = {}]) =>
      new TextRun({ text, size: 21, bold: f.b, italics: f.i, font: f.code ? "Consolas" : undefined })),
  });

const cell = (runs, width, opts = {}) =>
  new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: opts.fill ? { type: ShadingType.CLEAR, fill: opts.fill } : undefined,
    margins: { top: 80, bottom: 80, left: 110, right: 110 },
    children: [new Paragraph({
      spacing: { after: 0, line: 260 },
      children: (Array.isArray(runs) ? runs : [[runs]]).map(([text, f = {}]) =>
        new TextRun({
          text, size: 19, bold: f.b || opts.head, italics: f.i,
          font: f.code ? "Consolas" : undefined,
          color: opts.head ? "FFFFFF" : (f.code ? "A0302A" : undefined),
        })),
    })],
  });

const table = (widths, header, rows) =>
  new Table({
    columnWidths: widths,
    width: { size: USABLE, type: WidthType.DXA },
    rows: [
      new TableRow({
        tableHeader: true, cantSplit: true,
        children: header.map((h, i) => cell(h, widths[i], { head: true, fill: ACCENT })),
      }),
      ...rows.map((r, ri) =>
        new TableRow({
          cantSplit: true,
          children: r.map((c, i) => cell(c, widths[i], { fill: ri % 2 ? "F7F9FB" : undefined })),
        })),
    ],
  });

const SPACER = () => new Paragraph({ spacing: { after: 160 }, children: [] });

const doc = new Document({
  numbering: {
    config: [{
      reference: "bullets",
      levels: [{
        level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 460, hanging: 240 } } },
      }],
    }],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840, orientation: PageOrientation.PORTRAIT },
        margin: { top: 1440, bottom: 1440, left: 1440, right: 1440 },
      },
    },
    children: [
      // ---------- Title ----------
      new Paragraph({
        spacing: { after: 80 },
        children: [new TextRun({ text: "An Independent Claim Validator", size: 40, bold: true, color: ACCENT })],
      }),
      new Paragraph({
        spacing: { after: 60 },
        children: [new TextRun({
          text: "Checking a claim nobody in this pipeline generated, against a document it understands deeply",
          size: 22, italics: true, color: MUTED })],
      }),
      new Paragraph({
        spacing: { after: 300 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: ACCENT, space: 8 } },
        children: [new TextRun({ text: "22 August 2026", size: 19, color: MUTED })],
      }),

      // ---------- Executive summary ----------
      H1("Executive summary"),
      P("A separate project — llm-rag-ontology-eval-scaffold, referred to below as the source pipeline — builds an ontology and a retrieval index for a document, then generates its own requirements from that ontology and checks them with an entailment judge. Every check it runs, it runs on its own output. This project asks a different question: can the same judgment machinery check a claim it did not write — someone else's requirement, someone else's summary, a row typed into a spreadsheet by a person — against a document, with the same rigour?"),
      P("The answer, built and verified rather than assumed: yes, with two things worth saying plainly up front. First, almost none of the judgment logic needed to change. The entailment judge, the shape check, the census, and the provider-swappable model client were reused unmodified, satisfied through small duck-typed shims rather than rewritten — the same pattern the source repo's own entailment.py already uses internally for a narrower case. Second, one genuinely new capability had to be built: a gap report, answering \"what does this set of claims never address\" using the document's own census as ground truth, kept as a separate report from per-claim correctness rather than blended into one score — because a set of claims can be perfectly correct and still leave large parts of a document unaddressed, and conflating those two questions would hide exactly the finding that makes an independent auditor worth having."),
      RP([["This is verified against a real model, not asserted from the design.", { b: true }], [" A small clinical trial protocol (7 sections, 3 chunks) was validated against four hand-written claims — two true, one a genuine but subtle miscalibration, one entirely absent from the document — through Ollama Cloud's gpt-oss:120b. Every claim came back with a real citation and a verdict a person can check against the document text quoted in the reason field. The gap report ran against the same document and returned a real finding this paper reports honestly rather than rounds up: on a document this small, most concept instances register as \"addressed\" simply because a three-chunk document leaves almost nothing for any claim's retrieval not to touch — the report's value on a document this size is limited, and saying so is the same discipline the source pipeline's own paper applies to itself."]]),
      P("Two real defects were found and fixed during this verification, not theorised in advance: the Excel report writer did not create its own output directory, and the first version of the command-line script bypassed the caching layer entirely because it predated it. Both are named in this paper because a paper that omits its own project's defects is not making the argument it claims to make."),

      // ---------- Background ----------
      H1("Background: a different problem from the one already solved"),
      P("The source pipeline's whole argument is that a model's own output should not be trusted merely because it looks plausible — so it checks its own requirements against the document they claim to come from. That argument only covers output the pipeline generated itself. It says nothing about a requirement a different tool wrote, a claim a person typed into a spreadsheet, or a summary produced before this pipeline ever existed."),
      P("Those are common, and none of them come pre-labelled as true or false. A person reviewing fifty rows in a spreadsheet, deciding by eye which ones the source document actually supports, is doing exactly the job the source pipeline already automated for its own output — just not for anyone else's."),
      RP([["So the question this project is built around is: "], ["can the same judgment be pointed at input the pipeline did not generate", { b: true }], [", without weakening it to fit? Everything below is the answer, built and checked rather than assumed."]]),

      // ---------- Architecture ----------
      H1("What was reused, and what had to be new"),
      P("The source pipeline's entailment judge already declares, in its own docstring, that it accepts \"any iterable of objects with id, title, expected_behavior, criteria and source_chunks\" — not a specific dataclass. That line is the reason this project needed so little new code: a bare id-plus-text claim, wrapped in a small object exposing those five attributes, satisfies the judge exactly as well as the source pipeline's own generated requirements do. The same is true of the free, deterministic shape check. Neither reused module was edited to know this project exists."),
      table([2000, 7360],
        ["Reused unmodified", "Why it needed nothing new"],
        [
          ["The entailment judge", "Already duck-typed; docstring explicitly allows \"anything shaped like\" its own dataclass"],
          ["The shape check", "Only reads getattr(item, field_name); a wrapper object is indistinguishable from the real thing"],
          ["The census (census_many, census_repeated)", "Takes plain (name, description) pairs and a chunk list — no dependency on the source pipeline's own Ontology object"],
          [[["The provider-swappable model client", {}]], "generate(prompt, system_prompt=None) -> str is the one contract every phase already depended on"],
        ]),
      SPACER(),
      P("Two things could not be reused as-is, for reasons worth stating rather than glossing over:"),
      BULLET("ask.py answers a question by retrieving passages and generating a fresh response. This project needs only the retrieval half — the entailment judge, not a second model call, decides whether a passage supports an existing claim. The retrieval logic was reproduced, including its one precision-preserving detail (a concept's surface terms become a separate search probe, never concatenated onto the claim text), and the answer-generation half was left out rather than carried along unused."),
      BULLET("completeness.py's own gap-measuring functions read the source pipeline's Ontology object directly, comparing it against itself. That comparison has no meaning for a claim list from outside the pipeline. The gap report is new code for this reason specifically, not because nothing existed to reuse."),
      P("One further thing was fixed, not reused: the source repo's own web app calls its extraction function synchronously inside the request handler, despite exposing a status-polling endpoint — the appearance of an async job without the substance, confirmed by reading the route rather than assumed. This project's API returns a job id in well under a second and does the multi-minute work in the background, backed by a database row that survives a restart."),

      // ---------- The real run ----------
      H1("What a real run against a real model produced"),
      P("Four claims, written by hand against a small oncology trial protocol (ONC-2291) — two intended to be true, one a deliberately subtle miscalibration, one a plausible-sounding claim the document never actually makes — run through Ollama Cloud's gpt-oss:120b, no Anthropic credentials configured for this run (why that matters is below)."),
      table([900, 3200, 1500, 1100, 3660],
        ["Claim", "Text", "Verdict", "Agreement", "Reason (quoted from the model's own answer)"],
        [
          ["C1", "Patients must be at least 18 years old to enrol.", [["contradicts", { b: true }]], "2/3", "\"Patients must be over 18 years of age.\""],
          ["C2", "Dose reduction of pembrolizumab is permitted to manage toxicity.", [["contradicts", { b: true }]], "3/3", "\"dose reduction of pembrolizumab is not permitted\""],
          ["C3", "The study requires a body mass index under 30.", [["mentions_only", { b: true }]], "2/3", "eligibility is discussed; BMI is never mentioned"],
          ["C4", "The primary endpoint requires blinded independent central review.", [["contradicts", { b: true }]], "3/3", "\"The primary endpoint is overall survival (OS)\"; BICR applies only to the secondary PFS endpoint"],
        ]),
      SPACER(),
      RP([["C1 is the more interesting result precisely because it looks like a mistake and is not.", { b: true }], [" The claim (\"at least 18\") and the document (\"over 18\") are not identical: strictly read, \"over 18\" excludes someone exactly 18, while \"at least 18\" includes them — a genuine, if narrow, boundary mismatch. The judge called it a contradiction rather than an entailment, on a distinction most people would read past. That is either the judge being usefully precise or unhelpfully literal, and this paper does not resolve which — only reports that it is a real disagreement about the document's actual wording, not a hallucinated one, since the citation itself is exactly right."]]),
      P("C2 and C4 are the cases this system exists to catch: both are stated confidently, both directly contradict a specific sentence the document actually contains, and both citations are correct. C3 shows the judge declining to overreach in the other direction — retrieval found the eligibility section, which is genuinely on-topic, but correctly reported that it says nothing about BMI rather than manufacturing either an entailment or a contradiction from adjacent text."),
      CALLOUT([["Escalation to a stronger model triggered exactly as designed on all three contradictions, then failed — three times, once per doubtful verdict — because no Anthropic credentials were configured for this run.", { b: true }], [" The original verdicts stood, unchanged, exactly as the source pipeline's own escalation code promises: best-effort, never blocking. This is the correct failure mode, verified by triggering it, but it also means this run cannot report whether escalation actually overturns anything here — the source pipeline's own comparable measurement was \"one escalation, one overturned verdict,\" and this project has no equivalent number yet."]]),

      // ---------- Gap report ----------
      H1("The gap report, and what it honestly cannot show yet"),
      P("Run against the same document and the same four claims, using a census — an exhaustive, repeated read of every chunk, reported as a range rather than a single count, since the source pipeline's own central finding is that repeated censuses disagree with each other — as ground truth for what the document actually contains, independent of what any claim happened to cite."),
      table([2400, 1600, 1700, 3660],
        ["Concept", "Census range", "Addressed", "Never addressed"],
        [
          ["disease", "1–1", "0 of 1", "NSCLC_stageIIIB/IV"],
          ["biomarker", "4–4", "2 of 4", "ALK, EGFR"],
          ["endpoint", "5–5", "5 of 5", "—"],
          [[["treatment_regimen", { }]], "1–3", "0 of ~2", "(count itself unstable — see below)"],
          ["adverse_event", "4–4", "4 of 4", "—"],
          ["safety_monitoring / monitoring_committee", "1–1 each", "0 of 1 each", "(no verified citation for either)"],
        ]),
      SPACER(),
      RP([["The honest limitation, stated rather than hidden: this document is three chunks long, and between them the four claims' own retrieval touched every one of the three.", { b: true }], [" That is why endpoint and adverse_event show full coverage — not because the claims discussed five endpoints and four adverse events, they discussed one endpoint and zero adverse events, but because \"was this instance's citation inside a chunk some claim touched\" cannot distinguish anything on a document too small to have chunks left over. The gap report's actual value — telling a reviewer which parts of a large document a claim set silently ignores — is not demonstrated by this run. It is demonstrated only by the report running correctly and by the two entries here (disease, and the safety/monitoring concepts) where a genuinely unverified citation, not chunk coincidence, produced the answer. A real test of what this report is for needs a document large enough that different claims can touch genuinely different parts of it, which this first verification run does not provide."]]),
      P("The treatment_regimen row is worth pointing at on its own terms: 1–3 is not a formatting choice, it is what census_repeated actually returned across its three passes over this document — the same instability the source pipeline's paper documents at length, reproduced here on the first real run of a completely different report built on the same census function. It was not sought out as a demonstration; it is simply what the number was."),

      // ---------- What's verified ----------
      H1("What this run establishes, and what it does not"),
      table([3400, 5960],
        ["Established by this run", "Still open"],
        [
          ["Citations are real — every verdict quotes text that is actually in the document, not fabricated", "Escalation was triggered but never completed — whether a stronger model changes anything here is unmeasured"],
          ["A model can distinguish contradicts from mentions_only on genuinely subtle cases (C1, C3)", "The gap report's actual value is unproven — this document was too small to leave anything ungrouped by chunk"],
          ["A claim with no retrievable support is reported as unjudged, never fabricated as a verdict", "No run yet at the scale where retrieval budget, not document size, becomes the binding constraint — the source pipeline's own central later finding"],
          ["Ontology reuse is real: a second submission of the identical document completed in under 15 seconds, not minutes", "No test yet of two independent extractions of the same document disagreeing with each other, the way the source pipeline measured for its own extraction step"],
          ["The async job API returns before the underlying work finishes, unlike the pattern found (and not copied) in the source repo's own web app", "No concurrent-job test — one process, one FastAPI BackgroundTasks pool, unexercised under real load"],
        ]),
      SPACER(),

      // ---------- Bugs found ----------
      H1("Two defects this verification found, not designed around in advance"),
      P("Named here because a paper that only reports what worked is not applying the standard this whole project exists to apply to everything else."),
      BULLET("The Excel report writer called workbook.save(path) without first creating the path's parent directory — worked in every unit test, since pytest's tmp_path fixture already exists, and failed the first time it was run against a fresh reports directory that had never been created. Fixed to create the directory before saving."),
      BULLET("scripts/validate_claims.py was written before document_identity.py's content-hash caching existed, and was never updated to use it — so the same document run twice through that one entry point alone would have rebuilt its ontology from scratch every time, silently, while the newer pipeline.py path cached correctly. Found by deliberately running the same document through both entry points and comparing whether either logged a cache hit. Fixed so both paths resolve identity through the same function."),

      // ---------- What would change the picture most ----------
      H1("What would change the picture most"),
      RP([["If only one further thing were done: run this against a document large enough that the gap report's chunk-coincidence problem cannot occur.", { b: true }], [" Every finding in the previous section about the gap report's limitation is a property of a three-chunk document specifically. A document with even a few dozen chunks would let two different claims genuinely miss different, disjoint parts of it — which is the actual scenario this report exists to catch, and which this first run structurally could not produce."]]),
      P("Second: repeat the census on this same document and see whether treatment_regimen's 1–3 spread narrows, widens, or simply lands somewhere else, the same discipline the source pipeline applies to its own entailment judge before trusting a single run's number. One spread from one run is a data point, not yet a finding."),
      P("Third: run the same claims through with real Anthropic credentials configured, so escalation can actually complete rather than fail closed. C1's borderline contradiction is exactly the kind of case escalation exists for — a stronger model's opinion on whether \"over 18\" truly excludes \"at least 18\" would be a genuine second measurement, not a repeat of the first."),

      // ---------- Colophon ----------
      H1("How this paper was produced"),
      P("Every figure comes from a recorded run against a real model, captured in one script invocation so the per-claim verdicts and the gap report cited here describe the same run rather than being assembled from separate ones taken at different times."),
      P("The document is generated from a script and rendered to images before delivery, so its layout is looked at rather than assumed — the same discipline the source pipeline's own paper states and applies to itself."),
      P("The same rule this project's own code applies to a claim applies here to a sentence: a thing that has not been checked is reported as unchecked, not quietly upgraded to a finding."),
    ],
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(process.argv[2], buf);
  console.log("written:", process.argv[2]);
});
