"""Tests for `warm.hook_http.evaluate_cold` -- C3 of docs/plans/2026-09-01-a-guard-that-
cannot-reach-warmth-still-r.md.

Purpose: before this function existed, a caller holding a hook payload and no reachable
listener had no in-process route to a real guard verdict at all -- it either shelled out
to the cold CLI (paying a full interpreter start on the box's worst day) or, per the
problem this plan opens with, silently treated the unreachable engine as an unevaluated
command. `evaluate_cold` closes that gap: the same chain the served path runs, called
directly, with no socket and no subprocess.

Mirrors `coordinator_core/ops/tests/test_warm_guard_evaluate.py`'s own fixtures
deliberately -- `_NO_VERIFY_CMD`, `_event`, and the non-repo `cwd` default exist there for
reasons restated in that module's docstring (isolating `check_no_verify`'s verdict from
every other guard in the real chain); duplicating them here rather than importing is a
readability trade for a small, self-contained test file, not a drift risk -- both files
pin the SAME guard against the SAME real chain, so a change to one guard's behaviour
would surface identically in both.

Negative-spec: this file never calls `evaluate_cold` with a monkeypatched
`evaluate_payload_json` -- the whole point is that the REAL chain runs, in process,
producing the SAME verdict `_warm_guard_evaluate` (the served path) produces for the
identical payload.
"""

from __future__ import annotations

import asyncio

from coordinator_core.ops import warm_guard_evaluate
from coordinator_core.warm import hook_http

#: See `test_warm_guard_evaluate.py`'s own docstring for why this exact command: scoped
#: (`-- foo.py`) so the unconditional bare-commit advisory-deny never fires alongside it.
_NO_VERIFY_CMD = "git commit --no-verify -m x -- foo.py"


def _event(cmd: str, *, env: dict | None = None, session_id: str = "s-cold-guard") -> dict:
    # `cwd` deliberately outside this repo's own working tree -- see
    # `test_warm_guard_evaluate.py::_event` for why a real repo `cwd` would let an
    # unrelated guard fire and confound the assertion.
    return {
        "hook_event_name": "PreToolUse",
        "session_id": session_id,
        "cwd": "C:/Windows/Temp",
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "env": env or {},
    }


def _served(event: dict) -> dict:
    """The served-path verdict for the same event, via the real registered op --
    what `evaluate_cold` must match for the same payload."""
    payload = hook_http.payload_from_event(event)
    return asyncio.run(warm_guard_evaluate._warm_guard_evaluate({"payload": payload}))


class TestColdMatchesServed:
    def test_a_denied_event_matches_the_served_verdict(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)
        event = _event(_NO_VERIFY_CMD)

        cold_body = hook_http.evaluate_cold(event)
        served_result = _served(event)

        assert served_result.get("permissionDecision") == "deny"
        hso = cold_body["hookSpecificOutput"]
        assert hso["permissionDecision"] == "deny"
        assert hso["permissionDecisionReason"] == served_result["permissionDecisionReason"]

    def test_a_no_objection_event_matches_the_served_verdict(self, monkeypatch):
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)
        event = _event("echo probe")

        cold_body = hook_http.evaluate_cold(event)
        served_result = _served(event)

        assert served_result == {}
        assert "permissionDecision" not in cold_body["hookSpecificOutput"]

    def test_caller_override_on_the_event_is_honoured_cold(self, monkeypatch):
        """The same boundary `test_warm_guard_evaluate.py::TestBoundaryDeletion` pins
        for the served path: a per-event override travels with the payload, never off
        this process's own environ (module docstring, obligation 2)."""
        monkeypatch.setenv("COORDINATOR_OVERRIDE_NO_VERIFY", "0")
        event = _event(_NO_VERIFY_CMD, env={"COORDINATOR_OVERRIDE_NO_VERIFY": "1"})

        cold_body = hook_http.evaluate_cold(event)

        assert "permissionDecision" not in cold_body["hookSpecificOutput"]


class TestColdEmitsTheLoudSignal:
    """C2's durable degrade record -- `warm.telemetry.record_degrade` -- must fire on
    every cold run, unconditionally, because reaching `evaluate_cold` at all already
    means no reachable listener. Monkeypatches the recorder itself rather than reading
    the on-disk `degrade.jsonl` this test file has no `writes:` scope to depend on the
    shape of: the AC this pins is "a cold run calls the durable-signal seam", not the
    row format C2 owns.
    """

    def test_record_degrade_is_called_with_kind_cold_run(self, monkeypatch):
        calls = []

        def _fake_record_degrade(*args, **kwargs):
            calls.append((args, kwargs))

        monkeypatch.setattr(
            "coordinator_core.warm.telemetry.record_degrade", _fake_record_degrade
        )
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        hook_http.evaluate_cold(_event("echo probe"))

        assert len(calls) == 1
        _, kwargs = calls[0]
        assert kwargs.get("kind") == "cold_run"

    def test_record_degrade_fires_on_a_deny_too(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "coordinator_core.warm.telemetry.record_degrade",
            lambda *a, **kw: calls.append(kw),
        )
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        hook_http.evaluate_cold(_event(_NO_VERIFY_CMD))

        assert len(calls) == 1
        assert calls[0].get("kind") == "cold_run"


class TestColdTurnsAwayWhatItCannotServe:
    """Going cold widens where the guard chain runs, never what it answers about.

    `route_for_event`'s own docstring records this defect being fixed once already on the
    served path: the chain reads `tool_name`/`tool_input`, so an event carrying neither came
    back a confident no-objection about a question it was never asked. A second entry into
    the same chain has to make the same discrimination or it reintroduces it cold.
    """

    def test_an_unserveable_event_is_not_answered_with_a_verdict(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "coordinator_core.warm.telemetry.record_degrade",
            lambda *a, **kw: calls.append(kw),
        )

        out = hook_http.evaluate_cold(
            {"hook_event_name": "SessionStart", "session_id": "s-cold-guard"}
        )

        assert out == hook_http.unserved_response("SessionStart")
        assert "permissionDecision" not in out["hookSpecificOutput"]
        assert calls == []

    def test_a_missing_event_name_is_unserveable_and_not_echoed_back(self, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.warm.telemetry.record_degrade", lambda *a, **kw: None
        )

        for event in ({}, {"hook_event_name": None}, {"hook_event_name": 17}):
            out = hook_http.evaluate_cold(event)
            assert out == hook_http.unserved_response(None)
            assert out["hookSpecificOutput"]["hookEventName"] is None

    def test_a_serveable_event_still_reaches_the_chain(self, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.warm.telemetry.record_degrade", lambda *a, **kw: None
        )
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        out = hook_http.evaluate_cold(_event(_NO_VERIFY_CMD))

        assert out != hook_http.unserved_response("PreToolUse")


class TestColdItselfFailingStillLetsTheActProceed:
    """DR-402 rung 3. A caller reaching `evaluate_cold` has already exhausted the warm
    listener, so an exception out of this function lands in that caller's unreachable
    branch and becomes the deny the whole ladder exists to retire -- the mechanism
    observed 2026-08-30, where an `OSError` from an expensive evaluation surfaced inside
    DoE's forwarder as `no live engine backend reachable`.

    Asserts the three properties together, because any one alone is satisfiable by a
    wrong implementation: it must not raise (or the caller denies), it must not carry a
    verdict (a guard that could not run holds none), and it must be durably recorded (or
    rung 3 becomes the silent normal PM ruling 2 forbids).
    """

    @staticmethod
    def _explode(*args, **kwargs):
        raise RuntimeError("chain exploded")

    def test_a_chain_failure_is_not_raised_at_the_caller(self, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.bash_guards.dispatch.evaluate_payload_json", self._explode
        )
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        body = hook_http.evaluate_cold(_event("echo probe"))

        assert isinstance(body, dict)

    def test_a_chain_failure_carries_no_verdict_and_says_so_loudly(self, monkeypatch):
        monkeypatch.setattr(
            "coordinator_core.bash_guards.dispatch.evaluate_payload_json", self._explode
        )
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        body = hook_http.evaluate_cold(_event(_NO_VERIFY_CMD))

        # No verdict: the harness reads absence of `permissionDecision` as no objection,
        # which is rung 3's "the act proceeds". Asserted on the DENY-shaped command
        # specifically -- the case where a wrong implementation is most tempted to keep
        # denying on the strength of the command's shape rather than an evaluation.
        assert "permissionDecision" not in body.get("hookSpecificOutput", {})
        # Loud: the operator sees it and the model is told, so an unrun guard is never
        # indistinguishable from a guard that ran and passed.
        assert "did not run" in body.get("systemMessage", "")
        assert "chain exploded" in body.get("systemMessage", "")

    def test_a_chain_failure_is_durably_recorded_as_its_own_kind(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "coordinator_core.warm.telemetry.record_degrade",
            lambda *a, **kw: calls.append(kw),
        )
        monkeypatch.setattr(
            "coordinator_core.bash_guards.dispatch.evaluate_payload_json", self._explode
        )
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        hook_http.evaluate_cold(_event("echo probe"))

        kinds = [kw.get("kind") for kw in calls]
        # Both rows, in order: the cold run was entered, and then it collapsed. Recording
        # only `cold_run` would report the box as running its guards cold when it is not
        # running them at all -- the same blindness one rung further down.
        assert kinds == ["cold_run", "cold_failed"]
        assert "RuntimeError" in calls[1].get("cause", "")

    def test_a_recorder_failure_cannot_itself_deny_the_box(self, monkeypatch):
        """The instrument may not be the reason the request it describes also fails.
        `record_degrade` is best-effort by its own contract, but this asserts the
        property at THIS call site: rung 3 must survive its own telemetry breaking."""
        monkeypatch.setattr(
            "coordinator_core.warm.telemetry.record_degrade", self._explode
        )
        monkeypatch.delenv("COORDINATOR_OVERRIDE_NO_VERIFY", raising=False)

        body = hook_http.evaluate_cold(_event("echo probe"))

        assert "permissionDecision" not in body.get("hookSpecificOutput", {})
