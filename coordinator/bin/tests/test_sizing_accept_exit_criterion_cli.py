from __future__ import annotations

import importlib
import io
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_TESTS_DIR)
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)

cli = importlib.import_module("sizing-accept-exit-criterion")


class TestHelp(unittest.TestCase):
    def test_help_names_all_five_params_and_exit_codes(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        text = out.getvalue()
        for flag in ("--sizing", "--pm-quote", "--statement", "--mode", "--supersede"):
            self.assertIn(flag, text)
        self.assertIn("Exit 1", text)
        self.assertIn("applied=false", text)

    def test_missing_required_flag_exits_2(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.main(["--sizing", "x"])
        self.assertEqual(ctx.exception.code, 2)

    def test_bad_mode_exits_2(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.main(["--sizing", "x", "--pm-quote", "q", "--mode", "bogus"])
        self.assertEqual(ctx.exception.code, 2)


class TestParams(unittest.TestCase):
    def test_optional_params_omitted_when_absent(self):
        self.assertEqual(
            cli.build_params(["--sizing", "s", "--pm-quote", "q"]),
            {"sizing": "s", "pm_quote": "q"},
        )

    def test_all_params_forwarded(self):
        self.assertEqual(
            cli.build_params(
                ["--sizing", "s", "--pm-quote", "q", "--statement", "st",
                 "--mode", "pm", "--supersede"]
            ),
            {"sizing": "s", "pm_quote": "q", "statement": "st", "mode": "pm",
             "supersede": True},
        )


class TestApmRuling(unittest.TestCase):
    def test_apm_ruling_forwarded_without_pm_quote(self):
        self.assertEqual(
            cli.build_params(["--sizing", "s", "--apm-ruling", "r"]),
            {"sizing": "s", "apm_ruling": "r"},
        )

    def test_ruling_ref_is_forwarded(self):
        self.assertEqual(
            cli.build_params(["--sizing", "s", "--apm-ruling", "r", "--ruling-ref", "state/apm/x.md"]),
            {"sizing": "s", "apm_ruling": "r", "ruling_ref": "state/apm/x.md"},
        )

    def test_both_flags_is_a_usage_error(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.main(["--sizing", "s", "--pm-quote", "q", "--apm-ruling", "r"])
        self.assertEqual(ctx.exception.code, 2)

    def test_neither_flag_is_a_usage_error(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            cli.main(["--sizing", "s"])
        self.assertEqual(ctx.exception.code, 2)

    def test_help_names_apm_ruling(self):
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit):
            cli.main(["--help"])
        self.assertIn("--apm-ruling", out.getvalue())


class TestRouting(unittest.TestCase):
    def test_routes_the_op_and_returns_its_exit_code(self):
        import op_trampoline

        with mock.patch.object(op_trampoline, "run", return_value=1) as run:
            self.assertEqual(cli.main(["--sizing", "s", "--pm-quote", "q"]), 1)
        self.assertEqual(run.call_args.args[0], "sizing.accept_exit_criterion")


if __name__ == "__main__":
    unittest.main()
