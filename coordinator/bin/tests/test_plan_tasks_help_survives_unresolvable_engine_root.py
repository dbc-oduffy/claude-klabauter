"""test_plan_tasks_help_survives_unresolvable_engine_root.py — new coverage
for state/dispatch-briefs/2026-08-26-the-preswap-gate-learns-the-
destinations-layout/SPEC.md item 2.

Bug this pins: both `coordinator/bin/plan-tasks-stamp` and `coordinator/
bin/plan-tasks-resolve` used to call `cc_invoke.require_engine_on_path
(__file__)` at MODULE SCOPE — before argparse ever runs — so `--help` died
under any hermetic/no-engine-root environment (mktcache's gate; an OSS
user with no klabauter installed) instead of printing usage and exiting 0.
The fix defers that resolution to inside `main()`, after `parser.parse_
args()` — `--help` (and any other argparse exit path) now short-circuits
before the engine root is ever touched.

These tests prove the DEFERRAL itself, not merely that `--help` happens to
work in an environment where the engine root already resolves (both files'
own pre-existing --help tests already exercise the happy path). Each test
monkeypatches `cc_invoke.require_engine_on_path` to raise, then calls
`--help` and asserts SystemExit(0) — the strongest available proof that
main() never reaches that call on this path.

Every fail-loud site (main()'s own dispatch, `plan-tasks-resolve`'s
`_read_source_row`) is left untouched by this fix and is covered by each
module's own existing test suite; not re-tested here.

Run:
    python3 -m pytest coordinator/bin/tests/test_plan_tasks_help_survives_unresolvable_engine_root.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module(entrypoint_name: str, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(_BIN_DIR / entrypoint_name))
    spec = importlib.util.spec_from_loader(module_name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def _unresolvable_engine_root(*_a, **_k):
    raise RuntimeError(
        "cc_invoke: cannot resolve CLAUDE_KLABAUTER_ROOT — repos.claude_klabauter is not set."
    )


class TestPlanTasksResolveHelpDefersEngineRoot(unittest.TestCase):
    def test_help_exits_zero_with_engine_root_unresolvable(self):
        cli = _load_cli_module("plan-tasks-resolve", "plan_tasks_resolve_help_deferral_test_subject")
        with unittest.mock.patch.object(cli.cc_invoke, "require_engine_on_path", _unresolvable_engine_root):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["--help"])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn("--coded", buf.getvalue())


class TestPlanTasksStampHelpDefersEngineRoot(unittest.TestCase):
    def test_help_exits_zero_with_engine_root_unresolvable(self):
        cli = _load_cli_module("plan-tasks-stamp", "plan_tasks_stamp_help_deferral_test_subject")
        with unittest.mock.patch.object(cli.cc_invoke, "require_engine_on_path", _unresolvable_engine_root):
            buf = io.StringIO()
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    cli.main(["--help"])
            self.assertEqual(ctx.exception.code, 0)
            self.assertIn("--set", buf.getvalue())
