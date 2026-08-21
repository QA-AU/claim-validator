// Builds this project's paper.
//
//   NODE_PATH=<dir containing docx> node docs/build_paper.js docs/Independent-Claim-Validator.docx
//
// `docx` is not a project dependency — the pipeline is Python. Install it
// wherever you like and point NODE_PATH at it.
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
        children: [new TextRun({ text: "22 August 2026 (updated)", size: 19, color: MUTED })],
      }),

      // ---------- Executive summary ----------
      H1("Executive summary"),
      P("Given a reference document and a set of claims nobody in this system wrote — someone else's requirement, someone else's summary, a row typed into a spreadsheet by a person — this project independently judges whether each claim is actually supported, and separately reports what the claims never address at all. It generates nothing itself. It only checks."),
      P("The architecture is small on purpose. An ontology and a retrieval index are built once per document and reused automatically after that. An entailment judge checks each claim against a passage it either already cites or that retrieval finds for it, three times over, reporting the majority verdict rather than a single pass. A free, deterministic shape check runs on every claim regardless of the verdict, catching the ones with nothing checkable in them before a model is ever asked an opinion. And a gap report — new, and the reason this exists as its own thing rather than a single quality score — compares what the claims actually cite against a census of the document itself, so a claim set can be perfectly correct and still be shown, honestly, as leaving most of a document untouched."),
      RP([["This is verified against a real model twice, at two very different scales.", { b: true }], [" First, a small clinical trial protocol — 3 chunks — with four hand-written claims. Then RFC 6749, the OAuth 2.0 specification — 182 chunks, the actual scale this system is meant for — with eight claims spanning true, contradicting, entirely absent, and genuinely on-topic-but-unstated. Both runs went through Ollama Cloud's gpt-oss:120b. Every claim in both runs came back with a real citation and a verdict a person can check against quoted document text."]]),
      P("The larger run produced a finding the smaller one structurally could not: two concepts the claims plainly discussed by topic — tokens, and the specific HTTP redirect response one claim was written about — came back with zero gap-report coverage. Not a bug: the report checks whether a claim's citation lands in the same chunk where the census anchored a specific named instance, and at 182 chunks, \"discusses the same topic\" and \"cites the same chunk\" can genuinely diverge. That is a real limit of the current design, found by running it at scale rather than assumed in advance."),
      P("The report itself grew in the course of this work, and one of those changes is worth naming here specifically: writing a plain-language explanation of every quality metric — What it measures, How to read it — for a metric called escalated forced a check of what a zero there actually meant. It meant two different things depending on the run, collapsed into one number. That was fixed by adding a second metric next to it. The fix exists because the paper was being written, not because the defect was anticipated."),

      // ---------- Background ----------
      H1("Background: a job with no single owner"),
      P("Deciding whether a claim is actually supported by a document is common and usually informal — a person reading fifty rows in a spreadsheet, deciding by eye which ones the source text backs up. That judgment rarely gets written down as a repeatable method, and it rarely gets checked twice: once someone decides a claim looks right, nothing revisits it."),
      RP([["This project is built around making that judgment repeatable and checkable, without weakening it to make it easy: "], ["can a claim be judged against a document with the same rigour whether a machine wrote it, a person typed it, or it arrived from somewhere else entirely?", { b: true }], [" Everything below is the answer, built and checked rather than assumed."]]),

      // ---------- Architecture ----------
      H1("What the architecture actually does"),
      P("The entailment judge accepts any object exposing an id, a title, an expected outcome, a list of criteria and a list of cited chunk indices — not a specific class, just those attributes. That single design choice is why a bare id-plus-text claim, wrapped in a small object exposing those five fields, satisfies the judge exactly as well as anything more elaborate would. The free shape check works the same way, reading whatever attributes it needs off the object it's given rather than requiring a particular type."),
      table([2000, 7360],
        ["Component", "What makes it work on a bare claim"],
        [
          ["The entailment judge", "Duck-typed by design; nothing about it assumes where the claim came from"],
          ["The shape check", "Reads attributes generically; a small wrapper object is indistinguishable from anything else"],
          ["The census", "Takes plain (name, description) pairs and a chunk list — no dependency on any particular document model"],
          [[["The model client", {}]], "One contract — generate(prompt, system_prompt=None) -> str — is all any check depends on, so the underlying model is swappable without touching the checks themselves"],
        ]),
      SPACER(),
      P("Two pieces were built specifically for this project, for reasons worth stating rather than leaving implicit:"),
      BULLET("Retrieval for a claim finds supporting passages and stops there — the judge decides whether they actually support the claim, not a second model call generating a fresh answer. The one precision-preserving detail kept: a document concept's own vocabulary becomes a separate search probe, never concatenated onto the claim's own text, since concatenating lets a few generic words outweigh the one distinctive word in the claim and displace the right passage instead of adding to it."),
      BULLET("The gap report is new because comparing a document's census against a specific extraction only makes sense when you own that extraction. A claim list from outside the system has no such object to compare against — so the report instead diffs the chunks a claim set actually cited against where the census verified each concept instance lives, which is a different, and more generally applicable, comparison."),
      P("The API returns a job id in well under a second and does the actual multi-minute work — building or reusing the ontology, retrieving, judging, censusing — in the background, backed by a database row that survives a restart. Every completed job also writes a full Excel report automatically, not only a JSON response, since the JSON is for a program to read and the Excel is for a person to."),

      // ---------- Small document run ----------
      H1("The first real run: a small document"),
      P("Four claims, written by hand against a small oncology trial protocol (ONC-2291, 7 sections, 3 chunks) — two intended to be true, one a deliberately subtle miscalibration, one a plausible-sounding claim the document never actually makes."),
      table([900, 3200, 1500, 1100, 3660],
        ["Claim", "Text", "Verdict", "Agreement", "Reason (quoted from the model's own answer)"],
        [
          ["C1", "Patients must be at least 18 years old to enrol.", [["contradicts", { b: true }]], "2/3", "\"Patients must be over 18 years of age.\""],
          ["C2", "Dose reduction of pembrolizumab is permitted to manage toxicity.", [["contradicts", { b: true }]], "3/3", "\"dose reduction of pembrolizumab is not permitted\""],
          ["C3", "The study requires a body mass index under 30.", [["mentions_only", { b: true }]], "2/3", "eligibility is discussed; BMI is never mentioned"],
          ["C4", "The primary endpoint requires blinded independent central review.", [["contradicts", { b: true }]], "3/3", "\"The primary endpoint is overall survival (OS)\"; BICR applies only to the secondary PFS endpoint"],
        ]),
      SPACER(),
      RP([["C1 is the more interesting result precisely because it looks like a mistake and is not.", { b: true }], [" The claim (\"at least 18\") and the document (\"over 18\") are not identical: strictly read, \"over 18\" excludes someone exactly 18, while \"at least 18\" includes them — a genuine, if narrow, boundary mismatch. The judge called it a contradiction rather than an entailment, on a distinction most people would read past. The citation itself is exactly right; only the strictness of the reading is arguable."]]),
      CALLOUT([["Escalation to a stronger model triggered exactly as designed on all three contradictions, then failed — three times, once per doubtful verdict — because no Anthropic credentials were configured for this run.", { b: true }], [" The original verdicts stood, unchanged: best-effort, never blocking, exactly as designed. This is the correct failure mode, verified by triggering it — but it also means this run alone cannot say whether escalation actually changes anything here."]]),
      RP([["On a 3-chunk document, the gap report's own honest limitation showed immediately: the four claims' retrieval touched every one of the three chunks between them, so most concept instances registered as \"addressed\" by coincidence rather than by the report genuinely distinguishing covered from uncovered content.", { b: true }], [" That limitation is the reason a second run, at real scale, mattered."]]),

      // ---------- Large document run ----------
      H1("The second real run: the document this system is built for"),
      P("Eight claims, written by hand against RFC 6749 — the OAuth 2.0 specification, 182 chunks — spanning true, contradicting, absent, and genuinely on-topic-but-unstated. Same model, same discipline, no special-casing for the larger input."),
      table([700, 3500, 1400, 1000, 2760],
        ["Claim", "Text", "Verdict", "Agree.", "Reason"],
        [
          ["C1", "client_credentials is used when the client acts on its own behalf, not the resource owner's.", [["mentions_only", {}]], "2/3", "lists it as a grant type without describing its purpose"],
          ["C2", "A 302 redirect returns an authorization code via the user-agent.", [["mentions_only", {}]], "3/3", "redirection endpoint text found; close, not exact"],
          ["C3", "invalid_request is returned when a required parameter is missing or malformed.", [["entails", { b: true }]], "3/3", "—"],
          ["C4", "Refresh tokens are always issued for every grant type.", [["contradicts", { b: true }]], "3/3", "\"the authorization server MAY issue a new refresh token\""],
          ["C5", "Confidential clients need not authenticate when requesting a token.", [["contradicts", { b: true }]], "3/3", "\"the client requests an access token by authenticating\""],
          ["C6", "The spec mandates PKCE for public clients.", [["no_evidence", { b: true }]], "3/3", "PKCE is not mentioned anywhere in the cited passages"],
          ["C7", "Access tokens must be encrypted with AES-256 in the response.", [["mentions_only", {}]], "3/3", "confidentiality/TLS discussed; AES-256 never mentioned"],
          ["C8", "The authorization code grant requires an authorization code before an access token.", [["entails", { b: true }]], "3/3", "—"],
        ]),
      SPACER(),
      P("C6 is the same shape as the small run's absent-claim case, at real scale: PKCE is a later, separate specification, and the judge correctly found nothing supporting it anywhere in 182 chunks rather than manufacturing a plausible-sounding match. C4 and C5 are the cases this system exists to catch — both stated with confidence, both directly contradicted by a specific sentence, both citations correct."),
      RP([["Extraction's own honesty about itself is worth quoting directly, because it explains something the claim results don't show on their own:", { b: true }], [" \"Read 11 of 24 sections of the document (46%) ... worst is 'http_response' at 8 of 86 chunks (9.3%).\" The ontology built from this document only captured a fraction of it — extraction is bounded by a retrieval budget, not by document size. Claim validation is not bounded the same way: each claim's own retrieval searches the full chunk index fresh, independent of what extraction happened to capture. That is why all eight claims above found a real citation despite the ontology behind them being visibly incomplete — the two are different searches over the same document, and only one of them is capped."]]),
      P("The run took 1200.6 seconds — most of it the census, which reads the document multiple times over for the gap report below, the one part of this pipeline whose cost scales with document size rather than with retrieval."),

      // ---------- Gap report at scale ----------
      H1("The gap report, tested at the scale it needed"),
      P("Using a census — an exhaustive, repeated read of every chunk, reported as a range rather than a single count, since repeated censuses of the same document do not agree with each other — as ground truth for what the document actually contains, independent of what any claim happened to cite."),
      table([2200, 1400, 1700, 4060],
        ["Concept", "Census range", "Addressed", "Never addressed / note"],
        [
          ["role", "16–19", "1 of ~17", "authorization_server, client, resource_owner, user_agent, and 12 more"],
          ["grant_type", "13–19", "5 of ~16", "refresh_token, password, implicit_grant, saml2_bearer, and 7 more"],
          ["error_code", "12–18", "8 of ~13", "best coverage — error, invalid_scope, unsupported_grant_type, 2 more"],
          [[["token", { b: true }]], "7–9", [["0 of ~8", { b: true }]], "access_token, refresh_token, bearer_token, mac_token, authorization_code — despite two claims (C4, C7) directly about tokens"],
          ["parameter", "21–31", "3 of ~26", "the widest spread of any concept this run — real census instability at this scale"],
          [[["http_response", { b: true }]], "13–22", [["0 of ~17", { b: true }]], "despite C2 being specifically about a 302 redirect"],
        ]),
      SPACER(),
      RP([["token and http_response at zero, despite claims that plainly discuss both, is the finding this larger run was run to surface.", { b: true }], [" \"Addressed\" is computed at the chunk level: a concept instance counts only if the census verified it in a chunk some claim's own citation also touched. A claim can be genuinely, humanly about tokens without its retrieval happening to land on the exact chunk where the census anchored a specific named token instance — at 182 chunks, with the census and a claim's retrieval running as two independent searches, that gap is real rather than a formatting artefact. On the 3-chunk document this could not even arise; at this scale it is the report's most honest and most limiting property, found by running it here rather than assumed from the design."]]),
      P("parameter's 21–31 spread is worth its own note: the same census instability measured on the small document's treatment_regimen concept (1–3, one run earlier) reappears here, wider, on a completely different concept in a completely different document. Two independent examples now, not one — the same finding, reproduced rather than asserted from a single run."),

      // ---------- Report improvements ----------
      H1("The report, extended to explain itself"),
      P("Two additions, plus one defect the second one found while being written."),
      BULLET("A Shape checks tab, broken out from the Claims tab: one row per claim, pass or violation, with the reason. The shape check and the judge run independently — a claim can fail the shape check and still receive a real judge verdict on the same row, and the two questions (can this be acted on at all, versus is it true) are now visibly separate rather than folded into one column."),
      BULLET("The Claims tab's Quality classification (Quality / Human check / Incorrect) is now a visible column, not only a cell colour — the same three-way read this report has always computed, now legible in the exported data itself."),
      BULLET("The Quality tab was rewritten to explain every metric in place: what it measures, how to read it, and what a good value looks like — sixteen metrics, none left as a bare number with no way to know if it's good or bad."),
      CALLOUT([["Writing that last explanation surfaced a real defect, not a hypothetical one.", { b: true }], [" A metric called escalated read as 0 whether escalation was never triggered, or was triggered and failed every single time — the small document's own run above is a real example of the second case, invisible in that one number. Fixed by adding a second metric, escalation_failed_batches, so the two states — nothing needed escalation, versus escalation was attempted and never completed — are distinguishable rather than both collapsing to the same silent zero."]]),

      // ---------- What's verified ----------
      H1("What these two runs establish, and what is still open"),
      table([3400, 5960],
        ["Established", "Still open"],
        [
          ["Citations are real at both scales — every verdict quotes text actually in the document", "Escalation has twice triggered and twice failed to complete (no credentials configured) — whether it actually overturns a verdict here is still unmeasured"],
          ["The judge distinguishes contradicts from mentions_only on genuinely subtle cases (C1, C2, C7)", "The gap report's chunk-level \"addressed\" test can miss claims that are topically on-point — a section-level or concept-level alternative is untried"],
          ["A claim with no retrievable support is reported as unjudged or no_evidence, never fabricated as a verdict", "No test yet of two independent extractions of the same document disagreeing with each other"],
          ["Ontology reuse is real: a repeat submission of the identical document completed in under 15 seconds, not minutes", "No concurrent-job test — one process, one background task pool, unexercised under real load"],
          ["Census instability is real and reproduces: two different concepts, two different documents, two different runs, both showed a real spread rather than a fixed count", "Only one document has been run at scale so far; whether 182 chunks is representative of \"large\" is itself untested"],
        ]),
      SPACER(),

      // ---------- What would change the picture most ----------
      H1("What would change the picture most"),
      RP([["If only one further thing were done: run the same claims through with real credentials configured, so escalation can actually complete rather than fail closed twice in a row.", { b: true }], [" C1's borderline contradiction (\"over 18\" versus \"at least 18\") is exactly the kind of case escalation exists for — a stronger model's opinion on it would be a genuine second measurement, not a repeat of the first."]]),
      P("Second: reconsider what \"addressed\" should mean in the gap report given the token and http_response finding above. Exact-chunk matching is precise but may be stricter than useful — a concept-type-level or section-level match would likely close part of that gap, at the cost of being a coarser signal. Both are worth building and comparing rather than picking one from the design alone."),
      P("Third: repeat the census on the same document again and see whether parameter's 21–31 spread narrows, widens, or lands somewhere else entirely. Two data points establish that the instability is real; they do not yet establish its shape."),

      // ---------- Colophon ----------
      H1("How this paper was produced"),
      P("Every figure comes from a recorded run against a real model — two separate runs, each captured in one script invocation, so the per-claim verdicts and the gap report cited for each document describe the same execution rather than being assembled from runs taken at different times."),
      P("The document is generated from a script and rendered to images before delivery, so its layout is looked at rather than assumed."),
      P("The same rule this project's own code applies to a claim applies here to a sentence: a thing that has not been checked is reported as unchecked, not quietly upgraded to a finding."),
    ],
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(process.argv[2], buf);
  console.log("written:", process.argv[2]);
});
