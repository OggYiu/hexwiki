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

`.claude-plugin/marketplace.json` publishes the same root plugin through a
repository-hosted `hexwiki` marketplace. Claude Code and Grok Build therefore
install the same manifest and skill instead of packaging adapter-specific
implementations. Fresh-conversation model testing, tagging, and publication
remain release gates.

After the `v0.1.0` release tag is public, install the pinned adapter source with
the host's native commands:

```text
# Codex
codex plugin marketplace add OggYiu/hexwiki --ref v0.1.0
codex plugin add hexwiki@hexwiki

# Claude Code
claude plugin marketplace add OggYiu/hexwiki@v0.1.0
claude plugin install hexwiki@hexwiki

# Grok Build
grok plugin install OggYiu/hexwiki@v0.1.0 --trust
```

Pinning the tag makes every host load the same reviewed files. The source
repository's root `CLAUDE.md` is contributor governance, not plugin runtime
context; all runtime instructions are contained in `skills/hexwiki/SKILL.md`.
