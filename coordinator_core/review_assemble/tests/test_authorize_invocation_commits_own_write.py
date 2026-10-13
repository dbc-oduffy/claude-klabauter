"""authorize-invocation commits its own four-field write (tracked and untracked plans)."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter.primitives import stamp_approved_body_sha
from coordinator_core.review_assemble import exec_auth_stamp
from coordinator_core.review_assemble.exec_auth_stamp import EXIT_OK, main
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PLAN = "docs/plans/p.md"
_SIZING = "state/sizings/s.yaml"
_SIZING_TEXT = (
    "status: sized\ninteraction_mode: ceo\nexit_criterion:\n  statement: done\n"
    "  accepted:\n    pm_quote: yes that criterion is right\n    on: '2026-10-05'\n    mode: ceo\n"
)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=15,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


@pytest.fixture(autouse=True)
def _no_session_id(monkeypatch):
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)


def _repo(tmp_path: Path, monkeypatch, *, status: str, tracked: bool) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-b", "work/test/2026-01-01")
    for k, v in (("commit.gpgsign", "false"), ("user.email", "t@example.com"), ("user.name", "T")):
        _git(tmp_path, "config", k, v)
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / "README.md").write_text("init\n", encoding="utf-8")
    (tmp_path / _SIZING).write_text(_SIZING_TEXT, encoding="utf-8")
    plan = (
        f'---\ntitle: "t"\nstatus: {status}\ndeliverable_id: dlv-test-1\n---\n\n# Plan\n\nBody.\n'
    )
    if status == "approved":
        plan = stamp_approved_body_sha(plan)
    (tmp_path / _PLAN).write_text(plan, encoding="utf-8")
    _git(tmp_path, "add", "README.md", _SIZING)
    if tracked:
        _git(tmp_path, "add", _PLAN)
    _git(tmp_path, "commit", "-m", "init")
    monkeypatch.chdir(tmp_path)


def _assert_committed(tmp_path: Path) -> None:
    assert _git(tmp_path, "status", "--porcelain", "--", _PLAN).stdout.strip() == ""
    head = _git(tmp_path, "show", f"HEAD:{_PLAN}").stdout
    for field in (
        "execution_authorized_by:",
        "execution_authorized_at:",
        "execution_authorized_sha:",
        "execution_authorized_note:",
    ):
        assert field in head


def test_tracked_approved_plan_stamp_is_committed(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch, status="approved", tracked=True)
    assert main(["authorize-invocation", _PLAN, "--authorized-by-sizing", _SIZING]) == EXIT_OK
    assert "own_write_commit" not in capsys.readouterr().out
    _assert_committed(tmp_path)


def test_untracked_reviewed_plan_is_first_committed_with_the_stamp(tmp_path, monkeypatch):
    _repo(tmp_path, monkeypatch, status="reviewed", tracked=False)
    assert main(["authorize-invocation", _PLAN, "--authorized-by-sizing", _SIZING]) == EXIT_OK
    _assert_committed(tmp_path)
    assert "Deliverable-Id: dlv-test-1" in _git(tmp_path, "log", "-1", "--format=%B", "--", _PLAN).stdout


def test_commit_failure_is_named_and_exit_stays_zero(tmp_path, monkeypatch, capsys):
    _repo(tmp_path, monkeypatch, status="approved", tracked=True)
    from coordinator_core.ops import plan_status_transition as pst

    class _Bad:
        ok = False
        stderr = "boom"

    monkeypatch.setattr(pst, "_commit_plan_flip", lambda *a, **k: _Bad())
    monkeypatch.setattr(exec_auth_stamp, "_fire_stamp_approved", lambda *a, **k: None)
    assert main(["authorize-invocation", _PLAN, "--authorized-by-sizing", _SIZING]) == EXIT_OK
    cap = capsys.readouterr()
    assert '"own_write_commit": "failed"' in cap.out
    assert "boom" in cap.err
