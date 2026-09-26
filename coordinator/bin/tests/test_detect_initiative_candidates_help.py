from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import unittest
import unittest.mock
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "detect_initiative_candidates_help_test", str(_BIN_DIR / "detect-initiative-candidates.py")
    )
    spec = importlib.util.spec_from_loader(
        "detect_initiative_candidates_help_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class TestHelpExitsCleanWithoutTouchingStdin(unittest.TestCase):
    def test_long_flag_exits_zero_without_reading_stdin(self):
        fake_stdin = unittest.mock.Mock()
        fake_stdin.isatty.return_value = False
        fake_stdin.read.side_effect = AssertionError("must not read stdin on --help")
        with unittest.mock.patch("sys.argv", ["detect-initiative-candidates", "--help"]):
            with unittest.mock.patch("sys.stdin", fake_stdin):
                with unittest.mock.patch("sys.stdout", io.StringIO()) as out:
                    rc = _cli.main()
        self.assertEqual(rc, 0)
        self.assertIn("detect-initiative-candidates", out.getvalue())
        fake_stdin.read.assert_not_called()

    def test_short_flag_also_exits_zero(self):
        fake_stdin = unittest.mock.Mock()
        fake_stdin.isatty.return_value = False
        fake_stdin.read.side_effect = AssertionError("must not read stdin on -h")
        with unittest.mock.patch("sys.argv", ["detect-initiative-candidates", "-h"]):
            with unittest.mock.patch("sys.stdin", fake_stdin):
                with unittest.mock.patch("sys.stdout", io.StringIO()):
                    rc = _cli.main()
        self.assertEqual(rc, 0)

    def test_empty_devnull_stdin_without_help_still_fails_loudly(self):
        fake_stdin = unittest.mock.Mock()
        fake_stdin.isatty.return_value = False
        fake_stdin.read.return_value = ""
        with unittest.mock.patch("sys.argv", ["detect-initiative-candidates"]):
            with unittest.mock.patch("sys.stdin", fake_stdin):
                with unittest.mock.patch("sys.stderr", io.StringIO()) as err:
                    rc = _cli.main()
        self.assertEqual(rc, 1)
        self.assertIn("failed to parse JSON from stdin", err.getvalue())


if __name__ == "__main__":
    unittest.main()
