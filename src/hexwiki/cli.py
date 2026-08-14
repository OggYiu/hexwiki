"""Canonical ``hexwiki`` command-line interface."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__


COMMANDS = {
    "init": "create private user configuration",
    "extract": "extract an auditable PDF evidence layer",
    "profile": "create, check, or lock a document profile",
    "preflight": "verify local and optional network prerequisites",
    "smoke": "run a production-shaped non-publishing smoke",
    "build": "compile and atomically publish a new wiki",
    "status": "inspect a run directory without changing it",
    "lint": "lint a wiki without changing it",
    "verify": "verify a wiki manifest and source quotations",
    "query": "query a wiki without changing it",
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="hexwiki",
        description="Compile a bounded PDF scope into an auditable OKF wiki.",
    )
    root.add_argument("--version", action="version", version=f"HexWiki {__version__}")
    subcommands = root.add_subparsers(dest="command", metavar="COMMAND")
    for name, description in COMMANDS.items():
        subcommands.add_parser(name, help=description, description=description)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    command_parser = parser()
    namespace = command_parser.parse_args(argv)
    if namespace.command is None:
        command_parser.print_help()
        return 0
    command_parser.error(
        f"{namespace.command!r} is reserved but not available in this scaffold"
    )
    return 2

