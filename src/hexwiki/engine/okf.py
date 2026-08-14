"""OKF front-matter auditor for drift analysis across wiki snapshots.

Lives alongside lint.py: lint.py is the fast agent-facing gate that runs inside
the build loop; this is the deep auditor for periodic and cross-snapshot
comparison. Both read the same contract from lint.expected_type, so a change to
the note-type vocabulary lands in one place.

Usage:
    python okf.py <wiki-dir> [<wiki-dir> ...]

For each directory, walks *.md (skipping staging, schema, report, and audit
directories plus the structural files) and validates every remaining note:

  1. has-front-matter  -- starts with '---' and has a closing '---'.
  2. type              -- present and matching the type its location requires.
  3. title             -- present and exactly equal to the note's H1.
  4. description       -- present, non-empty, >= 8 words.
  5. tags              -- present, a non-empty list.
  6. epistemic-status  -- semantic_note is true, status is 'draft', 'verified'
                          is absent (OKF v0.2 signals the unverified tier by
                          omitting the key; the v0.1 `verified: false` spelling
                          is tolerated for already-published wikis), and
                          sources is a non-empty list whose mapping entries
                          each carry a 'resource'.

index.md and log.md are checked only for the *absence* of front matter.

This is a reporter, not a gate: it always exits 0. Output is one summary line
per directory, indented per-issue detail lines, and a final TSV table across
all given directories (handy for diffing snapshots over time).
"""

from __future__ import annotations

import sys
from pathlib import Path

from .lint import GATEWAY_PARTS, SKIP_DIRS, expected_type, parse_frontmatter

if sys.stdout.encoding is not None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


STRUCTURAL_BASENAMES = {"WIKI_GUIDE.md", "AGENTS.md", "README.md"}
NO_FRONT_MATTER_BASENAMES = {"index.md", "log.md"}

ERROR_KINDS = [
    "missing-fm",
    "wrong-type",
    "missing-type",
    "unknown-location",
    "title-mismatch",
    "missing-title",
    "weak-description",
    "missing-tags",
    "not-draft-unverified",
    "missing-sources",
    "no-fm-expected",
]


def issue(kind, relpath, detail, severity="error"):
    return {
        "kind": kind,
        "relpath": relpath.as_posix() if isinstance(relpath, Path) else str(relpath),
        "detail": detail,
        "severity": severity,
    }


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def starts_with_fm_delimiter(text):
    lines = text.lstrip("﻿").splitlines()
    return bool(lines) and lines[0].strip() == "---"


def find_h1(body):
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == "#":
            return ""
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return None


def validate_page(relpath, text):
    """Run the OKF contract check on one note."""
    issues = []
    gateway = relpath.parts[:2] == GATEWAY_PARTS

    if not starts_with_fm_delimiter(text):
        return [issue("missing-fm", relpath, "note has no '---' front-matter block")]
    metadata, body = parse_frontmatter(text)
    if not metadata:
        return [issue("missing-fm", relpath, "front matter is empty or has no closing '---'")]

    wanted = expected_type(relpath)
    actual = metadata.get("type")
    if actual is None or not str(actual).strip():
        issues.append(issue("missing-type", relpath, "front matter has no 'type' field"))
    elif gateway:
        pass  # source-page gateways are infrastructure, not typed notes
    elif wanted is None:
        issues.append(issue("unknown-location", relpath,
                            "note sits outside the guide's folder vocabulary"))
    elif str(actual).strip() != wanted:
        issues.append(issue("wrong-type", relpath,
                            "type '{}' != expected '{}'".format(actual, wanted)))

    h1 = find_h1(body)
    title = metadata.get("title")
    if title is None or not str(title).strip():
        issues.append(issue("missing-title", relpath, "front matter has no 'title' field"))
    elif h1 is None:
        issues.append(issue("title-mismatch", relpath,
                            "title '{}' but no H1 in body".format(str(title).strip())))
    elif str(title).strip() != h1.strip():
        issues.append(issue("title-mismatch", relpath,
                            "title '{}' != H1 '{}'".format(str(title).strip(), h1.strip())))

    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        issues.append(issue("weak-description", relpath, "description missing or empty"))
    elif len(description.split()) < 8:
        issues.append(issue("weak-description", relpath,
                            "description has only {} words (minimum 8)".format(
                                len(description.split()))))

    tags = metadata.get("tags")
    if not isinstance(tags, list) or not tags:
        issues.append(issue("missing-tags", relpath, "'tags' missing, empty, or not a list"))

    if gateway:
        return issues

    # OKF v0.2: the unverified trust tier is signalled by `verified` being
    # absent, not by a false value. `verified: false` is the v0.1 spelling and
    # is tolerated so published wikis still report clean.
    verified_present = "verified" in metadata
    verified_ok = not verified_present or metadata["verified"] is False
    if not (metadata.get("semantic_note") is True
            and metadata.get("status") == "draft"
            and verified_ok):
        issues.append(issue(
            "not-draft-unverified", relpath,
            "compiled notes must carry semantic_note: true, status: draft, and no 'verified' "
            "key (OKF v0.2 signals unverified by omission); "
            "got semantic_note={!r} status={!r} verified={}".format(
                metadata.get("semantic_note"), metadata.get("status"),
                repr(metadata["verified"]) if verified_present else "<absent>")))
    sources = metadata.get("sources")
    if not isinstance(sources, list) or not sources:
        issues.append(issue("missing-sources", relpath,
                            "'sources' must be a non-empty list of source locators"))
    elif any(isinstance(entry, dict) and not str(entry.get("resource", "")).strip()
             for entry in sources):
        issues.append(issue("missing-sources", relpath,
                            "each OKF v0.2 source entry needs a non-empty 'resource'"))
    return issues


def check_directory(root_path):
    """Walk one wiki snapshot and return {'n_pages': int, 'issues': [...]}."""
    root_path = Path(root_path)
    issues = []
    n_pages = 0
    for path in sorted(root_path.rglob("*.md"), key=lambda item: item.as_posix()):
        relpath = path.relative_to(root_path)
        if any(part in SKIP_DIRS for part in relpath.parts[:-1]):
            continue
        basename = relpath.parts[-1]
        if basename in STRUCTURAL_BASENAMES:
            continue
        if basename in NO_FRONT_MATTER_BASENAMES:
            if starts_with_fm_delimiter(read_text(path)):
                issues.append(issue("no-fm-expected", relpath,
                                    "{} must not start with YAML front matter".format(basename)))
            continue
        n_pages += 1
        issues.extend(validate_page(relpath, read_text(path)))
    return {"n_pages": n_pages, "issues": issues}


def count_kinds(issues):
    counts = {}
    for item in issues:
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    return counts


def n_errors_of(issues):
    counts = count_kinds(issues)
    return sum(counts.get(kind, 0) for kind in ERROR_KINDS)


def print_directory_report(name, result):
    print("{}: {} notes, {} errors".format(name, result["n_pages"], n_errors_of(result["issues"])))
    for item in result["issues"]:
        print("  {} {}: {}".format(item["kind"], item["relpath"], item["detail"]))


def print_tsv_summary(names, results):
    print("\t".join(["dir", "notes", "errors"] + ERROR_KINDS))
    for name, result in zip(names, results):
        counts = count_kinds(result["issues"])
        row = [name, str(result["n_pages"]), str(n_errors_of(result["issues"]))]
        row += [str(counts.get(kind, 0)) for kind in ERROR_KINDS]
        print("\t".join(row))


def main(argv):
    dirs = argv[1:]
    if not dirs:
        print("usage: okf.py <wiki-dir> [<wiki-dir> ...]")
        return 0
    names, results = [], []
    for value in dirs:
        root_path = Path(value)
        if not root_path.is_dir():
            print("{}: directory not found".format(value))
            names.append(value)
            results.append({"n_pages": 0, "issues": []})
            continue
        result = check_directory(root_path)
        print_directory_report(value, result)
        names.append(value)
        results.append(result)
    print()
    print_tsv_summary(names, results)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
