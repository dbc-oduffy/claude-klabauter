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

import re
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
        "coordinator_core.warm.client.try_warm_dispatch",
        warm_dispatch if warm_dispatch is not None else (lambda msg: warm_response),
    )
    # The boot-wait instrument appends to the REAL per-clone runtime directory.
    # A test's synthetic wait is not a sample of this box's boot, and letting one
    # land would corrupt the only evidence that can settle how long boot takes.
    monkeypatch.setattr(
        "coordinator_core.warm.telemetry.record_client_boot_wait",
        lambda **kwargs: None,
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


def test_transient_miss_names_the_defect_rather_than_a_wait(monkeypatch, tmp_path):
    """The other side of the same branch: with no permanent reason recorded the
    miss IS transient and a respawn is in flight -- but the advice must say how
    long that actually takes.

    WHY THIS ASSERTION CHANGED TWICE. It first pinned the words "retry in a
    moment"; four sessions read that as seconds and concluded the fault was
    permanent. The first correction pinned "MINUTES" instead -- and that was
    WORSE, for two reasons. It was not a measurement: the +0s/+30s/+4min
    samples behind it were the intervals a human CHOSE to retry at, so all the
    +4min point establishes is that the server was up by then. Nobody has ever
    measured this box's boot time. And stating it as a fact made an
    over-budget path read as the designed cadence -- the "the box was busy"
    answer CLAUDE.md forbids.

    Reaching the engine is budgeted in hundreds of milliseconds. A wait long
    enough to notice is a P0, so the message must name it as a defect and must
    NOT instruct anyone to wait it out. That is what this now pins."""
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=False
    )

    assert code != 0
    assert "warm dispatch unavailable" in stderr
    assert "THIS IS A DEFECT" in stderr, "an over-budget path must not read as a queue"
    # With the wait switched off (this helper's default), the message must say
    # so rather than claim a wait that never happened.
    assert "COORDINATOR_WARM_BOOT_WAIT_SECS=0" in stderr
    # Every phrasing that caused a misdiagnosis, pinned absent so none returns.
    assert "retry in a moment" not in stderr
    assert "MINUTES" not in stderr, "unmeasured, and it made a P0 read as cadence"
    assert "waiting it out" not in stderr


def test_transient_advice_does_not_assert_a_wedged_server_or_name_a_dead_knob(
    monkeypatch, tmp_path
):
    """The message may report what it observed; it may NOT diagnose the server,
    and it may not name an override the caller cannot reach.

    This process sees exactly one thing: its own dispatch did not land. On
    2026-09-01 (session 9b6b537a) one caller hit this branch five consecutive
    times while peers committed successfully through the same route in the
    same minutes, and while that caller's very next command succeeded -- the
    server was up throughout, so "the server is wedged or crash-looping" was
    never an observation. The old wording also named
    `COORDINATOR_WARM_BOOT_WAIT_SECS` bare; a reader who set it in their own
    shell got the identical message back (measured 2026-09-01: set to 20,
    five consecutive failures still reported 0) -- the value is read from the
    process this door runs in, not the caller's shell."""
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=False
    )

    assert code != 0
    # 1. Never asserted as this process's conclusion.
    assert "the server is wedged or crash-looping" not in stderr, (
        "this process cannot observe the server's state for other callers"
    )
    # 2. The discriminator must be named, or the reader has nothing to check.
    assert "the fault is local to this caller" in stderr
    # 3. The hatch stays gated and its blast radius stated.
    assert "warm-engine-stop" in stderr, "the hatch is still reachable when warranted"
    assert "do NOT restart it" in stderr, "the healthy-server case must be called out"
    assert "last rung, not the second" in stderr
    # 4. The boot-wait knob is named as unreachable from the caller's shell.
    assert "COORDINATOR_WARM_BOOT_WAIT_SECS=0" in stderr
    assert "will NOT change this" in stderr, (
        "a reader who sets it in their own shell must be told it has no effect here"
    )
    assert "only hook children should" in stderr, (
        "the message must point at the launcher, not at the server"
    )


def test_transient_advice_does_not_fabricate_an_eta(monkeypatch, tmp_path):
    """Order of magnitude, never a number this process cannot observe.

    Boot time is load-dependent and nothing here measures it. A specific ETA
    would be the same defect as "a moment" with more decimal places -- an
    operator who is told 90 seconds and waits 90 seconds draws exactly the
    wrong conclusion again."""
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch, tmp_path, warm_enabled=True, warm_response=None, allow_unstamped=False
    )

    assert not re.search(r"[0-9]+ *(s|sec|second|m|min|minute)[a-z]*\b", stderr), stderr


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


def test_bounded_wait_returns_a_server_that_comes_up_mid_wait(monkeypatch, tmp_path):
    """THE P0 THIS CLOSES (2026-08-25/26). A miss triggers a respawn on its way
    out, so the call that gets refused is the one whose fix is already in
    flight. Refusing anyway made the retry interval a human's guess, and the
    guess was unbounded: `cross-repo-memo send` took four minutes and three
    hand-retries to move one file, and four sessions lost an evening's memo
    traffic to it.

    A server that starts answering DURING the bound must serve the call, not
    watch it fail."""
    calls = {"n": 0}

    def _late(msg):
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


def test_bounded_wait_still_fails_hard_on_expiry(monkeypatch, tmp_path):
    """NOT BACKSTOP 2. The wait ends in a refusal, never in a cold spawn: what
    the PM retired was a SILENT degrade to cold on every miss, and this waits
    for the WARM server, once, announced, then fails.

    The refusal reports the duration it ACTUALLY waited -- a fact about this
    call, which is the opposite of the ETA the negative-spec forbids."""
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch,
        tmp_path,
        warm_enabled=True,
        warm_response=None,
        allow_unstamped=False,
        boot_wait_secs="0.3",
    )

    assert code != 0
    assert stdout == "", "expiry must not fall through to cold dispatch"
    assert "THIS IS A DEFECT" in stderr
    assert "without the warm server accepting connections" in stderr
    assert "--allow-unstamped-dispatch" in stderr


def test_bounded_wait_aborts_early_on_a_permanent_reason(monkeypatch, tmp_path):
    """A reason established mid-wait recurs identically on every poll, so
    waiting the bound out would spend it reaching a conclusion already in hand.
    The permanent reason leads, and the retry advice stays absent."""
    reason = "warm engine: resolved engine root does not exist: /nowhere/klabauter"
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: reason)

    stdout, stderr, code = _run(
        monkeypatch,
        tmp_path,
        warm_enabled=True,
        warm_response=None,
        allow_unstamped=False,
        boot_wait_secs="60",  # never reached: the abort fires on the first poll
    )

    assert code != 0
    assert reason in stderr
    assert "retrying will not clear this" in stderr


def test_boot_wait_knob_parsing(monkeypatch):
    """`0` is the only way to switch the wait off. A malformed or negative
    value falls back to the default rather than silently disabling it --
    a knob nobody can read must not be a knob that turns a safeguard off."""
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


def test_wedge_refusal_names_what_to_do_not_only_what_not_to_do(monkeypatch, tmp_path):
    """example-game-repo-em read this refusal, did exactly what it said (checked for a
    wedged server, found only the respawn it had just triggered), and was left
    with nothing to act on -- while the move that worked was the one the
    message never named: re-issue the call, which then returned in 2.9s.

    A refusal that names a defect and then only forbids ("rather than retrying
    by hand") is the shape defect 7 of `cross-repo/inbox/2026-09-01-example-game-repo-
    em-close-ceremony-engine-defects-seven.md` is about. The remedy must be
    positive and runnable.
    """
    monkeypatch.setattr("coordinator_core.warm.client.last_cold_reason", lambda: None)

    stdout, stderr, code = _run(
        monkeypatch,
        tmp_path,
        warm_enabled=True,
        warm_response=None,
        allow_unstamped=False,
        boot_wait_secs="0.3",
    )

    assert code != 0
    assert stdout == ""
    # The first move, which is what actually recovered it in the field.
    assert "Re-issue this same command once" in stderr
    # The second move, named as a RUNNABLE per the cold-path rule -- what fires
    # before a session exists cannot be fixed by a slash command.
    assert "warm-engine-stop" in stderr
    assert "/warm-engine-stop" not in stderr, "must be a runnable, never a slash command"
    # The prohibition survives, but it is no longer the only guidance present.
    assert "hand-roll" in stderr
