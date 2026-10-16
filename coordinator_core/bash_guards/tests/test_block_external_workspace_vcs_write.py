"""block_external_workspace_vcs_write: no git history write in or into a registered external workspace."""

from __future__ import annotations

import os
import time

import pytest

from coordinator_core import machine_profile
from coordinator_core import machine_resolver as mr
from coordinator_core.bash_guards import block_external_workspace_vcs_write as guard


@pytest.fixture
def ws(tmp_path, monkeypatch):
    reg = tmp_path / "ml"
    reg.mkdir()
    root = tmp_path / "slice"
    root.mkdir()
    (root / "sub").mkdir()
    (tmp_path / "slice2").mkdir()
    (tmp_path / "sibling").mkdir()
    (reg / "registry.local.toml").write_text(
        f'[external_workspaces.p4_x]\npath = "{root.as_posix()}"\nvcs = "p4"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
    mr._external_ws_cache.clear()
    yield tmp_path
    mr._external_ws_cache.clear()


def _p(cmd: str, cwd=None, tool="Bash") -> dict:
    payload = {"tool_name": tool, "tool_input": {"command": cmd}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


def _denied(out) -> bool:
    return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_commit_in_cwd_inside_denied(ws):
    out = guard.check(_p("git commit -m x", cwd=ws / "slice" / "sub"))
    assert _denied(out)
    assert "p4_x" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_git_dash_c_from_outside_denied(ws):
    assert _denied(guard.check(_p(f"git -C {(ws / 'slice').as_posix()} commit -m x", cwd=ws / "sibling")))


def test_relative_dash_c_denied(ws):
    assert _denied(guard.check(_p("git -C ../slice commit -m x", cwd=ws / "sibling")))


def test_cd_then_commit_denied(ws):
    assert _denied(guard.check(_p(f"cd {(ws / 'slice').as_posix()} && git commit -m x", cwd=ws / "sibling")))


def test_git_dir_and_work_tree_denied(ws):
    root = (ws / "slice").as_posix()
    assert _denied(guard.check(_p(f"git --git-dir={root}/.git commit -m x", cwd=ws / "sibling")))
    assert _denied(guard.check(_p(f"git --work-tree {root} commit -m x", cwd=ws / "sibling")))
    assert _denied(guard.check(_p(f"GIT_DIR={root}/.git git commit -m x", cwd=ws / "sibling")))


@pytest.mark.parametrize(
    "stmt",
    ["export GIT_DIR={r}/.git", "export GIT_WORK_TREE={r}", "GIT_DIR={r}/.git", "GIT_WORK_TREE={r}",
     "export FOO=1 GIT_DIR={r}/.git", "export GIT_DIR='{r}/.git'"],
)
@pytest.mark.parametrize("sep", ["; ", " && "])
def test_carried_env_then_commit_denied(ws, stmt, sep):
    root = (ws / "slice").as_posix()
    cmd = stmt.format(r=root) + sep + "git commit -m x"
    assert _denied(guard.check(_p(cmd, cwd=ws / "sibling")))


def test_carried_env_hidden_value_denies_only_inside(ws):
    cmd = "export GIT_DIR=$X/.git; git commit -m x"
    assert _denied(guard.check(_p(cmd, cwd=ws / "slice")))
    assert guard.check(_p(cmd, cwd=ws / "sibling")) is None


def test_carried_env_relative_value_resolves_against_cwd(ws):
    assert _denied(guard.check(_p("export GIT_WORK_TREE=../slice; git commit -m x", cwd=ws / "sibling")))


def test_carried_env_windows_shaped_path_does_not_crash(ws):
    cmd = "export GIT_DIR=C:\\other\\ws\\.git; git commit -m x"  # abs-path-ok: Windows-shape parse fixture
    assert guard.check(_p(cmd, cwd=ws / "sibling")) is None


def test_carried_env_outside_or_unrelated_allowed(ws):
    sib = (ws / "sibling").as_posix()
    assert guard.check(_p(f"export GIT_DIR={sib}/.git; git commit -m x", cwd=ws / "sibling")) is None
    assert guard.check(_p("export FOO=bar; git commit -m x", cwd=ws / "sibling")) is None
    root = (ws / "slice").as_posix()
    assert guard.check(_p(f"export GIT_DIR={root}/.git; git status", cwd=ws / "sibling")) is None


def test_carried_env_followed_even_when_command_has_parens(ws):
    root = (ws / "slice").as_posix()
    cmd = f"export GIT_DIR={root}/.git; echo $(true); git commit -m x"
    assert _denied(guard.check(_p(cmd, cwd=ws / "sibling")))


@pytest.mark.parametrize(
    "tmpl",
    [
        "(export GIT_DIR={r}/.git; git commit -m x)",
        "(GIT_WORK_TREE={r}; git commit -m x)",
        "(cd {r}; git commit -m x)",
        "( cd {r} ; git commit -m x )",
        "(cd {r} && git commit -m x)",
        "((cd {r}; git commit -m x))",
        "(true; (cd {r}; (git commit -m x)))",
        "(cd {r}; (git commit -m x); git status)",
        "(cd {r}) ; git status; (cd {r}; git commit -m x)",
        "echo $(cd {r}; git commit -m x)",
        "echo $(export GIT_DIR={r}/.git; git commit -m x)",
        "cd {r}; (git commit -m x)",
        "(git -C {r} commit -m x)",
    ],
)
def test_subshell_scoped_env_and_cd_denied(ws, tmpl):
    cmd = tmpl.format(r=(ws / "slice").as_posix())
    assert _denied(guard.check(_p(cmd, cwd=ws / "sibling")))


def test_subshell_carry_does_not_leak_past_close(ws):
    root = (ws / "slice").as_posix()
    for cmd in (
        f"(cd {root}); git commit -m x",
        f"(cd {root}) && git commit -m x",
        f"(export GIT_DIR={root}/.git); git commit -m x",
        f"(cd {root}; true); (git commit -m x)",
        f"((cd {root})); git commit -m x",
        f"(true; (cd {root}); git commit -m x)",
        f"echo $(cd {root}); git commit -m x",
        f"(cd {root}; git status); git commit -m x",
    ):
        assert guard.check(_p(cmd, cwd=ws / "sibling")) is None, cmd


def test_subshell_outer_carry_visible_inside(ws):
    root = (ws / "slice").as_posix()
    assert _denied(guard.check(_p(f"export GIT_DIR={root}/.git; (git commit -m x)", cwd=ws / "sibling")))
    assert _denied(guard.check(_p(f"cd {root}; (true; git commit -m x)", cwd=ws / "sibling")))


def test_subshell_git_write_inside_cwd_denied_and_outside_allowed(ws):
    assert _denied(guard.check(_p("(git commit -m x)", cwd=ws / "slice")))
    assert guard.check(_p("(git commit -m x)", cwd=ws / "sibling")) is None
    assert guard.check(_p("(git commit -m 'a)'; git status)", cwd=ws / "sibling")) is None


def test_quoted_close_paren_does_not_end_scope(ws):
    root = (ws / "slice").as_posix()
    assert _denied(guard.check(_p(f"(cd {root}; git log --format='%h)'; git commit -m x)", cwd=ws / "sibling")))


def test_cd_then_commit_across_statements_still_denied(ws):
    assert _denied(guard.check(_p(f"cd {(ws / 'slice').as_posix()}; git commit -m x", cwd=ws / "sibling")))


def test_init_and_clone_destination_denied(ws):
    root = (ws / "slice").as_posix()
    assert _denied(guard.check(_p(f"git init {root}/sub", cwd=ws / "sibling")))
    assert _denied(guard.check(_p(f"git clone x {root}/y", cwd=ws / "sibling")))
    assert _denied(guard.check(_p("git clone https://h/r.git", cwd=ws / "slice")))
    assert _denied(guard.check(_p("git init", cwd=ws / "slice")))


def test_clone_from_workspace_to_outside_allowed(ws):
    root = (ws / "slice").as_posix()
    assert guard.check(_p(f"git clone {root} {(ws / 'sibling' / 'c').as_posix()}", cwd=ws / "sibling")) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "merge x", "rebase main", "am p.patch", "cherry-pick abc", "revert abc", "tag v1",
        "push origin main", "stash", "stash push -m x", "reset --hard", "update-ref refs/x abc",
        "branch topic", "checkout -b topic", "switch -c topic", "worktree add ../w",
    ],
)
def test_history_and_ref_verbs_denied(ws, cmd):
    assert _denied(guard.check(_p(f"git {cmd}", cwd=ws / "slice")))


@pytest.mark.parametrize(
    "cmd",
    [
        "status", "log --oneline", "diff", "show HEAD", "ls-files", "rev-parse HEAD", "blame f",
        "branch", "branch -a", "branch --list", "tag", "tag -l", "tag --contains abc", "stash list", "worktree list",
        "checkout main", "-C . status",
    ],
)
def test_read_verbs_allowed(ws, cmd):
    assert guard.check(_p(f"git {cmd}", cwd=ws / "slice")) is None


def test_commit_in_sibling_allowed(ws):
    assert guard.check(_p("git commit -m x", cwd=ws / "sibling")) is None
    assert guard.check(_p("git commit -m x", cwd=ws / "slice2")) is None


def test_hidden_dir_inside_workspace_denies_outside_allows(ws):
    assert _denied(guard.check(_p("git -C $D commit -m x", cwd=ws / "slice")))
    assert guard.check(_p("git -C $D commit -m x", cwd=ws / "sibling")) is None
    assert _denied(guard.check(_p("cd $(pwd)/x && git commit -m y", cwd=ws / "slice")))


def test_quoted_prose_and_wrapped_shell_forms(ws):
    assert guard.check(_p('echo "git commit -m x"', cwd=ws / "slice")) is None
    assert _denied(guard.check(_p("sh -c 'git commit -m x'", cwd=ws / "slice")))
    assert _denied(guard.check(_p("git status && git commit -m x", cwd=ws / "slice")))


def test_powershell_tool_covered(ws):
    assert _denied(guard.check(_p("git.exe commit -m x", cwd=ws / "slice", tool="PowerShell")))


@pytest.mark.skipif(os.path.normcase("A") == "A", reason="case-insensitive normcase only on Windows")
def test_case_variant_denied(ws):
    assert _denied(guard.check(_p(f"git -C {(ws / 'slice').as_posix().upper()} commit -m x", cwd=ws)))


@pytest.mark.skipif(os.name != "nt", reason="drive-letter paths only resolve on Windows")
def test_drive_letter_forward_slash_denied(ws):
    root = (ws / "slice").as_posix()
    assert root[1] == ":"
    assert _denied(guard.check(_p(f"git -C {root} commit -m x", cwd=ws / "sibling")))


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privilege on Windows")
def test_symlink_into_workspace_denied(ws):
    link = ws / "sibling" / "lnk"
    link.symlink_to(ws / "slice", target_is_directory=True)
    assert _denied(guard.check(_p("git commit -m x", cwd=link)))


def test_deny_survives_every_guard_level(ws, monkeypatch):
    out = guard.check(_p("git commit -m x", cwd=ws / "slice"))
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        assert machine_profile.apply_guard_level("block-external-workspace-vcs-write", out) == out


def test_unreadable_registry_denies_write_allows_read(ws, monkeypatch):
    monkeypatch.setattr(mr, "external_workspace_registry_readable", lambda: False)
    out = guard.check(_p("git commit -m x", cwd=ws / "sibling"))
    assert _denied(out)
    assert "unreadable" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert guard.check(_p("git status", cwd=ws / "sibling")) is None


def test_no_workspace_registered_allows(ws, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(empty))
    mr._external_ws_cache.clear()
    assert guard.check(_p("git commit -m x", cwd=ws / "slice")) is None


def test_non_git_command_and_other_tools_ignored(ws):
    assert guard.check(_p("ls", cwd=ws / "slice")) is None
    assert guard.check({"tool_name": "Write", "tool_input": {"command": "git commit"}}) is None


def test_check_is_fast_and_spawns_nothing(ws, monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("spawn on guard path")

    monkeypatch.setattr(subprocess, "Popen", boom)
    payload = _p("git commit -m x", cwd=ws / "slice")
    assert _denied(guard.check(payload))
    start = time.process_time()
    for _ in range(20):
        guard.check(payload)
    assert (time.process_time() - start) / 20 < 0.2
