"""Optional-apparatus and legacy immutable-input compatibility tests."""

from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from hexwiki.engine import okf, source
from hexwiki.engine.audit import AuditLog, exclusive_json
from hexwiki.engine.finalize import prepare_wiki
from hexwiki.engine.profile import load_profile, load_profile_lock
from hexwiki.extraction.pdf import ExtractionOptions, extract
from hexwiki.tools.wiki import query_vault
from tests.test_engine import _profile_value, _write_synthetic_pdf


class CompatibilityTests(unittest.TestCase):
    def test_custom_bare_number_apparatus_walks_only_the_expected_sequence(self) -> None:
        text = "1\nFirst synthetic entry.\n42\nWrapped page range.\n2\nSecond entry.\n"
        self.assertEqual(
            source.apparatus_numbers(text, 1, 2, re.compile(r"(?m)^(\d+)$")),
            [1, 2],
        )

    def test_no_apparatus_scope_has_no_banner_note_or_dangling_link(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf_path = root / "source.pdf"
            extraction = root / "extracted"
            _write_synthetic_pdf(pdf_path)
            result = extract(
                ExtractionOptions(
                    input_pdf=pdf_path,
                    output_dir=extraction,
                    render_dpi=72,
                    ocr_mode="none",
                    ocr_image_fallback=False,
                    save_svg=False,
                    poppler_mode="never",
                )
            )
            self.assertEqual(result["status"], "passed")

            value = copy.deepcopy(_profile_value())
            value["scope"]["label"] = "selected primary text without an apparatus"
            value["scope"]["apparatus_pages"] = []
            value["scope"]["apparatus"] = None
            value["scope"]["canonical_banners"]["apparatus"] = None
            value["scope"]["boundaries"] = [value["scope"]["boundaries"][0]]
            profile_path = root / "profile.json"
            profile_path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
            profile = load_profile(profile_path)
            lock_path = root / "profile.lock.json"
            exclusive_json(lock_path, source.build_profile_lock(profile))
            lock = load_profile_lock(lock_path)
            inspected = source.verify_profile_lock(profile, lock)
            self.assertEqual(lock["scope"]["apparatus_entries"], [])
            self.assertNotIn("CITATION APPARATUS", inspected["canonical"])

            audit = AuditLog(root / "actions.jsonl", "no-apparatus-test")
            staged = source.stage_source(
                profile=profile,
                lock=lock,
                work_root=root / "source-stage",
                audit=audit,
            )
            wiki = root / "wiki"
            prepare_wiki(
                wiki_dir=wiki,
                pages=staged["pages"],
                profile=staged["runtime"],
                audit=audit,
                run_id="no-apparatus-test",
                extraction_root=profile.extraction_root,
            )
            source_text = "\n".join(
                path.read_text(encoding="utf-8")
                for path in sorted((wiki / "sources").glob("*.md"))
            )
            self.assertIn("Citation apparatus: none declared for this scope", source_text)
            self.assertNotIn("numbered-references.md", source_text)
            self.assertFalse((wiki / "sources" / "numbered-references.md").exists())

    def test_okf_v02_and_legacy_v01_notes_are_both_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            concepts = root / "concepts"
            concepts.mkdir()
            common = (
                "type: Concept\n"
                "title: Synthetic Compatibility Note\n"
                "description: This synthetic compatibility description has more than eight words.\n"
                "tags: [synthetic, compatibility]\n"
                "semantic_note: true\n"
                "status: draft\n"
            )
            legacy = (
                "---\n"
                + common
                + "verified: false\n"
                "sources:\n"
                "  - ../sources/pdf-pages/page-0001.md\n"
                "---\n\n"
                "# Synthetic Compatibility Note\n\n"
                "Legacy immutable input remains queryable.\n"
            )
            current = (
                "---\n"
                + common.replace("Compatibility Note", "Current Note")
                + "sources:\n"
                '  - { resource: "../sources/pdf-pages/page-0001.md" }\n'
                "---\n\n"
                "# Synthetic Current Note\n\n"
                "Current OKF output omits the verified key.\n"
            )
            (concepts / "legacy.md").write_text(legacy, encoding="utf-8")
            (concepts / "current.md").write_text(current, encoding="utf-8")

            report = okf.check_directory(root)
            self.assertEqual(okf.n_errors_of(report["issues"]), 0)
            self.assertTrue(query_vault(root, "immutable input", 5))
            self.assertNotIn("verified:", current)


if __name__ == "__main__":
    unittest.main()
