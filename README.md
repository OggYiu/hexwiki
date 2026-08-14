# HexWiki

HexWiki is an installable Python product for compiling a bounded PDF scope into
an auditable Open Knowledge Format wiki. The deterministic extraction and
validation layer is available; model-driven generation is still under active
development and is not ready for production use.

The public architecture has one canonical CLI and Python engine. Agent adapters
will invoke that CLI rather than implementing a second generation workflow.

## Offline commands

```text
hexwiki --help
hexwiki extract source.pdf --output extracted/source
hexwiki profile init --extraction extracted/source --pdf source.pdf --output profile.json
hexwiki profile check profile.json
hexwiki profile lock profile.json --output profile.lock.json
hexwiki preflight --profile profile.json --skip-network
hexwiki lint wiki-directory
hexwiki verify wiki-directory
hexwiki query wiki-directory "search terms"
```

Relative source paths resolve from the profile file. `profile lock` reads the
PDF and passed extraction, applies the declared page scope, clipping, repeating
furniture rules, and optional apparatus format, then writes all derived hashes
to a separate lock without modifying the authored profile. Every output command
refuses an existing destination unless an explicit resumable mode says otherwise.

`init`, the provider/network portion of preflight, `smoke`, `build`, and `status`
remain reserved until the guarded model runtime is implemented. No model or paid
provider call is made by the commands above.

No source document, extracted corpus, generated wiki, benchmark, account,
credential, or private route is included in this repository.

## License

[MIT](LICENSE)
