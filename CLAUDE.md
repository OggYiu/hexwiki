# Project instructions

HexWiki is a standalone public Python package: one canonical CLI and compiler
engine with thin host adapters.

## Boundaries

- Do not depend on a parent workspace or another checkout.
- Never add real PDFs, extracted source pages, generated wikis, gold references,
  comparisons, transcripts, credentials, private endpoints, personal paths, or
  account identifiers.
- Keep fixtures synthetic and redistributable.
- Keep adapters thin: they invoke and monitor `hexwiki`; they do not generate a
  substitute wiki.
- Prefer enforced invariants and regression tests to prompt-only requests.
- Refuse overwrite and make every write target explicit.
- Local audit records are canonical; optional observability cannot affect state,
  retry behavior, validation, or exit status.
- Do not run a paid smoke or build, create a release, publish a package, or submit
  a marketplace entry without explicit operator approval.

## Failures

Report every failed test, validator, provider call, or acceptance gate
immediately and wait before diagnosing, changing code, or rerunning it.

## Verification

Run the narrowest relevant offline test first, then the complete offline suite.
Packaging changes also require a built wheel and source archive installed in a
clean environment without access to a source checkout.

