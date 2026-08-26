# Agent adapters

The repository ships one portable Agent Skill at
`skills/hexwiki/SKILL.md`. It selects and monitors the installed `hexwiki` CLI;
it does not browse for source material, write wiki notes, invoke a host model as
a substitute compiler, or bypass the smoke/build contract.

| Host | Manifest | Local development check | Invocation |
|---|---|---|---|
| Codex | `.codex-plugin/plugin.json` | Validate the manifest and skill with the bundled validators | `$hexwiki` |
| Claude Code | `.claude-plugin/plugin.json` | `claude plugin validate .`, then `claude --plugin-dir .` | `/hexwiki:hexwiki` |
| Grok Build | `.claude-plugin/plugin.json` (Claude-compatible) | `grok plugin validate .`, then `grok plugin install .` | `/hexwiki` or the extensions picker |

Both manifests point to `./skills/` and carry the same package version. The
skill’s command contract is identical on every host:

1. Confirm the installed `hexwiki` command.
2. Use explicit profile, lock, run, report, and output paths.
3. Run offline preflight before any approved networked operation.
4. Monitor `hexwiki status RUN_DIRECTORY --json` until a terminal state exists.
5. Treat any nonzero exit or failed terminal record as a failure and preserve
   the run evidence.

Adapter formats were rechecked on 2026-08-26 against the current official
[OpenAI skill documentation](https://learn.chatgpt.com/docs/build-skills),
[OpenAI plugin documentation](https://learn.chatgpt.com/docs/build-plugins),
[Claude Code plugin documentation](https://code.claude.com/docs/en/plugins),
and [Grok Build compatibility documentation](https://docs.x.ai/build/features/skills-plugins-marketplaces).
Grok Build officially reads Claude Code plugins with no extra configuration,
so HexWiki deliberately reuses `.claude-plugin/plugin.json`. It does not ship an
undocumented duplicate `.grok-plugin` manifest.

No marketplace entry is included. Marketplace installation, fresh-conversation
model testing, tagging, and publication are operator-controlled release steps.

This source repository has a root `CLAUDE.md` for contributor governance.
Claude's validator correctly warns that a plugin does not load that file as
plugin context; the skill carries all runtime instructions. Release validation
also stages only the manifest and `skills/` tree and validates that isolated
plugin bundle with `--strict`.
