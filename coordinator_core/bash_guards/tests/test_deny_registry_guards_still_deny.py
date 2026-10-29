"""Every guard in ``deny_registry`` still denies a live harmful input.

``machine_profile.apply_guard_level`` clamps every unregistered guard to an advisory, so an
advisory re-pin of a retired deny is indistinguishable from a guard that stopped working
unless the registered guards are pinned the other way. This file is that other side: one
live harmful input per registered guard, driven through the production entry (the bash
dispatcher, or the hook handler for the three non-Bash hooks), that must come back
``permissionDecision: deny`` and name the registered guard in its reason.

The guard set is read from ``deny_registry`` at test time: a new registry entry with no case
below fails ``test_every_registered_guard_has_a_case``.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from coordinator_core import deny_registry, machine_resolver
from coordinator_core.bash_guards import dispatch

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_AGENT_ID = "a0123456789abcdef"
_OWNED = "https://github.com/owned/repo.git"
_THEIRS = "https://github.com/theirs/repo.git"

#: guard -> (command, dispatched-subagent caller). ``{repo}`` and ``{other}`` are tmp paths.
_BASH_CASES = {
    "block-push-pr-non-owned-remote": ("git push upstream main", False),
    "block-perforce-submit": ("p4 submit -d x", False),
    "p4-verb-fence": ("p4 reconcile", False),
    "destructive-git-orphan": ("git reset --hard $(git rev-parse HEAD~1)", False),
    "destructive-rm": ("rm -rf {repo}", False),
    "destructive-git-clean": ("git -C {repo} clean -fd", False),
    "destructive-git-revert": ("git checkout .", False),
    "block-stash-destruction": ("git stash drop", False),
    "block-worktree-creation": ("git worktree add ../y", False),
    "block-whole-filesystem-scan": ("grep -r needle /", False),
    "guard-disk-fill": ("dd if=/dev/zero of=big.bin bs=1M count=999999", False),
    "guard-heavy-command-admission": ("cargo build --release", False),
    "block-external-workspace-vcs-write": ("git -C {other} commit --allow-empty -m x", False),
    "block-editor-kill-by-name": ("taskkill /F /IM UnrealEditor.exe", False),
    "blanket-git-add": ("git -C {repo} add -A", False),
    "check-test-suite-invocation": ("pytest", False),
    "runaway-find": ("find / -name x", False),
    "guard-subagent-heavy-ue-launch": ("UnrealEditor-Cmd.exe proj.uproject -run=Foo", True),
    "block-subagent-destructive-action": ("chmod -R 777 .", True),
}

#: Registered guards that are hook handlers, not Bash-dispatcher entries.
_HOOK_GUARDS = frozenset(
    {"block-dispatch-suite-invocation", "block-worktree-tool", "block-workflow-foreign-emission"}
)


def _registered() -> list:
    return sorted(
        set(deny_registry.GUARD_DENY_CLASS) | set(deny_registry.SUBAGENT_DENY_CLASS)
    )


def _git(root: Path, *args: str) -> None:
    from coordinator_core.win_portability import no_console_creationflags

    subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags()
    )


@pytest.fixture()
def world(tmp_path, monkeypatch):
    registry = tmp_path / "registry"
    registry.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (registry / "registry.local.toml").write_text(
        '[policy]\nnon_owned_remotes = ["github.com/theirs/"]\np4_no_submit = ["//depot"]\n'
        f'[external_workspaces.p4_x]\npath = "{other.as_posix()}"\nvcs = "p4"\n'
        f'[repos]\nproject_x = "{(tmp_path / "repo").as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    machine_resolver._external_ws_cache.clear()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "guard-unlock-home"))
    (tmp_path / "guard-unlock-home").mkdir(exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "docs" / "plans" / "kept.md").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "docs/plans/kept.md")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "remote", "add", "origin", _OWNED)
    _git(repo, "remote", "add", "upstream", _THEIRS)
    (repo / "docs" / "plans" / "p.md").write_text("load-bearing\n", encoding="utf-8")
    (repo / ".git" / "coordinator-sessions" / "peer-session").mkdir(parents=True)
    _git(other, "init", "-q")
    yield {"repo": repo, "other": other}
    machine_resolver._external_ws_cache.clear()


def _deny_reason(out) -> str:
    hso = (out or {}).get("hookSpecificOutput", {})
    if hso.get("permissionDecision") != "deny":
        return ""
    return str(hso.get("permissionDecisionReason", ""))


def _hook_output(name: str, tmp_path: Path) -> dict:
    if name == "block-worktree-tool":
        from coordinator_core.hooks import block_worktree_tool as mod

        return mod._handler({"tool_name": "EnterWorktree", "tool_input": {}})
    if name == "block-dispatch-suite-invocation":
        from coordinator_core.hooks import block_dispatch_suite_invocation as mod

        return mod._handler(
            {
                "tool_name": "Agent",
                "tool_input": {"prompt": "Run the full suite now:\n\npytest\n"},
                "cwd": str(tmp_path),
                "env": {},
            }
        )
    from coordinator_core.hooks import block_workflow_foreign_emission as mod

    return mod._handler(
        {
            "tool_name": "Workflow",
            "tool_input": {"script": 'agent({ prompt: "do it" });\n'},
            "cwd": str(tmp_path),
            "env": {},
        }
    )


def test_every_registered_guard_has_a_case():
    cased = set(_BASH_CASES) | _HOOK_GUARDS
    assert set(_registered()) - cased == set(), "registry entries with no live-input case"
    assert cased - set(_registered()) == set(), "cases for guards no longer registered"


@pytest.mark.parametrize("guard", _registered())
def test_registered_guard_denies_a_live_harmful_input(guard, world, tmp_path):
    if guard in _HOOK_GUARDS:
        out = _hook_output(guard, tmp_path)
    else:
        cmd, subagent = _BASH_CASES[guard]
        if guard == "p4-verb-fence":
            (world["repo"] / "coordinator.local.md").write_text(
                "---\nvcs_mirror: p4\n---\n", encoding="utf-8"
            )
        cmd = cmd.format(repo=Path(world["repo"]).as_posix(), other=Path(world["other"]).as_posix())
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": cmd},
            "session_id": f"sess-deny-{guard}",
            "tool_use_id": f"tu-{guard}",
            "cwd": str(world["repo"]),
        }
        if subagent:
            payload["agent_id"] = _AGENT_ID
            payload["agent_type"] = "executor"
        out = dispatch.evaluate_payload_json(json.dumps(payload))
    reason = _deny_reason(out)
    assert reason, f"{guard}: a live harmful input was not denied: {json.dumps(out)[:300]}"
    if guard not in _HOOK_GUARDS:
        assert f"`{guard}`" in reason, f"{guard}: denied by another guard: {reason[-160:]}"
