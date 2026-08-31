"""test_coordinator_doc_new_goals_scope_type_scoping.py -- CLI type-scoping
coverage for `--goals` and `--scope` (2026-08-31).

Purpose: cross-repo/inbox/2026-08-18-example-retrieval-repo-em-doc-new-silently-drops-
type-inapplicable-flags.md reported that `--type`-inapplicable flags parse,
exit 0, and are silently never read or emitted. The 2026-08-19 follow-up
established refuse-not-warn as the posture and closed it for
--additional-predecessor/--summary/--gated-open/--gate-note/--gated-predicate/
--deliverable-ids/--plan-ids/--carried-items, but left --goals (goal-seed/
roadmap-seed-only) and --scope (review-findings-only) unguarded: both flags
were still parsed by argparse, accepted for any --type, and silently dropped
-- reproducing the memo's exact original report for these two flags. This
suite pins the same refuse-fail-loud posture for both.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint -- same load
idiom as test_coordinator_doc_new_summary_gated_open.py. Exercises the
type-scoping guard by calling `_cli.main()` in-process with `sys.argv`
patched (same spawn-ratchet STUB disposition as the sibling suite: no
subprocess needed for a property `main()` alone determines).

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_goals_scope_type_scoping.py -v
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import unittest
import unittest.mock
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_goals_scope_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_goals_scope_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _run(*extra_args: str) -> tuple[int, str]:
    """Run `_cli.main()` in-process with `sys.argv` patched, standing in for
    `proc.returncode`/`proc.stderr` -- see module docstring's STUB disposition
    note."""
    argv = ["coordinator-doc-new", *extra_args]
    stderr_buf = io.StringIO()
    with unittest.mock.patch("sys.argv", argv):
        with contextlib.redirect_stderr(stderr_buf):
            try:
                raw = _cli.main()
            except SystemExit as exc:
                raw = exc.code
            code = raw if isinstance(raw, int) else (1 if raw else 0)
            return code, stderr_buf.getvalue()


class GoalsTypeScopingTest(unittest.TestCase):
    def test_goals_rejected_for_non_goal_or_roadmap_seed_type(self):
        code, stderr = _run("--type", "goal", "--title", "t", "--goals", "a,b")
        self.assertNotEqual(code, 0)
        self.assertIn("--goals", stderr)
        self.assertIn("--type goal", stderr)

    def test_goals_accepted_for_goal_seed_type(self):
        # goal-seed with --goals must NOT hit the type-scoping refusal path
        # (it may still fail later for unrelated reasons, e.g. missing
        # --out/repo context -- this only asserts the --goals guard doesn't fire).
        code, stderr = _run(
            "--type", "goal-seed", "--title", "t", "--goals", "a,b", "--out", "-"
        )
        self.assertNotIn("--goals is not accepted", stderr)


class ScopeTypeScopingTest(unittest.TestCase):
    def test_scope_rejected_for_non_review_findings_type(self):
        code, stderr = _run("--type", "goal", "--title", "t", "--scope", "a.py,b.py")
        self.assertNotEqual(code, 0)
        self.assertIn("--scope", stderr)
        self.assertIn("--type goal", stderr)

    def test_scope_accepted_for_review_findings_type(self):
        code, stderr = _run(
            "--type", "review-findings", "--title", "t", "--scope", "a.py,b.py", "--out", "-"
        )
        self.assertNotIn("--scope is not accepted", stderr)


if __name__ == "__main__":
    unittest.main()
