"""cold_chain: prefilter classes, real deny-class guards on a tmp repo, apply_patch drive-root deny, fail-open."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.host_hooks import cold_chain
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
                   **no_console_creationflags())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "seed.txt")
    _git(root, "commit", "-qm", "seed")
    return root


def _bash(command: str, cwd: Path, **extra) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd),
            "session_id": "cold-chain-test", **extra}


def _denied(verdict) -> bool:
    return isinstance(verdict, dict) and verdict.get("permissionDecision") == "deny" \
        and bool(verdict.get("permissionDecisionReason"))


@pytest.mark.spawns_process
def test_rm_of_uncommitted_work_is_denied_and_a_clean_target_is_not(repo: Path) -> None:
    work = repo / "work"
    work.mkdir()
    (work / "a.py").write_text("# uncommitted\n", encoding="utf-8")
    assert _denied(cold_chain.evaluate(_bash("rm -rf %s" % work, repo)))
    assert cold_chain.evaluate(_bash("rm %s" % (repo / "seed.txt"), repo)) == {}


@pytest.mark.spawns_process
def test_git_clean_that_would_remove_files_is_denied_and_dry_run_is_not(repo: Path) -> None:
    (repo / "state").mkdir()
    (repo / "state" / "work.md").write_text("work\n", encoding="utf-8")
    assert _denied(cold_chain.evaluate(_bash("git clean -fdx", repo)))
    assert cold_chain.evaluate(_bash("git clean -nd", repo)) == {}


def test_stash_drop_and_clear_are_denied_and_push_is_not(repo: Path) -> None:
    assert _denied(cold_chain.evaluate(_bash("git stash clear", repo)))
    assert _denied(cold_chain.evaluate(_bash("git stash drop stash@{0}", repo)))
    pushed = cold_chain.evaluate(_bash("git stash push -m x", repo))
    assert "block-stash-destruction" not in (pushed or {}).get("permissionDecisionReason", "")


@pytest.mark.spawns_process
def test_subagent_git_clean_is_denied_and_the_em_and_dry_run_are_not(repo: Path) -> None:
    (repo / "untracked.txt").write_text("u", encoding="utf-8")
    sub = cold_chain.evaluate(_bash("git clean -f untracked.txt", repo, agent_id="sub-1"))
    em = cold_chain.evaluate(_bash("git clean -f untracked.txt", repo))
    assert _denied(sub)
    assert _denied(em) and "destructive-git-clean" in em["permissionDecisionReason"]
    assert not _denied(cold_chain.evaluate(_bash("git clean -n", repo, agent_id="sub-1")))


def test_rm_and_git_clean_walk_the_work_tree_without_spawning(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    def no_spawn(*args, **kwargs):
        raise AssertionError("the cold rm / git clean route spawned a process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    (repo / "work").mkdir()
    (repo / "work" / "a.py").write_text("# uncommitted\n", encoding="utf-8")
    (repo / "node_modules").mkdir()
    assert _denied(cold_chain.evaluate(_bash("rm -rf work", repo)))
    assert _denied(cold_chain.evaluate(_bash("git clean -fdx", repo)))
    assert cold_chain.evaluate(_bash("rm -rf node_modules", repo)) == {}
    assert cold_chain.evaluate(_bash("git clean -nd", repo)) == {}


def test_cold_never_denies_a_target_the_warm_rm_exempts_by_name(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    import tempfile

    from coordinator_core.bash_guards import _dc_rm

    assert _dc_rm._resolves_under_repo_scratch(str(repo / "scratch" / "run1"))
    (repo / "scratch" / "run1").mkdir(parents=True)
    assert cold_chain.evaluate(_bash("rm -rf scratch/run1", repo)) == {}
    assert cold_chain.evaluate(_bash("git clean -fdx scratch/run1", repo)) == {}
    assert cold_chain.evaluate(_bash("rm -rf %s" % (Path(tempfile.gettempdir()) / "cold-parity-probe"), repo)) == {}


@pytest.mark.spawns_process
@pytest.mark.parametrize("command", ["rm -rf work", "rm -rf clean_tracked", "git clean -fdx", "git clean -nd"])
def test_cold_rm_and_clean_deny_wherever_the_real_guard_denies(repo: Path, command: str) -> None:
    from coordinator_core.bash_guards._dc_git_misc import check_destructive_git_clean
    from coordinator_core.bash_guards._dc_rm import check_destructive_rm

    (repo / "work").mkdir()
    (repo / "work" / "a.py").write_text("# uncommitted\n", encoding="utf-8")
    (repo / "clean_tracked").mkdir()
    (repo / "clean_tracked" / "b.py").write_text("# tracked\n", encoding="utf-8")
    _git(repo, "add", "clean_tracked")
    _git(repo, "commit", "-qm", "tracked")
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo), "session_id": "cold-chain-test"}
    real = check_destructive_rm(command, "cold-chain-test", payload) if command.startswith("rm") \
        else check_destructive_git_clean(command, "cold-chain-test", payload)
    cold = _denied(cold_chain.evaluate(_bash(command, repo)))
    if command.startswith("rm"):
        # Both ways: a clean tracked target is recoverable, so the warm guard allows it and cold must too.
        assert cold == (real is not None), command
    elif real is not None:
        # git clean stays one-way: telling an empty clean from a destructive one needs gitignore evaluation.
        assert cold, command


def test_untracked_state_clean_denies_cold_in_a_fresh_process_under_the_bar(repo: Path) -> None:
    import json
    import sys

    (repo / "state").mkdir()
    (repo / "state" / "x.md").write_text("work\n", encoding="utf-8")
    code = (
        "import json,sys,time,subprocess\n"
        "def no_spawn(*a, **k): raise SystemExit('spawned')\n"
        "subprocess.Popen = no_spawn\n"
        "import coordinator_core.host_hooks.cold_chain as c\n"
        "r=c.evaluate({'tool_name':'Bash','tool_input':{'command':'git clean -fd'},'cwd':sys.argv[1],'session_id':'s'})\n"
        "print(json.dumps([r,c.take_trace(),time.process_time()]))\n"
    )
    runs = [json.loads(subprocess.run([sys.executable, "-c", code, str(repo)], capture_output=True, text=True, check=True,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout) for _ in range(3)]
    assert all(_denied(r[0]) and r[1][0] == ["destructive-git-clean", "deny"] for r in runs), runs
    assert min(r[2] for r in runs) < 0.2, runs


def test_cold_deny_remedy_names_the_warm_engine(repo: Path) -> None:
    (repo / "work").mkdir()
    (repo / "work" / "a.py").write_text("# uncommitted\n", encoding="utf-8")
    for command in ("rm -rf work", "git clean -fdx"):
        assert "warm engine" in cold_chain.evaluate(_bash(command, repo))["permissionDecisionReason"]


def test_trace_records_each_guard_outcome_in_run_order_and_clears(repo: Path) -> None:
    cold_chain.evaluate(_bash("git stash list; cp a b", repo))
    trace = cold_chain.take_trace()
    assert [t for t in trace if t[0] in ("block-stash-destruction", "guard-disk-fill")] == [
        ("block-stash-destruction", "allow"), ("guard-disk-fill", "allow")]
    assert [name for name, _ in trace] == [g for g in cold_chain.shell_candidates("git stash list; cp a b")]
    assert cold_chain.take_trace() == []


def test_trace_marks_a_deny_and_stops_at_it(repo: Path) -> None:
    verdict = cold_chain.evaluate(_bash("git stash clear; cp a b", repo))
    assert _denied(verdict)
    assert cold_chain.take_trace() == [("block-stash-destruction", "deny")]


def test_trace_marks_a_raising_guard(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(cold_chain, "_invoke_shell", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    cold_chain.evaluate(_bash("git stash list", repo))
    trace = cold_chain.take_trace()
    assert trace[0] == ("block-stash-destruction", "raised")
    assert {verdict for _, verdict in trace} == {"raised"}


def test_a_harmless_command_leaves_an_empty_trace(repo: Path) -> None:
    cold_chain.evaluate(_bash("ls", repo))
    assert cold_chain.take_trace() == []


@pytest.mark.parametrize("command", ["git status", "python run.py", "git log --oneline -3"])
def test_widened_prefilter_commands_run_cold_in_a_fresh_process_under_the_bar(repo: Path, command: str) -> None:
    import json
    import sys

    code = (
        "import json,sys,time\n"
        "import coordinator_core.host_hooks.cold_chain as c\n"
        "r=c.evaluate({'tool_name':'Bash','tool_input':{'command':sys.argv[2]},'cwd':sys.argv[1],'session_id':'s'})\n"
        "print(json.dumps([r,[t[0] for t in c.take_trace()],time.process_time()]))\n"
    )
    runs = [json.loads(subprocess.run([sys.executable, "-c", code, str(repo), command], capture_output=True, text=True,
                                      check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout)
            for _ in range(3)]
    assert all(r[0] == {} and r[1] for r in runs), runs
    assert min(r[2] for r in runs) < 0.2, runs
