"""Deterministic linter for a source-bounded OKF wiki.

Usage: python lint.py <wiki-dir> [--json]

Checks:
  broken-link       a [[wikilink]] or relative .md link points at no note
  broken-image      an embedded image does not resolve
  not-in-index      note exists but is not listed in index.md
  index-ghost       index.md lists a file that does not exist
  orphan            no other note links to it (index alone is not enough)
  duplicate-title   two notes share an H1 title
  no-sources        note has neither a "## Sources" section nor an inline citation
  no-title          note lacks an H1 title as its first content line
  no-evidence-limits  a Case Dossier has no "## Evidence limits" section, or has one
                    that names no absence
  bad-locators      front-matter pdf_pages claims a page the body never links
  no-instances      a Concept note lists no '## Instances in scope'
  no-guardrail      a Concept note states no licensing/evidence-status guardrail
  empty-log         log.md missing or has no entries
  bad-front-matter  the front-matter contract is violated: unclosed block, an
                    unquoted value truncated by '#', a title that doesn't match
                    the H1, a type that doesn't match the folder, or a missing
                    epistemic field (semantic_note/status/sources), or a present
                    'verified' key (OKF v0.2 signals unverified by omission)

Exit code 0 when clean, 1 when any error. --json prints a machine-readable
report.

Stdlib only, and no syntax newer than Python 3.7 once the __future__ import
below defers annotation evaluation - so it runs on whatever python is on PATH.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LINK_RE = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.md)\)")
# Obsidian-style [[slug]] or [[slug|Displayed title]].
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]*)?\]\]")
IMG_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
FM_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$")
SOURCES_RE = re.compile(r"^## Sources\s*$", re.M)
EVIDENCE_LIMITS_RE = re.compile(r"^## Evidence limits\s*$", re.M)
EVIDENCE_LIMITS_BODY_RE = re.compile(r"(?ms)^## Evidence limits\s*$(.*?)(?=^## |\Z)")
INSTANCES_RE = re.compile(r"^## Instances in scope\s*$", re.M)
GUARDRAIL_RE = re.compile(r"^## (?:What this licenses and what it does not|Evidence status)\s*$", re.M)
# An evidence-limits section exists to say what is ABSENT. One that only lists
# what is present has the heading and none of the function, and an independent
# reviewer flags it — so the linter should catch it first, cheaply.
ABSENCE_RE = re.compile(
    r"(?i)\b(no|not|none|never|nothing|without|absent|lacks?|lacking|missing|"
    r"unnamed|unattributed|uncorroborated|unverified|does not|do not|is not|are not)\b"
)
GATEWAY_LINK_RE = re.compile(r"(?:sources/)?pdf-pages/page-(\d{4})\.md")
# An inline citation: an italicised work, a comma, then a chapter or page locator.
CITE_RE = re.compile(r"^\*[^*]+\*,.*?(?:ch\.|pp\.)", re.M)
LOG_ENTRY_RE = re.compile(r"^(?:- |## \[\d{4}-\d{2}-\d{2}\])", re.M)
H1_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)

SPECIAL = {"index.md", "log.md", "WIKI_GUIDE.md", "AGENTS.md", "README.md"}
SKIP_DIRS = {"_ingest", "_plan", "_schema", "reports", "audit"}

# Folder -> the note type the guide assigns it. Root-level notes are keyed by
# filename. Source-page gateways are infrastructure and are exempt.
FOLDER_TYPES = {
    "chapters": "Chapter",
    "sections": "Section",
    "cases": "Case Dossier",
    "concepts": "Concept",
    "people": "Person",
    "synthesis": "Synthesis",
    "sources": "Source Guide",
    "reference": "Source Guide",
}
ROOT_TYPES = {"overview.md": "Overview", "reading-guide.md": "Reading Guide"}
# Notes whose type may differ from their folder default.
TYPE_EXCEPTIONS = {"synthesis/open-questions.md": "Open Question"}
GATEWAY_PARTS = ("sources", "pdf-pages")


def parse_frontmatter(text):
    """Return (metadata, body). Tolerant scalar/list parser, no yaml import.

    Values are unquoted, `[a, b]` becomes a list, `true`/`false` become bools,
    and indented continuation lines fold into the previous scalar (YAML plain
    scalars). Anything unrecognised is skipped rather than raising, because the
    linter must report a malformed block, not crash on it.
    """
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    closing = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        return {}, text
    metadata = {}
    current = None
    for raw in lines[1:closing]:
        if raw.strip() == "":
            current = None
            continue
        if raw[:1] in (" ", "\t") and current is not None:
            stripped = raw.strip()
            previous = metadata.get(current)
            if stripped.startswith("- "):
                # A YAML block sequence under `key:`. OKF v0.2 writes `sources`
                # this way, one flow mapping per line. An empty scalar is the
                # placeholder the bare `key:` line left behind.
                if previous == "":
                    metadata[current] = previous = []
                if isinstance(previous, list):
                    previous.append(_scalar(stripped[2:].strip()))
                continue
            if isinstance(previous, str):
                metadata[current] = (previous + " " + raw.strip()).strip()
            continue
        match = FM_KEY_RE.match(raw)
        if not match:
            current = None
            continue
        key, value = match.group(1), match.group(2).strip()
        current = key
        metadata[key] = _scalar(value)
    return metadata, "\n".join(lines[closing + 1:])


def _split_flow(body):
    """Split a flow collection body on top-level commas, respecting quotes."""
    parts = re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", body)
    return [item.strip() for item in parts if item.strip()]


def _unquote(item):
    if len(item) >= 2 and item[0] == item[-1] and item[0] in "\"'":
        return item[1:-1].replace('\\"', '"')
    return item


def _flow_mapping(value):
    """Parse `{ k: v, k2: v2 }` into a dict.

    OKF v0.2 types `generated` as a mapping and each `sources` entry as one, so
    the parser has to understand them. Only the flow form is supported, which is
    the form this pipeline writes - a value is split at its first colon, so a
    colon inside a quoted value (an ISO timestamp, a URL) is safe.
    """
    mapping = {}
    for item in _split_flow(value[1:-1]):
        key, sep, raw = item.partition(":")
        if not sep:
            continue
        mapping[_unquote(key.strip())] = _scalar(raw.strip())
    return mapping


def _scalar(value):
    if value.startswith("[") and value.endswith("]"):
        return [_unquote(item) for item in _split_flow(value[1:-1])]
    if value.startswith("{") and value.endswith("}"):
        return _flow_mapping(value)
    if value in ("true", "True"):
        return True
    if value in ("false", "False"):
        return False
    return _unquote(value)


def strip_front_matter(text):
    """Text after a leading YAML front-matter block, or unchanged if none."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "".join(lines[i + 1:]).lstrip("\n")
    return text


def expected_type(relative):
    """The type the guide assigns to a note at this wiki-relative path."""
    posix = relative.as_posix()
    if posix in TYPE_EXCEPTIONS:
        return TYPE_EXCEPTIONS[posix]
    if len(relative.parts) == 1:
        return ROOT_TYPES.get(relative.name)
    return FOLDER_TYPES.get(relative.parts[0])


def _is_gateway(relative):
    return relative.parts[:2] == GATEWAY_PARTS


def _pages(wiki_dir):
    """Every note the per-note checks apply to."""
    result = []
    for path in sorted(wiki_dir.rglob("*.md")):
        relative = path.relative_to(wiki_dir)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        if path.name in SPECIAL:
            continue
        result.append(path)
    return result


def orphans(wiki_dir):
    """Notes no other note links to. index.md alone does not count."""
    wiki_dir = Path(wiki_dir)
    pages = _pages(wiki_dir)
    linked = set()
    for path in sorted(wiki_dir.rglob("*.md")):
        relative = path.relative_to(wiki_dir)
        if any(part in SKIP_DIRS for part in relative.parts[:-1]):
            continue
        if relative.name == "index.md" or _is_gateway(relative):
            continue  # catalogs and gateways do not rescue a note from orphanhood
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            continue
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://")):
                continue
            resolved = (path.parent / target).resolve()
            if resolved != path.resolve():
                linked.add(str(resolved))
    return [
        str(page.relative_to(wiki_dir)).replace("\\", "/")
        for page in pages
        if not _is_gateway(page.relative_to(wiki_dir)) and str(page.resolve()) not in linked
    ]


def lint(wiki_dir):
    wiki_dir = Path(wiki_dir)
    errors = []

    def err(kind, file, detail):
        errors.append({
            "kind": kind,
            "file": str(Path(file).relative_to(wiki_dir)).replace("\\", "/")
            if Path(file).is_absolute() else str(file),
            "detail": detail,
        })

    def read(page):
        """None on a decode failure - callers must skip further checks on that
        note (already reported as 'bad-encoding') rather than crash.

        Decoded as utf-8-sig: a leading BOM (any file written by Windows
        PowerShell's default Set-Content -Encoding UTF8) would otherwise make
        line 1 '﻿---' instead of '---', silently hiding the whole front
        matter from every check below. Genuinely invalid bytes still raise."""
        try:
            return page.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError as exc:
            err("bad-encoding", page, "file is not valid UTF-8: {}".format(exc))
            return None

    index = wiki_dir / "index.md"
    if not index.exists():
        return [{"kind": "no-index", "file": "index.md", "detail": "index.md missing"}]

    pages = _pages(wiki_dir)
    all_md = [
        path for path in sorted(wiki_dir.rglob("*.md"))
        if not any(part in SKIP_DIRS for part in path.relative_to(wiki_dir).parts[:-1])
    ]
    by_slug = {}
    for path in all_md:
        by_slug.setdefault(path.stem, []).append(path)

    # ---- links, images -----------------------------------------------------
    # WIKI_GUIDE.md / AGENTS.md / README.md are structural documentation whose
    # example links deliberately point at notes that do not exist. They are read
    # so wikilinks elsewhere can resolve against them, but not link-checked.
    texts = {}
    for path in all_md:
        text = read(path)
        if text is None:
            continue
        texts[path] = text
        if path.name in SPECIAL and path.name != "index.md":
            continue
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://")):
                continue
            if not (path.parent / target).resolve().exists():
                err("broken-link", path, "link to {} does not resolve".format(target))
        for target in WIKILINK_RE.findall(text):
            if target.strip() not in by_slug:
                err("broken-link", path, "wikilink [[{}]] matches no note".format(target.strip()))
        for target in IMG_RE.findall(text):
            if target.startswith(("http://", "https://", "data:")):
                continue
            if not (path.parent / target).resolve().exists():
                err("broken-image", path, "image {} does not resolve".format(target))

    # ---- catalog -----------------------------------------------------------
    index_text = texts.get(index, "")
    indexed = {str((wiki_dir / t).resolve()) for t in LINK_RE.findall(index_text)}
    for slug in WIKILINK_RE.findall(index_text):
        for target in by_slug.get(slug.strip(), []):
            indexed.add(str(target.resolve()))
    for target in indexed:
        if not Path(target).exists():
            errors.append({
                "kind": "index-ghost", "file": "index.md",
                "detail": "index links missing file {}".format(Path(target).name),
            })

    # ---- per-note ----------------------------------------------------------
    titles = {}
    for path in pages:
        text = texts.get(path)
        if text is None:
            continue
        relative = path.relative_to(wiki_dir)
        gateway = _is_gateway(relative)
        if not gateway and str(path.resolve()) not in indexed:
            err("not-in-index", path, "note not listed in index.md")
        if not (SOURCES_RE.search(text) or CITE_RE.search(text)):
            err("no-sources", path, "no '## Sources' section and no inline citation line")
        body = strip_front_matter(text)
        first = next((ln for ln in body.splitlines() if ln.strip()), "")
        if not first.startswith("# "):
            err("no-title", path, "first line is not an H1 title")
        else:
            titles.setdefault(first[2:].strip(), []).append(relative.as_posix())
        metadata = _check_front_matter(err, path, relative, text, gateway)
        if metadata.get("type") == "Concept":
            if not INSTANCES_RE.search(text):
                err("no-instances", path,
                    "a Concept note must list every episode exhibiting it under "
                    "'## Instances in scope', each linked")
            if not GUARDRAIL_RE.search(text):
                err("no-guardrail", path,
                    "a Concept note must end with '## What this licenses and what it does "
                    "not' (methodological/epistemic) or '## Evidence status' (substantive)")
        if metadata.get("type") == "Case Dossier":
            section = EVIDENCE_LIMITS_BODY_RE.search(text)
            if not section:
                err("no-evidence-limits", path,
                    "a Case Dossier must state what supports it and what does not")
            elif not ABSENCE_RE.search(section.group(1)):
                err("no-evidence-limits", path,
                    "the 'Evidence limits' section names no absence - it must say what is "
                    "NOT there (no corroborating witness, no named investigator, no "
                    "measurement, no primary document), not only what is")
        _check_locators(err, path, metadata, text)

    for title, holders in sorted(titles.items()):
        if len(holders) > 1:
            errors.append({
                "kind": "duplicate-title", "file": holders[0],
                "detail": "title {!r} is shared by {}".format(title, ", ".join(holders)),
            })

    for relative in orphans(wiki_dir):
        errors.append({
            "kind": "orphan", "file": relative,
            "detail": "no other note links to this one",
        })

    # ---- log ---------------------------------------------------------------
    log = wiki_dir / "log.md"
    log_text = read(log) if log.exists() else None
    if log_text is None or not LOG_ENTRY_RE.search(log_text):
        errors.append({"kind": "empty-log", "file": "log.md",
                       "detail": "log.md missing or has no entries"})
    return errors


def _check_locators(err, path, metadata, text):
    """Front-matter pdf_pages and the body's gateway links must agree.

    `wrong-locator` was the single largest category the independent reviewer
    raised: notes listing a page in `pdf_pages` that carries nothing they cite
    it for. The semantic half of that needs a reader, but the bookkeeping half
    does not — a page claimed in metadata and never linked beside a claim is a
    locator the note cannot justify, and it is free to catch here rather than
    paying a model to find it.
    """
    declared = metadata.get("pdf_pages")
    if declared is None:
        return
    if not isinstance(declared, list):
        err("bad-locators", path, "'pdf_pages' must be a list of page numbers")
        return
    try:
        claimed = {int(str(value).strip()) for value in declared if str(value).strip()}
    except ValueError:
        err("bad-locators", path, "'pdf_pages' contains a non-numeric entry")
        return
    linked = {int(value) for value in GATEWAY_LINK_RE.findall(text)}
    unlinked = sorted(claimed - linked)
    if unlinked:
        err("bad-locators", path,
            "front matter claims pages {} that the body never links to a source "
            "gateway; cite the page beside the claim it supports or drop it from "
            "pdf_pages".format(unlinked))


def _check_front_matter(err, path, relative, text, gateway):
    """Validate the front-matter contract. Returns the parsed metadata."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        err("bad-front-matter", path, "note does not start with a '---' front-matter block")
        return {}
    closing = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
    if closing == -1:
        err("bad-front-matter", path, "front-matter block has no closing '---'")
        return {}

    # Unquoted '#' truncation: a plain-string check, before any parsing.
    unquoted_key = None
    for line in lines[1:closing]:
        if line.strip() == "":
            unquoted_key = None
            continue
        if line[:1] in (" ", "\t"):
            if unquoted_key and " #" in line:
                err("bad-front-matter", path,
                    "unquoted '#' in front-matter '{}' - YAML truncates at comments; "
                    "double-quote the value".format(unquoted_key))
            continue
        match = FM_KEY_RE.match(line)
        if not match:
            unquoted_key = None
            continue
        key, value = match.group(1), match.group(2)
        quoted = value.startswith('"') or value.startswith("'")
        unquoted_key = None if quoted else key
        if not quoted and " #" in value:
            err("bad-front-matter", path,
                "unquoted '#' in front-matter '{}' - YAML truncates at comments; "
                "double-quote the value".format(key))

    metadata, _ = parse_frontmatter(text)

    title = metadata.get("title")
    if title is not None:
        first = next((ln for ln in strip_front_matter(text).splitlines() if ln.strip()), "")
        if first.startswith("# ") and str(title).strip() != first[2:].strip():
            err("bad-front-matter", path,
                "front-matter title {!r} != H1 {!r}".format(str(title).strip(), first[2:].strip()))
    else:
        err("bad-front-matter", path, "front matter has no 'title'")

    wanted = expected_type(relative)
    actual = metadata.get("type")
    if gateway:
        return metadata  # gateways are infrastructure, not semantic notes
    if wanted is None:
        err("bad-front-matter", path,
            "note sits outside the guide's folder vocabulary; move it under one of "
            "{}".format(sorted(FOLDER_TYPES)))
    elif actual != wanted:
        err("bad-front-matter", path,
            "type {!r} != {!r} required for {}".format(actual, wanted, relative.as_posix()))

    if not str(metadata.get("description", "")).strip():
        err("bad-front-matter", path, "front matter has no 'description'")
    if not isinstance(metadata.get("tags"), list) or not metadata.get("tags"):
        err("bad-front-matter", path, "'tags' missing, empty, or not a list")

    # The epistemic contract. These are constants of the format, so a wrong
    # value is not a preference disagreement - it is a false claim about the
    # note's standing.
    if metadata.get("semantic_note") is not True:
        err("bad-front-matter", path, "every compiled note needs 'semantic_note: true'")
    if metadata.get("status") != "draft":
        err("bad-front-matter", path,
            "status must be 'draft' - machine-compiled notes are unverified by construction, "
            "got {!r}".format(metadata.get("status")))
    # OKF v0.2 types `verified` as a list of confirmation events and signals the
    # unverified tier by the key being ABSENT. Nothing in this pipeline verifies
    # a note, so the key must not appear at all - writing `verified: false` (the
    # v0.1 spelling) or `verified: []` would both put a value in a slot that is
    # read as "someone confirmed this". The v0.1 spelling is still accepted so
    # that already-published wikis keep linting; it is never written afresh.
    if "verified" in metadata and metadata["verified"] is not False:
        err("bad-front-matter", path,
            "'verified' must be absent - OKF v0.2 signals unverified by omitting the key, "
            "and nothing in this pipeline verifies a note, got {!r}".format(metadata["verified"]))
    sources = metadata.get("sources")
    if not isinstance(sources, list) or not sources:
        err("bad-front-matter", path, "'sources' must be a non-empty list of source locators")
    else:
        for entry in sources:
            # v0.2 entries are mappings carrying a `resource` URI; v0.1 prose
            # strings are still read so published wikis keep linting.
            if isinstance(entry, dict) and not str(entry.get("resource", "")).strip():
                err("bad-front-matter", path,
                    "each OKF v0.2 source entry needs a non-empty 'resource', got {!r}".format(entry))
                break
    return metadata


def main():
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    if len(args) != 1:
        sys.exit("usage: python lint.py <wiki-dir> [--json]")
    wiki_dir = Path(args[0]).resolve()
    if not wiki_dir.is_dir():
        sys.exit("not a directory: {}".format(wiki_dir))
    errors = lint(wiki_dir)
    if as_json:
        print(json.dumps({"errors": errors, "clean": not errors}, indent=2))
    else:
        for e in errors:
            print("{:18s} {}: {}".format(e["kind"], e["file"], e["detail"]))
        print("{} error(s)".format(len(errors)) if errors else "wiki is clean")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
