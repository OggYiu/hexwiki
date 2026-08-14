# HexWiki

HexWiki is an installable Python product for compiling a bounded PDF scope into
an auditable Open Knowledge Format wiki. The package is under active extraction
and is not yet ready for production generation.

The public architecture has one canonical CLI and Python engine. Agent adapters
will invoke that CLI rather than implementing a second generation workflow.

## Current scaffold

```text
python -m hexwiki --help
hexwiki --help
```

The extraction, profile, smoke, build, validation, and query commands are
reserved in the CLI but intentionally unavailable until their offline gates are
ported and verified.

No source document, extracted corpus, generated wiki, benchmark, account,
credential, or private route is included in this repository.

## License

[MIT](LICENSE)

