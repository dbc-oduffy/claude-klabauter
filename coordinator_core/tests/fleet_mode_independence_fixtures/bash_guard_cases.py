"""Deny/allow fixtures for denial-class `coordinator_core.bash_guards` modules.

Keyed by dotted module name; the contract is in `__init__.py`. Guards that read
host state (registry, process table, free disk, git status) are driven by
patching the guard's own reader so the case stays in-process and under `tmp_path`.

Guards ruled advisory (`deny_registry.ADVISORY_BY_RULING`) are driven with `resolved_level`
forced to `floor`, so their deny predicate runs and returns the deny. A guard with no deny leg is
listed in the test's `_ADVISORY_ONLY`, not given fixtures.
"""

from __future__ import annotations

import shutil
import tempfile
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest

from coordinator_core.tests.fleet_mode_independence_fixtures import Case, GuardFixtures

_G = "coordinator_core.bash_guards."


def _cmd(command: str, tool: str = "Bash", **extra: Any) -> dict:
    payload: dict = {"tool_name": tool, "tool_input": {"command": command}, "session_id": "sess-fmi"}
    payload.update(extra)
    return payload


def _plain(command: str, tool: str = "Bash", **extra: Any) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        return _cmd(command, tool, cwd=str(root), **extra)

    return case


def _guard(name: str) -> Any:
    import importlib

    return importlib.import_module(_G + name)


_ALLOW_LS = _plain("git status && ls -la")

# --- registry-driven guards -------------------------------------------------


def _external_workspace(command: str, *, inside: bool) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        from coordinator_core import machine_resolver as mr

        reg = root / "ml"
        reg.mkdir()
        slice_dir = root / "slice"
        slice_dir.mkdir()
        (root / "sibling").mkdir()
        (reg / "registry.local.toml").write_text(
            f'[external_workspaces.p4_x]\npath = "{slice_dir.as_posix()}"\nvcs = "p4"\n', encoding="utf-8"
        )
        mp.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
        mr._external_ws_cache.clear()
        return _cmd(command, cwd=str(slice_dir if inside else root / "sibling"))

    return case


_PUSH_CONFIG = (
    '[remote "origin"]\n\turl = git@github.com:mine/engine.git\n'
    '[remote "upstream"]\n\turl = https://github.com/Theirs/Repo.git\n'
)


def _push(command: str) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        repo = root / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / ".git" / "config").write_text(_PUSH_CONFIG, encoding="utf-8")
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        reg = root / "registry"
        reg.mkdir()
        (reg / "registry.local.toml").write_text(
            '[policy]\nnon_owned_remotes = ["github.com/theirs/"]\n', encoding="utf-8"
        )
        mp.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
        return _cmd(command, cwd=str(repo))

    return case


# --- config-file driven guards ---------------------------------------------


def _configured(command: str, frontmatter: str, *, tool: str = "Bash", agent_id: str | None = "dispatched-1") -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        (root / "coordinator.local.md").write_text(f"---\n{frontmatter}\n---\n", encoding="utf-8")
        extra = {"agent_id": agent_id} if agent_id is not None else {}
        return _cmd(command, tool, cwd=str(root), **extra)

    return case


# --- identity-stubbed guards -------------------------------------------------


def _identity_stubbed(module: str, command: str, subagent_type: str, *, agent_id: str | None = "deadbeef0123") -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        guard = _guard(module)
        mp.setattr(guard, "resolve_git_root", lambda cwd: "/fake/git-root")
        mp.setattr(guard, "_resolve_subagent_identity", lambda raw, session: agent_id or "")
        mp.setattr(guard, "_read_backpointer_subagent_type", lambda git_root, aid, **kw: subagent_type)
        if hasattr(guard, "_write_block_log"):
            mp.setattr(guard, "_write_block_log", lambda *a, **kw: None)
        if hasattr(guard, "is_confined_by_roster_absence"):
            mp.setattr(guard, "is_confined_by_roster_absence", lambda effective_type: False)
        extra: dict = {"agent_type": subagent_type}
        if agent_id is not None:
            extra["agent_id"] = agent_id
        return _cmd(command, cwd=str(root), **extra)

    return case


# --- stateful / host-stubbed guards -----------------------------------------

_Usage = namedtuple("_Usage", "total used free")
_GiB = 1024**3


def _disk(command: str, tool: str = "Bash", *, free: int) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        (root / "big").mkdir()
        (root / "big" / "a.bin").write_bytes(b"x" * 4096)
        mp.delenv("COORDINATOR_DISK_BUDGET_VOLUME_FREE_FLOOR", raising=False)
        mp.setattr(shutil, "disk_usage", lambda p: _Usage(100 * _GiB, 0, free))
        return _cmd(command, tool, cwd=str(root))

    return case


def _deploy(dirty: list[str]) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        mp.setattr(_guard("guard_deploy_dirty_tree"), "_dirty_paths", lambda cwd: list(dirty))
        return _cmd("firebase deploy", cwd=str(root))

    return case


def _hazard_repo(command: str, *, hazard: bool) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        mp.setattr(_guard("block_noncanonical_branch_creation"), "_is_hazard_repo", lambda git_root: hazard)
        return _cmd(command, cwd=str(root))

    return case


def _p4_policy(command: str, *, armed: bool) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        from coordinator_core import machine_resolver as mr

        reg = root / "registry"
        reg.mkdir()
        flag = "true" if armed else "false"
        (reg / "registry.local.toml").write_text(f"[policy]\np4_no_submit = {flag}\n", encoding="utf-8")
        mp.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(reg))
        mr._external_ws_cache.clear()
        return _cmd(command, cwd=str(root))

    return case


def _p4_gated(command: str, *, gated: bool) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        mp.setattr(_guard("p4_verb_fence"), "_is_p4_gated", lambda cwd: gated)
        return _cmd(command, cwd=str(root))

    return case


def _heavy_identity(command: str, *, agent_id: str | None) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        guard = _guard("guard_heavy_command_admission")
        mp.setattr(guard, "_workflow_runs_of", lambda payload: [])
        mp.setattr(guard, "_deny_log_path", lambda: root / "would-deny.jsonl")
        extra: dict = {"agent_type": "general-purpose"} if agent_id else {}
        if agent_id:
            extra["agent_id"] = agent_id
        payload = _cmd(command, cwd=str(root), **extra)
        payload["tool_input"]["run_in_background"] = False
        return payload

    return case


def _post_executor(*, executor_ran: bool) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        from coordinator_core.hooks import nudge_em_code_dispatch as nd

        g = _guard("guard_post_executor_verification")
        sid = "sess-verify-1"
        repo = root / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "pkg").mkdir()
        (repo / "pkg" / "__init__.py").write_text("")
        (root / "tmp").mkdir()
        mp.setattr(tempfile, "gettempdir", lambda: str(root / "tmp"))
        mp.setattr(nd, "_is_bootstrap_or_out_of_repo", lambda p: False)
        mp.setattr(nd, "_session_sizing_state", lambda payload, s: (True, False))
        mp.setattr(nd, "resolve_mode", lambda k, s: False)
        g._PKG_CACHE.clear()
        if executor_ran:
            d = repo / ".git" / "coordinator-sessions" / sid
            d.mkdir(parents=True)
            (d / "dispatched-agents.txt").write_text("abcdef0123456789\tsonnet\tcoordinator:executor\t1\n")
        payload = {
            "tool_name": "Bash",
            "session_id": sid,
            "cwd": str(repo),
            "tool_input": {"command": "python -m pytest pkg"},
        }
        if executor_ran:
            assert g.check(dict(payload)) is None  # the first verification run is free; the second denies
        return payload

    return case


def _home_with_claude(command: str, *, in_claude_home: bool) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        home = root / "home"
        claude_home = home / ".claude"
        claude_home.mkdir(parents=True)
        clone = root / "clone"
        clone.mkdir()
        env = {"HOME": str(home), "USERPROFILE": str(home)}
        return _cmd(command, cwd=str(claude_home if in_claude_home else clone), env=env)

    return case


# --- advisory-by-ruling guards: the deny logic runs, the guard level is forced to floor ---


def _floor(mp: pytest.MonkeyPatch) -> None:
    from coordinator_core import machine_profile

    mp.setattr(machine_profile, "resolved_level", lambda *a, **kw: "floor")


def _floored(command: str, **extra: Any) -> Case:
    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        _floor(mp)
        return _cmd(command, cwd=str(root), **extra)

    return case


def _under_root(template: str, *, drive: bool = False, existing: bool = False, descendant: bool = False) -> Case:
    """`template` formatted with `{top}` = a not-yet-existing entry directly under a tmp home/drive root.

    `existing` pre-creates the entry; `descendant` nests it one level deeper. Home and drive roots are
    re-pointed at tmp_path so the real ones are never touched.
    """

    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        _floor(mp)
        top = root / "top"
        top.mkdir()
        home = root / "home"
        home.mkdir()
        home_dir = home if drive else top
        mp.setenv("HOME", str(home_dir))
        mp.setenv("USERPROFILE", str(home_dir))
        mp.setenv("COORDINATOR_DRIVE_ROOTS", str(top) if drive else str(home))
        mp.delenv("COORDINATOR_ALLOW_DRIVE_ROOT_NEW_DIR", raising=False)
        entry = top / "entry"
        if existing:
            entry.mkdir()
        target = entry / "nested" if descendant else entry
        return _cmd(template.format(top=target.as_posix()), cwd=str(root))

    return case


def _doctrine_surface(command: str) -> Case:
    """`check` takes the governed-surface list as a second positional the harness never passes."""

    def case(root: Path, mp: pytest.MonkeyPatch) -> dict:
        _floor(mp)
        guard = _guard("guard_doctrine_surface_bash_write")
        original = guard.check
        mp.setattr(guard, "check", lambda payload: original(payload, []))
        return _cmd(command, cwd=str(root))

    return case


_SCAFFOLD = "python3 -m coordinator_core.install.scaffold_structure --manifest-root x"
_PLAN_WRITE = 'echo "in progress" >> docs/plans/2026-07-30-x.md'

FIXTURES: dict[str, GuardFixtures] = {
    _G + "block_approval_sentinel_creation": {
        "deny": [_floored("touch .coordinator-doctrine-edit-approved")],
        "allow": [_floored("git status && ls -la"), _floored("touch unrelated.txt")],
    },
    _G + "block_dev_repo_sentinel_removal": {
        "deny": [_plain("rm .coordinator-dev-repo"), _plain("git rm .coordinator-dev-repo")],
        "allow": [_ALLOW_LS, _plain("rm unrelated.txt")],
    },
    _G + "block_drive_root_new_dir_bash_write": {
        "deny": [_under_root("mkdir {top}", drive=True)],
        "allow": [
            _under_root("mkdir {top}", drive=True, existing=True),
            _under_root("mkdir -p {top}", drive=True, descendant=True, existing=True),
        ],
    },
    _G + "block_editor_kill_by_name": {
        "deny": [_plain("taskkill /IM UnrealEditor.exe /F")],
        "allow": [_plain("echo hi")],
    },
    _G + "block_external_workspace_vcs_write": {
        "deny": [_external_workspace("git commit -m x", inside=True)],
        "allow": [_external_workspace("git commit -m x", inside=False), _external_workspace("git status", inside=True)],
    },
    _G + "block_home_root_bash_write": {
        "deny": [_under_root("echo x > {top}")],
        "allow": [_under_root("echo x > {top}", existing=True), _under_root("ls {top}")],
    },
    _G + "block_noncanonical_branch_creation": {
        "deny": [_hazard_repo("git checkout -b foo", hazard=True)],
        "allow": [_hazard_repo("git checkout -b foo", hazard=False), _hazard_repo("git status", hazard=True)],
    },
    _G + "block_perforce_submit": {
        "deny": [_p4_policy("p4 submit -d x", armed=True)],
        "allow": [_p4_policy("p4 submit -d x", armed=False), _p4_policy("p4 sync", armed=True)],
    },
    _G + "block_push_pr_non_owned_remote": {
        "deny": [_push("git push upstream main")],
        "allow": [_push("git push origin main"), _push("git status")],
    },
    _G + "block_reviewer_bash_outside_allowlist": {
        "deny": [_identity_stubbed("block_reviewer_bash_outside_allowlist", "rm -rf /", "coordinator:code-reviewer")],
        "allow": [_identity_stubbed("block_reviewer_bash_outside_allowlist", "rm -rf /", "coordinator:enricher")],
    },
    _G + "block_stash_destruction": {
        "deny": [_plain("git stash drop")],
        "allow": [_plain("ls")],
    },
    _G + "block_subagent_commit": {
        "deny": [_plain("git commit -m x", agent_id="a1", agent_type="general-purpose")],
        "allow": [_plain("git commit -m x"), _plain("git status", agent_id="a1", agent_type="general-purpose")],
    },
    _G + "block_subagent_plan_body_bash_write": {
        "deny": [_identity_stubbed("block_subagent_plan_body_bash_write", _PLAN_WRITE, "coordinator:executor")],
        "allow": [
            _identity_stubbed("block_subagent_plan_body_bash_write", _PLAN_WRITE, "", agent_id=None),
            _identity_stubbed("block_subagent_plan_body_bash_write", _PLAN_WRITE, "coordinator:enricher"),
        ],
    },
    _G + "block_whole_filesystem_scan": {
        "deny": [_plain("find / -name foo")],
        "allow": [_plain("ls")],
    },
    _G + "block_worktree_creation": {
        "deny": [_plain("git worktree add ../x")],
        "allow": [_plain("ls")],
    },
    _G + "block_worktree_sentinel_creation": {
        "deny": [_floored("touch .coordinator-override-worktree-guard")],
        "allow": [_floored("git status && ls -la"), _floored("touch unrelated.txt")],
    },
    _G + "check_raw_pid_liveness": {
        "deny": [_plain("kill -0 1234")],
        "allow": [_plain("ls")],
    },
    _G + "check_test_suite_invocation": {
        "deny": [_plain("pytest")],
        "allow": [_plain("ls")],
    },
    _G + "guard_deploy_dirty_tree": {
        "deny": [_deploy(["peer.txt"])],
        "allow": [_deploy([]), _plain("git status")],
    },
    _G + "guard_disk_fill": {
        "deny": [_disk("cp -r big dst", free=10 * _GiB), _disk("dd if=/dev/zero of=out.img bs=1M count=100", free=10 * _GiB)],
        "allow": [_disk("cp -r big dst", free=90 * _GiB), _disk("ls", free=10 * _GiB)],
    },
    _G + "guard_doctrine_surface_bash_write": {
        "deny": [_doctrine_surface("echo x > state/sizings/foo.yaml")],
        "allow": [_doctrine_surface("git status && ls -la"), _doctrine_surface("echo x > scratch/notes.txt")],
    },
    _G + "guard_heavy_command_admission": {
        "deny": [_heavy_identity("tsc --noEmit", agent_id="deadbeef0123")],
        "allow": [_heavy_identity("git status", agent_id=None), _heavy_identity("git status", agent_id="deadbeef0123")],
    },
    _G + "guard_host_subagent_bash_ban": {
        "deny": [_configured("ls -la", "subagent_bash_policy: deny")],
        "allow": [
            _configured("ls -la", "subagent_bash_policy: deny", agent_id=None),
            _configured("ls -la", "project_type: general"),
        ],
    },
    _G + "guard_host_subagent_bash_spawn_shapes": {
        "deny": [_configured("for f in *.py; do echo $f; done", "subagent_bash_spawn_shapes: deny")],
        "allow": [
            _configured("cat one-known-file.txt", "subagent_bash_spawn_shapes: deny"),
            _configured("for f in *.py; do echo $f; done", "project_type: general"),
        ],
    },
    _G + "guard_post_executor_verification": {
        "deny": [_post_executor(executor_ran=True)],
        "allow": [_post_executor(executor_ran=False)],
    },
    _G + "guard_repo_setup_claude_home_refusal": {
        "deny": [_home_with_claude(_SCAFFOLD, in_claude_home=True)],
        "allow": [_home_with_claude(_SCAFFOLD, in_claude_home=False), _home_with_claude("ls", in_claude_home=True)],
    },
    _G + "guard_subagent_heavy_ue_launch": {
        "deny": [_plain("UnrealEditor-Cmd.exe proj.uproject -run=Foo", agent_id="agent-abc123")],
        "allow": [
            _plain("UnrealEditor-Cmd.exe proj.uproject -run=Foo"),
            _plain("grep -n UnrealBuildTool foo.txt", agent_id="agent-abc123"),
        ],
    },
    _G + "p4_verb_fence": {
        "deny": [_p4_gated("p4 submit -d x", gated=True)],
        "allow": [_p4_gated("p4 submit -d x", gated=False), _p4_gated("ls", gated=True)],
    },
}
