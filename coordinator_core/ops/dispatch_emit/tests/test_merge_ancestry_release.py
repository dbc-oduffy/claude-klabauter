"""merge_ancestry_release drops only the halted holds an open merge brings in."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.ops.dispatch_emit import merge_ancestry_release
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

H = "ask-20200101T000000-abc123"
H2 = "ask-20200101T000001-def456"
H3 = "ask-20200101T000002-0a1b2c"
LIVE = "ask-20200101T000003-fedcba"
P = "src/held.py"
Q = "src/other.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _halted(root: Path, run: str, *, status: str = "halted") -> None:
    _write(root, f"state/warp/{run}/halted.json", json.dumps({"status": status}))
    _write(root, f"state/warp/{run}/run.json", json.dumps({"sizing_rel": "state/sizings/s.yaml"}))


def _repo(tmp_path: Path, runs=(H,)) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.example")
    _git(root, "config", "user.name", "t")
    _write(root, "state/sizings/s.yaml", "deliverable_id: deliv-xyz\n")
    for run in runs:
        _halted(root, run)
    _write(root, P, "base\n")
    _write(root, Q, "base\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _branch_commit(root: Path, branch: str, message: str, rel: str = P) -> None:
    _git(root, "checkout", "-q", "-b", branch, "main")
    _write(root, rel, f"{branch}\n")
    _git(root, "add", rel)
    _git(root, "commit", "-q", "-m", message)
    _git(root, "checkout", "-q", "main")


def _open_merge(root: Path, branch: str = "theirs") -> None:
    _write(root, "main_side.txt", "x\n")
    _git(root, "add", "main_side.txt")
    _git(root, "commit", "-q", "-m", "main side")
    _git(root, "merge", "--no-ff", "--no-commit", "-q", branch)
    assert (root / ".git" / "MERGE_HEAD").exists()


def _count_run_git(monkeypatch) -> list:
    calls: list = []
    real = git_run.run_git

    def counting(args, **kwargs):
        calls.append(list(args))
        return real(args, **kwargs)

    monkeypatch.setattr(git_run, "run_git", counting)
    return calls


def test_halt_record_commit_on_theirs_releases_a_moved_path(tmp_path):
    root = _repo(tmp_path)
    _branch_commit(root, "theirs", f"warp: record halted {H} run artifacts")
    _open_merge(root)
    assert merge_ancestry_release.release(root, [(P, H)]) == []


def test_landing_commit_naming_the_holder_structurally_releases(tmp_path):
    root = _repo(tmp_path)
    _branch_commit(root, "theirs", f"land C1\n\nLands-Chunks: C1\nInline-Review: pass {H}")
    _open_merge(root)
    assert merge_ancestry_release.release(root, [(P, H)]) == []


def test_landing_naming_the_sizing_deliverable_releases(tmp_path):
    root = _repo(tmp_path)
    _branch_commit(root, "theirs", "land C1\n\nLands-Chunks: C1\nDeliverable-Id: deliv-xyz")
    _open_merge(root)
    assert merge_ancestry_release.release(root, [(P, H)]) == []


def test_commits_not_reachable_from_merge_head_still_hold(tmp_path):
    root = _repo(tmp_path)
    _branch_commit(root, "theirs", "unrelated change")
    _branch_commit(root, "unmerged", f"warp: record halted {H} run artifacts", rel=Q)
    _open_merge(root)
    assert merge_ancestry_release.release(root, [(P, H)]) == [(P, H)]


def test_halt_record_reachable_but_path_not_moved_by_merge_holds(tmp_path):
    root = _repo(tmp_path)
    _branch_commit(root, "theirs", f"warp: record halted {H} run artifacts", rel=Q)
    _open_merge(root)
    assert merge_ancestry_release.release(root, [(P, H)]) == [(P, H)]


def test_only_the_moved_path_of_a_released_holder_is_dropped(tmp_path):
    root = _repo(tmp_path)
    _branch_commit(root, "theirs", f"warp: record halted {H} run artifacts")
    _open_merge(root)
    assert merge_ancestry_release.release(root, [(P, H), (Q, H)]) == [(Q, H)]


def test_subject_prose_mention_does_not_release(tmp_path):
    root = _repo(tmp_path)
    _branch_commit(root, "theirs", f"fix the thing {H} complained about")
    _open_merge(root)
    assert merge_ancestry_release.release(root, [(P, H)]) == [(P, H)]


def test_live_holder_holds(tmp_path):
    root = _repo(tmp_path, runs=(H,))
    _write(root, f"state/warp/{LIVE}/run.json", json.dumps({"sizing_rel": "state/sizings/s.yaml"}))
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "live record")
    _branch_commit(root, "theirs", f"warp: record halted {LIVE} run artifacts")
    _open_merge(root)
    assert merge_ancestry_release.release(root, [(P, LIVE)]) == [(P, LIVE)]


def test_no_merge_head_returns_input_with_zero_git_calls(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    calls = _count_run_git(monkeypatch)
    holds = [(P, H)]
    assert merge_ancestry_release.release(root, holds) == holds
    assert calls == []


def test_three_halted_holders_cost_one_git_call(tmp_path, monkeypatch):
    root = _repo(tmp_path, runs=(H, H2, H3))
    _branch_commit(root, "theirs", f"warp: record halted {H} run artifacts")
    _git(root, "checkout", "-q", "theirs")
    for run, rel in ((H2, Q), (H3, "src/third.py")):
        _write(root, rel, "t\n")
        _git(root, "add", rel)
        _git(root, "commit", "-q", "-m", f"warp: record halted {run} run artifacts")
    _git(root, "checkout", "-q", "main")
    _open_merge(root)
    calls = _count_run_git(monkeypatch)
    holds = [(P, H), (Q, H2), ("src/third.py", H3)]
    assert merge_ancestry_release.release(root, holds) == []
    assert len(calls) == 1


def test_nonzero_git_rc_keeps_every_hold(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _branch_commit(root, "theirs", f"warp: record halted {H} run artifacts")
    _open_merge(root)
    real = git_run.run_git

    def failing(args, **kwargs):
        return real(["log", "--no-such-flag"], cwd=str(root))

    monkeypatch.setattr(git_run, "run_git", failing)
    assert merge_ancestry_release.release(root, [(P, H)]) == [(P, H)]


def test_raising_git_keeps_every_hold(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    _branch_commit(root, "theirs", f"warp: record halted {H} run artifacts")
    _open_merge(root)

    def boom(args, **kwargs):
        raise OSError("no git")

    monkeypatch.setattr(git_run, "run_git", boom)
    assert merge_ancestry_release.release(root, [(P, H)]) == [(P, H)]
