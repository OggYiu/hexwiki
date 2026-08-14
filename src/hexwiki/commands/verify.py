"""Verify a sealed wiki's integrity and substantive quotations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hexwiki.engine.finalize import verify_checksums
from hexwiki.tools.quotes import verify


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("wiki", type=Path)
    parser.add_argument("--min-length", type=int, default=40)
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args: argparse.Namespace) -> int:
    wiki = args.wiki.resolve()
    if not wiki.is_dir():
        raise ValueError(f"wiki is not a directory: {wiki}")
    checksums = verify_checksums(wiki)
    manifest_path = wiki / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("manifest.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "sealed":
        raise ValueError(f"manifest status is not sealed: {manifest.get('status')!r}")
    quotations = verify(wiki, args.min_length)
    quote_issues = (
        len(quotations["in_scope_but_not_on_a_cited_page"])
        + len(quotations["not_found_in_scope"])
    )
    report = {
        "status": "passed" if quote_issues == 0 else "failed",
        "wiki": str(wiki),
        "checksummed_files": len(checksums),
        "quotations": quotations,
        "limitation": (
            "Quotation support is a lower bound; this does not measure selection, "
            "weighting, completeness, or semantic quality."
        ),
    }
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"status: {report['status']}")
        print(f"checksummed files: {len(checksums)}")
        print(f"semantic notes: {quotations['semantic_notes']}")
        print(f"quotations checked: {quotations['quotations_checked']}")
        print(f"wrong-page quotations: {len(quotations['in_scope_but_not_on_a_cited_page'])}")
        print(f"unsupported quotations: {len(quotations['not_found_in_scope'])}")
        print(report["limitation"])
    return 0 if report["status"] == "passed" else 1
