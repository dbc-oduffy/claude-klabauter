"""A solo bare `git commit` carrying `-c coordinator.staged=<path>` is allowed when every staged path is named, denied naming the rest otherwise.

Real tmp_path repos; the staged partial hunk comes from `git apply --cached`.
"""

from __future__ import annotations

import shlex
import subprocess

import pytest

from coordinator_core.bash_guards import dispatch_checks

pytestmark = pytest.mark.spawns_process

_BASE = "".join("line%02d\n" % i for i in range(1, 31))
_HUNK1 = _BASE.replace("line02\n", "line02-A\n")
_BOTH = _HUNK1.replace("line29\n", "line29-B\n")


def _git(repo, *args, stdin=None):
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        input=stdin, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _repo_with_partial_hunk(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text(_BASE)
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "f.txt").write_text(_HUNK1)
    patch = _git(repo, "diff")
    (repo / "f.txt").write_text(_BOTH)
    _git(repo, "apply", "--cached", "-", stdin=patch)
    assert _git(repo, "diff", "--cached", "--name-only").split() == ["f.txt"]
    return repo


def _stage_file(repo, name, content="x\n"):
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", name)


def _cmd(repo, *carriers, extra=""):
    cs = "".join(" -c coordinator.staged=%s" % shlex.quote(c) for c in carriers)
    return "git -C %s%s commit%s -m x" % (shlex.quote(str(repo)), cs, extra)


def _out(cmd, payload=None):
    return dispatch_checks.check_git_commit_safe_commit_advise(cmd, "sess-named", payload)


def _decision(out):
    return None if out is None else out["hookSpecificOutput"]["permissionDecision"]


def _text(out):
    hook = out["hookSpecificOutput"]
    return hook.get("permissionDecisionReason") or hook.get("additionalContext") or ""


@pytest.fixture(autouse=True)
def _clean_probe_state():
    dispatch_checks._clear_probe_fail_open_reasons()
    yield
    dispatch_checks._clear_probe_fail_open_reasons()


def test_named_set_allows(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    assert _out(_cmd(repo, "f.txt")) is None


def test_extra_staged_path_denies_and_names_it(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    _stage_file(repo, "other.txt")
    out = _out(_cmd(repo, "f.txt"))
    assert _decision(out) == "deny"
    assert "staged but not named: other.txt" in _text(out)
    assert "f.txt," not in _text(out)


def test_foreign_staged_file_denies(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    out = _out(_cmd(repo, "mine.txt"))
    assert _decision(out) == "deny"
    assert "staged but not named: f.txt" in _text(out)


@pytest.mark.parametrize("bad", ["", "/abs/f.txt", "../f.txt", "a/../f.txt"])
def test_invalid_named_path_denies(tmp_path, bad):
    repo = _repo_with_partial_hunk(tmp_path)
    assert _decision(_out(_cmd(repo, "f.txt", bad))) == "deny"


def test_named_directory_does_not_cover_a_staged_file(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    _stage_file(repo, "d/g.txt")
    out = _out(_cmd(repo, "f.txt", "d"))
    assert _decision(out) == "deny"
    assert "d/g.txt" in _text(out)


def test_empty_index_with_carrier_stays_advisory(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    _git(repo, "reset", "-q")
    assert _decision(_out(_cmd(repo, "f.txt"))) == "allow"


def test_probe_failure_stays_fail_open_advisory(tmp_path, monkeypatch):
    def _stub(args, cwd=None, timeout=2.0, extra_env=None):
        return (0, "x\n") if args[0] == "rev-parse" else (-1, "")

    monkeypatch.setattr(dispatch_checks._dc_git_probe, "_run_git", _stub)
    out = _out('git -C /repo -c coordinator.staged=f.txt commit -m "x"')
    assert _decision(out) == "allow"
    assert "NOT read" in _text(out)


def test_sweep_all_with_carrier_is_unaffected(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    out = _out(_cmd(repo, "f.txt", extra=" -a"))
    assert "staged but not named" not in (_text(out) if out else "")


def test_compound_add_then_carrier_commit_still_denies(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    cmd = "git -C %s add -- f.txt && %s" % (shlex.quote(str(repo)), _cmd(repo, "f.txt"))
    out = _out(cmd)
    assert _decision(out) == "deny"
    assert "names no scope" in _text(out)


def test_amend_with_carrier_still_denies(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    out = _out(_cmd(repo, "f.txt", extra=" --amend"))
    assert _decision(out) == "deny"


def test_amend_gate_stays_first_without_override(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    out = _out(_cmd(repo, "f.txt", extra=" --amend"))
    assert "--amend" in _text(out)


def test_staged_rename_naming_only_destination_denies_naming_source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "old.txt").write_text(_BASE)
    _git(repo, "add", "old.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "mv", "old.txt", "new.txt")
    out = _out(_cmd(repo, "new.txt"))
    assert _decision(out) == "deny"
    assert "old.txt" in _text(out)
    assert _out(_cmd(repo, "new.txt", "old.txt")) is None


def test_carrier_absent_denies_and_offers_the_staged_form(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    out = _out(_cmd(repo))
    assert _decision(out) == "deny"
    assert "-- <paths>" in _text(out)
    assert "coordinator.staged=<path>" in _text(out)
    assert "prefer_staged" in _text(out)


def _ps_payload(repo, cmd):
    return {"tool_name": "PowerShell", "cwd": str(repo), "tool_input": {"command": cmd}}


def test_powershell_dialect_allow_and_deny_pair(tmp_path):
    repo = _repo_with_partial_hunk(tmp_path)
    assert _out(_cmd(repo, "f.txt"), _ps_payload(repo, "")) is None
    _stage_file(repo, "other.txt")
    out = _out(_cmd(repo, "f.txt"), _ps_payload(repo, ""))
    assert _decision(out) == "deny"
    assert "other.txt" in _text(out)


def test_carrier_path_adds_no_git_call(tmp_path, monkeypatch):
    repo = _repo_with_partial_hunk(tmp_path)
    real = dispatch_checks._dc_git_probe._run_git
    calls = []

    def _spy(args, cwd=None, timeout=2.0, extra_env=None):
        calls.append(list(args))
        return real(args, cwd=cwd, timeout=timeout, extra_env=extra_env)

    monkeypatch.setattr(dispatch_checks._dc_git_probe, "_run_git", _spy)
    assert _out(_cmd(repo, "f.txt")) is None
    diffs = [c for c in calls if c[0] == "diff"]
    assert len(diffs) == 1 and "--no-renames" in diffs[0]
    assert not [c for c in calls if c[0] not in ("diff", "rev-parse")]
