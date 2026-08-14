---
name: hexwiki
description: Operate the installed HexWiki CLI to extract PDFs, author and lock bounded profiles, run preflight, execute explicitly approved smoke/build workflows, monitor runs, and lint, verify, or query auditable OKF wikis. Use for HexWiki setup, compilation, status, and troubleshooting. Do not use to invent an alternate wiki-generation flow or bypass approval for paid model calls.
---

# HexWiki

Use the installed `hexwiki` command as the only compiler. Do not browse for
replacement source material, write semantic wiki notes yourself, call a host
model as a substitute, or reproduce engine logic in a helper script.

## Operate the workflow

1. Run `hexwiki --version` and `hexwiki --help`. If the command is unavailable,
   report that HexWiki must be installed; do not emulate it.
2. Identify explicit PDF, extraction, profile, lock, run, smoke-report, and
   output paths. Refuse an existing destination unless the named CLI mode
   explicitly supports resume.
3. For a new source, run `hexwiki extract`, then `hexwiki profile init`. Require
   the operator-authored profile to be reviewed for pages, boundaries, page
   furniture, apparatus, architecture floors, and rationales before locking it.
4. Run `hexwiki profile check` and `hexwiki profile lock`, followed by
   `hexwiki preflight --profile PROFILE --skip-network`.
5. Before networked preflight, smoke, or build, confirm the user explicitly
   authorized paid/model work and configured the exact route, model, and
   credential. State that cost and duration vary.
6. Run a smoke before a build. Pass the resulting fresh, unmodified smoke report
   and the same exact profile lock to `hexwiki build`.
7. While a long command runs, inspect it with
   `hexwiki status RUN_DIRECTORY --json`. Continue until `terminal.json` exists;
   do not infer success from a candidate directory or console silence.
8. On any nonzero exit or failed terminal state, report the exact component,
   error, elapsed cost when known, likely cause, and safe options. Preserve the
   failed run. Do not add retries beyond those already implemented by HexWiki.
9. After publication, run `hexwiki lint OUTPUT` and `hexwiki verify OUTPUT`.
   Use `hexwiki query OUTPUT QUERY` only for read-only retrieval.

Use subcommand `--help` rather than guessing flags. Keep adapters and host
instructions out of the generated wiki. Local HexWiki audit records and terminal
state are authoritative; optional observability never is.
