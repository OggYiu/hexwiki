#!/usr/bin/env python3
"""Dependency-free query and lint tools for generated LLM wikis."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import unquote


WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\]]+)\]\]|!\[\[([^\]]+)\]\]")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)
TITLE_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")
WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


@dataclass(frozen=True)
class LinkProblem:
    source: str
    target: str
    kind: str
    problem: str


def markdown_files(vault: Path) -> list[Path]:
    return sorted(path for path in vault.rglob("*.md") if ".git" not in path.parts)


def relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def note_title(path: Path, text: str) -> str:
    frontmatter = FRONTMATTER_RE.match(text)
    if frontmatter:
        match = re.search(r'(?m)^title:\s*["\']?(.*?)["\']?\s*$', frontmatter.group(1))
        if match:
            return match.group(1)
    match = TITLE_RE.search(text)
    return match.group(1) if match else path.stem


def normalize_wikilink(raw: str) -> str:
    target = raw.split("|", 1)[0].split("#", 1)[0].strip()
    return unquote(target).replace("\\", "/")


def resolve_wikilink(
    vault: Path,
    source: Path,
    raw: str,
    by_stem: dict[str, list[Path]],
) -> tuple[Path | None, str | None]:
    target = normalize_wikilink(raw)
    if not target:
        return source, None

    direct = vault / target
    direct_candidates = [direct]
    if not direct.suffix:
        direct_candidates.append(direct.with_suffix(".md"))
    for candidate in direct_candidates:
        if candidate.exists():
            return candidate.resolve(), None

    local = source.parent / target
    local_candidates = [local]
    if not local.suffix:
        local_candidates.append(local.with_suffix(".md"))
    for candidate in local_candidates:
        if candidate.exists():
            return candidate.resolve(), None

    stem = Path(target).stem.casefold()
    candidates = by_stem.get(stem, [])
    if len(candidates) == 1:
        return candidates[0].resolve(), None
    if len(candidates) > 1:
        options = ", ".join(relative_posix(path, vault) for path in candidates[:8])
        return None, f"ambiguous; candidates: {options}"
    return None, "target does not exist"


def markdown_target(raw: str) -> str:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    return unquote(target.split("#", 1)[0]).replace("\\", "/")


def is_external_link(target: str) -> bool:
    lowered = target.casefold()
    return (
        not target
        or target.startswith("#")
        or lowered.startswith(("http://", "https://", "mailto:", "data:", "obsidian:"))
    )


def resolve_markdown_link(vault: Path, source: Path, target: str) -> Path:
    """Resolve relative and vault-root Markdown links consistently."""

    if target.startswith("/"):
        resolved = (vault / target.lstrip("/")).resolve()
    else:
        resolved = (source.parent / target).resolve()
    if resolved.is_dir() and (resolved / "index.md").is_file():
        return (resolved / "index.md").resolve()
    return resolved


def lint_vault(vault: Path) -> dict[str, object]:
    vault = vault.resolve()
    if not vault.is_dir():
        raise ValueError(f"vault is not a directory: {vault}")

    notes = markdown_files(vault)
    by_stem: dict[str, list[Path]] = defaultdict(list)
    for path in notes:
        by_stem[path.stem.casefold()].append(path)

    problems: list[LinkProblem] = []
    inbound: Counter[str] = Counter()
    wikilinks = 0
    markdown_links = 0
    external_links = 0
    titles: dict[str, list[str]] = defaultdict(list)

    for source in notes:
        text = source.read_text(encoding="utf-8")
        source_rel = relative_posix(source, vault)
        titles[note_title(source, text).casefold()].append(source_rel)

        for match in WIKILINK_RE.finditer(text):
            raw = match.group(1) or match.group(2)
            wikilinks += 1
            resolved, error = resolve_wikilink(vault, source, raw, by_stem)
            if error:
                problems.append(LinkProblem(source_rel, raw, "wikilink", error))
            elif resolved is not None:
                try:
                    inbound[relative_posix(resolved, vault)] += 1
                except ValueError:
                    pass

        for match in MARKDOWN_LINK_RE.finditer(text):
            raw = match.group(1)
            target = markdown_target(raw)
            if is_external_link(target):
                external_links += 1
                continue
            markdown_links += 1
            resolved = resolve_markdown_link(vault, source, target)
            if not resolved.exists():
                problems.append(
                    LinkProblem(source_rel, raw, "markdown-link", "target does not exist")
                )
            else:
                try:
                    inbound[relative_posix(resolved, vault)] += 1
                except ValueError:
                    pass

    duplicate_titles = {
        title: paths for title, paths in sorted(titles.items()) if len(paths) > 1
    }
    exempt_names = {"AGENTS.md", "README.md", "index.md", "log.md"}
    orphans = [
        relative_posix(path, vault)
        for path in notes
        if path.name not in exempt_names
        and inbound[relative_posix(path, vault)] == 0
        and not relative_posix(path, vault).startswith("reports/")
    ]

    return {
        "status": "passed" if not problems and not duplicate_titles and not orphans else "failed",
        "vault": str(vault),
        "counts": {
            "markdown_files": len(notes),
            "wikilinks": wikilinks,
            "markdown_links": markdown_links,
            "external_links": external_links,
            "broken_or_ambiguous_links": len(problems),
            "duplicate_titles": len(duplicate_titles),
            "orphan_notes": len(orphans),
        },
        "problems": [asdict(problem) for problem in problems],
        "duplicate_titles": duplicate_titles,
        "orphans": orphans,
    }


def query_vault(vault: Path, query: str, limit: int) -> list[dict[str, object]]:
    vault = vault.resolve()
    terms = [term.casefold() for term in WORD_RE.findall(query)]
    if not terms:
        raise ValueError("query must contain at least one word")

    results: list[dict[str, object]] = []
    for path in markdown_files(vault):
        text = path.read_text(encoding="utf-8")
        title = note_title(path, text)
        title_folded = title.casefold()
        path_folded = relative_posix(path, vault).casefold()
        body_folded = text.casefold()
        counts = {term: body_folded.count(term) for term in terms}
        if not all(counts.values()):
            continue
        score = sum(
            counts[term]
            + 8 * title_folded.count(term)
            + 3 * path_folded.count(term)
            for term in terms
        )
        first_position = min(body_folded.find(term) for term in terms)
        start = max(0, first_position - 90)
        end = min(len(text), first_position + 220)
        snippet = re.sub(r"\s+", " ", text[start:end]).strip()
        results.append(
            {
                "score": score,
                "title": title,
                "path": relative_posix(path, vault),
                "snippet": snippet,
            }
        )
    results.sort(key=lambda item: (-int(item["score"]), str(item["path"])))
    return results[:limit]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    lint_parser = subparsers.add_parser("lint", help="validate links, titles, and reachability")
    lint_parser.add_argument("vault", type=Path)
    lint_parser.add_argument("--json", action="store_true", dest="as_json")

    query_parser = subparsers.add_parser("query", help="rank full-text matches")
    query_parser.add_argument("vault", type=Path)
    query_parser.add_argument("query")
    query_parser.add_argument("--limit", type=int, default=10)
    query_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "lint":
            report = lint_vault(args.vault)
            if args.as_json:
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                counts = report["counts"]
                print(f"status: {report['status']}")
                for key, value in counts.items():
                    print(f"{key}: {value}")
                for problem in report["problems"]:
                    print(
                        f"{problem['source']}: {problem['kind']} "
                        f"{problem['target']!r}: {problem['problem']}"
                    )
                for title, paths in report["duplicate_titles"].items():
                    print(f"duplicate title {title!r}: {', '.join(paths)}")
                for path in report["orphans"]:
                    print(f"orphan: {path}")
            return 0 if report["status"] == "passed" else 1

        results = query_vault(args.vault, args.query, args.limit)
        if args.as_json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            for index, result in enumerate(results, 1):
                print(f"{index}. [{result['score']}] {result['title']} — {result['path']}")
                print(f"   {result['snippet']}")
        return 0
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
