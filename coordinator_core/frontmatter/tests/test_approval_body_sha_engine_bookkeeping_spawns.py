"""`approved_body_sha` ignores the spine bookkeeping `dispatch.land_run` writes
(open -> coded, disposition_ref, D5 re-sort) and still refuses a real plan edit."""

from __future__ import annotations

import pytest

from coordinator_core.frontmatter.primitives import APPROVED_BODY_CHANGED, APPROVED_BODY_OK, check_approved_body, stamp_approved_body_sha
from pathlib import Path

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ROWS = """- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
- id: C4
  title: t4
  change_kind: code-edit
  surface: a.py
  deferred: false
  body: |
    disposition: coded
    multi-line
- id: C5
  title: t5
  change_kind: code-edit
  surface: a.py
  disposition: open
  deferred: false
"""


def _plan(rows: str = _ROWS, prose: str = "Prose.") -> str:
    return (
        "---\ntitle: p\nstatus: approved\n---\n\n# Plan\n\n"
        f"## Tasks\n\n```yaml plan-tasks\n{rows.rstrip()}\n```\n\n{prose}\n"
    )


def _approved(**kw) -> str:
    return stamp_approved_body_sha(_plan(**kw))


def _git(cwd, *args: str) -> None:
    """The one git spawn shared by every test here (windowless on Windows)."""
    import subprocess

    from coordinator_core.win_portability import no_console_creationflags

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, **no_console_creationflags())


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_land_run_then_only_incomplete_emit_is_not_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real dispatch.land_run on an incomplete approved plan, then the emit gate."""
    from coordinator_core.ops.dispatch_emit import land_run
    from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
    from coordinator_core.ops.dispatch_emit.op import _refuse_unapproved_body

    def git(*a) -> None:
        _git(tmp_path, *a)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "docs").mkdir()
    plan = tmp_path / "docs" / "plan.md"
    plan.write_text(_approved(), encoding="utf-8")
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-q", "-m", "seed")
    (tmp_path / "a.py").write_text("a2\n", encoding="utf-8")
    request = CommitRequest(
        chunks=(
            ChunkCommit(id="C3", title="t3", paths=("a.py",)),
            ChunkCommit(id="C5", title="t5", paths=("c.py",)),
        ),
        plan_path="docs/plan.md",
    )
    (tmp_path / "run.mjs").write_text("// s\n" + render_marker(request) + "\n", encoding="utf-8")

    monkeypatch.setattr(land_run, "_launch_close_job", lambda *a, **k: True)
    out = land_run._handler(
        {
            "script_path": "run.mjs",
            "incomplete_chunks": ["C5"],
            "inline_review": {"integration_stem": "s", "slices": 1, "fixes": 0},
        },
        repo_root=tmp_path / ".git",
    )

    assert not out.get("error") and not out.get("refused"), out
    from coordinator_core.ops.ceremony import close_background
    from coordinator_core.ops.dispatch_emit.run_close import close_job_path

    close_background._handler({"run_id": out["close"]["run_id"]}, repo_root=tmp_path / ".git")
    assert (tmp_path / close_job_path(out["close"]["run_id"])).is_file()
    after = plan.read_text(encoding="utf-8")
    assert "disposition_ref" in after
    _refuse_unapproved_body(str(plan))  # raises ValueError on "plan body changed since approval"


_GATED_ROWS = """- id: C3
  title: t3
  change_kind: code-edit
  surface: a.py
  deferred: false
  external_gate:
  - owner_repo: other-repo
    condition: land the thing
"""


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_clearing_an_external_gate_keeps_the_stamp_valid_and_real_edit_refuses(tmp_path: Path) -> None:
    from coordinator_core.ops.plan_tasks_mutate import _clear_gate

    _git(tmp_path, "init", "-q")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    plan = tmp_path / "docs" / "plans" / "plan.md"
    approved = _approved(rows=_GATED_ROWS)
    plan.write_text(approved, encoding="utf-8")
    res = _clear_gate("docs/plans/plan.md", "C3", "other-repo", "landed in abc123", tmp_path, tmp_path)
    assert res.get("applied") is True, res
    cleared = plan.read_text(encoding="utf-8")
    assert "cleared: true" in cleared and cleared != approved
    assert check_approved_body(cleared)[0] == APPROVED_BODY_OK
    assert check_approved_body(cleared.replace("land the thing", "land another"))[0] == APPROVED_BODY_CHANGED


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_evidence_append_keeps_a_sealed_plan_ok(tmp_path: Path) -> None:
    from coordinator_core.ops.plan_tasks_mutate import _evidence_append

    _git(tmp_path, "init", "-q")
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    plan = tmp_path / "docs" / "plans" / "plan.md"
    approved = _approved()
    plan.write_text(approved, encoding="utf-8")
    res = _evidence_append("docs/plans/plan.md", "C3", "ran the pin", tmp_path, tmp_path)
    assert res.get("applied") is True, res
    assert plan.read_text(encoding="utf-8") == approved
    assert check_approved_body(plan.read_text(encoding="utf-8"))[0] == APPROVED_BODY_OK
