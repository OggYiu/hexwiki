"""Strict, portable document profiles and reproducibility locks.

An authored profile contains document facts and scope choices only. Hashes are
computed into a separate lock file so checking a source never silently rewrites
operator-authored configuration. Relative paths are resolved from the profile
file, never from the current working directory or a repository root.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROFILE_SCHEMA_VERSION = 1
LOCK_SCHEMA_VERSION = 1
OKF_VERSION = "0.2"
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

REQUIRED_NOTE_TYPES = (
    "Overview",
    "Reading Guide",
    "Chapter",
    "Section",
    "Case Dossier",
    "Concept",
    "Person",
    "Synthesis",
    "Source Guide",
    "Open Question",
)

MINIMUM_KEYS = (
    "case_dossiers",
    "concept_notes",
    "section_notes",
    "claims",
    "motifs",
)


class ProfileError(ValueError):
    """An authored profile or lock violates the public contract."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProfileError(f"duplicate JSON key: {key!r}")
        value[key] = item
    return value


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8-sig"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as error:
        raise ProfileError(f"invalid JSON in {path}: {error}") from error
    if not isinstance(value, dict):
        raise ProfileError(f"{path} must contain one JSON object")
    return value


def _mapping(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProfileError(f"{location} must be an object")
    return value


def _keys(
    value: dict[str, Any],
    *,
    location: str,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required - optional)
    if missing:
        raise ProfileError(f"{location} is missing required fields: {', '.join(missing)}")
    if unknown:
        raise ProfileError(f"{location} has unknown fields: {', '.join(unknown)}")


def _text(value: Any, location: str, *, minimum: int = 1) -> str:
    if not isinstance(value, str) or len(value.strip()) < minimum:
        raise ProfileError(f"{location} must be a non-empty string")
    if "\x00" in value:
        raise ProfileError(f"{location} may not contain a NUL character")
    return value.strip()


def _slug(value: Any, location: str) -> str:
    candidate = _text(value, location)
    if not SLUG_RE.fullmatch(candidate):
        raise ProfileError(
            f"{location} must use lowercase letters, digits, and single hyphens"
        )
    return candidate


def _pages(value: Any, location: str, *, required: bool) -> list[int]:
    if not isinstance(value, list):
        raise ProfileError(f"{location} must be a list of positive PDF page numbers")
    if required and not value:
        raise ProfileError(f"{location} must contain at least one PDF page")
    if any(isinstance(page, bool) or not isinstance(page, int) or page < 1 for page in value):
        raise ProfileError(f"{location} must contain only positive integers")
    if value != sorted(set(value)):
        raise ProfileError(f"{location} must be unique and sorted in ascending order")
    return value


def _validate_source(value: Any) -> None:
    source = _mapping(value, "source")
    _keys(source, location="source", required={"pdf", "extraction"})
    pdf = _text(source["pdf"], "source.pdf")
    _text(source["extraction"], "source.extraction")
    if Path(pdf).suffix.lower() != ".pdf":
        raise ProfileError("source.pdf must name a PDF file")


def _validate_document(value: Any) -> None:
    document = _mapping(value, "document")
    _keys(document, location="document", required={"id", "title", "author"})
    _slug(document["id"], "document.id")
    _text(document["title"], "document.title")
    _text(document["author"], "document.author")


def _validate_boundary(
    value: Any, index: int, scoped_pages: set[int]
) -> tuple[int, str]:
    location = f"scope.boundaries[{index}]"
    boundary = _mapping(value, location)
    _keys(
        boundary,
        location=location,
        required={"page", "marker", "keep", "action", "note"},
    )
    page = boundary["page"]
    if isinstance(page, bool) or not isinstance(page, int) or page not in scoped_pages:
        raise ProfileError(f"{location}.page must name one of the scoped PDF pages")
    marker = _text(boundary["marker"], f"{location}.marker")
    if boundary["keep"] not in {"before", "after"}:
        raise ProfileError(f"{location}.keep must be 'before' or 'after'")
    _slug(boundary["action"], f"{location}.action")
    _text(boundary["note"], f"{location}.note", minimum=12)
    return page, marker


def _validate_furniture(value: Any, index: int) -> tuple[str, str]:
    location = f"scope.page_furniture[{index}]"
    item = _mapping(value, location)
    _keys(item, location=location, required={"pattern", "reason"})
    pattern = _text(item["pattern"], f"{location}.pattern")
    reason = _text(item["reason"], f"{location}.reason", minimum=12)
    try:
        re.compile(pattern)
    except re.error as error:
        raise ProfileError(f"{location}.pattern is not valid regex: {error}") from error
    return pattern, reason


def _validate_apparatus(
    value: Any, pages: list[int], banners: dict[str, Any]
) -> None:
    if not pages:
        if value is not None:
            raise ProfileError(
                "scope.apparatus must be null when scope.apparatus_pages is empty"
            )
        if banners["apparatus"] is not None:
            raise ProfileError(
                "scope.canonical_banners.apparatus must be null when no apparatus exists"
            )
        return

    apparatus = _mapping(value, "scope.apparatus")
    _keys(
        apparatus,
        location="scope.apparatus",
        required={"id", "label", "entry_range"},
        optional={"entry_pattern"},
    )
    _slug(apparatus["id"], "scope.apparatus.id")
    _text(apparatus["label"], "scope.apparatus.label")
    span = apparatus["entry_range"]
    if (
        not isinstance(span, list)
        or len(span) != 2
        or any(isinstance(number, bool) or not isinstance(number, int) for number in span)
        or span[0] < 1
        or span[1] < span[0]
    ):
        raise ProfileError(
            "scope.apparatus.entry_range must be [first, last] positive integers"
        )
    configured = apparatus.get("entry_pattern")
    if configured is not None:
        pattern = _text(configured, "scope.apparatus.entry_pattern")
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise ProfileError(
                f"scope.apparatus.entry_pattern is not valid regex: {error}"
            ) from error
        if compiled.groups < 1:
            raise ProfileError(
                "scope.apparatus.entry_pattern must capture the entry number as group 1"
            )
    if not isinstance(banners["apparatus"], str) or not banners["apparatus"].strip():
        raise ProfileError(
            "scope.canonical_banners.apparatus must be non-empty when an apparatus exists"
        )


def _validate_scope(value: Any) -> None:
    scope = _mapping(value, "scope")
    _keys(
        scope,
        location="scope",
        required={
            "id",
            "title",
            "label",
            "primary_pages",
            "apparatus_pages",
            "apparatus",
            "canonical_banners",
            "boundaries",
            "page_furniture",
        },
    )
    _slug(scope["id"], "scope.id")
    _text(scope["title"], "scope.title")
    _text(scope["label"], "scope.label")
    primary = _pages(scope["primary_pages"], "scope.primary_pages", required=True)
    apparatus_pages = _pages(
        scope["apparatus_pages"], "scope.apparatus_pages", required=False
    )
    overlap = sorted(set(primary).intersection(apparatus_pages))
    if overlap:
        raise ProfileError(
            "scope.primary_pages and scope.apparatus_pages overlap: "
            + ", ".join(str(page) for page in overlap)
        )

    banners = _mapping(scope["canonical_banners"], "scope.canonical_banners")
    _keys(
        banners,
        location="scope.canonical_banners",
        required={"primary", "apparatus"},
    )
    _text(banners["primary"], "scope.canonical_banners.primary")
    _validate_apparatus(scope["apparatus"], apparatus_pages, banners)

    boundaries = scope["boundaries"]
    if not isinstance(boundaries, list):
        raise ProfileError("scope.boundaries must be a list")
    seen_pages: set[int] = set()
    for index, item in enumerate(boundaries):
        page, _ = _validate_boundary(item, index, set(primary + apparatus_pages))
        if page in seen_pages:
            raise ProfileError(f"scope.boundaries declares PDF page {page} more than once")
        seen_pages.add(page)

    furniture = scope["page_furniture"]
    if not isinstance(furniture, list):
        raise ProfileError("scope.page_furniture must be a list")
    patterns: set[str] = set()
    reasons: set[str] = set()
    for index, item in enumerate(furniture):
        pattern, reason = _validate_furniture(item, index)
        if pattern in patterns:
            raise ProfileError(f"scope.page_furniture repeats pattern {pattern!r}")
        if reason in reasons:
            raise ProfileError(f"scope.page_furniture repeats reason {reason!r}")
        patterns.add(pattern)
        reasons.add(reason)


def _validate_architecture(value: Any) -> None:
    architecture = _mapping(value, "architecture")
    _keys(
        architecture,
        location="architecture",
        required={
            "rationale",
            "minimums",
            "nullable_rationales",
            "required_note_types",
        },
    )
    _text(architecture["rationale"], "architecture.rationale", minimum=20)
    minimums = _mapping(architecture["minimums"], "architecture.minimums")
    _keys(minimums, location="architecture.minimums", required=set(MINIMUM_KEYS))
    null_keys: set[str] = set()
    for key in MINIMUM_KEYS:
        value = minimums[key]
        if value is None:
            null_keys.add(key)
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ProfileError(
                f"architecture.minimums.{key} must be a non-negative integer or null"
            )

    rationales = _mapping(
        architecture["nullable_rationales"], "architecture.nullable_rationales"
    )
    if set(rationales) != null_keys:
        missing = sorted(null_keys - set(rationales))
        extra = sorted(set(rationales) - null_keys)
        detail = []
        if missing:
            detail.append("missing reasons for " + ", ".join(missing))
        if extra:
            detail.append("reasons supplied for non-null floors " + ", ".join(extra))
        raise ProfileError("architecture.nullable_rationales: " + "; ".join(detail))
    for key, rationale in rationales.items():
        _text(rationale, f"architecture.nullable_rationales.{key}", minimum=20)

    note_types = architecture["required_note_types"]
    if not isinstance(note_types, list) or any(not isinstance(item, str) for item in note_types):
        raise ProfileError("architecture.required_note_types must be a list of strings")
    if len(note_types) != len(set(note_types)):
        raise ProfileError("architecture.required_note_types may not contain duplicates")
    if set(note_types) != set(REQUIRED_NOTE_TYPES):
        missing = sorted(set(REQUIRED_NOTE_TYPES) - set(note_types))
        unknown = sorted(set(note_types) - set(REQUIRED_NOTE_TYPES))
        raise ProfileError(
            "architecture.required_note_types must match the supported vocabulary; "
            f"missing={missing}, unknown={unknown}"
        )


def _validate_output(value: Any) -> None:
    output = _mapping(value, "output")
    _keys(output, location="output", required={"format", "okf_version"})
    if output["format"] != "Open Knowledge Format":
        raise ProfileError("output.format must be 'Open Knowledge Format'")
    if output["okf_version"] != OKF_VERSION:
        raise ProfileError(f"output.okf_version must be {OKF_VERSION!r}")


def validate_profile(value: dict[str, Any]) -> None:
    """Validate all authored profile fields, including cross-field rules."""
    _keys(
        value,
        location="profile",
        required={
            "schema_version",
            "profile_id",
            "source",
            "document",
            "scope",
            "architecture",
            "output",
        },
    )
    if (
        isinstance(value["schema_version"], bool)
        or value["schema_version"] != PROFILE_SCHEMA_VERSION
    ):
        raise ProfileError(
            f"schema_version must be {PROFILE_SCHEMA_VERSION}, got {value['schema_version']!r}"
        )
    _slug(value["profile_id"], "profile_id")
    _validate_source(value["source"])
    _validate_document(value["document"])
    _validate_scope(value["scope"])
    _validate_architecture(value["architecture"])
    _validate_output(value["output"])


def canonical_json(value: Any) -> bytes:
    """Stable UTF-8 representation used by profile and lock hashes."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class DocumentProfile:
    path: Path
    data: dict[str, Any]

    @property
    def profile_sha256(self) -> str:
        return sha256_json(self.data)

    def resolve_path(self, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.path.parent / candidate
        return candidate.resolve()

    @property
    def pdf_path(self) -> Path:
        return self.resolve_path(self.data["source"]["pdf"])

    @property
    def extraction_root(self) -> Path:
        return self.resolve_path(self.data["source"]["extraction"])

    def runtime(self, lock: dict[str, Any] | None = None) -> dict[str, Any]:
        """Flatten public fields for the proven deterministic engine modules."""
        document = self.data["document"]
        scope = self.data["scope"]
        apparatus = scope["apparatus"] or {}
        value: dict[str, Any] = {
            "profile_id": self.data["profile_id"],
            "document_id": document["id"],
            "document_title": document["title"],
            "document_author": document["author"],
            "scope_id": scope["id"],
            "scope_title": scope["title"],
            "scope_label": scope["label"],
            "primary_pages": list(scope["primary_pages"]),
            "apparatus_pages": list(scope["apparatus_pages"]),
            "apparatus_range": list(apparatus.get("entry_range", [])),
            "apparatus_label": apparatus.get("label"),
            "apparatus_slug": apparatus.get("id"),
            "apparatus_entry_pattern": apparatus.get("entry_pattern"),
            "canonical_banners": dict(scope["canonical_banners"]),
            "boundaries": list(scope["boundaries"]),
            "page_furniture": list(scope["page_furniture"]),
            "architecture": dict(self.data["architecture"]),
            "output": dict(self.data["output"]),
            "source_pdf": self.data["source"]["pdf"],
            "extraction_root": self.data["source"]["extraction"],
        }
        if lock is not None:
            value.update(
                {
                    "source_pdf_sha256": lock["source"]["pdf_sha256"],
                    "canonical_scope_sha256": lock["scope"]["canonical_sha256"],
                    "canonical_scope_characters": lock["scope"]["canonical_characters"],
                    "page_text_characters": lock["scope"]["page_text_characters"],
                }
            )
        return value


def load_profile(path: Path | str) -> DocumentProfile:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    value = _read_json(resolved)
    validate_profile(value)
    return DocumentProfile(path=resolved, data=value)


def _hash(value: Any, location: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ProfileError(f"{location} must be a lowercase SHA-256 digest")


def validate_lock(value: dict[str, Any]) -> None:
    _keys(
        value,
        location="profile lock",
        required={"lock_version", "profile_id", "profile_sha256", "source", "scope"},
    )
    if (
        isinstance(value["lock_version"], bool)
        or value["lock_version"] != LOCK_SCHEMA_VERSION
    ):
        raise ProfileError(
            f"lock_version must be {LOCK_SCHEMA_VERSION}, got {value['lock_version']!r}"
        )
    _slug(value["profile_id"], "profile lock profile_id")
    _hash(value["profile_sha256"], "profile lock profile_sha256")

    source = _mapping(value["source"], "profile lock source")
    _keys(
        source,
        location="profile lock source",
        required={
            "pdf",
            "pdf_sha256",
            "extraction",
            "manifest_sha256",
            "validation_sha256",
            "checksums_sha256",
            "extractor_version",
        },
    )
    _text(source["pdf"], "profile lock source.pdf")
    _text(source["extraction"], "profile lock source.extraction")
    _text(source["extractor_version"], "profile lock source.extractor_version")
    for key in ("pdf_sha256", "manifest_sha256", "validation_sha256", "checksums_sha256"):
        _hash(source[key], f"profile lock source.{key}")

    scope = _mapping(value["scope"], "profile lock scope")
    _keys(
        scope,
        location="profile lock scope",
        required={
            "canonical_sha256",
            "canonical_characters",
            "page_text_characters",
            "pages",
            "apparatus_entries",
        },
    )
    _hash(scope["canonical_sha256"], "profile lock scope.canonical_sha256")
    for key in ("canonical_characters", "page_text_characters"):
        if isinstance(scope[key], bool) or not isinstance(scope[key], int) or scope[key] < 0:
            raise ProfileError(f"profile lock scope.{key} must be a non-negative integer")
    pages = _mapping(scope["pages"], "profile lock scope.pages")
    for page, record_value in pages.items():
        if not str(page).isdigit() or int(page) < 1:
            raise ProfileError(f"profile lock scope.pages has invalid page key {page!r}")
        record = _mapping(record_value, f"profile lock scope.pages.{page}")
        _keys(
            record,
            location=f"profile lock scope.pages.{page}",
            required={
                "native_sha256",
                "scoped_sha256",
                "scoped_characters",
                "section",
                "boundary_action",
                "furniture_removed",
            },
        )
        _hash(record["native_sha256"], f"profile lock scope.pages.{page}.native_sha256")
        _hash(record["scoped_sha256"], f"profile lock scope.pages.{page}.scoped_sha256")
        if (
            isinstance(record["scoped_characters"], bool)
            or not isinstance(record["scoped_characters"], int)
            or record["scoped_characters"] < 0
        ):
            raise ProfileError(
                f"profile lock scope.pages.{page}.scoped_characters must be non-negative"
            )
        if record["section"] not in {"primary_text", "citation_apparatus"}:
            raise ProfileError(f"profile lock scope.pages.{page}.section is invalid")
        _text(record["boundary_action"], f"profile lock scope.pages.{page}.boundary_action")
        if not isinstance(record["furniture_removed"], list) or any(
            not isinstance(item, str) for item in record["furniture_removed"]
        ):
            raise ProfileError(
                f"profile lock scope.pages.{page}.furniture_removed must be a string list"
            )
    entries = scope["apparatus_entries"]
    if not isinstance(entries, list) or any(
        isinstance(item, bool) or not isinstance(item, int) or item < 1 for item in entries
    ):
        raise ProfileError("profile lock scope.apparatus_entries must be positive integers")


def load_profile_lock(path: Path | str) -> dict[str, Any]:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    value = _read_json(resolved)
    validate_lock(value)
    return value
