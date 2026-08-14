"""Deterministic wiki infrastructure, sealing, and checksum verification."""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from hexwiki import __version__

from . import lint, okf, source, sourceguide
from .audit import AuditLog, atomic_json, atomic_text, sha256_file
from .profile import DocumentProfile, sha256_json


STRUCTURAL_NAMES = {"AGENTS.md", "README.md", "WIKI_GUIDE.md", "index.md", "log.md"}
CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")


def _yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _gateway_text(page: int, item: dict[str, Any], profile: dict[str, Any]) -> str:
    if item["section"] == "primary_text":
        section = f"{profile['scope_title']} primary text"
    else:
        section = f"{profile['scope_title']} {profile['apparatus_label']}"
    return (
        "---\n"
        "type: Source\n"
        f"title: {_yaml_quote(profile['document_title'] + f' — PDF page {page}')}\n"
        f"description: {_yaml_quote(f'Canonical native text for PDF page {page}, limited to the declared {section.lower()} scope.')}\n"
        f"tags: [sources, {profile['document_id']}, {profile['scope_id']}, source-page]\n"
        "---\n\n"
        f"# {profile['document_title']} — PDF page {page}\n\n"
        "**Canonical native extraction used by this source-bounded run.**\n\n"
        f"Scope: {section}. Boundary action: `{item['boundary_action']}`. "
        "The native PDF text is the authoritative generation layer; the extraction "
        "bundle retains redundant layers for audit.\n\n"
        "[Return to the source-page index](index.md).\n\n"
        "## Scoped native text\n\n"
        "````text\n"
        f"{item['text'].rstrip()}\n"
        "````\n\n"
        "## Sources\n\n"
        f"- *{profile['document_title']}* ({profile['document_author']}), PDF page {page} — "
        "canonical scoped native text; source PDF SHA-256 "
        f"`{profile['source_pdf_sha256']}`.\n"
    )


def prepare_wiki(
    *,
    wiki_dir: Path,
    pages: dict[int, dict[str, Any]],
    profile: dict[str, Any],
    audit: AuditLog,
    run_id: str,
    extraction_root: Path,
) -> None:
    """Create fresh deterministic source infrastructure for a candidate wiki."""
    wiki_dir = Path(wiki_dir)
    if wiki_dir.exists():
        raise FileExistsError(wiki_dir)
    gateway_dir = wiki_dir / "sources" / "pdf-pages"
    gateway_dir.mkdir(parents=True)
    guide = resources.files("hexwiki.resources").joinpath("guide.md").read_text(
        encoding="utf-8"
    )
    atomic_text(wiki_dir / "WIKI_GUIDE.md", guide)

    ordered = [
        int(value) for value in profile["primary_pages"] + profile["apparatus_pages"]
    ]
    for page in ordered:
        atomic_text(
            gateway_dir / f"page-{page:04d}.md",
            _gateway_text(page, pages[page], profile),
        )
    gateway_index = [
        f"# {profile['document_title']} {profile['scope_label']} source pages",
        "",
        "These gateways expose the exact scoped native text used by this run. "
        "Catalog inclusion is not verification.",
        "",
    ]
    gateway_index.extend(
        f"- [PDF page {page}](page-{page:04d}.md)" for page in ordered
    )
    atomic_text(gateway_dir / "index.md", "\n".join(gateway_index) + "\n")

    sourceguide.build_source_guides(
        wiki_dir=wiki_dir,
        pages=pages,
        profile=profile,
        extraction_root=Path(extraction_root),
        audit=audit,
    )

    root_index = [
        f"This source-bounded wiki covers {profile['document_title']} "
        f"{profile['scope_label']}. It was compiled from the locked extraction. "
        "Inclusion is not verification and generated semantic notes are unverified drafts.",
        "",
        "## Source guides",
        "",
        f"- [{profile['scope_title']} source and scope](sources/source-and-scope.md) — "
        "exact inclusion and exclusion boundaries.",
        f"- [{profile['scope_title']} provenance and limitations]"
        "(sources/provenance-and-limitations.md) — evidence layers and known anomalies.",
        f"- [{profile['scope_title']} extraction and audit]"
        "(sources/extraction-and-audit.md) — verification chain and audit path.",
    ]
    if source.has_apparatus(profile):
        root_index.append(
            f"- [{profile['scope_title']} {profile['apparatus_label']}]"
            f"(sources/{profile['apparatus_slug']}.md) — verbatim citation inventory."
        )
    root_index.extend(
        [
            "",
            "## Reference",
            "",
            f"- [{profile['scope_title']} PDF page map](reference/pdf-page-map.md) — "
            "one row per scoped page.",
            "",
            "## Source pages",
            "",
        ]
    )
    root_index.extend(
        f"- [{profile['document_title']} — PDF page {page}]"
        f"(sources/pdf-pages/page-{page:04d}.md) — exact scoped native text."
        for page in ordered
    )
    atomic_text(wiki_dir / "index.md", "\n".join(root_index) + "\n")
    date = datetime.now().astimezone().date().isoformat()
    atomic_text(
        wiki_dir / "log.md",
        f"- {date} [hexwiki/{__version__}] source-stage: created {len(ordered)} "
        f"exact PDF-page gateways and deterministic source guides for run {run_id}.\n",
    )
    audit.record(
        phase="wiki",
        action="prepare_source_grounded_wiki",
        what=(
            f"Created a fresh candidate with {len(ordered)} exact source-page gateways, "
            "deterministic source guides, a root catalog, and an append-only log."
        ),
        why="Every later note needs a direct path to canonical current-run evidence.",
        how=(
            "Rendered installed package resources and machine-derived notes from the "
            "already verified scoped extraction into an explicitly named new directory."
        ),
        details={"wiki_dir": ".", "pages": ordered},
    )


def _write_vault_structure(
    wiki_dir: Path, run_id: str, profile: dict[str, Any]
) -> None:
    atomic_text(
        wiki_dir / "AGENTS.md",
        "# Generated wiki instructions\n\n"
        "Treat this sealed vault as immutable. Claims are source-bounded drafts, not "
        "verification. Start at `index.md`, use the source guides for audit paths, and "
        "follow `_schema/okf-v0.2.md` for note metadata.\n",
    )
    atomic_text(
        wiki_dir / "README.md",
        f"# {profile['document_title']} — {profile['scope_label']} wiki\n\n"
        f"Source-bounded HexWiki run `{run_id}`. Start at `index.md`; inclusion is not "
        "verification.\n",
    )
    atomic_text(
        wiki_dir / "_schema" / "okf-v0.2.md",
        "# Open Knowledge Format 0.2 profile\n\n"
        "Compiled notes carry YAML front matter with `type`, an exact `title`, a "
        "retrieval-oriented `description`, non-empty `tags`, a non-empty mapping-based "
        "`sources` list, and the epistemic constants `semantic_note: true` and "
        "`status: draft`. No newly compiled note carries a `verified` field: the format "
        "signals the unverified tier by its absence. `index.md` is the catalog and "
        "`log.md` is append-only. Every compiled note links to canonical source pages.\n",
    )


def _validate_gateway_inventory(
    wiki_dir: Path,
    pages: dict[int, dict[str, Any]],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    ordered = profile["primary_pages"] + profile["apparatus_pages"]
    for page in [int(value) for value in ordered]:
        path = wiki_dir / "sources" / "pdf-pages" / f"page-{page:04d}.md"
        expected = _gateway_text(page, pages[page], profile)
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            raise ValueError(f"source gateway changed or is missing: {path}")
        records.append(
            {
                "pdf_page": page,
                "wiki_path": path.relative_to(wiki_dir).as_posix(),
                "scoped_text_sha256": pages[page]["sha256"],
                "gateway_sha256": sha256_file(path),
                "boundary_action": pages[page]["boundary_action"],
            }
        )
    return records


def checksum_inventory(wiki_dir: Path) -> str:
    lines: list[str] = []
    for path in sorted(wiki_dir.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file() or path.name == "checksums.sha256" or ".obsidian" in path.parts:
            continue
        lines.append(f"{sha256_file(path)}  {path.relative_to(wiki_dir).as_posix()}")
    return "\n".join(lines) + "\n"


def verify_checksums(wiki_dir: Path) -> dict[str, str]:
    """Verify both contents and membership of a sealed wiki tree."""
    wiki_dir = Path(wiki_dir)
    inventory = wiki_dir / "checksums.sha256"
    if not inventory.is_file():
        raise ValueError("checksums.sha256 is missing")
    expected: dict[str, str] = {}
    for line_number, line in enumerate(
        inventory.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        digest, separator, relative = line.partition("  ")
        posix = PurePosixPath(relative)
        if (
            not separator
            or not CHECKSUM_RE.fullmatch(digest)
            or posix.is_absolute()
            or ".." in posix.parts
            or not relative
        ):
            raise ValueError(f"invalid checksum inventory line {line_number}: {line!r}")
        if relative in expected:
            raise ValueError(f"duplicate checksum inventory path: {relative}")
        expected[relative] = digest
    actual = {
        path.relative_to(wiki_dir).as_posix(): sha256_file(path)
        for path in wiki_dir.rglob("*")
        if path.is_file() and path.name != "checksums.sha256" and ".obsidian" not in path.parts
    }
    if expected != actual:
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(
            path for path in set(expected).intersection(actual) if expected[path] != actual[path]
        )
        raise ValueError(
            "checksums.sha256 does not match the sealed wiki "
            f"(missing={missing}, extra={extra}, changed={changed})"
        )
    return expected


def _copy_audit_and_checksum(wiki_dir: Path, audit: AuditLog) -> None:
    destination = wiki_dir / "audit" / "actions.jsonl"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if audit.path.resolve() != destination.resolve():
        if destination.exists():
            raise FileExistsError(destination)
        shutil.copy2(audit.path, destination)
    atomic_text(wiki_dir / "checksums.sha256", checksum_inventory(wiki_dir))
    verify_checksums(wiki_dir)


def seal_wiki(
    *,
    wiki_dir: Path,
    profile: DocumentProfile,
    lock: dict[str, Any],
    audit: AuditLog,
    run_id: str,
    source_manifest: dict[str, Any],
    model_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-verify source identity, run offline gates, and seal a fresh candidate."""
    wiki_dir = Path(wiki_dir)
    if not wiki_dir.is_dir():
        raise FileNotFoundError(wiki_dir)
    for reserved in (wiki_dir / "manifest.json", wiki_dir / "checksums.sha256"):
        if reserved.exists():
            raise FileExistsError(reserved)

    inspected = source.verify_profile_lock(profile, lock)
    runtime = profile.runtime(lock)
    gateway_records = _validate_gateway_inventory(wiki_dir, inspected["pages"], runtime)
    _write_vault_structure(wiki_dir, run_id, runtime)

    lint_errors = lint.lint(wiki_dir)
    okf_report = okf.check_directory(wiki_dir)
    okf_errors = okf.n_errors_of(okf_report["issues"])
    model_errors: list[str] = []
    if model_evidence is not None:
        mode = model_evidence.get("mode")
        if mode not in {"smoke", "build"}:
            model_errors.append("model evidence mode must be 'smoke' or 'build'")
        independent = model_evidence.get("independent_review")
        if not isinstance(independent, dict):
            model_errors.append("independent review evidence is missing")
        elif independent.get("execution_status") != "passed":
            model_errors.append("independent review did not execute successfully")
        if mode == "build" and isinstance(independent, dict):
            if independent.get("finding_status") != "clear":
                model_errors.append("independent review has material findings")
            if independent.get("material_findings") != 0:
                model_errors.append("independent review finding count is not zero")
            if independent.get("page_coverage") != "complete":
                model_errors.append("independent review page coverage is incomplete")
            if independent.get("coverage_across_rounds") != "complete":
                model_errors.append("independent review note/packet coverage is incomplete")
            release = model_evidence.get("release_review")
            if not isinstance(release, dict) or release.get("status") != "clear":
                model_errors.append("release review did not clear the candidate")

    validation = {
        "status": (
            "passed"
            if not lint_errors and okf_errors == 0 and not model_errors
            else "failed"
        ),
        "level": "model-reviewed" if model_evidence is not None else "deterministic",
        "semantic_selection_quality": (
            "independent source support reviewed; reference alignment not measured"
            if model_evidence is not None
            else "not measured by offline gates"
        ),
        "gateways": gateway_records,
        "lint_errors": lint_errors,
        "okf": okf_report,
        "model_errors": model_errors,
    }
    reports = wiki_dir / "reports"
    atomic_json(reports / "deterministic-validation.json", validation)
    if validation["status"] != "passed":
        raise ValueError(
            f"deterministic wiki validation failed: lint={len(lint_errors)}, "
            f"okf={okf_errors}, model={len(model_errors)}; "
            f"see {reports / 'deterministic-validation.json'}"
        )

    manifest = {
        "status": "sealed",
        "validation_level": validation["level"],
        "semantic_selection_quality": validation["semantic_selection_quality"],
        "run_id": run_id,
        "hexwiki_version": __version__,
        "profile_id": profile.data["profile_id"],
        "profile_sha256": profile.profile_sha256,
        "profile_lock_sha256": sha256_json(lock),
        "source_pdf_sha256": lock["source"]["pdf_sha256"],
        "canonical_scope_sha256": lock["scope"]["canonical_sha256"],
        "scoped_pages": runtime["primary_pages"] + runtime["apparatus_pages"],
        "source_stage_sha256": source_manifest["raw_stage_sha256"],
        "deterministic_validation": "reports/deterministic-validation.json",
    }
    if model_evidence is not None:
        manifest["model_run"] = {
            "mode": model_evidence["mode"],
            "binding_sha256": model_evidence["binding_sha256"],
            "route": model_evidence["route"],
            "metrics": model_evidence.get("metrics", {}),
            "smoke_report_sha256": model_evidence.get("smoke_report_sha256"),
            "independent_review": "reports/independent-review.json",
            "release_review": "reports/release-review.json",
        }
    atomic_json(wiki_dir / "manifest.json", manifest)
    audit.record(
        phase="validation",
        action="seal_deterministic_wiki",
        what=(
            f"Sealed {len(gateway_records)} source gateways with clean lint, OKF, "
            "manifest, audit, and full-tree checksums."
        ),
        why="A portable wiki needs verifiable identity and integrity independent of a model.",
        how=(
            "Recomputed the profile lock, byte-compared every gateway, ran deterministic "
            "lint and OKF checks, wrote the manifest and report, copied the append-only "
            "audit, and hashed every file in the completed tree."
        ),
        details={
            "profile_lock_sha256": manifest["profile_lock_sha256"],
            "gateway_count": len(gateway_records),
            "lint_errors": 0,
            "okf_errors": 0,
        },
    )
    _copy_audit_and_checksum(wiki_dir, audit)
    return manifest


def publish_candidate(candidate: Path, output: Path) -> Path:
    """Atomically rename a sealed candidate to an explicit, absent destination."""
    candidate = Path(candidate).resolve()
    output = Path(output).resolve()
    if not candidate.is_dir():
        raise FileNotFoundError(candidate)
    if output.exists():
        raise FileExistsError(output)
    verify_checksums(candidate)
    output.parent.mkdir(parents=True, exist_ok=True)
    os.rename(candidate, output)
    return output
