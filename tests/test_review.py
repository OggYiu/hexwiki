"""Whole-page evidence packet and review-execution regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hexwiki.engine import review
from hexwiki.engine.audit import AuditLog, atomic_text


def _profile() -> dict[str, object]:
    return {
        "document_title": "Synthetic Review Source",
        "document_author": "Example Author",
        "scope_label": "synthetic pages 1-2",
        "primary_pages": [1, 2],
        "apparatus_pages": [],
    }


def _write_note(root: Path, relative: str, pages: list[int]) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    resources = "\n".join(
        f'  - {{ resource: "../sources/pdf-pages/page-{page:04d}.md" }}'
        for page in pages
    )
    links = "\n".join(
        f"- [PDF page {page}](../sources/pdf-pages/page-{page:04d}.md)"
        for page in pages
    )
    atomic_text(
        path,
        "---\n"
        "type: Concept\n"
        "title: Synthetic Review Note\n"
        "description: A sufficiently detailed synthetic description for review packet testing.\n"
        "tags: [synthetic, review]\n"
        "semantic_note: true\n"
        "status: draft\n"
        f"pdf_pages: {pages}\n"
        "sources:\n"
        f"{resources}\n"
        "---\n\n"
        "# Synthetic Review Note\n\n"
        "The note makes a bounded synthetic claim.\n\n"
        "## Sources\n\n"
        f"{links}\n",
    )


class ClearClient:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def ask(self, *, system: str, user: str, label: str) -> dict[str, object]:
        self.prompts.append(user)
        return {"findings": []}


class FailingClient:
    def __init__(self) -> None:
        self.calls = 0

    def ask(self, *, system: str, user: str, label: str) -> dict[str, object]:
        self.calls += 1
        raise TimeoutError("synthetic reviewer timeout")


class ReviewPacketTests(unittest.TestCase):
    def test_oversized_page_is_whole_and_other_evidence_is_explicitly_omitted(self) -> None:
        tail = "END-OF-OVERSIZED-SYNTHETIC-PAGE"
        group = [
            {
                "path": "concepts/review-note.md",
                "text": "synthetic note",
                "pages": [1, 2],
            }
        ]
        pages = {
            1: {"text": "x" * 100 + tail},
            2: {"text": "small second page"},
        }
        with patch.object(review, "PACKET_CHARACTER_BUDGET", 50):
            prompt, supplied = review.packet_prompt(group, pages, _profile())
        self.assertEqual(supplied, [1])
        self.assertIn(tail, prompt)
        self.assertIn(
            "In-scope cited pages intentionally omitted from this packet: [2]",
            prompt,
        )
        self.assertNotIn("--- PDF page 2 ---", prompt)

    def test_report_marks_never_supplied_pages_and_never_claims_partial_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wiki = root / "wiki"
            _write_note(wiki, "concepts/review-note.md", [1, 2])
            client = ClearClient()
            audit = AuditLog(root / "actions.jsonl", "review-test")
            pages = {
                1: {"text": "x" * 100 + "WHOLE-PAGE-TAIL"},
                2: {"text": "small second page"},
            }
            with patch.object(review, "PACKET_CHARACTER_BUDGET", 50):
                report = review.run_independent_review(
                    wiki_dir=wiki,
                    pages=pages,
                    profile=_profile(),
                    audit=audit,
                    client=client,
                    attempts=1,
                    retry_seconds=(),
                    sleep=lambda _: None,
                )
            self.assertEqual(report["execution_status"], "passed")
            self.assertEqual(report["coverage"], "partial")
            self.assertEqual(report["pages_cited_but_never_supplied_this_round"], [2])
            self.assertEqual(report["packets"][0]["pages_shown_in_part"], [])
            self.assertIn("WHOLE-PAGE-TAIL", client.prompts[0])

    def test_exhausted_reviewer_attempts_are_an_execution_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wiki = root / "wiki"
            _write_note(wiki, "concepts/review-note.md", [1])
            client = FailingClient()
            report = review.run_independent_review(
                wiki_dir=wiki,
                pages={1: {"text": "whole synthetic page"}},
                profile=_profile(),
                audit=AuditLog(root / "actions.jsonl", "review-failure-test"),
                client=client,
                attempts=2,
                retry_seconds=(0,),
                sleep=lambda _: None,
            )
            self.assertEqual(client.calls, 2)
            self.assertEqual(report["execution_status"], "failed")
            self.assertEqual(report["finding_status"], "findings")
            self.assertEqual(report["coverage"], "partial")
            self.assertIn("TimeoutError", report["packets"][0]["error"])


if __name__ == "__main__":
    unittest.main()
