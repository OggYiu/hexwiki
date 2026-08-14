"""Package metadata and resource regressions."""

from __future__ import annotations

import json
import tomllib
import unittest
from importlib import resources
from pathlib import Path

from hexwiki import __version__


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
        self.assertIn("HexWiki", guide)

    def test_license_is_mit(self) -> None:
        license_text = (REPOSITORY_ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)


if __name__ == "__main__":
    unittest.main()

