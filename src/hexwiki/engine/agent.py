"""Plan-driven staged wiki compilation with a guarded filesystem boundary."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Protocol

from . import lint, plan, review
from .audit import AuditLog, atomic_json, atomic_text, sha256_file
from .config import (
    RuntimeConfig,
    TranscriptRecorder,
    flush_observability,
    langchain_callbacks,
    load_runtime_config,
)
from .profile import load_profile, load_profile_lock


NOTES_PER_WRITE_STAGE = 2
# Review rounds are bounded, but three is not enough margin when a narrow
# re-review discovers a fresh defect in a note that was just repaired.  The
# fourth production pass observed in practice needed one more repair and one
# final clean confirmation.  Later rounds remain cheap because they receive
# only the notes named by the preceding findings.
REVIEW_ROUNDS = 5
LINT_ROUNDS = 6
COMPLETENESS_ROUNDS = 3

SYSTEM_ROLE = """You are an automated source-bounded wiki compiler. Your filesystem
tools are rooted at one fresh candidate wiki. Never ask questions. Work only from the
immutable current-run source under `_ingest/`, the canonical gateways under
`sources/pdf-pages/`, the generated survey under `_plan/`, and notes in this candidate.

Standing rules:
- State only what the staged source supports. Never import outside knowledge.
- Attribute reported claims and keep report, source chain, author interpretation, and
  wiki inference visibly distinct.
- Every semantic note is an unverified draft with the exact OKF metadata and direct
  relative source-gateway links required by WIKI_GUIDE.md. Never add `verified`.
- Never write or edit `_ingest/`, `sources/`, `reference/`, or WIKI_GUIDE.md.
- Never create aliases, redirect notes, scripts, probes, or temporary files.
- Use delete_file only for a model-written duplicate or wrong-path file.
- Append to log.md; never rewrite its prior entries.
- Create or materially rewrite no more than the explicitly named one or two artifacts
  in a stage. Finish with a short summary.

=== WIKI GUIDE ===
"""


class SandboxViolation(ValueError):
    """A filesystem request escapes the candidate or targets immutable evidence."""


class SandboxPolicy:
    """OS-neutral containment and immutability rules shared by all file tools."""

    IMMUTABLE_ROOTS = {"_ingest", "sources", "reference"}

    _LONG_PATH_PREFIX = "\\\\?\\"
    _LONG_UNC_PREFIX = "\\\\?\\UNC\\"

    def __init__(self, root: Path) -> None:
        self.root = self._plain(Path(root).resolve(strict=False))

    @classmethod
    def _plain(cls, path: Path) -> Path:
        """Drop Windows' extended-length prefix without otherwise changing the path.

        ``Path.resolve`` may hand back a ``\\\\?\\``-prefixed path, and whether it
        does is not stable between two calls on sibling paths. An unprefixed root
        and a prefixed child then compare as unrelated even though one plainly
        contains the other, so ``relative_to`` raises ``ValueError`` immediately
        after containment has just proved the opposite.

        Containment already normalised the prefix away for its own comparison in
        ``_canonical``. The path that is *returned* has to carry the same
        normalisation, or every caller doing arithmetic on it inherits the bug --
        which is exactly how one note write killed a 35-minute run.
        """
        value = os.fspath(path)
        if value.startswith(cls._LONG_UNC_PREFIX):
            return Path("\\\\" + value[len(cls._LONG_UNC_PREFIX) :])
        if value.startswith(cls._LONG_PATH_PREFIX):
            return Path(value[len(cls._LONG_PATH_PREFIX) :])
        return path

    @classmethod
    def _canonical(cls, path: Path) -> str:
        value = os.path.normcase(os.path.abspath(os.fspath(path)))
        return value[4:] if value.startswith(cls._LONG_PATH_PREFIX) else value

    def resolve(self, key: str) -> Path:
        if not isinstance(key, str) or not key.strip() or "\x00" in key:
            raise SandboxViolation("file path must be a non-empty string")
        normalized = key.replace("\\", "/")
        if PureWindowsPath(normalized).drive:
            raise SandboxViolation("drive-qualified paths are not allowed")
        candidate_key = normalized[1:] if normalized.startswith("/") else normalized
        parts = [part for part in candidate_key.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts) or (
            parts and parts[0].startswith("~")
        ):
            raise SandboxViolation("path traversal is not allowed")
        if not parts:
            # DeepAgents' virtual filesystem presents ``/`` as the backend root.
            # Map root-only spellings to the candidate instead of treating them
            # as host-absolute paths; resolved containment still guards children.
            return self.root
        candidate = self._plain(self.root.joinpath(*parts).resolve(strict=False))
        root_name = self._canonical(self.root)
        candidate_name = self._canonical(candidate)
        try:
            common = os.path.commonpath([root_name, candidate_name])
        except ValueError as error:
            raise SandboxViolation("path is on a different filesystem root") from error
        if common != root_name:
            raise SandboxViolation("path is outside the candidate wiki")
        return candidate

    def relative(self, key: str) -> Path:
        # Normalise here as well as in ``resolve``: this method does path
        # arithmetic, so it must not trust the spelling it is handed.
        path = self._plain(self.resolve(key))
        try:
            return path.relative_to(self.root)
        except ValueError:
            # ``resolve`` has already proved containment against the canonical
            # forms, so reaching here means the two paths merely *spell* the same
            # location differently. Recompute from the canonical comparison
            # rather than letting a bare ValueError escape the sandbox: an
            # unhandled one propagates out of the tool node and takes the whole
            # compilation child with it.
            root_text = os.fspath(self.root)
            path_text = os.fspath(path)
            root_name = os.path.normcase(root_text)
            path_name = os.path.normcase(path_text)
            if path_name == root_name:
                return Path(".")
            if not path_name.startswith(root_name + os.sep):
                raise SandboxViolation("path is outside the candidate wiki") from None
            # normcase only changes case, never length, so slicing the
            # original-cased text by the root's length preserves real filenames.
            return Path(path_text[len(root_text) + 1 :])

    def is_immutable(self, key: str) -> bool:
        relative = self.relative(key)
        return (
            relative.name == "WIKI_GUIDE.md"
            or bool(relative.parts and relative.parts[0] in self.IMMUTABLE_ROOTS)
        )

    def require_mutable(self, key: str) -> Path:
        path = self.resolve(key)
        if self.is_immutable(key):
            raise SandboxViolation(f"immutable current-run evidence: {key}")
        return path

    def delete_file(self, key: str) -> Path:
        path = self.require_mutable(key)
        if not path.is_file():
            raise FileNotFoundError(key)
        path.unlink()
        return path


def _guarded_backend(wiki_dir: Path, recorder: TranscriptRecorder) -> tuple[Any, Any]:
    try:
        from deepagents.backends import FilesystemBackend
        from deepagents.backends.protocol import EditResult, FileUploadResponse, WriteResult
        from langchain_core.tools import tool
    except ImportError as error:
        raise RuntimeError(
            "model runtime is unavailable; install HexWiki with the 'model' extra"
        ) from error

    policy = SandboxPolicy(wiki_dir)

    class GuardedBackend(FilesystemBackend):
        def _resolve_path(self, key: str) -> Path:
            return policy.resolve(key)

        def write(self, file_path: str, content: str) -> Any:
            # A refused path is a defect on one write, never a reason to lose the
            # whole compilation: report it the same way an immutable target is
            # reported, so the model can correct the path and continue.
            try:
                immutable = policy.is_immutable(file_path)
            except SandboxViolation as error:
                return WriteResult(error=f"Refused: {error}")
            if immutable:
                return WriteResult(error=f"Immutable current-run evidence: {file_path}")
            return super().write(file_path, content)

        def edit(
            self,
            file_path: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ) -> Any:
            try:
                immutable = policy.is_immutable(file_path)
            except SandboxViolation as error:
                return EditResult(error=f"Refused: {error}")
            if immutable:
                return EditResult(error=f"Immutable current-run evidence: {file_path}")
            return super().edit(file_path, old_string, new_string, replace_all)

        def upload_files(self, files: list[tuple[str, bytes]]) -> list[Any]:
            responses: list[Any] = []
            for file_path, content in files:
                try:
                    immutable = policy.is_immutable(file_path)
                except SandboxViolation:
                    responses.append(
                        FileUploadResponse(path=file_path, error="refused_path")
                    )
                    continue
                if immutable:
                    responses.append(
                        FileUploadResponse(path=file_path, error="immutable_current_run_evidence")
                    )
                else:
                    responses.extend(super().upload_files([(file_path, content)]))
            return responses

    backend = GuardedBackend(root_dir=wiki_dir, virtual_mode=True)

    @tool("delete_file", parse_docstring=False)
    def delete_file(file_path: str) -> str:
        """Delete one mutable model-written file by its wiki-relative path."""
        try:
            removed = policy.delete_file(file_path)
        except (SandboxViolation, FileNotFoundError) as error:
            return f"Refused: {error}"
        recorder.append(
            "tool-events",
            {"event": "guarded-delete", "path": removed.relative_to(wiki_dir).as_posix()},
        )
        return f"Deleted {file_path}"

    return backend, delete_file


@dataclass(frozen=True)
class StageRequest:
    label: str
    prompt: str
    expected_paths: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    allow_unchanged: bool = False

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.prompt.strip():
            raise ValueError("a stage needs a label and prompt")
        if len(self.expected_paths) > NOTES_PER_WRITE_STAGE:
            raise ValueError(
                f"stage {self.label!r} names {len(self.expected_paths)} artifacts; maximum is two"
            )
        for path in self.expected_paths:
            normalized = path.replace("\\", "/")
            pure = PurePosixPath(normalized)
            if pure.is_absolute() or PureWindowsPath(path).drive or ".." in pure.parts:
                raise ValueError(f"stage output is not wiki-relative: {path}")
        for key in ("files", "folders", "missing", "orphans"):
            value = self.payload.get(key)
            if isinstance(value, (list, tuple, set, dict)) and len(value) > NOTES_PER_WRITE_STAGE:
                raise ValueError(
                    f"stage {self.label!r} payload names {len(value)} {key}; maximum is two"
                )


def _stage_prompt(request: StageRequest) -> str:
    if not request.expected_paths:
        return request.prompt
    paths = ", ".join(f"`{path}`" for path in request.expected_paths)
    unchanged = (
        " If the stage explicitly permits no change, leave those exact files unchanged."
        if request.allow_unchanged
        else " Each named artifact must be created or materially updated."
    )
    return (
        "ARTIFACT CONTRACT. The required artifact path(s) for this stage are exactly: "
        f"{paths}. Do not substitute a similarly named path.{unchanged}\n\n"
        + request.prompt
    )


class StageExecutor(Protocol):
    wiki_dir: Path

    def execute(self, request: StageRequest) -> str: ...


def _retryable(error: BaseException) -> bool:
    if isinstance(error, (PermissionError, FileNotFoundError, SandboxViolation, plan.PlanError)):
        return False
    name = type(error).__name__.casefold()
    markers = (
        "timeout",
        "connection",
        "ratelimit",
        "rate_limit",
        "internalserver",
        "serviceunavailable",
        "apierror",
    )
    return isinstance(error, (TimeoutError, ConnectionError)) or any(
        marker in name for marker in markers
    )


class DeepAgentExecutor:
    """A fresh DeepAgents context for every bounded model-facing stage."""

    def __init__(
        self,
        *,
        wiki_dir: Path,
        runtime: RuntimeConfig,
        recorder: TranscriptRecorder,
        recursion_limit: int,
        sleep: Any = time.sleep,
    ) -> None:
        self.wiki_dir = Path(wiki_dir).resolve()
        self.runtime = runtime
        self.recorder = recorder
        self.recursion_limit = recursion_limit
        self.sleep = sleep
        self.guide = (self.wiki_dir / "WIKI_GUIDE.md").read_text(encoding="utf-8")
        self.executed_labels: list[str] = []

    def _once(self, request: StageRequest) -> str:
        try:
            from deepagents import create_deep_agent
            from langchain_openai import ChatOpenAI
        except ImportError as error:
            raise RuntimeError(
                "model runtime is unavailable; install HexWiki with the 'model' extra"
            ) from error
        model = ChatOpenAI(
            base_url=self.runtime.base_url,
            api_key=self.runtime.api_key,
            model=self.runtime.model,
            timeout=900,
            max_retries=0,
            streaming=False,
        )
        backend, delete_tool = _guarded_backend(self.wiki_dir, self.recorder)
        agent = create_deep_agent(
            model=model,
            system_prompt=SYSTEM_ROLE + self.guide,
            backend=backend,
            tools=[delete_tool],
        )
        final = ""
        for chunk in agent.stream(
            {"messages": [{"role": "user", "content": _stage_prompt(request)}]},
            config={
                "recursion_limit": self.recursion_limit,
                "callbacks": langchain_callbacks(self.runtime, self.recorder),
            },
            stream_mode="updates",
        ):
            self.recorder.append(
                "stage-events",
                {"event": "agent-update", "stage": request.label, "update": chunk},
            )
            for update in (chunk or {}).values():
                messages = update.get("messages", []) if isinstance(update, dict) else []
                for message in messages if isinstance(messages, list) else []:
                    content = getattr(message, "content", None)
                    if isinstance(content, str) and content.strip():
                        final = content
        return final

    def execute(self, request: StageRequest) -> str:
        started = time.monotonic()
        before = {
            relative: sha256_file(self.wiki_dir / relative)
            if (self.wiki_dir / relative).is_file()
            else None
            for relative in request.expected_paths
        }
        self.recorder.append(
            "stage-events",
            {
                "event": "stage-started",
                "stage": request.label,
                "prompt": _stage_prompt(request),
                "expected_paths": request.expected_paths,
                "payload": request.payload,
            },
        )
        last: BaseException | None = None
        for attempt in range(1, self.runtime.limits.stage_attempts + 1):
            try:
                final = self._once(request)
                missing = [
                    relative
                    for relative in request.expected_paths
                    if not (self.wiki_dir / relative).is_file()
                ]
                if missing:
                    raise RuntimeError(
                        f"stage {request.label} returned without its named artifacts: {missing}"
                    )
                unchanged = [
                    relative
                    for relative, digest in before.items()
                    if digest is not None
                    and sha256_file(self.wiki_dir / relative) == digest
                ]
                if unchanged and not request.allow_unchanged:
                    raise RuntimeError(
                        f"stage {request.label} returned without materially changing "
                        f"its named artifacts: {unchanged}"
                    )
                self.recorder.append(
                    "stage-events",
                    {
                        "event": "stage-completed",
                        "stage": request.label,
                        "attempt": attempt,
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "final": final,
                    },
                )
                self.executed_labels.append(request.label)
                return final
            except BaseException as error:
                last = error
                self.recorder.append(
                    "stage-events",
                    {
                        "event": "stage-attempt-failed",
                        "stage": request.label,
                        "attempt": attempt,
                        "error_type": type(error).__name__,
                        "error": str(error),
                    },
                )
                if attempt >= self.runtime.limits.stage_attempts or not _retryable(error):
                    raise
                self.sleep(self.runtime.limits.stage_retry_seconds[attempt - 1])
        raise RuntimeError(f"stage failed without an exception: {last}")


def _execute(
    executor: StageExecutor,
    label: str,
    prompt: str,
    expected: tuple[str, ...] = (),
    payload: dict[str, Any] | None = None,
    *,
    allow_unchanged: bool = False,
) -> str:
    return executor.execute(
        StageRequest(
            label=label,
            prompt=prompt,
            expected_paths=expected,
            payload=payload or {},
            allow_unchanged=allow_unchanged,
        )
    )


def _page_ranges(profile: dict[str, Any]) -> list[tuple[int, int]]:
    pages = [int(page) for page in profile["primary_pages"]]
    parts = min(2, len(pages))
    size = -(-len(pages) // parts)
    return [
        (chunk[0], chunk[-1])
        for chunk in (pages[offset : offset + size] for offset in range(0, len(pages), size))
        if chunk
    ]


def _pages_clause(pages: list[int]) -> str:
    listed = ", ".join(
        f"sources/pdf-pages/page-{page:04d}.md" for page in sorted(set(pages))
    )
    return f"Read these canonical page gateways before writing: {listed}."


def _survey_prompt(raw_name: str, task: str, schema: str, extra: str = "") -> str:
    return (
        f"SURVEY ONLY. Read `_ingest/{raw_name}` to the end using offsets. {task}\n\n"
        "Do not write wiki notes. Every pdf_pages value must occur in a `pdf page N` "
        "marker in the staged source. Write the named `_plan/` file as valid JSON only, "
        "without fences. Keep fields terse so the artifact arrives whole.\n\n"
        f"Schema:\n{schema}{extra}"
    )


def _survey_correction_targets(
    message: str,
    requests: list[tuple[str, str, str, dict[str, Any]]],
) -> list[str]:
    """Return only survey files implicated by a deterministic plan error."""
    lowered = message.casefold()
    direct = [
        filename
        for filename, label, _, _ in requests
        if label in lowered or filename.casefold() in lowered
    ]
    if direct:
        return direct

    families: set[str] = set()
    if "chapter" in lowered or (
        "section" in lowered and "section_order" not in lowered
    ):
        families.add("outline")
    if "episode" in lowered or "case_dossier" in lowered:
        families.add("episodes")
    if "concept" in lowered:
        families.add("concepts")
    if any(
        token in lowered
        for token in ("people", "person", "author", "claim", "motif")
    ):
        families.add("roster")

    targeted = [
        filename
        for filename, label, _, _ in requests
        if label in families
        or ("episodes" in families and label.startswith("episodes-"))
    ]
    return targeted or [filename for filename, _, _, _ in requests]


def run_survey(
    *,
    executor: StageExecutor,
    raw_name: str,
    plan_dir: Path,
    profile: dict[str, Any],
    attempts: int = 3,
) -> dict[str, Any]:
    ranges = _page_ranges(profile)
    episode_floor = plan.floor(profile, "case_dossiers")
    per_part = None if episode_floor is None else -(-episode_floor // len(ranges))

    requests: list[tuple[str, str, str, dict[str, Any]]] = [
        (
            plan.OUTLINE_FILE,
            "outline",
            _survey_prompt(
                raw_name,
                "Enumerate the scope's ordered sections and argument architecture. "
                + plan.count_clause(
                    profile,
                    "section_notes",
                    floor_text="The source demonstrably has at least {n} sections.",
                    open_text="Use exactly the divisions the source makes; do not pad or merge.",
                ),
                plan.OUTLINE_SCHEMA,
            ),
            {"kind": "outline"},
        ),
        (
            plan.CONCEPTS_FILE,
            "concepts",
            _survey_prompt(
                raw_name,
                "Enumerate every distinct substantive, methodological, and epistemic "
                "concept. All three kinds are mandatory. "
                + plan.count_clause(
                    profile,
                    "concept_notes",
                    floor_text="The scope demonstrably contains at least {n} concepts.",
                    open_text="Use the concepts actually present; a short list may be correct.",
                ),
                plan.CONCEPTS_SCHEMA,
            ),
            {"kind": "concepts"},
        ),
        (
            plan.ROSTER_FILE,
            "roster",
            _survey_prompt(
                raw_name,
                "Enumerate every named person, the document author as the sole `author` "
                "group member, substantive claims with evidence limits, and recurring "
                "motifs. Do not turn a one-off feature into a motif. "
                + plan.count_clause(
                    profile,
                    "claims",
                    floor_text="Record at least {n} substantive claims.",
                    open_text="Record the substantive claims actually made.",
                ),
                plan.ROSTER_SCHEMA,
            ),
            {"kind": "roster"},
        ),
    ]
    for index, (first, last) in enumerate(ranges, 1):
        count = (
            f"Expect at least {per_part}; fewer likely skips brief mentions."
            if per_part is not None
            else "Use the episode count the range actually contains; never pad it."
        )
        filename = plan.episodes_file(index)
        requests.append(
            (
                filename,
                f"episodes-{index}",
                _survey_prompt(
                    raw_name,
                    f"Read only PDF pages {first}-{last}. Enumerate every distinct narrated "
                    "incident, observation, experiment, record, or worked example, including "
                    f"brief mentions and compilations. {count}",
                    plan.EPISODES_SCHEMA,
                ),
                {"kind": "episodes", "part": index, "first": first, "last": last},
            )
        )

    corrections: dict[str, str] = {}
    for survey_attempt in range(1, attempts + 1):
        for filename, label, prompt, payload in requests:
            correcting = filename in corrections
            if not (plan_dir / filename).is_file() or correcting:
                correction = corrections.pop(filename, "")
                _execute(
                    executor,
                    f"survey:{label}",
                    prompt + correction,
                    (f"_plan/{filename}",),
                    payload,
                    allow_unchanged=correcting,
                )
        try:
            inventory = plan.load_inventory(plan_dir, profile, len(ranges))
        except plan.PlanError as error:
            if survey_attempt >= attempts:
                raise
            message = str(error)
            correction = (
                "\n\nCORRECTION. The deterministic plan validator rejected the prior file "
                f"with: {message}. Rewrite only this file while preserving valid entries."
            )
            for filename in _survey_correction_targets(message, requests):
                corrections[filename] = correction
            continue

        snapshots = {
            plan.episodes_file(index): (plan_dir / plan.episodes_file(index)).read_text(
                encoding="utf-8"
            )
            for index in range(1, len(ranges) + 1)
        }
        for index, (first, last) in enumerate(ranges, 1):
            filename = plan.episodes_file(index)
            known = [
                item["slug"]
                for item in inventory["episodes"]
                if any(first <= page <= last for page in item["pdf_pages"])
            ]
            _execute(
                executor,
                f"survey-audit:{index}",
                f"Re-read `_ingest/{raw_name}` only over PDF pages {first}-{last}. Existing "
                f"episode slugs are {known}. Append to `_plan/{filename}` only genuinely "
                "missed brief incidents; otherwise change nothing. Preserve all prior entries.",
                (f"_plan/{filename}",),
                {"kind": "episode-audit", "part": index, "known": known},
                allow_unchanged=True,
            )
        try:
            return plan.load_inventory(plan_dir, profile, len(ranges))
        except plan.PlanError:
            for filename, text in snapshots.items():
                atomic_text(plan_dir / filename, text)
            return inventory
    raise plan.PlanError("survey did not produce a usable inventory")


def stage_structure(
    executor: StageExecutor,
    inventory: dict[str, Any],
    date: str,
    writer: str,
) -> None:
    chapter = inventory["chapter"]
    chapter_path = f"chapters/{chapter['slug']}.md"
    _execute(
        executor,
        "structure:chapter",
        f"Write exactly `{chapter_path}` (type Chapter). Reconstruct the organizing "
        f"question and ordered inference from this inventory: {json.dumps(chapter)}. Link "
        "every planned section, distinguish what the scope establishes from what it leaves "
        f"open, identify the evidential hinge, and append a {date} [{writer}] log entry.",
        (chapter_path,),
        {"kind": "chapter", "item": chapter},
    )
    for section in inventory["sections"]:
        path = f"sections/{section['slug']}.md"
        _execute(
            executor,
            f"structure:section-{section['order']:02d}",
            f"Write exactly `{path}` (type Section), titled {section['title']!r}. "
            f"{_pages_clause(section['pdf_pages'])} Explain the section in source order, "
            "its role, its limits, and link the chapter and related planned notes. "
            f"Append a {date} [{writer}] structure log entry.",
            (path,),
            {"kind": "section", "item": section, "chapter": chapter},
        )


def stage_cases(
    executor: StageExecutor,
    inventory: dict[str, Any],
    date: str,
    writer: str,
) -> None:
    groups = plan.batches(inventory["episodes"], NOTES_PER_WRITE_STAGE)
    for index, items in enumerate(groups, 1):
        paths = tuple(f"cases/{item['slug']}.md" for item in items)
        pages = [page for item in items for page in item["pdf_pages"]]
        _execute(
            executor,
            f"cases:{index}",
            f"Write exactly these separate Case Dossier notes: {list(paths)}. "
            f"Inventory: {json.dumps(items, ensure_ascii=False)}. {_pages_clause(pages)} "
            "Each dossier must give the attributed reported account, source chain, use in "
            "the argument, and explicit Evidence limits naming support that is absent. "
            f"Append one {date} [{writer}] cases log entry.",
            paths,
            {"kind": "cases", "items": items},
        )


def stage_concepts(
    executor: StageExecutor,
    inventory: dict[str, Any],
    date: str,
    writer: str,
) -> None:
    groups = plan.batches(inventory["concepts"], NOTES_PER_WRITE_STAGE)
    for index, items in enumerate(groups, 1):
        paths = tuple(f"concepts/{item['slug']}.md" for item in items)
        pages = [page for item in items for page in item["pdf_pages"]]
        _execute(
            executor,
            f"concepts:{index}",
            f"Write exactly these distinct Concept notes: {list(paths)}. Inventory: "
            f"{json.dumps(items, ensure_ascii=False)}. {_pages_clause(pages)} Each note "
            "defines the idea, explains its use, links every instance in scope, and gives "
            "the kind-specific guardrail required by the guide. Distinguish a supported weak "
            f"claim from an unsupported strong one. Append one {date} [{writer}] log entry.",
            paths,
            {"kind": "concepts", "items": items},
        )


def stage_people(
    executor: StageExecutor,
    inventory: dict[str, Any],
    profile: dict[str, Any],
    date: str,
    writer: str,
) -> None:
    for path, person in plan.author_note_paths(inventory, profile):
        _execute(
            executor,
            "people:author",
            f"Write exactly `{path}` (type Person) about {person['name']}'s role in this "
            "scope, not a biography. Separate compilation, interpretation, caveats, and "
            f"auditable moves. {_pages_clause(person['pdf_pages'])} Append one {date} "
            f"[{writer}] people log entry.",
            (path,),
            {"kind": "author", "item": person},
        )
    groups = plan.role_groups(inventory)
    for group, members in sorted(groups.items()):
        if group == plan.AUTHOR_GROUP:
            continue
        path = f"people/{group}.md"
        _execute(
            executor,
            f"people:{group}",
            f"Write exactly `{path}` (type Person) as a role roster for these people only: "
            f"{json.dumps(members, ensure_ascii=False)}. Use a table with role, linked "
            "appearance, and caution columns; stay within the source. Append one "
            f"{date} [{writer}] people log entry.",
            (path,),
            {"kind": "people-roster", "group": group, "items": members},
        )


def stage_synthesis(
    executor: StageExecutor,
    inventory: dict[str, Any],
    date: str,
    writer: str,
) -> None:
    tasks = [
        (
            "argument-map",
            "Synthesis",
            "Map the ordered inference. For each move name its support and assumption; end "
            "where the argument outruns the evidence.",
        ),
        (
            "claim-evidence-matrix",
            "Synthesis",
            "Build a literal table with Claim, Owner, In-scope evidence, Evidential limit, "
            "and linked Notes columns using every surveyed claim.",
        ),
        (
            "motif-matrix",
            "Synthesis",
            "Build a literal table with one row per case and one column per motif. Mark a "
            "cell only when that case's page states the feature; blanks are valid.",
        ),
        (
            "critical-reading",
            "Synthesis",
            "Set genuine strengths against concrete controls absent in this scope, including "
            "selection, source dependence, transmission, and compression where applicable.",
        ),
        (
            "open-questions",
            "Open Question",
            "State material questions the scope leaves unresolved and evidence that could "
            "discriminate readings; never list wiki backlog or outside chapters.",
        ),
    ]
    for index, (slug, note_type, instruction) in enumerate(tasks, 1):
        path = f"synthesis/{slug}.md"
        _execute(
            executor,
            f"synthesis:{slug}",
            f"Write exactly `{path}` (type {note_type}), synthesis task {index}/5. "
            f"{instruction} Survey inventory: {json.dumps(inventory, ensure_ascii=False)}. "
            f"Append one {date} [{writer}] synthesis log entry.",
            (path,),
            {"kind": "synthesis", "slug": slug, "note_type": note_type},
        )


def stage_navigation(
    executor: StageExecutor,
    inventory: dict[str, Any],
    date: str,
    writer: str,
) -> None:
    _execute(
        executor,
        "navigation:overview",
        "Write exactly `overview.md` (type Overview): orient the reader to the question, "
        "argument, established/open distinction, neutral-reading rule, chapter, synthesis, "
        f"and source guides. Append one {date} [{writer}] navigation log entry.",
        ("overview.md",),
        {"kind": "overview", "inventory": inventory},
    )
    _execute(
        executor,
        "navigation:reading-guide",
        "Write exactly `reading-guide.md` (type Reading Guide): give a first-read path, "
        "claim audit path to gateways, episode lookup workflow, page-map pointer, and exact "
        f"scope warning. Append one {date} [{writer}] navigation log entry.",
        ("reading-guide.md",),
        {"kind": "reading-guide", "inventory": inventory},
    )
    folders = ["chapters", "sections", "cases", "concepts", "people", "synthesis"]
    for index, group in enumerate(plan.batches(folders, NOTES_PER_WRITE_STAGE), 1):
        expected = tuple(f"{folder}/index.md" for folder in group)
        _execute(
            executor,
            f"navigation:folder-indexes-{index}",
            f"Write exactly these folder catalogs: {list(expected)}. List every note now in "
            "each folder once as a relative Markdown link plus a short hook. Catalogs have "
            f"no front matter. Append one {date} [{writer}] navigation log entry.",
            expected,
            {"kind": "folder-indexes", "folders": group},
        )
    _execute(
        executor,
        "navigation:root-index",
        "Replace only root `index.md` with a complete catalog of every semantic note, every "
        "source/reference guide, and the source-page gateway index. Start with a short scope "
        "and unverified-draft warning. Use one section per family and exact relative links. "
        f"Append one {date} [{writer}] navigation log entry.",
        ("index.md",),
        {"kind": "root-index", "inventory": inventory},
    )


def stage_completeness(
    executor: StageExecutor,
    missing: dict[str, str],
    index: str,
    date: str,
    writer: str,
) -> None:
    expected = tuple(sorted(missing))
    _execute(
        executor,
        f"completeness:{index}",
        f"Write exactly these missing planned notes: {json.dumps(missing)}. Read their "
        "survey entry and source gateways, follow the guide, and add them to catalogs. "
        f"Append one {date} [{writer}] completeness log entry.",
        expected,
        {"kind": "completeness", "missing": missing},
    )


def stage_crosslink(
    executor: StageExecutor,
    orphans: list[str],
    index: int,
    date: str,
    writer: str,
) -> None:
    _execute(
        executor,
        f"crosslink:{index}",
        f"Repair links for only these at-most-two orphan notes: {orphans}. Add genuine "
        "incoming links from their section, chapter, concept, case, person, or synthesis "
        "neighbors. Preserve content; create no notes and never edit evidence. Append one "
        f"{date} [{writer}] crosslink log entry.",
        (),
        {"kind": "crosslink", "orphans": orphans},
    )


LINT_REPAIR = """Fix every listed deterministic lint error, but touch only the named one
or two files. Read each first. Resolve links to canonical existing notes; never create an
alias. Repair exact OKF v0.2 front matter, source links, catalogs, evidence limits, and
concept guardrails. Do not edit immutable evidence. Errors:\n{errors}\nAppend one
`{date} [{writer}] lint-repair: ...` log entry."""


def repair_lint(
    executor: StageExecutor,
    wiki_dir: Path,
    date: str,
    writer: str,
) -> list[dict[str, Any]]:
    for round_index in range(1, LINT_ROUNDS + 1):
        errors = lint.lint(wiki_dir)
        if not errors:
            return []
        by_file: dict[str, list[dict[str, Any]]] = {}
        for error in errors:
            by_file.setdefault(str(error.get("file", "index.md")), []).append(error)
        for batch_index, names in enumerate(
            plan.batches(sorted(by_file), NOTES_PER_WRITE_STAGE), 1
        ):
            subset = [item for name in names for item in by_file[name]]
            _execute(
                executor,
                f"lint-repair:{round_index}.{batch_index}",
                LINT_REPAIR.format(
                    errors=json.dumps(subset, ensure_ascii=False, indent=2),
                    date=date,
                    writer=writer,
                ),
                tuple(names),
                {"kind": "lint-repair", "files": names, "errors": subset},
            )
        sweep_strays(wiki_dir)
    return lint.lint(wiki_dir)


def sweep_strays(wiki_dir: Path) -> list[str]:
    allowed_roots = set(lint.FOLDER_TYPES) | {
        "images",
        "reports",
        "audit",
        "_schema",
        "_ingest",
        "_plan",
    }
    allowed_root_files = set(lint.ROOT_TYPES) | lint.SPECIAL | {
        "checksums.sha256",
        "manifest.json",
    }
    removed: list[str] = []
    for path in sorted(Path(wiki_dir).rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(wiki_dir)
        if len(relative.parts) == 1:
            invalid = relative.name not in allowed_root_files
        else:
            invalid = relative.parts[0] not in allowed_roots
        if invalid:
            path.unlink()
            removed.append(relative.as_posix())
    for directory in sorted(Path(wiki_dir).rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    return removed


def review_and_repair(
    *,
    executor: StageExecutor,
    reviewer: review.ReviewClient,
    wiki_dir: Path,
    profile: dict[str, Any],
    audit: AuditLog,
    runtime: RuntimeConfig,
    date: str,
    writer: str,
    smoke: bool,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    scope = {
        int(page)
        for page in profile["primary_pages"] + profile["apparatus_pages"]
    }
    pages = review.load_pages_from_gateways(wiki_dir, scope)
    repaired: set[str] | None = None
    seen_pages: set[int] = set()
    cited_pages: set[int] = set()
    seen_notes: set[str] = set()
    pending_notes = {
        path.relative_to(wiki_dir).as_posix()
        for path in review.semantic_notes(wiki_dir)
    }
    rounds: list[dict[str, Any]] = []
    report: dict[str, Any] = {}
    for round_index in range(1, REVIEW_ROUNDS + 1):
        report = review.run_independent_review(
            wiki_dir=wiki_dir,
            pages=pages,
            profile=profile,
            audit=audit,
            client=reviewer,
            attempts=runtime.limits.review_attempts,
            retry_seconds=runtime.limits.review_retry_seconds,
            round_label=str(round_index),
            max_packets=2 if smoke else None,
            only_notes=repaired,
            sleep=sleep,
        )
        seen_pages.update(report.get("pages_reviewed_this_round", []))
        successful_notes: set[str] = set()
        for packet in report.get("packets", []):
            cited_pages.update(packet.get("pages_cited", []))
            if "error" not in packet:
                successful_notes.update(packet.get("notes", []))
        seen_notes.update(successful_notes)
        pending_notes.difference_update(successful_notes)
        rounds.append(
            {
                "round": str(round_index),
                "scope": report.get("scope_of_round"),
                "pages_reviewed": report.get("pages_reviewed_this_round", []),
                "notes_reviewed": len(successful_notes),
                "material_findings": report.get("material_findings"),
                "execution_status": report.get("execution_status"),
                "coverage": report.get("coverage"),
            }
        )
        report["pages_reviewed"] = sorted(seen_pages)
        report["pages_cited_but_never_supplied"] = sorted(cited_pages - seen_pages)
        report["notes_reviewed"] = sorted(seen_notes)
        report["notes_pending_review"] = sorted(pending_notes)
        report["page_coverage"] = (
            "complete" if not (cited_pages - seen_pages) else "incomplete"
        )
        report["coverage_across_rounds"] = (
            "complete"
            if not pending_notes and not (cited_pages - seen_pages)
            else "partial"
        )
        report["rounds"] = list(rounds)
        if report["finding_status"] == "clear" or round_index == REVIEW_ROUNDS:
            break
        if report["execution_status"] != "passed":
            break
        findings = [
            finding
            for packet in report["packets"]
            for finding in packet.get("findings", [])
        ]
        by_note: dict[str, list[dict[str, Any]]] = {}
        for finding in findings:
            note = str(finding.get("note", ""))
            if note:
                by_note.setdefault(note, []).append(finding)
        repaired = set(by_note)
        for batch_index, names in enumerate(
            plan.batches(sorted(by_note), NOTES_PER_WRITE_STAGE), 1
        ):
            group = [finding for name in names for finding in by_note[name]]
            _execute(
                executor,
                f"review-repair:{round_index}.{batch_index}",
                review.findings_prompt(group, date, writer),
                tuple(names),
                {"kind": "review-repair", "files": names, "findings": group},
            )
        # A repaired path has a new version.  Its earlier review remains useful
        # for cumulative page coverage, but the note is pending until a later
        # round successfully inspects the changed artifact.
        pending_notes.update(repaired)
        remaining_lint = repair_lint(executor, wiki_dir, date, writer)
        if remaining_lint:
            raise RuntimeError(
                f"lint failed after review repair: {len(remaining_lint)} error(s)"
            )
    atomic_json(wiki_dir / "reports" / "independent-review.json", report)
    return report


IMAGE_MARKER_RE = re.compile(r"\[IMAGE: images/([^ \]|]+)")


def _stage_images(
    raw_text: str,
    extraction_root: Path,
    wiki_dir: Path,
    document_id: str,
) -> list[str]:
    staged: list[str] = []
    for name in sorted(set(IMAGE_MARKER_RE.findall(raw_text))):
        source = Path(extraction_root) / "images" / name
        if not source.is_file():
            continue
        destination = Path(wiki_dir) / "images" / document_id / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination)
        shutil.copy2(source, destination)
        staged.append(destination.relative_to(wiki_dir).as_posix())
    return staged


def compile_wiki(
    *,
    executor: StageExecutor,
    reviewer: review.ReviewClient,
    wiki_dir: Path,
    raw_path: Path,
    extraction_root: Path,
    profile: dict[str, Any],
    audit: AuditLog,
    recorder: TranscriptRecorder,
    runtime: RuntimeConfig,
    smoke: bool,
    writer: str = "hexwiki",
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Run every drafting, completeness, repair, and independent-review stage."""
    wiki_dir = Path(wiki_dir).resolve()
    if executor.wiki_dir.resolve() != wiki_dir:
        raise ValueError("stage executor is bound to a different candidate wiki")
    ingest_dir = wiki_dir / "_ingest"
    plan_dir = wiki_dir / "_plan"
    if ingest_dir.exists() or plan_dir.exists():
        raise FileExistsError("candidate already contains transient model staging")
    ingest_dir.mkdir()
    plan_dir.mkdir()
    staged = ingest_dir / Path(raw_path).name
    raw_text = Path(raw_path).read_text(encoding="utf-8")
    atomic_text(staged, raw_text)
    images = _stage_images(
        raw_text, extraction_root, wiki_dir, str(profile["document_id"])
    )
    date = dt.date.today().isoformat()
    started = time.monotonic()
    inventory: dict[str, Any] = {}
    try:
        inventory = run_survey(
            executor=executor,
            raw_name=staged.name,
            plan_dir=plan_dir,
            profile=profile,
        )
        frozen = plan.freeze_inventory(plan_dir, inventory)
        audit.record(
            phase="survey",
            action="freeze_validated_inventory",
            what=(
                f"Froze a plan with {len(inventory['sections'])} sections, "
                f"{len(inventory['episodes'])} episodes, and "
                f"{len(inventory['concepts'])} concepts."
            ),
            why="Every planned unit must have a deterministic completeness check.",
            how="Validated all survey JSON against the locked scope and hashed the aggregate.",
            details={"inventory_sha256": frozen["inventory_sha256"]},
        )
        active_inventory = (
            plan.narrow_for_smoke(plan_dir, inventory) if smoke else inventory
        )
        stage_structure(executor, active_inventory, date, writer)
        stage_cases(executor, active_inventory, date, writer)
        stage_concepts(executor, active_inventory, date, writer)
        stage_people(executor, active_inventory, profile, date, writer)
        stage_synthesis(executor, active_inventory, date, writer)
        stage_navigation(executor, active_inventory, date, writer)

        for round_index in range(1, COMPLETENESS_ROUNDS + 1):
            missing = plan.missing_notes(wiki_dir, active_inventory, profile)
            if not missing:
                break
            for batch_index, group in enumerate(
                plan.batches(sorted(missing.items()), NOTES_PER_WRITE_STAGE), 1
            ):
                stage_completeness(
                    executor,
                    dict(group),
                    f"{round_index}.{batch_index}",
                    date,
                    writer,
                )
        remaining = plan.missing_notes(wiki_dir, active_inventory, profile)
        if remaining:
            raise RuntimeError(
                f"{len(remaining)} planned notes were never written: {sorted(remaining)}"
            )

        for index, group in enumerate(
            plan.batches(lint.orphans(wiki_dir), NOTES_PER_WRITE_STAGE), 1
        ):
            stage_crosslink(executor, group, index, date, writer)
        removed = sweep_strays(wiki_dir)
        if removed:
            recorder.append(
                "stage-events", {"event": "strays-removed", "paths": removed}
            )
        lint_errors = repair_lint(executor, wiki_dir, date, writer)
        if lint_errors:
            raise RuntimeError(f"{len(lint_errors)} lint errors remain: {lint_errors}")

        review_report = review_and_repair(
            executor=executor,
            reviewer=reviewer,
            wiki_dir=wiki_dir,
            profile=profile,
            audit=audit,
            runtime=runtime,
            date=date,
            writer=writer,
            smoke=smoke,
            sleep=sleep,
        )
        release_evidence = {
            "mode": "diagnostic-smoke" if smoke else "production-candidate",
            "deterministic_lint": "passed" if not lint.lint(wiki_dir) else "failed",
            "independent_review": {
                key: review_report.get(key)
                for key in (
                    "execution_status",
                    "finding_status",
                    "material_findings",
                    "page_coverage",
                    "coverage_across_rounds",
                )
            },
            "semantic_notes": len(review.semantic_notes(wiki_dir)),
            "all_notes_are_unverified_drafts": True,
            "profile_id": profile["profile_id"],
            "locked_scope": {
                "profile_lock_verification": "passed",
                "scope_declaration_matches_locked_evidence": True,
                "scope_id": profile["scope_id"],
                "scope_label": profile["scope_label"],
                "primary_pages": list(profile["primary_pages"]),
                "apparatus_pages": list(profile["apparatus_pages"]),
                "source_pdf_sha256": profile["source_pdf_sha256"],
                "canonical_scope_sha256": profile["canonical_scope_sha256"],
            },
        }
        release_report = review.run_release_review(
            evidence=release_evidence,
            audit=audit,
            client=reviewer,
            attempts=runtime.limits.review_attempts,
            retry_seconds=runtime.limits.review_retry_seconds,
            sleep=sleep,
        )
        atomic_json(wiki_dir / "reports" / "release-review.json", release_report)
        result = {
            "status": "passed",
            "smoke": smoke,
            "inventory_sha256": frozen["inventory_sha256"],
            "inventory_counts": {
                key: len(inventory[key])
                for key in ("sections", "episodes", "concepts", "people", "claims")
            },
            "staged_images": images,
            "independent_review": review_report,
            "release_review": release_report["release_review"],
            "stage_labels": list(getattr(executor, "executed_labels", [])),
            "duration_seconds": round(time.monotonic() - started, 3),
        }
        return result
    finally:
        for path in sorted(plan_dir.glob("*.json")):
            destination = recorder.root / f"plan-{path.name}"
            if not destination.exists():
                atomic_text(destination, path.read_text(encoding="utf-8"))
        if staged.exists():
            staged.unlink()
        for transient in (ingest_dir, plan_dir):
            if not transient.exists():
                continue
            for path in sorted(transient.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            transient.rmdir()
        flush_observability(runtime, recorder)


def _hard_deadline(seconds: int) -> threading.Timer | None:
    if seconds <= 0:
        return None

    def exit_child() -> None:
        print(
            json.dumps(
                {"event": "absolute_child_deadline_exceeded", "seconds": seconds}
            ),
            file=sys.stderr,
            flush=True,
        )
        os._exit(124)

    timer = threading.Timer(seconds, exit_child)
    timer.daemon = True
    timer.start()
    return timer


def child_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--profile-lock", type=Path, required=True)
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--extraction", type=Path, required=True)
    parser.add_argument("--wiki-dir", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--mode", choices=("smoke", "build"), required=True)
    parser.add_argument("--recursion-limit", type=int, required=True)
    parser.add_argument("--deadline-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if stream.encoding and stream.encoding.casefold() != "utf-8":
            stream.reconfigure(encoding="utf-8", errors="replace")

    profile = load_profile(args.profile)
    lock = load_profile_lock(args.profile_lock)
    runtime_profile = profile.runtime(lock)
    runtime = load_runtime_config(require_network=True)
    run_dir = args.run_dir.resolve()
    recorder = TranscriptRecorder(
        run_dir / "stage-transcripts",
        args.run_id,
        secrets=(runtime.api_key,),
    )
    audit = AuditLog(run_dir / "child-actions.jsonl", args.run_id)
    timer = _hard_deadline(args.deadline_seconds)
    try:
        executor = DeepAgentExecutor(
            wiki_dir=args.wiki_dir,
            runtime=runtime,
            recorder=recorder,
            recursion_limit=args.recursion_limit,
        )
        reviewer_client = review.OpenAIReviewClient(runtime, recorder)
        result = compile_wiki(
            executor=executor,
            reviewer=reviewer_client,
            wiki_dir=args.wiki_dir,
            raw_path=args.raw,
            extraction_root=args.extraction,
            profile=runtime_profile,
            audit=audit,
            recorder=recorder,
            runtime=runtime,
            smoke=args.mode == "smoke",
        )
        atomic_json(run_dir / "child-result.json", result)
        return 0
    except BaseException as error:
        atomic_json(
            run_dir / "child-failure.json",
            {
                "status": "failed",
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
            },
        )
        raise
    finally:
        if timer is not None:
            timer.cancel()


if __name__ == "__main__":
    raise SystemExit(child_main())
