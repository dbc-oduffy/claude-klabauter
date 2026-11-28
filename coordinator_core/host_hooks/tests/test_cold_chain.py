"""cold_chain: prefilter classes, real deny-class guards on a tmp repo, apply_patch drive-root deny, fail-open."""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.host_hooks import cold_chain


def _bash(command: str, cwd: Path, **extra) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd),
            "session_id": "cold-chain-test", **extra}


def _denied(verdict) -> bool:
    return isinstance(verdict, dict) and verdict.get("permissionDecision") == "deny" \
        and bool(verdict.get("permissionDecisionReason"))


EXTERNAL_WS = "block-external-workspace-vcs-write"
REVERT = "destructive-git-revert"


@pytest.mark.parametrize("command,expected", [
    ("ls", []),
    ("git status", [EXTERNAL_WS]),
    ("npm run format && echo confirm", []),
    ("git commit -m 'update notes'", [EXTERNAL_WS]),
    ("rm -rf build", ["destructive-rm"]),
    ("'r''m' -rf build", ["destructive-rm"]),
    ('sh -c "rm -rf build"', ["destructive-rm"]),
    ("cd x && /usr/bin/git clean -fdx", ["destructive-git-clean", EXTERNAL_WS]),
    ("git clean --force -d", ["destructive-git-clean", EXTERNAL_WS]),
    ("git clean -n", [EXTERNAL_WS]),
    ("git clean", [EXTERNAL_WS]),
    ("git -C x stash clear", ["block-stash-destruction", EXTERNAL_WS, REVERT]),
    ("Remove-Item -Recurse -Force build", ["destructive-rm"]),
    ("dd if=/dev/zero of=out bs=1M count=10", ["guard-disk-fill"]),
    ("truncate -s 10G big.bin", ["guard-disk-fill"]),
    ("git worktree add ../x", ["block-worktree-creation", EXTERNAL_WS]),
    ("find / -name x", ["block-whole-filesystem-scan", "runaway-find"]),
    ("pytest -p xdist", ["guard-uncapped-pytest-plugin"]),
    ("python run.py", ["block-editor-kill-by-name", "deny-kill-by-port-or-tree"]),
])
def test_prefilter_names_the_classes_a_command_could_match(command: str, expected: list) -> None:
    assert cold_chain.shell_candidates(command) == expected


DAY_BRANCH = "tester/box/2026-11-22"
BRANCH = cold_chain.BRANCH_GUARD


@pytest.mark.parametrize("command", [
    "git checkout -b %s" % DAY_BRANCH, "git switch -c %s" % DAY_BRANCH, "git checkout -B x/y/2026-11-22",
    "git switch --create probe/x", "git branch probe/x", "cd r && git -C r checkout -b probe/x",
])
def test_branch_create_names_the_branch_guard(command: str) -> None:
    assert cold_chain.shell_candidates(command)[0] == BRANCH


@pytest.mark.parametrize("command", ["git checkout main", "git switch main", "git branch", "git branch -d x", "git branch --list",
    "git branch -m old new", "git log branch", "git diff branch",
])
def test_non_create_git_commands_are_decided_by_the_guard_not_the_prefilter(command: str, tmp_path: Path) -> None:
    assert cold_chain.evaluate(_bash(command, tmp_path)) == {}
    assert cold_chain.take_trace()[0] == (BRANCH, "allow")


@pytest.mark.parametrize("command", ["git status", "git commit -m x", "ls", "git config branch.main.remote origin"])
def test_commands_without_a_branch_verb_do_not_name_the_branch_guard(command: str) -> None:
    assert BRANCH not in cold_chain.shell_candidates(command)


def test_branch_guard_cold_process_time_stays_under_the_bar() -> None:
    import json
    import subprocess
    import sys

    code = (
        "import json,time,coordinator_core.host_hooks.cold_chain as c\n"
        "r=c.evaluate({'tool_name':'Bash','tool_input':{'command':'git checkout -b x/y/2026-11-22'},'cwd':'.','session_id':'s'})\n"
        "print(json.dumps([r,c.take_trace(),time.process_time()]))\n"
    )
    runs = []
    for _ in range(3):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        runs.append(json.loads(out))
    assert all(r[0] == {} and r[1][0][0] == BRANCH for r in runs)
    assert min(r[2] for r in runs) < 0.2, runs


def test_day_branch_create_is_an_evaluated_allow_in_the_trace(tmp_path: Path) -> None:
    for command in ("git checkout -b %s" % DAY_BRANCH, "git switch -c %s" % DAY_BRANCH):
        assert cold_chain.evaluate(_bash(command, tmp_path)) == {}
        assert cold_chain.take_trace()[0] == (BRANCH, "allow")


def test_topic_branch_create_is_traced_advisory_and_never_denied(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from coordinator_core.bash_guards import block_noncanonical_branch_creation as guard

    monkeypatch.setattr(guard, "resolve_git_root", lambda cwd=None: str(tmp_path))
    monkeypatch.setattr(guard, "_is_hazard_repo", lambda git_root: True)
    assert guard.check(_bash("git checkout -b probe/x", tmp_path)) is not None
    for command in ("git checkout -b probe/x", "git switch -c probe/x", "git branch probe/x"):
        assert cold_chain.evaluate(_bash(command, tmp_path)) == {}
        assert cold_chain.take_trace()[0] == (BRANCH, "advisory")
    assert cold_chain.evaluate(_bash("git checkout -b %s" % DAY_BRANCH, tmp_path)) == {}
    assert cold_chain.take_trace()[0] == (BRANCH, "allow")


@pytest.mark.parametrize("command", ["git checkout -b %s" % DAY_BRANCH, "git switch -c probe/x"])
def test_branch_guard_evaluation_spawns_no_process_and_restores_its_root_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, command: str,
) -> None:
    import subprocess

    from coordinator_core.bash_guards import block_noncanonical_branch_creation as guard

    def no_spawn(*args, **kwargs):
        raise AssertionError("the cold branch-guard evaluation spawned a process")

    before = guard.resolve_git_root
    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    assert cold_chain.evaluate(_bash(command, tmp_path)) == {}
    assert cold_chain.take_trace()[0][0] == BRANCH
    assert guard.resolve_git_root is before


def test_branch_guard_never_joins_the_deny_set() -> None:
    assert BRANCH not in cold_chain.SHELL_DENY_GUARDS and BRANCH in cold_chain.ADVISORY_SHELL_GUARDS


def test_harmless_command_imports_and_runs_no_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("a guard ran for a harmless command")

    monkeypatch.setattr(cold_chain, "_invoke_shell", boom)
    assert cold_chain.evaluate(_bash("ls", tmp_path)) == {}
    assert cold_chain.evaluate(_bash("echo hi", tmp_path)) == {}


def test_disk_fill_over_the_floor_is_denied_and_a_small_write_is_not(tmp_path: Path) -> None:
    assert _denied(cold_chain.evaluate(_bash("truncate -s 900000000G %s" % (tmp_path / "big.bin"), tmp_path)))
    assert cold_chain.evaluate(_bash("truncate -s 1K %s" % (tmp_path / "small.bin"), tmp_path)) == {}


def _patch(path: Path, kind: str = "Add") -> str:
    body = "+x\n" if kind == "Add" else "@@\n-a\n+b\n"
    return "*** Begin Patch\n*** %s File: %s\n%s*** End Patch" % (kind, path, body)


def _patch_event(path: Path, cwd: Path, kind: str = "Add", **extra) -> dict:
    return {"tool_name": "apply_patch", "tool_input": {"command": _patch(path, kind)}, "cwd": str(cwd),
            "session_id": "cold-chain-test", **extra}


def test_apply_patch_adding_a_drive_root_directory_is_denied_for_a_subagent(tmp_path: Path) -> None:
    target = Path(tmp_path.anchor) / "zz-cold-chain-new-dir" / "f.txt"
    assert _denied(cold_chain.evaluate(_patch_event(target, tmp_path, agent_id="sub-1")))


def test_apply_patch_inside_the_repo_or_by_the_main_session_is_allowed(tmp_path: Path) -> None:
    root_target = Path(tmp_path.anchor) / "zz-cold-chain-new-dir" / "f.txt"
    assert cold_chain.evaluate(_patch_event(tmp_path / "scratch" / "f.txt", tmp_path, agent_id="sub-1")) == {}
    assert cold_chain.evaluate(_patch_event(root_target, tmp_path)) == {}


def test_apply_patch_update_never_reaches_the_write_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import coordinator_core.host_hooks.codex_tools as codex_tools

    def boom(payload):
        raise AssertionError("an Update-only patch was projected")

    monkeypatch.setattr(codex_tools, "patch_events", boom)
    assert cold_chain.evaluate(_patch_event(tmp_path / "a.py", tmp_path, kind="Update")) == {}


def test_a_raising_guard_is_skipped_and_an_all_raising_chain_did_not_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*args, **kwargs):
        raise RuntimeError("guard exploded")

    monkeypatch.setattr(cold_chain, "_invoke_shell", boom)
    assert cold_chain.evaluate(_bash("rm -rf x", tmp_path)) is None


def test_other_tools_are_not_the_cold_chains_to_answer(tmp_path: Path) -> None:
    assert cold_chain.evaluate({"tool_name": "Edit", "tool_input": {"file_path": "a"}, "cwd": str(tmp_path)}) is None
    assert cold_chain.evaluate({"tool_name": "Bash", "tool_input": {}, "cwd": str(tmp_path)}) == {}


def test_every_guard_the_chain_can_deny_for_is_box_degrading_in_the_registry() -> None:
    from coordinator_core.deny_registry import BOX_DEGRADING, GUARD_DENY_CLASS

    assert cold_chain.SUBAGENT_DENY_GUARD in cold_chain.SHELL_DENY_GUARDS
    for guard in cold_chain.SHELL_DENY_GUARDS + cold_chain.PATCH_DENY_GUARDS:
        assert GUARD_DENY_CLASS[guard] == BOX_DEGRADING, guard


def test_subagent_draws_the_subagent_guard_only_for_git_commands() -> None:
    assert cold_chain.shell_candidates("git clean -n", subagent=True)[0] == "block-subagent-destructive-action"
    assert cold_chain.SUBAGENT_DENY_GUARD not in cold_chain.shell_candidates("git clean -n")
    assert cold_chain.shell_candidates("ls", subagent=True) == []


def test_apply_patch_runs_every_guard_in_the_write_table_and_denies_from_any(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import types

    ran = []

    def fake(name: str, deny: bool):
        def check(event):
            ran.append(name)
            return {"permissionDecision": "deny", "permissionDecisionReason": "no"} if deny else None
        return types.SimpleNamespace(check=check)

    table = {"guard-a": fake("guard-a", False), "guard-b": fake("guard-b", True)}
    monkeypatch.setattr(cold_chain, "PATCH_DENY_GUARDS", tuple(table))
    monkeypatch.setattr(cold_chain, "_guard_module", lambda name: table[name])
    verdict = cold_chain.evaluate(_patch_event(tmp_path / "f.txt", tmp_path))
    assert _denied(verdict) and "guard-b" in verdict["permissionDecisionReason"]
    assert ran == ["guard-a", "guard-b"]


def test_apply_patch_with_every_write_guard_unimportable_did_not_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def gone(name):
        raise ImportError(name)

    monkeypatch.setattr(cold_chain, "_guard_module", gone)
    assert cold_chain.evaluate(_patch_event(tmp_path / "f.txt", tmp_path)) is None


def _fake_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("command", ["git clean -fd", "git clean -fdx"])
def test_forced_clean_over_an_untracked_load_bearing_path_is_denied_even_under_the_tmp_dir(command: str, tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    (repo / "state").mkdir()
    (repo / "state" / "x.md").write_text("work\n", encoding="utf-8")
    verdict = cold_chain.evaluate(_bash(command, repo))
    assert _denied(verdict) and "destructive-git-clean" in verdict["permissionDecisionReason"]
    assert "state" in verdict["permissionDecisionReason"]
    assert cold_chain.take_trace()[0] == ("destructive-git-clean", "deny")


def test_a_dry_run_clean_over_an_untracked_load_bearing_path_is_allowed(tmp_path: Path) -> None:
    repo = _fake_repo(tmp_path)
    (repo / "state").mkdir()
    (repo / "state" / "x.md").write_text("work\n", encoding="utf-8")
    assert cold_chain.evaluate(_bash("git clean -nd", repo)) == {}


def test_table_guards_deny_their_command_shape_without_a_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    for command, guard in (("find / -name x", "block-whole-filesystem-scan"), ("git worktree add ../x", "block-worktree-creation")):
        verdict = cold_chain.evaluate(_bash(command, tmp_path))
        assert _denied(verdict) and guard in verdict["permissionDecisionReason"], command


def test_runaway_find_denies_an_unbounded_root_find_and_allows_a_local_one(tmp_path: Path) -> None:
    payload = _bash("find ~ -name x", tmp_path)
    assert cold_chain._invoke_shell("runaway-find", payload, "find ~ -name x") is not None
    assert cold_chain._invoke_shell("runaway-find", _bash("find . -name x", tmp_path), "find . -name x") is None


def test_a_guard_that_spawns_is_skipped_and_the_popen_patch_is_restored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import subprocess

    from coordinator_core.bash_guards import block_worktree_creation as guard

    def spawning(payload):
        subprocess.Popen(["git", "--version"])
        return {"permissionDecision": "deny", "permissionDecisionReason": "must not be reached"}

    before = subprocess.Popen
    monkeypatch.setattr(guard, "check", spawning)
    assert cold_chain.evaluate(_bash("git worktree add ../x", tmp_path)) == {}
    assert ("block-worktree-creation", "raised") in cold_chain.take_trace()
    assert subprocess.Popen is before


def test_every_table_guard_has_a_callable_prefilter_and_invoker_and_a_unique_name() -> None:
    for guard in cold_chain.SHELL_GUARDS:
        assert callable(guard.wanted) and callable(guard.invoke), guard.name
    assert len({g.name for g in cold_chain.SHELL_GUARDS}) == len(cold_chain.SHELL_GUARDS)


def test_the_table_accounts_for_every_irreversible_registry_guard() -> None:
    from coordinator_core.deny_registry import GUARD_DENY_CLASS, IRREVERSIBLE_CLASSES
    from coordinator_core.host_hooks import backstop

    not_bash_dispatch = {"block-worktree-tool", "block-dispatch-suite-invocation"}
    registry = {g for g, cls in GUARD_DENY_CLASS.items() if cls in IRREVERSIBLE_CLASSES}
    table, absent = set(cold_chain.SHELL_DENY_GUARDS), set(cold_chain.NO_COLD_FORM)
    assert not table & absent
    assert set(cold_chain.COLD_PARTIAL) <= table
    covered = table | absent | set(backstop.GUARD_MODULES) | set(cold_chain.PATCH_DENY_GUARDS) | not_bash_dispatch
    assert registry - covered == set()
    assert table | absent <= registry
    assert all(reason for reason in cold_chain.NO_COLD_FORM.values())


def test_apply_patch_reaches_every_deny_class_write_guard() -> None:
    import coordinator_core.write_guards as write_guards
    from coordinator_core.deny_registry import GUARD_DENY_CLASS, IRREVERSIBLE_CLASSES

    modules = {p.stem.replace("_", "-") for p in Path(write_guards.__file__).parent.glob("*.py")}
    denying = {g for g, cls in GUARD_DENY_CLASS.items() if g in modules and cls in IRREVERSIBLE_CLASSES}
    assert denying == set(cold_chain.PATCH_DENY_GUARDS)
