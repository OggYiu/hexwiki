"""Compile, validate, and atomically publish one new wiki directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hexwiki.engine.config import load_runtime_config
from hexwiki.engine.runtime import exit_code, run_build


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--profile-lock", type=Path)
    parser.add_argument("--smoke-report", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")


def _lock_path(profile: Path, configured: Path | None) -> Path:
    return configured or profile.with_name(profile.stem + ".lock.json")


def run(args: argparse.Namespace) -> int:
    try:
        runtime = load_runtime_config(require_network=True)
        warning = {
            "operation": "build",
            "maximum_minutes": round(runtime.limits.build_max_seconds / 60, 1),
            "cost_notice": "This command makes paid provider calls; cost and duration vary.",
            "output": str(args.output),
        }
        if args.as_json:
            print(json.dumps({"notice": warning}))
        else:
            print(warning["cost_notice"], file=sys.stderr)
            print(
                f"The build may run for up to {warning['maximum_minutes']} minutes. "
                "The output must not already exist.",
                file=sys.stderr,
            )
        output = run_build(
            profile_path=args.profile,
            lock_path=_lock_path(args.profile, args.profile_lock),
            smoke_report_path=args.smoke_report,
            run_dir=args.run_dir,
            output=args.output,
            runtime=runtime,
        )
    except Exception as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return exit_code(error)
    result = {"status": "passed", "wiki": str(output)}
    print(json.dumps(result, indent=2) if args.as_json else f"wiki: {output}")
    return 0
