"""Installed-package-shaped offline extraction-to-seal integration test."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz

from hexwiki.engine import lint, okf, source
from hexwiki.engine.audit import AuditLog, exclusive_json
from hexwiki.engine.finalize import (
    prepare_wiki,
    publish_candidate,
    seal_wiki,
    verify_checksums,
)
from hexwiki.engine.profile import REQUIRED_NOTE_TYPES, load_profile, load_profile_lock
from hexwiki.extraction.pdf import ExtractionOptions, extract
from hexwiki.tools.quotes import verify as verify_quotes
from hexwiki.tools.wiki import query_vault


def _write_synthetic_pdf(path: Path) -> None:
    document = fitz.open()
    texts = [
        (
            "SYNTHETIC RUNNING HEAD\n"
            "Outside introductory material.\n"
            "BEGIN SELECTED TEXT\n"
            "The blue triangle demonstrates a bounded synthetic claim.\n"
        ),
        (
            "SYNTHETIC RUNNING HEAD\n"
            "A second page explains that the blue triangle remains an example only.\n"
        ),
        (
            "SYNTHETIC RUNNING HEAD\n"
            "1\n"
            "Synthetic Reference Alpha.\n"
            "2\n"
            "Synthetic Reference Beta.\n"
            "OUTSIDE APPARATUS\n"
            "3\n"
            "Reference for another scope.\n"
        ),
    ]
    for text in texts:
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 72), text, fontsize=11)
    document.save(path)
    document.close()


def _profile_value() -> dict:
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
            "id": "selected-pages",
            "title": "Selected pages",
            "label": "selected primary text with numbered references",
            "primary_pages": [1, 2],
            "apparatus_pages": [3],
            "apparatus": {
                "id": "numbered-references",
                "label": "numbered references",
                "entry_range": [1, 2],
                "entry_pattern": r"(?m)^(\d{1,2})\s*$",
            },
            "canonical_banners": {
                "primary": "=== PRIMARY TEXT ===",
                "apparatus": "=== CITATION APPARATUS ===",
            },
            "boundaries": [
                {
                    "page": 1,
                    "marker": "BEGIN SELECTED TEXT",
                    "keep": "after",
                    "action": "keep-after-selection-start",
                    "note": "discard introductory text printed before the selected scope",
                },
                {
                    "page": 3,
                    "marker": "OUTSIDE APPARATUS",
                    "keep": "before",
                    "action": "keep-before-next-apparatus",
                    "note": "discard numbered material belonging to the following scope",
                },
            ],
            "page_furniture": [
                {
                    "pattern": r"(?m)^SYNTHETIC RUNNING HEAD[ \t]*\r?\n?",
                    "reason": "synthetic running head repeated by the test edition",
                }
            ],
        },
        "architecture": {
            "rationale": "The synthetic scope exists to exercise every deterministic source mechanism.",
            "minimums": {
                "case_dossiers": 0,
                "concept_notes": 0,
                "section_notes": 0,
                "claims": 0,
                "motifs": 0,
            },
            "nullable_rationales": {},
            "required_note_types": list(REQUIRED_NOTE_TYPES),
        },
        "output": {"format": "Open Knowledge Format", "okf_version": "0.2"},
    }


class OfflinePipelineTests(unittest.TestCase):
    def test_extract_lock_stage_seal_verify_and_query(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pdf_path = root / "source.pdf"
            extraction_root = root / "extracted"
            profile_path = root / "profile.json"
            lock_path = root / "profile.lock.json"
            _write_synthetic_pdf(pdf_path)
            result = extract(
                ExtractionOptions(
                    input_pdf=pdf_path,
                    output_dir=extraction_root,
                    render_dpi=72,
                    ocr_mode="none",
                    ocr_image_fallback=False,
                    save_svg=False,
                    poppler_mode="never",
                )
            )
            self.assertEqual(result["status"], "passed")
            extraction_manifest = json.loads(
                (extraction_root / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(extraction_manifest["source"]["path"], "source.pdf")
            self.assertEqual(extraction_manifest["output"], ".")

            profile_path.write_text(
                json.dumps(_profile_value(), indent=2) + "\n", encoding="utf-8"
            )
            profile = load_profile(profile_path)

            bad_value = copy.deepcopy(_profile_value())
            bad_value["scope"]["page_furniture"][0]["pattern"] = "NEVER PRESENT"
            bad_path = root / "bad-profile.json"
            bad_path.write_text(json.dumps(bad_value) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "matched nothing"):
                source.build_profile_lock(load_profile(bad_path))

            missing_boundary = copy.deepcopy(_profile_value())
            missing_boundary["scope"]["boundaries"][0]["marker"] = "ABSENT MARKER"
            missing_boundary_path = root / "missing-boundary-profile.json"
            missing_boundary_path.write_text(
                json.dumps(missing_boundary) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "boundary marker"):
                source.build_profile_lock(load_profile(missing_boundary_path))

            lock = source.build_profile_lock(profile)
            exclusive_json(lock_path, lock)
            lock = load_profile_lock(lock_path)
            self.assertEqual(lock["scope"]["apparatus_entries"], [1, 2])

            mismatched_value = copy.deepcopy(_profile_value())
            mismatched_value["scope"]["label"] = "a different synthetic selection"
            mismatched_path = root / "mismatched-profile.json"
            mismatched_path.write_text(
                json.dumps(mismatched_value, indent=2) + "\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "authored profile"):
                source.verify_profile_lock(load_profile(mismatched_path), lock)

            native_page = extraction_root / "text/native-pages/page-0002.txt"
            original = native_page.read_bytes()
            native_page.write_bytes(original + b"changed\n")
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                source.verify_profile_lock(profile, lock)
            native_page.write_bytes(original)

            audit = AuditLog(root / "run-audit.jsonl", "synthetic-run")
            staged = source.stage_source(
                profile=profile,
                lock=lock,
                work_root=root / "work",
                audit=audit,
            )
            self.assertNotIn(
                "Outside introductory material",
                staged["pages"][1]["text"],
            )
            self.assertNotIn("OUTSIDE APPARATUS", staged["pages"][3]["text"])
            self.assertTrue(
                all("SYNTHETIC RUNNING HEAD" not in item["text"] for item in staged["pages"].values())
            )

            candidate = root / "candidate"
            runtime = profile.runtime(lock)
            prepare_wiki(
                wiki_dir=candidate,
                pages=staged["pages"],
                profile=runtime,
                audit=audit,
                run_id="synthetic-run",
                extraction_root=profile.extraction_root,
            )
            manifest = seal_wiki(
                wiki_dir=candidate,
                profile=profile,
                lock=lock,
                audit=audit,
                run_id="synthetic-run",
                source_manifest=staged["manifest"],
            )
            self.assertEqual(manifest["status"], "sealed")
            self.assertGreater(len(verify_checksums(candidate)), 10)
            readme = candidate / "README.md"
            pristine_readme = readme.read_bytes()
            readme.write_bytes(pristine_readme + b"tampered\n")
            with self.assertRaisesRegex(ValueError, "does not match the sealed wiki"):
                verify_checksums(candidate)
            readme.write_bytes(pristine_readme)
            self.assertEqual(lint.lint(candidate), [])
            self.assertEqual(okf.n_errors_of(okf.check_directory(candidate)["issues"]), 0)
            quotation_report = verify_quotes(candidate, 40)
            self.assertEqual(quotation_report["in_scope_but_not_on_a_cited_page"], [])
            self.assertEqual(quotation_report["not_found_in_scope"], [])
            results = query_vault(candidate, "blue triangle", 5)
            self.assertTrue(results)
            self.assertTrue(any("page-000" in item["path"] for item in results))

            existing = root / "existing-output"
            existing.mkdir()
            with self.assertRaises(FileExistsError):
                publish_candidate(candidate, existing)
            published = publish_candidate(candidate, root / "published-wiki")
            self.assertFalse(candidate.exists())
            self.assertGreater(len(verify_checksums(published)), 10)


if __name__ == "__main__":
    unittest.main()
