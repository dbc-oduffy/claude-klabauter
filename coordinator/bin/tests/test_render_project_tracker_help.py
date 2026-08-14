"""test_render_project_tracker_help.py — `--help`/`-h` must exit 0 without
performing any real render (2026-08-13 fix).

Defect this closes: `render-project-tracker` had no `--help` handling at
all. `main()` ignored `sys.argv` entirely and unconditionally resolved the
store root and ran the real `render_project_tracker` op. The end-of-run
entrypoint gate (`coordinator_core.percolate.engine.run_entrypoint_gate` /
`_run_one_entrypoint`) launches every scanned entrypoint as
`[interpreter, script, "--help"]` and treats a non-zero exit as a genuine
failure. Without an early `--help` exit, a real render ran against this
repo's own tree and hit the frozen-tracker guard (own docs/project-
tracker.md is hand-curated, not queue-generated), exiting 3 — a real
app-level refusal, not a startup failure, but one `--help` should never
have reached in the first place.

Spec backlink: state/bug-backlog/2026-08-13-four-coordinator-bin-entrypoints-fail-th-1e16c15ed8ef.yaml

Run:
    python3 -m pytest coordinator/bin/tests/test_render_project_tracker_help.py -v
"""
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
        "render_project_tracker_help_test", str(_BIN_DIR / "render-project-tracker.py")
    )
    spec = importlib.util.spec_from_loader(
        "render_project_tracker_help_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


class TestHelpExitsCleanWithoutRendering(unittest.TestCase):
    def test_long_flag_exits_zero_without_resolving_store_root(self):
        with unittest.mock.patch("sys.argv", ["render-project-tracker", "--help"]):
            with unittest.mock.patch.object(
                _cli, "_resolve_store_root", side_effect=AssertionError("must not run on --help")
            ):
                with unittest.mock.patch("sys.stdout", io.StringIO()) as out:
                    with self.assertRaises(SystemExit) as ctx:
                        _cli.main()
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("render-project-tracker", out.getvalue())

    def test_short_flag_also_exits_zero(self):
        with unittest.mock.patch("sys.argv", ["render-project-tracker", "-h"]):
            with unittest.mock.patch.object(
                _cli, "_resolve_store_root", side_effect=AssertionError("must not run on -h")
            ):
                with unittest.mock.patch("sys.stdout", io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        _cli.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_no_help_flag_still_calls_resolve_store_root(self):
        # Unchanged, pre-existing behavior: a real (non---help) invocation
        # must still reach the real render path, not get silently
        # short-circuited by this fix.
        with unittest.mock.patch("sys.argv", ["render-project-tracker"]):
            with unittest.mock.patch.object(
                _cli, "_resolve_store_root", side_effect=RuntimeError("reached real path")
            ) as mock_resolve:
                with self.assertRaises(RuntimeError):
                    _cli.main()
        mock_resolve.assert_called_once()


if __name__ == "__main__":
    unittest.main()
