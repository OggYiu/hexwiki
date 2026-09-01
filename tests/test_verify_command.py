from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hexwiki.commands import verify as verify_command
from hexwiki.tools.quotes import normalise, supports


class QuotationMatchingTests(unittest.TestCase):
    def test_latin_fallback_ignores_extractor_glyph_without_changing_words(
        self,
    ) -> None:
        quotation = normalise(
            "The measured signal continued across the boundary and remained stable"
        )
        gateway = normalise(
            "The measured signal\u14fccontinued across the boundary and remained stable"
        )

        self.assertTrue(supports(quotation, gateway))

    def test_latin_fallback_does_not_accept_changed_word(self) -> None:
        quotation = normalise(
            "The measured signal remained authorized throughout the recorded interval"
        )
        gateway = normalise(
            "The measured signal remained unauthorized throughout the recorded interval"
        )

        self.assertFalse(supports(quotation, gateway))

    def test_latin_fallback_does_not_override_non_latin_text(self) -> None:
        quotation = normalise(
            "\u6e2c\u5b9a\u3055\u308c\u305f\u4fe1\u53f7\u306f\u5883\u754c\u3092\u8d8a\u3048\u3066\u5b89\u5b9a\u3057\u3066\u3044\u305f"
        )
        gateway = normalise(
            "\u6e2c\u5b9a\u3055\u308c\u305f\u4fe1\u53f7\u306f\u5883\u754c\u3092\u8d8a\u3048\u3066\u5909\u5316\u3057\u3066\u3044\u305f"
        )

        self.assertFalse(supports(quotation, gateway))


class VerifyCommandTests(unittest.TestCase):
    def run_command(self, quotation_report: dict) -> tuple[int, dict]:
        with tempfile.TemporaryDirectory() as tmp:
            wiki = Path(tmp) / "wiki"
            wiki.mkdir()
            (wiki / "manifest.json").write_text(
                json.dumps({"status": "sealed"}), encoding="utf-8"
            )
            args = argparse.Namespace(
                wiki=wiki,
                min_length=40,
                as_json=True,
            )
            stdout = io.StringIO()
            with (
                patch.object(
                    verify_command, "verify_checksums", return_value=["one.md"]
                ),
                patch.object(verify_command, "verify", return_value=quotation_report),
                redirect_stdout(stdout),
            ):
                exit_code = verify_command.run(args)
            return exit_code, json.loads(stdout.getvalue())

    @staticmethod
    def report(*, checked: int, wrong_page: int = 0, unsupported: int = 0) -> dict:
        return {
            "wiki": "synthetic-wiki",
            "semantic_notes": 1,
            "quotations_checked": checked,
            "supported_by_cited_page": checked - wrong_page - unsupported,
            "in_scope_but_not_on_a_cited_page": [{}] * wrong_page,
            "not_found_in_scope": [{}] * unsupported,
            "support_rate": 1.0 if checked else None,
        }

    def test_findings_are_reported_without_overriding_sealed_integrity(self) -> None:
        exit_code, output = self.run_command(
            self.report(checked=3, wrong_page=1, unsupported=1)
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(output["status"], "passed")
        self.assertEqual(output["quotation_status"], "findings")
        self.assertEqual(len(output["quotations"]["not_found_in_scope"]), 1)

    def test_clear_quotation_report_is_clear(self) -> None:
        exit_code, output = self.run_command(self.report(checked=2))

        self.assertEqual(exit_code, 0)
        self.assertEqual(output["status"], "passed")
        self.assertEqual(output["quotation_status"], "clear")

    def test_zero_checked_quotations_remains_inconclusive(self) -> None:
        exit_code, output = self.run_command(self.report(checked=0))

        self.assertEqual(exit_code, 1)
        self.assertEqual(output["status"], "inconclusive")
        self.assertEqual(output["quotation_status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
