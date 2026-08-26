"""Package metadata and resource regressions."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tarfile
import tomllib
import unittest
import zipfile
from importlib import resources
from pathlib import Path

from hexwiki import __version__
from hexwiki.engine.profile import validate_profile
from tests.test_public_boundary import (
    BANNED_HASHES,
    ngram_hashes,
    privacy_findings,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_metadata_and_runtime_versions_match(self) -> None:
        metadata = tomllib.loads(
            (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(metadata["project"]["name"], "hexwiki")
        self.assertEqual(metadata["project"]["version"], __version__)
        self.assertEqual(metadata["project"]["scripts"]["hexwiki"], "hexwiki.cli:main")

    def test_resources_load_without_a_repository_root(self) -> None:
        package = resources.files("hexwiki.resources")
        schema = json.loads(package.joinpath("profile.schema.json").read_text(encoding="utf-8"))
        example = json.loads(package.joinpath("profile.example.json").read_text(encoding="utf-8"))
        guide = package.joinpath("guide.md").read_text(encoding="utf-8")
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertEqual(example["schema_version"], 1)
        validate_profile(example)
        self.assertIn("HexWiki", guide)

    def test_license_is_mit(self) -> None:
        license_text = (REPOSITORY_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)

    def test_git_free_wheel_and_sdist_have_only_public_package_material(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "git-free-source"
            ignored = shutil.ignore_patterns(
                ".git",
                ".pytest_cache",
                ".ruff_cache",
                ".venv",
                "__pycache__",
                "build",
                "dist",
                "*.egg-info",
            )
            shutil.copytree(REPOSITORY_ROOT, source, ignore=ignored)
            self.assertFalse((source / ".git").exists())
            output = root / "artifacts"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--no-isolation",
                    "--wheel",
                    "--sdist",
                    "--outdir",
                    str(output),
                    str(source),
                ],
                cwd=root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + "\n" + completed.stderr,
            )
            wheels = list(output.glob("*.whl"))
            sdists = list(output.glob("*.tar.gz"))
            self.assertEqual(len(wheels), 1)
            self.assertEqual(len(sdists), 1)

            with zipfile.ZipFile(wheels[0]) as archive:
                wheel_names = archive.namelist()
                self.assertIn("hexwiki/resources/guide.md", wheel_names)
                self.assertIn("hexwiki/resources/profile.schema.json", wheel_names)
                self.assertIn("hexwiki/resources/profile.example.json", wheel_names)
                self.assertTrue(any(name.endswith(".dist-info/METADATA") for name in wheel_names))
                self.assertTrue(
                    all(
                        name.startswith("hexwiki/") or ".dist-info/" in name
                        for name in wheel_names
                    )
                )
                wheel_members = {
                    name: archive.read(name)
                    for name in wheel_names
                    if not name.endswith("/")
                }

            with tarfile.open(sdists[0], "r:gz") as archive:
                members = [member for member in archive.getmembers() if member.isfile()]
                stripped = {
                    "/".join(member.name.split("/")[1:]): archive.extractfile(member).read()
                    for member in members
                }
            for required in (
                ".claude-plugin/plugin.json",
                ".codex-plugin/plugin.json",
                ".github/workflows/ci.yml",
                ".github/workflows/publish.yml",
                "pyproject.toml",
                "LICENSE",
                "README.md",
                "MANIFEST.in",
                "docs/adapters.md",
                "docs/release.md",
                "docs/release-notes-0.1.0.md",
                "skills/hexwiki/SKILL.md",
                "skills/hexwiki/agents/openai.yaml",
                "src/hexwiki/resources/guide.md",
                "src/hexwiki/resources/profile.schema.json",
                "src/hexwiki/resources/profile.example.json",
                "tests/test_packaging.py",
            ):
                self.assertIn(required, stripped)
            self.assertFalse(any("/.git/" in f"/{name}/" for name in stripped))

            for name, content in {**wheel_members, **stripped}.items():
                normalized = name.replace("\\", "/")
                self.assertFalse(normalized.startswith("/"), normalized)
                self.assertFalse(
                    {"input", "extracted", "llm-wikis", "compare_results", "logs"}
                    .intersection(normalized.split("/")),
                    normalized,
                )
                if Path(normalized).suffix.lower() not in {
                    "",
                    ".cfg",
                    ".in",
                    ".ini",
                    ".json",
                    ".md",
                    ".py",
                    ".rst",
                    ".toml",
                    ".txt",
                    ".yaml",
                    ".yml",
                }:
                    continue
                text = content.decode("utf-8", errors="replace")
                self.assertFalse(ngram_hashes(text).intersection(BANNED_HASHES), normalized)
                self.assertEqual(privacy_findings(text), [], normalized)

            probe = (
                "import json,sys; "
                "sys.path.insert(0, sys.argv[1]); "
                "import hexwiki; "
                "from importlib import resources; "
                "from hexwiki.cli import main; "
                "schema=json.loads(resources.files('hexwiki.resources')"
                ".joinpath('profile.schema.json').read_text(encoding='utf-8')); "
                "assert schema['properties']['schema_version']['const'] == 1; "
                "assert main([]) == 0"
            )
            for label, artifact in (("wheel", wheels[0]), ("sdist", sdists[0])):
                install_root = root / f"installed-{label}"
                installed = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-deps",
                        "--no-build-isolation",
                        "--target",
                        str(install_root),
                        str(artifact),
                    ],
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    check=False,
                )
                self.assertEqual(
                    installed.returncode,
                    0,
                    installed.stdout + "\n" + installed.stderr,
                )
                checked = subprocess.run(
                    [sys.executable, "-I", "-c", probe, str(install_root)],
                    cwd=root,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(
                    checked.returncode,
                    0,
                    checked.stdout + "\n" + checked.stderr,
                )


if __name__ == "__main__":
    unittest.main()
