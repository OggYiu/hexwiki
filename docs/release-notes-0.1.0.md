# HexWiki 0.1.0 release candidate

This alpha candidate provides one CLI for bounded PDF extraction, authored and
locked scope profiles, guarded model-driven compilation, independent review,
deterministic lint and integrity gates, local audit records, and offline wiki
inspection. Portable Codex and Claude Code manifests delegate to that same CLI;
Grok Build consumes the Claude-compatible plugin directly.

Supported runtimes are Python 3.11 through 3.13 on Windows and Linux. Package
tests cover Git-free source archives, clean wheel/source installs, bundled
schemas and guides, version synchronization, synthetic extraction and runtime
failures, filesystem confinement, and the complete reachable Git history.

Important limitations:

- HexWiki is alpha software. Model smoke and build runs may be slow and consume
  paid provider capacity.
- OpenAI-compatible transport does not guarantee equivalent model behavior.
  Each exact provider route and model needs its own preflight and smoke.
- Deterministic gates establish source identity, structure, provenance
  mechanics, quotation checks, and tree integrity. Without a suitable
  independent reference, they do not prove ideal selection, weighting, or
  synthesis.
- Operators are responsible for rights to process source documents and for the
  privacy, retention, and deletion of local PDFs, extractions, transcripts,
  run directories, and generated wikis.
- The package contains no source corpus, generated evidence, private endpoint,
  credential, or benchmark result.

## One measured benchmark result

One end-to-end run was measured against a frozen, privately held reference
bundle. The numbers below are that single run and nothing more.

| | |
|---|---|
| Package | `hexwiki` 0.1.0, runtime commit `87e12e4` |
| Route | operator-managed OpenAI-compatible endpoint, model `grok-aiprogramming` |
| Date | 2026-08-29 |
| Source scope | one chapter of one book, 25 PDF pages, 18 apparatus entries |
| Score | **86.41 / 100** |
| Threshold | `> 95.00` — **not met; the candidate was not accepted** |
| Hard gates | **14 / 14 passed** |

By component: semantic alignment 28.05/40, structure and retrieval 18.36/20,
source fidelity and provenance 20/20, epistemic discipline 15/15, integrity and
reproducibility 5/5.

Coverage of the 43 reference units: 18 covered, 25 partial, **0 missing**.
Retrieval 10/12. The build made 615 model calls with zero provider errors across
64 stages, and independent review closed with zero material findings and
complete page coverage. Quotation verification checked 362 substantive
quotations and found 91.16% supported by a page the note itself cites, with two
quotations attributable to a page the note did not cite.

What the shortfall is, and is not. No reference unit is missing, and the
deterministic half of the benchmark is perfect. The gap is concentrated in
partial semantic alignment, and the recorded reasons are dominated by editorial
divergence rather than absent or wrong content: the run's motif matrix covers 32
episodes where the reference selects 12, its argument map orders the chapter's
inference differently from the reference's distinctions, and its rosters carry
different caution columns. Closing those would mean writing one reference
wiki's particular selections into the compiler's prompts, which would improve
this score without improving the product.

Read this as one measurement of one chapter of one book on one route, not as a
general quality claim. The reference bundle is not a blinded control, and the
comparator's semantic half uses a model to judge; its deterministic half uses
none. A different source, scope, or route needs its own preflight, smoke, and
measurement.

This file describes a candidate, not a completed publication. A tag, hosted
release, marketplace submission, and PyPI upload remain explicit operator
actions under the release procedure.
