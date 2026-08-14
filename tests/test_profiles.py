"""Strict document-profile contract tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from importlib import resources
from pathlib import Path

from hexwiki.engine.profile import (
    DocumentProfile,
    ProfileError,
    REQUIRED_NOTE_TYPES,
    load_profile,
    validate_profile,
)


def valid_profile() -> dict:
    return {
        "schema_version": 1,
        "profile_id": "synthetic-selection",
        "source": {"pdf": "source.pdf", "extraction": "extracted"},
        "document": {
            "id": "synthetic-source",
            "title": "Synthetic Source",
            "author": "Example Author",
        },
        "scope": {
            "id": "selection",
            "title": "Selected pages",
            "label": "selected pages 1-2",
            "primary_pages": [1, 2],
            "apparatus_pages": [],
            "apparatus": None,
            "canonical_banners": {
                "primary": "=== PRIMARY TEXT ===",
                "apparatus": None,
            },
            "boundaries": [],
            "page_furniture": [],
        },
        "architecture": {
            "rationale": "The short synthetic selection tests a general explanatory scope.",
            "minimums": {
                "case_dossiers": None,
                "concept_notes": 1,
                "section_notes": 1,
                "claims": 1,
                "motifs": None,
            },
            "nullable_rationales": {
                "case_dossiers": "The explanatory selection contains no narrated episodes.",
                "motifs": "A cross-episode motif is undefined when no episodes are present.",
            },
            "required_note_types": list(REQUIRED_NOTE_TYPES),
        },
        "output": {"format": "Open Knowledge Format", "okf_version": "0.2"},
    }


class ProfileTests(unittest.TestCase):
    def test_valid_profile_and_bundled_schema_agree_on_top_level_contract(self) -> None:
        value = valid_profile()
        validate_profile(value)
        schema = json.loads(
            resources.files("hexwiki.resources")
            .joinpath("profile.schema.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(set(schema["required"]), set(value))
        self.assertFalse(schema["additionalProperties"])

    def test_unknown_field_is_rejected(self) -> None:
        value = valid_profile()
        value["model"] = {"api_key": "not-allowed"}
        with self.assertRaisesRegex(ProfileError, "unknown fields: model"):
            validate_profile(value)

    def test_apparatus_is_all_or_nothing(self) -> None:
        value = valid_profile()
        value["scope"]["apparatus_pages"] = [3]
        with self.assertRaisesRegex(ProfileError, "scope.apparatus must be an object"):
            validate_profile(value)

    def test_custom_apparatus_pattern_must_capture_number(self) -> None:
        value = valid_profile()
        value["scope"].update(
            {
                "apparatus_pages": [3],
                "apparatus": {
                    "id": "notes",
                    "label": "notes",
                    "entry_range": [1, 2],
                    "entry_pattern": r"(?m)^\d+$",
                },
                "canonical_banners": {
                    "primary": "=== PRIMARY TEXT ===",
                    "apparatus": "=== NOTES ===",
                },
            }
        )
        with self.assertRaisesRegex(ProfileError, "capture the entry number"):
            validate_profile(value)

    def test_each_null_floor_requires_its_own_rationale(self) -> None:
        value = valid_profile()
        del value["architecture"]["nullable_rationales"]["motifs"]
        with self.assertRaisesRegex(ProfileError, "missing reasons for motifs"):
            validate_profile(value)

    def test_duplicate_json_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "profile.json"
            path.write_text('{"schema_version": 1, "schema_version": 1}\n', encoding="utf-8")
            with self.assertRaisesRegex(ProfileError, "duplicate JSON key"):
                load_profile(path)

    def test_paths_resolve_from_profile_file_not_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            profile_path = root / "profiles" / "selection.json"
            profile_path.parent.mkdir()
            profile = DocumentProfile(profile_path, copy.deepcopy(valid_profile()))
            self.assertEqual(profile.pdf_path, (profile_path.parent / "source.pdf").resolve())
            self.assertEqual(
                profile.extraction_root,
                (profile_path.parent / "extracted").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
