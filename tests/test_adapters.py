from __future__ import annotations

import json
import re
from pathlib import Path

import hexwiki


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "hexwiki" / "SKILL.md"
MANIFESTS = (
    ROOT / ".codex-plugin" / "plugin.json",
    ROOT / ".claude-plugin" / "plugin.json",
    ROOT / ".grok-plugin" / "plugin.json",
)


def _skill_front_matter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(?P<header>.*?)\n---\n", text, re.DOTALL)
    assert match is not None
    result: dict[str, str] = {}
    for line in match.group("header").splitlines():
        key, separator, value = line.partition(":")
        assert separator
        result[key.strip()] = value.strip()
    return result


def test_all_host_manifests_share_one_skill_and_version() -> None:
    for path in MANIFESTS:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["name"] == "hexwiki"
        assert manifest["version"] == hexwiki.__version__
        assert manifest["skills"] == "./skills/"
        assert "mcpServers" not in manifest
        assert "apps" not in manifest
        assert "hooks" not in manifest


def test_portable_skill_delegates_to_the_cli() -> None:
    text = SKILL.read_text(encoding="utf-8")
    header = _skill_front_matter(text)
    assert header == {
        "name": "hexwiki",
        "description": (
            "Operate the installed HexWiki CLI to extract PDFs, author and lock "
            "bounded profiles, run preflight, execute explicitly approved smoke/build "
            "workflows, monitor runs, and lint, verify, or query auditable OKF wikis. "
            "Use for HexWiki setup, compilation, status, and troubleshooting. Do not "
            "use to invent an alternate wiki-generation flow or bypass approval for "
            "paid model calls."
        ),
    }
    assert "[TODO:" not in text
    assert "hexwiki --version" in text
    assert "hexwiki profile lock" in text
    assert "hexwiki preflight --profile PROFILE --skip-network" in text
    assert "hexwiki status RUN_DIRECTORY --json" in text
    assert "hexwiki build" in text
    assert "Do not browse" in text
    assert "write semantic wiki notes yourself" in text
    assert "call a host\nmodel as a substitute" in text


def test_adapter_ui_metadata_is_generated_and_thin() -> None:
    metadata = (SKILL.parent / "agents" / "openai.yaml").read_text(encoding="utf-8")
    assert 'display_name: "HexWiki"' in metadata
    assert 'short_description: "Build auditable source-bounded PDF wikis"' in metadata
    assert "$hexwiki" in metadata
    assert "dependencies:" not in metadata


def test_public_docs_do_not_overclaim_model_quality() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    matrix = (ROOT / "docs" / "model-support.md").read_text(encoding="utf-8")
    assert "consume paid model capacity" in readme
    assert "do not prove" in " ".join(readme.split())
    assert "Not yet demonstrated" in matrix
    assert "Expected by protocol" in matrix
