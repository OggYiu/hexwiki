# HexWiki

HexWiki compiles an explicitly bounded PDF scope into an auditable Open
Knowledge Format wiki. One installed CLI owns extraction, source locking,
model-driven generation, review, validation, publication, and verification.

HexWiki is alpha software. Start with a small scope. A smoke or build can run
for a long time and consume paid model capacity.

## Install

For the complete workflow, including model stages:

```text
uv tool install "hexwiki[model]"
hexwiki --version
```

From a source checkout that has not been published:

```text
uv tool install --editable ".[model]"
```

Python 3.11, 3.12, and 3.13 are supported. Install Tesseract only when OCR is
needed and Poppler only when its independent extraction checks are required.

## Shortest supported workflow

Every destination below must be absent unless the command explicitly documents
a resumable mode.

```text
# 1. Create private runtime configuration, then edit the path it prints.
hexwiki init

# 2. Put the API key in the process environment; it is never stored by HexWiki.
# PowerShell: $env:HEXWIKI_API_KEY = "..."
# POSIX:      export HEXWIKI_API_KEY="..."

# 3. Extract a PDF into a new evidence directory.
hexwiki extract book.pdf --output extracted/book

# 4. Create and review an authored scope profile.
hexwiki profile init --pdf book.pdf --extraction extracted/book --pages 1-12 --output profile.json
hexwiki profile check profile.json
hexwiki profile lock profile.json --output profile.lock.json

# 5. Prove local prerequisites first, then the configured network route.
hexwiki preflight --profile profile.json --skip-network
hexwiki preflight --profile profile.json --run-dir runs/preflight-001

# 6. Run a paid, nonpublishing smoke for the exact package/profile/route binding.
hexwiki smoke --profile profile.json --profile-lock profile.lock.json --run-dir runs/smoke-001

# 7. Use that fresh smoke report for one paid build and a new output directory.
hexwiki build --profile profile.json --profile-lock profile.lock.json --smoke-report runs/smoke-001/smoke-report.json --run-dir runs/build-001 --output wikis/book-001
```

`profile init` writes a starter, not an approved scope. Before locking it,
inspect the selected pages and edit its document metadata, boundary clips,
repeating page-furniture rules, citation apparatus, and architecture rationale.
A declared furniture rule that matches no scoped page fails instead of silently
describing another edition.

Monitor a long run from another terminal:

```text
hexwiki status runs/build-001
hexwiki status runs/build-001 --json
```

A build publishes only after its exact fresh smoke, source identity, independent
review, lint, OKF, manifest, and checksum gates pass. It atomically renames a
sealed candidate into the requested absent output. Run artifacts stay in the
explicit run directory; the wiki is at the explicit output path.

## Offline inspection

```text
hexwiki lint wikis/book-001
hexwiki verify wikis/book-001
hexwiki query wikis/book-001 "search terms"
```

Deterministic gates prove source identity, structure, provenance mechanics, and
tree integrity. Without a suitable independent gold reference, they do not
prove that a wiki selected, weighted, or synthesized the source perfectly.

## Configuration and responsibility

Runtime configuration is separate from document profiles. `HEXWIKI_BASE_URL`
and `HEXWIKI_MODEL` select one exact OpenAI-compatible route per run;
`HEXWIKI_API_KEY` supplies its credential. HexWiki never loads a working-folder
`.env` implicitly. Local append-only transcripts are authoritative. Langfuse is
optional outbound observability and cannot change retries, validation, or exit
status.

You are responsible for permission to process the PDF and to send scoped text
to the selected provider, for declaring and checking privacy-sensitive page
furniture, and for retaining or deleting local extractions, transcripts, run
directories, and wikis. See [security and data](docs/security-and-data.md).

## More documentation

- [Operations and failure states](docs/operations.md)
- [Model and quality support matrix](docs/model-support.md)
- [Codex, Claude Code, and Grok Build adapters](docs/adapters.md)
- [Security and data handling](docs/security-and-data.md)

No source document, extracted corpus, generated wiki, benchmark, account,
credential, private endpoint, or private evidence is included in this project.

## License

[MIT](LICENSE)
