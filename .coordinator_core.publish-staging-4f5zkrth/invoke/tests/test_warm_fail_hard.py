"""coordinator_core.invoke.tests.test_warm_fail_hard

Guard tests for the "fail hard, not fail closed" warm-axis policy
(state/handoffs/2026-08-21_103635_reaching-the-warm-engine.md; PM ruling
verbatim: "I'd rather have a fail than a silent slow. Much rather.").

`_dispatch_argv_body` (invoke/__main__.py) must refuse to fall through to
cold dispatch when warm is enabled but a warm miss occurs, UNLESS the caller
opted in via `--allow-unstamped-dispatch` / `ipc.allow_unstamped_dispatch()`.
This retires `warm.client`'s own documented "Backstop 2: the cold path is a
SUCCESS path" for this one caller -- see that module's own docstring for the
retirement notice.

This suite's own `conftest.py::pytest_configure` already calls
`ipc.allow_unstamped_dispatch()` for the whole session, so every test here
that wants to see the FAIL-HARD behaviour must flip
`ipc._unstamped_dispatch_allowed` off itself via `monkeypatch` -- reverts
automatically at that test's own teardown.
"""

from __future__ import annotations

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.invoke.__main__ import _dispatch_argv


@pytest.fixture(autouse=True)
def _registered_ping_op():
    """A trivial op registered for the duration of each test in this file --
    isolates these tests from whatever real ops happen to be registered,
    mirroring `test_dispatch_message.py::_RegistryScope`'s own rationale."""
    saved = ipc._REGISTRY.get("test.warm_fail_hard_ping")
    ipc._REGISTRY["test.warm_fail_hard_ping"] = lambda params, ctx=None, repo_root=None: {"pong": True}
    yield
    if saved is None:
        ipc._REGISTRY.pop("test.warm_fail_hard_ping", None)
    else:
        ipc._REGISTRY["test.warm_fail_hard_ping"] = saved


def _run(monkeypatch, tmp_path, *, warm_enabled: bool, warm_response, allow_unstamped: bool):
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", allow_unstamped)
    # Isolates the fail-hard warm policy under test from the SEPARATE
    # dispatch-axis stamp gate (already covered by test_dispatch_message.py's
    # own gate tests) -- this repo's own tree is genuinely unstamped, so a
    # cold-dispatch assertion here would otherwise fail on the wrong check
    # whenever `allow_unstamped` is False.
    monkeypatch.setattr(ipc, "_is_dispatch_engine_stamped", lambda: True)
    monkeypatch.setattr(
        "coordinator_core.warm.settings.is_warm_enabled", lambda: warm_enabled
    )
    monkeypatch.setattr(
        "coordinator_core.warm.client.try_warm_dispatch", lambda msg: warm_response
    )
    # No --repo: "test.warm_fail_hard_ping" is unregistered in op_scopes.py
    # and therefore "none"-scoped by default, on which --repo is REFUSED
    # (DR-279) rather than silently ignored -- repo_root resolution never
    # runs for a none-scoped op, so `tmp_path` need not be a git checkout.
    argv = ["test.warm_fail_hard_ping", "{}"]
    return _dispatch_argv(argv, str(tmp_path), allow_warm=True)


def test_warm_miss_fails_hard_when_enabled_and_not_opted_in(monkeypatch, tmp_path):
    """THE POLICY ITSELF: warm enabled, warm returned None (a miss), no
    opt-in -> non-zero exit, no cold dispatch attempted (op never ran)."""
    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=False
    )

    assert code != 0
    assert stdout == ""  # never reached the cold dispatch print
    assert "warm dispatch unavailable" in stderr
    assert "--allow-unstamped-dispatch" in stderr


def test_permanent_cold_reason_replaces_the_retry_advice(monkeypatch, tmp_path):
    """THE CONTRADICTION THIS CLOSES (observed 2026-08-22, every settings-home
    `bin/` live op): the client says every call from this tree goes cold, then
    this block says cold fallback is disabled and to retry in a moment --
    neither half naming a path, and retrying never clearing it. When the
    client has established a PERMANENT reason, that reason leads, and the
    retry advice must not appear."""
    reason = "warm engine: resolved engine root does not exist: /nowhere/klabauter"
    monkeypatch.setattr(
        "coordinator_core.warm.client.last_cold_reason", lambda: reason
    )

    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=False
    )

    assert code != 0
    assert stdout == ""
    assert reason in stderr, "the fatal message must carry the path that failed to resolve"
    assert "retry in a moment" not in stderr
    assert "--allow-unstamped-dispatch" in stderr


def test_transient_miss_keeps_the_retry_advice(monkeypatch, tmp_path):
    """The other side of the same branch: with no permanent reason recorded,
    the miss IS transient (server booting, busy, skew-evicted), a respawn is
    already in flight, and "retry in a moment" is the correct remediation."""
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=False
    )

    assert code != 0
    assert "retry in a moment" in stderr


def test_warm_miss_falls_through_to_cold_when_opted_in(monkeypatch, tmp_path):
    """The manual-testing carve-out: the SAME opt-in that bypasses the
    stamp gate also permits cold fallback on a warm miss."""
    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=True
    )

    assert code == 0
    assert '"pong":true' in stdout.lower().replace(" ", "")


def test_warm_hit_never_reaches_the_policy_check(monkeypatch, tmp_path):
    """A served warm response is used as-is -- the fail-hard check only
    fires on a MISS (`None`), never on an actual response (including a
    warm-served error envelope, per this module's own existing contract)."""
    served = {"jsonrpc": "2.0", "id": 1, "result": {"pong": "warm"}}
    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=served, allow_unstamped=False
    )

    assert code == 0
    assert "warm" in stdout


def test_warm_disabled_still_falls_through_to_cold(monkeypatch, tmp_path):
    """Warm turned OFF for this machine (a deliberate configuration choice,
    not degradation) is unaffected by this policy -- cold dispatch proceeds
    exactly as before. `try_warm_dispatch` is never even called in this
    case (see invoke/__main__.py's own W11 comment), so its monkeypatched
    return value here is a no-op sentinel proving that."""
    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=False, warm_response=None, allow_unstamped=False
    )

    assert code == 0
    assert '"pong":true' in stdout.lower().replace(" ", "")
