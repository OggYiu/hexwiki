"""Inspect one run directory without changing it."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hexwiki.engine.runtime import inspect_run


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args: argparse.Namespace) -> int:
    report = inspect_run(args.run_directory)
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        terminal = report.get("terminal", {})
        progress = report.get("progress", {})
        print(f"state: {terminal.get('state', progress.get('status', 'unknown'))}")
        print(f"review index: {report['review_index']['status']}")
        if report.get("failure"):
            print(f"failure: {report['failure'].get('error')}")
        if report.get("run_result"):
            print(f"wiki: {report['run_result'].get('wiki')}")
    return 0 if report["review_index"]["status"] != "failed" else 5
