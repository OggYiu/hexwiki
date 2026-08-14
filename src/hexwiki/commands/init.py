"""Create a private, secret-free HexWiki runtime configuration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from hexwiki.engine.audit import exclusive_json
from hexwiki.engine.config import CONFIG_FILENAME, config_template, default_config_dir


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config-dir", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")


def run(args: argparse.Namespace) -> int:
    root = (args.config_dir or default_config_dir()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / CONFIG_FILENAME
    exclusive_json(path, config_template())
    result = {
        "status": "created",
        "config": str(path),
        "credential": "set HEXWIKI_API_KEY in the process environment; it is never stored",
    }
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(f"created: {path}")
        print(result["credential"])
    return 0
