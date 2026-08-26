"""Network-free integration tests for the guarded model orchestration."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

import pymupdf as fitz

from hexwiki.engine import review, source
from hexwiki.engine.agent import StageRequest, run_survey
from hexwiki.engine.audit import atomic_text, exclusive_json, sha256_file
from hexwiki.engine.config import RuntimeConfig, RuntimeLimits, TranscriptRecorder
from hexwiki.engine.finalize import verify_checksums
from hexwiki.engine.lint import parse_frontmatter
from hexwiki.engine.profile import REQUIRED_NOTE_TYPES, load_profile
from hexwiki.engine.runtime import (
    ConfigurationFailure,
    PreflightFailure,
    RuntimeServices,
    ValidationFailure,
    WorkflowFailure,
    inspect_run,
    run_build,
    run_smoke,
)
from hexwiki.extraction.pdf import ExtractionOptions, extract


def _write_pdf(path: Path) -> None:
    document = fitz.open()
    texts = [
        (
            "SYNTHETIC HEADER\n"
            "Outside the selected text.\n"
            "BEGIN SCOPE\n"
            "The author presents a blue triangle as a bounded worked example.\n"
        ),
        (
            "SYNTHETIC HEADER\n"
            "The second page compares description with explanation and leaves the cause open.\n"
        ),
        (
            "SYNTHETIC HEADER\n"
            "1\nSynthetic Reference One.\n"
            "2\nSynthetic Reference Two.\n"
            "END SCOPE\n3\nOutside Reference.\n"
        ),
    ]
    for text in texts:
        page = document.new_page(width=612, height=792)
        page.insert_text((72, 72), text, fontsize=11)
    document.save(path)
    document.close()


def _profile() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "profile_id": "synthetic-runtime",
        "source": {"pdf": "source.pdf", "extraction": "extracted"},
        "document": {
            "id": "synthetic-document",
            "title": "Synthetic Runtime Document",
            "author": "Example Author",
        },
        "scope": {
            "id": "selected-scope",
            "title": "Selected Scope",
            "label": "two synthetic pages and references",
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
                    "marker": "BEGIN SCOPE",
                    "keep": "after",
                    "action": "keep-after-scope-start",
                    "note": "remove synthetic introductory material before the selected scope",
                },
                {
                    "page": 3,
                    "marker": "END SCOPE",
                    "keep": "before",
                    "action": "keep-before-scope-end",
                    "note": "remove synthetic references belonging to the following scope",
                },
            ],
            "page_furniture": [
                {
                    "pattern": r"(?m)^SYNTHETIC HEADER[ \t]*\r?\n?",
                    "reason": "remove the repeated synthetic running header from every page",
                }
            ],
        },
        "architecture": {
            "rationale": "The synthetic runtime scope has one section and enough units to exercise all stages.",
            "minimums": {
                "case_dossiers": 1,
                "concept_notes": 3,
                "section_notes": 1,
                "claims": 1,
                "motifs": 1,
            },
            "nullable_rationales": {},
            "required_note_types": list(REQUIRED_NOTE_TYPES),
        },
        "output": {"format": "Open Knowledge Format", "okf_version": "0.2"},
    }


def _inventory() -> dict[str, Any]:
    return {
        "chapter": {
            "number": 1,
            "slug": "01-selected-scope",
            "title": "Selected Scope",
            "organizing_question": "What can the synthetic example establish?",
            "argument_steps": [
                "Present the bounded example.",
                "Describe its observed feature.",
                "Compare description with explanation.",
                "Leave the causal question open.",
            ],
            "establishes": ["The example is described in scope."],
            "leaves_open": ["The example's cause is not established."],
        },
        "sections": [
            {
                "order": 1,
                "slug": "01-bounded-example",
                "title": "Bounded Example",
                "pdf_pages": [1, 2],
                "summary": "Introduces an example and separates description from explanation.",
            }
        ],
        "episodes": [
            {
                "slug": "blue-triangle-example",
                "title": "Blue Triangle Example",
                "pdf_pages": [1],
                "summary": "The author presents a blue triangle as a worked example.",
                "support": "The page supplies a description but no independent corroboration.",
                "section_order": 1,
            }
        ],
        "concepts": [
            {
                "slug": "bounded-description",
                "title": "Bounded Description",
                "kind": "substantive",
                "pdf_pages": [1],
                "summary": "Names the concrete feature in the example.",
            },
            {
                "slug": "description-versus-explanation",
                "title": "Description Versus Explanation",
                "kind": "methodological",
                "pdf_pages": [2],
                "summary": "Separates recording a feature from explaining its cause.",
            },
            {
                "slug": "open-causal-status",
                "title": "Open Causal Status",
                "kind": "epistemic",
                "pdf_pages": [2],
                "summary": "Keeps the cause unresolved within the selected evidence.",
            },
        ],
        "people": [
            {
                "name": "Example Author",
                "group": "author",
                "pdf_pages": [1, 2],
                "role": "presents and interprets the worked example",
                "caution": "the selected pages do not supply outside corroboration",
            }
        ],
        "claims": [
            {
                "claim": "Description alone does not settle explanation.",
                "owner": "Example Author",
                "evidence": "The second page states the distinction.",
                "limit": "No causal test is supplied in the selected scope.",
            }
        ],
        "motifs": ["description kept distinct from explanation"],
    }


class FakeStageExecutor:
    def __init__(
        self,
        *,
        wiki_dir: Path,
        recorder: TranscriptRecorder,
        fail_immediately: bool = False,
        **_: Any,
    ) -> None:
        self.wiki_dir = Path(wiki_dir).resolve()
        self.recorder = recorder
        self.executed_labels: list[str] = []
        self.fail_immediately = fail_immediately
        self.skipped_concept = False
        self.cycle_written = False
        self.broken_link_written = False

    def _write_json(self, relative: str, value: Any) -> None:
        path = self.wiki_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    @staticmethod
    def _title_from_path(relative: str) -> str:
        return Path(relative).stem.replace("-", " ").title()

    @staticmethod
    def _note_type(relative: str) -> str:
        if relative == "overview.md":
            return "Overview"
        if relative == "reading-guide.md":
            return "Reading Guide"
        if relative == "synthesis/open-questions.md":
            return "Open Question"
        return {
            "chapters": "Chapter",
            "sections": "Section",
            "cases": "Case Dossier",
            "concepts": "Concept",
            "people": "Person",
            "synthesis": "Synthesis",
        }[Path(relative).parts[0]]

    def _write_note(
        self,
        relative: str,
        *,
        title: str | None = None,
        note_type: str | None = None,
        pages: list[int] | None = None,
        kind: str | None = None,
    ) -> None:
        path = self.wiki_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        title = title or self._title_from_path(relative)
        note_type = note_type or self._note_type(relative)
        pages = sorted(set(pages or [1]))
        resources = []
        body_links = []
        for page in pages:
            gateway = self.wiki_dir / "sources" / "pdf-pages" / f"page-{page:04d}.md"
            link = os.path.relpath(gateway, path.parent).replace("\\", "/")
            resources.append(
                f'  - {{ resource: "{link}", locator: "PDF page {page}" }}'
            )
            body_links.append(f"- [PDF page {page}]({link})")
        lines = [
            "---",
            f"type: {note_type}",
            f"title: {json.dumps(title)}",
            "description: \"A bounded synthetic source note used to test the complete guarded runtime.\"",
            f"tags: [synthetic, {Path(relative).parts[0] if len(Path(relative).parts) > 1 else 'root'}]",
            "semantic_note: true",
            "status: draft",
            f"pdf_pages: [{', '.join(str(page) for page in pages)}]",
            "sources:",
            *resources,
            'generated: { by: "hexwiki-test-stub", mode: "offline" }',
            "---",
            "",
            f"# {title}",
            "",
            "**This synthetic note records only what the selected test pages support.**",
            "",
            "## What the source says",
            "",
            "The selected source presents a bounded example and keeps description distinct from explanation.",
        ]
        if note_type == "Case Dossier":
            lines.extend(
                [
                    "",
                    "## Reported account",
                    "",
                    "The source presents the blue triangle as a synthetic worked example.",
                    "",
                    "## Source chain",
                    "",
                    "The account appears directly in the selected synthetic page.",
                    "",
                    "## Use in the argument",
                    "",
                    "It illustrates the difference between recording and explaining.",
                    "",
                    "## Evidence limits",
                    "",
                    "No independent witness, measurement, or external corroboration is supplied.",
                ]
            )
        if note_type == "Concept":
            case = self.wiki_dir / "cases" / "blue-triangle-example.md"
            case_link = os.path.relpath(case, path.parent).replace("\\", "/")
            lines.extend(
                [
                    "",
                    "## Instances in scope",
                    "",
                    f"- [Blue Triangle Example]({case_link}) — the bounded instance.",
                    "",
                    (
                        "## Evidence status"
                        if kind == "substantive"
                        else "## What this licenses and what it does not"
                    ),
                    "",
                    "Supports: the selected page describes the example.",
                    "",
                    "Does not support: the selected page does not establish a unique cause.",
                    "",
                    "Controls absent in scope: no comparison sample or causal test is supplied.",
                ]
            )
        lines.extend(
            [
                "",
                "## Connections",
                "",
                "The navigation and synthesis notes connect this item to the wider scope.",
                "",
                "## Sources",
                "",
                *body_links,
                "",
            ]
        )
        atomic_text(path, "\n".join(lines))

    def _write_folder_index(self, folder: str) -> None:
        directory = self.wiki_dir / folder
        directory.mkdir(exist_ok=True)
        lines = [f"# {folder.title()}", ""]
        for path in sorted(directory.glob("*.md")):
            if path.name == "index.md":
                continue
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            title = metadata.get("title", path.stem)
            lines.append(f"- [{title}]({path.name}) — synthetic test entry")
        atomic_text(directory / "index.md", "\n".join(lines) + "\n")

    def _write_root_index(self) -> None:
        lines = [
            "This synthetic source-bounded wiki is an unverified draft used for testing.",
            "",
            "## Notes",
            "",
        ]
        for path in sorted(self.wiki_dir.rglob("*.md")):
            relative = path.relative_to(self.wiki_dir)
            if path.name == "index.md" or relative.parts[0] in {
                "_ingest",
                "_plan",
                "_schema",
                "reports",
                "audit",
            }:
                continue
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            if metadata.get("semantic_note") is True:
                lines.append(
                    f"- [{metadata.get('title', path.stem)}]({relative.as_posix()}) — synthetic entry"
                )
        lines.extend(
            [
                "- [Source page gateways](sources/pdf-pages/index.md) — canonical evidence",
                "",
            ]
        )
        atomic_text(self.wiki_dir / "index.md", "\n".join(lines))

    def _write_cycle(self) -> None:
        notes = review.semantic_notes(self.wiki_dir)
        if len(notes) < 2:
            return
        for index, path in enumerate(notes):
            target = notes[(index + 1) % len(notes)]
            link = os.path.relpath(target, path.parent).replace("\\", "/")
            text = path.read_text(encoding="utf-8")
            marker = f"\nCycle link: [next synthetic note]({link}).\n"
            if marker not in text:
                atomic_text(path, text.rstrip() + "\n" + marker)
        self.cycle_written = True

    def _write_from_item(self, relative: str, item: dict[str, Any], kind: str) -> None:
        note_type = self._note_type(relative)
        self._write_note(
            relative,
            title=item.get("title") or item.get("name"),
            note_type=note_type,
            pages=item.get("pdf_pages", [1]),
            kind=item.get("kind") if kind == "concepts" else None,
        )

    def execute(self, request: StageRequest) -> str:
        if self.fail_immediately:
            raise WorkflowFailure("synthetic stage executor failure")
        self.executed_labels.append(request.label)
        self.recorder.append(
            "stage-events",
            {"event": "stage-started", "stage": request.label, "payload": request.payload},
        )
        kind = request.payload.get("kind")
        inventory = _inventory()
        if kind == "outline":
            self._write_json(
                "_plan/outline.json",
                {"chapter": inventory["chapter"], "sections": inventory["sections"]},
            )
        elif kind == "concepts" and request.label.startswith("survey:"):
            self._write_json("_plan/concepts.json", {"concepts": inventory["concepts"]})
        elif kind == "roster":
            self._write_json(
                "_plan/roster.json",
                {key: inventory[key] for key in ("people", "claims", "motifs")},
            )
        elif kind == "episodes":
            episodes = inventory["episodes"] if request.payload["part"] == 1 else []
            self._write_json(
                f"_plan/episodes-{request.payload['part']}.json", {"episodes": episodes}
            )
        elif kind == "episode-audit":
            pass
        elif kind in {"chapter", "section", "author"}:
            self._write_from_item(request.expected_paths[0], request.payload["item"], kind)
        elif kind in {"cases", "concepts"}:
            for relative, item in zip(request.expected_paths, request.payload["items"]):
                if kind == "concepts" and not self.skipped_concept:
                    self.skipped_concept = True
                    continue
                self._write_from_item(relative, item, kind)
        elif kind == "people-roster":
            pages = sorted(
                {page for item in request.payload["items"] for page in item["pdf_pages"]}
            )
            self._write_note(
                request.expected_paths[0],
                title=request.payload["group"].replace("-", " ").title(),
                pages=pages,
            )
        elif kind == "synthesis":
            self._write_note(
                request.expected_paths[0],
                note_type=request.payload["note_type"],
                pages=[1, 2],
            )
        elif kind in {"overview", "reading-guide"}:
            self._write_note(request.expected_paths[0], pages=[1, 2])
        elif kind == "folder-indexes":
            for folder in request.payload["folders"]:
                self._write_folder_index(folder)
        elif kind == "root-index":
            self._write_root_index()
        elif kind == "completeness":
            for relative in request.payload["missing"]:
                matched = next(
                    (
                        item
                        for item in inventory["concepts"]
                        if relative == f"concepts/{item['slug']}.md"
                    ),
                    None,
                )
                if matched:
                    self._write_from_item(relative, matched, "concepts")
                else:
                    self._write_note(relative)
            for folder in ("chapters", "sections", "cases", "concepts", "people", "synthesis"):
                self._write_folder_index(folder)
            self._write_root_index()
        elif kind == "crosslink":
            self._write_cycle()
            if not self.broken_link_written:
                target = self.wiki_dir / request.payload["orphans"][0]
                atomic_text(
                    target,
                    target.read_text(encoding="utf-8").rstrip()
                    + "\nBroken test link: [missing](missing-note.md).\n",
                )
                self.broken_link_written = True
        elif kind == "lint-repair":
            for relative in request.payload["files"]:
                path = self.wiki_dir / relative
                if path.is_file():
                    text = path.read_text(encoding="utf-8").replace(
                        "\nBroken test link: [missing](missing-note.md).", ""
                    )
                    atomic_text(path, text)
            self._write_cycle()
            self._write_root_index()
        elif kind == "review-repair":
            for relative in request.payload["files"]:
                path = self.wiki_dir / relative
                if path.is_file():
                    atomic_text(
                        path,
                        path.read_text(encoding="utf-8").rstrip()
                        + "\n\n<!-- independently reviewed synthetic repair -->\n",
                    )
        else:
            raise AssertionError(f"unhandled fake stage kind: {kind!r}")
        self.recorder.append(
            "stage-events",
            {"event": "stage-completed", "stage": request.label},
        )
        return f"completed {request.label}"


class FakeReviewer:
    def __init__(self, *, persistent_findings: bool = False) -> None:
        self.finding_sent = False
        self.persistent_findings = persistent_findings

    def ask(self, *, system: str, user: str, label: str) -> dict[str, Any]:
        if label.startswith("release-review"):
            return {
                "status": "blocked" if self.persistent_findings else "clear",
                "rationale": "Synthetic release evidence was evaluated by the fake reviewer.",
                "concerns": ["synthetic persistent finding"] if self.persistent_findings else [],
            }
        paths = re.findall(r"--- ([^\s]+\.md) \(cites", user)
        mutable = [
            path
            for path in paths
            if not path.startswith(("sources/", "reference/"))
        ]
        if mutable and (self.persistent_findings or not self.finding_sent):
            self.finding_sent = True
            return {
                "findings": [
                    {
                        "note": mutable[0],
                        "kind": "unsupported-claim",
                        "detail": "Exercise the repair path in the synthetic runtime.",
                        "quote": "synthetic review target",
                    }
                ]
            }
        return {"findings": []}


class SurveyCorrectionTests(unittest.TestCase):
    def test_duplicate_episode_correction_targets_episodes_and_accepts_valid_noop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            wiki_dir = Path(temporary)
            plan_dir = wiki_dir / "_plan"
            requests: list[StageRequest] = []

            class DuplicateEpisodeExecutor:
                def execute(self, request: StageRequest) -> str:
                    requests.append(request)
                    inventory = _inventory()
                    kind = request.payload["kind"]
                    relative = request.expected_paths[0]
                    path = wiki_dir / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    existed = path.is_file()

                    if kind == "outline":
                        value = {
                            "chapter": inventory["chapter"],
                            "sections": inventory["sections"],
                        }
                    elif kind == "concepts":
                        value = {"concepts": inventory["concepts"]}
                    elif kind == "roster":
                        value = {
                            key: inventory[key]
                            for key in ("people", "claims", "motifs")
                        }
                    elif kind == "episodes":
                        part = request.payload["part"]
                        if existed:
                            if not request.allow_unchanged:
                                raise AssertionError("correction must permit a valid no-op")
                            if part == 1:
                                return "kept the valid first episode file"
                            value = {"episodes": []}
                        else:
                            episode = dict(inventory["episodes"][0])
                            episode["pdf_pages"] = [part]
                            value = {"episodes": [episode]}
                    elif kind == "episode-audit":
                        if not request.allow_unchanged:
                            raise AssertionError("episode audit must permit a no-op")
                        return "no missed episodes"
                    else:
                        raise AssertionError(f"unexpected survey kind: {kind}")

                    atomic_text(path, json.dumps(value, indent=2) + "\n")
                    return f"wrote {relative}"

            profile_path = wiki_dir / "profile.json"
            atomic_text(profile_path, json.dumps(_profile(), indent=2) + "\n")
            profile = load_profile(profile_path).runtime()
            result = run_survey(
                executor=DuplicateEpisodeExecutor(),
                raw_name="synthetic-source.md",
                plan_dir=plan_dir,
                profile=profile,
            )

            self.assertEqual(result["episodes"], _inventory()["episodes"])
            labels = [request.label for request in requests]
            self.assertEqual(labels.count("survey:outline"), 1)
            self.assertEqual(labels.count("survey:concepts"), 1)
            self.assertEqual(labels.count("survey:roster"), 1)
            self.assertEqual(labels.count("survey:episodes-1"), 2)
            self.assertEqual(labels.count("survey:episodes-2"), 2)
            corrected = [
                request
                for request in requests
                if request.label.startswith("survey:episodes-")
                and "CORRECTION." in request.prompt
            ]
            self.assertEqual(len(corrected), 2)
            self.assertTrue(all(request.allow_unchanged for request in corrected))


def _runtime(root: Path) -> RuntimeConfig:
    return RuntimeConfig(
        base_url="https://offline.invalid/v1",
        model="synthetic-model",
        api_key="synthetic-secret-not-persisted",
        config_dir=root / "config",
        runs_dir=root / "runs",
        observability_enabled=False,
        limits=RuntimeLimits(
            smoke_max_seconds=120,
            build_max_seconds=120,
            child_margin_seconds=5,
            heartbeat_seconds=1,
            smoke_fresh_hours=24,
            smoke_recursion_limit=20,
            build_recursion_limit=20,
            stage_attempts=1,
            stage_retry_seconds=(),
            review_attempts=1,
            review_retry_seconds=(),
        ),
    )


def _preflight(**_: Any) -> dict[str, Any]:
    return {
        "status": "passed",
        "components": {
            "filesystem_sandbox": {
                "status": "passed",
                "immutable_write": "refused",
                "traversal_write": "refused",
            },
            "model_discovery": {"status": "passed"},
            "chat": {"status": "passed"},
            "tool_calling": {"status": "passed"},
            "observability": {"status": "disabled", "required": False},
        },
    }


def _services(
    *,
    fail_preflight: bool = False,
    fail_runtime: bool = False,
    persistent_findings: bool = False,
) -> RuntimeServices:
    def preflight(**kwargs: Any) -> dict[str, Any]:
        if fail_preflight:
            raise PreflightFailure("synthetic preflight failure")
        return _preflight(**kwargs)

    def executor_factory(**kwargs: Any) -> FakeStageExecutor:
        return FakeStageExecutor(fail_immediately=fail_runtime, **kwargs)

    def reviewer_factory(**_: Any) -> FakeReviewer:
        return FakeReviewer(persistent_findings=persistent_findings)

    return RuntimeServices(
        preflight=preflight,
        executor_factory=executor_factory,
        reviewer_factory=reviewer_factory,
        sleep=lambda _: None,
        provider_kind="stub",
    )


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        _write_pdf(self.root / "source.pdf")
        result = extract(
            ExtractionOptions(
                input_pdf=self.root / "source.pdf",
                output_dir=self.root / "extracted",
                render_dpi=72,
                ocr_mode="none",
                ocr_image_fallback=False,
                save_svg=False,
                poppler_mode="never",
            )
        )
        self.assertEqual(result["status"], "passed")
        self.profile_path = self.root / "profile.json"
        self.lock_path = self.root / "profile.lock.json"
        atomic_text(self.profile_path, json.dumps(_profile(), indent=2) + "\n")
        profile = load_profile(self.profile_path)
        exclusive_json(self.lock_path, source.build_profile_lock(profile))
        self.runtime = _runtime(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_stub_smoke_and_build_exercise_every_stage(self) -> None:
        smoke_dir = self.root / "smoke-run"
        smoke_report = run_smoke(
            profile_path=self.profile_path,
            lock_path=self.lock_path,
            run_dir=smoke_dir,
            runtime=self.runtime,
            services=_services(),
        )
        self.assertTrue(smoke_report.is_file())
        self.assertFalse((self.root / "published-before-build").exists())
        smoke_child = json.loads(
            (smoke_dir / "child-result.json").read_text(encoding="utf-8")
        )
        labels = smoke_child["stage_labels"]
        for prefix in (
            "survey:",
            "survey-audit:",
            "structure:",
            "cases:",
            "concepts:",
            "people:",
            "synthesis:",
            "navigation:",
            "completeness:",
            "crosslink:",
            "lint-repair:",
            "review-repair:",
        ):
            self.assertTrue(any(label.startswith(prefix) for label in labels), prefix)
        self.assertEqual(inspect_run(smoke_dir)["terminal"]["state"], "passed")
        self.assertGreaterEqual(len(smoke_child["independent_review"]["rounds"]), 2)
        self.assertEqual(
            smoke_child["independent_review"]["rounds"][0]["material_findings"],
            1,
        )

        build_dir = self.root / "build-run"
        output = self.root / "published-wiki"
        published = run_build(
            profile_path=self.profile_path,
            lock_path=self.lock_path,
            smoke_report_path=smoke_report,
            run_dir=build_dir,
            output=output,
            runtime=self.runtime,
            services=_services(),
        )
        self.assertEqual(published, output.resolve())
        self.assertTrue(output.is_dir())
        self.assertGreater(len(verify_checksums(output)), 20)
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["validation_level"], "model-reviewed")
        self.assertEqual(manifest["model_run"]["mode"], "build")
        before_status = {
            path.relative_to(build_dir).as_posix(): sha256_file(path)
            for path in build_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(inspect_run(build_dir)["review_index"]["status"], "passed")
        after_status = {
            path.relative_to(build_dir).as_posix(): sha256_file(path)
            for path in build_dir.rglob("*")
            if path.is_file()
        }
        self.assertEqual(after_status, before_status)
        self.assertFalse(any(output.parent.glob(f".{output.name}.*.candidate")))
        build_child = json.loads(
            (build_dir / "child-result.json").read_text(encoding="utf-8")
        )
        review_report = build_child["independent_review"]
        self.assertEqual(review_report["page_coverage"], "complete")
        self.assertEqual(review_report["coverage_across_rounds"], "complete")
        self.assertEqual(review_report["rounds"][0]["material_findings"], 1)
        self.assertEqual(review_report["rounds"][-1]["material_findings"], 0)
        self.assertEqual(review_report["pages_reviewed"], [1, 2, 3])
        self.assertNotEqual(
            review_report["pages_reviewed"],
            review_report["pages_reviewed_this_round"],
        )
        for path in review.semantic_notes(output):
            metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
            self.assertNotIn("verified", metadata, path)
            self.assertTrue(metadata["sources"], path)
            self.assertTrue(
                all(
                    isinstance(item, dict) and item.get("resource")
                    for item in metadata["sources"]
                ),
                path,
            )
        secret = self.runtime.api_key.encode("utf-8")
        for run_root in (smoke_dir, build_dir):
            for path in run_root.rglob("*"):
                if path.is_file():
                    self.assertNotIn(secret, path.read_bytes(), path)

    def test_terminal_failure_categories_are_recorded(self) -> None:
        preflight_dir = self.root / "preflight-failure"
        with self.assertRaises(PreflightFailure):
            run_smoke(
                profile_path=self.profile_path,
                lock_path=self.lock_path,
                run_dir=preflight_dir,
                runtime=self.runtime,
                services=_services(fail_preflight=True),
            )
        terminal = json.loads(
            (preflight_dir / "terminal.json").read_text(encoding="utf-8")
        )
        self.assertEqual((terminal["category"], terminal["exit_code"]), ("preflight", 3))

        runtime_dir = self.root / "runtime-failure"
        with self.assertRaises(WorkflowFailure):
            run_smoke(
                profile_path=self.profile_path,
                lock_path=self.lock_path,
                run_dir=runtime_dir,
                runtime=self.runtime,
                services=_services(fail_runtime=True),
            )
        terminal = json.loads((runtime_dir / "terminal.json").read_text(encoding="utf-8"))
        self.assertEqual((terminal["category"], terminal["exit_code"]), ("runtime", 4))

        valid_smoke = run_smoke(
            profile_path=self.profile_path,
            lock_path=self.lock_path,
            run_dir=self.root / "valid-smoke",
            runtime=self.runtime,
            services=_services(),
        )
        report_dir = self.root / "corrected-smoke"
        report_dir.mkdir()
        report = json.loads(valid_smoke.read_text(encoding="utf-8"))
        report["corrected"] = True
        corrected = report_dir / "smoke-report.json"
        atomic_text(corrected, json.dumps(report, indent=2, sort_keys=True) + "\n")
        digest = sha256_file(corrected)
        atomic_text(report_dir / "smoke-report.sha256", f"{digest}  smoke-report.json\n")
        run_record = json.loads((valid_smoke.parent / "run.json").read_text(encoding="utf-8"))
        atomic_text(report_dir / "run.json", json.dumps(run_record) + "\n")
        atomic_text(
            report_dir / "terminal.json",
            json.dumps(
                {
                    "state": "passed",
                    "kind": "smoke",
                    "smoke_report_sha256": digest,
                }
            )
            + "\n",
        )
        config_dir = self.root / "configuration-failure"
        with self.assertRaises(ConfigurationFailure):
            run_build(
                profile_path=self.profile_path,
                lock_path=self.lock_path,
                smoke_report_path=corrected,
                run_dir=config_dir,
                output=self.root / "never-published",
                runtime=self.runtime,
                services=_services(),
            )
        terminal = json.loads((config_dir / "terminal.json").read_text(encoding="utf-8"))
        self.assertEqual(
            (terminal["category"], terminal["exit_code"]), ("configuration", 2)
        )

        validation_dir = self.root / "validation-failure"
        with self.assertRaisesRegex(ValidationFailure, "model="):
            run_build(
                profile_path=self.profile_path,
                lock_path=self.lock_path,
                smoke_report_path=valid_smoke,
                run_dir=validation_dir,
                output=self.root / "blocked-output",
                runtime=self.runtime,
                services=_services(persistent_findings=True),
            )
        terminal = json.loads(
            (validation_dir / "terminal.json").read_text(encoding="utf-8")
        )
        self.assertEqual((terminal["category"], terminal["exit_code"]), ("validation", 5))
        self.assertFalse((self.root / "blocked-output").exists())
        retained = list(self.root.glob(".blocked-output.*.candidate"))
        self.assertEqual(len(retained), 1)
        self.assertTrue((retained[0] / "reports" / "independent-review.json").is_file())

        def write_variant(name: str, **changes: Any) -> Path:
            directory = self.root / name
            directory.mkdir()
            value = json.loads(valid_smoke.read_text(encoding="utf-8"))
            value.update(changes)
            target = directory / "smoke-report.json"
            atomic_text(target, json.dumps(value, indent=2, sort_keys=True) + "\n")
            checksum = sha256_file(target)
            atomic_text(
                directory / "smoke-report.sha256",
                f"{checksum}  smoke-report.json\n",
            )
            run_record = json.loads(
                (valid_smoke.parent / "run.json").read_text(encoding="utf-8")
            )
            atomic_text(directory / "run.json", json.dumps(run_record) + "\n")
            atomic_text(
                directory / "terminal.json",
                json.dumps(
                    {
                        "state": "passed",
                        "kind": "smoke",
                        "smoke_report_sha256": checksum,
                    }
                )
                + "\n",
            )
            return target

        variants = (
            (
                "expired-smoke",
                write_variant("expired-smoke-report", expires_at="2000-01-01T00:00:00+00:00"),
                "expired",
            ),
            (
                "failed-smoke",
                write_variant("failed-smoke-report", status="failed"),
                "failed",
            ),
            (
                "mismatched-smoke",
                write_variant("mismatched-smoke-report", binding_sha256="0" * 64),
                "disagree",
            ),
        )
        for run_name, report_path, message in variants:
            with self.subTest(report=run_name), self.assertRaisesRegex(
                ConfigurationFailure, message
            ):
                run_build(
                    profile_path=self.profile_path,
                    lock_path=self.lock_path,
                    smoke_report_path=report_path,
                    run_dir=self.root / run_name,
                    output=self.root / f"{run_name}-output",
                    runtime=self.runtime,
                    services=_services(fail_preflight=True),
                )
            self.assertFalse((self.root / f"{run_name}-output").exists())

        existing = self.root / "existing-output"
        existing.mkdir()
        with self.assertRaisesRegex(ConfigurationFailure, "already exists"):
            run_build(
                profile_path=self.profile_path,
                lock_path=self.lock_path,
                smoke_report_path=valid_smoke,
                run_dir=self.root / "existing-output-run",
                output=existing,
                runtime=self.runtime,
                services=_services(fail_preflight=True),
            )


if __name__ == "__main__":
    unittest.main()
