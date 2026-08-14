#!/usr/bin/env python3
"""Check that substantive quotations appear on the source pages a note cites.

This check needs no reference wiki, but it is deliberately narrow. It does not
judge paraphrase, emphasis, selection, completeness, or truth. It only tests
verbatim quotation, and a note may still mislead while quoting accurately.

Read the result as a lower bound, not a semantic score. The matcher accounts for
six common false-positive sources:

1. directional quote-character pairing avoids capturing prose between quotes;
2. only the gateway's fenced source text enters the comparison haystack;
3. editorial terminal punctuation is ignored;
4. sections that quote propositions merely to reject them are excluded;
5. ellipsis-separated fragments must appear in source order; and
6. extraction-split or soft-hyphenated words may be silently rejoined.

Unmatched text still requires inspection. It can reflect a real provenance
defect, or another extraction/editorial artifact such as a bracketed insertion,
nested quotation, or heading cited from a neighboring page.

Normalisation matters more than the matching does. Print-typeset sources break
words across lines with a soft hyphen ("mul<shy> tistep"), reflowed ebooks use
curly quotes and en dashes where a note may use straight ones, and every source
wraps lines at arbitrary points. Comparing raw strings would report dozens of
false failures — as it did to me by hand before this existed. So both sides are
folded to the same shape first.

Usage:
    hexwiki verify <wiki> [--min-length N] [--json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

SOFT_HYPHEN = "­"
GATEWAY_RE = re.compile(r"(?:sources/)?pdf-pages/page-(\d{4})\.md", re.I)
FRONT_MATTER_RE = re.compile(
    r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S
)
SEMANTIC_NOTE_RE = re.compile(
    r"(?m)^[ \t]*semantic_note[ \t]*:[ \t]*true[ \t]*(?:#.*)?$"
)
# Curly quotes are DIRECTIONAL, and that is the whole trick: match an opening
# quote to its closing one. Matching "any quote character to any quote
# character" also captures the prose BETWEEN one quotation and the next, which
# is the note's own writing and is not expected to appear in the source. That
# This distinction prevents the checker from treating a note's connecting prose
# as if the note had attributed it verbatim to the source.
QUOTE_RE = re.compile(r"“([^“”]{%d,}?)”", re.S)
SKIP_DIRS = {"_schema", ".obsidian", "reports", "audit"}


def normalise(text: str) -> str:
    """Fold a string to the shape both sides can be compared in."""
    text = unicodedata.normalize("NFKC", text)
    # A soft hyphen marks a typesetting line break inside a word; the word is
    # not really broken, so close it up along with any whitespace it introduced.
    text = re.sub(SOFT_HYPHEN + r"\s*", "", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―]", "-", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def needle_of(quote: str) -> str:
    """A quotation reduced to what must actually appear in the source.

    Terminal punctuation is editorial. A writer quoting a clause that ends in a
    comma will close the sentence with a period instead — "…prelates of the
    ninth century." for a source reading "…prelates of the ninth century," —
    and an exact substring test then fails on the last character. That single
    detail accounted for most of a 50% false "not found" rate here, so strip the
    edges of both sides rather than pretending the mismatch is a defect.
    """
    return normalise(quote).strip(" .,;:!?'\"-–—").strip("…").strip()


def supports(needle: str, haystack: str) -> bool:
    """Whether the source carries this quotation, allowing marked elision.

    A quotation may legitimately elide with an ellipsis — "a similar rumor…
    spreading there, too" — and an exact substring test can never match it,
    because the source has words in the gap. Requiring every fragment to appear
    IN ORDER is the honest reading of what the ellipsis promises: these words,
    from this passage, in this sequence, with something omitted between them.

    Fragments shorter than four characters are dropped; they match anywhere and
    would turn a real check into a rubber stamp.
    """
    parts = [p for p in re.split(r"\s*(?:…|\.\.\.)\s*", needle) if len(p.strip()) >= 4]
    if not parts:
        parts = [needle]

    def ordered_in(hay: str, fragments: list[str]) -> bool:
        cursor = 0
        for fragment in fragments:
            found = hay.find(fragment.strip(), cursor)
            if found < 0:
                return False
            cursor = found + len(fragment.strip())
        return True

    if ordered_in(haystack, parts):
        return True
    # Fallback: compare with all whitespace removed. Extraction splits words at
    # arbitrary points — extracted text may split a short word across spaces,
    # and print sources hyphenate across lines. The guide tells
    # the writer to repair such damage silently rather than propagate it, so a
    # note quoting a joined word from extraction-split letters is doing the right thing
    # and must not be scored as a fabrication. Safe for spans this long: a 25+
    # character letter sequence does not collide by accident.
    def squeeze(value: str) -> str:
        return re.sub(r"\s+", "", value)

    return ordered_in(squeeze(haystack), [squeeze(p) for p in parts])


def cited_pages(text: str) -> set[int]:
    return {int(n) for n in GATEWAY_RE.findall(text)}


def is_semantic_note(text: str) -> bool:
    """Whether leading YAML front matter declares this a semantic note.

    Reserved documentation such as WIKI_GUIDE.md contains literal front-matter
    examples. Searching the entire file for ``semantic_note: true`` therefore
    counts documentation as content and lets its prose distort quotation rates.
    Only the file's own leading front matter can classify the file.
    """
    front_matter = FRONT_MATTER_RE.match(text)
    return bool(front_matter and SEMANTIC_NOTE_RE.search(front_matter.group(1)))


# Sections where a quoted span is a PROPOSITION the note names, not words taken
# from the source. "What this licenses and what it does not" exists to quote
# inferences a reader might wrongly draw — '"Because the answer looks right, the
# intermediate process was logically sound"' is precisely a claim the note
# rejects, and demanding it appear in the source inverts its meaning. These
# sections are the wiki working correctly, so they are excluded rather than
# counted as failures.
CONSTRUCTED_SECTIONS = (
    "what this licenses and what it does not",
    "competing readings the material admits",
    "evidence limits",
    "connections",
)


def strip_constructed_sections(body: str) -> str:
    out, skipping = [], False
    for line in body.splitlines():
        heading = re.match(r"^##+\s+(.*?)\s*$", line)
        if heading:
            skipping = heading.group(1).strip().lower() in CONSTRUCTED_SECTIONS
        if not skipping:
            out.append(line)
    return "\n".join(out)


def quotations(text: str, minimum: int) -> list[str]:
    body = re.sub(r"(?ms)^---.*?^---\s*", "", text, count=1)   # drop front matter
    body = re.sub(r"(?m)^\s*\|.*\|\s*$", "", body)             # drop tables
    body = strip_constructed_sections(body)
    found = re.compile(QUOTE_RE.pattern % minimum, re.S).findall(body)
    out, seen = [], set()
    for item in found:
        item = item.strip()
        # A link, a heading marker, or an embedded quote means this span is the
        # note's own prose rather than words attributed to the source.
        if "](" in item or item.startswith("#") or '"' in item:
            continue
        key = needle_of(item)
        if key and key not in seen:
            seen.add(key)
            out.append(item)
    return out


def scoped_text(gateway: str) -> str:
    """Just the source text a gateway carries, without its own framing.

    A gateway note wraps the page in a title, headings, a fenced block and a
    Sources footer. Including that framing puts the wiki's own words into the
    haystack, and worse, it sits between one page's text and the next so a
    sentence broken across a page boundary can never be rejoined.
    """
    match = re.search(r"```+text\s*\n(.*?)\n```+", gateway, re.S)
    return match.group(1) if match else gateway


def verify(wiki: Path, minimum: int) -> dict:
    raw = {}
    for path in (wiki / "sources" / "pdf-pages").glob("page-*.md"):
        raw[int(path.stem.split("-")[1])] = scoped_text(path.read_text(encoding="utf-8"))
    # Pages break mid-sentence, so a quotation legitimately spans two of them.
    # Join in page order to rebuild the running text before matching.
    whole = normalise(" ".join(raw[page] for page in sorted(raw)))

    checked = supported = 0
    elsewhere: list[dict] = []
    unsupported: list[dict] = []
    notes = 0
    for path in sorted(wiki.rglob("*.md")):
        rel = path.relative_to(wiki)
        if any(part in SKIP_DIRS for part in rel.parts) or rel.parts[:2] == ("sources", "pdf-pages"):
            continue
        text = path.read_text(encoding="utf-8")
        if not is_semantic_note(text):
            continue
        notes += 1
        pages = cited_pages(text)
        # Join the cited pages in page order, from the raw text, so a sentence
        # that runs across a page break is matchable.
        haystack = normalise(" ".join(raw[p] for p in sorted(pages) if p in raw))
        for quote in quotations(text, minimum):
            checked += 1
            needle = needle_of(quote)
            if supports(needle, haystack):
                supported += 1
            elif supports(needle, whole):
                # Present in the scope but not on a page this note cites: a
                # provenance defect, not a fabrication.
                elsewhere.append({"note": rel.as_posix(), "quote": quote[:120]})
            else:
                unsupported.append({"note": rel.as_posix(), "quote": quote[:120]})
    return {
        "wiki": wiki.name,
        "semantic_notes": notes,
        "quotations_checked": checked,
        "supported_by_cited_page": supported,
        "in_scope_but_not_on_a_cited_page": elsewhere,
        "not_found_in_scope": unsupported,
        "support_rate": round(supported / checked, 4) if checked else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wiki", type=Path)
    parser.add_argument("--min-length", type=int, default=40,
                        help="shortest quotation to check; short spans are usually "
                             "scare quotes or single terms, not source quotation")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path,
                        help="also write the complete report as JSON")
    args = parser.parse_args()

    report = verify(args.wiki, args.min_length)
    report_json = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(report_json)
    if args.json:
        print(report_json, end="")
        return 0

    print(f"{report['wiki']}: {report['semantic_notes']} semantic notes, "
          f"{report['quotations_checked']} quotations >= {args.min_length} chars")
    rate = report["support_rate"]
    print(f"  supported by a cited page : {report['supported_by_cited_page']}"
          + (f"  ({rate:.1%})" if rate is not None else ""))
    print(f"  in scope, wrong page cited: {len(report['in_scope_but_not_on_a_cited_page'])}")
    print(f"  not found in scope        : {len(report['not_found_in_scope'])}")
    for item in report["in_scope_but_not_on_a_cited_page"][:5]:
        print(f"    [wrong page] {item['note']}: \"{item['quote'][:80]}\"")
    for item in report["not_found_in_scope"][:10]:
        print(f"    [NOT FOUND] {item['note']}: \"{item['quote'][:80]}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
