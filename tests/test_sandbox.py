"""Filesystem isolation and bounded-stage regressions."""

from __future__ import annotations

import os
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
    _stage_prompt,
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


class ExtendedLengthPolicy(SandboxPolicy):
    """Reproduce ``Path.resolve`` handing back a ``\\\\?\\``-prefixed path.

    Whether Windows returns the extended-length spelling is not stable between
    calls, so this forces the case that a live run hit only intermittently.
    """

    def resolve(self, key: str) -> Path:
        resolved = super().resolve(key)
        return Path(self._LONG_PATH_PREFIX + os.fspath(resolved))


class RespelledRootPolicy(SandboxPolicy):
    """Reproduce a root and its children being spelled differently.

    A hosted Windows runner sets TEMP to its 8.3 short form, so the sandbox root
    arrives with a truncated ``NAME~1`` component while a resolved child comes
    back with the long name. This forces the same divergence portably.
    """

    def resolve(self, key: str) -> Path:
        resolved = super().resolve(key)
        return Path(os.fspath(resolved).replace("candidate", "CANDID~1", 1))


class SandboxTests(unittest.TestCase):
    def test_relative_survives_a_root_spelled_differently_from_its_children(self) -> None:
        """One location, two spellings, must not read as an escape.

        ``relative`` is rebuilt from the caller's key rather than by subtracting
        the root, so no spelling difference can make it raise.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            (root / "concepts").mkdir(parents=True)
            policy = RespelledRootPolicy(root)

            self.assertEqual(
                policy.relative("concepts/note.md"), Path("concepts") / "note.md"
            )
            self.assertFalse(policy.is_immutable("concepts/note.md"))
            self.assertTrue(policy.is_immutable("sources/page.md"))
            self.assertTrue(policy.is_immutable("WIKI_GUIDE.md"))
            self.assertEqual(policy.relative("/"), Path("."))

    def test_plain_strips_only_the_extended_length_prefix(self) -> None:
        stripped = {
            "\\\\?\\C:\\wiki\\notes\\item.md": "C:\\wiki\\notes\\item.md",
            "\\\\?\\UNC\\server\\share\\item.md": "\\\\server\\share\\item.md",
        }
        for given, expected in stripped.items():
            with self.subTest(given=given):
                self.assertEqual(os.fspath(SandboxPolicy._plain(Path(given))), expected)
        for unchanged in ("C:\\wiki\\notes\\item.md", "/wiki/notes/item.md", "notes/item.md"):
            with self.subTest(unchanged=unchanged):
                given = Path(unchanged)
                self.assertEqual(SandboxPolicy._plain(given), given)

    def test_extended_length_resolution_stays_relative_to_its_root(self) -> None:
        """A prefixed candidate under an unprefixed root is contained, not an escape.

        Containment compares canonical forms and passes; ``relative_to`` then
        compared the raw spellings and raised a bare ``ValueError``, which escaped
        the sandbox and killed a whole compilation run.
        """
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            (root / "concepts").mkdir(parents=True)
            policy = ExtendedLengthPolicy(root)

            self.assertEqual(
                policy.relative("concepts/observed-or-imagined-displays.md"),
                Path("concepts") / "observed-or-imagined-displays.md",
            )
            self.assertFalse(policy.is_immutable("concepts/note.md"))
            self.assertTrue(policy.is_immutable("sources/source.md"))
            self.assertTrue(policy.is_immutable("WIKI_GUIDE.md"))

            # The unprefixed policy must agree with the prefixed one.
            plain = SandboxPolicy(root)
            self.assertEqual(
                plain.relative("concepts/observed-or-imagined-displays.md"),
                policy.relative("concepts/observed-or-imagined-displays.md"),
            )
            self.assertFalse(os.fspath(plain.resolve("concepts/note.md")).startswith("\\\\?\\"))

    def test_relative_raises_sandbox_violation_rather_than_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            root.mkdir()
            policy = SandboxPolicy(root)
            with self.assertRaises(SandboxViolation):
                policy.relative("../outside/escaped.md")

    def test_refused_path_is_reported_and_does_not_abort_the_run(self) -> None:
        """One bad write is a defect on one write, never a lost compilation."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            root.mkdir()
            recorder = TranscriptRecorder(root / "transcripts", "sandbox-test")
            backend, _ = _guarded_backend(root, recorder)

            written = backend.write("Z:\\outside\\escaped.md", "payload")
            self.assertTrue(getattr(written, "error", None))
            self.assertIn("Refused", str(written.error))

            edited = backend.edit("Z:\\outside\\escaped.md", "a", "b")
            self.assertTrue(getattr(edited, "error", None))
            self.assertIn("Refused", str(edited.error))

            uploaded = backend.upload_files([("Z:\\outside\\escaped.md", b"payload")])
            self.assertEqual([item.error for item in uploaded], ["refused_path"])

            self.assertFalse((Path(temporary) / "outside").exists())

    def test_policy_maps_virtual_root_without_weakening_containment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "candidate"
            root.mkdir()
            policy = SandboxPolicy(root)
            # Resolve independently of the policy: a temp directory can have a
            # second spelling (an 8.3 short name, a symlink), and asserting the
            # raw one makes this test fail on machines where it does.
            resolved = Path(os.path.realpath(root))

            self.assertEqual(policy.resolve("/"), resolved)
            self.assertEqual(policy.resolve("."), resolved)
            self.assertEqual(policy.resolve("/notes/item.md"), resolved / "notes" / "item.md")
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

        request = StageRequest("survey:outline", "survey", ("_plan/outline.json",))
        rendered = _stage_prompt(request)
        self.assertIn("`_plan/outline.json`", rendered)
        self.assertIn("Do not substitute a similarly named path", rendered)

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
