"""The static guard table (`guard_table.ROWS`) matches the guards it describes, and pruning never drops a guard its command can trigger."""
import importlib
import json

import pytest

from coordinator_core.bash_guards import dispatch, guard_table
from coordinator_core.host_hooks import cold_chain

PKG = "coordinator_core.bash_guards."
PRUNED = sorted(name for name, row in guard_table.ROWS.items() if row.triggers is not guard_table.ALWAYS)


@pytest.mark.parametrize("name", sorted(guard_table.ROWS))
def test_a_row_names_its_modules_declared_matchers(name):
    row = guard_table.ROWS[name]
    assert tuple(importlib.import_module(PKG + row.module).MATCHERS) == row.matchers


def test_the_master_gate_set_is_the_union_of_every_module_matchers():
    union = frozenset(("Bash",)).union(*(importlib.import_module(PKG + r.module).MATCHERS for r in guard_table.ROWS.values()))
    assert guard_table.DECLARED_MATCHERS == union
    assert dispatch._any_declared_matchers() == union


@pytest.mark.parametrize("name", PRUNED)
def test_a_pruned_row_carries_the_guards_crash_triggers(name):
    assert guard_table.ROWS[name].triggers == dispatch._CRASH_TRIGGER_SUBSTRINGS[name]


def test_every_crash_deny_exempt_guard_is_always():
    exempt = {name for name, _why in dispatch._CRASH_DENY_EXEMPT}
    assert not exempt & set(PRUNED), exempt & set(PRUNED)


def test_altering_a_trigger_changes_what_a_command_imports(monkeypatch):
    """Refuse side: the row is the only thing deciding; emptied triggers prune the guard for a command that holds the old one."""
    variants = dispatch._crash_probe_variants("git worktree add x")
    assert guard_table.trigger_wanted("block-worktree-creation", variants)
    row = guard_table.ROWS["block-worktree-creation"]
    monkeypatch.setitem(guard_table.ROWS, "block-worktree-creation", row._replace(triggers=("zzz",)))
    assert not guard_table.trigger_wanted("block-worktree-creation", variants)


def test_an_unknown_guard_is_wanted():
    assert guard_table.trigger_wanted("no-such-guard", ())


@pytest.mark.parametrize("command", ("git worktree add x", "git stash drop", "git push origin main", "p4 submit -c 1", "echo hi"))
def test_the_cold_prefilter_never_selects_a_guard_the_table_prunes(command):
    """One predicate: a guard `cold_chain.SHELL_GUARDS` would evaluate for `command` is also wanted by the table."""
    variants = dispatch._crash_probe_variants(command)
    for name in cold_chain.shell_candidates(command, subagent=False):
        assert guard_table.trigger_wanted(name, variants), (command, name)


@pytest.mark.parametrize("command", ("git status", "git push origin main", "git worktree add x", "git stash drop", "git add -A", "rm -rf build"))
def test_the_pruned_chain_imports_fewer_guards_and_keeps_every_always_guard(command):
    full = dispatch._build_guard_chain(command, "s", ".", {"tool_name": "Bash", "tool_input": {"command": command}}, None, None, None)
    lean = dispatch._build_guard_chain(command, "s", ".", {"tool_name": "Bash", "tool_input": {"command": command}}, None, None, None, prune=True)
    assert [e.name for e in lean] == [e.name for e in full]
    live = {e.name for e in lean if e.matchers}
    variants = dispatch._crash_probe_variants(command)
    for name in PRUNED:
        assert (name in live) == guard_table.trigger_wanted(name, variants), name
    assert all(e.matchers for e in full)
    assert {e.name for e in full} - live


_REAL_BUILD_GUARD_CHAIN = dispatch._build_guard_chain


def _verdict(command, monkeypatch, prune, **identity):
    # The real builder is captured once at import: re-reading it here would wrap the previous call's patch,
    # whose `prune` overrides this one and makes both sides of the comparison the same chain.
    monkeypatch.setattr(
        dispatch, "_build_guard_chain", lambda *a, **k: _REAL_BUILD_GUARD_CHAIN(*a, **{**k, "prune": prune})
    )
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "s-table", "cwd": ".", **identity}
    return json.dumps(dispatch.evaluate_payload_json(json.dumps(payload)), sort_keys=True, default=str)


@pytest.mark.parametrize("identity", ({}, {"agent_id": "a1", "agent_type": "coordinator:executor"}))
@pytest.mark.parametrize(
    "command",
    ("git status", "git push origin main", "git worktree add x", "git stash drop", "git add -A", "rm -rf build", "echo hi", "p4 submit -c 1", "nohup sleep 1 &"),
)
def test_the_pruned_and_full_chain_answer_the_same_verdict(command, identity, monkeypatch):
    monkeypatch.setenv(dispatch._VERDICT_ONLY_ENV, "1")
    assert _verdict(command, monkeypatch, True, **identity) == _verdict(command, monkeypatch, False, **identity)
