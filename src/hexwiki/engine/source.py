"""Verify, lock, and stage one bounded slice of an extracted PDF.

The document profile chooses the pages, boundary clips, repeating furniture,
and optional numbered apparatus. The lock records what those choices resolve to
for one exact PDF and extraction. Staging always recomputes the lock before it
writes, so a profile cannot silently drift to a different source or edition.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .audit import AuditLog, atomic_json, atomic_text, sha256_file
from .profile import (
    LOCK_SCHEMA_VERSION,
    DocumentProfile,
    sha256_json,
    validate_lock,
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _boundaries(profile: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(item["page"]): item for item in profile.get("boundaries", [])}


def strip_furniture(text: str, profile: dict[str, Any]) -> tuple[str, list[str]]:
    """Remove declared edition furniture and return each applied reason."""
    applied: list[str] = []
    for item in profile.get("page_furniture") or []:
        text, count = re.compile(item["pattern"]).subn("", text)
        if count:
            applied.append(item["reason"])
    return text, applied


def _clip(page: int, text: str, profile: dict[str, Any]) -> tuple[str, str]:
    boundary = _boundaries(profile).get(page)
    if boundary is None:
        return text, "none"
    marker = boundary["marker"]
    if marker not in text:
        raise ValueError(f"boundary marker {marker!r} missing on PDF page {page}")
    index = text.index(marker)
    kept = text[index:] if boundary["keep"] == "after" else text[:index]
    return kept.rstrip() + "\n", boundary["action"]


def clipped_away(page: int, text: str, profile: dict[str, Any]) -> str:
    boundary = _boundaries(profile).get(page)
    if boundary is None or boundary["marker"] not in text:
        return ""
    index = text.index(boundary["marker"])
    removed = text[:index] if boundary["keep"] == "after" else text[index:]
    return removed.strip()


def discarded_summary(page: int, text: str, profile: dict[str, Any]) -> str:
    """Describe the part of a boundary page excluded from the scope."""
    removed = clipped_away(page, text, profile)
    if not removed:
        return ""
    entries = [int(value) for value in re.findall(r"(?m)^(\d{1,3})\.\s", removed)]
    if len(entries) >= 2 and entries == sorted(entries):
        return (
            f"the discarded portion carries {len(entries)} numbered entries "
            f"({entries[0]}-{entries[-1]}) belonging to an adjoining division"
        )
    return (
        f"the discarded portion is about {len(removed.split())} words "
        "of adjoining material"
    )


ENTRY_START_RE = re.compile(r"(?m)^(\d{1,3})\.\s")


def entry_pattern(profile: dict[str, Any]) -> re.Pattern[str]:
    """Compile the apparatus entry pattern, whose first group is the number."""
    configured = profile.get("apparatus_entry_pattern")
    return re.compile(configured) if configured else ENTRY_START_RE


def has_apparatus(profile: dict[str, Any]) -> bool:
    """Return whether both halves of a numbered apparatus are declared."""
    pages = profile.get("apparatus_pages") or []
    span = profile.get("apparatus_range") or []
    if bool(pages) != bool(span):
        raise ValueError(
            "profile must declare both apparatus_pages and apparatus_range or neither; "
            f"got apparatus_pages={pages!r} apparatus_range={span!r}"
        )
    return bool(pages)


def apparatus_numbers(
    text: str,
    first: int,
    last: int,
    pattern: re.Pattern[str] | None = None,
) -> list[int]:
    """Read a numbered apparatus as a running sequence.

    A page range wrapped onto a new line can resemble a new entry. Taking only
    the next number actually due avoids fabricating an entry from that layout.
    """
    expected = first
    found: list[int] = []
    for match in (pattern or ENTRY_START_RE).finditer(text):
        value = int(match.group(1))
        if value == expected:
            found.append(value)
            expected += 1
            if expected > last:
                break
    return found


def boundary_note(page: int, profile: dict[str, Any]) -> str:
    boundary = _boundaries(profile).get(page)
    return str(boundary["note"]) if boundary else ""


def _checksum_inventory(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        digest, separator, relative = line.partition("  ")
        if (
            not separator
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not relative.strip()
        ):
            raise ValueError(
                f"invalid extraction checksum inventory line {line_number}: {line!r}"
            )
        normalized = relative.replace("\\", "/")
        if normalized in result:
            raise ValueError(f"duplicate extraction checksum path: {normalized}")
        result[normalized] = digest
    return result


def _required_extraction_files(profile: DocumentProfile) -> tuple[Path, Path, Path]:
    root = profile.extraction_root
    return root / "manifest.json", root / "validation.json", root / "checksums.sha256"


def inspect_source(profile: DocumentProfile) -> dict[str, Any]:
    """Compute the exact source snapshot represented by an authored profile."""
    runtime = profile.runtime()
    pdf = profile.pdf_path
    extraction = profile.extraction_root
    manifest_path, validation_path, checksums_path = _required_extraction_files(profile)
    for path in (pdf, manifest_path, validation_path, checksums_path):
        if not path.is_file():
            raise FileNotFoundError(path)

    pdf_hash = sha256_file(pdf)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "passed" or validation.get("status") != "passed":
        raise ValueError("extraction manifest and validation must both report 'passed'")
    if manifest.get("source", {}).get("sha256") != pdf_hash:
        raise ValueError("extraction manifest belongs to a different source PDF")

    inventory = _checksum_inventory(checksums_path)
    pages: dict[int, dict[str, Any]] = {}
    furniture_applied: set[str] = set()
    primary = [int(page) for page in runtime["primary_pages"]]
    apparatus = [int(page) for page in runtime["apparatus_pages"]]
    ordered = primary + apparatus

    for page in ordered:
        relative = f"text/native-pages/page-{page:04d}.txt"
        native = extraction / relative
        if not native.is_file():
            raise FileNotFoundError(native)
        native_hash = sha256_file(native)
        if inventory.get(relative) != native_hash:
            raise ValueError(f"extraction native-page checksum mismatch: {relative}")
        raw_page = native.read_text(encoding="utf-8")
        without_furniture, removed = strip_furniture(raw_page, runtime)
        furniture_applied.update(removed)
        clipped, action = _clip(page, without_furniture, runtime)
        pages[page] = {
            "page": page,
            "text": clipped,
            "section": "primary_text" if page in primary else "citation_apparatus",
            "boundary_action": action,
            "furniture_removed": sorted(removed),
            "discarded": discarded_summary(page, without_furniture, runtime),
            "native_sha256": native_hash,
            "sha256": _sha256_text(clipped),
        }

    declared_furniture = {
        item["reason"] for item in runtime.get("page_furniture") or []
    }
    unused = sorted(declared_furniture - furniture_applied)
    if unused:
        raise ValueError(
            "page_furniture patterns matched nothing on any scoped page: "
            + "; ".join(unused)
        )

    banners = runtime["canonical_banners"]
    parts = [
        banners["primary"],
        *[pages[page]["text"].rstrip() for page in primary],
    ]
    apparatus_entries: list[int] = []
    if has_apparatus(runtime):
        parts.append(banners["apparatus"])
        parts.extend(pages[page]["text"].rstrip() for page in apparatus)
        first, last = (int(value) for value in runtime["apparatus_range"])
        apparatus_text = "\n".join(pages[page]["text"] for page in apparatus)
        apparatus_entries = apparatus_numbers(
            apparatus_text, first, last, entry_pattern(runtime)
        )
        expected = list(range(first, last + 1))
        if apparatus_entries != expected:
            raise ValueError(
                f"{runtime['apparatus_label']} are not continuous {first}-{last}; "
                f"found {apparatus_entries}"
            )
    canonical = "\n".join(parts) + "\n"

    page_lock = {
        str(page): {
            "native_sha256": pages[page]["native_sha256"],
            "scoped_sha256": pages[page]["sha256"],
            "scoped_characters": len(pages[page]["text"]),
            "section": pages[page]["section"],
            "boundary_action": pages[page]["boundary_action"],
            "furniture_removed": pages[page]["furniture_removed"],
        }
        for page in ordered
    }
    lock = {
        "lock_version": LOCK_SCHEMA_VERSION,
        "profile_id": profile.data["profile_id"],
        "profile_sha256": profile.profile_sha256,
        "source": {
            "pdf": profile.data["source"]["pdf"],
            "pdf_sha256": pdf_hash,
            "extraction": profile.data["source"]["extraction"],
            "manifest_sha256": sha256_file(manifest_path),
            "validation_sha256": sha256_file(validation_path),
            "checksums_sha256": sha256_file(checksums_path),
            "extractor_version": str(manifest.get("extractor_version", "unknown")),
        },
        "scope": {
            "canonical_sha256": _sha256_text(canonical),
            "canonical_characters": len(canonical),
            "page_text_characters": sum(len(item["text"]) for item in pages.values()),
            "pages": page_lock,
            "apparatus_entries": apparatus_entries,
        },
    }
    validate_lock(lock)
    return {
        "runtime": runtime,
        "pages": pages,
        "canonical": canonical,
        "manifest": manifest,
        "validation": validation,
        "lock": lock,
    }


def build_profile_lock(profile: DocumentProfile) -> dict[str, Any]:
    return inspect_source(profile)["lock"]


def _first_difference(expected: Any, actual: Any, location: str = "lock") -> str:
    if type(expected) is not type(actual):
        return f"{location}: expected {type(expected).__name__}, got {type(actual).__name__}"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual)):
            if key not in expected:
                return f"{location}.{key}: unexpected field"
            if key not in actual:
                return f"{location}.{key}: missing field"
            difference = _first_difference(expected[key], actual[key], f"{location}.{key}")
            if difference:
                return difference
        return ""
    if isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{location}: expected {len(expected)} items, got {len(actual)}"
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _first_difference(left, right, f"{location}[{index}]")
            if difference:
                return difference
        return ""
    if expected != actual:
        return f"{location}: expected {expected!r}, got {actual!r}"
    return ""


def verify_profile_lock(
    profile: DocumentProfile, lock: dict[str, Any]
) -> dict[str, Any]:
    """Recompute and compare a lock, returning the verified source snapshot."""
    validate_lock(lock)
    if lock["profile_id"] != profile.data["profile_id"]:
        raise ValueError(
            f"profile lock is for {lock['profile_id']!r}, not {profile.data['profile_id']!r}"
        )
    if lock["profile_sha256"] != profile.profile_sha256:
        raise ValueError("profile lock does not match the authored profile")
    inspection = inspect_source(profile)
    if inspection["lock"] != lock:
        difference = _first_difference(lock, inspection["lock"])
        raise ValueError(f"profile lock no longer matches the source: {difference}")
    return inspection


def stage_source(
    *,
    profile: DocumentProfile,
    lock: dict[str, Any],
    work_root: Path,
    audit: AuditLog,
) -> dict[str, Any]:
    """Verify the lock again and write a fresh run-local canonical source stage."""
    work_root = Path(work_root)
    data_root = work_root / "data"
    manifest_path = work_root / "source-manifest.json"
    if work_root.exists():
        raise FileExistsError(
            f"explicit source-stage directory already exists: {work_root}"
        )

    inspected = verify_profile_lock(profile, lock)
    runtime = profile.runtime(lock)
    pages = inspected["pages"]
    ordered = runtime["primary_pages"] + runtime["apparatus_pages"]
    raw_dir = data_root / "raw" / runtime["document_id"]
    raw_dir.mkdir(parents=True, exist_ok=False)

    page_summary = (
        f"{runtime['primary_pages'][0]}-{runtime['primary_pages'][-1]}"
        + (
            f"; {runtime['apparatus_label']} "
            + ",".join(str(page) for page in runtime["apparatus_pages"])
            if has_apparatus(runtime)
            else ""
        )
    )
    lines = [
        "---",
        f"document: {json.dumps(runtime['document_id'])}",
        f"document_title: {json.dumps(runtime['document_title'], ensure_ascii=False)}",
        f"author: {json.dumps(runtime['document_author'], ensure_ascii=False)}",
        f"scope: {json.dumps(runtime['scope_label'], ensure_ascii=False)}",
        f"pdf_pages: {json.dumps(page_summary)}",
        "note: immutable current-run canonical extraction - do not edit",
        "---",
        "",
    ]
    for page in ordered:
        item = pages[page]
        lines.extend(
            [
                f'<!-- pdf page {page}; scope {item["section"]}; '
                f'boundary {item["boundary_action"]} -->',
                item["text"].rstrip(),
                "",
            ]
        )
    raw_path = raw_dir / "01-canonical-scope.md"
    atomic_text(raw_path, "\n".join(lines).rstrip() + "\n")

    manifest_pages = {
        str(page): {
            key: value
            for key, value in pages[page].items()
            if key != "text"
        }
        | {"characters": len(pages[page]["text"])}
        for page in ordered
    }
    source_manifest = {
        "status": "passed",
        "profile_id": runtime["profile_id"],
        "profile_sha256": profile.profile_sha256,
        "profile_lock_sha256": sha256_json(lock),
        "source_pdf": runtime["source_pdf"],
        "source_pdf_sha256": runtime["source_pdf_sha256"],
        "extraction_root": runtime["extraction_root"],
        "canonical_scope_sha256": runtime["canonical_scope_sha256"],
        "canonical_scope_characters": runtime["canonical_scope_characters"],
        "page_text_characters": runtime["page_text_characters"],
        "pages": manifest_pages,
        "raw_stage": raw_path.relative_to(work_root).as_posix(),
        "raw_stage_sha256": sha256_file(raw_path),
        "reference_or_prior_wiki_read": False,
    }
    atomic_json(manifest_path, source_manifest)
    audit.record(
        phase="source",
        action="stage_verified_canonical_scope",
        what=(
            f"Verified and staged {len(ordered)} canonical pages for "
            f"{runtime['scope_label']}."
        ),
        why="Compilation must use the locked extraction rather than prior generated material.",
        how=(
            "Re-hashed the PDF and extraction records, checked each scoped native page, "
            "applied declared boundaries and furniture, rebuilt the canonical scope, "
            "and compared the complete result with the operator-approved profile lock."
        ),
        details={
            "raw_path": raw_path.relative_to(work_root).as_posix(),
            "raw_sha256": source_manifest["raw_stage_sha256"],
            "pages": ordered,
            "canonical_scope_sha256": runtime["canonical_scope_sha256"],
        },
    )
    return {
        "data_root": data_root,
        "raw_path": raw_path,
        "manifest": source_manifest,
        "pages": pages,
        "runtime": runtime,
    }
