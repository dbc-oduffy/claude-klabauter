"""Ratchet: nothing denies recoverable work unless deny_registry names its irreversible class."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from coordinator_core import deny_registry as dr
from coordinator_core import machine_profile as mp

ROOT = Path(dr.__file__).resolve().parent
# Discovery covers only modules declaring CLASS = "hard-deny"; a deny emitted elsewhere is
# held by the apply_guard_level clamp tests below, not by this scan.
_CLASS_HARD_DENY = re.compile(r'^CLASS\s*=\s*["\']hard-deny["\']', re.M)


@pytest.fixture(autouse=True)
def _empty_registry(tmp_path, monkeypatch):
    d = tmp_path / "reg"
    d.mkdir()
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(d))
    for key in list(os.environ):
        if key.startswith("MACHINE_LOCAL_COORDINATOR_"):
            monkeypatch.delenv(key)
    mp.reset_cache()
    yield
    mp.reset_cache()


def _denies(out):
    """True when the guard-level output still carries a hard deny (a nudge line is not one)."""
    if not out:
        return False
    spec = out.get("hookSpecificOutput", {})
    return spec.get("permissionDecision") == "deny" or out.get("decision") == "block"


def _deny(event="PreToolUse"):
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "permissionDecision": "deny",
            "permissionDecisionReason": "BLOCKED: x",
        }
    }


def _kebab_names_in_tree():
    names = {p.stem.replace("_", "-") for p in (ROOT / "write_guards").glob("*.py")}
    names |= {p.stem.replace("_", "-") for p in (ROOT / "hooks").rglob("*.py")}
    from coordinator_core.bash_guards.roster import guard_roster

    names |= {e.id for e in guard_roster()}
    return names


def test_registry_is_internally_consistent():
    assert dr.registry_problems() == []


def test_every_registered_guard_names_a_real_guard():
    known = _kebab_names_in_tree()
    unknown = sorted(n for n in (*dr.GUARD_DENY_CLASS, *dr.SUBAGENT_DENY_CLASS) if n not in known)
    assert not unknown, f"registry names no guard: {unknown}"


def test_floor_views_derive_from_the_registry():
    assert mp.FLOOR_GUARDS == frozenset(dr.GUARD_DENY_CLASS)
    assert mp.SUBAGENT_FLOOR_GUARDS == frozenset(dr.SUBAGENT_DENY_CLASS)


def test_registered_guard_keeps_its_deny_at_every_level_and_unregistered_never_denies(monkeypatch):
    registered = sorted(dr.GUARD_DENY_CLASS)
    unregistered = sorted(n for n in _kebab_names_in_tree() if n not in dr.GUARD_DENY_CLASS)
    assert registered and unregistered
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        mp.reset_cache()
        for name in registered:
            deny = _deny()
            assert mp.apply_guard_level(name, deny) is deny, (name, level)
        for name in unregistered:
            assert not _denies(mp.apply_guard_level(name, _deny())), (name, level)
            assert not _denies(mp.apply_guard_level(name, _deny("Stop"))), (name, level)


def test_every_hard_deny_class_module_is_registered_or_clamped():
    declared = []
    for sub in ("bash_guards", "write_guards", "hooks"):
        for path in sorted((ROOT / sub).rglob("*.py")):
            if "tests" in path.parts:
                continue
            if _CLASS_HARD_DENY.search(path.read_text(encoding="utf-8")):
                declared.append(path.stem.replace("_", "-"))
    assert declared
    for name in declared:
        if name in dr.GUARD_DENY_CLASS or name in dr.SUBAGENT_DENY_CLASS:
            continue
        assert mp.resolved_level(name) == "log", name
        assert not _denies(mp.apply_guard_level(name, _deny())), name


def test_every_op_refusal_is_classed_or_pending_a_named_fix():
    sites = dr.op_refusal_sites(ROOT / "ops")
    assert len({s.rsplit(":", 1)[0] for s in sites}) >= 20
    assert dr.registry_problems(refusal_sites=sites) == []


def test_every_registered_op_refusal_still_exists():
    sites = dr.op_refusal_sites(ROOT / "ops")
    stale = sorted(k for k in dr.OP_REFUSAL_CLASS if k not in sites)
    assert not stale, f"registry entries with no raise site: {stale}"


def test_injected_unregistered_op_refusal_turns_the_ratchet_red(tmp_path):
    ops = tmp_path / "ops"
    ops.mkdir()
    (ops / "newop.py").write_text("def f():\n    raise NewThingRefused('x')\n", encoding="utf-8")
    sites = dr.op_refusal_sites(ops, package="coordinator_core.ops")
    assert list(sites) == ["coordinator_core.ops.newop:NewThingRefused"]
    problems = dr.registry_problems(refusal_sites=sites)
    assert [p[0] for p in problems] == list(sites)


def test_injected_test_file_refusal_is_not_a_site(tmp_path):
    ops = tmp_path / "ops"
    (ops / "tests").mkdir(parents=True)
    (ops / "tests" / "t.py").write_text("raise FooRefused()\n", encoding="utf-8")
    (ops / "test_x.py").write_text("raise FooRefused()\n", encoding="utf-8")
    assert dr.op_refusal_sites(ops) == {}


def test_a_class_outside_the_closed_list_is_red():
    bad_guard = {"block-new-thing": "convenience"}
    assert [p[0] for p in dr.registry_problems(guard_classes=bad_guard)] == ["block-new-thing"]
    bad_op = {"m:XRefused": "convenience", "m:YRefused": {"pending_fix": ""}, "m:ZRefused": {"owner": "C1"}}
    assert sorted(p[0] for p in dr.registry_problems(op_classes=bad_op)) == [
        "m:XRefused",
        "m:YRefused",
        "m:ZRefused",
    ]


def test_disruptive_is_a_guard_class_and_not_an_op_class():
    assert dr.DISRUPTIVE in dr.GUARD_CLASSES
    assert dr.DISRUPTIVE not in dr.IRREVERSIBLE_CLASSES
    assert dr.registry_problems(op_classes={"m:XRefused": dr.DISRUPTIVE})


def test_post_fire_and_foreign_write_guards_are_not_registered():
    gone = {
        "block-em-post-fire-product-write",
        "block-topic-branch",
        "block-approval-sentinel-creation",
        "bump-foreign-repo-write",
        "block-unreal-engine-resave",
        "block-consumed-handoff-edit",
    }
    assert not gone & (set(dr.GUARD_DENY_CLASS) | set(dr.SUBAGENT_DENY_CLASS))
    assert not gone & (mp.FLOOR_GUARDS | mp.SUBAGENT_FLOOR_GUARDS)


def test_blanket_git_add_denies_at_every_level_and_names_the_commit_op(monkeypatch, tmp_path):
    from coordinator_core.bash_guards import _dc_git_probe, _dc_git_shared
    from coordinator_core.bash_guards import dispatch_checks as guard
    from coordinator_core.guard_messages.testing import envelope_of

    assert dr.GUARD_DENY_CLASS["blanket-git-add"] == dr.DISRUPTIVE
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setattr(_dc_git_shared, "_is_hazard_repo", lambda r: True)

    def fake_git(args, cwd=None, timeout=2.0, extra_env=None):
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return 0, str(root) + "\n"
        if args[:2] == ["add", "--dry-run"]:
            return 0, "add 'src/peer.py'\n"
        raise AssertionError(args)

    monkeypatch.setattr(_dc_git_probe, "_run_git", fake_git)
    result = guard.check_blanket_git_add("git add -A", session_id="s1", hook_payload={})
    assert result is not None
    env = envelope_of(result)
    assert "ceremony.commit_v2" in env["hookSpecificOutput"]["permissionDecisionReason"]
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        mp.reset_cache()
        assert mp.apply_guard_level("blanket-git-add", env) is env, level


def test_subagent_destructive_action_denies_a_subagent_only(monkeypatch):
    from coordinator_core.bash_guards import block_subagent_destructive_action as guard
    from coordinator_core.guard_messages.testing import envelope_of

    assert dr.SUBAGENT_DENY_CLASS["block-subagent-destructive-action"] == dr.BOX_DEGRADING
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git reset --hard HEAD~3"},
        "session_id": "s1",
        "agent_id": "deadbeef0123",
        "agent_type": "coordinator:executor",
    }
    env = envelope_of(guard.check(payload))
    assert env is not None
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        mp.reset_cache()
        assert mp.apply_guard_level("block-subagent-destructive-action", env, subagent=True) is env, level
        assert not _denies(mp.apply_guard_level("block-subagent-destructive-action", env)), level


def test_malformed_request_is_op_only():
    assert dr.MALFORMED_REQUEST not in dr.GUARD_CLASSES
    assert [p[0] for p in dr.registry_problems(guard_classes={"g": dr.MALFORMED_REQUEST})] == ["g"]


def test_injected_unregistered_deny_is_clamped_to_a_log(monkeypatch):
    monkeypatch.setitem(dr.GUARD_DENY_CLASS, "block-injected-new-deny", dr.BOX_DEGRADING)
    assert "block-injected-new-deny" not in mp.FLOOR_GUARDS
    assert not _denies(mp.apply_guard_level("block-injected-new-deny", _deny()))
    assert not _denies(mp.apply_guard_level("block-injected-new-deny", _deny(), session_id="s"))


def test_unregistered_deny_writes_a_record_and_no_context(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    shared = tmp_path / "share"
    monkeypatch.setattr(
        "coordinator_core.guard_advisory_counter._share_dir", lambda root, sid: str(shared / sid)
    )
    out = mp.apply_guard_level("block-some-demoted-guard", _deny(), session_id="sess-1", cwd=str(tmp_path))
    assert not _denies(out)
    record = (shared / "sess-1" / "advisory-fire-counts.jsonl").read_text(encoding="utf-8")
    assert '"guard": "block-some-demoted-guard"' in record


def test_a_failing_record_never_reaches_the_caller(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("coordinator_core.guard_advisory_counter.record_advisory_fire", boom)
    out = mp.apply_guard_level("block-some-demoted-guard", _deny(), session_id="s", cwd="/nonexistent")
    assert not _denies(out)


def test_dispatch_drops_a_crashed_unregistered_guard_and_keeps_a_registered_one_denying():
    from coordinator_core.bash_guards import dispatch

    crash = dispatch._crash_deny("block-some-demoted-guard", RuntimeError("x"))
    assert not _denies(dispatch._apply_guard_level("block-some-demoted-guard", crash))
    crash = dispatch._crash_deny("destructive-rm", RuntimeError("x"))
    assert dispatch._apply_guard_level("destructive-rm", crash) is crash


@pytest.mark.parametrize("cmd", ["python -m pytest $FILES", "pytest $F -q", "python -m pytest $(cat list.txt)"])
def test_unscheduled_suite_run_with_variable_held_paths_denies_at_every_level(monkeypatch, tmp_path, cmd):
    import subprocess

    from coordinator_core.bash_guards import check_test_suite_invocation as guard

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert dr.GUARD_DENY_CLASS["check-test-suite-invocation"] == dr.BOX_DEGRADING
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s", "cwd": str(tmp_path)}
    env = guard.check(payload)
    assert env is not None
    for level in ("strict", "warn", "off"):
        monkeypatch.setenv("MACHINE_LOCAL_COORDINATOR_GUARD_LEVEL", level)
        mp.reset_cache()
        assert mp.apply_guard_level("check-test-suite-invocation", env) is env, level
