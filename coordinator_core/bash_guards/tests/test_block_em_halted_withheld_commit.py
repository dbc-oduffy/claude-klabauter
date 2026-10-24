"""block_em_halted_withheld_commit denies an EM commit naming a halted run's withheld path."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from coordinator_core.bash_guards import block_em_halted_withheld_commit as guard
from coordinator_core.guard_messages.testing import envelope_of

RUN = "ask-20261024T000000-aaaaaa"
L_R47 = "git add a.py DELIVERY.md && git commit -F scratch/commitmsg.txt -- a.py DELIVERY.md"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    index = tmp_path / ".coordinator-local" / "halt-withheld"
    index.mkdir(parents=True)
    (index / ".built").write_bytes(b"")
    (index / f"{RUN}.json").write_text(json.dumps({"paths": ["a.py", "DELIVERY.md", "d/x.py"]}))
    for name in ("a.py", "DELIVERY.md", "b.py"):
        (tmp_path / name).write_text("x")
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x.py").write_text("x")
    return tmp_path


def _payload(repo, cmd, tool="Bash", **extra):
    p = {"tool_name": tool, "tool_input": {"command": cmd}, "session_id": "s1", "cwd": str(repo)}
    p.update(extra)
    return p


def _verdict(repo, cmd, **kw):
    return envelope_of(guard.check(_payload(repo, cmd, **kw)))


def _denied(result):
    return result is not None and result["hookSpecificOutput"]["permissionDecision"] == "deny"


def _text(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.fixture
def spawns(monkeypatch):
    calls = []
    out = {"cached": [], "tracked": []}

    from coordinator_core.git import run as git_run

    def fake_run(argv, **kwargs):
        calls.append(argv)
        key = "cached" if "--cached" in argv else "tracked"
        return SimpleNamespace(returncode=0, stdout="\n".join(out[key]))

    def fake_run_git(args, **kwargs):
        return fake_run(["git", *args], **kwargs)

    monkeypatch.setattr("coordinator_core.git.run.run_git", fake_run_git)
    return SimpleNamespace(calls=calls, out=out)


def test_l_r47_command_verbatim_is_denied_with_run_id_and_alternative(repo, spawns):
    result = _verdict(repo, L_R47)
    assert _denied(result)
    text = _text(result)
    assert RUN in text and "DELIVERY.md" in text
    assert "re-fire" in text and "workflow.revise" in text
    assert spawns.calls == []


def test_pathspec_after_double_dash_denied(repo, spawns):
    assert _denied(_verdict(repo, "git commit -m x -- a.py"))


def test_positional_pathspec_denied_and_message_value_not_taken_as_path(repo, spawns):
    assert _denied(_verdict(repo, "git commit -m 'msg' a.py"))
    assert _verdict(repo, "git commit -m a.py -- b.py") is None


def test_unrelated_pathspec_passes(repo, spawns):
    assert _verdict(repo, "git commit -m x -- b.py") is None
    assert spawns.calls == []


def test_directory_pathspec_covering_withheld_denied(repo, spawns):
    assert _denied(_verdict(repo, "git commit -m x -- d"))


def test_git_dash_c_and_absolute_path_denied(repo, spawns):
    assert _denied(_verdict(repo, f"git -C {repo} commit -m x -- a.py"))
    assert _denied(_verdict(repo, f"git commit -m x -- {repo}/a.py"))


def test_add_then_bare_commit_in_one_command_denied_without_spawn(repo, spawns):
    assert _denied(_verdict(repo, "git add a.py && git commit -m x"))
    assert spawns.calls == []


def test_add_of_unrelated_path_then_bare_commit_falls_to_cached_diff(repo, spawns):
    assert _verdict(repo, "git add b.py && git commit -m x") is None
    assert len(spawns.calls) == 1
    assert "--cached" in spawns.calls[0]


def test_bare_commit_with_withheld_path_staged_earlier_denied_via_one_diff(repo, spawns):
    spawns.out["cached"] = ["a.py"]
    assert _denied(_verdict(repo, "git commit -m x"))
    assert len(spawns.calls) == 1


def test_bare_commit_with_clean_index_passes(repo, spawns):
    spawns.out["cached"] = ["b.py"]
    assert _verdict(repo, "git commit -m x") is None


def test_commit_a_spawns_at_most_two_diffs(repo, spawns):
    spawns.out["tracked"] = ["a.py"]
    assert _denied(_verdict(repo, "git commit -am x"))
    assert len(spawns.calls) == 2
    spawns.calls.clear()
    spawns.out["tracked"] = []
    assert _verdict(repo, "git commit -a -m x") is None
    assert len(spawns.calls) == 2


def test_add_all_then_bare_commit_denied(repo, spawns):
    assert _denied(_verdict(repo, "git add -A && git commit -m x"))
    assert _denied(_verdict(repo, "git add . && git commit -m x"))


def test_cd_then_commit_resolves_against_the_new_directory(repo, spawns):
    assert _denied(_verdict(repo, "cd d && git commit -m x -- x.py"))
    assert _verdict(repo, "cd d && git commit -m x -- a.py") is None


def test_sh_c_wrapped_commit_denied(repo, spawns):
    assert _denied(_verdict(repo, 'bash -c "git commit -m x -- a.py"'))


def test_heredoc_message_body_does_not_confuse_the_scan(repo, spawns):
    cmd = "git commit -F - -- b.py <<'EOF'\ngit commit -- a.py\nEOF"
    assert _verdict(repo, cmd) is None


def test_powershell_start_process_leg_denied(repo, spawns):
    cmd = "Start-Process git -ArgumentList 'commit','-m','x','--','a.py'"
    result = _verdict(repo, cmd, tool="PowerShell")
    assert _denied(result)


def test_powershell_plain_git_commit_denied(repo, spawns):
    assert _denied(_verdict(repo, "git commit -m x -- a.py", tool="PowerShell"))


def test_windows_separators_in_pathspec_denied(repo, spawns):
    assert _denied(_verdict(repo, "git commit -m x -- d\\x.py"))


@pytest.mark.parametrize(
    "extra",
    [
        {"agent_id": "deadbeef0123"},
        {"agent_type": "coordinator:git-commit-agent"},
        {"agent_type": "coordinator:executor"},
        {"transcript_path": "/h/projects/p/sess/subagents/agent-a1.jsonl"},
    ],
)
def test_subagent_identities_are_not_denied_by_this_guard(repo, spawns, extra):
    assert _verdict(repo, L_R47, **extra) is None
    assert spawns.calls == []


def test_empty_index_spawns_no_git_and_allows_bare_commit(repo, spawns):
    (repo / ".coordinator-local" / "halt-withheld" / f"{RUN}.json").unlink()
    assert _verdict(repo, "git commit -m x") is None
    assert _verdict(repo, "git commit -am x") is None
    assert _verdict(repo, L_R47) is None
    assert spawns.calls == []


def test_released_run_leaves_the_index_and_commit_passes(repo, spawns):
    from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx

    assert _denied(_verdict(repo, "git commit -m x -- a.py"))
    idx.remove_run(repo, RUN)
    assert _verdict(repo, "git commit -m x -- a.py") is None


def test_non_commit_and_other_tools_untouched(repo, spawns):
    assert _verdict(repo, "git status && echo commit") is None
    assert _verdict(repo, "git log --oneline -- a.py") is None
    assert guard.check({"tool_name": "Read", "tool_input": {"command": L_R47}}) is None
    assert spawns.calls == []


def test_outside_a_git_repo_passes(tmp_path, spawns):
    assert _verdict(tmp_path, "git commit -m x -- a.py") is None


def test_message_is_one_fact_plus_alternative_without_override(repo, spawns):
    text = _text(_verdict(repo, "git commit -m x -- a.py"))
    assert f"workflow.revise {RUN}" in text
    assert "override" not in text.lower() and "sorry" not in text.lower()


@pytest.mark.spawns_process
def test_merge_commit_exempts_landed_blobs_but_denies_a_path_matching_neither_side(repo):
    from coordinator_core.win_portability import no_console_creationflags

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=repo, check=True, capture_output=True, **no_console_creationflags(),
        )

    git("init", "-q", "-b", "main")
    git("add", "a.py", "b.py", "DELIVERY.md", "d")
    git("commit", "-q", "-m", "base")
    git("checkout", "-q", "-b", "side")
    (repo / "a.py").write_text("landed")
    git("commit", "-q", "-am", "side")
    git("checkout", "-q", "main")
    (repo / "b.py").write_text("main")
    git("commit", "-q", "-am", "main")
    git("merge", "--no-ff", "--no-commit", "side")

    assert _verdict(repo, "git commit -m merge") is None

    (repo / "a.py").write_text("run dirt")
    git("add", "a.py")
    assert _denied(_verdict(repo, "git commit -m merge"))


@pytest.mark.spawns_process
def test_real_git_staged_withheld_path_denied_then_clean_passes(repo):
    from coordinator_core.win_portability import no_console_creationflags

    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, **no_console_creationflags())

    git("init", "-q")
    git("add", "a.py")
    assert _denied(_verdict(repo, "git commit -m x"))
    git("reset", "-q")
    git("add", "b.py")
    assert _verdict(repo, "git commit -m x") is None
