"""Whole-page evidence packet and review-execution regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hexwiki.engine import agent, review
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


class RepairExecutor:
    def __init__(self, wiki_dir: Path) -> None:
        self.wiki_dir = wiki_dir
        self.requests: list[agent.StageRequest] = []

    def execute(self, request: agent.StageRequest) -> str:
        self.requests.append(request)
        for relative in request.expected_paths:
            path = self.wiki_dir / relative
            atomic_text(
                path,
                path.read_text(encoding="utf-8").rstrip()
                + f"\n\n<!-- {request.label} -->\n",
            )
        return f"completed {request.label}"


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

    def test_cumulative_coverage_survives_narrow_rounds_until_fourth_round_clear(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wiki = root / "wiki"
            _write_note(wiki, "concepts/a.md", [1, 2])
            _write_note(wiki, "concepts/b.md", [1, 2])
            executor = RepairExecutor(wiki)

            def scripted_review(**kwargs: object) -> dict[str, object]:
                round_label = str(kwargs["round_label"])
                finding = {
                    "note": "concepts/a.md",
                    "kind": "wrong-locator",
                    "detail": "Synthetic finding for bounded round coverage.",
                    "quote": "synthetic review target",
                }
                if round_label == "1":
                    notes = ["concepts/a.md", "concepts/b.md"]
                    supplied = [1, 2]
                elif round_label == "2":
                    notes = ["concepts/a.md"]
                    supplied = [1]
                elif round_label == "3":
                    notes = ["concepts/a.md"]
                    supplied = [2]
                else:
                    notes = ["concepts/a.md"]
                    supplied = [1, 2]
                findings = [] if round_label == "4" else [finding]
                return {
                    "round": round_label,
                    "pages_reviewed_this_round": supplied,
                    "pages_cited_but_never_supplied_this_round": sorted(
                        {1, 2} - set(supplied)
                    ),
                    "execution_status": "passed",
                    "finding_status": "clear" if not findings else "findings",
                    "material_findings": len(findings),
                    "packets": [
                        {
                            "notes": notes,
                            "pages_supplied": supplied,
                            "pages_cited": [1, 2],
                            "findings": findings,
                        }
                    ],
                    "coverage": "complete" if supplied == [1, 2] else "partial",
                    "scope_of_round": (
                        "all notes" if round_label == "1" else "repaired notes only"
                    ),
                }

            runtime = SimpleNamespace(
                limits=SimpleNamespace(review_attempts=1, review_retry_seconds=())
            )
            with (
                patch.object(review, "load_pages_from_gateways", return_value={}),
                patch.object(review, "run_independent_review", side_effect=scripted_review),
                patch.object(agent, "repair_lint", return_value=[]),
            ):
                report = agent.review_and_repair(
                    executor=executor,
                    reviewer=object(),
                    wiki_dir=wiki,
                    profile={"primary_pages": [1, 2], "apparatus_pages": []},
                    audit=AuditLog(root / "actions.jsonl", "cumulative-review-test"),
                    runtime=runtime,
                    date="2026-01-01",
                    writer="test",
                    smoke=False,
                    sleep=lambda _: None,
                )

            self.assertEqual(len(report["rounds"]), 4)
            self.assertEqual(report["finding_status"], "clear")
            self.assertEqual(report["coverage_across_rounds"], "complete")
            self.assertEqual(report["page_coverage"], "complete")
            self.assertEqual(report["notes_pending_review"], [])
            self.assertEqual(
                [request.label for request in executor.requests],
                ["review-repair:1.1", "review-repair:2.1", "review-repair:3.1"],
            )


if __name__ == "__main__":
    unittest.main()
