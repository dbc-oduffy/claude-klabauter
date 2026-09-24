"""
coordinator_core.ops.tests.test_plan_tasks_spine_drift_check — pytest for the
"plan.tasks.spine_drift_check" op.

Evidence join deleted (P153-C22, docs/plans/2026-09-22-spawn-budget-and-
census.md, R1, DR-344 kill bar): `_handler` no longer has any mechanical
evidence source, so a commit-required `open` row is now ALWAYS reported
inside `"unknown"`, never `"drift_detected"`/`"verified_no_drift"` -- this
suite pins that permanent outcome and asserts the deleted `git log`
evidence leg is gone (no `git`/subprocess call from this module at all,
verified by patching `subprocess.run` to fail loudly if invoked).

Run (from repo root): python3 -m pytest coordinator_core/ops/tests/test_plan_tasks_spine_drift_check.py -q

Spec backlink: state/sizings/2026-08-21-a-spine-that-disagrees-with-the-tree-sho.yaml
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import coordinator_core.ops.plan_tasks_spine_drift_check as drift_mod
from coordinator_core.win_portability import no_console_creationflags

_REPO_ROOT = Path(__file__).resolve().parents[3]

_handler = drift_mod._handler

_DELIVERABLE_ID = "dlv-fixture-spine-drift-000001"

_PLAN_TEXT = """---
title: "Fixture plan -- spine drift check"
status: draft
plan_id: "pln-fixture-spine-drift-000001"
deliverable_id: "{deliverable_id}"
---

# Fixture plan -- spine drift check

## Tasks

```yaml plan-tasks
- id: C1
  title: "Ship the widget"
  change_kind: script-edit
  surface: widget.py
  deferred: false
  body: |
    Ship the widget end to end.
- id: C2
  title: "Ship the gadget"
  change_kind: script-edit
  surface: gadget.py
  deferred: false
  body: |
    Ship the gadget end to end.
```
""".format(deliverable_id=_DELIVERABLE_ID)


def _seed_plan(root: Path, text: str = _PLAN_TEXT) -> Path:
    # `main_worktree_root` requires a `.git` entry beneath `root` -- a bare
    # empty directory suffices (it never shells out to git; see module
    # docstring, EVIDENCE JOIN DELETED).
    git_dir = root / ".git"
    if not git_dir.exists():
        git_dir.mkdir()
    path = root / "plan.md"
    path.write_text(text, encoding="utf-8")
    return path


class TestSpineDriftCheckEvidenceJoinDeleted:
    """No fixture in this class touches a real git repo -- the whole point
    of the deletion is that `_handler` no longer spawns `git` at all.
    `repo_root` is a plain scratch directory, never an initialized repo,
    and a monkeypatched `subprocess.run` fails the test loudly if this
    module ever calls it."""

    @pytest.fixture(autouse=True)
    def _fail_on_subprocess(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise AssertionError(
                "plan.tasks.spine_drift_check spawned a subprocess -- the "
                "evidence join was deleted (P153-C22) and must never shell "
                "out to git for commit evidence again"
            )

        monkeypatch.setattr(subprocess, "run", _boom)

    def test_open_rows_always_report_unknown_never_drift_or_clean(self, tmp_path: Path) -> None:
        """Positive pin for the delete arm: with open commit-required
        rows present, the result is always `"unknown"` with
        `evidence_available: False` and a reason naming the deleted join
        -- regardless of what the (nonexistent) tree contains, since
        there is no longer any evidence source to consult."""
        _seed_plan(tmp_path)

        result = _handler({"plan_path": "plan.md"}, repo_root=tmp_path)

        assert result["exit_code"] == 0, result
        assert result["open_row_count"] == 2
        assert result["drifted_rows"] == []
        assert result["drifted_row_count"] == 0
        assert result["evidence_available"] is False
        assert result["join_provenance"] is None
        assert result["drift_status"] == "unknown"
        assert result["drift_status"] not in ("drift_detected", "verified_no_drift")
        assert "commit-log join was deleted" in result["evidence_reason"]

    def test_no_open_rows_reports_no_open_rows(self, tmp_path: Path) -> None:
        plan_text = _PLAN_TEXT.replace(
            "  deferred: false\n  body: |\n    Ship the widget end to end.\n"
            "- id: C2\n  title: \"Ship the gadget\"\n  change_kind: script-edit\n"
            "  surface: gadget.py\n  deferred: false\n  body: |\n"
            "    Ship the gadget end to end.\n",
            "  deferred: false\n"
            "  disposition: coded\n"
            "  disposition_ref: deadbeef\n"
            "  disposition_detail: \"landed earlier\"\n"
            "  body: |\n    Ship the widget end to end.\n",
        )
        _seed_plan(tmp_path, plan_text)

        result = _handler({"plan_path": "plan.md"}, repo_root=tmp_path)

        assert result["exit_code"] == 0, result
        assert result["open_row_count"] == 0
        assert result["drifted_rows"] == []
        assert result["evidence_available"] is None
        assert result["drift_status"] == "no_open_rows"

    def test_never_writes_the_plan_file(self, tmp_path: Path) -> None:
        _seed_plan(tmp_path)
        before = (tmp_path / "plan.md").read_text(encoding="utf-8")

        _handler({"plan_path": "plan.md"}, repo_root=tmp_path)
        _handler({"plan_path": "plan.md"}, repo_root=tmp_path)

        after = (tmp_path / "plan.md").read_text(encoding="utf-8")
        assert after == before

    def test_missing_plan_file_reports_error_drift_status(self, tmp_path: Path) -> None:
        """Every error/`exit_code: 1` return must carry an explicit
        `drift_status: "error"` (team-lead dogfooding, 2026-08-21: 254 real
        plans, 3 came back with `drift_status` absent -- read as `None` by
        any `dict.get`-style caller, an unhandled fifth shape outside the
        documented states). A plan path that doesn't exist is the
        cheapest way to force this op down an error return."""
        _seed_plan(tmp_path)

        result = _handler({"plan_path": "does-not-exist.md"}, repo_root=tmp_path)

        assert result["exit_code"] == 1, result
        assert "error" in result
        assert result["drift_status"] == "error"
        assert result.get("drift_status") is not None


class TestImportOrderRegression:
    @pytest.mark.spawns_process
    @pytest.mark.cadence
    def test_registers_when_close_out_and_stamp_imports_first(self) -> None:
        """Import-cycle regression (team-lead review, 2026-08-21):
        `close_out_and_stamp.py` imports from `coordinator_core.ops.*` in
        several places, so this op's ORIGINAL module-level
        `from coordinator_core.execute_plan_assemble.close_out_and_stamp
        import (...)` created a real cycle whenever `close_out_and_stamp`
        happened to be imported FIRST: close_out_and_stamp ->
        coordinator_core.ops -> the package's own eager-import loop ->
        this module -> back into a partially-initialized
        close_out_and_stamp (`ImportError: cannot import name ... from
        partially initialized module`). `coordinator_core/ops/__init__.py`'s
        eager-import loop swallows that ImportError, so the failure mode is
        NOT a loud crash -- this op simply never registers: present in the
        source tree, absent from `plan.tasks.spine_drift_check`'s own
        registry entry. Fixed by deferring the import to call time
        (`plan_tasks_spine_drift_check._coas()`).

        Runs in a fresh subprocess, deliberately importing
        `close_out_and_stamp` BEFORE `coordinator_core.ops` -- the exact
        order that broke registration before the fix, and the one order a
        same-process test can't reliably reproduce once anything else in
        the test session has already imported these modules in the other
        order and populated `sys.modules`."""
        script = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(_REPO_ROOT)!r})
            import coordinator_core.execute_plan_assemble.close_out_and_stamp  # deliberately first
            import coordinator_core.ops as ops
            ops._eager_import_all()
            import coordinator_core.ipc as ipc
            assert "plan.tasks.spine_drift_check" in ipc._REGISTRY, (
                "plan.tasks.spine_drift_check failed to register when "
                "close_out_and_stamp imports first"
            )
            print("OK")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            **no_console_creationflags(),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout
