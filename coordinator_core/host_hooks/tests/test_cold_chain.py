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


@pytest.mark.parametrize("command,expected", [
    ("ls", []),
    ("git status", []),
    ("npm run format && echo confirm", []),
    ("git commit -m 'update notes'", []),
    ("rm -rf build", ["destructive-rm"]),
    ("'r''m' -rf build", ["destructive-rm"]),
    ('sh -c "rm -rf build"', ["destructive-rm"]),
    ("cd x && /usr/bin/git clean -fdx", ["destructive-git-clean"]),
    ("git clean --force -d", ["destructive-git-clean"]),
    ("git clean -n", []),
    ("git clean", []),
    ("git -C x stash clear", ["block-stash-destruction"]),
    ("Remove-Item -Recurse -Force build", ["destructive-rm"]),
    ("dd if=/dev/zero of=out bs=1M count=10", ["guard-disk-fill"]),
    ("truncate -s 10G big.bin", ["guard-disk-fill"]),
])
def test_prefilter_names_the_classes_a_command_could_match(command: str, expected: list) -> None:
    assert cold_chain.shell_candidates(command) == expected


def test_harmless_command_imports_and_runs_no_guard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("a guard ran for a harmless command")

    monkeypatch.setattr(cold_chain, "_invoke_shell", boom)
    assert cold_chain.evaluate(_bash("ls", tmp_path)) == {}
    assert cold_chain.evaluate(_bash("git status", tmp_path)) == {}


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

    for guard in cold_chain.SHELL_DENY_GUARDS + (cold_chain.SUBAGENT_DENY_GUARD,) + cold_chain.PATCH_DENY_GUARDS:
        assert GUARD_DENY_CLASS[guard] == BOX_DEGRADING, guard


def test_subagent_draws_the_subagent_guard_only_for_git_commands() -> None:
    assert cold_chain.shell_candidates("git clean -n", subagent=True) == ["block-subagent-destructive-action"]
    assert cold_chain.shell_candidates("git clean -n") == []
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


def test_apply_patch_reaches_every_deny_class_write_guard() -> None:
    import coordinator_core.write_guards as write_guards
    from coordinator_core.deny_registry import GUARD_DENY_CLASS, IRREVERSIBLE_CLASSES

    modules = {p.stem.replace("_", "-") for p in Path(write_guards.__file__).parent.glob("*.py")}
    denying = {g for g, cls in GUARD_DENY_CLASS.items() if g in modules and cls in IRREVERSIBLE_CLASSES}
    assert denying == set(cold_chain.PATCH_DENY_GUARDS)
