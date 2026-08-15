"""Tests for `coordinator_core.session.messaging_gate`.

Every case passes an explicit `environ` mapping -- never `monkeypatch.setenv`
-- so no test here mutates process-wide state and none needs the
`allow_environ_leak` marker (`pyproject.toml`). That the classifier takes the
mapping as a parameter is the reason that is possible, and is itself asserted
below.
"""

from __future__ import annotations

from coordinator_core.session import messaging_gate
from coordinator_core.session.messaging_gate import GateState


# ---------------------------------------------------------------------------
# The four states
# ---------------------------------------------------------------------------

def test_absent_variable_is_not_requested():
    gate = messaging_gate.classify({})

    assert gate.state == GateState.NOT_REQUESTED
    assert gate.requested is False
    assert gate.inbox_bound is False


def test_empty_variable_is_declined_not_not_requested():
    # An empty string is the one value that defeats BOTH the launcher's
    # `os.environ.setdefault` and the harness's own truthiness check -- a
    # deliberate opt-out, and a different fact from "the launch chain never
    # set it", which is the fact that identifies a launch-chain defect.
    gate = messaging_gate.classify({messaging_gate.GATE_ENV_VAR: ""})

    assert gate.state == GateState.DECLINED
    assert gate.requested is False


def test_requested_without_a_bound_inbox_is_the_actionable_state():
    # State B: we asked and it did not open. Before this module it was
    # indistinguishable from "nothing asked" -- both rendered as
    # `messaging_available: false`, which three repos read as "the remote
    # GrowthBook flag is still off".
    gate = messaging_gate.classify({messaging_gate.GATE_ENV_VAR: "1"})

    assert gate.state == GateState.REQUESTED_UNBOUND
    assert gate.requested is True
    assert gate.inbox_bound is False


def test_bound_socket_is_open():
    gate = messaging_gate.classify(
        {
            messaging_gate.GATE_ENV_VAR: "1",
            messaging_gate.SOCKET_ENV_VAR: r"\\.\pipe\cc-msg-deadbeef",
        }
    )

    assert gate.state == GateState.OPEN
    assert gate.inbox_bound is True


def test_the_four_states_are_pairwise_distinct():
    states = {
        messaging_gate.classify({}).state,
        messaging_gate.classify({messaging_gate.GATE_ENV_VAR: ""}).state,
        messaging_gate.classify({messaging_gate.GATE_ENV_VAR: "1"}).state,
        messaging_gate.classify(
            {
                messaging_gate.GATE_ENV_VAR: "1",
                messaging_gate.SOCKET_ENV_VAR: "sock",
            }
        ).state,
    }

    assert len(states) == 4


# ---------------------------------------------------------------------------
# Truthiness mirrors the HARNESS predicate, not Python's
# ---------------------------------------------------------------------------

def test_zero_string_requests_the_gate_because_the_predicate_is_js_truthiness():
    # `if (q.CLAUDE_CODE_HARBOR_KITE) return !0;` -- every non-empty JS string
    # is truthy, so "0" opens the gate rather than declining it. Reading "0"
    # as a decline here would report an opt-out the harness never performed.
    gate = messaging_gate.classify({messaging_gate.GATE_ENV_VAR: "0"})

    assert gate.requested is True
    assert gate.state == GateState.REQUESTED_UNBOUND


def test_arbitrary_non_empty_value_requests_the_gate():
    gate = messaging_gate.classify({messaging_gate.GATE_ENV_VAR: "false"})

    assert gate.requested is True


# ---------------------------------------------------------------------------
# Bound-ness wins over request-ness
# ---------------------------------------------------------------------------

def test_bound_inbox_reports_open_even_when_the_variable_was_never_set():
    # The late-bind path: a mid-session GrowthBook refresh binds an inbox
    # without any local request. `open` is a fact about the socket, so it
    # must not be gated on the request signal.
    gate = messaging_gate.classify({messaging_gate.SOCKET_ENV_VAR: "sock"})

    assert gate.state == GateState.OPEN
    assert gate.requested is False
    assert gate.inbox_bound is True


def test_empty_socket_value_is_not_bound():
    gate = messaging_gate.classify(
        {messaging_gate.GATE_ENV_VAR: "1", messaging_gate.SOCKET_ENV_VAR: ""}
    )

    assert gate.inbox_bound is False
    assert gate.state == GateState.REQUESTED_UNBOUND


# ---------------------------------------------------------------------------
# Register discipline on the display note
# ---------------------------------------------------------------------------

def test_every_state_carries_a_note_within_the_prose_cap():
    # `docs/wiki/guard-messaging.md` § Register B5 -- the shipped cap is
    # `coordinator_core/bash_guards/_message_size.py`'s 220 bytes.
    for state in (
        GateState.NOT_REQUESTED,
        GateState.DECLINED,
        GateState.REQUESTED_UNBOUND,
        GateState.OPEN,
    ):
        note = messaging_gate._NOTES[state]
        assert note
        assert len(note.encode("utf-8")) <= 220


def test_notes_never_name_the_gate_variable_or_an_override_recipe():
    # Register B6/B8: an agent-facing message does not hand over the switch
    # it is reporting on. The variable name belongs in the module docstring,
    # which is read by an engineer, not emitted to an agent.
    for note in messaging_gate._NOTES.values():
        assert messaging_gate.GATE_ENV_VAR not in note
        assert "export " not in note
        assert "setx" not in note.lower()


# ---------------------------------------------------------------------------
# Negative-spec
# ---------------------------------------------------------------------------

def test_classify_defaults_to_the_process_environment_and_never_raises():
    gate = messaging_gate.classify()

    assert gate.state in {
        GateState.NOT_REQUESTED,
        GateState.DECLINED,
        GateState.REQUESTED_UNBOUND,
        GateState.OPEN,
    }


def test_classify_does_not_mutate_the_mapping_it_is_given():
    env = {messaging_gate.GATE_ENV_VAR: "1"}
    messaging_gate.classify(env)

    assert env == {messaging_gate.GATE_ENV_VAR: "1"}


def test_module_reads_no_registry_and_holds_no_address():
    # Negative-spec: environment only. A `harness_registry` import here would
    # make this a second, divergent reader of the session registry, and an
    # address-shaped value would put it inside `reachability`'s Anti-scope.
    source = (messaging_gate.__file__ or "")
    assert source.endswith("messaging_gate.py")

    import inspect

    body = inspect.getsource(messaging_gate)
    code = body.split('"""', 2)[-1]
    assert "harness_registry" not in code
    assert "snapshot(" not in code
    assert "messaging_available" not in code


def test_to_dict_carries_every_dataclass_field():
    import dataclasses

    gate = messaging_gate.classify({messaging_gate.GATE_ENV_VAR: "1"})
    payload = messaging_gate.to_dict(gate)

    assert set(payload) == {f.name for f in dataclasses.fields(gate)}
    assert payload["state"] == GateState.REQUESTED_UNBOUND
