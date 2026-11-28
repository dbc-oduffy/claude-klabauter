"""Backstop: guard set pinned to the registry, deny/allow on push remotes, no warm/ops import."""
from __future__ import annotations

import os

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


def _write_index(root: Path, names) -> None:
    """A v2 `.git/index` recording each named work-tree file's current stat and blob hash (no git process)."""
    import hashlib
    import struct

    body = b"DIRC" + struct.pack(">II", 2, len(names))
    for name in sorted(names):
        content = (root / name).read_bytes()
        info = (root / name).stat()
        sec, nsec = divmod(info.st_mtime_ns, 10 ** 9)
        raw = (struct.pack(">IIIIIIIIII", 0, 0, sec, nsec, 0, 0, 0o100644, 0, 0, len(content))
               + hashlib.sha1(b"blob %d\0" % len(content) + content).digest()
               + struct.pack(">H", min(len(name), 0xFFF)) + name.encode() + b"\0")
        body += raw + b"\0" * (-len(raw) % 8)
    (root / ".git" / "index").write_bytes(body + hashlib.sha1(body).digest())


def _tracked(repo: Path, count: int = 3) -> list:
    names = [f"tracked/f{i}.py" for i in range(count)]
    (repo / "tracked").mkdir()
    for name in names:
        (repo / name).write_bytes(b"a")
    _write_index(repo, names)
    return names


@pytest.mark.parametrize("command", ["rm -rf tracked", "rm -r tracked/f0.py", "rm -rf tracked/missing"])
def test_degrade_allows_recursive_rm_of_clean_tracked_or_absent_targets(repo: Path, command: str) -> None:
    _tracked(repo)
    assert _degrade(command, cwd=str(repo)) is None, command


def test_degrade_denies_recursive_rm_once_the_target_holds_work_git_cannot_restore(repo: Path) -> None:
    _tracked(repo)
    (repo / "tracked" / "new.py").write_bytes(b"untracked")
    assert _degrade("rm -rf tracked", cwd=str(repo))["decision"] == "deny"
    (repo / "tracked" / "new.py").unlink()
    assert _degrade("rm -rf tracked", cwd=str(repo)) is None
    (repo / "tracked" / "f0.py").write_bytes(b"edited, different length")
    assert _degrade("rm -rf tracked", cwd=str(repo))["decision"] == "deny"
    assert _degrade("rm -r tracked/f0.py", cwd=str(repo))["decision"] == "deny"


@pytest.mark.parametrize("command", ["rm -rf .", "rm -rf .git", "rm -rf .git/objects"])
def test_degrade_denies_recursive_rm_of_the_repo_root_or_store_on_identity(repo: Path, command: str) -> None:
    _tracked(repo)
    (repo / ".git" / "objects").mkdir()
    assert _degrade(command, cwd=str(repo))["decision"] == "deny", command


def test_degrade_recursive_rm_with_an_unreadable_index_is_not_clean(repo: Path) -> None:
    _tracked(repo)
    (repo / ".git" / "index").unlink()
    assert _degrade("rm -rf tracked", cwd=str(repo))["decision"] == "deny"


def test_degrade_recursive_rm_over_the_walk_cap_is_not_clean(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tracked(repo, count=4)
    assert _degrade("rm -rf tracked", cwd=str(repo)) is None
    monkeypatch.setattr(backstop, "_WALK_CAP", 3)
    assert _degrade("rm -rf tracked", cwd=str(repo))["decision"] == "deny"


def test_degrade_recursive_rm_keys_nested_dirs_against_the_index(repo: Path) -> None:
    names = _tracked(repo, count=1)
    (repo / "tracked" / "sub" / "deep").mkdir(parents=True)
    (repo / "tracked" / "sub" / "deep" / "g.py").write_bytes(b"g")
    names.append("tracked/sub/deep/g.py")
    _write_index(repo, names)
    assert _degrade("rm -rf tracked", cwd=str(repo)) is None
    (repo / "tracked" / "sub" / "deep" / "h.py").write_bytes(b"untracked")
    assert _degrade("rm -rf tracked", cwd=str(repo))["decision"] == "deny"
    assert _degrade("rm -rf tracked/sub", cwd=str(repo))["decision"] == "deny"


def test_degrade_recursive_rm_over_the_index_entry_cap_is_not_clean(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _tracked(repo, count=4)
    monkeypatch.setattr(backstop, "_WALK_CAP", 3)
    assert backstop._index_entries(str(repo), b"tracked", True) is None
    monkeypatch.setattr(backstop, "_WALK_CAP", 4)
    assert len(backstop._index_entries(str(repo), b"tracked", True)) == 4


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


def _loses(command: str, repo: Path) -> list:
    return backstop._git_clean_loses_loadbearing(command, str(repo))


def _clean_repo(repo: Path, untracked=("state/x.md",), tracked=("tracked/f0.py",)) -> Path:
    for name in (*tracked, *untracked):
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(b"a")
    _write_index(repo, list(tracked))
    return repo


@pytest.mark.parametrize("command", ["git clean -fd", "git clean -fdx", "git clean -f -d", "git -C . clean --force -d", "git clean -fd -- state"])
def test_clean_oracle_names_an_untracked_state_dir_inside_system_tmp(repo: Path, command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_repo(repo)
    assert _loses(command, repo) == ["state"], command
    monkeypatch.setattr(backstop, "_git_clean_forced", lambda c, d: False)  # the tmp-exempt classifier's verdict
    verdict = _degrade(command, cwd=str(repo))
    assert verdict["decision"] == "deny" and "state" in verdict["reason"], command


@pytest.mark.parametrize("command", ["git clean -fd -- tracked", "git clean -nd", "git clean -fdn", "git clean --dry-run -fd", "git clean -fd -e state",
                                     "git clean -fd --exclude=state", "git status", "echo git clean -fd"])
def test_clean_oracle_allows_what_warm_keeps(repo: Path, command: str, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_repo(repo)
    assert _loses(command, repo) == [], command
    monkeypatch.setattr(backstop, "_git_clean_forced", lambda c, d: False)
    assert _degrade(command, cwd=str(repo)) is None, command


def test_clean_oracle_keeps_a_tracked_state_dir_and_names_only_untracked_children(repo: Path) -> None:
    _clean_repo(repo, untracked=("state/new.md",), tracked=("state/old.md",))
    assert _loses("git clean -fd", repo) == ["state/new.md"]
    (repo / "state" / "new.md").unlink()
    assert _loses("git clean -fd", repo) == []


def test_clean_oracle_ignores_non_loadbearing_untracked_files(repo: Path) -> None:
    _clean_repo(repo, untracked=("scratch/a.txt", "build/o.bin", "notes.txt"))
    assert _loses("git clean -fdx", repo) == []


def test_clean_oracle_honours_gitignore_per_mode(repo: Path) -> None:
    _clean_repo(repo)
    (repo / ".gitignore").write_text("state/\n", encoding="utf-8")
    assert _loses("git clean -fd", repo) == []
    assert _loses("git clean -fdx", repo) == ["state"]
    assert _loses("git clean -fdX", repo) == ["state"]
    (repo / ".gitignore").write_text("*.md\n!keep.md\n", encoding="utf-8")
    assert _loses("git clean -fdX", repo) == ["state/x.md"]


def test_clean_oracle_only_ignored_mode_keeps_unignored_state(repo: Path) -> None:
    _clean_repo(repo)
    assert _loses("git clean -fdX", repo) == []


def test_clean_oracle_scopes_to_the_pathspec_and_dash_c(repo: Path) -> None:
    _clean_repo(repo, untracked=("pkg/state/x.md",), tracked=("tracked/f0.py", "pkg/t.py"))
    assert _loses("git clean -fd -- tracked", repo) == []
    assert _loses("git clean -fd -- pkg", repo) == ["pkg/state"]
    assert _loses("git -C pkg clean -fd", repo) == ["pkg/state"]
    assert _loses("git -C tracked clean -fd", repo) == []
    assert _loses("git clean -fd -- pk*", repo) == ["pkg/state"]


def test_clean_oracle_skips_a_nested_repo_unless_doubly_forced(repo: Path) -> None:
    _clean_repo(repo, untracked=("state/x.md", "state/.git/HEAD"))
    assert _loses("git clean -fd", repo) == []
    assert _loses("git clean -ffd", repo) == ["state"]
    assert _loses("git clean -f -f -d", repo) == ["state"]


def test_clean_oracle_reports_a_wholly_untracked_tree_with_one_scandir(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_repo(repo, untracked=("junk/a/b/c/d/e/f.txt", "junk/a/b/c/d/state/g.txt"))
    calls = []
    real = os.scandir
    monkeypatch.setattr(os, "scandir", lambda p: calls.append(p) or real(p))
    assert _loses("git clean -fd", repo) == []
    assert len(calls) == 2, calls  # the work tree root and its tracked dir; `junk/` is never entered


def test_clean_oracle_over_the_scan_cap_counts_as_a_loss(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clean_repo(repo, untracked=("notes.txt",))
    assert _loses("git clean -fd", repo) == []
    monkeypatch.setattr(backstop, "_CLEAN_SCAN_CAP", 1)
    assert _loses("git clean -fd", repo) == ["(scan cap exceeded)"]


def test_clean_oracle_with_no_index_treats_every_path_as_untracked(repo: Path) -> None:
    (repo / "state").mkdir()
    (repo / "state" / "x.md").write_bytes(b"a")
    assert _loses("git clean -fd", repo) == ["state"]


def test_clean_oracle_outside_a_work_tree_names_nothing(tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    assert backstop._git_clean_loses_loadbearing("git clean -fd", str(tmp_path / "nowhere")) == []


def test_clean_oracle_loadbearing_predicate_matches_the_warm_one() -> None:
    from coordinator_core.bash_guards import _dc_git_shared

    assert backstop._LOADBEARING_RE.pattern == _dc_git_shared._GC_LOADBEARING_RE.pattern
    assert backstop._LOADBEARING_PREFIXES == _dc_git_shared._GC_LOADBEARING_PREFIXES
    for path in ("state", "state/x.md", "a/state/x", "docs/plans/p.md", "docs", "docs/", "src/a.py", "x-handoff-1.md", "statement", "cross-repo/inbox/m.md",
                 "review-trail/a.json", "completion/x", ""):
        assert backstop._loadbearing(path) == _dc_git_shared._is_loadbearing(path), path


def test_clean_oracle_index_reader_matches_the_tracked_names(repo: Path) -> None:
    names = ["a.py", "tracked/f0.py", "tracked/sub/g.py", "z"]
    for name in names:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(b"a")
    _write_index(repo, names)
    assert backstop._index_names(str(repo)) == set(names)


def test_floor_constant_matches_disk_budget_policy() -> None:
    from coordinator_core.bash_guards import _disk_fill_contract

    assert backstop._DISK_FREE_FLOOR == _disk_fill_contract.load_policy().volume_free_floor
