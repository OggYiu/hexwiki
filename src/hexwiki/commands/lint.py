"""Run deterministic HexWiki and OKF lint checks."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hexwiki.engine import lint as engine_lint
from hexwiki.engine import okf


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("wiki", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args: argparse.Namespace) -> int:
    wiki = args.wiki.resolve()
    if not wiki.is_dir():
        raise ValueError(f"wiki is not a directory: {wiki}")
    lint_errors = engine_lint.lint(wiki)
    okf_report = okf.check_directory(wiki)
    okf_errors = okf.n_errors_of(okf_report["issues"])
    report = {
        "status": "passed" if not lint_errors and okf_errors == 0 else "failed",
        "wiki": str(wiki),
        "lint_errors": lint_errors,
        "okf": okf_report,
    }
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"status: {report['status']}")
        print(f"lint errors: {len(lint_errors)}")
        print(f"OKF errors: {okf_errors}")
        for error in lint_errors:
            print(f"{error['kind']:18s} {error['file']}: {error['detail']}")
        for issue in okf_report["issues"]:
            print(f"{issue['kind']:18s} {issue['relpath']}: {issue['detail']}")
    return 0 if report["status"] == "passed" else 1
