"""coordinator_core.ops.tests.test_distill_bookkeeping -- `distill.bookkeeping` composes the three
shipped bookkeeping ops in process over a tmp_path fixture repo; no agent, no subprocess hop."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.distill_bookkeeping import _handler
from coordinator_core.win_portability import no_console_creationflags

# Spawns real git processes; runs at cadence gates, not per-commit.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_requires_rg = pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
_SID = "00000000-0000-4000-8000-000000000000"
_RUN = "2026-11-14-01h00"
_DONE_PLAN = "docs/plans/2026-11-01-done-plan.md"
_LIVE_PLAN = "docs/plans/2026-11-02-live-plan.md"


def _run(coro):
    return asyncio.run(coro)


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, **no_console_creationflags()
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _plan(status: str) -> str:
    return f"---\ntitle: plan\nstatus: {status}\ncreated: 2026-11-01\n---\n\n# plan\n"


def _fixture_repo(root: Path) -> None:
    """Root commit; harvest commit touching the log; candidate handoff, one terminal and one live plan
    committed together; a quiescent scratch dir."""
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    _write(root, "README.md", "root\n")
    _git("add", "--", "README.md", cwd=root)
    _git("commit", "-q", "-m", "root", cwd=root)
    _write(root, "state/distillation-log.md", "## Run seed\n- seed -> EPHEMERAL, seed (run: seed)\n")
    _git("add", "--", "state/distillation-log.md", cwd=root)
    _git("commit", "-q", "-m", "harvest", cwd=root)
    (root / "state" / "cross-repo-commitments").mkdir(parents=True, exist_ok=True)
    _write(
        root, "archive/handoffs/eligible.md",
        "---\nshipped_in: 68b27420\nstatus: consumed\ndeployment_state: shipped\nrealized_by: inline\n---\nbody\n",
    )
    _write(root, _DONE_PLAN, _plan("implemented"))
    _write(root, _LIVE_PLAN, _plan("executing"))
    _git("add", "--", "archive/handoffs/eligible.md", _DONE_PLAN, _LIVE_PLAN, cwd=root)
    _git("commit", "-q", "-m", "candidates", cwd=root)
    _write(root, "scratch/old-residue/blob.txt", "residue\n")
    old = 1_700_000_000
    os.utime(root / "scratch" / "old-residue" / "blob.txt", (old, old))
    os.utime(root / "scratch" / "old-residue", (old, old))


def _dispatch(root: Path, method: str, params: dict) -> dict:
    import coordinator_core.ipc as ipc
    import coordinator_core.ops  # noqa: F401 -- eager registration

    msg = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params, "_origin_worktree": str(root)}
    return _run(ipc.dispatch_message(msg))


def _assemble(root: Path, *, stamp: bool) -> None:
    assert "result" in _dispatch(
        root, "distill.assemble_disposal_manifest", {"run_id": _RUN, "candidates": ["archive/handoffs/eligible.md"]}
    )
    if stamp:
        assert "result" in _dispatch(
            root, "distill.stamp_disposal",
            {"run_id": _RUN, "by": "pm", "at": "2026-11-14T10:00:00Z", "note": "approved, mass-throttle-ack"},
        )


def _bookkeep(root: Path, **extra) -> dict:
    return _run(_handler({"run_id": _RUN, **extra}, repo_root=root / ".git"))


@_requires_rg
def test_default_call_runs_every_step(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    _fixture_repo(tmp_path)
    _assemble(tmp_path, stamp=True)

    out = _bookkeep(tmp_path)

    assert out["exit_code"] == 0, out
    steps = out["steps"]
    assert steps["disposal"]["ran"] is True
    assert steps["disposal"]["deleted_tracked"] == ["archive/handoffs/eligible.md"]
    assert not (tmp_path / "archive/handoffs/eligible.md").exists()
    assert steps["spec_archive"]["ran"] is True
    assert [a["id"] for a in steps["spec_archive"]["acted"]] == [_DONE_PLAN]
    assert (tmp_path / "archive/specs/2026-11/2026-11-01-done-plan.md").is_file()
    assert not (tmp_path / _DONE_PLAN).exists()
    assert (tmp_path / _LIVE_PLAN).is_file()
    assert steps["scratch"]["ran"] is True
    assert steps["scratch"]["walk"]["deleted"] >= 1
    assert not (tmp_path / "scratch/old-residue").exists()


@_requires_rg
def test_unstamped_manifest_refuses_disposal_but_the_other_steps_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    _fixture_repo(tmp_path)
    _assemble(tmp_path, stamp=False)

    out = _bookkeep(tmp_path)

    assert out["exit_code"] == 1 and out["errored"] == ["disposal"]
    assert "stamp" in out["steps"]["disposal"]["error"]
    assert (tmp_path / "archive/handoffs/eligible.md").is_file()
    assert out["steps"]["spec_archive"]["ran"] is True
    assert out["steps"]["scratch"]["ran"] is True


def test_absent_manifest_skips_disposal_without_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    _fixture_repo(tmp_path)

    out = _bookkeep(tmp_path)

    assert out["exit_code"] == 0, out
    assert out["steps"]["disposal"] == {"ran": False, "reason": "no disposal manifest for this run"}
    assert out["steps"]["spec_archive"]["ran"] is True


def test_non_terminal_plan_is_never_archived(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    _fixture_repo(tmp_path)
    (tmp_path / _DONE_PLAN).write_text(_plan("executing"), encoding="utf-8")
    _git("add", "--", _DONE_PLAN, cwd=tmp_path)
    _git("commit", "-q", "-m", "reopen", cwd=tmp_path)

    out = _bookkeep(tmp_path)

    assert out["steps"]["spec_archive"]["ran"] is False
    assert (tmp_path / _DONE_PLAN).is_file()


def test_plan_cap_bounds_plans_archived_per_call(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    _fixture_repo(tmp_path)
    second = "docs/plans/2026-11-03-second-done-plan.md"
    _write(tmp_path, second, _plan("implemented"))
    _git("add", "--", second, cwd=tmp_path)
    _git("commit", "-q", "-m", "second terminal plan", cwd=tmp_path)

    out = _bookkeep(tmp_path, plan_cap=1)

    assert len(out["steps"]["spec_archive"]["acted"]) == 1
    assert [p for p in (_DONE_PLAN, second) if (tmp_path / p).exists()] != []


def test_run_id_and_repo_root_are_required(tmp_path: Path):
    with pytest.raises(ValueError, match="run_id"):
        _run(_handler({}, repo_root=tmp_path / ".git"))
    with pytest.raises(ValueError, match="repo_root"):
        _run(_handler({"run_id": _RUN}, repo_root=None))
    with pytest.raises(ValueError, match="plan_cap"):
        _run(_handler({"run_id": _RUN, "plan_cap": 0}, repo_root=tmp_path / ".git"))
