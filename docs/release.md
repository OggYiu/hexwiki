# Release process

HexWiki releases separate ordinary verification from the only job allowed to
request a PyPI identity token. A release operator must complete the account and
repository configuration below; committing this workflow does not publish
anything.

## One-time repository configuration

1. Create the `hexwiki` project on PyPI or add a pending trusted publisher.
2. Register this repository, `.github/workflows/publish.yml`, and the `pypi`
   environment as the project's PyPI Trusted Publisher.
3. Create a GitHub environment named `pypi`. Add required reviewers and limit
   deployment to release tags.
4. Require the `CI` workflow before release tags are approved.

No PyPI username, password, or API token belongs in repository secrets. The
publish job receives `id-token: write` only after the build job succeeds and the
protected `pypi` environment is approved. Every other job has read-only content
permission. Builds use the hosted runner account without `sudo` or another
privilege-elevation step.

## Candidate procedure

1. Choose a three-component version and update `pyproject.toml`,
   `src/hexwiki/__init__.py`, and both plugin manifests together.
2. Install `.[dev,model]`, then run `python -m ruff check .` and
   `python -m pytest -q` from a complete clone. Tests use a synthetic provider;
   they do not make paid model calls.
3. Confirm CI on Windows and Ubuntu for Python 3.11, 3.12, and 3.13. CI also
   clean-installs the wheel, Git-free source archive, and hosted commit archive.
4. Review the release notes and the complete-history privacy scan.
5. Obtain explicit approval before creating or pushing `vVERSION`.
6. Dispatch **Publish to PyPI** for that tag, enter the same version, and type
   `publish-hexwiki`. Review the protected-environment prompt before approving
   the OIDC publish job.
7. Only after PyPI verification, create the matching hosted release and test
   `uvx hexwiki --help`, `uv tool install hexwiki`, and every tagged remote
   adapter command documented in `docs/adapters.md` from a fresh conversation.

The workflow refuses a branch, a mismatched tag/version, an unsynchronized
plugin version, a failed test, or invalid distribution metadata. GitHub-hosted
action dependencies are pinned to full commit identifiers and monitored by
Dependabot.

## Adapter validation

Before a release, run the current native validator for each installed host:

```text
python path/to/quick_validate.py skills/hexwiki
python path/to/plugin-creator/scripts/validate_plugin.py .
claude plugin validate .
claude plugin validate .claude-plugin/marketplace.json --strict
grok plugin validate .
```

Grok Build validates and loads the same `.claude-plugin/plugin.json`; its
official compatibility layer does not require a separate Grok manifest.

The current Codex CLI manages plugin marketplaces but does not expose a
`plugin validate` subcommand; its plugin-creator package supplies the canonical
manifest validator used above.

Validate both the complete checkout and an isolated bundle containing the
plugin manifests and `skills/` tree. The contributor-only root `CLAUDE.md` is
not plugin runtime context. The repository marketplace points to the root
plugin and must pass strict catalog validation before publication.
