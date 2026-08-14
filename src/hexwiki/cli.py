"""Canonical ``hexwiki`` command-line interface."""

from __future__ import annotations

import argparse
import importlib
import sys
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

ACTIVE_COMMANDS = {
    "init": "hexwiki.commands.init",
    "extract": "hexwiki.commands.extract",
    "profile": "hexwiki.commands.profile",
    "preflight": "hexwiki.commands.preflight",
    "smoke": "hexwiki.commands.smoke",
    "build": "hexwiki.commands.build",
    "status": "hexwiki.commands.status",
    "lint": "hexwiki.commands.lint",
    "verify": "hexwiki.commands.verify",
    "query": "hexwiki.commands.query",
}


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="hexwiki",
        description="Compile a bounded PDF scope into an auditable OKF wiki.",
    )
    root.add_argument("--version", action="version", version=f"HexWiki {__version__}")
    subcommands = root.add_subparsers(dest="command", metavar="COMMAND")
    for name, description in COMMANDS.items():
        command = subcommands.add_parser(name, help=description, description=description)
        module_name = ACTIVE_COMMANDS.get(name)
        if module_name:
            module = importlib.import_module(module_name)
            module.configure(command)
            command.set_defaults(_handler=module.run)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    command_parser = parser()
    namespace = command_parser.parse_args(argv)
    if namespace.command is None:
        command_parser.print_help()
        return 0
    handler = getattr(namespace, "_handler", None)
    if handler is None:
        print(
            f"error: {namespace.command!r} is reserved for a later implementation phase",
            file=sys.stderr,
        )
        return 2
    try:
        return int(handler(namespace))
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
