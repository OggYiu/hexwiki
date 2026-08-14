"""Run a production-shaped, nonpublishing model smoke."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from hexwiki.engine.config import load_runtime_config
from hexwiki.engine.runtime import exit_code, run_smoke


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--profile-lock", type=Path)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")


def _lock_path(profile: Path, configured: Path | None) -> Path:
    return configured or profile.with_name(profile.stem + ".lock.json")


def run(args: argparse.Namespace) -> int:
    try:
        runtime = load_runtime_config(require_network=True)
        warning = {
            "operation": "smoke",
            "publishes_wiki": False,
            "maximum_minutes": round(runtime.limits.smoke_max_seconds / 60, 1),
            "cost_notice": "This command makes paid provider calls; cost and duration vary.",
        }
        if args.as_json:
            print(json.dumps({"notice": warning}))
        else:
            print(warning["cost_notice"], file=sys.stderr)
            print(
                f"The smoke may run for up to {warning['maximum_minutes']} minutes and "
                "will not publish a wiki.",
                file=sys.stderr,
            )
        report = run_smoke(
            profile_path=args.profile,
            lock_path=_lock_path(args.profile, args.profile_lock),
            run_dir=args.run_dir,
            runtime=runtime,
        )
    except Exception as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return exit_code(error)
    result = {"status": "passed", "smoke_report": str(report)}
    print(json.dumps(result, indent=2) if args.as_json else f"smoke report: {report}")
    return 0
