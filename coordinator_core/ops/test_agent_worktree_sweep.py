from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.agent_worktree_sweep import (
    _delete_branch_best_effort,
    _json_escape,
    _parse_worktree_porcelain,
    classify_worktree,
    main,
)
from coordinator_core.session import liveness as cs_liveness
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]


def _git(*args: str, cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        timeout=30,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


@pytest.fixture(autouse=True)
def _isolate_global_git_config(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def _init_repo(root: Path) -> None:
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("hello\n")
    (root / ".claude").mkdir(exist_ok=True)
    (root / ".claude" / "settings.local.json").write_text("{}\n")
    (root / ".last-cleanup").write_text("2026-01-01\n")
    _git("add", "README.md", ".claude/settings.local.json", ".last-cleanup", cwd=root)
    _git("commit", "-q", "-m", "initial", cwd=root)
    _git("branch", "-M", "main", cwd=root)


def _add_agent_worktree(root: Path, name: str) -> Path:
    wt_path = root / ".claude" / "worktrees" / f"agent-{name}"
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-b", f"worktree-agent-{name}", str(wt_path), cwd=root)
    return wt_path


def _lines(capsys) -> list:
    out = capsys.readouterr().out
    return [line for line in out.splitlines() if line]


def _states(lines: list) -> dict:
    result = {}
    for line in lines:
        obj = json.loads(line)
        result[obj["path"]] = obj
    return result


def test_not_a_git_repo_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "not in a git repo" in captured.err


def test_unknown_arg_exits_2(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main(["--bogus"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "unknown arg" in captured.err


def test_help_exits_0(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["--help"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "Usage: agent-worktree-sweep.sh" in captured.out


def test_no_agent_worktrees_empty_output(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    assert _lines(capsys) == []


def test_non_agent_worktree_ignored(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    other = tmp_path / "sibling-worktree"
    _git("worktree", "add", "-b", "some-branch", str(other), cwd=tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    assert _lines(capsys) == []


def test_empty_clean_worktree_classified(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "one")
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["state"] == "empty-clean"
    assert states[wt.as_posix()]["action"] == "scan-only"


def test_commits_clean_worktree_classified(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "two")
    (wt / "extra.txt").write_text("stuff\n")
    _git("add", "extra.txt", cwd=wt)
    _git("commit", "-q", "-m", "wip", cwd=wt)
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["state"] == "commits-clean"


def test_dirty_worktree_classified(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "three")
    (wt / "scratch.txt").write_text("uncommitted\n")
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["state"] == "dirty"


def test_dirty_benign_allowlist_classified(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "four")
    (wt / ".claude" / "settings.local.json").write_text('{"edited": true}\n')
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["state"] == "dirty-benign"


def test_dirty_benign_mixed_with_nonbenign_falls_to_dirty(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "five")
    (wt / ".claude" / "settings.local.json").write_text('{"edited": true}\n')
    (wt / "real-change.py").write_text("x = 1\n")
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["state"] == "dirty"


def test_reap_removes_empty_clean(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "reap-empty")
    monkeypatch.chdir(tmp_path)
    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["action"] == "removed"
    assert not wt.exists()


def test_reap_removes_dirty_benign(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "reap-benign")
    (wt / ".last-cleanup").write_text("2026-07-17\n")
    monkeypatch.chdir(tmp_path)
    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["action"] == "removed"
    assert not wt.exists()


def test_reap_cherry_picks_commits_clean(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "reap-commits")
    (wt / "new-file.txt").write_text("payload\n")
    _git("add", "new-file.txt", cwd=wt)
    _git("commit", "-q", "-m", "agent commit", cwd=wt)
    monkeypatch.chdir(tmp_path)
    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["action"] == "salvaged-removed"
    assert not wt.exists()
    log = _git("log", "--oneline", "-1", cwd=tmp_path).stdout
    assert "agent commit" in log


def test_reap_cherry_pick_spawn_count_does_not_grow_with_commit_count(tmp_path, capsys, monkeypatch):
    """Amplification-gate regression for `_KNOWN_SITES`'s
    `_sweep_one -> _cherry_pick_with_env` row (REFUTED, 2026-08-19 adversarial
    re-verification: `git cherry-pick -x active_branch..tip_sha` batches the
    whole range in one spawn). Model:
    `test_schema_drift_watch.py::TestSchemaAdvisoryBatch::
    test_process_count_does_not_grow_with_the_set`. Before this fix the sweep
    issued one `git cherry-pick` subprocess per commit (N spawns for N
    commits); after, it issues exactly one regardless of N."""
    import coordinator_core.ops.agent_worktree_sweep as aws

    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "reap-many-commits")
    n_commits = 5
    for i in range(n_commits):
        (wt / f"file{i}.txt").write_text(f"payload {i}\n")
        _git("add", f"file{i}.txt", cwd=wt)
        _git("commit", "-q", "-m", f"agent commit {i}", cwd=wt)
    monkeypatch.chdir(tmp_path)

    real_subprocess_run = aws.subprocess.run
    cherry_pick_calls: list = []

    def _wrapped_subprocess_run(args, *a, **kw):
        if len(args) >= 4 and args[0] == "git" and "cherry-pick" in args:
            cherry_pick_calls.append(list(args))
        return real_subprocess_run(args, *a, **kw)

    monkeypatch.setattr(aws.subprocess, "run", _wrapped_subprocess_run)

    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    row = states[wt.as_posix()]
    assert row["action"] == "salvaged-removed"
    assert f"cherry-picked={n_commits}" in row["detail"]
    assert not wt.exists()

    assert len(cherry_pick_calls) == 1
    argv = cherry_pick_calls[0]
    assert argv[-1].startswith("main..")
    assert "-x" in argv


def test_reap_leaves_dirty_worktree(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "reap-dirty")
    (wt / "scratch.txt").write_text("uncommitted\n")
    monkeypatch.chdir(tmp_path)
    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["action"] == "warned-skip"
    assert wt.exists()


def test_advisory_scan_does_not_prune(tmp_path, capsys, monkeypatch):
    import coordinator_core.ops.agent_worktree_sweep as mod

    _init_repo(tmp_path)
    calls: list = []
    monkeypatch.setattr(mod, "_worktree_prune", lambda repo_root: calls.append(repo_root))
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    assert calls == []


def test_reap_scan_still_prunes(tmp_path, capsys, monkeypatch):
    import coordinator_core.ops.agent_worktree_sweep as mod

    _init_repo(tmp_path)
    calls: list = []
    monkeypatch.setattr(mod, "_worktree_prune", lambda repo_root: calls.append(repo_root))
    monkeypatch.chdir(tmp_path)
    rc = main(["--reap"])
    assert rc == 0
    assert len(calls) == 1


def test_reap_forced_off_on_detached_head(tmp_path, capsys, monkeypatch):
    """Regression test for the detached-HEAD reap-clamp edge case: without
    the guard, ACTIVE_BRANCH is empty and COMPARE_REF falls back to the SHA
    only for classification — reap must still be forced off so a
    commits-clean worktree is never cherry-picked/removed while detached."""
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "detached")
    (wt / "extra.txt").write_text("stuff\n")
    _git("add", "extra.txt", cwd=wt)
    _git("commit", "-q", "-m", "wip", cwd=wt)

    head_sha = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()
    _git("checkout", "-q", head_sha, cwd=tmp_path)

    monkeypatch.chdir(tmp_path)
    rc = main(["--reap"])
    captured = capsys.readouterr()
    assert "detached HEAD; refuse to reap" in captured.err
    states = _states([line for line in captured.out.splitlines() if line])
    assert states[wt.as_posix()]["action"] == "scan-only"
    assert states[wt.as_posix()]["state"] == "commits-clean"
    assert wt.exists()
    assert rc == 0


def test_json_escape_backslash_before_quote():
    assert _json_escape('a\\"b') == 'a\\\\\\"b'


def test_json_escape_tab_and_newline():
    assert _json_escape("a\tb\nc") == "a\\tb\\nc"


def test_parse_worktree_porcelain_multiple_stanzas():
    text = (
        "worktree /repo\n"
        "HEAD abc123\n"
        "branch refs/heads/main\n"
        "\n"
        "worktree /repo/.claude/worktrees/agent-xyz\n"
        "HEAD def456\n"
        "branch refs/heads/worktree-agent-xyz\n"
        "locked\n"
    )
    parsed = _parse_worktree_porcelain(text)
    assert len(parsed) == 2
    assert parsed[0].path == "/repo"
    assert parsed[0].branch == "main"
    assert parsed[0].locked is False
    assert parsed[1].path == "/repo/.claude/worktrees/agent-xyz"
    assert parsed[1].branch == "worktree-agent-xyz"
    assert parsed[1].locked is True


def test_classify_worktree_missing_compare_ref_treats_as_zero_ahead(tmp_path):
    _init_repo(tmp_path)
    result = classify_worktree(str(tmp_path), "")
    assert result.commits_ahead == 0


# KNOWN STRUCTURAL GAP note), so the gate answers a coarser question: is any

def _track_run_argv(monkeypatch):
    import coordinator_core.ops.agent_worktree_sweep as aws

    real_run = aws._run
    recorded: list = []

    def _wrapped(args, cwd=None, timeout=aws._GIT_TIMEOUT_SECS):
        recorded.append(list(args))
        return real_run(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(aws, "_run", _wrapped)
    return recorded


def test_reap_skipped_when_live_peer_session_present(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "peer-live")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "self-sid")
    monkeypatch.setattr(
        cs_liveness, "live_session_ids", lambda cwd=None: frozenset({"self-sid", "peer-sid"})
    )
    recorded = _track_run_argv(monkeypatch)

    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    row = states[wt.as_posix()]
    assert row["action"] == "reap-skipped"
    assert "peer-sid" in row["detail"]
    assert wt.exists()
    assert not any(
        len(a) >= 5 and a[0] == "git" and a[3] == "worktree" and a[4] == "remove"
        for a in recorded
    )
    assert not any(
        len(a) >= 5 and a[0] == "git" and a[3] == "branch" and a[4] == "-D" for a in recorded
    )


def test_reap_proceeds_when_no_live_peer_session(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "self-only-live")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "self-sid")
    monkeypatch.setattr(cs_liveness, "live_session_ids", lambda cwd=None: frozenset({"self-sid"}))

    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    assert states[wt.as_posix()]["action"] == "removed"
    assert not wt.exists()


def test_reap_skipped_when_owner_unresolvable(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "unresolvable-self")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.setattr(cs_liveness, "live_session_ids", lambda cwd=None: frozenset({"peer-sid"}))

    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    row = states[wt.as_posix()]
    assert row["action"] == "reap-skipped"
    assert "unknown" in row["detail"]
    assert wt.exists()


def test_reap_skipped_when_liveness_probe_raises(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "probe-raises")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "self-sid")

    def _raise(cwd=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(cs_liveness, "live_session_ids", _raise)

    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    row = states[wt.as_posix()]
    assert row["action"] == "reap-skipped"
    assert "unknown" in row["detail"]
    assert wt.exists()


def test_reap_skip_distinguishable_from_scan_only_and_nothing_eligible(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "distinguish")
    monkeypatch.chdir(tmp_path)

    rc = main([])
    assert rc == 0
    scan_only_action = _states(_lines(capsys))[wt.as_posix()]["action"]

    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "self-sid")
    monkeypatch.setattr(
        cs_liveness, "live_session_ids", lambda cwd=None: frozenset({"self-sid", "peer-sid"})
    )
    rc = main(["--reap"])
    assert rc == 0
    blocked_action = _states(_lines(capsys))[wt.as_posix()]["action"]

    assert scan_only_action == "scan-only"
    assert blocked_action == "reap-skipped"
    assert scan_only_action != blocked_action
    assert wt.exists()


def test_reap_skipped_emits_visible_line_in_json_and_text(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "visible-both")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "self-sid")
    monkeypatch.setattr(
        cs_liveness, "live_session_ids", lambda cwd=None: frozenset({"self-sid", "peer-sid"})
    )

    rc = main(["--reap", "--format", "json"])
    assert rc == 0
    json_out = capsys.readouterr().out
    assert '"action":"reap-skipped"' in json_out
    assert "peer-sid" in json_out

    rc = main(["--reap", "--format", "text"])
    assert rc == 0
    text_out = capsys.readouterr().out
    assert "reap-skipped" in text_out
    assert "peer-sid" in text_out
    assert wt.exists()


def test_delete_branch_best_effort_returns_none_on_success(tmp_path):
    _init_repo(tmp_path)
    _git("branch", "throwaway", cwd=tmp_path)
    assert _delete_branch_best_effort(tmp_path, "throwaway") is None


def test_delete_branch_best_effort_returns_none_for_empty_branch(tmp_path):
    _init_repo(tmp_path)
    assert _delete_branch_best_effort(tmp_path, "") is None


def test_delete_branch_best_effort_returns_error_string_on_failure(tmp_path):
    _init_repo(tmp_path)
    result = _delete_branch_best_effort(tmp_path, "no-such-branch-exists")
    assert result is not None
    assert result != ""


def test_reap_surfaces_branch_delete_failure_in_emitted_detail(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "branch-delete-fails")
    monkeypatch.chdir(tmp_path)

    import coordinator_core.ops.agent_worktree_sweep as aws

    real_run = aws._run

    def _faulty_run(args, cwd=None, timeout=aws._GIT_TIMEOUT_SECS):
        if len(args) >= 5 and args[3] == "branch" and args[4] == "-D":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="branch is checked out\n")
        return real_run(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(aws, "_run", _faulty_run)

    rc = main(["--reap"])
    assert rc == 0
    states = _states(_lines(capsys))
    row = states[wt.as_posix()]
    assert row["action"] == "removed"
    assert not wt.exists()
    assert "branch delete failed" in row["detail"]
    assert "branch is checked out" in row["detail"]


def test_parse_worktree_porcelain_captures_lock_reason():
    text = (
        "worktree /repo/.claude/worktrees/agent-xyz\n"
        "HEAD def456\n"
        "branch refs/heads/worktree-agent-xyz\n"
        "locked some reason text\n"
    )
    parsed = _parse_worktree_porcelain(text)
    assert parsed[0].locked is True
    assert parsed[0].lock_reason == "some reason text"


def test_parse_worktree_porcelain_locked_no_reason_is_empty_string():
    text = (
        "worktree /repo/.claude/worktrees/agent-xyz\n"
        "HEAD def456\n"
        "branch refs/heads/worktree-agent-xyz\n"
        "locked\n"
    )
    parsed = _parse_worktree_porcelain(text)
    assert parsed[0].locked is True
    assert parsed[0].lock_reason == ""


def test_scan_only_output_includes_lock_reason_field(tmp_path, capsys, monkeypatch):
    _init_repo(tmp_path)
    wt = _add_agent_worktree(tmp_path, "lockfield")
    monkeypatch.chdir(tmp_path)
    rc = main([])
    assert rc == 0
    states = _states(_lines(capsys))
    assert "lock_reason" in states[wt.as_posix()]
