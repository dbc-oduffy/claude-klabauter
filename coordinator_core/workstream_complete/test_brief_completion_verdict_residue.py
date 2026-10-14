"""brief() emits the owned_uncommitted and unstamped_run_batons readings into
gates and folds them into gates.completion_verdict. Real git repo under
tmp_path; the session shape gate is patched to name the session."""

import subprocess
from pathlib import Path

import pytest

import coordinator_core.workstream_complete as wsc
from coordinator_core.session import scope
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SID = "44444444-4444-4444-4444-444444444444"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "t@t", cwd=tmp_path)
    _git("config", "user.name", "test", cwd=tmp_path)
    (tmp_path / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git("add", "tracked.txt", cwd=tmp_path)
    _git("commit", "-q", "-m", "seed", cwd=tmp_path)
    for var in ("COORDINATOR_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", SID)
    return tmp_path


def _claim(repo: Path, rel: str) -> None:
    scope.touch(SID, rel, cwd=str(repo), root=str(repo))


def _hold_baton(repo: Path, basename: str) -> None:
    claim_dir = repo / ".git" / "coordinator-sessions" / "handoff-claims" / basename
    claim_dir.mkdir(parents=True)
    (claim_dir / "session_id").write_text(SID, encoding="utf-8")


def _dirty_claimed(repo: Path, rel: str = "tracked.txt") -> None:
    (repo / rel).write_text("v2\n", encoding="utf-8")
    _claim(repo, rel)


def test_call_one_reads_owned_uncommitted_indeterminate_and_computes_nothing(repo: Path, monkeypatch) -> None:
    _dirty_claimed(repo)
    calls: list[tuple] = []
    real = wsc._close_residue.owned_uncommitted_payload
    monkeypatch.setattr(wsc._close_residue, "owned_uncommitted_payload", lambda *a, **k: calls.append(a) or real(*a, **k))

    gates = wsc.brief(decisions={}, repo_root=repo)["gates"]

    assert gates["owned_uncommitted"] == {
        "verdict": "indeterminate",
        "items": [],
        "contested": [],
        "reason": "stage_paths unresolved",
    }
    assert calls == []
    assert gates["completion_verdict"]["verdict"] != "complete"


def test_owned_dirty_file_outside_stage_paths_reads_open_and_verdict_incomplete(repo: Path) -> None:
    _dirty_claimed(repo)

    gates = wsc.brief(decisions={"stage_paths": []}, repo_root=repo)["gates"]

    assert gates["owned_uncommitted"]["verdict"] == "open"
    assert gates["owned_uncommitted"]["items"] == ["tracked.txt"]
    assert gates["completion_verdict"]["verdict"] == "incomplete"


def test_owned_dirty_file_named_in_stage_paths_is_not_residue(repo: Path) -> None:
    _dirty_claimed(repo)

    gates = wsc.brief(decisions={"stage_paths": ["tracked.txt"]}, repo_root=repo)["gates"]

    assert gates["owned_uncommitted"]["verdict"] == "clean"


def test_held_baton_without_disposition_reads_open(repo: Path) -> None:
    _hold_baton(repo, "2026-10-14-a.md")

    gates = wsc.brief(decisions={"stage_paths": []}, repo_root=repo)["gates"]

    assert gates["unstamped_run_batons"]["verdict"] == "open"
    assert gates["unstamped_run_batons"]["items"] == ["2026-10-14-a.md"]
    assert gates["completion_verdict"]["verdict"] == "incomplete"


def test_all_paths_staged_and_all_batons_disposed_reads_clean(repo: Path) -> None:
    _dirty_claimed(repo)
    _hold_baton(repo, "2026-10-14-a.md")

    gates = wsc.brief(
        decisions={"stage_paths": ["tracked.txt"], "handoff_dispositions": {"2026-10-14-a.md": {"disposition": "shipped"}}},
        repo_root=repo,
    )["gates"]

    assert gates["owned_uncommitted"]["verdict"] == "clean"
    assert gates["unstamped_run_batons"]["verdict"] == "clean"
    residue = [
        item
        for item in gates["completion_verdict"]["residue"]
        if item.get("gate") in ("owned_uncommitted", "unstamped_run_batons")
    ]
    assert residue == []
