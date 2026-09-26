
from __future__ import annotations

import re
import pytest

import coordinator_core.ipc as ipc
from coordinator_core.invoke.__main__ import _dispatch_argv


@pytest.fixture(autouse=True)
def _registered_ping_op():
    saved = ipc._REGISTRY.get("test.warm_fail_hard_ping")
    ipc._REGISTRY["test.warm_fail_hard_ping"] = lambda params, ctx=None, repo_root=None: {"pong": True}
    yield
    if saved is None:
        ipc._REGISTRY.pop("test.warm_fail_hard_ping", None)
    else:
        ipc._REGISTRY["test.warm_fail_hard_ping"] = saved


def _run(
    monkeypatch,
    tmp_path,
    *,
    warm_enabled: bool,
    warm_response,
    allow_unstamped: bool,
    boot_wait_secs: str = "0",
    warm_dispatch=None,
):
    """`boot_wait_secs` defaults to "0" -- the bounded boot wait OFF.

    Every test in this file that predates the wait is asserting the
    MISS-TO-REFUSAL policy, not the wait, and a nonzero default would make
    each of them sit through a real deadline to reach the same assertion.
    The wait's own behaviour is covered by the tests at the bottom of this
    file, which set this explicitly."""
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", boot_wait_secs)
    monkeypatch.setattr(ipc, "_unstamped_dispatch_allowed", allow_unstamped)
    # Isolates the warm-miss policy under test from the SEPARATE
    monkeypatch.setattr(ipc, "_is_dispatch_engine_stamped", lambda: True)
    monkeypatch.setattr(
        "coordinator_core.warm.settings.is_warm_enabled", lambda: warm_enabled
    )
    monkeypatch.setattr(
        "coordinator_core.warm.client.try_warm_dispatch",
        warm_dispatch if warm_dispatch is not None else (lambda msg, **kw: warm_response),
    )
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.record_client_boot_wait",
        lambda **kwargs: None,
    )
    argv = ["test.warm_fail_hard_ping", "{}"]
    return _dispatch_argv(argv, str(tmp_path), allow_warm=True)


def test_warm_miss_runs_cold_loudly_when_not_opted_in(monkeypatch, tmp_path):
    """AN UNREACHABLE ENGINE PASSES LOUDLY, NEVER DENIES (PM ruling 2026-09-21,
    replacing the 2026-08-21 refusal). A miss is never a delivered mutation, so
    the op runs cold -- and says so on stderr, because the ruling it replaces
    was against a SILENT slow, not a slow."""
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=False
    )

    assert code == 0
    assert '"pong":true' in stdout.lower().replace(" ", "")
    assert "ENGINE UNREACHABLE" in stderr
    assert "COLD" in stderr
    assert "defect" in stderr, "a slow path must still name itself a defect, not a queue"


def test_a_permanent_reason_runs_cold_at_once_and_names_itself(monkeypatch, tmp_path):
    reason = "warm engine: resolved engine root does not exist: /nowhere/klabauter"
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: reason)

    stdout, stderr, code = _run(
        monkeypatch,
        tmp_path,
        warm_enabled=True,
        warm_response=None,
        allow_unstamped=False,
        boot_wait_secs="60",
    )

    assert code == 0
    assert '"pong":true' in stdout.lower().replace(" ", "")
    assert reason in stderr
    assert "ENGINE UNREACHABLE" in stderr


def test_a_bounded_wait_that_expires_runs_cold_and_reports_what_it_waited(monkeypatch, tmp_path):
    """The duration it ACTUALLY waited is a fact about this call, never an ETA."""
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch,
        tmp_path,
        warm_enabled=True,
        warm_response=None,
        allow_unstamped=False,
        boot_wait_secs="0.3",
    )

    assert code == 0
    assert '"pong":true' in stdout.lower().replace(" ", "")
    assert "no warm server answered within" in stderr


def test_a_delivered_but_unanswered_mutation_is_never_re_run_cold(monkeypatch, tmp_path):
    indeterminate = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32004, "message": "warm dispatch indeterminate: may have COMPLETED"},
    }
    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=indeterminate, allow_unstamped=False
    )

    assert code != 0
    assert "-32004" in stdout
    assert '"pong"' not in stdout, "the op must not have been run cold"
    assert "ENGINE UNREACHABLE" not in stderr


def test_transient_advice_does_not_fabricate_an_eta(monkeypatch, tmp_path):
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=False
    )

    assert not re.search(r"[0-9]+ *(s|sec|second|m|min|minute)[a-z]*\b", stderr), stderr


def test_warm_miss_falls_through_to_cold_when_opted_in(monkeypatch, tmp_path):
    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=True
    )

    assert code == 0
    assert '"pong":true' in stdout.lower().replace(" ", "")


def test_warm_hit_never_reaches_the_policy_check(monkeypatch, tmp_path):
    served = {"jsonrpc": "2.0", "id": 1, "result": {"pong": "warm"}}
    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=served, allow_unstamped=False
    )

    assert code == 0
    assert "warm" in stdout


def test_bounded_wait_returns_a_server_that_comes_up_mid_wait(monkeypatch, tmp_path):
    calls = {"n": 0}

    def _late(msg, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            return None
        return {"jsonrpc": "2.0", "id": 1, "result": {"pong": "warm-after-boot"}}

    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch,
        tmp_path,
        warm_enabled=True,
        warm_response=None,
        warm_dispatch=_late,
        allow_unstamped=False,
        boot_wait_secs="5",
    )

    assert code == 0
    assert "warm-after-boot" in stdout
    assert "waiting up to" in stderr, "a wait must announce itself, never be silent"


def _boot_wait_harness(monkeypatch, *, mutating, dispatch):
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0.3")
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)
    monkeypatch.setattr("coordinator_core.warm.client._op_may_mutate", lambda m: mutating)
    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", dispatch)
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.record_client_boot_wait", lambda **kwargs: None
    )


def test_boot_wait_cannot_outrun_its_bound_when_an_attempt_blocks(monkeypatch):
    import time

    from coordinator_core.invoke.__main__ import _wait_for_warm_boot

    deadlines = []

    def _blocks_to_its_deadline(msg, *, read_deadline_secs=None):
        deadlines.append(read_deadline_secs)
        time.sleep(30 if read_deadline_secs is None else read_deadline_secs)
        return None

    _boot_wait_harness(monkeypatch, mutating=False, dispatch=_blocks_to_its_deadline)

    t0 = time.monotonic()
    response, waited = _wait_for_warm_boot({"jsonrpc": "2.0", "id": 1, "method": "x"})
    elapsed = time.monotonic() - t0

    assert response is None
    assert deadlines and all(d is not None and 0 < d <= 0.3 for d in deadlines)
    assert waited <= 0.3 + 0.1
    assert elapsed <= 0.3 + 0.1


def test_boot_wait_polls_a_mutation_with_ping_and_sends_it_once(monkeypatch):
    from coordinator_core.invoke.__main__ import _wait_for_warm_boot

    calls = []

    def _dispatch(msg, *, read_deadline_secs=None):
        calls.append((msg["method"], read_deadline_secs))
        if msg["method"] == "ping":
            return None if len(calls) < 2 else {"jsonrpc": "2.0", "id": 1, "result": {}}
        return {"jsonrpc": "2.0", "id": 1, "result": {"appended": True}}

    _boot_wait_harness(monkeypatch, mutating=True, dispatch=_dispatch)

    response, _ = _wait_for_warm_boot(
        {"jsonrpc": "2.0", "id": 1, "method": "queue.append", "params": {}}
    )

    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"appended": True}}
    assert [m for m, _ in calls] == ["ping", "ping", "queue.append"]
    assert all(d is not None for m, d in calls if m == "ping")
    assert calls[-1][1] is None


def test_boot_wait_never_sends_a_mutation_the_server_did_not_answer_for(monkeypatch):
    from coordinator_core.invoke.__main__ import _wait_for_warm_boot

    methods = []

    def _dispatch(msg, *, read_deadline_secs=None):
        methods.append(msg["method"])
        return None

    _boot_wait_harness(monkeypatch, mutating=True, dispatch=_dispatch)

    response, _ = _wait_for_warm_boot(
        {"jsonrpc": "2.0", "id": 1, "method": "queue.append", "params": {}}
    )

    assert response is None
    assert methods and set(methods) == {"ping"}


def _boot_wait_harness(monkeypatch, *, mutating, dispatch):
    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0.3")
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)
    monkeypatch.setattr("coordinator_core.warm.client._op_may_mutate", lambda m: mutating)
    monkeypatch.setattr("coordinator_core.warm.client.try_warm_dispatch", dispatch)
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.record_client_boot_wait", lambda **kwargs: None
    )


def test_boot_wait_cannot_outrun_its_bound_when_an_attempt_blocks(monkeypatch):
    import time

    from coordinator_core.invoke.__main__ import _wait_for_warm_boot

    deadlines = []

    def _blocks_to_its_deadline(msg, *, read_deadline_secs=None):
        deadlines.append(read_deadline_secs)
        time.sleep(30 if read_deadline_secs is None else read_deadline_secs)
        return None

    _boot_wait_harness(monkeypatch, mutating=False, dispatch=_blocks_to_its_deadline)

    t0 = time.monotonic()
    response, waited = _wait_for_warm_boot({"jsonrpc": "2.0", "id": 1, "method": "x"})
    elapsed = time.monotonic() - t0

    assert response is None
    assert deadlines and all(d is not None and 0 < d <= 0.3 for d in deadlines)
    assert waited <= 0.3 + 0.1
    assert elapsed <= 0.3 + 0.1


def test_boot_wait_polls_a_mutation_with_ping_and_sends_it_once(monkeypatch):
    from coordinator_core.invoke.__main__ import _wait_for_warm_boot

    calls = []

    def _dispatch(msg, *, read_deadline_secs=None):
        calls.append((msg["method"], read_deadline_secs))
        if msg["method"] == "ping":
            return None if len(calls) < 2 else {"jsonrpc": "2.0", "id": 1, "result": {}}
        return {"jsonrpc": "2.0", "id": 1, "result": {"appended": True}}

    _boot_wait_harness(monkeypatch, mutating=True, dispatch=_dispatch)

    response, _ = _wait_for_warm_boot(
        {"jsonrpc": "2.0", "id": 1, "method": "queue.append", "params": {}}
    )

    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"appended": True}}
    assert [m for m, _ in calls] == ["ping", "ping", "queue.append"]
    assert all(d is not None for m, d in calls if m == "ping")
    assert calls[-1][1] is None


def test_boot_wait_never_sends_a_mutation_the_server_did_not_answer_for(monkeypatch):
    from coordinator_core.invoke.__main__ import _wait_for_warm_boot

    methods = []

    def _dispatch(msg, *, read_deadline_secs=None):
        methods.append(msg["method"])
        return None

    _boot_wait_harness(monkeypatch, mutating=True, dispatch=_dispatch)

    response, _ = _wait_for_warm_boot(
        {"jsonrpc": "2.0", "id": 1, "method": "queue.append", "params": {}}
    )

    assert response is None
    assert methods and set(methods) == {"ping"}


def test_boot_wait_knob_parsing(monkeypatch):
    from coordinator_core.invoke.__main__ import (
        WARM_BOOT_WAIT_SECS,
        _warm_boot_wait_deadline,
    )

    monkeypatch.delenv("COORDINATOR_WARM_BOOT_WAIT_SECS", raising=False)
    assert _warm_boot_wait_deadline() == WARM_BOOT_WAIT_SECS

    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "0")
    assert _warm_boot_wait_deadline() == 0.0

    monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", "2.5")
    assert _warm_boot_wait_deadline() == 2.5

    for bad in ("", "   ", "soon", "-4"):
        monkeypatch.setenv("COORDINATOR_WARM_BOOT_WAIT_SECS", bad)
        assert _warm_boot_wait_deadline() == WARM_BOOT_WAIT_SECS, bad


def test_warm_disabled_still_falls_through_to_cold(monkeypatch, tmp_path):
    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=False, warm_response=None, allow_unstamped=False
    )

    assert code == 0
    assert '"pong":true' in stdout.lower().replace(" ", "")

