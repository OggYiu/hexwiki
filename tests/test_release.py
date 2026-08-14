"""Release workflow and candidate-document regressions."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import yaml

import hexwiki


ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH = ROOT / ".github" / "workflows" / "publish.yml"
ACTION_PIN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


def _workflow(path: Path) -> dict[str, object]:
    loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(loaded, dict)
    return loaded


def _steps(workflow: dict[str, object]) -> list[dict[str, object]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    return [
        step
        for job in jobs.values()
        for step in job["steps"]
        if isinstance(step, dict)
    ]


def test_ci_covers_supported_matrix_and_release_boundaries() -> None:
    workflow = _workflow(CI)
    assert set(workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    matrix = jobs["test"]["strategy"]["matrix"]
    assert set(matrix["os"]) == {"ubuntu-latest", "windows-latest"}
    assert set(matrix["python"]) == {"3.11", "3.12", "3.13"}
    assert {"test", "package", "remote-archive"}.issubset(jobs)

    text = CI.read_text(encoding="utf-8")
    for command in (
        "python -m ruff check .",
        "python -m pytest -q",
        "hexwiki preflight --skip-network --json",
        "python -m build --wheel --sdist",
        "python -m twine check",
        "source-archive.tar.gz",
    ):
        assert command in text
    assert "id-token" not in text
    assert "sudo" not in text


def test_publish_is_manual_tag_bound_and_oidc_isolated() -> None:
    workflow = _workflow(PUBLISH)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert "refs/tags/v" in jobs["build"]["if"]
    assert "publish-hexwiki" in jobs["build"]["if"]
    assert "id-token" not in jobs["build"].get("permissions", {})
    assert jobs["publish"]["environment"]["name"] == "pypi"
    assert jobs["publish"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
    }
    publish_steps = jobs["publish"]["steps"]
    publish_step = next(
        step for step in publish_steps if step.get("name") == "Publish without a long-lived upload token"
    )
    assert publish_step["uses"].startswith("pypa/gh-action-pypi-publish@")
    assert "username" not in publish_step.get("with", {})
    assert "password" not in publish_step.get("with", {})


def test_all_external_actions_are_immutable_pins() -> None:
    for path in (CI, PUBLISH):
        for step in _steps(_workflow(path)):
            action = step.get("uses")
            if action is not None:
                assert ACTION_PIN.fullmatch(action), f"mutable action reference in {path}: {action}"


def test_release_version_and_limitations_are_synchronized() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    versions = {metadata["project"]["version"], hexwiki.__version__}
    for manifest in (
        ROOT / ".codex-plugin" / "plugin.json",
        ROOT / ".claude-plugin" / "plugin.json",
        ROOT / ".grok-plugin" / "plugin.json",
    ):
        versions.add(json.loads(manifest.read_text(encoding="utf-8"))["version"])
    assert versions == {"0.1.0"}

    notes = (ROOT / "docs" / "release-notes-0.1.0.md").read_text(encoding="utf-8")
    assert "alpha" in notes.lower()
    assert "do not prove" in " ".join(notes.split())
    assert "not a completed publication" in notes
    process = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
    assert "required reviewers" in process
    assert "explicit approval" in process
    assert "No PyPI username, password, or API token" in process
    assert "plugin-creator/scripts/validate_plugin.py" in process
    assert "codex plugin validate" not in process
