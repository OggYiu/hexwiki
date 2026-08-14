# Operations and failure states

HexWiki has one engine and one CLI. Use `hexwiki --help` and the subcommand help
as the canonical command reference.

## Author the scope before spending model capacity

An extraction retains multiple evidence layers. A profile selects the native
page text used for generation and declares all transformations that affect its
hash:

- primary and optional apparatus pages;
- exact boundary markers and which side to retain;
- repeating page-furniture regular expressions and reasons;
- apparatus entry range and, when needed, its numbering pattern;
- per-scope architecture floors and rationales for nullable floors.

`hexwiki profile lock` rechecks the PDF, extraction manifests, extraction
checksums, every selected native page, furniture matches, boundary markers, and
apparatus continuity. It writes derived hashes to a separate lock file and does
not rewrite the authored profile.

## Preflight, smoke, and build

Run offline preflight first. Networked preflight separately checks filesystem
sandbox behavior, model-runtime imports, model discovery, ordinary chat, tool
calling, and optional observability. Its explicit run directory must not exist.

A smoke executes the production-shaped model and review path but never
publishes a wiki. Its report is bound to the exact installed package resources,
dependency versions, profile, lock, source, route, model, and runtime limits.
A build rejects a missing, corrected, failed, expired, or differently bound
smoke report.

The default upper bounds are 90 minutes for smoke and 300 minutes for build.
Actual duration and provider cost vary with the source, plan size, retries,
route, and model. Stage-level retries declared by the engine are part of normal
operation. Do not wrap the CLI in an additional blind retry loop.

## Terminal state and recovery

Read `hexwiki status RUN_DIRECTORY --json`. The terminal record, not a partial
candidate or the last visible console line, decides the outcome.

- `passed`: all required stages and gates for that command completed.
- `configuration`: source, profile, lock, smoke binding, or runtime setup is
  invalid.
- `provider`: a required provider operation failed.
- `validation`: generation completed but a required review or deterministic
  gate did not pass.
- `timeout`: the parent process enforced the absolute wall-clock deadline.
- `runtime`: another required component failed.

Preserve a failed run directory as evidence. Read `failure.json`,
`terminal.json`, `progress.json`, `actions.jsonl`, and the relevant local stage
transcript before deciding whether a new, explicitly named run is justified.
Never edit a smoke report or completed output to make it reusable.

Build publication is an atomic rename. The destination must be absent, and a
failed build does not publish a partial wiki there.
