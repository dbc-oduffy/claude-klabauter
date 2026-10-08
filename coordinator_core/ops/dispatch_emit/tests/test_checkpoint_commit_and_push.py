"""Each settled wave's DONE rows are committed once through `ceremony.commit_v2`
by a serialised `coordinator:git-commit-agent` leg and checkpoint-pushed to the
run's own work branch, so a run cut off mid-way strands nothing."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import (
    _SHARED_PATH_ARRAY_THRESHOLD,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW, execute_section
from coordinator_core.ops.dispatch_emit.tests.test_emit_dag import _write_row

_SESSION = "9d860631-c114-462a-a6ee-f685cc671ced"


def _script(rows=None, **kw):
    rows = rows or [_write_row("C1"), _write_row("C2")]
    return compose_script([rows], name="wf", description="d", session_id=_SESSION, **REVIEW_KW, **kw)


def test_each_row_with_declared_paths_registers_a_commit_request():
    script = _script(expected_branch="work/run")

    assert "_runRow('C1', [], null, async () =>" in script
    assert "{ title: 'title-C1', paths: ['pkg/C1.py', 'pkg/tests/test_C1.py'] });" in script
    assert "{ title: 'title-C2', paths: ['pkg/C2.py', 'pkg/tests/test_C2.py'] });" in script


def test_row_without_declared_paths_registers_no_commit():
    script = _script([_write_row("C1", writes=[])], expected_branch="work/run")

    row_line = script.split("_runRow('C1'", 1)[1].split("\n", 1)[0]
    assert row_line.endswith(", null);")


def test_commit_leg_uses_the_sanctioned_route_and_never_raw_git_commit():
    script = _script(expected_branch="work/run")

    assert "coordinator-invoke\" ceremony.commit_v2" in script
    assert "Raw `git commit` is refused by the block-subagent-commit guard and is NOT a route." in script
    assert f"Dispatching Session-Id: {_SESSION}" in script
    assert "agentType: 'coordinator:git-commit-agent'" in script


def test_one_committer_per_wave_not_per_row():
    script = _script(expected_branch="work/run")

    assert script.count("agentType: 'coordinator:git-commit-agent'") == 1
    assert "_waveCommit(1, ['C1', 'C2']);" in script
    assert "label: 'commit:wave-' + n" in script
    assert "'checkpoint(wave ' + n + '): ' + done.length + ' rows" in script
    assert "const paths = [...new Set(done.flatMap((i) => _landed[i].paths))];" in script
    assert "_landed[id] = commit;" in script


def test_two_waves_register_two_wave_commits_chained_serially():
    script = compose_script(
        [[_write_row("C1")], [_write_row("C2", writes=["pkg/other.py"])]],
        name="wf", description="d", session_id=_SESSION, expected_branch="work/run", **REVIEW_KW,
    )

    assert "_waveCommit(1, ['C1']);" in script
    assert "_waveCommit(2, ['C2']);" in script
    assert "_commitChain = _commitChain.then(() => _commitWave(n, ids));" in script


def test_wave_commit_is_not_awaited_by_rows_or_later_waves():
    script = _script(expected_branch="work/run")

    run_row = script.split("async function _runRow", 1)[1].split("\n  }\n", 1)[0]
    assert "_commitChain" not in run_row and "_commitWave" not in run_row
    first_row = script.index("_rows['C1'] = _runRow(")
    assert script.index("_waveCommit(1,") > script.index("_rows['C2'] = _runRow(") > first_row
    execute = execute_section(script)
    assert execute.index("await Promise.all(_waveTriggers);") < execute.index("await _commitChain;")
    assert execute.index("await _commitChain;") < execute.index("await Promise.all(_checkpointPushes);")
    assert "if (_commitFailures.length) log(" in execute


def test_wave_with_no_done_row_emits_no_commit_leg():
    script = _script(expected_branch="work/run")

    assert "if (!done.length) return;" in script
    assert "const done = ids.filter((i) => _landed[i]);" in script
    no_paths = _script([_write_row("C1", writes=[])], expected_branch="work/run")
    assert "_waveCommit(1," not in no_paths


def test_checkpoint_push_targets_only_the_runs_own_branch_and_never_forces():
    script = _script(expected_branch="work/machine-b/2026-10-02")

    assert "git push origin HEAD:refs/heads/work/machine-b/2026-10-02" in script
    assert "Never pass --force or --force-with-lease" in script
    assert "refs/heads/main" not in script
    assert "_checkpointPushes.push(agent(" in script


def test_no_checkpoint_push_on_main_master_or_detached_head():
    for branch in ("main", "master", None):
        script = _script(expected_branch=branch)

        assert "_checkpointPushes.push(agent(" not in script
        assert "No checkpoint push: the run is not on a non-protected work branch" in script
        assert "agentType: 'coordinator:git-commit-agent'" in script


def test_row_over_the_path_threshold_is_left_to_the_terminal_commit():
    paths = [f"pkg/many_{i}.py" for i in range(_SHARED_PATH_ARRAY_THRESHOLD + 1)]
    script = _script([_write_row("C1", writes=paths)], expected_branch="work/run")

    assert "No checkpoint commit for rows declaring more than" in script
    assert "{ title: 'title-C1'" not in script
    assert "_waveCommit(1," not in script


def test_push_failures_are_collected_and_logged_after_pushes_settle():
    script = _script(expected_branch="work/run")

    assert ".catch(() => null)" not in script
    assert "_pushFailures.push(id + ': '" in script
    assert "label: 'checkpoint-push:' + id" in script
    execute = execute_section(script)
    settled = execute.index("await Promise.all(_checkpointPushes);")
    assert settled < execute.index("if (_pushFailures.length) log(")
    assert "slice(-300)" in script


def test_committer_passes_the_declared_list_verbatim_with_skip_missing():
    script = _script(expected_branch="work/run")

    assert "Pass the list VERBATIM as `paths` with `skip_missing` true" in script
    assert 'skip_missing' in script and 'true' in script
    assert "Do not run `git status` and do not filter" in script
    assert "git status --porcelain" not in script


def test_skip_missing_commits_existing_declared_paths_and_reports_the_missing(tmp_path):
    import subprocess

    from coordinator_core.ops.ceremony import commit_v2

    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=str(tmp_path), capture_output=True, text=True, check=True, **flags
        ).stdout

    git("init", "-q", "-b", "work/p")
    git("config", "user.email", "t@local")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "tracked.ts").write_text("a\n", encoding="utf-8", newline="\n")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    (tmp_path / "tracked.ts").write_text("b\n", encoding="utf-8", newline="\n")
    (tmp_path / "new_declared.ts").write_text("n\n", encoding="utf-8", newline="\n")
    declared = ["tracked.ts", "new_declared.ts", "never_written.ts"]

    refused = commit_v2._handler({"paths": declared, "message": "cp"}, repo_root=tmp_path / ".git")
    assert refused["committed"] is False

    out = commit_v2._handler(
        {"paths": declared, "skip_missing": True, "message": "cp"}, repo_root=tmp_path / ".git"
    )

    assert out["committed"] is True, out
    assert out["skipped_missing"] == ["never_written.ts"]
    landed = git("ls-tree", "-r", "--name-only", "HEAD").split()
    assert "new_declared.ts" in landed and "tracked.ts" in landed
    assert "never_written.ts" not in landed


def test_skip_missing_keeps_a_tracked_path_gone_from_disk_for_the_engine_to_refuse(tmp_path):
    import subprocess

    from coordinator_core.ops.ceremony import commit_v2

    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=str(tmp_path), capture_output=True, text=True, check=True, **flags
        ).stdout

    git("init", "-q", "-b", "work/p")
    git("config", "user.email", "t@local")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "t.ts").write_text("a\n", encoding="utf-8", newline="\n")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")
    (tmp_path / "t.ts").unlink()

    out = commit_v2._handler(
        {"paths": ["t.ts"], "skip_missing": True, "message": "cp"}, repo_root=tmp_path / ".git"
    )

    assert out["committed"] is False
    assert "gone from the worktree but still tracked" in out["error"]


def test_checkpoint_route_commits_modified_and_untracked_declared_paths_only(tmp_path):
    import subprocess

    from coordinator_core.ops.ceremony import commit_v2

    flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=str(tmp_path), capture_output=True, text=True, check=True, **flags
        ).stdout

    git("init", "-q", "-b", "work/p")
    git("config", "user.email", "t@local")
    git("config", "user.name", "t")
    git("config", "commit.gpgsign", "false")
    (tmp_path / "tracked.ts").write_text("a\n", encoding="utf-8", newline="\n")
    git("add", "-A")
    git("commit", "-q", "-m", "seed")

    (tmp_path / "tracked.ts").write_text("b\n", encoding="utf-8", newline="\n")
    (tmp_path / "new_declared.ts").write_text("n\n", encoding="utf-8", newline="\n")
    (tmp_path / "stray.ts").write_text("s\n", encoding="utf-8", newline="\n")

    declared = ["tracked.ts", "new_declared.ts"]
    status = git("status", "--porcelain", "-uall", "--", *declared)
    assert "?? new_declared.ts" in status and " M tracked.ts" in status
    out = commit_v2._handler(
        {
            "paths": declared,
            "message": "checkpoint(wave 1): 1 rows \u2014 C1\n\nCheckpoint-Plan: docs/plans/p.md\n"
            f"Checkpoint-Base: {git('rev-parse', 'HEAD').strip()}",
        },
        repo_root=tmp_path / ".git",
    )

    assert out["committed"] is True, out
    landed = git("ls-tree", "-r", "--name-only", "HEAD").split()
    assert "new_declared.ts" in landed and "tracked.ts" in landed
    assert "stray.ts" not in landed
    assert "?? stray.ts" in git("status", "--porcelain", "-uall")
