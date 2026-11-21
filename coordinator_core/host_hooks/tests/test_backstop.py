"""Backstop: guard set pinned to the registry, deny/allow on push remotes, no warm/ops import."""
from __future__ import annotations

import subprocess

import pytest

from coordinator_core import deny_registry
from coordinator_core.bash_guards import block_push_pr_non_owned_remote as push_guard
from coordinator_core.host_hooks import backstop
from coordinator_core.host_hooks.contract import HostEvent
from pathlib import Path


def _event(command, tool_name: str = "Bash", cwd: str = "/nonexistent-backstop-cwd") -> HostEvent:
    return HostEvent(
        host="codex", event="PreToolUse", session_id="s1",
        tool_name=tool_name, tool_input={"command": command}, cwd=cwd,
    )


def test_guard_set_equals_irreversible_external_registry_entries() -> None:
    expected = {
        g for g, c in deny_registry.GUARD_DENY_CLASS.items()
        if c == deny_registry.IRREVERSIBLE_EXTERNAL
    }
    assert set(backstop.GUARD_MODULES) == expected


def test_push_to_non_owned_remote_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    verdict = backstop.match(_event("git push https://github.com/stranger/repo.git main"))
    assert verdict is not None
    assert verdict["decision"] == "deny"
    assert verdict["reason"]


def test_push_to_owned_remote_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    assert backstop.match(_event("git push https://github.com/mine/repo.git main")) is None


def test_trace_names_each_guard_run_and_the_denier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    backstop.take_trace()
    assert backstop.match(_event("git push https://github.com/stranger/repo.git main")) is not None
    trace = backstop.take_trace()
    assert trace[0] == ("block-push-pr-non-owned-remote", "deny")
    assert backstop.take_trace() == []
    assert backstop.match(_event("ls")) is None
    allowed = backstop.take_trace()
    assert [g for g, _ in allowed][:len(backstop.GUARD_MODULES)] == list(backstop.GUARD_MODULES)
    assert {v for _, v in allowed} == {"allow"}


def test_non_command_tool_input_is_ignored() -> None:
    event = HostEvent(host="agy", event="PreToolUse", session_id="s", tool_name="view_file",
                      tool_input={"path": "x"})
    assert backstop.match(event) is None


def test_agy_commandline_shape_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(push_guard, "_configured_prefixes", lambda: ["github.com/stranger/"])
    event = HostEvent(host="agy", event="PreToolUse", session_id="s", tool_name="run_command",
                      tool_input={"CommandLine": "git push https://github.com/stranger/r.git main"})
    assert backstop.match(event)["decision"] == "deny"


def test_raising_guard_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_payload):
        raise RuntimeError("guard broke")

    monkeypatch.setattr(push_guard, "check", boom)
    assert backstop.match(_event("git push https://github.com/stranger/r.git main")) is None


@pytest.mark.parametrize("command", [
    "git push --force origin main",
    "git push -f origin main",
    "git push origin +main",
    "git push origin +HEAD:refs/heads/main",
    "git push --force-with-lease origin master",
    "git -C sub push --force origin main",
    "git add . && git push origin main --force",
    "git push --force --all",
])
def test_force_push_to_main_denied(command) -> None:
    verdict = backstop.match(_event(command))
    assert verdict is not None and verdict["decision"] == "deny"
    assert "force-pushes main" in verdict["reason"]


@pytest.mark.parametrize("command", [
    "git status",
    "git push origin main",
    "git push --force origin topic/x",
    "git push origin +topic/x",
    "git stash push -f -- file",
    "git commit -m 'never git push --force origin main'",
    "echo git push --force origin main",
])
def test_non_main_or_non_force_push_not_denied(command) -> None:
    assert backstop.match(_event(command)) is None


def _repo_on(tmp_path, branch) -> str:
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/%s\n" % branch, encoding="utf-8")
    return str(tmp_path)


@pytest.mark.parametrize("command", ["git push --force", "git push -f origin HEAD"])
def test_bare_force_push_resolves_the_current_branch(tmp_path: Path, command) -> None:
    assert backstop.match(_event(command, cwd=_repo_on(tmp_path, "main")))["decision"] == "deny"


@pytest.mark.parametrize("command", ["git push --force", "git push -f origin HEAD"])
def test_bare_force_push_on_a_topic_branch_passes(tmp_path: Path, command) -> None:
    assert backstop.match(_event(command, cwd=_repo_on(tmp_path, "topic"))) is None


def test_agy_force_push_to_main_denied() -> None:
    event = HostEvent(host="agy", event="PreToolUse", session_id="s", tool_name="run_command",
                      tool_input={"CommandLine": "git push --force origin main"})
    assert backstop.match(event)["decision"] == "deny"


def _degrade(command, cwd=""):
    return backstop.degrade_match(_event(command, cwd=cwd))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "scratch").mkdir()
    return tmp_path


@pytest.mark.parametrize("command", [
    "rm -rf src", "rm -r docs/old", "rm -fr ./builds", "rm --recursive coordinator_core",
    "cd x && rm -rf src", "Remove-Item -Recurse -Force src", "rm -rf scratch/../src",
])
def test_degrade_denies_recursive_rm_outside_scratch(repo: Path, command) -> None:
    for name in ("src", "docs/old", "builds", "coordinator_core"):
        (repo / name).mkdir(parents=True, exist_ok=True)
        (repo / name / "untracked.py").write_text("x\n", encoding="utf-8")
    verdict = _degrade(command, cwd=str(repo))
    assert verdict is not None and verdict["decision"] == "deny"


def _tracked_repo(root: Path) -> Path:
    """A real repo whose `tracked/` dir, `top.txt` and a symlink-free tree are committed and clean."""
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    root.mkdir()
    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (root / "tracked" / "sub").mkdir(parents=True)
    (root / "tracked" / "a.py").write_text("a\n", encoding="utf-8")
    (root / "tracked" / "sub" / "b.py").write_text("b\n", encoding="utf-8")
    (root / "top.txt").write_text("top\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-qm", "base")
    return root


@pytest.mark.spawns_process
@pytest.mark.parametrize("command", [
    "rm -rf tracked", "rm -rf tracked/sub", "rm -r top.txt", "Remove-Item -Recurse -Force tracked",
    "rm -rf tracked/missing",
])
def test_degrade_allows_recursive_rm_of_clean_tracked_or_absent_targets(tmp_path: Path, command: str) -> None:
    repo = _tracked_repo(tmp_path / "repo")
    assert _degrade(command, cwd=str(repo)) is None, command


@pytest.mark.spawns_process
def test_degrade_denies_recursive_rm_once_the_target_holds_work_git_cannot_restore(tmp_path: Path) -> None:
    repo = _tracked_repo(tmp_path / "repo")
    assert _degrade("rm -rf tracked", cwd=str(repo)) is None
    (repo / "tracked" / "sub" / "new.py").write_text("untracked\n", encoding="utf-8")
    assert _degrade("rm -rf tracked", cwd=str(repo))["decision"] == "deny"
    assert _degrade("rm -rf tracked/sub", cwd=str(repo))["decision"] == "deny"
    (repo / "tracked" / "sub" / "new.py").unlink()
    (repo / "tracked" / "a.py").write_text("edited, different length\n", encoding="utf-8")
    assert _degrade("rm -rf tracked", cwd=str(repo))["decision"] == "deny"
    assert _degrade("rm -r tracked/a.py", cwd=str(repo))["decision"] == "deny"
    assert _degrade("rm -rf tracked/sub", cwd=str(repo)) is None


@pytest.mark.spawns_process
@pytest.mark.parametrize("command", ["rm -rf .", "rm -rf .git", "rm -rf .git/objects", "rm -rf ../repo"])
def test_degrade_denies_recursive_rm_of_the_repo_root_or_store_on_identity(tmp_path: Path, command: str) -> None:
    repo = _tracked_repo(tmp_path / "repo")
    assert _degrade(command, cwd=str(repo))["decision"] == "deny", command


@pytest.mark.spawns_process
def test_degrade_recursive_rm_with_an_unreadable_index_is_not_clean(tmp_path: Path) -> None:
    repo = _tracked_repo(tmp_path / "repo")
    (repo / ".git" / "index").unlink()
    assert _degrade("rm -rf tracked", cwd=str(repo))["decision"] == "deny"


@pytest.mark.spawns_process
def test_degrade_recursive_rm_reads_the_index_without_spawning(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _tracked_repo(tmp_path / "repo")

    def no_spawn(*args, **kwargs) -> None:
        raise AssertionError("the cold rm classifier spawned a process")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    assert _degrade("rm -rf tracked", cwd=str(repo)) is None


@pytest.mark.parametrize("command", [
    "rm -rf scratch/run1", "rm -rf ./scratch/x", "rm file.txt", "rm -f src/a.py", "ls -la",
    "Remove-Item -Recurse scratch/x", "git status",
])
def test_degrade_allows_scratch_rm_and_harmless_commands(repo: Path, command) -> None:
    assert _degrade(command, cwd=str(repo)) is None


@pytest.mark.parametrize("command", [
    "rm -rf node_modules", "rm -rf .venv", "rm -rf src/__pycache__", "rm -rf build", "rm -rf target",
    "git clean -fdx scratch/x", "git clean -n", "git clean -fdn", "git clean -fdx -- node_modules",
])
def test_degrade_allows_regenerable_and_dry_run(repo: Path, command) -> None:
    for name in ("node_modules", ".venv", "src/__pycache__", "build", "target"):
        (repo / name).mkdir(parents=True, exist_ok=True)
    assert _degrade(command, cwd=str(repo)) is None


@pytest.mark.parametrize("command", ["git clean -fdx", "git clean -f", "git clean -fd -- src", "git -C . clean --force -d"])
def test_degrade_denies_git_clean_in_work_tree(repo: Path, command) -> None:
    (repo / "src").mkdir()
    assert _degrade(command, cwd=str(repo))["decision"] == "deny"


def test_degrade_allows_rm_and_clean_outside_a_work_tree(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    # No .git up the chain of tmp_path unless the tmp root sits in a repo; system tmp is allowed anyway.
    assert _degrade("rm -rf src", cwd=str(tmp_path)) is None


def test_degrade_denies_forced_clean_when_no_work_tree_resolves(tmp_path: Path) -> None:
    assert _degrade("git clean -fd", cwd=str(tmp_path / "nowhere"))["decision"] == "deny"
    assert _degrade("git clean -n", cwd=str(tmp_path / "nowhere")) is None


def test_degrade_allows_recursive_rm_under_system_tmp(repo: Path) -> None:
    import tempfile

    target = Path(tempfile.gettempdir()) / "backstop-degrade-probe"
    assert _degrade("rm -rf %s" % target, cwd=str(repo)) is None


@pytest.mark.parametrize("command", ["git stash drop", "git stash clear", "git -C x stash drop stash@{1}"])
def test_degrade_denies_stash_drop_and_clear(command) -> None:
    assert _degrade(command)["decision"] == "deny"


@pytest.mark.parametrize("command", ["git stash list", "git stash pop", "git stash push -- f", "echo git stash drop"])
def test_degrade_allows_other_stash_verbs(command) -> None:
    assert _degrade(command) is None


def test_degrade_denies_disk_fill_past_floor(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda p: usage(0, 0, 50 * 1024 ** 3))
    for command in ("dd if=/dev/zero of=big.bin bs=1M count=45000", "truncate -s 45G big.bin",
                    "fsutil file createnew big.bin 48318382080"):
        assert _degrade(command, cwd=str(repo))["decision"] == "deny", command
    for command in ("dd if=/dev/zero of=small.bin bs=1M count=100", "truncate -s 1G f", "truncate -s -1G f",
                    "dd if=a of=b"):
        assert _degrade(command, cwd=str(repo)) is None, command


def test_floor_constant_matches_disk_budget_policy() -> None:
    from coordinator_core.bash_guards import _disk_fill_contract

    assert backstop._DISK_FREE_FLOOR == _disk_fill_contract.load_policy().volume_free_floor
