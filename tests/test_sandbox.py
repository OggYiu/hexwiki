"""Filesystem isolation and bounded-stage regressions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from hexwiki.engine.agent import (
    DeepAgentExecutor,
    SandboxPolicy,
    SandboxViolation,
    StageRequest,
    _guarded_backend,
)
from hexwiki.engine.audit import atomic_text
from hexwiki.engine.config import RuntimeConfig, RuntimeLimits, TranscriptRecorder


def _runtime(root: Path, *, attempts: int = 1) -> RuntimeConfig:
    return RuntimeConfig(
        base_url="https://offline.invalid/v1",
        model="synthetic-model",
        api_key="synthetic-key",
        config_dir=root / "config",
        runs_dir=root / "runs",
        observability_enabled=False,
        limits=RuntimeLimits(
            stage_attempts=attempts,
            stage_retry_seconds=tuple(0 for _ in range(max(0, attempts - 1))),
        ),
    )


class StubDeepAgentExecutor(DeepAgentExecutor):
    def __init__(self, *, outcomes: list[Any], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.outcomes = list(outcomes)
        self.calls = 0

    def _once(self, request: StageRequest) -> str:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            outcome(request)
        return "synthetic completion"


class SandboxTests(unittest.TestCase):
    def test_policy_maps_virtual_root_without_weakening_containment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            root.mkdir()
            policy = SandboxPolicy(root)

            self.assertEqual(policy.resolve("/"), root.resolve())
            self.assertEqual(policy.resolve("."), root.resolve())
            self.assertEqual(policy.resolve("/notes/item.md"), root / "notes" / "item.md")
            with self.assertRaises(SandboxViolation):
                policy.resolve("/../outside/escaped.md")

    def test_policy_rejects_traversal_and_resolved_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            root = workspace / "candidate"
            outside = workspace / "outside"
            root.mkdir()
            outside.mkdir()
            policy = SandboxPolicy(root)
            with self.assertRaises(SandboxViolation):
                policy.resolve("../outside/escaped.md")
            with self.assertRaises(SandboxViolation):
                policy.resolve("notes/../../outside/escaped.md")
            with self.assertRaisesRegex(SandboxViolation, "drive-qualified"):
                policy.resolve("Z:\\outside\\escaped.md")

            link = root / "linked-outside"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            with self.assertRaisesRegex(SandboxViolation, "outside"):
                policy.resolve("linked-outside/escaped.md")
            self.assertFalse((outside / "escaped.md").exists())

    def test_guarded_backend_enforces_mutability_and_delete_scope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            root.mkdir()
            for directory in ("_ingest", "sources", "reference"):
                (root / directory).mkdir()
            atomic_text(root / "_ingest" / "source.md", "immutable\n")
            atomic_text(root / "sources" / "source.md", "immutable\n")
            atomic_text(root / "reference" / "source.md", "immutable\n")
            atomic_text(root / "WIKI_GUIDE.md", "immutable\n")
            recorder = TranscriptRecorder(root / "transcripts", "sandbox-test")
            backend, delete_tool = _guarded_backend(root, recorder)

            self.assertIsNotNone(backend.ls("/"))
            allowed = backend.write("notes/allowed.md", "allowed")
            self.assertFalse(getattr(allowed, "error", None))
            self.assertEqual((root / "notes" / "allowed.md").read_text(), "allowed")
            for relative in (
                "_ingest/source.md",
                "sources/source.md",
                "reference/source.md",
                "WIKI_GUIDE.md",
            ):
                refused = backend.write(relative, "changed")
                self.assertTrue(getattr(refused, "error", None), relative)
                self.assertEqual((root / relative).read_text(), "immutable\n")

            traversal_refused = False
            try:
                result = backend.write("../escaped.md", "escaped")
                traversal_refused = bool(getattr(result, "error", None))
            except SandboxViolation:
                traversal_refused = True
            self.assertTrue(traversal_refused)
            self.assertFalse((root.parent / "escaped.md").exists())

            self.assertIn(
                "Refused",
                str(delete_tool.invoke({"file_path": "_ingest/source.md"})),
            )
            self.assertTrue((root / "_ingest" / "source.md").is_file())
            self.assertIn(
                "Deleted",
                str(delete_tool.invoke({"file_path": "notes/allowed.md"})),
            )
            self.assertFalse((root / "notes" / "allowed.md").exists())

    def test_stage_contract_caps_named_and_payload_artifacts(self) -> None:
        with self.assertRaisesRegex(ValueError, "maximum is two"):
            StageRequest("stage", "prompt", ("a.md", "b.md", "c.md"))
        with self.assertRaisesRegex(ValueError, "maximum is two"):
            StageRequest(
                "stage",
                "prompt",
                payload={"files": ["a.md", "b.md", "c.md"]},
            )
        with self.assertRaisesRegex(ValueError, "wiki-relative"):
            StageRequest("stage", "prompt", ("../outside.md",))
        with self.assertRaisesRegex(ValueError, "wiki-relative"):
            StageRequest("stage", "prompt", ("Z:\\outside\\note.md",))

    def test_executor_retries_only_retryable_errors_and_requires_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            atomic_text(root / "WIKI_GUIDE.md", "Synthetic guide.\n")
            recorder = TranscriptRecorder(root / "transcripts", "stage-test")
            sleeps: list[int] = []

            def write_output(_: StageRequest) -> None:
                atomic_text(root / "note.md", "created\n")

            executor = StubDeepAgentExecutor(
                outcomes=[TimeoutError("synthetic timeout"), write_output],
                wiki_dir=root,
                runtime=_runtime(root, attempts=2),
                recorder=recorder,
                recursion_limit=10,
                sleep=sleeps.append,
            )
            executor.execute(StageRequest("retry", "write", ("note.md",)))
            self.assertEqual(executor.calls, 2)
            self.assertEqual(sleeps, [0])

            unchanged = StubDeepAgentExecutor(
                outcomes=[None, None],
                wiki_dir=root,
                runtime=_runtime(root),
                recorder=recorder,
                recursion_limit=10,
            )
            with self.assertRaisesRegex(RuntimeError, "materially changing"):
                unchanged.execute(StageRequest("unchanged", "rewrite", ("note.md",)))
            unchanged.execute(
                StageRequest(
                    "audit-no-op",
                    "audit",
                    ("note.md",),
                    allow_unchanged=True,
                )
            )

            nonretryable = StubDeepAgentExecutor(
                outcomes=[ValueError("synthetic invalid result"), write_output],
                wiki_dir=root,
                runtime=_runtime(root, attempts=2),
                recorder=recorder,
                recursion_limit=10,
                sleep=sleeps.append,
            )
            with self.assertRaisesRegex(ValueError, "invalid result"):
                nonretryable.execute(StageRequest("invalid", "write"))
            self.assertEqual(nonretryable.calls, 1)


if __name__ == "__main__":
    unittest.main()
