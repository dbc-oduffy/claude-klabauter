"""C3: `warm_guard.evaluate` supplies `policy_file` and `resolution_class`.

THE LOAD-BEARING PAIR IN THIS FILE is `test_policy_file_is_recomputed_per_caller` and
`test_resolution_class_is_stable_within_a_process`. They assert OPPOSITE caching behaviour on
purpose, and that asymmetry is the correctness of the change, not an accident anyone should
"simplify" away:

  * `policy_file` is a CALLER fact, derived from the payload's own `plugin_root`. Memoizing it
    would freeze it to whichever session booted this resident server and apply it invisibly to
    every other session on the box -- the same hazard DoE bound their governed-surfaces manifest
    acceptance to ("read per-call, never memoized in the resident server"), one seam over.
  * `resolution_class` classifies THIS ENGINE'S OWN resolution of itself, cannot vary per caller,
    and pays an unconditional shim load per call if it is not memoized (DR-344 hot path -- this
    op runs per Bash tool call).

A test that only pinned the happy path could not see either failure: a frozen `policy_file` and a
correct one are indistinguishable on a single call, which is the whole reason (b) below compares
TWO calls with different roots rather than asserting one call's value.

`evaluate_payload_json` is monkeypatched here to record the kwargs it receives. That is
deliberate and does not contradict the sibling suite's "never a monkeypatched stand-in" rule:
that rule exists so boundary-deletion coverage exercises the REAL chain's env forwarding, whereas
what is under test HERE is precisely which kwargs this op hands across that boundary. Recording
the call is the only way to observe it without asserting on a downstream guard's behaviour.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from coordinator_core.ops import warm_guard_evaluate

_POLICY_BASENAME = "subagent-sandbox-policy.yaml"


@pytest.fixture(autouse=True)
def _clear_resolution_cache():
    """The process-level `resolution_class` memo leaks across tests otherwise."""
    warm_guard_evaluate._RESOLUTION_CLASS = None
    yield
    warm_guard_evaluate._RESOLUTION_CLASS = None


@pytest.fixture
def recorded(monkeypatch):
    calls = []

    def _fake(raw, policy_file=None, host_is_windows=None, resolved=None, resolution_class=None, **kw):
        calls.append({"policy_file": policy_file, "resolution_class": resolution_class})
        return None

    monkeypatch.setattr(warm_guard_evaluate, "evaluate_payload_json", _fake)
    return calls


def _run(payload):
    return asyncio.run(warm_guard_evaluate._warm_guard_evaluate({"payload": payload}))


def _payload(plugin_root=None):
    p = {
        "session_id": "s1",
        "cwd": "/tmp/x",
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "echo hi"},
        "env": {},
    }
    if plugin_root is not None:
        p["plugin_root"] = plugin_root
    return p


def test_policy_file_comes_from_the_payloads_plugin_root(recorded):
    """(a) The path is built under the CALLER's root, not this process's."""
    _run(_payload(plugin_root="/caller/clone/coordinator"))
    got = recorded[0]["policy_file"]
    assert got is not None
    assert Path(got) == Path("/caller/clone/coordinator") / _POLICY_BASENAME


def test_policy_file_is_recomputed_per_caller(recorded):
    """(b) Two callers, two roots, two answers -- the anti-memoization pin.

    This is the test that fails if someone folds `policy_file` into the same process-level
    cache `resolution_class` uses. On a single call a frozen value looks correct.
    """
    _run(_payload(plugin_root="/first/clone/coordinator"))
    _run(_payload(plugin_root="/second/clone/coordinator"))
    first, second = recorded[0]["policy_file"], recorded[1]["policy_file"]
    assert first != second
    assert Path(first) == Path("/first/clone/coordinator") / _POLICY_BASENAME
    assert Path(second) == Path("/second/clone/coordinator") / _POLICY_BASENAME


def test_absent_plugin_root_passes_none_and_does_not_raise(recorded, monkeypatch):
    """(c) A payload miss degrades to today's behaviour, never to a guessed path.

    `resolve_caller_context` falls back to an ambient probe on a payload miss, so the ambient
    leg is stubbed to `None` here to pin what THIS op does with an unresolvable root: pass
    `None`, which makes the reviewer guard use its hardcoded default ruleset -- never an
    empty/unconfined one.
    """
    monkeypatch.setattr(
        warm_guard_evaluate,
        "resolve_caller_context",
        lambda payload: type("_C", (), {"plugin_root": None})(),
    )
    _run(_payload())
    assert recorded[0]["policy_file"] is None


def test_resolution_class_is_stable_within_a_process(recorded, monkeypatch):
    """(d) The opposite assertion to (b): this one IS cached, deliberately.

    Counts resolutions rather than comparing values, so it fails if the memo is removed even
    when the underlying value happens to be constant.
    """
    calls = {"n": 0}

    def _fake_with_class():
        calls["n"] += 1
        return ("/engine", "live-working-tree")

    monkeypatch.setattr(
        "coordinator_core.engine_root.coordinator_engine_root_with_class", _fake_with_class
    )
    _run(_payload(plugin_root="/a/coordinator"))
    _run(_payload(plugin_root="/b/coordinator"))
    assert [c["resolution_class"] for c in recorded] == ["live-working-tree"] * 2
    assert calls["n"] == 1, "resolution_class must resolve once per process, not per Bash call"


def test_unresolvable_resolution_class_degrades_to_none_and_is_negative_cached(recorded, monkeypatch):
    """A failing resolution must not re-pay the shim load on every subsequent Bash call."""
    calls = {"n": 0}

    def _boom():
        calls["n"] += 1
        raise RuntimeError("no engine root here")

    monkeypatch.setattr(
        "coordinator_core.engine_root.coordinator_engine_root_with_class", _boom
    )
    _run(_payload(plugin_root="/a/coordinator"))
    _run(_payload(plugin_root="/b/coordinator"))
    assert [c["resolution_class"] for c in recorded] == [None, None]
    assert calls["n"] == 1, "a failed resolution must be negative-cached, not retried per call"
