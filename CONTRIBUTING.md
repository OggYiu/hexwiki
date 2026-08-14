# Contributing

HexWiki is being extracted from a private evidence workspace into an independent
public package. Contributions must improve the reusable compiler, validator, or
packaging rather than one generated wiki.

## Offline checks

```text
ruff check .
python -m pytest -q
python -m hexwiki --help
python path/to/quick_validate.py skills/hexwiki
python path/to/validate_plugin.py .
```

The last two commands use the validators bundled with the current Codex
skill-creator and plugin-creator tools. Claude Code and Grok Build contributors
should also run their installed `plugin validate` commands.

Do not add source PDFs, extracted pages, generated wikis, model transcripts,
benchmarks, credentials, private endpoints, personal paths, or account details.
Networked model checks and releases require separate operator approval.
