"""Report local extraction and profile prerequisites without changing state."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from hexwiki.engine.profile import load_profile
from hexwiki.engine.audit import AuditLog, exclusive_json
from hexwiki.engine.config import ConfigError, TranscriptRecorder, load_runtime_config
from hexwiki.engine.runtime import PreflightFailure, network_preflight
from hexwiki.engine.source import inspect_source


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--require-ocr", action="store_true")
    parser.add_argument("--require-poppler", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "missing"


def run(args: argparse.Namespace) -> int:
    tools = {
        name: shutil.which(name)
        for name in ("tesseract", "pdfinfo", "pdftotext", "pdfimages", "pdffonts")
    }
    poppler_names = ("pdfinfo", "pdftotext", "pdfimages", "pdffonts")
    failures: list[str] = []
    if args.require_ocr and not tools["tesseract"]:
        failures.append("Tesseract is required but was not found on PATH")
    missing_poppler = [name for name in poppler_names if not tools[name]]
    if args.require_poppler and missing_poppler:
        failures.append("required Poppler tools are missing: " + ", ".join(missing_poppler))

    profile_report = None
    if args.profile:
        profile = load_profile(args.profile)
        inspected = inspect_source(profile)
        profile_report = {
            "profile_id": profile.data["profile_id"],
            "profile_sha256": profile.profile_sha256,
            "pdf_exists": profile.pdf_path.is_file(),
            "extraction_exists": profile.extraction_root.is_dir(),
            "canonical_scope_sha256": inspected["lock"]["scope"]["canonical_sha256"],
            "status": "passed",
        }

    network: Any = "skipped by operator" if args.skip_network else None
    if not args.skip_network:
        if args.run_dir is None:
            failures.append("--run-dir is required for a networked preflight")
        else:
            run_root = args.run_dir.expanduser().resolve()
            if run_root.exists():
                failures.append(f"explicit preflight run directory already exists: {run_root}")
            else:
                try:
                    runtime = load_runtime_config(require_network=True)
                    run_root.parent.mkdir(parents=True, exist_ok=True)
                    run_root.mkdir(exist_ok=False)
                    run_id = "hexwiki-preflight-" + datetime.now().astimezone().strftime(
                        "%Y-%m-%d_%H-%M-%S"
                    )
                    audit = AuditLog(run_root / "actions.jsonl", run_id)
                    recorder = TranscriptRecorder(
                        run_root / "stage-transcripts",
                        run_id,
                        secrets=(runtime.api_key,),
                    )
                    network = network_preflight(
                        runtime=runtime,
                        run_root=run_root,
                        audit=audit,
                        recorder=recorder,
                    )
                    exclusive_json(run_root / "preflight-report.json", network)
                except (ConfigError, PreflightFailure, OSError, ValueError) as error:
                    failures.append(f"network preflight failed: {type(error).__name__}: {error}")

    report = {
        "status": "passed" if not failures else "failed",
        "python": sys.version.split()[0],
        "packages": {
            "PyMuPDF": _version("PyMuPDF"),
            "Pillow": _version("Pillow"),
        },
        "tools": {name: bool(path) for name, path in tools.items()},
        "profile": profile_report,
        "network": network,
        "failures": failures,
    }
    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"status: {report['status']}")
        print(f"Python: {report['python']}")
        for name, version in report["packages"].items():
            print(f"{name}: {version}")
        for name, available in report["tools"].items():
            print(f"{name}: {'available' if available else 'not found'}")
        if profile_report:
            print(f"profile: {profile_report['profile_id']} ({profile_report['status']})")
        print(f"network: {report['network']}")
        for failure in failures:
            print(f"failure: {failure}")
    return 0 if not failures else 3
