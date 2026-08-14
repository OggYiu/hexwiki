"""Create, validate, and lock portable document profiles."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

from hexwiki.engine import source
from hexwiki.engine.audit import exclusive_json
from hexwiki.engine.profile import (
    PROFILE_SCHEMA_VERSION,
    REQUIRED_NOTE_TYPES,
    load_profile,
    validate_profile,
)


def _slugify(value: str, fallback: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return candidate or fallback


def _portable_path(target: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(target, base)).as_posix()
    except ValueError:
        return str(target)


def _page_spec(value: str) -> list[int]:
    pages: set[int] = set()
    try:
        for part in value.split(","):
            item = part.strip()
            if not item:
                continue
            if "-" in item:
                first_text, last_text = item.split("-", 1)
                first, last = int(first_text), int(last_text)
                if first < 1 or last < first:
                    raise ValueError
                pages.update(range(first, last + 1))
            else:
                page = int(item)
                if page < 1:
                    raise ValueError
                pages.add(page)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "pages must look like '1-4,7,9-10' with positive ascending ranges"
        ) from error
    if not pages:
        raise argparse.ArgumentTypeError("at least one page is required")
    return sorted(pages)


def _entry_range(value: str) -> list[int]:
    try:
        first_text, last_text = value.split("-", 1)
        first, last = int(first_text), int(last_text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("entry range must look like '1-12'") from error
    if first < 1 or last < first:
        raise argparse.ArgumentTypeError("entry range must be positive and ascending")
    return [first, last]


def _read_extraction(extraction: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = extraction / "manifest.json"
    metadata_path = extraction / "metadata.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    if not metadata_path.is_file():
        raise FileNotFoundError(metadata_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "passed":
        raise ValueError("profile init requires an extraction whose manifest reports passed")
    return manifest, metadata


def _infer_pdf(
    *, extraction: Path, manifest: dict[str, Any], explicit: Path | None
) -> Path:
    if explicit is not None:
        return explicit.resolve()
    recorded = manifest.get("source", {}).get("path")
    if not isinstance(recorded, str) or not recorded.strip():
        raise ValueError("extraction manifest does not name its source; pass --pdf")
    candidate = Path(recorded)
    if candidate.is_absolute():
        return candidate.resolve()
    beside_extraction = (extraction.parent / candidate).resolve()
    if beside_extraction.is_file():
        return beside_extraction
    raise ValueError(
        "the extraction records only a portable PDF filename that is not beside the "
        "extraction directory; pass --pdf explicitly"
    )


def _initial_profile(args: argparse.Namespace) -> dict[str, Any]:
    extraction = args.extraction.resolve()
    manifest, metadata = _read_extraction(extraction)
    pdf = _infer_pdf(extraction=extraction, manifest=manifest, explicit=args.pdf)
    if not pdf.is_file():
        raise FileNotFoundError(pdf)
    page_count = manifest.get("counts", {}).get("pdf_pages")
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        raise ValueError("extraction manifest has no valid PDF page count")

    pdf_metadata = metadata.get("pdf", {}).get("metadata", {})
    title = str(pdf_metadata.get("title") or pdf.stem.replace("_", " ")).strip()
    author = str(pdf_metadata.get("author") or "Unknown author").strip()
    document_id = _slugify(title, "document")
    apparatus_pages = list(args.apparatus_pages or [])
    primary_pages = list(args.pages or range(1, page_count + 1))
    overlap = sorted(set(primary_pages).intersection(apparatus_pages))
    if overlap:
        raise ValueError(
            "--pages and --apparatus-pages must be disjoint; overlapping pages: "
            + ", ".join(str(page) for page in overlap)
        )
    if any(page > page_count for page in primary_pages + apparatus_pages):
        raise ValueError(f"a selected page exceeds the extraction's {page_count} pages")
    if bool(apparatus_pages) != bool(args.apparatus_range):
        raise ValueError(
            "--apparatus-pages and --apparatus-range must be supplied together"
        )

    apparatus = None
    apparatus_banner = None
    if apparatus_pages:
        apparatus = {
            "id": args.apparatus_id,
            "label": args.apparatus_label,
            "entry_range": args.apparatus_range,
        }
        if args.apparatus_entry_pattern:
            apparatus["entry_pattern"] = args.apparatus_entry_pattern
        apparatus_banner = "=== CITATION APPARATUS ==="

    output_parent = args.output.resolve().parent
    value = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": f"{document_id}-selected-scope",
        "source": {
            "pdf": _portable_path(pdf, output_parent),
            "extraction": _portable_path(extraction, output_parent),
        },
        "document": {
            "id": document_id,
            "title": title,
            "author": author,
        },
        "scope": {
            "id": "selected-scope",
            "title": "Selected scope",
            "label": f"{len(primary_pages)} selected primary PDF page(s)",
            "primary_pages": primary_pages,
            "apparatus_pages": apparatus_pages,
            "apparatus": apparatus,
            "canonical_banners": {
                "primary": "=== PRIMARY TEXT ===",
                "apparatus": apparatus_banner,
            },
            "boundaries": [],
            "page_furniture": [],
        },
        "architecture": {
            "rationale": (
                "Starter floors are zero and must be reviewed against the selected source "
                "before any model-driven smoke or build."
            ),
            "minimums": {
                "case_dossiers": 0,
                "concept_notes": 0,
                "section_notes": 0,
                "claims": 0,
                "motifs": 0,
            },
            "nullable_rationales": {},
            "required_note_types": list(REQUIRED_NOTE_TYPES),
        },
        "output": {
            "format": "Open Knowledge Format",
            "okf_version": "0.2",
        },
    }
    validate_profile(value)
    return value


def configure(parser: argparse.ArgumentParser) -> None:
    actions = parser.add_subparsers(dest="profile_command", required=True)

    init_parser = actions.add_parser("init", help="create an editable profile")
    init_parser.add_argument("--extraction", type=Path, required=True)
    init_parser.add_argument("--output", type=Path, required=True)
    init_parser.add_argument("--pdf", type=Path)
    init_parser.add_argument(
        "--pages",
        type=_page_spec,
        help="primary page selection, for example 2-12,15 (default: every PDF page)",
    )
    init_parser.add_argument("--apparatus-pages", type=_page_spec)
    init_parser.add_argument("--apparatus-range", type=_entry_range)
    init_parser.add_argument("--apparatus-id", default="citation-apparatus")
    init_parser.add_argument("--apparatus-label", default="numbered references")
    init_parser.add_argument("--apparatus-entry-pattern")

    check_parser = actions.add_parser("check", help="validate an authored profile")
    check_parser.add_argument("profile", type=Path)

    lock_parser = actions.add_parser("lock", help="compute a reproducibility lock")
    lock_parser.add_argument("profile", type=Path)
    lock_parser.add_argument("--output", type=Path, required=True)


def run(args: argparse.Namespace) -> int:
    if args.profile_command == "init":
        value = _initial_profile(args)
        exclusive_json(args.output.resolve(), value)
        print(f"created profile: {args.output.resolve()}")
        print("review architecture floors, scope boundaries, and page furniture before smoke")
        return 0

    profile = load_profile(args.profile)
    if args.profile_command == "check":
        print(f"profile: {profile.data['profile_id']}")
        print(f"profile_sha256: {profile.profile_sha256}")
        print(f"pdf: {profile.pdf_path}")
        print(f"extraction: {profile.extraction_root}")
        print("status: valid")
        return 0

    lock = source.build_profile_lock(profile)
    exclusive_json(args.output.resolve(), lock)
    print(f"created lock: {args.output.resolve()}")
    print(f"profile_sha256: {lock['profile_sha256']}")
    print(f"canonical_scope_sha256: {lock['scope']['canonical_sha256']}")
    return 0
