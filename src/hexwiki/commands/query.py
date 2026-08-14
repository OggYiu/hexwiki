"""Query a wiki with deterministic local full-text ranking."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hexwiki.tools.wiki import query_vault


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("wiki", type=Path)
    parser.add_argument("query")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args: argparse.Namespace) -> int:
    if args.limit < 1:
        raise ValueError("--limit must be at least 1")
    results = query_vault(args.wiki, args.query, args.limit)
    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for index, result in enumerate(results, start=1):
            print(f"{index}. [{result['score']}] {result['title']} — {result['path']}")
            print(f"   {result['snippet']}")
    return 0
