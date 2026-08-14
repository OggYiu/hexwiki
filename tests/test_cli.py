"""Public CLI scaffold tests."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from hexwiki.cli import COMMANDS, main


class CliTests(unittest.TestCase):
    def test_root_help_succeeds(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main([])
        self.assertEqual(result, 0)
        self.assertIn("hexwiki", output.getvalue().lower())
        for command in COMMANDS:
            self.assertIn(command, output.getvalue())

    def test_each_reserved_command_has_help(self) -> None:
        for command in COMMANDS:
            with self.subTest(command=command):
                output = io.StringIO()
                with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                    main([command, "--help"])
                self.assertEqual(raised.exception.code, 0)
                self.assertIn(command, output.getvalue())

    def test_offline_preflight_passes_and_uses_the_documented_failure_code(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["preflight", "--skip-network", "--json"])
        self.assertEqual(result, 0)
        self.assertIn('"status": "passed"', output.getvalue())

        output = io.StringIO()
        with (
            patch("hexwiki.commands.preflight.shutil.which", return_value=None),
            contextlib.redirect_stdout(output),
        ):
            result = main(
                ["preflight", "--skip-network", "--require-poppler", "--json"]
            )
        self.assertEqual(result, 3)
        self.assertIn('"status": "failed"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
