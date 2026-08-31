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

SYSTEM_ROLE = """You are an automated wiki-compilation agent building a source-bounded
knowledge wiki from an immutable staged source text. Your filesystem tools are rooted at
one fresh candidate wiki: `index.md`, `log.md`, and folders like `cases/` and
`concepts/` are top-level paths. Work only from the immutable current-run source under
`_ingest/`, the canonical gateways under `sources/pdf-pages/`, the generated survey
under `_plan/`, and notes already in this candidate. You work autonomously: never ask
questions; when the task is complete, finish with a one-paragraph summary.

You MUST follow the wiki guide below exactly - the note-type table, the completeness
rules, the front-matter contract, the body template, and the epistemic rules.

Standing rules for every stage:
- GROUNDING. Write only what the staged source supports. Never add a fact, date, full
  name, or background detail from your own knowledge, however certain you are. An
  unsupported sentence is a defect even when it is true.
- PROVENANCE. Every note ends with `## Sources` citing the specific pages, and every
  cited page also appears as a direct relative link to its
  `sources/pdf-pages/page-NNNN.md` gateway. Page numbers as bare prose are not
  provenance.
- EPISTEMIC STATUS. Every note you write carries `semantic_note: true` and
  `status: draft` in its front matter, plus a non-empty `sources:` list. These are
  constants of the format. Never write a `verified` field at all - the format reads its
  ABSENCE as "nobody has checked this", which is the truth, and any value there claims a
  confirmation that does not exist. Never raise a status either: you have not verified
  anything, you have compiled it.
- ATTRIBUTION. Attribute contested claims to whoever makes them ("the chapter reports",
  "the witness is said to have", "the author argues"). Keep reported account, source
  chain, author interpretation, and wiki inference visibly distinct. Where the source
  declines to settle a question, record the competing readings and say the scope offers
  no test between them; do not manufacture a verdict either way.
- EVIDENCE LIMITS. Every Case Dossier ends with an `## Evidence limits` section that
  states what in-scope support exists AND, explicitly, what does not: no corroborating
  witness, no named investigator, no primary document, no measurement, a single
  intermediary. Absence of support is a finding. Write it down.
- IMMUTABLE PATHS. Never write or edit anything under `_ingest/`, `sources/`,
  `reference/`, or `WIKI_GUIDE.md`. Read them and link to them freely.
- LINKING. Link liberally to other notes; every link must resolve to a file that exists
  or that you create in the same stage. Never create alias or redirect notes.
- REMOVING. You have a `delete_file` tool. If you wrote a note at the wrong path, or a
  duplicate exists, delete it. Never signal a deletion by writing another file - no
  removal-marker note, no tombstone file, no shell script, no probe file - each of
  those is a new lint error, not a deletion. Write only under the folders the guide
  defines; there is no `tmp/` in this wiki.
- LOG. `log.md` is append-only: add entries at the end, never rewrite earlier ones.
  Entry format: `- {date} [{writer}] <description>`.
- STAGE SCOPE. Create or materially rewrite no more than the explicitly named one or two
  artifacts in a stage.

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
        # Resolve the root the same way children are resolved, so the two
        # cannot end up spelled differently for the containment comparison.
        self.root = self._plain(Path(os.path.realpath(os.fspath(root))))

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
        # ``realpath``, not ``abspath``: the same directory can be spelled more
        # than one way on Windows, and only real resolution collapses them. A
        # hosted Windows runner sets TEMP to its 8.3 short form, so the root
        # arrives with a truncated ``NAME~1`` component while a resolved child
        # comes back with the long name. Compared as text those look unrelated,
        # and containment then refuses a write that is plainly inside the
        # candidate. ``realpath`` also collapses symlinks, so an escape through
        # one is still caught.
        value = os.path.normcase(os.path.realpath(os.fspath(path)))
        return value[4:] if value.startswith(cls._LONG_PATH_PREFIX) else value

    @staticmethod
    def _key_parts(key: str) -> list[str]:
        """Split a wiki-relative key, rejecting anything that could escape."""
        if not isinstance(key, str) or not key.strip() or "\x00" in key:
            raise SandboxViolation("file path must be a non-empty string")
        normalized = key.replace("\\", "/")
        if PureWindowsPath(normalized).drive:
            raise SandboxViolation("drive-qualified paths are not allowed")
        candidate_key = normalized[1:] if normalized.startswith("/") else normalized
        parts = [part for part in candidate_key.split("/") if part not in {"", "."}]
        if any(part == ".." for part in parts) or (parts and parts[0].startswith("~")):
            raise SandboxViolation("path traversal is not allowed")
        return parts

    def resolve(self, key: str) -> Path:
        parts = self._key_parts(key)
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
        """The wiki-relative path for ``key``, once ``resolve`` proves containment.

        Deliberately *not* computed by subtracting the root from the resolved
        path. Two spellings of one location -- an extended-length ``\\\\?\\``
        prefix, an 8.3 short name, a differing case, a symlink -- make that
        subtraction raise ``ValueError`` on a path that is plainly inside the
        candidate, and an unhandled one propagates out of the tool node and
        takes the whole compilation child with it. It cost a 35-minute run
        once and three Windows CI jobs afterwards.

        So containment is proved by ``resolve`` against fully resolved forms,
        and the relative path is rebuilt from the caller's own key. There is no
        spelling left to disagree about.
        """
        self.resolve(key)
        parts = self._key_parts(key)
        return Path(*parts) if parts else Path(".")

    def is_immutable(self, key: str) -> bool:
        relative = self.relative(key)
        return relative.name == "WIKI_GUIDE.md" or bool(
            relative.parts and relative.parts[0] in self.IMMUTABLE_ROOTS
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
        from deepagents.backends.protocol import (
            EditResult,
            FileUploadResponse,
            WriteResult,
        )
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
                        FileUploadResponse(
                            path=file_path, error="immutable_current_run_evidence"
                        )
                    )
                else:
                    responses.extend(super().upload_files([(file_path, content)]))
            return responses

    # Hand the backend the *resolved* root. It does its own path arithmetic
    # against this value, so if it keeps an unresolved spelling while
    # ``_resolve_path`` returns a resolved one, the two disagree on a machine
    # where the same directory has two names -- an 8.3 short form, a symlinked
    # temp directory -- and a legitimate write raises ValueError from inside
    # deepagents, where this module cannot catch it.
    backend = GuardedBackend(root_dir=policy.root, virtual_mode=True)

    @tool("delete_file", parse_docstring=False)
    def delete_file(file_path: str) -> str:
        """Delete one mutable model-written file by its wiki-relative path."""
        try:
            policy.delete_file(file_path)
            # Ask the policy for the relative path rather than subtracting the
            # raw ``wiki_dir`` from the resolved one: those are two spellings of
            # the same directory on a machine with 8.3 short names, and the
            # subtraction raises ValueError out of the tool.
            recorded = policy.relative(file_path).as_posix()
        except (SandboxViolation, FileNotFoundError) as error:
            return f"Refused: {error}"
        recorder.append(
            "tool-events",
            {"event": "guarded-delete", "path": recorded},
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
            if (
                isinstance(value, (list, tuple, set, dict))
                and len(value) > NOTES_PER_WRITE_STAGE
            ):
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
    if isinstance(
        error, (PermissionError, FileNotFoundError, SandboxViolation, plan.PlanError)
    ):
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
                messages = (
                    update.get("messages", []) if isinstance(update, dict) else []
                )
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
                if attempt >= self.runtime.limits.stage_attempts or not _retryable(
                    error
                ):
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
        for chunk in (
            pages[offset : offset + size] for offset in range(0, len(pages), size)
        )
        if chunk
    ]


def _pages_clause(pages: list[int]) -> str:
    listed = ", ".join(
        f"sources/pdf-pages/page-{page:04d}.md" for page in sorted(set(pages))
    )
    return f"Read these canonical page gateways before writing: {listed}."


def _survey_prompt(raw_name: str, task: str, schema: str, extra: str = "") -> str:
    return (
        f"SURVEY ONLY. Read `_ingest/{raw_name}` to the end using offsets. The survey "
        "decides what the wiki will contain: a functional unit omitted here cannot be "
        "recovered by the later completeness check. Work through the requested material "
        f"in source order. {task}\n\n"
        "Do not write wiki notes. The staged source is the only authority: do not use "
        "outside knowledge, a prior wiki, or an inferred fact the pages do not state. "
        "Every pdf_pages value must occur in a `pdf page N` marker in the staged source. "
        "Summaries are terse locators for later writers, not substitutes for the pages, "
        "but they must preserve the names, dates, quantities, reasoning moves, and "
        "evidential cautions that distinguish one unit from another. Write the named "
        "`_plan/` file as valid JSON only, without fences. Keep fields terse so the "
        "artifact arrives whole.\n\n"
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
        token in lowered for token in ("people", "person", "author", "claim", "motif")
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
                "Enumerate the scope's own architecture and argument. Sections are the "
                "source's divisions in its own order, whether marked by headings or a clear "
                "shift of subject. Argument steps are the ordered inference from the opening "
                "move to the close; keep 4-8 distinct steps and do not replace the source's "
                "sequence with a new editorial outline. `establishes` records only what the "
                "pages actually support, while `leaves_open` records questions or causal "
                "choices the scope does not settle. "
                + plan.count_clause(
                    profile,
                    "section_notes",
                    floor_text="The source demonstrably has at least {n} sections.",
                    open_text=(
                        "Use exactly the divisions the source makes; do not split one to reach "
                        "a count or merge two because the list looks short."
                    ),
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
                "concept. Substantive concepts are phenomena, mechanisms, categories, and "
                "recurring effects. Methodological concepts are comparisons, criteria, and "
                "inference rules, including what each rule licenses and does not. Epistemic "
                "concepts are load-bearing assumptions and unresolved competing readings. "
                "Give distinct mechanisms distinct entries even when they co-occur; a broad "
                "topic label must not hide a narrower idea a reader would search for. All "
                "three kinds are mandatory: an all-substantive list has catalogued subject "
                "matter and missed the reasoning. "
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
                "Write three complete inventories. PEOPLE includes every named person, with "
                "the document author as the sole `author` group member. Put everyone else "
                "into a small number of broad role groups (two to four is usual), because "
                "each group becomes one roster note. Each caution must state the person's "
                "in-scope source status or interpretive risk, not a generic warning. CLAIMS "
                "records each substantive claim, its owner, the actual in-scope support, and "
                "the specific break in that support. MOTIFS includes only features recurring "
                "across several episodes and important to the source's comparison. Look "
                "beyond visual form when the pages warrant it: recurring effects, traces, "
                "responses, social consequences, or transmission patterns may matter too. "
                "Do not turn a one-off feature into a motif. "
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
                    "incident, encounter, observation, experiment, record, transaction, or "
                    "worked example, including one-sentence mentions with weak or absent "
                    "apparatus. Do not merge incidents by theme and do not filter by "
                    "importance: a brief entry is still useful when its support field says "
                    "exactly how little the scope provides. Where the source presents three "
                    "or more records together as a compilation, survey, chronicle, or series, "
                    "add one compilation entry for the set in addition to any distinct member "
                    f"episodes; it preserves the source's claim about the body as a whole. {count}",
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
                f"Re-read `_ingest/{raw_name}` only over PDF pages {first}-{last}, in order, "
                "looking specifically for what a first reading skips: an incident in one "
                "clause, a dated observation inside a list, a record attributed to a named "
                "collection or intermediary, a second-hand account used for a passing point, "
                "or a compilation whose collective role is distinct from its members. "
                f"Existing episode slugs are {known}. Append to `_plan/{filename}` only "
                "genuinely missed entries; otherwise change nothing. Preserve every prior "
                "entry without removing, reordering, or rewording it, and never duplicate one "
                "under another slug.",
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
    chapter_pages = sorted(
        {page for section in inventory["sections"] for page in section["pdf_pages"]}
    )
    steps = "\n".join(
        f"  {index}. {step}" for index, step in enumerate(chapter["argument_steps"], 1)
    )
    sections = "\n".join(
        f"  {section['order']}. sections/{section['slug']}.md — "
        f"{section['title']!r} (PDF pages {section['pdf_pages']}): "
        f"{section['summary']}"
        for section in inventory["sections"]
    )
    _execute(
        executor,
        "structure:chapter",
        f"STRUCTURE — CHAPTER HUB. Write exactly `{chapter_path}` (type Chapter), the "
        "single hub from which a reader can reconstruct this scope's architecture. "
        f"{_pages_clause(chapter_pages)} Read the pages for the source's own progression; "
        "the survey below is a locator and build contract, not a substitute for them.\n\n"
        f"Organizing question: {chapter['organizing_question']}\n\n"
        "Reconstruct the ordered inference as a numbered list using every surveyed step. "
        "For each move, state what the preceding material supports and where an assumption "
        "enters. Then give an ordered linked list of every section, an explicit `What this "
        "establishes` versus `What it leaves open` pair, and the evidential hinge where the "
        "argument first needs more than the preceding pages supply. Do not replace the "
        "source's sequence with a smoother thesis of your own.\n\n"
        f"Surveyed argument steps:\n{steps}\n\n"
        f"Planned sections, in source order:\n{sections}\n\n"
        f"Append one `{date} [{writer}] structure: ...` log entry.",
        (chapter_path,),
        {"kind": "chapter", "item": chapter},
    )
    for section in inventory["sections"]:
        path = f"sections/{section['slug']}.md"
        section_pages = set(section["pdf_pages"])
        related_cases = [
            f"cases/{item['slug']}.md"
            for item in inventory["episodes"]
            if item["section_order"] == section["order"]
        ]
        related_concepts = [
            f"concepts/{item['slug']}.md"
            for item in inventory["concepts"]
            if section_pages.intersection(item["pdf_pages"])
        ]
        _execute(
            executor,
            f"structure:section-{section['order']:02d}",
            f"STRUCTURE — SECTION {section['order']} OF {len(inventory['sections'])}. "
            f"Write exactly `{path}` (type Section), titled {section['title']!r}. "
            f"{_pages_clause(section['pdf_pages'])} Write from those pages, not from the "
            f"survey summary: {section['summary']}\n\n"
            "Follow the material in source order. Explain the division's function in the "
            "larger inference, preserve its named examples and methodological cautions, "
            "link back to the Chapter hub, link forward to the planned cases and concepts "
            "below, and end with what this section does not establish. A section note is "
            "not a topical abstract; it must preserve the progression and caveats that make "
            "this division distinct.\n\n"
            f"Related planned cases: {related_cases or ['(none)']}\n"
            f"Related planned concepts: {related_concepts or ['(none)']}\n\n"
            f"Append one `{date} [{writer}] structure: ...` log entry.",
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
        listing = "\n".join(
            f"- {path} — {item['title']!r} (PDF pages {item['pdf_pages']})\n"
            f"  narrated: {item['summary']}\n"
            f"  surveyed support and limits: {item['support']}"
            for path, item in zip(paths, items, strict=True)
        )
        _execute(
            executor,
            f"cases:{index}",
            f"CASE DOSSIERS — BATCH {index} OF {len(groups)}. Write exactly the separate "
            "notes listed below; never merge two incidents because they share a theme.\n\n"
            f"{listing}\n\n{_pages_clause(pages)} Read each gateway before writing. The "
            "survey identifies the episode; the page supplies the account. Preserve every "
            "specific the scope gives—names, dates, places, quantities, duration, sequence, "
            "physical details, and uncertainty—rather than generalizing them away.\n\n"
            "Each Case Dossier uses the guide's exact order: attributed `Reported account`; "
            "`Source chain` naming intermediaries and any applicable apparatus entry; `Use "
            "in the argument`; and mandatory `Evidence limits`. The limits section must say "
            "both what support exists and what the scope does not supply: for example no "
            "second account, named investigator, primary record, measurement, date, or "
            "independent source, when that absence is actually visible in the pages. A "
            "briefly narrated incident deserves a brief dossier with a precise limit, not "
            "padding or omission. Finish with Connections and direct page-gateway Sources. "
            f"Append one `{date} [{writer}] cases: ...` log entry.",
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
        listing = "\n".join(
            f"- {path} — {item['title']!r} [{item['kind']}] "
            f"(PDF pages {item['pdf_pages']}): {item['summary']}"
            for path, item in zip(paths, items, strict=True)
        )
        _execute(
            executor,
            f"concepts:{index}",
            f"CONCEPTS — BATCH {index} OF {len(groups)}. Write exactly one distinct note "
            f"for each item below; do not merge mechanisms merely because they co-occur.\n\n"
            f"{listing}\n\n{_pages_clause(pages)} Read those gateways before writing. "
            "Define each idea, explain the work it does in the source's reasoning, and keep "
            "the source's label distinct from any claim about what exists outside the text.\n\n"
            "Every Concept note needs `## Instances in scope`. Run one focused grep over "
            "`cases/` for the concept's vocabulary and close synonyms, read every dossier "
            "that hits, and link every genuine instance with a clause explaining how it "
            "qualifies. Do not list only the examples remembered from the current pages.\n\n"
            "Give each reusable rule one canonical home. If another concept owns the same "
            "method or assumption, link to it instead of repeating a weaker paraphrase. For "
            "a substantive concept, add `Evidence status` instance by instance and distinguish "
            "allegation from support by a document, measurement, or named investigation. For "
            "a methodological or epistemic concept, add `What this licenses and what it does "
            "not` with explicit `Supports`, `Does not support`, and `Controls absent in scope` "
            "lists. State a weak supported inference and the stronger tempting inference the "
            "material does not establish; if the two could be swapped unnoticed, the "
            "guardrail is too vague. Cite direct gateways and append one "
            f"`{date} [{writer}] concepts: ...` log entry.",
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
            f"PEOPLE — DOCUMENT AUTHOR. Write exactly `{path}` (type Person) about "
            f"{person['name']}'s role in this scope, never a biography. "
            f"{_pages_clause(person['pdf_pages'])} Read the gateways and distinguish what "
            "the author compiles from what they interpret, where selection or sequence does "
            "rhetorical work, which cautions the author states, which causal or comparative "
            "moves a reader should audit, and what the selected pages leave unresolved. "
            f"The survey records this role: {person['role']}; and this caution: "
            f"{person['caution']}. Preserve and substantiate both rather than replacing them "
            "with generic prose. Link the Chapter and the synthesis notes that audit these "
            f"moves. Append one `{date} [{writer}] people: ...` log entry.",
            (path,),
            {"kind": "author", "item": person},
        )
    groups = plan.role_groups(inventory)
    for group, members in sorted(groups.items()):
        if group == plan.AUTHOR_GROUP:
            continue
        path = f"people/{group}.md"
        pages = [page for person in members for page in person["pdf_pages"]]
        rows = "\n".join(
            f"- {person['name']} (PDF pages {person['pdf_pages']}): role="
            f"{person['role']!r}; caution={person['caution']!r}"
            for person in members
        )
        _execute(
            executor,
            f"people:{group}",
            f"PEOPLE — ROLE ROSTER. Write exactly `{path}` (type Person) for these "
            f"{len(members)} surveyed people and no others:\n{rows}\n\n"
            f"{_pages_clause(pages)} Read the relevant gateways. Run one focused grep over "
            "`cases/` to locate every appearance of the surveyed names, then write a literal "
            "table with `Name`, `Role in this scope`, `Where they appear` (linked), and `What "
            "to be careful about`. Every surveyed person gets exactly one row. The caution "
            "column is evidential: preserve and expand each surveyed caution into the "
            "person's actual source status—direct statement, reported account, anonymous or "
            "named intermediary, unreproduced record, translation, interpretation, or other "
            "limit the pages support—instead of substituting a generic warning. Below the "
            "table, explain what the role group has in common and how the source's reliance "
            "on it should be read. Add no outside biography or unlisted person. Append one "
            f"`{date} [{writer}] people: ...` log entry.",
            (path,),
            {"kind": "people-roster", "group": group, "items": members},
        )


def stage_synthesis(
    executor: StageExecutor,
    inventory: dict[str, Any],
    date: str,
    writer: str,
) -> None:
    steps = "\n".join(
        f"  {index}. {step}"
        for index, step in enumerate(inventory["chapter"]["argument_steps"], 1)
    )
    claims = "\n".join(
        f"  - claim={item['claim']!r}; owner={item['owner']!r}; "
        f"evidence={item['evidence']!r}; limit={item['limit']!r}"
        for item in inventory["claims"]
    )
    motifs = ", ".join(str(item) for item in inventory["motifs"])
    episodes = "\n".join(
        f"  - cases/{item['slug']}.md — {item['title']}"
        for item in inventory["episodes"]
    )
    tasks = [
        (
            "argument-map",
            "Synthesis",
            "Map the source's inference chain as an ordered numbered list. For every move, "
            "name the in-scope support, the assumption or category shift it requires, and "
            "the section or concept note where a reader can audit it. End at the point where "
            "the argument first outruns its evidence; do not replace the source's sequence "
            f"with a smoother thesis. Surveyed steps:\n{steps}",
        ),
        (
            "claim-evidence-matrix",
            "Synthesis",
            "Build a literal table with Claim, Owner, In-scope evidence, Evidential limit, "
            "and linked Notes columns. Include every surveyed claim below, then add only a "
            "further substantive claim you actually find while reading the existing notes. "
            "The limit column is the point of the table: state the missing record, control, "
            "measurement, independence, or discriminating evidence specifically rather than "
            f"writing `uncertain`. Surveyed claims:\n{claims}",
        ),
        (
            "motif-matrix",
            "Synthesis",
            "Build a literal Markdown table—not a thematic essay—with one linked Case "
            "Dossier per row and one genuinely recurring feature per column. Start from the "
            f"surveyed motifs ({motifs}) and add only features that recur in the dossiers. "
            "Do not let surface appearance crowd out other feature families that the "
            "source uses in comparison, such as bodily or mechanical effects, material "
            "traces, "
            "responses, social consequences, or transmission patterns. Every marked cell is "
            "a claim about that case's cited page: read the dossier or gateway and mark only "
            "what it states; a blank is correct when support is absent. Below the grid, state "
            "how the rows and columns were selected and why recurrence does not by itself "
            "establish one object, mechanism, or cause. Planned case dossiers:\n"
            f"{episodes}",
        ),
        (
            "critical-reading",
            "Synthesis",
            "Set the argument's genuine strengths against the concrete controls it lacks. "
            "Show where each applicable risk bites by linking notes: selection of examples, "
            "heterogeneous source quality, non-independent transmission, translation or "
            "category drift, compression of records into summary, missing comparison sets, "
            "and absent measurements. This is an audit rather than a dismissal, so state "
            "what the source's method can validly establish before stating what it cannot.",
        ),
        (
            "open-questions",
            "Open Question",
            "State the material and methodological questions this scope leaves unresolved. "
            "For each, name the primary record, operational definition, comparison set, "
            "independence check, or discriminating observation that could settle it, and link "
            "the note that exposes the gap. Do not answer from later divisions, list wiki "
            "backlog, or merely repeat the source's rhetorical questions.",
        ),
    ]
    for index, (slug, note_type, instruction) in enumerate(tasks, 1):
        path = f"synthesis/{slug}.md"
        _execute(
            executor,
            f"synthesis:{slug}",
            f"SYNTHESIS — TASK {index} OF {len(tasks)}. Write exactly `{path}` (type "
            f"{note_type}). Synthesis analyses the source's reasoning; it does not restate "
            "the scope or invent a verdict. Use `ls` and focused `grep` over the existing "
            "Chapter, Section, Case Dossier, Concept, and Person notes, follow their direct "
            "gateway links, and cite only pages you read. Link the notes that let a reader "
            f"audit every row or inference.\n\n{instruction}\n\nAppend one "
            f"`{date} [{writer}] synthesis: ...` log entry.",
            (path,),
            {"kind": "synthesis", "slug": slug, "note_type": note_type},
        )


def stage_navigation(
    executor: StageExecutor,
    inventory: dict[str, Any],
    date: str,
    writer: str,
) -> None:
    chapter = inventory["chapter"]
    chapter_path = f"chapters/{chapter['slug']}.md"
    argument = "\n".join(
        f"  {index}. {step}" for index, step in enumerate(chapter["argument_steps"], 1)
    )
    sections = "\n".join(
        f"  {item['order']}. sections/{item['slug']}.md — {item['title']}"
        for item in inventory["sections"]
    )
    _execute(
        executor,
        "navigation:overview",
        "NAVIGATION — OVERVIEW. Every semantic note now exists. Use `ls` on every folder "
        "and read the Chapter, synthesis, and source-guide notes before writing exactly "
        "`overview.md` (type Overview), the single orientation note for the whole scope.\n\n"
        f"State the organizing question: {chapter['organizing_question']}\n\n"
        "Give the source's argument as a short numbered list grounded in the surveyed "
        "sequence below, not as a new thesis. Preserve the major categories of evidence and "
        "recurring effects the existing notes treat as load-bearing. Then give an explicit "
        "`What this establishes` versus `What it leaves open` pair. State the neutral-reading "
        "rule concretely: reported accounts are not verified events; recurring descriptions "
        "do not alone establish a shared mechanism; and a source's labels do not by "
        "themselves settle ontology, wherever those distinctions apply. Link the Chapter, "
        "all five synthesis notes, source-and-scope, extraction-and-audit, and the page map.\n\n"
        f"Surveyed argument sequence:\n{argument}\n\n"
        f"Append one `{date} [{writer}] navigation: ...` log entry.",
        ("overview.md",),
        {"kind": "overview", "inventory": inventory},
    )
    _execute(
        executor,
        "navigation:reading-guide",
        "NAVIGATION — READING GUIDE. Use `ls` on every folder so every path below resolves, "
        "then write exactly `reading-guide.md` (type Reading Guide). This note explains how "
        "to use the wiki through concrete linked routes rather than describing its contents.\n\n"
        f"First-read route: `{chapter_path}`, then these Section notes in source order:\n"
        f"{sections}\n\n"
        "Also provide: a case lookup route through `cases/index.md`; a concept and method "
        "route through `concepts/index.md`; a recurrence route through "
        "`synthesis/motif-matrix.md`; a claim-audit route from "
        "`synthesis/claim-evidence-matrix.md` to a linked note and then its direct PDF-page "
        "gateway; a critical route through the argument map, critical reading, and open "
        "questions; and a page-first route through `reference/pdf-page-map.md`. Explain that "
        "a Case Dossier separates reported account, use, source chain, and limits. End with "
        "an exact scope-boundary warning: inclusion is not verification, unscoped material "
        "cannot fill a gap, and the source-and-scope guide is authoritative. "
        f"Append one `{date} [{writer}] navigation: ...` log entry.",
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
    allowed_root_files = (
        set(lint.ROOT_TYPES)
        | lint.SPECIAL
        | {
            "checksums.sha256",
            "manifest.json",
        }
    )
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
        int(page) for page in profile["primary_pages"] + profile["apparatus_pages"]
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
