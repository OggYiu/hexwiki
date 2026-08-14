"""Independent source review isolated from every drafting context."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Protocol

from .audit import AuditLog
from .config import RuntimeConfig, TranscriptRecorder, langchain_callbacks
from .lint import GATEWAY_PARTS, SKIP_DIRS, parse_frontmatter


REVIEWER_ID = "hexwiki-independent-source-review-v1"
NOTES_PER_PACKET = 5
NOTE_CHARACTER_LIMIT = 6_000
MAX_PAGES_PER_PACKET = 12
PACKET_CHARACTER_BUDGET = 50_400
HALVE_PACKET_FROM_ATTEMPT = 4

REVIEWER_SYSTEM = """You are an independent reviewer auditing a machine-compiled,
source-bounded wiki. You did not write these notes. You receive only finished notes,
the complete declared PDF-page scope, and whole verbatim source pages.

Report a MATERIAL FINDING only for one of these:
- unsupported-claim: a supplied page does not support what the note states.
- misattribution: the note assigns a statement to the wrong source or treats a report
  as established fact.
- missing-evidence-limits: a case dossier does not say what support is absent.
- wrong-locator: a cited page does not contain what it is cited for.
- out-of-scope: a cited page is absent from the complete declared scope list.

An in-scope page omitted from this packet is not a finding: do not judge claims that
rest on evidence you were not supplied. Do not report style preferences, desired
expansion, or uncertainty. Reply with JSON only:
{"findings": [{"note": "<wiki path>", "kind": "<kind>",
"detail": "<specific defect>", "quote": "<offending text>"}]}
Return {"findings": []} when the supplied evidence reveals no material defect."""

RELEASE_SYSTEM = """You are the release reviewer for a source-bounded wiki. Decide
whether the supplied build evidence permits publication as an unverified draft.
Release requires deterministic validators to pass, independent review to execute
with complete coverage and no material findings, every semantic note to remain a
draft without a verified field, and the scope declaration to match locked evidence.
Reply with JSON only:
{"status": "clear" | "blocked", "rationale": "<two sentences>",
"concerns": ["<blocking concern>", ...]}"""

GATEWAY_TEXT_RE = re.compile(r"(?ms)^````text\s*$(.*?)^````\s*$")


class ReviewClient(Protocol):
    def ask(self, *, system: str, user: str, label: str) -> dict[str, Any]: ...


def _json_object(text: str, label: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError(f"{label}: reviewer returned no JSON object")


class OpenAIReviewClient:
    """Fresh, tool-free reviewer calls through the configured compatible route."""

    def __init__(
        self,
        runtime: RuntimeConfig,
        recorder: TranscriptRecorder,
    ) -> None:
        self.runtime = runtime
        self.recorder = recorder

    def ask(self, *, system: str, user: str, label: str) -> dict[str, Any]:
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as error:
            raise RuntimeError(
                "model runtime is unavailable; install HexWiki with the 'model' extra"
            ) from error
        self.recorder.append(
            "review-events",
            {"event": "review-request", "stage": label, "system": system, "user": user},
        )
        client = ChatOpenAI(
            base_url=self.runtime.base_url,
            api_key=self.runtime.api_key,
            model=self.runtime.model,
            timeout=600,
            max_retries=0,
            streaming=False,
            temperature=0,
        )
        response = client.invoke(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            config={"callbacks": langchain_callbacks(self.runtime, self.recorder)},
        )
        text = response.content if isinstance(response.content, str) else str(response.content)
        self.recorder.append(
            "review-events",
            {"event": "review-response", "stage": label, "response": text},
        )
        return _json_object(text, label)


def semantic_notes(wiki_dir: Path) -> list[Path]:
    notes: list[Path] = []
    for path in sorted(Path(wiki_dir).rglob("*.md")):
        relative = path.relative_to(wiki_dir)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        if relative.parts[:2] == GATEWAY_PARTS or relative.name == "index.md":
            continue
        metadata, _ = parse_frontmatter(path.read_text(encoding="utf-8-sig"))
        if metadata.get("semantic_note") is True:
            notes.append(path)
    return notes


def load_pages_from_gateways(
    wiki_dir: Path,
    scope: set[int],
) -> dict[int, dict[str, Any]]:
    pages: dict[int, dict[str, Any]] = {}
    for page in sorted(scope):
        path = Path(wiki_dir) / "sources" / "pdf-pages" / f"page-{page:04d}.md"
        if not path.is_file():
            continue
        match = GATEWAY_TEXT_RE.search(path.read_text(encoding="utf-8-sig"))
        if match:
            pages[page] = {"text": match.group(1).strip()}
    return pages


def _cited_pages(text: str, scope: set[int]) -> list[int]:
    found = {
        int(value)
        for value in re.findall(r"(?:sources/)?pdf-pages/page-(\d{4})\.md", text)
    }
    return sorted(found & scope)


def packets(
    wiki_dir: Path,
    scope: set[int],
    only_notes: set[str] | None = None,
) -> list[list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    for path in semantic_notes(Path(wiki_dir)):
        relative = path.relative_to(wiki_dir).as_posix()
        if only_notes is not None and relative not in only_notes:
            continue
        text = path.read_text(encoding="utf-8-sig")
        records.append(
            {
                "path": relative,
                "text": text[:NOTE_CHARACTER_LIMIT],
                "pages": _cited_pages(text, scope),
            }
        )
    ranked = sorted(
        records,
        key=lambda item: (item["pages"][0] if item["pages"] else 10**9, item["path"]),
    )
    return [
        sorted(ranked[offset : offset + NOTES_PER_PACKET], key=lambda item: item["path"])
        for offset in range(0, len(ranked), NOTES_PER_PACKET)
    ]


def packet_pages(
    group: list[dict[str, Any]],
    pages: dict[int, dict[str, Any]],
) -> list[int]:
    """Choose evidence by total budget while never truncating a source page."""
    demand: dict[int, int] = {}
    for record in group:
        for page in record["pages"]:
            if page in pages:
                demand[page] = demand.get(page, 0) + 1
    ranked = sorted(demand, key=lambda page: (-demand[page], page))[
        :MAX_PAGES_PER_PACKET
    ]
    chosen: list[int] = []
    remaining = PACKET_CHARACTER_BUDGET
    for page in ranked:
        cost = len(pages[page]["text"])
        if chosen and cost > remaining:
            continue
        # A single unusually long page is supplied whole.  The character budget
        # is a packet-sizing aid, never permission to silently remove evidence.
        chosen.append(page)
        remaining = max(0, remaining - cost)
    if not chosen and pages:
        chosen = [sorted(pages)[0]]
    return sorted(chosen)


def packet_prompt(
    group: list[dict[str, Any]],
    pages: dict[int, dict[str, Any]],
    profile: dict[str, Any],
) -> tuple[str, list[int]]:
    supplied = packet_pages(group, pages)
    declared_scope = sorted(
        int(page)
        for page in profile["primary_pages"] + profile["apparatus_pages"]
    )
    omitted = sorted(
        {
            page
            for record in group
            for page in record["pages"]
            if page not in supplied
        }
    )
    parts = [
        f"Document: {profile['document_title']} ({profile['document_author']}).",
        f"Declared scope: {profile['scope_label']}.",
        f"Every PDF page in the declared scope: {declared_scope}.",
        f"Whole pages supplied below: {supplied}.",
        "",
        "A page in the declared list but not supplied is not out of scope and is not a "
        "finding. Do not judge claims resting on evidence you cannot see.",
    ]
    if omitted:
        parts.append(
            f"In-scope cited pages intentionally omitted from this packet: {omitted}."
        )
    parts.append("\n=== SOURCE PAGES (whole, verbatim, authoritative) ===")
    for page in supplied:
        parts.append(f"\n--- PDF page {page} ---\n{pages[page]['text']}")
    parts.append("\n=== NOTES UNDER REVIEW ===")
    for record in group:
        parts.append(
            f"\n--- {record['path']} (cites {record['pages']}) ---\n{record['text']}"
        )
    parts.append(
        "\nAudit only against supplied evidence and use exact wiki paths in findings."
    )
    return "\n".join(parts), supplied


def run_independent_review(
    *,
    wiki_dir: Path,
    pages: dict[int, dict[str, Any]],
    profile: dict[str, Any],
    audit: AuditLog,
    client: ReviewClient,
    attempts: int,
    retry_seconds: tuple[int, ...],
    round_label: str = "1",
    max_packets: int | None = None,
    only_notes: set[str] | None = None,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    scope = {
        int(page)
        for page in profile["primary_pages"] + profile["apparatus_pages"]
    }
    groups = packets(wiki_dir, scope, only_notes)
    skipped = 0
    if max_packets is not None and len(groups) > max_packets:
        skipped = len(groups) - max_packets
        groups = groups[:max_packets]
    records: list[dict[str, Any]] = []
    execution_status = "passed"
    reduced_notes = 0
    for index, original_group in enumerate(groups, 1):
        group = original_group
        findings: list[Any] | None = None
        supplied: list[int] = []
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if attempt >= HALVE_PACKET_FROM_ATTEMPT and len(original_group) > 1:
                group = original_group[: max(1, len(original_group) // 2)]
            prompt, supplied = packet_prompt(group, pages, profile)
            label = f"independent-review-{round_label}-packet-{index:02d}"
            if attempt > 1:
                label += f"-retry-{attempt}"
            try:
                result = client.ask(system=REVIEWER_SYSTEM, user=prompt, label=label)
                value = result.get("findings", [])
                if not isinstance(value, list):
                    raise ValueError("review findings must be a list")
                findings = value
                break
            except Exception as error:
                last_error = error
                if attempt < attempts:
                    sleep(retry_seconds[attempt - 1])
        if findings is None:
            execution_status = "failed"
            records.append(
                {
                    "packet": index,
                    "notes": [item["path"] for item in original_group],
                    "findings": [],
                    "attempts": attempts,
                    "error": f"{type(last_error).__name__}: {last_error}",
                }
            )
            continue
        omitted_notes = [
            item["path"] for item in original_group if item["path"] not in {x["path"] for x in group}
        ]
        reduced_notes += len(omitted_notes)
        records.append(
            {
                "packet": index,
                "notes": [item["path"] for item in group],
                "notes_omitted_after_reduction": omitted_notes,
                "pages_supplied": supplied,
                "pages_cited": sorted(
                    {page for item in group for page in item["pages"]}
                ),
                "pages_shown_in_part": [],
                "findings": findings,
            }
        )

    total = sum(len(record["findings"]) for record in records)
    reviewed = sorted(
        {page for record in records for page in record.get("pages_supplied", [])}
    )
    cited = sorted(
        {page for record in records for page in record.get("pages_cited", [])}
    )
    unreviewed = sorted(set(cited) - set(reviewed))
    coverage = "partial" if skipped or reduced_notes else "complete"
    report = {
        "reviewer": REVIEWER_ID,
        "round": round_label,
        "pages_reviewed_this_round": reviewed,
        "pages_cited_but_never_supplied_this_round": unreviewed,
        "round_scope_is_narrow": bool(only_notes),
        "execution_status": execution_status,
        "finding_status": (
            "clear" if execution_status == "passed" and total == 0 else "findings"
        ),
        "packet_count": len(records),
        "note_count": sum(len(record.get("notes", [])) for record in records),
        "material_findings": total,
        "packets": records,
        "packets_skipped": skipped,
        "notes_omitted_after_reduction": reduced_notes,
        "coverage": coverage,
        "isolated_from_drafting_context": True,
        "scope_of_round": "repaired notes only" if only_notes else "all notes",
    }
    audit.record(
        phase="review",
        action="run_independent_source_review",
        what=(
            f"Audited {report['note_count']} notes in {len(records)} isolated packets."
        ),
        why="Successful drafting is not evidence that the resulting claims are supported.",
        how=(
            "Used a fresh tool-free reviewer context with finished notes, the complete "
            "declared scope, and selected whole canonical source pages."
        ),
        status="passed" if execution_status == "passed" else "failed",
        details={
            "round": round_label,
            "material_findings": total,
            "coverage": coverage,
            "pages_reviewed": reviewed,
        },
    )
    return report


def run_release_review(
    *,
    evidence: dict[str, Any],
    audit: AuditLog,
    client: ReviewClient,
    attempts: int,
    retry_seconds: tuple[int, ...],
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    user = "Build evidence:\n" + json.dumps(evidence, ensure_ascii=False, indent=2)
    verdict: dict[str, Any] | None = None
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        label = "release-review" if attempt == 1 else f"release-review-retry-{attempt}"
        try:
            value = client.ask(system=RELEASE_SYSTEM, user=user, label=label)
            if value.get("status") not in {"clear", "blocked"}:
                raise ValueError(f"unexpected release status {value.get('status')!r}")
            concerns = value.get("concerns", [])
            if not isinstance(concerns, list):
                raise ValueError("release concerns must be a list")
            verdict = value
            break
        except Exception as error:
            last_error = error
            if attempt < attempts:
                sleep(retry_seconds[attempt - 1])
    if verdict is None:
        verdict = {
            "status": "blocked",
            "rationale": (
                "The release review did not execute successfully: "
                f"{type(last_error).__name__}: {last_error}"
            ),
            "concerns": ["release review did not execute"],
        }
    report = {"reviewer": REVIEWER_ID, "release_review": verdict, "evidence": evidence}
    audit.record(
        phase="review",
        action="run_release_review",
        what=f"Recorded a release verdict of {verdict['status']!r}.",
        why="Release is an explicit evidence-backed decision, not an inferred exit code.",
        how="Used a fresh tool-free context and stored its returned verdict verbatim.",
        status="passed" if verdict["status"] == "clear" else "failed",
        details={"status": verdict["status"], "concerns": verdict.get("concerns", [])},
    )
    return report


def findings_prompt(findings: list[dict[str, Any]], date: str, writer: str) -> str:
    lines = [
        f"- {item.get('note')} [{item.get('kind')}] - {item.get('detail')}"
        + (f"\n  Offending text: {item.get('quote')}" if item.get("quote") else "")
        for item in findings
    ]
    return (
        "INDEPENDENT REVIEW REPAIR. Fix every finding by making the note match its "
        "immutable source gateway. Read the note and every gateway it cites before the "
        "edit, then re-read both. Delete an unsupportable claim instead of searching for "
        "new evidence. Correct attribution precisely, add explicit absent support to case "
        "evidence limits, replace wrong locators, and remove out-of-scope material. Do not "
        "create notes or edit source infrastructure.\n\n"
        + "\n".join(lines)
        + f"\n\nAppend one log entry: '- {date} [{writer}] review-repair: ...'. "
        "Finish by listing each replacement sentence you verified against its gateway."
    )
