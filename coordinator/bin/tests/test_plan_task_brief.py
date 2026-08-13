"""test_plan_task_brief.py — proving tests for `coordinator/bin/plan-task-brief.py`,
the single-task ad-hoc dispatch-brief lift over a plan's `## Tasks` spine.

Spec backlink: cross-repo/inbox/2026-08-13-example-doctrine-repo-em-pcli-02-plan-task-brief-copyout.md

Invokes the CLI as a subprocess (python plan-task-brief.py ...) rather than
importing it, exercising the real exit-code contract (0/1/2), stdout/stderr
split, and byte-faithfulness of --out FILE against stdout.

Run with: python3 -m pytest coordinator/bin/tests/test_plan_task_brief.py
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

import pytest

# Every test invokes plan-task-brief.py as a REAL subprocess (see module
# docstring) to exercise its actual exit-code contract (0/1/2) and
# stdout/stderr split -- an in-process call would not observe those
# subprocess-boundary behaviours. The spawn ratchet's `_BASELINE` is
# shrink-only pre-existing residue and is explicitly not the route for this
# file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_SCRIPT = _BIN_DIR / "plan-task-brief.py"

try:
    _REPO_ROOT = Path(__file__).resolve().parents[3]
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    from coordinator_core.win_portability import no_console_creationflags
except Exception:  # pragma: no cover - fallback if import path differs
    def no_console_creationflags() -> dict:
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_MULTILINE_BODY_PLAN = """\
---
title: fixture plan
---

## Tasks

```yaml plan-tasks
- id: C1
  title: "Fix the widget"
  surface: widgets/core.py
  change_kind: code-edit
  body: |
    Line one of the body.
    Line two, indented differently:
        still inside the block.

    A blank line inside the block survives too.
  queue_scope: project
  pm_approved: false
  deferred: false
  writes:
    - widgets/core.py
  reads:
    - widgets/util.py
- id: C1
  title: duplicate row
  surface: widgets/dup.py
  change_kind: code-edit
```
"""

_SIMPLE_PLAN = """\
---
title: fixture plan
---

## Tasks

```yaml plan-tasks
- id: C1
  title: "Fix the widget"
  surface: widgets/core.py
  change_kind: code-edit
  body: "one line body"
  queue_scope: project
  pm_approved: false
  deferred: false
  case_against: "not urgent"
  disposition: open
  disposition_ref: null
  disposition_detail: null
  writes:
    - widgets/core.py
  reads:
    - widgets/util.py
- id: C2
  title: "Second task"
  surface: widgets/second.py
  change_kind: doc-edit
```
"""

_ABSENT_SPINE_PLAN = """\
---
title: fixture plan, no spine
---

## Tasks

Nothing here yet.
"""

_MALFORMED_SPINE_PLAN = """\
---
title: fixture plan, malformed spine
---

## Tasks

```yaml plan-tasks
this is: not a list of rows: - broken
```
"""


def _run(plan_path: Path, task_id: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), str(plan_path), task_id, *extra],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )


class TestHappyPath(unittest.TestCase):
    def test_out_set_fields_do_not_leak(self):
        plan = Path(self._make_plan(_SIMPLE_PLAN))
        proc = _run(plan, "C1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        self.assertIn("Fix the widget", out)
        self.assertIn("widgets/core.py", out)
        self.assertIn("code-edit", out)
        self.assertIn("one line body", out)
        for banned in (
            "queue_scope",
            "pm_approved",
            "deferred",
            "case_against",
            "disposition",
            "writes",
            "reads",
            "project",
        ):
            self.assertNotIn(banned, out, f"out-set field {banned!r} leaked into brief")

    def test_out_flag_matches_stdout_and_plan_untouched(self):
        plan = Path(self._make_plan(_SIMPLE_PLAN))
        before = plan.read_text(encoding="utf-8")
        stdout_proc = _run(plan, "C1")
        self.assertEqual(stdout_proc.returncode, 0, stdout_proc.stderr)

        out_file = plan.parent / "brief.txt"
        out_proc = _run(plan, "C1", "--out", str(out_file))
        self.assertEqual(out_proc.returncode, 0, out_proc.stderr)
        self.assertEqual(out_file.read_text(encoding="utf-8"), stdout_proc.stdout)

        after = plan.read_text(encoding="utf-8")
        self.assertEqual(before, after, "plan file must never be written to")

    def _make_plan(self, content: str) -> str:
        tmp_dir = self._tmp_dir()
        plan_path = tmp_dir / "plan.md"
        plan_path.write_text(content, encoding="utf-8")
        return str(plan_path)

    def _tmp_dir(self) -> Path:
        import tempfile

        d = Path(tempfile.mkdtemp(prefix="plan-task-brief-test-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        return d


class TestMultilineBody(unittest.TestCase):
    def test_multiline_body_survives_intact_and_duplicate_id_fails_loud(self):
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="plan-task-brief-multiline-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp_dir, ignore_errors=True))
        plan = tmp_dir / "plan.md"
        plan.write_text(_MULTILINE_BODY_PLAN, encoding="utf-8")

        proc = _run(plan, "C1")
        # C1 is duplicated in this fixture on purpose — duplicate ids must
        # fail loud, never first-wins.
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("duplicate", proc.stderr.lower())


class TestMultilineBodySingleRow(unittest.TestCase):
    _PLAN = """\
---
title: fixture plan
---

## Tasks

```yaml plan-tasks
- id: C1
  title: "Fix the widget"
  surface: widgets/core.py
  change_kind: code-edit
  body: |
    Line one of the body.
    Line two, indented differently:
        still inside the block.

    A blank line inside the block survives too.
```
"""

    def test_multiline_body_block_scalar_survives_intact(self):
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix="plan-task-brief-multiline-single-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp_dir, ignore_errors=True))
        plan = tmp_dir / "plan.md"
        plan.write_text(self._PLAN, encoding="utf-8")

        proc = _run(plan, "C1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = proc.stdout
        self.assertIn("Line one of the body.", out)
        self.assertIn("Line two, indented differently:", out)
        self.assertIn("    still inside the block.", out)
        self.assertIn(
            "Line one of the body.\nLine two, indented differently:\n"
            "    still inside the block.\n\nA blank line inside the block survives too.",
            out,
        )


class TestFailLoudPaths(unittest.TestCase):
    def _write(self, tmp_prefix: str, content: str) -> Path:
        import tempfile

        tmp_dir = Path(tempfile.mkdtemp(prefix=tmp_prefix))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp_dir, ignore_errors=True))
        plan = tmp_dir / "plan.md"
        plan.write_text(content, encoding="utf-8")
        return plan

    def test_unknown_id_fails_loud(self):
        plan = self._write("plan-task-brief-unknown-", _SIMPLE_PLAN)
        proc = _run(plan, "NOPE")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("NOPE", proc.stderr)
        self.assertIn("not found", proc.stderr.lower())

    def test_absent_spine_fails_loud_with_absent_specific_message(self):
        plan = self._write("plan-task-brief-absent-", _ABSENT_SPINE_PLAN)
        proc = _run(plan, "C1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("ABSENT", proc.stderr)

    def test_malformed_spine_fails_loud_with_malformed_specific_message(self):
        plan = self._write("plan-task-brief-malformed-", _MALFORMED_SPINE_PLAN)
        proc = _run(plan, "C1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("MALFORMED", proc.stderr)

    def test_absent_and_malformed_messages_are_distinct(self):
        absent_plan = self._write("plan-task-brief-absent2-", _ABSENT_SPINE_PLAN)
        malformed_plan = self._write("plan-task-brief-malformed2-", _MALFORMED_SPINE_PLAN)
        absent_proc = _run(absent_plan, "C1")
        malformed_proc = _run(malformed_plan, "C1")
        self.assertNotEqual(absent_proc.stderr, malformed_proc.stderr)

    def test_duplicate_ids_fail_loud(self):
        plan = self._write("plan-task-brief-dup-", _MULTILINE_BODY_PLAN)
        proc = _run(plan, "C1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertIn("duplicate", proc.stderr.lower())


if __name__ == "__main__":
    unittest.main()
