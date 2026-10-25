"""block_em_halted_withheld_commit lets only the merge-owning run's withheld paths through a whole-index merge commit."""

from __future__ import annotations

import json
import subprocess

import pytest

from coordinator_core.bash_guards import block_em_halted_withheld_commit as guard
from coordinator_core.git import merge_owner
from coordinator_core.guard_messages.testing import envelope_of
from coordinator_core.win_portability import no_console_creationflags

R = "ask-20261024T000000-aaaaaa"
S = "ask-20261024T000000-bbbbbb"
CMD = "git " + "commit"

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=repo, check=check, capture_output=True, text=True, **no_console_creationflags(),
    )


def _withhold(repo, run, paths):
    index = repo / ".coordinator-local" / "halt-withheld"
    index.mkdir(parents=True, exist_ok=True)
    (index / ".built").write_bytes(b"")
    (index / f"{run}.json").write_text(json.dumps({"paths": paths}))


@pytest.fixture
def merging(tmp_path):
    """Open merge in tmp_path: P (a.py) conflicted and resolved+staged; Q (q.py) staged, non-conflicting."""
    repo = tmp_path
    _git(repo, "init", "-q", "-b", "main")
    for name in ("a.py", "q.py", "b.py"):
        (repo / name).write_text("base\n")
    _git(repo, "add", "a.py", "q.py", "b.py")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "a.py").write_text("side\n")
    _git(repo, "commit", "-q", "-am", "side")
    _git(repo, "checkout", "-q", "main")
    (repo / "a.py").write_text("main\n")
    _git(repo, "commit", "-q", "-am", "main")
    _git(repo, "merge", "--no-ff", "--no-commit", "side", check=False)
    return repo


def _resolve(repo):
    (repo / "a.py").write_text("resolved\n")
    _git(repo, "add", "a.py")


def _own(repo, run):
    merge_head = (repo / ".git" / "MERGE_HEAD").read_text().strip()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    merge_owner.record(repo, run, merge_head, head)


def _verdict(repo, cmd):
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1", "cwd": str(repo)}
    return envelope_of(guard.check(payload))


def _denied(result):
    return result is not None and result["hookSpecificOutput"]["permissionDecision"] == "deny"


def _text(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_owner_run_merge_commit_with_resolved_conflict_is_allowed(merging):
    _withhold(merging, R, ["a.py"])
    _resolve(merging)
    _own(merging, R)
    assert guard.check({"tool_name": "Bash", "tool_input": {"command": CMD + " -m x"}, "cwd": str(merging)}) is None


def test_other_runs_owner_record_still_denies(merging):
    _withhold(merging, R, ["a.py"])
    _resolve(merging)
    _own(merging, S)
    assert _denied(_verdict(merging, CMD + " -m x"))


def test_no_owner_record_still_denies(merging):
    _withhold(merging, R, ["a.py"])
    _resolve(merging)
    assert _denied(_verdict(merging, CMD + " -m x"))


def test_unmerged_path_still_denies(merging, monkeypatch):
    monkeypatch.setattr(guard, "_drop_landed_merge_paths", lambda root, hits: hits)
    _withhold(merging, R, ["a.py", "q.py"])
    (merging / "q.py").write_text("q2\n")
    _git(merging, "add", "q.py")
    _own(merging, R)
    assert "a.py" in _git(merging, "ls-files", "-u").stdout
    assert _denied(_verdict(merging, CMD + " -m x"))


def test_second_path_withheld_by_other_run_denies_naming_it(merging):
    _withhold(merging, R, ["a.py"])
    _withhold(merging, S, ["q.py"])
    _resolve(merging)
    (merging / "q.py").write_text("q2\n")
    _git(merging, "add", "q.py")
    _own(merging, R)
    result = _verdict(merging, CMD + " -m x")
    assert _denied(result)
    text = _text(result)
    assert S in text and "q.py" in text


def test_plain_hand_commit_of_withheld_path_without_merge_denied(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("x\n")
    _withhold(tmp_path, R, ["a.py"])
    assert _denied(_verdict(tmp_path, CMD + " -m x a.py"))


def test_sweeping_commit_with_owner_merge_resolved_denied(merging):
    _withhold(merging, R, ["a.py"])
    _resolve(merging)
    _own(merging, R)
    assert _denied(_verdict(merging, CMD + " -a -m x"))
    assert _denied(_verdict(merging, CMD + " --all -m x"))


def test_unreadable_index_keeps_every_hit_and_denies(merging, monkeypatch):
    from coordinator_core.git import git_index

    def _boom(*_a, **_k):
        raise OSError("unreadable")

    monkeypatch.setattr(git_index, "parse_index_identity", _boom)
    _withhold(merging, R, ["a.py"])
    _resolve(merging)
    _own(merging, R)
    assert _denied(_verdict(merging, CMD + " -m x"))


def test_pathspec_commit_with_owner_merge_denied(merging):
    _withhold(merging, R, ["a.py"])
    _resolve(merging)
    _own(merging, R)
    assert _denied(_verdict(merging, CMD + " -m x a.py"))
