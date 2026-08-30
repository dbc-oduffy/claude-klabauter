"""
coordinator_core.ops.tests.test_plan_tasks_spine_drift_check — pytest for the
"plan.tasks.spine_drift_check" op.

Run (from repo root): python3 -m pytest coordinator_core/ops/tests/test_plan_tasks_spine_drift_check.py -q

Spec backlink: state/sizings/2026-08-21-a-spine-that-disagrees-with-the-tree-sho.yaml
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import coordinator_core.ops.plan_tasks_spine_drift_check as drift_mod
from coordinator_core.win_portability import no_console_creationflags

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Spawns a real external process (one batched `git log` per case); run at
# cadence gates, not per-commit. Spawn ratchet: coordinator_core/tests/
# test_no_new_spawning_tests.py.
pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_handler = drift_mod._handler

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

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


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")


def _seed_plan(repo: Path, text: str = _PLAN_TEXT) -> None:
    (repo / "plan.md").write_text(text, encoding="utf-8")
    _git(repo, "add", "plan.md")
    _git(repo, "commit", "-m", "seed plan")


def _commit_chunk(repo: Path, chunk_id: str, deliverable_id: str) -> None:
    marker = repo / f"{chunk_id.lower()}.txt"
    marker.write_text(f"{chunk_id} landed\n", encoding="utf-8")
    _git(repo, "add", marker.name)
    _git(
        repo,
        "commit",
        "-m",
        f"{chunk_id}: land chunk",
        "-m",
        f"Deliverable-Id: {deliverable_id}",
    )


class TestSpineDriftCheck:
    def test_clean_spine_reports_unknown_not_clean(self, tmp_path: Path) -> None:
        """No commits landed for either chunk -- both stay open, and since
        the join found zero comparable evidence, this is the third state
        (`unknown`), never a false 'verified clean' claim. This is the
        overwhelmingly common case on a real branch (measured: ~6% of
        commits carry a chunk-id subject at all) and is the most
        important assertion in this file, per the team-lead's
        measurement-driven widening."""
        repo = tmp_path
        _init_repo(repo)
        _seed_plan(repo)

        result = _handler({"plan_path": "plan.md"}, repo_root=repo)

        assert result["exit_code"] == 0, result
        assert result["open_row_count"] == 2
        assert result["drifted_rows"] == []
        assert result["evidence_available"] is False
        assert result["drift_status"] == "unknown"
        assert result["join_provenance"] == "no_join_candidates"

    def test_drifted_spine_names_the_open_row_and_covering_sha(self, tmp_path: Path) -> None:
        """C1 lands with a matching Deliverable-Id trailer while the spine
        row still reads (default) open -- this must surface as drift,
        named with its covering sha, and never write the plan file."""
        repo = tmp_path
        _init_repo(repo)
        _seed_plan(repo)
        _commit_chunk(repo, "C1", _DELIVERABLE_ID)

        before = (repo / "plan.md").read_text(encoding="utf-8")
        result = _handler({"plan_path": "plan.md"}, repo_root=repo)
        after = (repo / "plan.md").read_text(encoding="utf-8")

        assert result["exit_code"] == 0, result
        assert result["evidence_available"] is True
        assert result["open_row_count"] == 2
        assert result["drifted_row_count"] == 1
        assert result["drifted_rows"][0]["chunk_id"] == "C1"
        assert result["drifted_rows"][0]["covering_sha"]
        assert result["drift_status"] == "drift_detected"
        assert after == before, "report-only op must never write the plan file"

    def test_no_evidence_row_is_reported_as_unknown_not_drift_or_clean(
        self, tmp_path: Path
    ) -> None:
        """A commit lands for C1 but under a DIFFERENT Deliverable-Id (the
        two-producer desync close_out_and_stamp.py already documents) --
        the join has no evidence for THIS plan at all, so C1 must be
        reported `unknown`: NOT drift (it's unattributable, not genuinely
        shipped-and-open), and NOT folded into a clean/no-drift verdict
        either -- this is the third state a caller must be able to see.
        This is the most important test in the file (team-lead
        measurement, 2026-08-21): the no-evidence case is the common case,
        not the edge case, on a real branch."""
        repo = tmp_path
        _init_repo(repo)
        _seed_plan(repo)
        _commit_chunk(repo, "C1", "dlv-some-other-plan-000002")

        result = _handler({"plan_path": "plan.md"}, repo_root=repo)

        assert result["exit_code"] == 0, result
        assert result["drifted_rows"] == []
        assert result["evidence_available"] is False
        assert result["join_provenance"] == "key_mismatch"
        assert result["drift_status"] == "unknown"
        assert result["drift_status"] not in ("drift_detected", "verified_no_drift")

    def test_evidence_backed_open_rows_report_verified_no_drift(self, tmp_path: Path) -> None:
        """A THIRD spine row, C3, already `disposition: coded` (resolved
        elsewhere, not open), lands with a matching Deliverable-Id trailer
        and its own chunk-id subject -- real evidence for THIS plan
        (join_provenance == "joined"). Neither open row (C1, C2) is
        covered by C3's commit (`_committed_id_covers_spine_id("C3", "C1")`
        is False -- unrelated ids), so this is the third state's OTHER
        half: real evidence existed and genuinely found no drift among the
        open rows -- distinct from `unknown`, which never got to look."""
        repo = tmp_path
        _init_repo(repo)
        plan_text = _PLAN_TEXT.replace(
            "    Ship the gadget end to end.\n```",
            "    Ship the gadget end to end.\n"
            "- id: C3\n"
            "  title: \"Ship the gizmo\"\n"
            "  change_kind: script-edit\n"
            "  surface: gizmo.py\n"
            "  deferred: false\n"
            "  disposition: coded\n"
            "  disposition_ref: deadbeef\n"
            "  disposition_detail: \"landed earlier\"\n"
            "  body: |\n"
            "    Already resolved elsewhere.\n"
            "```",
        )
        assert "id: C3" in plan_text
        _seed_plan(repo, plan_text)
        _commit_chunk(repo, "C3", _DELIVERABLE_ID)

        result = _handler({"plan_path": "plan.md"}, repo_root=repo)

        assert result["exit_code"] == 0, result
        assert result["evidence_available"] is True
        assert result["join_provenance"] == "joined"
        assert result["open_row_count"] == 2
        assert result["drifted_rows"] == []
        assert result["drift_status"] == "verified_no_drift"

    def test_never_writes_the_plan_file(self, tmp_path: Path) -> None:
        """Report-only across both branches: a drifted AND a clean run each
        leave the plan file byte-identical to what was committed."""
        repo = tmp_path
        _init_repo(repo)
        _seed_plan(repo)
        _commit_chunk(repo, "C1", _DELIVERABLE_ID)
        before = (repo / "plan.md").read_text(encoding="utf-8")

        _handler({"plan_path": "plan.md"}, repo_root=repo)
        _handler({"plan_path": "plan.md"}, repo_root=repo)

        after = (repo / "plan.md").read_text(encoding="utf-8")
        assert after == before

    def test_missing_plan_file_reports_error_drift_status(self, tmp_path: Path) -> None:
        """Every error/`exit_code: 1` return must carry an explicit
        `drift_status: "error"` (team-lead dogfooding, 2026-08-21: 254 real
        plans, 3 came back with `drift_status` absent -- read as `None` by
        any `dict.get`-style caller, an unhandled fifth shape outside the
        four documented states). A plan path that doesn't exist is the
        cheapest way to force this op down an error return."""
        repo = tmp_path
        _init_repo(repo)
        _seed_plan(repo)

        result = _handler({"plan_path": "does-not-exist.md"}, repo_root=repo)

        assert result["exit_code"] == 1, result
        assert "error" in result
        assert result["drift_status"] == "error"
        assert result.get("drift_status") is not None


class TestImportOrderRegression:
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
