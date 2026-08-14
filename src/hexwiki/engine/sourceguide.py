"""Deterministic Source Guide and locator-map notes.

The provenance family of a source-bounded wiki states machine-knowable facts:
which units are in scope, what the extraction pipeline produced, which layer is
authoritative, which boundaries were clipped, and what the source's own citation
apparatus contains. None of that is a judgement call, so none of it is left to
the model — it is derived here from the already-verified staged source and the
extraction manifest, and the model is told to link to it.

Everything is driven by the profile and the staged page records; no note text
below names a subject, only a structure.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import source
from .audit import AuditLog, atomic_text


NUMBERED_ENTRY_RE = re.compile(r"(?m)^(\d{1,3})\.\s+")
# Layers a reader can audit against, in the order this wiki trusts them.
EVIDENCE_LAYERS = [
    ("text/native-pages", "Native per-page text embedded in the PDF",
     "authoritative for born-digital pages"),
    ("text/reading-order-pages", "Reading-order reconstruction of the same pages",
     "corroborating; resolves column and flow ambiguity"),
    ("pages", "Page raster renders (PNG)",
     "visual adjudication of anything the text layers disagree on"),
    ("pages-svg", "Vector page renders (SVG)",
     "glyph-level inspection of damaged or unusual text"),
    ("ocr", "OCR of page images", "fallback only, for pages with no native text"),
    ("layout", "Raw layout blocks with coordinates", "diagnostic; explains ordering artifacts"),
]


def _yaml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _front_matter(
    *, note_type: str, title: str, description: str, sources: list[str],
    pages: list[int], tags: list[str],
) -> str:
    source_title = sources[0] if sources else title
    source_entries = "".join(
        "  - { id: " + _yaml(f"pdf-page-{page:04d}")
        + ", resource: " + _yaml(f"sources/pdf-pages/page-{page:04d}.md")
        + ", title: " + _yaml(f"{source_title} — PDF page {page}") + " }\n"
        for page in pages
    )
    return (
        "---\n"
        f"type: {note_type}\n"
        f"title: {_yaml(title)}\n"
        f"description: {_yaml(description)}\n"
        "semantic_note: true\n"
        "status: draft\n"
        # No `verified` key: OKF v0.2 reads the unverified tier from its absence.
        # Omission is the OKF v0.2 representation of an unverified draft.
        "sources:\n"
        f"{source_entries}"
        "generated: { by: \"hexwiki/deterministic\" }\n"
        f"pdf_pages: [{', '.join(str(page) for page in pages)}]\n"
        f"tags: [{', '.join(tags)}]\n"
        "---\n\n"
    )


def _gateway_link(page: int, depth: int) -> str:
    return f"[PDF p. {page}]({'../' * depth}sources/pdf-pages/page-{page:04d}.md)"


def _citation(profile: dict[str, Any], detail: str) -> str:
    return (
        f"- *{profile['document_title']}* ({profile['document_author']}), "
        f"{profile['scope_label']} — {detail}\n"
    )


def _all_pages_link_line(profile: dict[str, Any], detail: str) -> str:
    """Link every page a scope-wide note declares.

    A note about the whole scope legitimately carries all of its pages in
    `pdf_pages`, but declaring a locator and never linking it is the same
    unjustified citation the linter rejects everywhere else. Scope-wide notes do
    not get an exemption; they get the links.
    """
    pages = [int(page) for page in profile["primary_pages"] + profile["apparatus_pages"]]
    links = ", ".join(_gateway_link(page, 1) for page in pages)
    return f"- {detail}: {links}\n"


def _numbered_entries(
    pages: dict[int, dict[str, Any]], apparatus_pages: list[int], first: int = 1,
    pattern: "re.Pattern[str] | None" = None,
) -> list[dict[str, Any]]:
    """Split the scoped citation apparatus into one record per numbered entry.

    Numbers are taken as a running sequence rather than as every line-start
    digit: a citation whose page range wraps ("…1,103-\\n104.") otherwise
    becomes a fabricated entry 104 in the inventory, sitting in the table as if
    the source had printed it.
    """
    entries: list[dict[str, Any]] = []
    expected = first
    for page in apparatus_pages:
        text = pages[page]["text"]
        starts = []
        for match in (pattern or NUMBERED_ENTRY_RE).finditer(text):
            if int(match.group(1)) == expected:
                starts.append(match)
                expected += 1
        for index, match in enumerate(starts):
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            entries.append(
                {
                    "number": int(match.group(1)),
                    "text": re.sub(r"\s+", " ", text[match.end():end].strip()),
                    "page": page,
                }
            )
    return entries


def _duplicated_blocks(pages: dict[int, dict[str, Any]], scope: list[int],
                       minimum: int = 120) -> dict[int, str]:
    """Pages whose extracted text repeats a long passage verbatim.

    Printed pages sometimes carry a duplicated block — a running head repeated
    into the body, a paragraph set twice at a division boundary. A reader who
    meets the same sentence twice needs to know it is an artifact of the page
    rather than the source saying something twice, and the check costs nothing.
    """
    found: dict[int, str] = {}
    for page in scope:
        text = re.sub(r"\s+", " ", pages[page]["text"]).strip()
        best = ""
        for start in range(0, max(0, len(text) - minimum), 20):
            window = text[start:start + minimum]
            if text.count(window) > 1 and len(window) > len(best):
                best = window
        if best:
            found[page] = best[:110].strip()
    return found


SOFT_HYPHEN = "­"


def _hyphenated_words(pages: dict[int, dict[str, Any]], scope: list[int]) -> dict[int, int]:
    """Pages whose extracted text splits words at a typesetting line break.

    Print typesetting can hyphenate across lines, and an extractor may preserve
    the soft hyphen: "mul<shy> tistep", "reason<shy> ing". Its presence and
    frequency are properties of a particular edition.

    It matters twice over. A reader searching a gateway page for "multistep" will
    not find it, and the independent reviewer checking whether a claim appears on
    a page can read a present phrase as absent — the same shape as the packet
    truncation bug, though far milder in effect. The canonical text stays
    faithful to the extraction; the anomaly is declared instead.
    """
    return {
        page: pages[page]["text"].count(SOFT_HYPHEN)
        for page in scope
        if SOFT_HYPHEN in pages[page]["text"]
    }


def _boundary_gloss(page: int, profile: dict[str, Any]) -> str:
    """What the clip removed, taken from the clipper rather than guessed."""
    return source.boundary_note(page, profile) or (
        "part of this page falls outside the scope and was cut at a verified marker"
    )


def _entries_per_page(
    pages: dict[int, dict[str, Any]], apparatus_pages: list[int],
    first: int = 1,
    pattern: "re.Pattern[str] | None" = None,
) -> dict[int, list[int]]:
    grouped: dict[int, list[int]] = {}
    for entry in _numbered_entries(
        pages, apparatus_pages, first=first, pattern=pattern
    ):
        grouped.setdefault(entry["page"], []).append(entry["number"])
    return grouped


def _scope_note(profile: dict[str, Any], pages: dict[int, dict[str, Any]]) -> str:
    primary = [int(p) for p in profile["primary_pages"]]
    apparatus = [int(p) for p in profile["apparatus_pages"]]
    apparatus_present = source.has_apparatus(profile)
    clipped = [
        (page, pages[page]["boundary_action"])
        for page in primary + apparatus
        if pages[page]["boundary_action"] != "none"
    ]
    body = _front_matter(
        note_type="Source Guide",
        title=f"{profile['scope_title']} source and scope",
        description=(
            f"Exact inclusion and exclusion boundaries for this wiki: "
            f"{profile['document_title']} {profile['scope_label']}, primary PDF pages "
            f"{', '.join(str(page) for page in primary)}"
            + (
                f" plus apparatus pages {', '.join(str(page) for page in apparatus)}"
                if apparatus_present else ""
            )
            + "; states what was deliberately excluded and that no prior or reference "
            "wiki was imported."
        ),
        sources=[f"{profile['document_title']}, {profile['scope_label']}"],
        pages=primary + apparatus,
        tags=["sources", profile["document_id"], "scope", "provenance"],
    )
    body += f"# {profile['scope_title']} source and scope\n\n"
    body += (
        "**The exact boundary of this wiki: everything inside it comes from the pages "
        "listed here, and nothing else in the work is cited.**\n\n"
    )
    body += "## In scope\n\n"
    body += (
        f"- Primary text: PDF pages {', '.join(str(page) for page in primary)} "
        f"({len(primary)} pages), covering {profile['scope_label']}.\n"
    )
    if apparatus_present:
        body += (
            f"- Citation apparatus: PDF pages {', '.join(str(p) for p in apparatus)}, "
            f"limited to the {profile['apparatus_label']} belonging to this scope.\n"
        )
    else:
        body += (
            "- Citation apparatus: none declared for this scope. There is no numbered "
            "apparatus inventory to consult and no apparatus entry for a claim to cite. "
            "Any attribution printed in the primary text remains visible on its page.\n"
        )
    body += (
        f"- Canonical scoped text: {profile['canonical_scope_characters']:,} characters, "
        f"SHA-256 `{profile['canonical_scope_sha256']}`.\n\n"
    )
    body += "## Out of scope\n\n"
    body += (
        "- Every PDF page not listed above. The profile enumerates exact pages rather than "
        "implying that every page between the first and last is present.\n"
        "- Any material the scoped text explicitly defers to outside those pages. Such "
        "references are recorded where they occur and are never resolved from outside the "
        "scope.\n"
        "- All external literature. Works named inside the scope are recorded as "
        "citations only; their contents were not consulted.\n"
        "- Any prior, reference, or comparison wiki. None was read during generation; "
        "the source manifest records that boundary.\n\n"
    )
    body += "## Boundary handling\n\n"
    if clipped:
        body += (
            "Pages that carry material from more than one division were clipped at a "
            "verified textual marker, so no out-of-scope sentence entered the wiki. Each row "
            "says which side of the marker was kept:\n\n"
            "| PDF page | Boundary action | What this means for the page |\n|---|---|---|\n"
        )
        for page, action in clipped:
            gloss = _boundary_gloss(page, profile)
            discarded = pages[page].get("discarded")
            body += (
                f"| {_gateway_link(page, 1)} | `{action}` | {gloss}"
                + (f"; {discarded}" if discarded else "")
                + " |\n"
            )
        body += "\n"
    else:
        body += "No scoped page required clipping; each falls wholly inside the scope.\n\n"

    # Furniture is removed from every page rather than cut at one boundary, so it
    # gets its own account. Deleting source text without saying so would be the
    # exact failure this bundle exists to make impossible.
    furniture = sorted({
        reason for page in primary + apparatus
        for reason in pages[page].get("furniture_removed", [])
    })
    if furniture:
        body += "## Page furniture removed\n\n"
        body += (
            "The edition repeats material on every page that belongs to the printing "
            "rather than to the work. It was removed before the scope was hashed, so the "
            "canonical text contains the in-scope content and not that furniture. What was removed, and "
            "why:\n\n"
        )
        for reason in furniture:
            body += f"- {reason}\n"
        body += (
            "\nThis is the only text deleted from the scoped pages. Everything else on "
            "them is present verbatim, and the page gateways show exactly what was kept.\n\n"
        )
    first_entry = int(profile["apparatus_range"][0]) if apparatus_present else 1
    per_page = _entries_per_page(
        pages,
        apparatus,
        first=first_entry,
        pattern=source.entry_pattern(profile),
    )
    if per_page:
        body += (
            "The citation apparatus is split across its pages as follows; a page holding only "
            "part of the scope's entries holds later entries belonging to other divisions, "
            "which were cut:\n\n"
            "| PDF page | Entries on this page |\n|---|---|\n"
        )
        for page, numbers in per_page.items():
            span = (f"{numbers[0]}–{numbers[-1]}" if len(numbers) > 1 else str(numbers[0]))
            body += f"| {_gateway_link(page, 1)} | {span} ({len(numbers)} entries) |\n"
        body += "\n"
    body += (
        "Nothing was imported from any prior, reference, or comparison wiki: every sentence "
        "in this bundle was written from the pages above during this run.\n\n"
    )
    body += "## Connections\n\n"
    body += (
        "- [Provenance and limitations](provenance-and-limitations.md) — which evidence "
        "layer is authoritative and what is known to be imperfect.\n"
        "- [Extraction and audit](extraction-and-audit.md) — how to re-verify any claim "
        "against the source.\n"
    )
    if apparatus_present:
        # Linked only when the apparatus note is actually written, or the link
        # dangles and the linter reports a broken link on generated material.
        body += (
            f"- [{profile['scope_title']} {profile['apparatus_label']}]"
            f"({profile['apparatus_slug']}.md) — the citation entries inside this "
            "boundary, transcribed verbatim.\n"
        )
    body += (
        f"- [{profile['scope_title']} page map](../reference/pdf-page-map.md) — what is on "
        "each scoped page.\n\n"
    )
    body += "## Sources\n\n"
    body += _citation(profile, "the scoped pages themselves define this boundary")
    body += (
        f"- Source PDF SHA-256 `{profile['source_pdf_sha256']}` — pinned by the profile "
        "lock and checked again while staging.\n"
    )
    body += _all_pages_link_line(profile, "Every page inside the boundary")
    return body


def _provenance_note(
    profile: dict[str, Any], pages: dict[int, dict[str, Any]], extraction: dict[str, Any]
) -> str:
    all_pages = [int(p) for p in profile["primary_pages"] + profile["apparatus_pages"]]
    counts = extraction.get("counts", {})
    empty = set(counts.get("pages_without_native_text", []))
    scoped_empty = sorted(empty & set(all_pages))
    body = _front_matter(
        note_type="Source Guide",
        title=f"{profile['scope_title']} provenance and limitations",
        description=(
            "The evidence layers behind every note, which layer is authoritative, the "
            "extraction anomalies known to affect the scoped pages, and the attribution "
            "vocabulary this wiki uses to keep report, source chain, interpretation, and "
            "inference apart."
        ),
        sources=[f"{profile['document_title']}, {profile['scope_label']}"],
        pages=all_pages,
        tags=["sources", profile["document_id"], "provenance", "evidence-limits"],
    )
    body += f"# {profile['scope_title']} provenance and limitations\n\n"
    body += (
        "**Every statement in this wiki rests on one extraction layer of one PDF; this "
        "note says which layer, how far it can be trusted, and where it is known to be "
        "imperfect.**\n\n"
    )
    body += "## Evidence layers, most authoritative first\n\n"
    body += "| Layer | What it is | Standing |\n|---|---|---|\n"
    for path, what, standing in EVIDENCE_LAYERS:
        body += f"| `{path}` | {what} | {standing} |\n"
    body += (
        "\nThe wiki's notes are written from the native per-page text. The immutable "
        "copies exposed under `sources/pdf-pages/` are that layer verbatim; a quotation "
        "that does not appear there is not supported by this wiki.\n\n"
    )
    body += "## Known limitations of this extraction\n\n"
    body += (
        "- Native PDF text preserves the typesetting of the printed edition, including "
        "its own typographic and spelling errors. Obvious character-level artifacts are "
        "corrected silently in note prose; the gateway pages keep the raw form.\n"
        "- Line breaks and hyphenation follow the printed column, so a phrase may be "
        "split across lines in the gateway text.\n"
        "- Page renders and OCR were not used to write notes and are listed only as "
        "audit paths.\n"
    )
    if scoped_empty:
        body += (
            f"- Scoped pages with no native text layer: "
            f"{', '.join(str(p) for p in scoped_empty)}. OCR may provide a diagnostic "
            "fallback in the extraction bundle, but it is not silently substituted for "
            "the authoritative native layer.\n"
        )
    for page, snippet in _duplicated_blocks(pages, all_pages).items():
        body += (
            f"- PDF page {page} repeats a passage verbatim in the extracted text, beginning "
            f"\"{snippet}…\". A reader meeting that sentence twice is seeing a page artifact, "
            "not the source saying it twice; notes drawing on this page cite it once.\n"
        )
    hyphenated = _hyphenated_words(pages, all_pages)
    if hyphenated:
        total = sum(hyphenated.values())
        listed = ", ".join(str(p) for p in sorted(hyphenated))
        body += (
            f"- The edition hyphenates words across line breaks, and {total} such breaks "
            f"survive in the extracted text on PDF pages {listed}. A word may therefore "
            "appear split — \"multi-\" on one line and its ending on the next — so a search "
            "of a page's text can miss a word that is plainly there on the printed page. "
            "Notes quote such words whole, which is a silent repair of the extraction "
            "rather than a departure from the source.\n"
        )
    clipped = [
        (p, pages[p]["boundary_action"]) for p in all_pages
        if pages[p]["boundary_action"] != "none"
    ]
    if clipped:
        body += (
            f"- Clipped boundary pages ({', '.join(str(p) for p, _ in clipped)}) contain "
            "material from adjoining divisions; only the in-scope remainder was used. See "
            "[source and scope](source-and-scope.md).\n"
        )
    body += "\n## Attribution vocabulary\n\n"
    body += (
        "The wiki keeps four things apart, and its wording signals which is which:\n\n"
        "| Phrase | Means |\n|---|---|\n"
        "| \"the source reports\", \"the record states\" | **Reported account** — "
        "what a person or document is described as saying. The wiki does not endorse it. |\n"
        "| \"reported by X, transmitted through Y\" | **Source chain** — how the account "
        "reached the scoped text, and how many hands it passed through. |\n"
        "| \"the author argues\", \"the section presents this as\" | **Author interpretation** "
        "— a conclusion belonging to the scoped source, not to the wiki. |\n"
        "| \"the scoped text offers no…\", \"within this scope, nothing distinguishes…\" | "
        "**Wiki inference** — an observation about the material that the source did not "
        "make. Kept rare and modest. |\n\n"
        "Absence of support is stated explicitly rather than left implied: a note that "
        "says nothing about corroboration is weaker than one that says no corroborating "
        "record appears in scope.\n\n"
    )
    body += "## Connections\n\n"
    body += (
        "- [Source and scope](source-and-scope.md) — the boundary these layers were "
        "read within.\n"
        "- [Extraction and audit](extraction-and-audit.md) — the verification chain.\n"
    )
    if source.has_apparatus(profile):
        body += (
            f"- [{profile['scope_title']} {profile['apparatus_label']}]"
            f"({profile['apparatus_slug']}.md) — where the attribution vocabulary "
            "is applied to the source's own citations.\n"
        )
    body += "\n"
    body += "## Sources\n\n"
    body += _citation(profile, "the scoped native text is the evidence base for every note")
    body += (
        f"- Extraction manifest status `{extraction.get('status', 'unknown')}`, extractor "
        f"version `{extraction.get('extractor_version', 'unknown')}` — layer inventory and "
        "per-page counts.\n"
    )
    body += _all_pages_link_line(
        profile, "Every scoped page whose provenance this note describes")
    return body


def _audit_note(profile: dict[str, Any], extraction: dict[str, Any]) -> str:
    all_pages = [int(p) for p in profile["primary_pages"] + profile["apparatus_pages"]]
    body = _front_matter(
        note_type="Source Guide",
        title=f"{profile['scope_title']} extraction and audit",
        description=(
            "The verification chain from the pinned source PDF to the scoped text used by "
            "this wiki, and the step-by-step path a reader follows to re-check any claim "
            "against the original page."
        ),
        sources=[f"{profile['document_title']}, {profile['scope_label']}"],
        pages=all_pages,
        tags=["sources", profile["document_id"], "audit", "reproducibility"],
    )
    body += f"# {profile['scope_title']} extraction and audit\n\n"
    body += (
        "**How the text behind this wiki was obtained and verified, and how to check any "
        "sentence in it against the source.**\n\n"
    )
    body += "## Verification chain\n\n"
    body += (
        f"1. The source PDF was pinned by hash in a separate profile lock: "
        f"`{profile['source_pdf_sha256']}`, and staging recomputed that hash before writing.\n"
        "2. The extraction bundle's own manifest and validation reports were required to "
        f"read `passed` (they report `{extraction.get('status', 'unknown')}`).\n"
        "3. Each scoped page's native text file was checked against the extraction "
        "checksum inventory before being read.\n"
        "4. Boundary pages were clipped at verified textual markers, so the scope contains "
        "no sentence from an adjoining division.\n"
        f"5. The concatenated scope was required to match "
        f"{profile['canonical_scope_characters']:,} characters and SHA-256 "
        f"`{profile['canonical_scope_sha256']}` before any note was written.\n"
        + (
            "6. The scoped citation apparatus was required to be continuous and complete.\n"
            if source.has_apparatus(profile) else
            "6. This profile declares no numbered citation apparatus, so no continuity "
            "check applies; the absence is explicit rather than an omitted step.\n"
        )
        + "7. Every note was required to link the specific source pages it cites, and the "
        "finished wiki was sealed with a full-tree checksum inventory.\n\n"
    )
    body += "## Auditing a claim\n\n"
    body += (
        "1. Find the claim's note and read its `## Sources` section, which names the "
        "specific pages.\n"
        "2. Follow the `PDF p. N` link to the immutable gateway under "
        "`sources/pdf-pages/`; that file holds the scoped native text of the page verbatim.\n"
        "3. If the gateway text is ambiguous — a broken column, a suspect character — "
        "compare the other layers listed in "
        "[provenance and limitations](provenance-and-limitations.md).\n"
        "4. If the note's wording goes beyond what the page supports, that is a defect. "
        "Notes in this wiki are unverified drafts and are labelled as such in their front "
        "matter.\n\n"
    )
    body += "## Reproducing the bundle\n\n"
    body += (
        "`manifest.json` records the profile, lock, source, scope hashes, and validation state; "
        "`reports/` holds the deterministic and any separately produced review artifacts; "
        "`checksums.sha256` covers every file in the wiki; `audit/actions.jsonl` records "
        "what each step did, why, and how.\n\n"
    )
    body += "## Connections\n\n"
    body += (
        "- [Source and scope](source-and-scope.md) — what the chain was applied to.\n"
        "- [Provenance and limitations](provenance-and-limitations.md) — what the chain "
        "cannot rule out.\n\n"
    )
    body += "## Sources\n\n"
    body += _citation(profile, "the pinned PDF and its verified extraction bundle")
    body += _all_pages_link_line(profile, "Every scoped page the verification chain covers")
    return body


def _apparatus_note(
    profile: dict[str, Any], entries: list[dict[str, Any]], apparatus_pages: list[int]
) -> str:
    numbers = [entry["number"] for entry in entries]
    body = _front_matter(
        note_type="Source Guide",
        title=f"{profile['scope_title']} {profile['apparatus_label']}",
        description=(
            f"Complete inventory of {profile['apparatus_label']} "
            f"{numbers[0]}-{numbers[-1]} for {profile['scope_label']}, each with its "
            "verbatim text and PDF page, plus the back-reference and attribution cautions "
            "that apply when reading them."
        ),
        sources=[f"{profile['document_title']}, {profile['scope_label']}"],
        pages=apparatus_pages,
        tags=["sources", profile["document_id"], "citations", "apparatus"],
    )
    body += f"# {profile['scope_title']} {profile['apparatus_label']}\n\n"
    body += (
        f"**All {len(entries)} {profile['apparatus_label']} belonging to "
        f"{profile['scope_label']}, transcribed from the scoped pages so every citation in "
        "this wiki can be traced to what the source actually printed.**\n\n"
    )
    body += "| # | Entry as printed | Page |\n|---:|---|---|\n"
    for entry in entries:
        text = entry["text"].replace("|", "\\|")
        body += f"| {entry['number']} | {text} | {_gateway_link(entry['page'], 1)} |\n"
    body += "\n## Reading these entries\n\n"
    body += (
        "- The entries are citations, not evidence. Listing a work here records what the "
        "source cited; it does not verify that the cited work says what the scoped text "
        "reports it saying. None of the cited works was consulted.\n"
        "- Back-references such as `Ibid.` and `op. cit.` inherit their target from the "
        "preceding entry; resolving one wrongly silently reassigns an attribution, so "
        "each is read only against its immediate neighbour.\n"
        "- Some entries identify a periodical, issue, or section without page numbers; a "
        "claim resting on such an entry cannot be pinned more precisely than the entry "
        "itself.\n"
        "- Where a claim in the scoped primary text carries no entry at all, its note says "
        "so under evidence limits.\n\n"
    )
    body += "## Connections\n\n"
    body += (
        "- [Source and scope](source-and-scope.md) — why only these entries are in scope.\n"
        "- [Provenance and limitations](provenance-and-limitations.md) — the attribution "
        "vocabulary applied to them.\n\n"
    )
    body += "## Sources\n\n"
    body += _citation(
        profile,
        f"{profile['apparatus_label']} {numbers[0]}-{numbers[-1]}, transcribed verbatim",
    )
    for page in apparatus_pages:
        body += f"- {_gateway_link(page, 1)} — canonical scoped text of the apparatus page.\n"
    return body


def _opening(text: str, limit: int = 64) -> str:
    """The page's first words, verbatim.

    A locator table is only useful if a reader scanning it can tell which row
    holds the passage they remember. A machine cannot summarise a page it has
    not read, but it can quote where the page starts — which is enough to find
    a passage, and cannot misdescribe one.
    """
    flat = re.sub(r"\s+", " ", text).strip()
    clipped = flat[:limit].rstrip()
    return (clipped + "…" if len(flat) > limit else clipped).replace("|", "\\|") or "—"


def _page_map_note(profile: dict[str, Any], pages: dict[int, dict[str, Any]]) -> str:
    ordered = [int(p) for p in profile["primary_pages"] + profile["apparatus_pages"]]
    body = _front_matter(
        note_type="Source Guide",
        title=f"{profile['scope_title']} PDF page map",
        description=(
            f"One row per scoped PDF page ({ordered[0]}-{ordered[-1]}) giving its role, "
            "boundary handling, size, text hash, and a direct link to the canonical page "
            "text — the lookup table for locating any passage in the source."
        ),
        sources=[f"{profile['document_title']}, {profile['scope_label']}"],
        pages=ordered,
        tags=["reference", profile["document_id"], "page-map", "locators"],
    )
    body += f"# {profile['scope_title']} PDF page map\n\n"
    body += (
        "**Every PDF page inside this wiki's scope, what it holds, and where to read its "
        "exact text.**\n\n"
    )
    body += (
        "| PDF page | Role | Opens with | Boundary | Chars | Text SHA-256 | Canonical text | "
        "Other layers |\n"
    )
    body += "|---:|---|---|---|---:|---|---|---|\n"
    for page in ordered:
        item = pages[page]
        role = "primary text" if item["section"] == "primary_text" else "citation apparatus"
        boundary = "—" if item["boundary_action"] == "none" else f"`{item['boundary_action']}`"
        layers = " · ".join([
            f"`text/reading-order-pages/page-{page:04d}.txt`",
            f"`pages/page-{page:04d}.png`",
            f"`pages-svg/page-{page:04d}.svg`",
        ])
        body += (
            f"| {page} | {role} | {_opening(item['text'])} | {boundary} | "
            f"{len(item['text']):,} | `{item['sha256'][:16]}…` | {_gateway_link(page, 1)} | "
            f"{layers} |\n"
        )
    body += (
        f"\nTotal: {len(ordered)} pages, "
        f"{sum(len(pages[p]['text']) for p in ordered):,} characters of scoped page text.\n\n"
    )
    body += "## Other layers for the same pages\n\n"
    body += (
        "The wiki's notes come from the native text layer. For a page whose native text "
        "looks wrong, the extraction bundle also holds a reading-order reconstruction, a "
        "PNG render, an SVG render, an OCR pass, and raw layout blocks, each under the "
        "path given in "
        "[provenance and limitations](../sources/provenance-and-limitations.md). "
        "Those layers are audit material; nothing in this wiki was written from them.\n\n"
    )
    body += "## Connections\n\n"
    body += (
        "- [Source and scope](../sources/source-and-scope.md) — why these pages and no "
        "others.\n"
        "- [Extraction and audit](../sources/extraction-and-audit.md) — how to use this "
        "map to check a claim.\n\n"
    )
    body += "## Sources\n\n"
    body += _citation(profile, "page-level inventory of the scoped extraction")
    return body


def _folder_index(title: str, orientation: str, rows: list[tuple[str, str, str]]) -> str:
    lines = [f"# {title}", "", orientation, ""]
    for note_title, target, hook in rows:
        lines.append(f"- [{note_title}]({target}) — {hook}")
    return "\n".join(lines) + "\n"


def build_source_guides(
    *, wiki_dir: Path, pages: dict[int, dict[str, Any]], profile: dict[str, Any],
    extraction_root: Path, audit: AuditLog,
) -> list[str]:
    """Write the machine-derivable Source Guide family before the model runs."""
    extraction = json.loads((extraction_root / "manifest.json").read_text(encoding="utf-8"))
    apparatus_pages = [int(p) for p in profile["apparatus_pages"]]
    first_entry = int(profile["apparatus_range"][0]) if apparatus_pages else 1
    last_entry = int(profile["apparatus_range"][1]) if apparatus_pages else 0
    entries = _numbered_entries(
        pages,
        apparatus_pages,
        first=first_entry,
        pattern=source.entry_pattern(profile),
    )
    entries = [entry for entry in entries if entry["number"] <= last_entry]
    if apparatus_pages and [entry["number"] for entry in entries] != list(
        range(first_entry, last_entry + 1)
    ):
        raise ValueError("staged apparatus entries do not match the locked entry range")

    written: list[str] = []

    def emit(relative: str, text: str) -> None:
        path = wiki_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_text(path, text.rstrip() + "\n")
        written.append(relative)

    emit("sources/source-and-scope.md", _scope_note(profile, pages))
    emit("sources/provenance-and-limitations.md", _provenance_note(profile, pages, extraction))
    emit("sources/extraction-and-audit.md", _audit_note(profile, extraction))
    # A work with no numbered apparatus declares no label or slug for one, so
    # these are read only when there is something to name.
    if entries:
        apparatus_slug = profile["apparatus_slug"]
        emit(f"sources/{apparatus_slug}.md", _apparatus_note(profile, entries, apparatus_pages))
    emit("reference/pdf-page-map.md", _page_map_note(profile, pages))

    source_rows = [
        (f"{profile['scope_title']} source and scope", "source-and-scope.md",
         "exact inclusion and exclusion boundaries."),
        (f"{profile['scope_title']} provenance and limitations", "provenance-and-limitations.md",
         "evidence layers, known anomalies, attribution vocabulary."),
        (f"{profile['scope_title']} extraction and audit", "extraction-and-audit.md",
         "verification chain and how to re-check a claim."),
    ]
    if entries:
        source_rows.append(
            (f"{profile['scope_title']} {profile['apparatus_label']}",
             f"{apparatus_slug}.md",
             f"verbatim inventory of {profile['apparatus_label']} "
             f"{entries[0]['number']}-{entries[-1]['number']}.")
        )
    source_rows.append(
        ("Scoped source pages", "pdf-pages/index.md",
         "immutable canonical text, one file per PDF page.")
    )
    emit(
        "sources/index.md",
        _folder_index(
            "Source guides",
            "Where the wiki's material comes from, how far it can be trusted, and how to "
            "audit it. Inclusion is not verification.",
            source_rows,
        ),
    )
    emit(
        "reference/index.md",
        _folder_index(
            "Reference",
            "Lookup tables for locating material in the source.",
            [(f"{profile['scope_title']} PDF page map", "pdf-page-map.md",
              "one row per scoped PDF page, with a link to its canonical text.")],
        ),
    )
    audit.record(
        phase="wiki",
        action="generate_deterministic_source_guides",
        what=f"Generated {len(written)} Source Guide and reference notes from verified run facts.",
        why="Scope, provenance, audit path, citation apparatus, and the page map are machine-knowable; leaving them to the model invites unsupported prose in exactly the family that must be exact.",
        how="Derived every value from the staged page records, the pinned source hashes, and the extraction manifest, then wrote OKF notes marked as unverified drafts.",
        details={"notes": written, "apparatus_entries": len(entries)},
    )
    return written
